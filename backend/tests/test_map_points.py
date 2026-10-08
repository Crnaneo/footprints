import json
from pathlib import Path
import subprocess
from backend.store import TripStore
from backend.map_points import register_view,tile,point_detail
from backend.models import TripCreate


def record(store,points):
 return store.create(TripCreate(title='原始点测试',mode='other',travel_date='2026-10-03',points=points))


def decode(data,tmp_path):
 p=tmp_path/'points.pbf';p.write_bytes(data)
 script="""import fs from 'node:fs';import {VectorTile} from '@mapbox/vector-tile';import {PbfReader} from 'pbf';const t=new VectorTile(new PbfReader(fs.readFileSync(process.argv[1])));const l=t.layers.points;const out=[];for(let i=0;i<l.length;i++){const f=l.feature(i);out.push({id:f.id,type:f.type,properties:f.properties,geometry:f.loadGeometry()})}process.stdout.write(JSON.stringify(out));"""
 return json.loads(subprocess.check_output(['node','--input-type=module','-e',script,str(p)],cwd=Path(__file__).resolve().parents[2]))


def test_tile_keeps_every_point_and_duplicate_and_is_only_point_geometry(tmp_path):
 store=TripStore(tmp_path/'db.sqlite3')
 points=[{'longitude':100+i*.00001,'latitude':30} for i in range(350)]
 points.append(dict(points[0]))
 saved=record(store,points);view=register_view(store,[saved['id']]);data,count=tile(store,view['id'],0,0,0)
 features=decode(data,tmp_path)
 assert count==view['point_count']==len(features)==351
 assert len({f['id'] for f in features})==351
 assert all(f['type']==1 and len(f['geometry'][0])==1 for f in features)
 detail=point_detail(store,features[0]['id'])
 assert detail['point'] in saved['points']
 assert all(f['properties']['tripId']==saved['id'] for f in features)


def test_tile_filter_and_delete_keep_index_consistent(tmp_path):
 store=TripStore(tmp_path/'db.sqlite3');a=record(store,[{'longitude':0,'latitude':0}]);b=record(store,[{'longitude':1,'latitude':1}])
 view=register_view(store,[a['id']]);data,count=tile(store,view['id'],0,0,0)
 assert count==1
 assert decode(data,tmp_path)[0]['properties']['tripId']==a['id']
 store.delete(a['id']);assert tile(store,view['id'],0,0,0)[1]==0
 with store.connect() as db:
  assert db.execute('SELECT count(*) FROM map_points').fetchone()[0]==1
  assert db.execute('SELECT count(*) FROM map_point_index').fetchone()[0]==1
  # Force legacy migration without touching the original payload.
  db.execute('DELETE FROM map_points');db.execute('DELETE FROM map_indexed')
 again=TripStore(store.path);v=register_view(again,[b['id']])
 assert tile(again,v['id'],0,0,0)[1]==1


def test_tile_boundaries_and_full_detail_preserve_original_measurements(tmp_path):
 store=TripStore(tmp_path/'db.sqlite3');s=record(store,[{'longitude':0,'latitude':0,'sequence':99,'speed_kmh':45,'altitude_m':15}]);v=register_view(store,[s['id']])
 for x in (0,1):
  for y in (0,1):
   data,n=tile(store,v['id'],1,x,y);assert n==1
   f=decode(data,tmp_path)[0];assert point_detail(store,f['id'])['point']==s['points'][0]


def test_overview_bounds_include_unsampled_extreme_and_dateline(tmp_path):
 store=TripStore(tmp_path/'db.sqlite3');saved=record(store,[{'longitude':179,'latitude':10},{'longitude':-179,'latitude':30},{'longitude':178,'latitude':5}])
 view=register_view(store,[saved['id']]);assert view['bounds']==[[178,5],[181,30]]


def test_map_api_returns_all_points_and_original_detail(tmp_path,monkeypatch):
 from fastapi.testclient import TestClient
 from backend.app import app
 monkeypatch.setenv('FOOTPRINT_DATA_DIR',str(tmp_path))
 store=TripStore();saved=record(store,[{'longitude':10,'latitude':10},{'longitude':10,'latitude':10}])
 with TestClient(app) as client:
  view=client.post('/api/map/views',json={'trip_ids':[saved['id']]}).json()
  response=client.get(f"/api/map/tiles/{view['id']}/0/0/0.pbf")
  assert response.headers['x-point-count']=='2'
  features=decode(response.content,tmp_path)
  assert client.get('/api/map/point/'+str(features[0]['id'])).json()['point'] in saved['points']
  assert client.post('/api/map/views',json={'trip_ids':['missing']}).status_code==400


