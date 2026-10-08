import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend import flight_reconstruction as fr
from backend import flight_local as fl
from backend import providers as p
from backend.app import app
from backend.flight_local import recover_local_flight
from backend.models import TripCreate
from backend.store import TripStore

DAY = date(2025, 4, 3)
START = datetime(2025, 4, 3, 2, tzinfo=timezone.utc)
AIRPORTS = {
    'AAA': {'icao': 'KAAA', 'iata': 'AAA', 'lat': 30, 'lon': 110, 'tz': 'Asia/Shanghai'},
    'BBB': {'icao': 'KBBB', 'iata': 'BBB', 'lat': 30, 'lon': 120, 'tz': 'Asia/Shanghai'},
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    async def no_schedule(*args, **kwargs):return {}
    monkeypatch.setattr(fr, 'lookup_dated_flight', no_schedule)
    monkeypatch.setenv('FOOTPRINT_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('FOOTPRINT_SKIP_WARMUP', '1')
    monkeypatch.setattr(p, '_airports', lambda: AIRPORTS)
    monkeypatch.setattr(fr, 'load_navaids', lambda: {})
    monkeypatch.setattr(fr, 'load_runways', lambda: {})
    return TripStore()


def gps(store, start=START, day=DAY, **changes):
    # Long ground travel on both sides must not become part of the flight.
    points = []
    for i in range(15):
        airborne = 2 <= i <= 12
        points.append({'longitude': 110 + min(10, max(0, i - 2)), 'latitude': 30,
                       'timestamp': (start + timedelta(minutes=i * 10)).isoformat(),
                       'altitude_m': 8000 if airborne else 50, 'speed_kmh': 700 if airborne else 20})
    return store.create(TripCreate(title='original GPS', mode='other', travel_date=day, source='csv',
                                  geometry_kind='observed', points=points, **changes))


def empty_flight(store):
    return store.create(TripCreate(title='AB123', mode='flight', travel_date=DAY,
                                  service_number='AB123', origin='AAA', destination='BBB',
                                  geometry_kind='scheduled', points=[], metadata={'flight': {'registration': 'N123AB', 'icao24': 'aabbcc'}}))


def test_recovers_observations_without_hex_and_preserves_missing_measurements(store):
    original = gps(store)
    def missing(r):
        r['points'][7]['speed_kmh'] = None
        r['points'][8]['altitude_m'] = None
        return r
    store.update_record(original['id'], missing)
    result = recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB')
    assert len(result['points']) == 11
    assert result['points'][5]['speed_kmh'] is None
    assert result['points'][6]['altitude_m'] is None
    assert result['metadata']['altitude_kind'] != 'barometric_pressure'
    assert result['metadata']['flight']['times_inferred'] is True
    assert result['metadata']['local_recovery']['source_trip_ids'] == [original['id']]
    assert store.get(original['id'])['point_count'] == 15


def test_requires_both_airports_and_rejects_reverse_direction_and_train(store):
    gps(store)
    assert recover_local_flight(store, 'AB123', DAY, None, 'BBB') is None
    assert recover_local_flight(store, 'AB123', DAY, 'BBB', 'AAA') is None
    assert recover_local_flight(store, 'AB123', DAY + timedelta(days=1), 'AAA', 'BBB') is None
    def train(r):
        for point in r['points']:
            point['speed_kmh'] = 300
        return r
    store.update_record(store.list()[0]['id'], train)
    assert recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB') is None


def test_multiple_legs_require_selection_and_stale_selection_fails(store):
    gps(store)
    gps(store, start=START + timedelta(hours=6))
    with pytest.raises(p.ProviderError) as exc:
        recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB')
    assert exc.value.status_code == 409
    candidates = exc.value.details['candidates']
    assert len(candidates) == 2
    result = recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB', candidates[1]['local_candidate_id'])
    assert result['points'][0]['timestamp'].startswith('2025-04-03T08:20')
    with pytest.raises(p.ProviderError):
        recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB', 'stale')
    hinted = recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB', departure_time='10:20')
    assert hinted['points'][0]['timestamp'].startswith('2025-04-03T02:20')


def test_excludes_sandbox_and_derived_copies(store):
    gps(store, metadata={'sandbox': True})
    assert recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB') is None
    gps(store, metadata={'local_recovery': {'source_trip_ids': ['deleted']}})
    assert recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB') is None


def test_matches_local_departure_date_across_utc_midnight(store, monkeypatch):
    monkeypatch.setitem(AIRPORTS['AAA'], 'tz', 'America/Los_Angeles')
    original = gps(store, start=datetime(2025, 4, 4, 2, tzinfo=timezone.utc), day=DAY + timedelta(days=1))
    result = recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB')
    assert result['travel_date'] == DAY.isoformat()
    assert result['metadata']['local_recovery']['source_trip_ids'] == [original['id']]


def test_preview_is_offline_and_recovery_updates_same_id_and_map(store, monkeypatch):
    original = gps(store)
    target = empty_flight(store)
    async def network_forbidden(**kwargs):
        raise AssertionError('local recovery must not require an online source')
    monkeypatch.setattr(fr, 'fetch_flight_unified', network_forbidden)
    body = {'flight_number': 'AB123', 'travel_date': DAY.isoformat(), 'origin': 'AAA', 'destination': 'BBB',
            'track_source': 'local', 'existing_trip_id': target['id']}
    with TestClient(app) as client:
        preview = client.post('/api/preview/flight', json=body)
        assert preview.status_code == 200, preview.text
        assert store.get(target['id'])['points'] == []
        candidate = preview.json()['trip']['metadata']['local_recovery']['candidate_id']
        result = client.post(f"/api/flight/{target['id']}/recover", json={**body, 'local_candidate_id': candidate})
        assert result.status_code == 200, result.text
        record = result.json()
        assert record['id'] == target['id'] and record['created_at'] == target['created_at']
        assert record['point_count'] == 11
        assert record['metadata']['flight']['icao24'] == 'aabbcc'
        assert store.get(original['id'])['point_count'] == 15
        overview = next(r for r in store.overview() if r['id'] == target['id'])
        assert len(overview['points']) == 11 and overview['bounds'] is not None
        repeat = client.post(f"/api/flight/{target['id']}/recover", json={**body, 'local_candidate_id': candidate})
        assert repeat.status_code == 409


def test_preview_candidate_expires_when_source_measurements_change(store):
    original = gps(store)
    draft = recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB')
    candidate = draft['metadata']['local_recovery']['candidate_id']
    def edit(r):
        r['points'][5]['altitude_m'] += 100
        return r
    store.update_record(original['id'], edit)
    with pytest.raises(p.ProviderError) as exc:
        recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB', candidate)
    assert exc.value.status_code == 409


AUTO_IMPORT = {'flight_number': 'AB123', 'travel_date': DAY.isoformat(), 'origin': 'AAA', 'destination': 'BBB'}


def test_online_observations_win_before_local_lookup(store, monkeypatch):
    gps(store)
    online = recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB')
    online['source'] = 'adsb.lol'
    del online['metadata']['local_recovery']
    async def fetch(**kwargs):
        assert kwargs['icao24'] == 'aabbcc'
        assert kwargs['first_seen'] == int((START + timedelta(minutes=20)).timestamp())
        return online
    def local_forbidden(*args):
        raise AssertionError('usable online observations must win before local GPS')
    monkeypatch.setattr(fr, 'fetch_flight_unified', fetch)
    monkeypatch.setattr(fl, 'recover_local_flight', local_forbidden)
    with TestClient(app) as client:
        response = client.post('/api/preview/flight', json={**AUTO_IMPORT, 'icao24': 'aabbcc', 'departure_time': '10:20'})
    assert response.status_code == 200, response.text
    assert response.json()['trip']['source'] == 'adsb.lol'
    assert len(store.list()) == 1  # preview must not save


@pytest.mark.parametrize('status', [402, 403, 404, 429, 502, 503, 504])
def test_online_unavailable_falls_back_to_local_gps(store, monkeypatch, status):
    gps(store)
    events = []
    async def unavailable(**kwargs):
        events.append('online')
        raise p.ProviderError(status, 'unavailable')
    def recover(*args):
        events.append('local')
        return recover_local_flight(*args)
    monkeypatch.setattr(fr, 'fetch_flight_unified', unavailable)
    monkeypatch.setattr(fl, 'recover_local_flight', recover)
    with TestClient(app) as client:
        response = client.post('/api/preview/flight', json=AUTO_IMPORT)
    assert response.status_code == 200, response.text
    assert response.json()['trip']['metadata']['local_recovery']
    assert events == ['online', 'local']
    assert len(store.list()) == 1


@pytest.mark.parametrize('status', [409, 422])
def test_online_ambiguity_and_invalid_input_require_correction(store, monkeypatch, status):
    gps(store)
    async def invalid(**kwargs):
        raise p.ProviderError(status, 'needs correction')
    def local_forbidden(*args):
        raise AssertionError('input errors must not silently fall back')
    monkeypatch.setattr(fr, 'fetch_flight_unified', invalid)
    monkeypatch.setattr(fl, 'recover_local_flight', local_forbidden)
    with TestClient(app) as client:
        response = client.post('/api/preview/flight', json=AUTO_IMPORT)
    assert response.status_code == status


@pytest.mark.parametrize('online_kind', ['unavailable', 'route_only', 'single_point'])
def test_no_actual_online_track_and_no_gps_returns_no_data_without_saving(store, monkeypatch, online_kind):
    async def missing(**kwargs):
        if online_kind == 'unavailable':
            raise p.ProviderError(404, 'no observations')
        # Even an unexpected legacy adapter result cannot create a placeholder.
        return {'geometry_kind': 'scheduled', 'origin': 'AAA', 'destination': 'BBB',
                'points': [] if online_kind == 'route_only' else [{'longitude': 110, 'latitude': 30}]}
    monkeypatch.setattr(fr, 'fetch_flight_unified', missing)
    with TestClient(app) as client:
        response = client.post('/api/preview/flight', json=AUTO_IMPORT)
    assert response.status_code == 404, response.text
    assert '无数据' in response.json()['detail']
    assert store.list() == []


def test_local_candidate_confirmation_skips_online_retry(store, monkeypatch):
    gps(store)
    gps(store, start=START + timedelta(hours=6))
    online_calls = []
    async def missing(**kwargs):
        online_calls.append(kwargs)
        raise p.ProviderError(404, 'no observations')
    monkeypatch.setattr(fr, 'fetch_flight_unified', missing)
    with TestClient(app) as client:
        response = client.post('/api/preview/flight', json=AUTO_IMPORT)
        assert response.status_code == 409, response.text
        candidate = response.json()['details']['candidates'][1]['local_candidate_id']
        selected = client.post('/api/preview/flight', json={**AUTO_IMPORT, 'local_candidate_id': candidate})
    assert selected.status_code == 200, selected.text
    assert len(online_calls) == 1
    assert len(store.list()) == 2


def test_no_track_preview_returns_aircraft_info_without_creating_trip(store, monkeypatch):
    info = {'registration': 'N123AB', 'icao24': 'abcdef', 'aircraft_type': 'Example type',
            'photo_url': 'https://example.com/plane.jpg'}
    async def missing(**kwargs):
        raise p.ProviderError(404, 'track missing', {'aircraft_info': info})
    monkeypatch.setattr(fr, 'fetch_flight_unified', missing)
    with TestClient(app) as client:
        response = client.post('/api/preview/flight', json={**AUTO_IMPORT, 'registration': 'N123AB'})
    assert response.status_code == 200
    assert response.json()['trip']['points'] == []
    assert response.json()['trip']['metadata']['information_only']
    result = response.json()['trip']['metadata']['flight']
    assert {key: result[key] for key in info} == info
    assert result['flight_number'] == AUTO_IMPORT['flight_number']
    assert result['departure_airport'] == AUTO_IMPORT['origin']
    assert store.list() == []


def test_enrich_existing_aircraft_does_not_require_tracks_or_change_geometry(store, monkeypatch):
    original = gps(store)
    target = empty_flight(store)
    async def aircraft(registration=None, icao24=None):
        assert registration == 'N123AB'
        return {'registration': 'N123AB', 'icao24': 'aabbcc', 'aircraft_type': 'Example type',
                'photo_url': 'https://example.com/plane.jpg'}
    async def no_tracks(*args, **kwargs):
        raise AssertionError('aircraft enrichment must be independent of historical flights')
    monkeypatch.setattr(fr, 'lookup_aircraft_info', aircraft)
    monkeypatch.setattr(fr, 'fetch_flight_reconstruction', no_tracks)
    with TestClient(app) as client:
        response = client.post('/api/flight/enrich', json={**AUTO_IMPORT, 'trip_id': target['id'], 'registration': 'N123AB'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['metadata']['flight']['photo_url'] == 'https://example.com/plane.jpg'
    assert result['points'] == target['points'] and result['stats'] == target['stats']
    assert result['geometry_kind'] == target['geometry_kind']
    assert result['id'] == target['id'] and result['created_at'] == target['created_at']
    assert store.get(original['id']) == original
    assert len(store.list()) == 2


def test_correction_clears_previous_airframe_photo_and_type(store, monkeypatch):
    target = empty_flight(store)
    def old_photo(record):
        record['metadata']['flight'].update({'photo_url': 'old.jpg', 'aircraft_type': 'Old type'})
        return record
    store.update_record(target['id'], old_photo)
    async def aircraft(registration=None, icao24=None):
        return {'registration': 'N456CD'}
    monkeypatch.setattr(fr, 'lookup_aircraft_info', aircraft)
    result = asyncio.run(fr.enrich_trip(store, target['id'], 'AB123', DAY, registration='N456CD'))
    assert result['metadata']['flight'] == {'registration': 'N456CD', 'flight_number': 'AB123'}
    assert result['points'] == []


def test_logger_segments_do_not_hide_one_flight_but_remain_disconnected(store):
    original = gps(store)
    def fragmented(record):
        for i, point in enumerate(record['points']):
            point['segment_id'] = 'first' if i <= 7 else 'second'
        return record
    store.update_record(original['id'], fragmented)
    result = recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB')
    assert len(result['points']) == 11
    assert len({p['segment_id'] for p in result['points']}) == 2
    assert 'partial_observed_track' in result['quality_flags']
    assert result['metadata']['flight']['track_stats']['flown_distance_km'] < 950
    assert store.get(original['id'])['point_count'] == 15


def test_cruise_reception_hole_preserves_points_and_does_not_draw_or_count_gap(store):
    original = gps(store)
    def missing_hour(record):
        for i, point in enumerate(record['points']):
            if i > 7:
                point['timestamp'] = (START + timedelta(minutes=i * 10 + 60)).isoformat()
        return record
    source = store.update_record(original['id'], missing_hour)
    result = recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB')
    assert len(result['points']) == 11
    assert [(p['longitude'], p['latitude'], p['timestamp']) for p in result['points']] == [
        (p['longitude'], p['latitude'], p['timestamp']) for p in source['points'][2:13]]
    assert result['points'][5]['segment_id'] != result['points'][6]['segment_id']
    assert result['metadata']['flight']['track_stats']['flown_distance_km'] < 950
    assert 'partial_observed_track' in result['quality_flags']


def test_recorded_ground_stop_prevents_joining_separate_flights(store):
    original = gps(store)
    def intermediate_stop(record):
        for i in (7, 8, 9):
            record['points'][i]['speed_kmh'] = 20
            record['points'][i]['altitude_m'] = 50
        return record
    store.update_record(original['id'], intermediate_stop)
    assert recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB') is None


def test_excessive_cruise_hole_does_not_join_unrelated_observations(store):
    original = gps(store)
    def excessive_gap(record):
        for i, point in enumerate(record['points']):
            if i > 7:
                point['timestamp'] = (START + timedelta(minutes=i * 10 + 180)).isoformat()
        return record
    store.update_record(original['id'], excessive_gap)
    assert recover_local_flight(store, 'AB123', DAY, 'AAA', 'BBB') is None


@pytest.mark.parametrize('has_gps', [False, True])
def test_fr24_then_adsb_then_local_keeps_metadata(store, monkeypatch, has_gps):
    monkeypatch.setenv('FR24_HISTORY_DAYS', '1096')
    if has_gps:
        gps(store)
    events = []
    monkeypatch.setenv('FR24_API_TOKEN', 'test-local-secret')
    async def fr24(*args, **kwargs):
        events.append('fr24')
        raise p.ProviderError(404, 'track missing', {'fr24_flight': {
            'origin': 'AAA', 'destination': 'BBB', 'departure': START.isoformat(),
            'arrival': (START + timedelta(hours=2)).isoformat(), 'icao24': 'aabbcc',
            'summary': {'callsign': 'TST123', 'reg': 'N123AB', 'type': 'A320'}}})
    async def registry(registration=None, icao24=None):
        return {'icao24': 'aabbcc', 'registration': 'N123AB', 'photo_url': 'https://example.com/plane.jpg'}
    async def adsb(*args, **kwargs):
        events.append('adsb')
        raise p.ProviderError(404, 'no observations')
    def local(*args, **kwargs):
        events.append('local')
        return recover_local_flight(*args, **kwargs)
    monkeypatch.setattr(fr, 'fetch_flight', fr24)
    monkeypatch.setattr(fr, 'lookup_aircraft_info', registry)
    monkeypatch.setattr(fr, 'fetch_flight_reconstruction', adsb)
    monkeypatch.setattr(fl, 'recover_local_flight', local)
    with TestClient(app) as client:
        response = client.post('/api/preview/flight', json=AUTO_IMPORT)
    assert events == ['fr24', 'adsb', 'local']
    assert response.status_code == 200, response.text
    info = response.json()['trip']['metadata']['flight']
    assert info['flight_number'] == 'AB123' and info['callsign'] == 'TST123'
    assert info['registration'] == 'N123AB' and info['photo_url']
    assert len(store.list()) == (1 if has_gps else 0)


def test_enrich_without_airframe_hint_can_save_summary_despite_missing_track(store, monkeypatch):
    target = store.create(TripCreate(title='old flight', mode='flight', travel_date=DAY,
                         service_number='AB123', origin='AAA', destination='BBB', points=[]))
    async def no_tracks(*args, **kwargs):
        raise p.ProviderError(404, 'no observations', {'flight_info': {
            'flight_number': 'AB123', 'callsign': 'TST123', 'registration': 'N456CD',
            'icao24': '112233', 'photo_url': 'https://example.com/plane.jpg'}})
    monkeypatch.setattr(fr, 'fetch_flight_unified', no_tracks)
    with TestClient(app) as client:
        response = client.post('/api/flight/enrich', json={**AUTO_IMPORT, 'trip_id': target['id']})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['metadata']['flight']['registration'] == 'N456CD'
    assert result['metadata']['flight']['photo_url']
    assert result['points'] == target['points'] and result['stats'] == target['stats']
    assert result['id'] == target['id'] and len(store.list()) == 1


def test_information_only_preview_can_be_saved_without_geometry(store, monkeypatch):
    async def missing(**kwargs):
        raise p.ProviderError(404, 'no observations', {'flight_info': {
            'registration': 'N123AB', 'aircraft_type': 'A320', 'photo_url': 'https://example.com/plane.jpg',
            'track_stats': {'flown_distance_km': 99}, 'route_waypoints': [{'ident': 'FAKE'}]}})
    monkeypatch.setattr(fr, 'fetch_flight_unified', missing)
    with TestClient(app) as client:
        draft = client.post('/api/preview/flight', json=AUTO_IMPORT).json()['trip']
        assert store.list() == []
        assert draft['metadata']['information_only'] and draft['points'] == []
        assert 'track_stats' not in draft['metadata']['flight']
        assert 'route_waypoints' not in draft['metadata']['flight']
        response = client.post('/api/trips', json=draft)
    assert response.status_code == 201, response.text
    saved = store.get(response.json()['id'])
    assert saved['points'] == [] and saved['point_count'] == 0 and saved['distance_km'] == 0
    assert saved['metadata']['flight']['photo_url']
    from backend.map_points import register_view
    assert register_view(store, [saved['id']])['point_count'] == 0


@pytest.mark.parametrize('has_track', [False, True])
def test_information_only_update_keeps_existing_identity_and_track(store, monkeypatch, has_track):
    original = empty_flight(store)
    if has_track:
        def observed(record):
            record.update(source='adsb.lol', points=[{'longitude': 110, 'latitude': 30, 'segment_id': '0', 'sequence': 0},
                                                    {'longitude': 111, 'latitude': 30, 'segment_id': '0', 'sequence': 1}], point_count=2)
            return record
        original = store.update_record(original['id'], observed)
    async def missing(**kwargs):
        raise p.ProviderError(404, 'no observations', {'flight_info': {
            'registration': 'N123AB', 'icao24': 'aabbcc', 'photo_url': 'https://example.com/new.jpg'}})
    monkeypatch.setattr(fr, 'fetch_flight_unified', missing)
    with TestClient(app) as client:
        draft = client.post('/api/preview/flight', json=AUTO_IMPORT).json()['trip']
        assert draft['metadata']['information_update']['trip_id'] == original['id']
        assert 'online_update' not in draft['metadata']
        response = client.post(f"/api/flight/{original['id']}/information", json=draft)
        assert response.status_code == 200, response.text
        assert client.post(f"/api/flight/{original['id']}/information", json=draft).status_code == 409
    saved = store.get(original['id'])
    assert len(store.list()) == 1
    for key in ('id', 'created_at', 'points', 'point_count', 'distance_km', 'stats', 'notes'):
        assert saved[key] == original[key]
    assert saved['metadata']['flight']['photo_url'] == 'https://example.com/new.jpg'
    if has_track:
        assert saved['source'] == original['source']
        assert not saved['metadata'].get('information_only')


def test_schedule_preparation_returns_pending_without_saving(store, monkeypatch):
    monkeypatch.delenv('FR24_API_TOKEN', raising=False)
    async def pending(*args, **kwargs):
        raise p.ProviderError(202, 'preparing', {'quarter':'2026 Q1', 'received_bytes':1,
            'total_bytes':100, 'status_path':'/api/flight/schedules/status?name=test'})
    monkeypatch.setattr(fr, 'lookup_dated_flight', pending)
    with TestClient(app) as client:
        response = client.post('/api/preview/flight', json={'flight_number':'UA123','travel_date':'2026-02-08'})
    assert response.status_code == 202
    assert response.json()['lookup_pending']['quarter'] == '2026 Q1'
    assert store.list() == []
