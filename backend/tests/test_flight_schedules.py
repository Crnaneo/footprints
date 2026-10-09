import asyncio
import json
import subprocess
import sys
from datetime import date

import duckdb
import httpx
import pytest

from backend import flight_schedules as fs, flight_reconstruction as fr
from backend.providers import ProviderError

DAY = date(2026, 2, 8)


def test_download_lock_excludes_other_process_and_releases_on_close(tmp_path):
    path = tmp_path / 'download.lock'
    code = '''
import sys
from backend.flight_schedules import _lock_download
with open(sys.argv[1], 'a+b') as lock:
    try:
        _lock_download(lock)
    except BlockingIOError:
        sys.exit(2)
'''
    def attempt():
        return subprocess.run([sys.executable, '-c', code, str(path)],
                              capture_output=True, text=True, timeout=30)
    with path.open('a+b') as lock:
        fs._lock_download(lock)
        result = attempt()
        assert result.returncode == 2, result.stderr
    result = attempt()
    assert result.returncode == 0, result.stderr


def test_cache_json_preserves_unicode(tmp_path):
    path = tmp_path / 'cache.json'
    value = {'airline': '中国航空 ✈️'}
    fs._write(path, value)
    assert json.loads(path.read_bytes().decode('utf-8')) == value
    assert fs._read(path) == value


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('FOOTPRINT_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('FR24_API_TOKEN', raising=False)


def row(**changes):
    return {'ICAO_Hex': 'icaohex-a1b2c3', 'Reg': 'N123AB', 'Callsign': 'UAL123', 'Airline': 'UAL',
            'AC_Type': 'A320', 'AC_Type_Description': 'Airbus A320',
            'Track_Origin_FL_Ft': 'ground', 'Track_Origin_DateTime_UTC': '2026-02-09 02:00:00',
            'Track_Origin_ApplicableAirports': "['KLAX']", 'Track_Destination_FL_Ft': 'ground',
            'Track_Destination_DateTime_UTC': '2026-02-09 07:00:00',
            'Track_Destination_ApplicableAirports': "['KJFK']", **changes}


@pytest.mark.parametrize('flight,parts', [('MU5105', ('MU','5105')), ('3U8881', ('3U','8881')),
                                         ('U2123', ('U2','123')), ('6E123', ('6E','123')),
                                         ('9C0123A', ('9C','0123A')), ('HVN787', ('HVN','787'))])
def test_flight_parsing(flight, parts):
    assert fs.split_flight(flight) == parts


def test_mixed_prefix_also_fixed_for_opensky():
    assert fr.callsign_candidates('3U8881') == ['CSC8881', '3U8881']
    assert fr.callsign_candidates('6E123') == ['IGO123', '6E123']


def test_local_departure_date_and_actual_hex_format():
    result = fs.candidates_from_rows([row()], 'UA123', DAY)
    assert len(result) == 1
    info = result[0]['flight_info']
    assert info['registration'] == 'N123AB' and info['icao24'] == 'a1b2c3'
    assert info['departure_airport'] == 'KLAX' and info['arrival_airport'] == 'KJFK'
    assert result[0]['date_confirmed']
    assert info['times_inferred']
    assert fs.candidates_from_rows([row()], 'UA123', date(2026,2,9)) == []


def test_cruise_endpoint_and_standing_route_never_supply_airport():
    result = fs.candidates_from_rows([row(Track_Origin_FL_Ft='37000',
        Track_Origin_ApplicableAirports="['KLAX', 'KLGB']", Route_Validation_Based_on_Callsign='KLAX-KJFK')], 'UA123', DAY)
    assert result[0]['flight_info']['departure_airport'] is None
    assert result[0]['date_confirmed'] is False
    assert result[0]['flight_info']['arrival_airport'] == 'KJFK'


def test_multiple_ground_airports_not_silently_selected():
    result = fs.candidates_from_rows([row(Track_Origin_ApplicableAirports='KLAX KLGB')], 'UA123', DAY)
    assert result[0]['flight_info']['departure_airport'] is None
    assert result[0]['flight_info']['identity_evidence']['origin_airports'] == ['KLAX','KLGB']


def test_overlap_dedup_and_airframe_filter():
    rows = [row(), row(), row(ICAO_Hex='aabbcc',Reg='N999AB')]
    assert len(fs.candidates_from_rows(rows, 'UA123', DAY)) == 2
    assert len(fs.candidates_from_rows(rows, 'UA123', DAY, registration='N123AB',icao24='a1b2c3')) == 1


def test_quarter_boundary_and_extreme_timezones():
    assert fs.quarters(date(2026,4,1)) == {(2026,1),(2026,2)}
    assert fs.quarters(date(2026,3,31)) == {(2026,1),(2026,2)}
    assert fs.quarters(DAY) == {(2026,1)}


def test_parquet_missing_detailed_type_and_query_cache(tmp_path):
    path = tmp_path / '2024_Q1_github.parquet'
    with duckdb.connect() as con:
        columns = ','.join('?' + ' AS "' + key + '"' for key in row())
        con.execute('CREATE TABLE flights AS SELECT '+columns, list(row().values()))
        con.execute("COPY flights TO ? (FORMAT PARQUET)", [str(path)])
    result = fs.query_rows(path,['UAL123'],DAY)
    assert result == [row()]
    assert fs.query_rows(path,['UAL123'],DAY) == result
    assert fs.query_rows(path,['UAL999'],DAY) == []


def patch_lookup(monkeypatch, rows):
    async def assets(day):return [{'name':'test.parquet','release_url':'https://example.com/release'}]
    async def ensure(asset):return fs.cache_dir() / asset['name']
    async def callsigns(*args):return ['UAL123']
    async def alternate(*args):return None
    monkeypatch.setattr(fs,'release_assets',assets)
    monkeypatch.setattr(fs,'ensure_dataset',ensure)
    monkeypatch.setattr(fs,'resolve_callsigns',callsigns)
    monkeypatch.setattr(fs,'alternate_callsign',alternate)
    monkeypatch.setattr(fs,'query_rows',lambda *args:rows)


def test_multiple_segments_require_explicit_selection(monkeypatch):
    patch_lookup(monkeypatch,[row(),row(ICAO_Hex='aabbcc',Reg='N999AB')])
    async def run():
        with pytest.raises(ProviderError) as exc:await fs.lookup_dated_flight('UA123',DAY,{})
        assert exc.value.status_code == 409
        selected = exc.value.details['candidates'][1]
        result = await fs.lookup_dated_flight('UA123',DAY,{},schedule_candidate_id=selected['schedule_candidate_id'])
        assert result['icao24'] == 'aabbcc'
        assert result['identity_evidence']['datasets'][0]['name'] == 'test.parquet'
    asyncio.run(run())


def test_unknown_date_requires_confirmation_even_single_aircraft(monkeypatch):
    patch_lookup(monkeypatch,[row(Track_Origin_FL_Ft='37000')])
    with pytest.raises(ProviderError) as exc:asyncio.run(fs.lookup_dated_flight('UA123',DAY,{}))
    assert exc.value.status_code == 409
    assert exc.value.details['candidates'][0]['date_confirmed'] is False


def test_unpublished_quarter_does_not_download(monkeypatch):
    async def assets(day):return []
    monkeypatch.setattr(fs,'release_assets',assets)
    result = asyncio.run(fs.lookup_dated_flight('UA123',DAY,{}))
    assert '尚无' in result['identity_lookup_note']


def test_callsign_alias_discards_standing_route(monkeypatch):
    original = httpx.AsyncClient
    def handler(request):
        return httpx.Response(200,json={'response':{'flightroute':{'callsign_icao':'UAL12A',
            'origin':{'icao_code':'KLAX'},'destination':{'icao_code':'KJFK'}}}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    assert asyncio.run(fs.alternate_callsign('UA123')) == 'UAL12A'
    saved = json.loads((fs.cache_dir()/'callsign-UA123.json').read_text())
    assert 'origin' not in saved and 'destination' not in saved


def test_preparation_reports_progress_then_reuses_verified_parquet(tmp_path, monkeypatch):
    from hashlib import sha256
    fixture = tmp_path/'source.parquet'
    with duckdb.connect() as con:con.execute('COPY (SELECT 1 AS value) TO ? (FORMAT PARQUET)',[str(fixture)])
    raw = fixture.read_bytes()
    asset = {'year':2026,'quarter':1,'name':'2026_Q1_github.parquet','size':len(raw),
             'url':'https://example.com/source','digest':'sha256:'+sha256(raw).hexdigest()}
    original = httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=raw)),**kw))
    async def run():
        with pytest.raises(ProviderError) as exc:await fs.ensure_dataset(asset)
        assert exc.value.status_code == 202
        assert exc.value.details['total_bytes'] == len(raw)
        await fs._tasks[str(fs.cache_dir()/asset['name'])]
        path = await fs.ensure_dataset(asset)
        assert path.read_bytes() == raw
        assert fs._valid(path,asset)
    asyncio.run(run())