def test_tile_cache_reuses_encoding_and_invalidates_after_import_and_delete(tmp_path,monkeypatch):
 import gzip
 import backend.map_points as m
 store=TripStore(tmp_path/'db.sqlite3');a=record(store,[{'longitude':1,'latitude':2}]);v=register_view(store,[a['id']])
 calls=[];original=m.encode_points
 def counted(*args):
  calls.append(1);return original(*args)
 monkeypatch.setattr(m,'encode_points',counted)
 first=m.tile_entry(store,v['id'],0,0,0)
 assert m.tile_entry(store,v['id'],0,0,0) is first and len(calls)==1
 assert gzip.decompress(first[2])==first[0]
 b=record(store,[{'longitude':10,'latitude':20}])
 # An old all-records view must not unexpectedly include a newly imported trip.
 assert m.tile_entry(store,v['id'],0,0,0)[1]==1 and len(calls)==2
 updated=register_view(store,[a['id'],b['id']])
 assert updated['id']!=v['id'] and updated['bounds']==[[1,2],[10,20]]
 assert tile(store,updated['id'],0,0,0)[1]==2
 store.delete(a['id'])
 assert tile(store,v['id'],0,0,0)[1]==0
 assert register_view(store,[b['id']])['bounds']==[[10,20],[10,20]]


def test_concurrent_requests_encode_the_same_tile_once(tmp_path,monkeypatch):
 from concurrent.futures import ThreadPoolExecutor
 import backend.map_points as m
 store=TripStore(tmp_path/'db.sqlite3');a=record(store,[{'longitude':1,'latitude':2} for _ in range(500)])
 v=register_view(store,[a['id']]);calls=[];original=m.encode_points
 def counted(*args):calls.append(1);return original(*args)
 monkeypatch.setattr(m,'encode_points',counted)
 with ThreadPoolExecutor(max_workers=4) as pool:
  results=list(pool.map(lambda _:m.tile_entry(store,v['id'],0,0,0),range(4)))
 assert len(calls)==1 and all(r is results[0] for r in results)


def test_tile_http_cache_revalidates_after_deletion(tmp_path,monkeypatch):
 from fastapi.testclient import TestClient
 from backend.app import app
 monkeypatch.setenv('FOOTPRINT_DATA_DIR',str(tmp_path))
 store=TripStore();a=record(store,[{'longitude':0,'latitude':0}])
 with TestClient(app) as client:
  v=client.post('/api/map/views',json={'trip_ids':[a['id']]}).json();url=f"/api/map/tiles/{v['id']}/0/0/0.pbf"
  first=client.get(url);assert first.headers['content-encoding']=='gzip'
  assert len(decode(first.content,tmp_path))==1
  cached=client.get(url,headers={'If-None-Match':first.headers['etag']})
  assert cached.status_code==304 and not cached.content
  store.delete(a['id'])
  changed=client.get(url,headers={'If-None-Match':first.headers['etag']})
  assert changed.status_code==200 and changed.headers['etag']!=first.headers['etag'] and changed.headers['x-point-count']=='0'


def test_lightweight_overview_has_exact_bounds_without_loading_point_payloads(tmp_path):
 store=TripStore(tmp_path/'db.sqlite3')
 saved=record(store,[{'longitude':179,'latitude':10},{'longitude':-179,'latitude':30},{'longitude':178,'latitude':5}])
 # A missing legacy preview must not trigger migration or a payload read for
 # the lightweight endpoint; original points remain available on demand.
 with store.connect() as db:db.execute('DELETE FROM map_previews')
 overview=store.overview(include_points=False)
 assert len(overview)==1 and overview[0]['points']==[] and overview[0]['preview']
 assert overview[0]['bounds']==[[178,5],[181,30]]
 assert overview[0]['point_count']==3 and overview[0]['map_revision']>0
 with store.connect() as db:assert db.execute('SELECT count(*) FROM map_previews').fetchone()[0]==0
 assert store.get(saved['id'])==saved


def test_map_view_cache_skips_repeated_summary_reads_and_returns_isolated_results(tmp_path,monkeypatch):
 from contextlib import contextmanager
 store=TripStore(tmp_path/'db.sqlite3');saved=record(store,[{'longitude':10,'latitude':20}])
 original=store.connect;queries=[]
 @contextmanager
 def traced():
  with original() as db:
   db.set_trace_callback(queries.append)
   yield db
 monkeypatch.setattr(store,'connect',traced)
 first=register_view(store,[saved['id']]);queries.clear()
 first['bounds'][0][0]=999
 second=register_view(store,[saved['id'],saved['id']])
 assert second['bounds']==[[10,20],[10,20]]
 assert not any('summary' in q or 'payload' in q for q in queries)
 changed=record(store,[{'longitude':11,'latitude':21}])
 refreshed=register_view(store,[saved['id'],changed['id']])
 assert refreshed['id']!=second['id'] and refreshed['point_count']==2


