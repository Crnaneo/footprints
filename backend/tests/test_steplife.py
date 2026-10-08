import csv,io,json
from datetime import datetime,timezone,timedelta
import pytest
from fastapi.testclient import TestClient
from backend.app import app
from backend.store import TripStore
from backend.steplife import FIELDS,import_backup,iter_backup_csv,point_row
from backend.csv_io import CsvImportError
from backend.rail_coverage import merge_intervals,RailIndex,project
from shapely.geometry import LineString

HEADER=','.join(FIELDS)+'\n'
def row(epoch=1790899200,speed='12.500000',lon='116.372000',lat='39.863000'):
 return f'{epoch},1,{lon},{lat},0.000000,10.000000,{speed},0.000000,1,0,-2.500000\n'

def test_lossless_original_order_duplicates_sentinel_and_day_sort(tmp_path):
 path=tmp_path/'source.csv';data=HEADER+row(1790985600)+row()+row(speed='-1.000000')
 path.write_text(data);store=TripStore(tmp_path/'db.sqlite3')
 result=import_backup(path,store,archive_filename='source.csv')
 assert result['count']==2 and result['point_count']==3
 assert [t['travel_date'] for t in result['trips']]==['2026-10-02','2026-10-03']
 day=store.get(result['trips'][0]['id'])
 assert day['points'][0]['speed_kmh']==45 and day['points'][1]['speed_kmh'] is None
 assert day['points'][0]['altitude_m']==-2.5
 assert day['point_count']==2 # Duplicate timestamps are retained.
 exported=b''.join(iter_backup_csv(store)).decode('utf-8-sig')
 assert list(csv.reader(io.StringIO(exported)))==list(csv.reader(io.StringIO(data)))
 again=import_backup(path,store)
 assert again['already_imported'] and len(store.list())==2
 assert all(t['preview'] for t in store.overview())

def test_invalid_last_row_rolls_back_all_days(tmp_path):
 path=tmp_path/'bad.csv';path.write_text(HEADER+row()+row(lon='NaN'))
 store=TripStore(tmp_path/'db.sqlite3')
 with pytest.raises(CsvImportError):import_backup(path,store)
 assert not store.list()
 with store.connect() as db:assert db.execute('SELECT count(*) FROM point_csv').fetchone()[0]==0

def test_backup_allows_more_than_generic_trip_limit(tmp_path):
 path=tmp_path/'many.csv';path.write_text(HEADER+''.join(row(1501826400+i*86400) for i in range(1001)))
 result=import_backup(path,TripStore(tmp_path/'db.sqlite3'))
 assert result['count']==1001 and result['point_count']==1001

def test_empty_timestamp_uses_date_without_fabricating_time(tmp_path):
 path=tmp_path/'untimed.csv';path.write_text(HEADER+row(epoch=''))
 store=TripStore(tmp_path/'db.sqlite3')
 with pytest.raises(CsvImportError):import_backup(path,store)
 result=import_backup(path,store,fallback_date='2020-01-01')
 t=store.get(result['trips'][0]['id']);assert t['travel_date']=='2020-01-01' and t['points'][0]['timestamp'] is None
 assert list(csv.reader(io.StringIO(b''.join(iter_backup_csv(store)).decode('utf-8-sig'))))[1][0]==''

def test_default_api_profile_archive_cleanup_and_extended_compatibility(tmp_path,monkeypatch):
 monkeypatch.setenv('FOOTPRINT_DATA_DIR',str(tmp_path))
 with TestClient(app) as c:
  r=c.post('/api/import/csv',files={'file':('backup.csv',(HEADER+row()).encode())})
  assert r.status_code==201
  trip=r.json()['trips'][0];archive=tmp_path/'imports'/trip['metadata']['original_file'];assert archive.exists()
  out=c.get('/api/export.csv');assert list(csv.reader(io.StringIO(out.content.decode('utf-8-sig'))))[0]==FIELDS
  assert 'trip_id' in c.get('/api/export.csv?format=extended').text
  assert c.get('/api/overview').json()[0]['preview']
  assert c.delete('/api/trips/'+trip['id']).status_code==204 and not archive.exists()

@pytest.mark.parametrize('pieces,expected',[
 ([(0,10),(10,20)],[[0,20]]), ([(10,0),(8,30)],[[0,30]]),
 ([(0,10),(11,20)],[[0,10],[11,20]]), ([(20,30),(0,10)],[[0,10],[20,30]])])
def test_interval_union_touch_overlap_reverse_and_gap(pieces,expected):
 assert merge_intervals(pieces)==expected

