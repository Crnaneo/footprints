"""Via-route reconstruction: station search, chaining, auto times and preview/save."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.models import TripCreate
from backend.store import TripStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('FOOTPRINT_DATA_DIR', str(tmp_path))
    store = TripStore()
    rail = tmp_path / 'rail'; rail.mkdir()
    monkeypatch.setattr('backend.rail_coverage.passenger_directory', lambda _: {})
    monkeypatch.setattr('backend.passenger_sections.passenger_directory', lambda _: {})
    with sqlite3.connect(rail / 'catalog.sqlite3') as db:
        db.execute('CREATE TABLE lines(id INTEGER,name TEXT,tags TEXT,ways TEXT,stations TEXT)')
        db.execute('CREATE TABLE ways(id INTEGER,nodes TEXT,geometry TEXT,tags TEXT)')
        db.execute('CREATE TABLE stations(id INTEGER,name TEXT,lon REAL,lat REAL,tags TEXT)')
        names = ['甲', '乙', '丙', '丁']
        ids = [10 + i for i in range(len(names))]
        ways = []
        for i in range(len(names) - 1):
            wid = 100 + i; ways.append(wid)
            tags = {'maxspeed': '200'} if i == 0 else {}
            db.execute('INSERT INTO ways VALUES(?,?,?,?)', (wid, json.dumps(ids[i:i + 2]),
                       json.dumps([[110 + i * .05, 35], [110 + (i + 1) * .05, 35]]), json.dumps(tags)))
        db.execute('INSERT INTO lines VALUES(?,?,?,?,?)', (1, '测试一线',
                   json.dumps({'network': '中国铁路', 'from': names[0], 'to': names[-1]}, ensure_ascii=False),
                   json.dumps(ways), json.dumps(ids)))
        for i, name in enumerate(names):
            db.execute('INSERT INTO stations VALUES(?,?,?,?,?)', (ids[i], name, 110 + i * .05, 35,
                       json.dumps({'railway': 'station', 'passenger': 'yes'})))
    with TestClient(app) as client:
        yield client, store


def test_station_search_and_lines(setup):
    client, _ = setup
    stations = client.get('/api/rail-via/stations', params={'q': '甲'}).json()['stations']
    assert stations and stations[0]['name'] == '甲' and stations[0]['id'] == 'osm:10'
    lines = client.get('/api/rail-via/station-lines', params={'station_id': 'osm:10'}).json()['lines']
    assert {'line_id': '1', 'name': '测试一线'} in lines


def test_preview_builds_geometry_and_auto_time(setup):
    client, _ = setup
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:10', 'destination_id': 'osm:12', 'rows': [{
        'origin_id': 'osm:10', 'origin_name': '甲', 'line_id': '1', 'line_name': '测试一线',
        'via_id': 'osm:12', 'via_name': '丙',
        'departure': {'mode': 'manual', 'value': '08:00'}, 'arrival': {'mode': 'auto', 'value': None}}]}
    response = client.post('/api/rail-via/preview', json=body)
    assert response.status_code == 200
    result = response.json()
    segment = result['segments'][0]
    assert segment['origin_name'] == '甲' and segment['via_name'] == '丙'
    assert segment['max_speed_kmh'] == 200 and segment['speed_source'] == 'osm'
    assert 8 < segment['distance_km'] < 11
    assert segment['departure']['time'] == '08:00' and segment['arrival']['time'].startswith('08:0')
    assert len(result['trip']['points']) >= 2
    TripCreate.model_validate(result['trip'])
    saved = client.post('/api/trips', json=result['trip'])
    assert saved.status_code == 201 and saved.json()['mode'] == 'rail'


@pytest.mark.parametrize('reverse', [False, True])
def test_via_original_curve_vertices_survive_preview_save_and_fallback(setup, reverse):
    from backend.rail_via import _LineAxis
    from backend.rail_manual import editor_index
    client, store = setup
    raw = [[110, 35], [110.02, 35.00004], [110.03, 34.99996], [110.05, 35]]
    with sqlite3.connect(store.path.parent / 'rail' / 'catalog.sqlite3') as db:
        db.execute('UPDATE ways SET geometry=? WHERE id=100', (json.dumps(raw),))
    origin, destination = ('osm:11', 'osm:10') if reverse else ('osm:10', 'osm:11')
    body = {'travel_date': '2026-10-05', 'origin_id': origin, 'destination_id': destination, 'rows': [{
        'origin_id': origin, 'origin_name': '乙' if reverse else '甲', 'line_id': '1',
        'via_id': destination, 'via_name': '甲' if reverse else '乙',
        'departure': {'mode': 'manual', 'value': '08:00'}, 'arrival': {'mode': 'auto'}}]}
    response = client.post('/api/rail-via/preview', json=body)
    assert response.status_code == 200
    draft = response.json()['trip']
    expected = list(reversed(raw)) if reverse else raw
    assert len(draft['points']) == len(expected)
    for point, want in zip(draft['points'], expected):
        assert (point['longitude'], point['latitude']) == pytest.approx(want, abs=1e-9)
    assert [p['timestamp'] for p in draft['points']] == sorted(p['timestamp'] for p in draft['points'])
    saved = client.post('/api/trips', json=draft)
    assert saved.status_code == 201
    expected_points = TripCreate.model_validate(draft).model_dump(mode='json')['points']
    assert client.get('/api/trips/' + saved.json()['id']).json()['points'] == expected_points
    # The way-chain fallback must use the same original nodes as normal axes.
    axis = _LineAxis(editor_index(store), 1)
    pieces = axis._cross_axis_pieces(origin, destination)
    assert len(pieces) == 1 and len(pieces[0][0]) == len(expected)
    for got, want in zip(pieces[0][0], expected):
        assert got == pytest.approx(want, abs=1e-9)


def test_all_auto_times_rejected(setup):
    client, _ = setup
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:10', 'destination_id': 'osm:12', 'rows': [{
        'origin_id': 'osm:10', 'origin_name': '甲', 'line_id': '1', 'line_name': '测试一线',
        'via_id': 'osm:12', 'via_name': '丙',
        'departure': {'mode': 'auto', 'value': None}, 'arrival': {'mode': 'auto', 'value': None}}]}
    assert client.post('/api/rail-via/preview', json=body).status_code == 400


def test_chained_segments_share_station(setup):
    client, _ = setup
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:10', 'destination_id': 'osm:13', 'rows': [
        {'origin_id': 'osm:10', 'origin_name': '甲', 'line_id': '1', 'line_name': '测试一线',
         'via_id': 'osm:11', 'via_name': '乙', 'departure': {'mode': 'manual', 'value': '09:00'},
         'arrival': {'mode': 'auto', 'value': None}},
        {'origin_id': 'osm:11', 'origin_name': '乙', 'line_id': '1', 'line_name': '测试一线',
         'via_id': 'osm:13', 'via_name': '丁', 'departure': {'mode': 'auto', 'value': None},
         'arrival': {'mode': 'auto', 'value': None}}]}
    result = client.post('/api/rail-via/preview', json=body).json()
    assert [s['via_name'] for s in result['segments']] == ['乙', '丁']
    # The second segment departs exactly when the first arrives (same station).
    assert result['segments'][0]['arrival']['time'] == result['segments'][1]['departure']['time']


def test_dual_manual_times_fill_by_average_speed(setup):
    client, _ = setup
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:10', 'destination_id': 'osm:12', 'rows': [
        {'origin_id': 'osm:10', 'origin_name': '甲', 'line_id': '1', 'line_name': '测试一线',
         'via_id': 'osm:11', 'via_name': '乙', 'departure': {'mode': 'manual', 'value': '08:00'},
         'arrival': {'mode': 'auto', 'value': None}},
        {'origin_id': 'osm:11', 'origin_name': '乙', 'line_id': '1', 'line_name': '测试一线',
         'via_id': 'osm:12', 'via_name': '丙', 'departure': {'mode': 'auto', 'value': None},
         'arrival': {'mode': 'manual', 'value': '10:00'}}]}
    response = client.post('/api/rail-via/preview', json=body)
    assert response.status_code == 200
    result = response.json()
    # Two equal legs between 08:00 and 10:00: the middle station falls exactly on
    # 09:00 at the observed average speed (the 200 km/h line speed would give 08:01).
    assert result['segments'][0]['arrival']['time'] == '09:00'
    assert result['segments'][1]['departure']['time'] == '09:00'
    assert result['segments'][1]['arrival']['time'] == '10:00'
    # The middle point (乙站, 09:00 Beijing time) is linearly interpolated too.
    assert result['trip']['points'][1]['timestamp'] == '2026-10-05T01:00:00Z'


def test_manual_stopover_creates_dwell(setup):
    client, _ = setup
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:10', 'destination_id': 'osm:12', 'rows': [
        {'origin_id': 'osm:10', 'origin_name': '甲', 'line_id': '1', 'line_name': '测试一线',
         'via_id': 'osm:11', 'via_name': '乙', 'departure': {'mode': 'auto', 'value': None},
         'arrival': {'mode': 'manual', 'value': '09:00'}},
        {'origin_id': 'osm:11', 'origin_name': '乙', 'line_id': '1', 'line_name': '测试一线',
         'via_id': 'osm:12', 'via_name': '丙', 'departure': {'mode': 'manual', 'value': '09:10'},
         'arrival': {'mode': 'auto', 'value': None}}]}
    response = client.post('/api/rail-via/preview', json=body)
    assert response.status_code == 200
    result = response.json()
    # Arrival 09:00 and departure 09:10 at 乙 are both honoured as a stopover;
    # the leg before rolls back at line speed (09:00 - ~82s) and the leg after
    # runs forward from 09:10.
    assert result['segments'][0]['arrival']['time'] == '09:00'
    assert result['segments'][0]['departure']['time'] == '08:58'
    assert result['segments'][1]['departure']['time'] == '09:10'
    assert result['segments'][1]['arrival']['time'] == '09:11'


def test_last_via_must_be_title_destination(setup):
    client, _ = setup
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:10', 'destination_id': 'osm:12', 'rows': [{
        'origin_id': 'osm:10', 'origin_name': '甲', 'line_id': '1', 'line_name': '测试一线',
        'via_id': 'osm:11', 'via_name': '乙',
        'departure': {'mode': 'manual', 'value': '08:00'}, 'arrival': {'mode': 'auto', 'value': None}}]}
    response = client.post('/api/rail-via/preview', json=body)
    assert response.status_code == 400 and '终到站' in response.json()['detail']


def test_manual_times_backwards_rejected(setup):
    client, _ = setup
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:10', 'destination_id': 'osm:11', 'rows': [{
        'origin_id': 'osm:10', 'origin_name': '甲', 'line_id': '1', 'line_name': '测试一线',
        'via_id': 'osm:11', 'via_name': '乙',
        'departure': {'mode': 'manual', 'value': '09:00'}, 'arrival': {'mode': 'manual', 'value': '08:00'}}]}
    response = client.post('/api/rail-via/preview', json=body)
    assert response.status_code == 400 and '冲突' in response.json()['detail']


@pytest.fixture
def setup_split(tmp_path, monkeypatch):
    """One line whose two ways model a junction crossover: their endpoints sit
    9 m apart without sharing a node, so each station lands on its own spine."""
    monkeypatch.setenv('FOOTPRINT_DATA_DIR', str(tmp_path))
    store = TripStore()
    rail = tmp_path / 'rail'; rail.mkdir()
    monkeypatch.setattr('backend.rail_coverage.passenger_directory', lambda _: {})
    monkeypatch.setattr('backend.passenger_sections.passenger_directory', lambda _: {})
    with sqlite3.connect(rail / 'catalog.sqlite3') as db:
        db.execute('CREATE TABLE lines(id INTEGER,name TEXT,tags TEXT,ways TEXT,stations TEXT)')
        db.execute('CREATE TABLE ways(id INTEGER,nodes TEXT,geometry TEXT,tags TEXT)')
        db.execute('CREATE TABLE stations(id INTEGER,name TEXT,lon REAL,lat REAL,tags TEXT)')
        db.execute('INSERT INTO ways VALUES(201,?, ?,?)', (json.dumps([1, 2]),
                   json.dumps([[110.00, 35], [110.05, 35]]), json.dumps({})))
        db.execute('INSERT INTO ways VALUES(202,?, ?,?)', (json.dumps([3, 4]),
                   json.dumps([[110.0501, 35], [110.10, 35]]), json.dumps({})))
        db.execute('INSERT INTO lines VALUES(?,?,?,?,?)', (2, '测试岔线',
                   json.dumps({'network': '中国铁路', 'from': '甲', 'to': '乙'}, ensure_ascii=False),
                   json.dumps([201, 202]), json.dumps([11, 12])))
        db.execute('INSERT INTO stations VALUES(?,?,?,?,?)', (11, '甲', 110.00, 35,
                   json.dumps({'railway': 'station', 'passenger': 'yes'})))
        db.execute('INSERT INTO stations VALUES(?,?,?,?,?)', (12, '乙', 110.10, 35,
                   json.dumps({'railway': 'station', 'passenger': 'yes'})))
    with TestClient(app) as client:
        yield client, store


def test_split_spine_stations_reconnect_through_junction_bridge(setup_split):
    client, _ = setup_split
    lines = client.get('/api/rail-via/station-lines', params={'station_id': 'osm:12'}).json()['lines']
    assert {'line_id': '2', 'name': '测试岔线'} in lines
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:11', 'destination_id': 'osm:12', 'rows': [{
        'origin_id': 'osm:11', 'origin_name': '甲', 'line_id': '2', 'line_name': '测试岔线',
        'via_id': 'osm:12', 'via_name': '乙',
        'departure': {'mode': 'manual', 'value': '08:00'}, 'arrival': {'mode': 'auto', 'value': None}}]}
    response = client.post('/api/rail-via/preview', json=body)
    assert response.status_code == 200
    segment = response.json()['segments'][0]
    # 5 km main run + bridged crossover + 4.5 km branch.
    assert 8 < segment['distance_km'] < 11
    TripCreate.model_validate(response.json()['trip'])


@pytest.fixture
def setup_broken(tmp_path, monkeypatch):
    """One line whose relation misses a whole link: a ~450 m gap between two
    ways exceeds the 120 m junction bridge, so the run must be split."""
    monkeypatch.setenv('FOOTPRINT_DATA_DIR', str(tmp_path))
    store = TripStore()
    rail = tmp_path / 'rail'; rail.mkdir()
    monkeypatch.setattr('backend.rail_coverage.passenger_directory', lambda _: {})
    monkeypatch.setattr('backend.passenger_sections.passenger_directory', lambda _: {})
    with sqlite3.connect(rail / 'catalog.sqlite3') as db:
        db.execute('CREATE TABLE lines(id INTEGER,name TEXT,tags TEXT,ways TEXT,stations TEXT)')
        db.execute('CREATE TABLE ways(id INTEGER,nodes TEXT,geometry TEXT,tags TEXT)')
        db.execute('CREATE TABLE stations(id INTEGER,name TEXT,lon REAL,lat REAL,tags TEXT)')
        db.execute('INSERT INTO ways VALUES(201,?,?,?)', (json.dumps([1, 2]),
                   json.dumps([[110.00, 35], [110.03, 35]]), json.dumps({})))
        db.execute('INSERT INTO ways VALUES(202,?,?,?)', (json.dumps([3, 4]),
                   json.dumps([[110.035, 35], [110.10, 35]]), json.dumps({})))
        db.execute('INSERT INTO lines VALUES(?,?,?,?,?)', (2, '测试断线',
                   json.dumps({'network': '中国铁路', 'from': '甲', 'to': '乙'}, ensure_ascii=False),
                   json.dumps([201, 202]), json.dumps([11, 12])))
        db.execute('INSERT INTO stations VALUES(?,?,?,?,?)', (11, '甲', 110.00, 35,
                   json.dumps({'railway': 'station', 'passenger': 'yes'})))
        db.execute('INSERT INTO stations VALUES(?,?,?,?,?)', (12, '乙', 110.10, 35,
                   json.dumps({'railway': 'station', 'passenger': 'yes'})))
    with TestClient(app) as client:
        yield client, store


def test_disconnected_line_splits_into_pieces(setup_broken):
    client, _ = setup_broken
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:11', 'destination_id': 'osm:12', 'rows': [{
        'origin_id': 'osm:11', 'origin_name': '甲', 'line_id': '2', 'line_name': '测试断线',
        'via_id': 'osm:12', 'via_name': '乙',
        'departure': {'mode': 'manual', 'value': '08:00'}, 'arrival': {'mode': 'auto', 'value': None}}]}
    response = client.post('/api/rail-via/preview', json=body)
    assert response.status_code == 200
    payload = response.json()
    # 2.74 km + 5.93 km pieces; the ~450 m missing link is not drawn or counted.
    segment = payload['segments'][0]
    assert 8.5 < segment['distance_km'] < 8.9
    assert 'via_gap_split' in payload['trip']['quality_flags']
    points = payload['trip']['points']
    ids = {p['segment_id'] for p in points}
    assert ids == {'0', '0/1'}
    # The timeline stays continuous across the gap: the last point of piece 0
    # and the first point of piece 0/1 share the same instant.
    piece0_last = [p for p in points if p['segment_id'] == '0'][-1]
    piece1_first = [p for p in points if p['segment_id'] == '0/1'][0]
    assert piece0_last['timestamp'] == piece1_first['timestamp']
    TripCreate.model_validate(payload['trip'])
    saved = client.post('/api/trips', json=payload['trip'])
    assert saved.status_code == 201
    record = client.get('/api/trips/' + saved.json()['id']).json()
    assert {p['segment_id'] for p in record['points']} == ids
    assert 8.5 < record['distance_km'] < 8.9


def test_connected_rows_keep_all_edges_and_dwell_times(setup):
    client, _ = setup
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:10', 'destination_id': 'osm:12', 'rows': [
        {'origin_id': 'osm:10', 'origin_name': '甲', 'line_id': '1', 'via_id': 'osm:11', 'via_name': '乙',
         'departure': {'mode': 'manual', 'value': '08:00'}, 'arrival': {'mode': 'manual', 'value': '09:00'}},
        {'origin_id': 'osm:11', 'origin_name': '乙', 'line_id': '1', 'via_id': 'osm:12', 'via_name': '丙',
         'departure': {'mode': 'manual', 'value': '09:10'}, 'arrival': {'mode': 'manual', 'value': '10:00'}}]}
    result = client.post('/api/rail-via/preview', json=body).json()
    points = result['trip']['points']
    assert [(p['longitude'], p['segment_id']) for p in points] == [(110, '0'), (110.05, '0'), (110.05, '1'), (110.1, '1')]
    assert [p['timestamp'] for p in points] == ['2026-10-05T00:00:00Z', '2026-10-05T01:00:00Z',
                                              '2026-10-05T01:10:00Z', '2026-10-05T02:00:00Z']
    saved = client.post('/api/trips', json=result['trip'])
    assert saved.status_code == 201
    assert 9 < saved.json()['distance_km'] < 9.2


def test_gap_between_rows_excludes_missing_geometry_and_keeps_time(setup, monkeypatch):
    from backend import rail_via
    client, _ = setup
    class Axis:
        def segment(self, start, end):
            coords = [(110, 35), (110.04, 35)] if start == 'osm:10' else [(110.06, 35), (110.1, 35)]
            return [(coords, rail_via._haversine(*coords))]
    monkeypatch.setattr(rail_via, '_axis', lambda *args: Axis())
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:10', 'destination_id': 'osm:12', 'rows': [
        {'origin_id': 'osm:10', 'origin_name': '甲', 'line_id': '1', 'via_id': 'osm:11', 'via_name': '乙',
         'departure': {'mode': 'manual', 'value': '23:00'}, 'arrival': {'mode': 'auto'}},
        {'origin_id': 'osm:11', 'origin_name': '乙', 'line_id': '1', 'via_id': 'osm:12', 'via_name': '丙',
         'departure': {'mode': 'auto'}, 'arrival': {'mode': 'manual', 'value': '01:00+1'}}]}
    response = client.post('/api/rail-via/preview', json=body)
    assert response.status_code == 200
    result = response.json()
    assert 'via_gap_split' in result['trip']['quality_flags']
    points = result['trip']['points']
    assert points[1]['timestamp'] == points[2]['timestamp'] == '2026-10-05T16:00:00Z'
    saved = client.post('/api/trips', json=result['trip'])
    assert saved.status_code == 201
    assert 7.2 < saved.json()['distance_km'] < 7.4


@pytest.mark.parametrize('reverse', [False, True])
def test_disconnected_components_clip_to_interior_stations(setup_broken, reverse):
    from backend.rail_via import _LineAxis
    from backend.rail_coverage import RailIndex
    _, store = setup_broken
    # Stations need not coincide with way endpoints. The split must retain
    # just the chosen span in both directions, excluding track beyond them.
    index = RailIndex(store.path.parent / 'rail' / 'catalog.sqlite3')
    axis = _LineAxis(index, 2)
    from backend.rail_coverage import project
    axis.station_xy['osm:11'] = project(110.01, 35)
    axis.station_xy['osm:12'] = project(110.09, 35)
    start, end = ('osm:12', 'osm:11') if reverse else ('osm:11', 'osm:12')
    pieces = axis._cross_axis_pieces(start, end)
    assert len(pieces) == 2
    assert pieces[0][0][0] == pytest.approx((110.09 if reverse else 110.01, 35))
    assert pieces[-1][0][-1] == pytest.approx((110.01 if reverse else 110.09, 35))


def test_next_day_manual_time(setup):
    client, _ = setup
    body = {'travel_date': '2026-10-05', 'origin_id': 'osm:10', 'destination_id': 'osm:13', 'rows': [{
        'origin_id': 'osm:10', 'origin_name': '甲', 'line_id': '1', 'line_name': '测试一线',
        'via_id': 'osm:13', 'via_name': '丁',
        'departure': {'mode': 'manual', 'value': '23:40+1'}, 'arrival': {'mode': 'auto', 'value': None}}]}
    response = client.post('/api/rail-via/preview', json=body)
    assert response.status_code == 200
    points = response.json()['trip']['points']
    # 23:40+1 CST = 15:40Z on the next day; the tail keeps rolling forward.
    assert points[0]['timestamp'].startswith('2026-10-06T15:40')
    assert points[-1]['timestamp'] > points[0]['timestamp']