def test_unified_auto_identity_before_registry_and_adsb(monkeypatch):
    calls=[]
    async def lookup(*args,**kw):
        calls.append('dated');return fs.candidates_from_rows([row()], 'UA123',DAY)[0]['flight_info']
    async def aircraft(registration=None,icao24=None):
        calls.append('registry');assert registration == 'N123AB';return {'photo_url':'https://example.com/photo.jpg'}
    async def trace(*args,**kw):
        calls.append('adsb');assert kw['icao24']=='a1b2c3';assert kw['matched_callsign']=='UAL123'
        assert args[2:]==('KLAX','KJFK')
        assert kw['last_seen']>kw['first_seen']
        raise ProviderError(404,'no track')
    monkeypatch.setattr(fr,'lookup_dated_flight',lookup)
    monkeypatch.setattr(fr,'lookup_aircraft_info',aircraft)
    monkeypatch.setattr(fr,'fetch_flight_reconstruction',trace)
    with pytest.raises(ProviderError) as exc:asyncio.run(fr.fetch_flight_unified('UA123',DAY))
    assert calls==['dated','registry','adsb']
    assert exc.value.details['flight_info']['photo_url']
    assert exc.value.details['flight_info']['registration']=='N123AB'


def test_cruise_flyover_does_not_conflict_with_known_departure():
    result = fs.candidates_from_rows([row(Track_Origin_FL_Ft='37000',
        Track_Origin_ApplicableAirports="['KDEN']")], 'UA123', DAY, origin='LAX')
    assert result[0]['flight_info']['departure_airport'] == 'KLAX'
    assert result[0]['date_confirmed'] is False


