from copy import deepcopy

from fastapi.testclient import TestClient

from backend import providers
from backend.app import app
from backend.flight_timezones import with_flight_timezones
from backend.models import TripCreate
from backend.store import TripStore


def test_airport_zones_support_legacy_fields_and_actual_arrival(monkeypatch):
    monkeypatch.setattr(providers, '_airports', lambda: {
        'AAA': {'tz': 'Asia/Tokyo'}, 'KBBB': {'tz': 'America/Los_Angeles'},
        'CCC': {'tz': 'Australia/Sydney'}})
    record = {'mode': 'flight', 'origin': 'AAA', 'destination': 'KBBB',
              'metadata': {'flight': {'summary': {'dest_icao_actual': 'CCC'}}}}
    original = deepcopy(record)
    info = with_flight_timezones(record)['metadata']['flight']
    assert info['departure_timezone'] == 'Asia/Tokyo'
    assert info['arrival_timezone'] == 'Australia/Sydney'
    assert record == original
    record['metadata']['flight'] = {'origin': 'AAA', 'destination': 'KBBB'}
    assert with_flight_timezones(record)['metadata']['flight']['arrival_timezone'] == 'America/Los_Angeles'


def test_missing_airports_do_not_reuse_departure_zone_or_machine_zone(monkeypatch):
    monkeypatch.setattr(providers, '_airports', lambda: {'AAA': {'tz': 'Asia/Tokyo'}})
    record = {'mode': 'flight', 'origin': 'AAA', 'destination': 'UNKNOWN', 'metadata': {'flight': {}}}
    info = with_flight_timezones(record)['metadata']['flight']
    assert info['departure_timezone'] == 'Asia/Tokyo'
    assert info['arrival_timezone'] is None


def test_existing_flight_read_adds_zones_without_rewriting_record(tmp_path, monkeypatch):
    monkeypatch.setenv('FOOTPRINT_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('FOOTPRINT_SKIP_WARMUP', '1')
    monkeypatch.setattr(providers, '_airports', lambda: {
        'AAA': {'tz': 'Asia/Tokyo'}, 'BBB': {'tz': 'America/Los_Angeles'}})
    store = TripStore()
    saved = store.create(TripCreate(title='old flight', mode='flight', travel_date='2026-02-08',
        origin='AAA', destination='BBB', points=[], metadata={'flight': {'takeoff_time': '2026-02-08T14:00:00Z'}}))
    with TestClient(app) as client:
        for url in (f"/api/trips/{saved['id']}", '/api/overview', '/api/trips'):
            result = client.get(url).json()
            info = (result[0] if isinstance(result, list) else result)['metadata']['flight']
            assert info['departure_timezone'] == 'Asia/Tokyo'
            assert info['arrival_timezone'] == 'America/Los_Angeles'
            assert info['takeoff_time'] == '2026-02-08T14:00:00Z'
    assert store.get(saved['id']) == saved
