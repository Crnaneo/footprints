"""Manual correction persistence, catalog selection, real dates and safe undo."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.models import TripCreate
from backend.rail_coverage import COVERAGE_VERSION, fingerprint, read_coverage
from backend.rail_manual import line_catalog, preview_interval, section_id
from backend.store import TripStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('FOOTPRINT_DATA_DIR', str(tmp_path))
    store = TripStore()
    rail = tmp_path/'rail'; rail.mkdir()
    monkeypatch.setattr('backend.rail_coverage.passenger_directory', lambda _: {})
    monkeypatch.setattr('backend.passenger_sections.passenger_directory', lambda _: {})
    with sqlite3.connect(rail/'catalog.sqlite3') as db:
        db.execute('CREATE TABLE lines(id INTEGER,name TEXT,tags TEXT,ways TEXT,stations TEXT)')
        db.execute('CREATE TABLE ways(id INTEGER,nodes TEXT,geometry TEXT,tags TEXT)')
        db.execute('CREATE TABLE stations(id INTEGER,name TEXT,lon REAL,lat REAL,tags TEXT)')
        for lid,names,offset in [(1,['甲','乙线路所','丙','丁'],110), (2,['戊','己'],112)]:
            ids = [lid*10+i for i in range(len(names))]
            ways = []
            for i in range(len(names)-1):
                wid = lid*100+i; ways.append(wid)
                db.execute('INSERT INTO ways VALUES(?,?,?,?)',(wid,json.dumps(ids[i:i+2]),json.dumps([[offset+i*.05,35],[offset+(i+1)*.05,35]]),'{}'))
            db.execute('INSERT INTO lines VALUES(?,?,?,?,?)',(lid,'测试'+str(lid)+'线',json.dumps({'network':'中国铁路','from':names[0],'to':names[-1]},ensure_ascii=False),json.dumps(ways),json.dumps(ids)))
            for i,name in enumerate(names):
                tags = {'railway':'signal_box','passenger':'no'} if '线路所' in name else {'railway':'station','passenger':'yes'}
                db.execute('INSERT INTO stations VALUES(?,?,?,?,?)',(ids[i],name,offset+i*.05,35,json.dumps(tags)))
    def trip(date,lon,sandbox=False):
        return TripCreate(title='真实近站记录',mode='other',travel_date=date,source='csv',geometry_kind='imported',metadata={'sandbox':sandbox},points=[{'longitude':lon,'latitude':35}])
    store.create_many([trip('2024-05-01',110.0001),trip('2023-02-03',110.1),trip('2020-01-01',110.1,True),trip('2021-01-01',110.013)])
    catalog = line_catalog(store,'1')
    section = preview_interval(store,'1','osm:10','osm:12',catalog)
    section.pop('manual')
    base = {'coverage_version':COVERAGE_VERSION,'fingerprint':fingerprint(store),'catalog_available':True,'lines':[{'line_id':'1','name':'测试1线','intervals':[section],'trip_count':2,'source_url':'https://example.test/1','direction':catalog.direction}]}
    (rail/'coverage.json').write_text(json.dumps(base),encoding='utf-8')
    with TestClient(app) as client:
        yield client,store,base


def edit_body(client,origin='osm:10',destination='osm:13',line_id='1',interval_id=None):
    current = client.get('/api/rail-coverage').json()
    revision = next((l['manual_revision'] for l in current['lines'] if l['line_id']==line_id),'auto')
    return {'line_id':line_id,'origin_id':origin,'destination_id':destination,'interval_id':interval_id,'expected_revision':revision}


def test_catalog_includes_unvisited_passengers_and_control_stations(setup):
    client,store,_ = setup
    response = client.get('/api/rail-catalog/lines',params={'q':'测试'})
    assert response.status_code==200 and response.json()['total']==2
    rows = client.get('/api/rail-catalog/lines/1/stations').json()['stations']
    assert [r['name'] for r in rows]==['甲','乙线路所','丙','丁']
    assert rows[1]['kind']=='线路所'
    assert rows[3]['id']=='osm:13' # This station has no points or automatic interval.
    assert client.get('/api/rail-catalog/lines/999/stations').status_code==400


def test_preview_uses_real_nearby_dates_excludes_sandbox_and_does_not_invent_dates(setup):
    client,store,_ = setup
    before = len(store.list())
    body = edit_body(client,origin='osm:13',destination='osm:10')
    response = client.post('/api/rail-manual/preview',json=body)
    assert response.status_code==200
    s = response.json()
    assert (s['origin'],s['destination'])==('甲','丁')
    assert len(s['station_details'])==4
    passengers = [s for s in s['station_details'] if s['kind']=='客运站']
    assert [(r['name'],r['first_arrival_date']) for r in passengers]==[('甲','2024-05-01'),('丙','2023-02-03'),('丁',None)]
    assert len(store.list())==before
    assert not any(l.get('manual') for l in read_coverage(store)['lines'])


@pytest.mark.parametrize('origin,destination',[('free text','osm:13'),('osm:10','osm:20'),('osm:10','osm:10')])
def test_bad_selection_is_atomic(setup,origin,destination):
    client,store,_ = setup
    assert client.put('/api/rail-manual/interval',json=edit_body(client,origin,destination)).status_code==400
    assert not read_coverage(store)['lines'][0].get('manual')


def test_edit_delete_and_undo_restore_exact_original_without_touching_gps(setup):
    client,store,_ = setup
    original = client.get('/api/rail-coverage').json()['lines']
    before = store.list()
    body = edit_body(client,interval_id=original[0]['intervals'][0]['interval_id'])
    saved = client.put('/api/rail-manual/interval',json=body)
    assert saved.status_code==200
    updated = read_coverage(TripStore())['lines'][0]
    assert updated['manual'] and len(updated['intervals'])==1
    assert updated['intervals'][0]['destination']=='丁'
    deletion = client.post('/api/rail-manual/delete',json={'line_id':'1','expected_revision':updated['manual_revision'],'interval_id':updated['intervals'][0]['interval_id']})
    assert deletion.status_code==200
    assert read_coverage(store)['lines']==[]
    assert read_coverage(store)['hidden_lines'][0]['name']=='测试1线'
    assert client.post('/api/rail-manual/undo',json={'token':deletion.json()['undo_token']}).status_code==200
    assert read_coverage(store)['lines'][0]['intervals'][0]['destination']=='丁'
    assert client.post('/api/rail-manual/undo',json={'token':saved.json()['undo_token']}).status_code==200
    assert read_coverage(store)['lines']==original
    assert store.list()==before
    assert client.post('/api/rail-manual/undo',json={'token':saved.json()['undo_token']}).status_code==409


def test_touching_sections_merge_and_new_line_automatically_populates_passengers(setup):
    client,store,_ = setup
    added = client.put('/api/rail-manual/interval',json=edit_body(client,'osm:12','osm:13'))
    assert added.status_code==200
    line = read_coverage(store)['lines'][0]
    assert len(line['intervals'])==1
    assert line['intervals'][0]['origin']=='甲' and line['intervals'][0]['destination']=='丁'
    assert len(line['intervals'][0]['station_details'])==4
    assert client.put('/api/rail-manual/interval',json=edit_body(client,'osm:20','osm:21','2')).status_code==200
    second = next(l for l in read_coverage(store)['lines'] if l['line_id']=='2')
    assert second['manual'] and second['trip_count']==0
    assert [s['name'] for s in second['intervals'][0]['station_details']]==['戊','己']
    assert all(s['first_arrival_date'] is None for s in second['intervals'][0]['station_details'])


def test_snapshots_and_deletions_survive_rebuild_and_restore_uses_new_auto_result(setup):
    client,store,base = setup
    assert client.put('/api/rail-manual/interval',json=edit_body(client)).status_code==200
    manual = read_coverage(store)['lines'][0]
    base['lines'][0]['intervals'][0]['destination']='自动结果已变化'
    (store.path.parent/'rail'/'coverage.json').write_text(json.dumps(base))
    assert read_coverage(TripStore())['lines'][0]['intervals'][0]['destination']=='丁'
    assert client.post('/api/rail-manual/restore',json={'line_id':'1','expected_revision':manual['manual_revision']}).status_code==200
    restored = read_coverage(store)['lines'][0]
    assert not restored.get('manual')
    assert restored['intervals'][0]['destination']=='自动结果已变化'
    assert client.post('/api/rail-manual/delete',json={'line_id':'1','expected_revision':restored['manual_revision']}).status_code==200
    assert not read_coverage(TripStore())['lines']
    hidden = read_coverage(store)['hidden_lines'][0]
    assert client.post('/api/rail-manual/restore',json={'line_id':'1','expected_revision':hidden['manual_revision']}).status_code==200
    assert read_coverage(store)['lines'][0]['intervals'][0]['destination']=='自动结果已变化'


def test_concurrent_edits_and_stale_undo_do_not_clobber_new_changes(setup):
    client,store,_ = setup
    old = edit_body(client)
    first = client.put('/api/rail-manual/interval',json=old)
    assert first.status_code==200
    assert client.put('/api/rail-manual/interval',json=old).status_code==409
    line = read_coverage(store)['lines'][0]
    assert client.post('/api/rail-manual/delete',json={'line_id':'1','expected_revision':line['manual_revision']}).status_code==200
    assert client.post('/api/rail-manual/undo',json={'token':first.json()['undo_token']}).status_code==409
    assert read_coverage(store)['lines']==[]


def test_missing_interval_cannot_add_instead_of_editing(setup):
    client,store,_ = setup
    assert client.put('/api/rail-manual/interval',json=edit_body(client,interval_id='missing')).status_code==409
    assert not read_coverage(store)['lines'][0].get('manual')


def test_declared_station_inside_catalog_gap_stays_selectable(setup):
    _,store,_ = setup
    index = __import__('backend.rail_manual',fromlist=['editor_index']).editor_index(store)
    # The declared intermediate control station is off the clipped geometry;
    # this directly exercises public catalog construction with declared rows.
    index.lines[1]['station_nodes'].add(99)
    index.station_nodes.append(99);index.station_ids.append('osm:99');index.station_names.append('缺口线路所')
    index.station_kinds.append('线路所');index.station_tags.append({})
    import numpy as np
    from backend.rail_coverage import project
    index.station_xy=np.concatenate([index.station_xy,[project(110.075,35.1)]])
    from backend.rail_manual import LineCatalog
    catalog=LineCatalog(index,1)
    assert catalog.rows['osm:99']['catalog_gap']
    section=preview_interval(store,'1','osm:10','osm:99',catalog)
    assert section['reference_gaps']
    assert section['destination_station']['first_arrival_date'] is None


def test_partial_overlap_merges_without_counting_shared_mileage_twice(setup):
    client,store,_ = setup
    before = read_coverage(store)['lines'][0]['intervals'][0]['distance_km']
    addition = client.put('/api/rail-manual/interval',json=edit_body(client,'osm:11','osm:13'))
    assert addition.status_code==200
    line = read_coverage(store)['lines'][0]
    assert len(line['intervals'])==1
    assert [row['id'] for row in line['intervals'][0]['station_details']]==['osm:10','osm:11','osm:12','osm:13']
    assert line['intervals'][0]['distance_km']==pytest.approx(before*1.5,abs=.15)


def test_edit_to_control_station_refreshes_passenger_list_and_preserves_other_records(setup):
    client,store,_ = setup
    original = read_coverage(store)['lines'][0]['intervals'][0]
    body = edit_body(client,'osm:11','osm:12',interval_id=original['interval_id'])
    assert client.put('/api/rail-manual/interval',json=body).status_code==200
    changed = read_coverage(store)['lines'][0]['intervals'][0]
    assert changed['origin_kind']=='线路所'
    assert [s['name'] for s in changed['station_details'] if s['kind']=='客运站']==['丙']
    assert len(store.list())==4


def test_hidden_line_can_be_explicitly_readded_and_undo_keeps_it_hidden(setup):
    client,store,_ = setup
    assert client.post('/api/rail-manual/delete',json={'line_id':'1','expected_revision':'auto'}).status_code==200
    revision = client.get('/api/rail-catalog/lines/1/stations').json()['manual_revision']
    response = client.put('/api/rail-manual/interval',json={**edit_body(client),'expected_revision':revision})
    assert response.status_code==200 and len(read_coverage(store)['lines'])==1
    assert read_coverage(store)['hidden_lines']==[]
    assert client.post('/api/rail-manual/undo',json={'token':response.json()['undo_token']}).status_code==200
    assert read_coverage(store)['lines']==[]
    assert len(read_coverage(store)['hidden_lines'])==1