def test_tile_revalidation_does_not_rebuild_evicted_bytes(tmp_path,monkeypatch):
 from fastapi.testclient import TestClient
 from backend.app import app
 import backend.map_points as m
 monkeypatch.setenv('FOOTPRINT_DATA_DIR',str(tmp_path))
 store=TripStore();saved=record(store,[{'longitude':0,'latitude':0}])
 with TestClient(app) as client:
  view=client.post('/api/map/views',json={'trip_ids':[saved['id']]}).json()
  url=f"/api/map/tiles/{view['id']}/0/0/0.pbf"
  first=client.get(url)
  with m.TILE_LOCK:
   m.TILE_CACHE.clear();m.TILE_CACHE_BYTES=0
  def forbidden(*args):raise AssertionError('304 must not encode points')
  monkeypatch.setattr(m,'encode_points',forbidden)
  response=client.get(url,headers={'If-None-Match':first.headers['etag']})
  assert response.status_code==304 and not response.content
  assert client.get(f"/api/map/tiles/{view['id']}/20/0/0.pbf",headers={'If-None-Match':'anything'}).status_code==400


def test_tile_cache_releases_old_revisions_and_respects_byte_limit(tmp_path,monkeypatch):
 import backend.map_points as m
 store=TripStore(tmp_path/'db.sqlite3');saved=record(store,[{'longitude':0,'latitude':0}])
 view=register_view(store,[saved['id']]);m.tile_entry(store,view['id'],0,0,0)
 old_revision=m.view_revision(store,view['id'],0,0,0)
 record(store,[{'longitude':1,'latitude':1}])
 m.tile_entry(store,view['id'],0,0,0)
 path=str(store.path.resolve())
 assert not any(key[0]==path and key[1]==old_revision for key in m.TILE_CACHE)
 assert m.TILE_CACHE_BYTES<=m.TILE_CACHE_LIMIT


def test_merge_overlay_ranges_does_not_expand_ordinals():
 from backend.map_points import merge_ranges
 assert merge_ranges([[9,12],[0,2**31],[5,6]])==[[0,2**31]]
 assert merge_ranges([[10,12],[1,3],[3,7],[8,9],[20,20]])==[[1,12],[20,20]]


def unpack(features):
 result={}
 for feature in features:
  identity=feature['id'];deltas=json.loads(feature['properties'].get('pointDeltas','[]'))
  coordinates=[ring[0] for ring in feature['geometry']]
  assert feature['type']==1 and all(len(ring)==1 for ring in feature['geometry'])
  assert len(coordinates)==len(deltas)+1 and len(coordinates)<=128
  for i,coordinate in enumerate(coordinates):
   if i:identity+=deltas[i-1]
   assert identity not in result
   result[identity]=(coordinate,feature['properties']['tripId'],feature['properties']['color'])
 return result


def test_packed_tiles_preserve_all_ids_duplicate_coordinates_colours_and_boundaries(tmp_path):
 from backend.map_points import encode_points,encode_point_batches
 # Interleave 80 trips, negative ID deltas, all colours, and exact duplicates.
 rows=[(2000-i,str(i%80),.5+(i%9)*.00001,.5,'rail' if i%2 else 'flight') for i in range(900)]
 rows.extend((3000+i,'dense',.5,.5,'other') for i in range(400))
 for z,x,y in [(0,0,0),(1,0,1),(12,2048,2048)]:
  old,count=encode_points(rows,z,x,y);new,packed_count=encode_point_batches(iter(rows),z,x,y)
  expected={f['id']:(f['geometry'][0][0],f['properties']['tripId'],f['properties']['color']) for f in decode(old,tmp_path)}
  packed=decode(new,tmp_path)
  assert count==packed_count==len(unpack(packed))==1300
  assert unpack(packed)==expected
  assert len(packed)<len(rows) and len(new)<len(old)


def test_packed_tile_api_and_cache_are_distinct_and_invalidate_with_original_data(tmp_path,monkeypatch):
 from fastapi.testclient import TestClient
 from backend.app import app
 from backend.map_points import tile_entry
 monkeypatch.setenv('FOOTPRINT_DATA_DIR',str(tmp_path));store=TripStore()
 saved=record(store,[{'longitude':10+i*.00001,'latitude':30,'speed_kmh':i} for i in range(300)])
 with TestClient(app) as client:
  view=client.post('/api/map/views',json={'trip_ids':[saved['id']]}).json()
  url=f"/api/map/tiles/{view['id']}/0/0/0.pbf"
  original=client.get(url);packed=client.get(url+'?packed=true')
  assert packed.headers['x-point-count']=='300' and packed.headers['etag']!=original.headers['etag']
  points=unpack(decode(packed.content,tmp_path))
  for identity in [min(points),max(points)]:
   detail=client.get('/api/map/point/'+str(identity)).json()
   assert detail['point'] in saved['points']
  assert client.get(url+'?packed=true',headers={'If-None-Match':packed.headers['etag']}).status_code==304
  assert client.get(url+'?packed=true',headers={'If-None-Match':original.headers['etag']}).status_code==200
  a=tile_entry(store,view['id'],0,0,0,packed=True)
  assert tile_entry(store,view['id'],0,0,0,packed=True) is a
  store.delete(saved['id'])
  changed=client.get(url+'?packed=true',headers={'If-None-Match':packed.headers['etag']})
  assert changed.headers['x-point-count']=='0' and changed.headers['etag']!=packed.headers['etag']