def test_successful_fr24_track_does_not_query_quarter(monkeypatch):
    monkeypatch.setenv('FR24_API_TOKEN','configured')
    monkeypatch.setenv('FR24_HISTORY_DAYS','1096')
    async def fetched(*args):return {'points':[{},{}], 'metadata':{'flight':{'registration':'N123AB'}}}
    async def finished(draft):return draft
    async def forbidden(*args,**kwargs):raise AssertionError('FR24 already succeeded')
    async def registry(*args,**kwargs):return {}
    async def photo(*args):pass
    monkeypatch.setattr(fr,'fetch_flight',fetched)
    monkeypatch.setattr(fr,'finish_fr24_summary_draft',finished)
    monkeypatch.setattr(fr,'lookup_dated_flight',forbidden)
    monkeypatch.setattr(fr,'lookup_aircraft_info',registry)
    monkeypatch.setattr(fr,'_fill_missing_photo',photo)
    assert len(asyncio.run(fr.fetch_flight_unified('UA123',DAY))['points'])==2


def test_old_dates_skip_fr24_with_a_thirty_day_subscription(monkeypatch):
    from backend import providers
    monkeypatch.setenv('FR24_API_TOKEN','configured')
    monkeypatch.setenv('FR24_HISTORY_DAYS','30')
    monkeypatch.setattr(providers,'_today',lambda:date(2026,10,8))
    assert fr.fr24_date_available(date(2026,9,9))
    assert not fr.fr24_date_available(date(2026,9,8))
    calls=[]
    async def forbidden(*args,**kw):raise AssertionError('Unsupported date must not request FR24')
    async def dated(*args,**kw):calls.append('dated');return {'registration':'N123AB','icao24':'a1b2c3'}
    async def aircraft(*args,**kw):calls.append('registry');return {}
    async def trace(*args,**kw):calls.append('adsb');raise ProviderError(404,'no track')
    monkeypatch.setattr(fr,'fetch_flight',forbidden)
    monkeypatch.setattr(fr,'lookup_dated_flight',dated)
    monkeypatch.setattr(fr,'lookup_aircraft_info',aircraft)
    monkeypatch.setattr(fr,'fetch_flight_reconstruction',trace)
    with pytest.raises(ProviderError):asyncio.run(fr.fetch_flight_unified('UA123',DAY))
    assert calls==['dated','registry','adsb']