def test_geometry_union_joins_shared_nodes_but_keeps_missing_piece():
 index=object.__new__(RailIndex);index.lines={1:{'name':'测试铁路'}}
 index.ways={10:{'line':1,'nodes':(1,2),'geom':LineString([(0,0),(2000,0)]),'length':2000},11:{'line':1,'nodes':(2,3),'geom':LineString([(2000,0),(4000,0)]),'length':2000}}
 index.endpoint_label=lambda p:f'{int(p[0])}'
 continuous=index.summarize({10:[(0,2000)],11:[(0,2000)]},{'1':{'a','b'}})[0]
 assert len(continuous['intervals'])==1 and continuous['intervals'][0]['distance_km']==4
 separated=index.summarize({10:[(0,1200)],11:[(800,2000)]},{'1':{'a'}})[0]
 assert len(separated['intervals'])==2

def test_parallel_aircraft_and_short_proximity_are_not_rail():
 import numpy as np
 from shapely.strtree import STRtree
 index=object.__new__(RailIndex);index.lines={1:{'name':'测试干线'}}
 g=LineString([project(116,39),project(116.2,39)])
 index.ids=[1];index.geometries=[g];index.tree=STRtree([g]);index.ways={1:{'line':1,'geom':g,'nodes':(1,2),'length':g.length}}
 pts=[{'longitude':116+i*.005,'latitude':39,'segment_id':'0','speed_kmh':250,'altitude_m':10,'timestamp':None} for i in range(30)]
 assert index.match_trip({'id':'flight','mode':'flight','points':pts})[0]=={}
 assert index.match_trip({'id':'short','mode':'other','points':pts[:6]})[0]=={}
 assert index.match_trip({'id':'rail','mode':'other','points':pts})[0]

def test_partial_deletion_restores_only_missing_days(tmp_path):
 path=tmp_path/'source.csv';path.write_text(HEADER+row()+row(1790985600));store=TripStore(tmp_path/'db.sqlite3')
 first=import_backup(path,store);retained=first['trips'][1]['id'];store.delete(first['trips'][0]['id'])
 restored=import_backup(path,store)
 assert restored['count']==2 and retained in [t['id'] for t in restored['trips']]
 assert len(store.list())==2
 with store.connect() as db:assert db.execute('SELECT count(*) FROM point_csv').fetchone()[0]==2

def test_gcj_export_is_normalized_to_wgs84_for_unmarked_interchange(tmp_path):
 from backend.models import gcj02_to_wgs84
 path=tmp_path/'source.csv';path.write_text(HEADER+row());store=TripStore(tmp_path/'db.sqlite3')
 import_backup(path,store,source_crs='GCJ02')
 data=list(csv.reader(io.StringIO(b''.join(iter_backup_csv(store)).decode('utf-8-sig'))))[1]
 expected=gcj02_to_wgs84(116.372,39.863)
 assert tuple(map(float,data[2:4]))==expected

def test_parallel_axes_merge_continuous_observations_without_filling_gap():
 from collections import defaultdict
 import numpy as np
 from scipy.spatial import cKDTree
 idx=object.__new__(RailIndex);idx.lines={1:{'name':'测试铁路'}};idx.graph={1:defaultdict(list)};idx.ways={}
 for wid,a,b,x,y in [(10,1,2,0,0),(11,2,3,2000,0),(20,4,5,0,10),(21,5,6,2000,10)]:
  g=LineString([(x,y),(x+2000,y)]);idx.ways[wid]={'line':1,'nodes':(a,b),'geom':g,'length':2000}
  idx.graph[1][a].append((b,wid,2000));idx.graph[1][b].append((a,wid,2000))
 idx.station_xy=np.array([[0,0],[1000,0],[3000,0],[4000,0]]);idx.station_names=['甲站','乙站','丙站','丁站'];idx.station_tree=cKDTree(idx.station_xy)
 idx.observations=[(1,np.array([[0,0],[1000,10],[2000,0],[3000,10],[4000,0]]))]
 sections=idx.reference_sections({10:[(0,900)],21:[(1100,2000)]},{'1':{'a'}})[0]['intervals']
 assert len(sections)==1 and sections[0]['distance_km']==4
 idx.observations=[(1,np.array([[0,0],[1000,10]])),(1,np.array([[3000,10],[4000,0]]))]
 sections=idx.reference_sections({10:[(0,900)],21:[(1100,2000)]},{'1':{'a'}})[0]['intervals']
 assert len(sections)==2