@pytest.mark.parametrize('trust_env', [True, False])
def test_download_resumes_matching_asset_and_checks_published_digest(tmp_path, monkeypatch, trust_env):
    from hashlib import sha256
    fixture = tmp_path / 'source.parquet'
    with duckdb.connect() as con:con.execute('COPY (SELECT 42 AS value) TO ? (FORMAT PARQUET)',[str(fixture)])
    raw = fixture.read_bytes()
    asset = {'year':2026,'quarter':1,'name':'2026_Q1_github.parquet','size':len(raw),
             'url':'https://example.com/source','digest':'sha256:'+sha256(raw).hexdigest()}
    path = fs.cache_dir()/asset['name']
    path.with_suffix('.part').write_bytes(raw[:20])
    fs._write(path.with_suffix('.status.json'),{'asset':asset,'state':'downloading'})
    original = httpx.Client
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.headers['range']=='bytes=20-'
        return httpx.Response(206,content=raw[20:],headers={'content-range':f'bytes 20-{len(raw)-1}/{len(raw)}'})
    def client(**kw):
        assert kw['trust_env'] is trust_env
        return original(transport=httpx.MockTransport(handler),**kw)
    monkeypatch.setattr(httpx,'Client',client)
    fs._download(asset, trust_env=trust_env)
    assert len(requests)==1 and fs._valid(path,asset)
    assert path.read_bytes()==raw


def test_release_catalog_can_ignore_environment_proxies(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    original = httpx.AsyncClient
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[])
    def client(**kw):
        assert kw['trust_env'] is False
        return original(transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    assert asyncio.run(fs.release_assets(DAY, trust_env=False)) == []
    assert requests


def test_corrupt_completed_partial_is_not_published(tmp_path):
    asset = {'year':2026,'quarter':1,'name':'2026_Q1_github.parquet','size':4,
             'url':'https://example.com/source','digest':'sha256:'+'0'*64}
    path=fs.cache_dir()/asset['name']
    path.with_suffix('.part').write_bytes(b'nope')
    fs._write(path.with_suffix('.status.json'),{'asset':asset,'state':'downloading'})
    fs._download(asset)
    assert not path.exists()
    assert fs._read(path.with_suffix('.status.json'))['state']=='failed'


def test_complete_manual_identity_needs_no_quarter_or_departure_time(monkeypatch):
    async def forbidden(*args,**kw):raise AssertionError('Manual airframe and airports do not need the historical index')
    async def aircraft(*args,**kw):return {'registration':'N123AB','icao24':'a1b2c3','photo_url':'https://example.com/plane.jpg'}
    async def trace(*args,**kw):
        assert kw['icao24']=='a1b2c3' and kw['first_seen'] is None
        raise ProviderError(404,'no track')
    monkeypatch.setattr(fr,'lookup_dated_flight',forbidden)
    monkeypatch.setattr(fr,'lookup_aircraft_info',aircraft)
    monkeypatch.setattr(fr,'fetch_flight_reconstruction',trace)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(fr.fetch_flight_unified('UA123',DAY,'LAX','JFK',registration='N123AB'))
    assert exc.value.details['flight_info']['photo_url']=='https://example.com/plane.jpg'


def test_download_can_be_cancelled_during_stream_and_explicitly_resumed(tmp_path,monkeypatch):
    from hashlib import sha256
    source=tmp_path/'source.parquet'
    with duckdb.connect() as con:con.execute('COPY (SELECT * FROM range(100000)) TO ? (FORMAT PARQUET)',[str(source)])
    raw=source.read_bytes()
    asset={'year':2026,'quarter':1,'name':'2026_Q1_github.parquet','size':len(raw),'url':'https://example.com/source','digest':'sha256:'+sha256(raw).hexdigest()}
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield raw[:65536]
            fs.cancel_download(asset)
            yield raw[65536:]
    original=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=Stream())),**kw))
    fs._download(asset)
    path=fs.cache_dir()/asset['name']
    assert not path.exists() and path.with_suffix('.part').stat().st_size==65536
    assert fs._read(path.with_suffix('.status.json'))['state']=='cancelled'
    async def resume():
        with pytest.raises(ProviderError) as exc:await fs.ensure_dataset(asset,resume=False)
        assert exc.value.status_code==409
        def handler(request):
            assert request.headers['range']=='bytes=65536-'
            return httpx.Response(206,content=raw[65536:],headers={'content-range':f'bytes 65536-{len(raw)-1}/{len(raw)}'})
        monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
        with pytest.raises(ProviderError) as pending:await fs.ensure_dataset(asset)
        assert pending.value.status_code==202
        await fs._tasks[str(path)]
        assert await fs.ensure_dataset(asset)==path
        assert path.read_bytes()==raw
    asyncio.run(resume())
