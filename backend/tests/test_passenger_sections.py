from collections import defaultdict
import json
import sqlite3
import numpy as np
import pytest
from scipy.spatial import cKDTree
from shapely.geometry import LineString

from backend.passenger_sections import station_bins, merge_station_edges, downward_direction, station_mileage, trim_parallel_return
from backend.rail_coverage import RailIndex, read_coverage
from backend.store import TripStore


def stops():
 return [{'id':n,'name':n,'position':i*10000} for i,n in enumerate('ABCDE')]


def edges_for(ranges):
 stations=stops();edges=[]
 for start,end in station_bins(ranges,stations):
  for i in range(start,end):
   a,b=stations[i],stations[i+1]
   edges.append({'origin_id':a['id'],'destination_id':b['id'],'origin':a['name'],'destination':b['name'],
                 'start':[i,0],'end':[i+1,0],'distance_km':10,'order_start':i,'order_end':i+1})
 return merge_station_edges(edges)


def test_near_station_ends_share_boundary_and_merge():
 sections=edges_for([(100,9500),(11000,19800)])
 assert [(s['origin'],s['destination']) for s in sections]==[('A','C')]
 assert sections[0]['stations']==['A','B','C'] and sections[0]['station_count']==3


def test_two_intervals_with_exact_shared_station_merge_transitively():
 sections=edges_for([(0,10000),(10000,20000),(20000,30000)])
 assert [(s['origin'],s['destination']) for s in sections]==[('A','D')]
 assert sections[0]['distance_km']==30


def test_station_blocks_containing_partial_points_are_bounded_by_stations():
 assert station_bins([(4000,6000)],stops())==[(0,1)]
 assert station_bins([(12000,18000)],stops())==[(1,2)]


def test_unvisited_whole_station_block_remains_gap_and_order_is_not_distance():
 sections=edges_for([(0,10000),(20000,40000)])
 assert [(s['origin'],s['destination']) for s in sections]==[('A','B'),('C','E')]
 assert sections[0]['distance_km']<sections[1]['distance_km']


def test_reverse_recording_duplicate_and_overlapping_blocks_union():
 sections=edges_for([(30000,10000),(0,20000),(0,20000)])
 assert [(s['origin'],s['destination']) for s in sections]==[('A','D')]
 assert sections[0]['station_count']==4


def test_only_station_vicinity_or_exterior_does_not_create_unseen_pair():
 assert station_bins([(9700,10300),(-8000,-100),(40100,50000)],stops())==[]


def test_different_station_identity_does_not_merge_similar_label():
 edge={'origin':'甲','destination':'乙','origin_id':'a','destination_id':'b','start':[0,0],'end':[1,0], 'distance_km':5,'order_start':0,'order_end':1}
 other={**edge,'origin':'乙','destination':'丙','origin_id':'different-b','destination_id':'c','order_start':1,'order_end':2}
 assert len(merge_station_edges([edge,other]))==2


def test_shared_hub_branches_form_one_continuous_display_interval():
 base={'origin':'A','destination':'B','origin_id':'a','destination_id':'b','start':[0,0],'end':[1,0], 'distance_km':5,'order_start':0,'order_end':1}
 c={**base,'origin':'B','destination':'C','origin_id':'b','destination_id':'c','order_start':1,'order_end':2}
 d={**c,'destination':'D','destination_id':'d','order_end':3}
 sections=merge_station_edges([base,c,d])
 assert len(sections)==1 and sections[0]['stations']==list('ABCD')
 assert sections[0]['distance_km']==15


def test_mileage_selects_correct_line_and_preserves_zero():
 assert station_mileage({'railway:position:exact:京沪高铁':'0','railway:position:exact:京沪':'500'},'京沪高铁')==0
 assert station_mileage({'railway:position:exact:京广高速':'12+250'},'京广高速线')==12.25
 assert station_mileage({'railway:position:exact:京沪高铁':'123'},'京沪线') is None


def test_downward_order_uses_mileage_over_conflicting_relation_endpoints():
 axis=LineString([(0,0),(20000,0)])
 stations=[{'position':0,'name':'B','mileage':20},{'position':20000,'name':'A','mileage':0}]
 d=downward_direction(axis,stations,'测试线',{'from':'B','to':'A'},{'A':[(20000,0)],'B':[(0,0)]})
 assert d['reverse'] and d['known'] and d['source']=='线路公里标递增'
 assert d['from']=='A' and d['to']=='B'


def test_from_to_orients_east_west_line_instead_of_geographic_north_south():
 axis=LineString([(0,0),(20000,0)])
 d=downward_direction(axis,[], '陇海线',{'from':'连云','to':'兰州西'},{'连云':[(20000,0)],'兰州西':[(0,0)]})
 assert d['reverse'] and d['source']=='线路起终点（推定）'
 assert not downward_direction(axis,[],'无资料线',{}, {})['known']


def test_station_index_keeps_freight_and_excludes_subway(tmp_path,monkeypatch):
 from backend import rail_coverage
 monkeypatch.setattr(rail_coverage,'passenger_directory',lambda p:{'甲':'AAA','乙':'BBB','丙':'CCC'})
 path=tmp_path/'catalog.sqlite3'
 with sqlite3.connect(path) as db:
  db.execute('create table lines(id integer,name text,tags text,ways text)')
  db.execute('insert into lines values(1,?,?,?)',('测试线',json.dumps({'network':'中国铁路'}),'[1]'))
  db.execute('create table ways(id integer,nodes text,geometry text,tags text)')
  db.execute('insert into ways values(1,?,?,?)',('[1,2]','[[116,39],[116.1,39]]','{}'))
  db.execute('create table stations(id integer,name text,lon real,lat real,tags text)')
  db.executemany('insert into stations values(?,?,?,?,?)',[(1,'甲站',116,39,'{}'),(2,'货运站',116.02,39,'{}'),(3,'乙',116.05,39,'{"passenger":"no"}'),(4,'丙',116.08,39,'{"station":"subway"}'),(5,'建设中站',116.07,39,'{"construction:railway":"station"}'),(6,'货运站',116.0201,39,'{}'),(7,'其他货场',116.0201,39,'{}')])
 idx=RailIndex(path)
 assert idx.station_names==['甲','货运','乙','货运','其他货场']
 assert idx.station_ids==['AAA','osm:2','osm:3','osm:2','osm:7']


def test_transfer_station_is_boundary_of_both_lines():
 idx=object.__new__(RailIndex);idx.lines={1:{'name':'横线','tags':{'from':'A','to':'C'}},2:{'name':'竖线','tags':{'from':'B','to':'E'}}};idx.graph=defaultdict(lambda:defaultdict(list));idx.ways={}
 ways=[(1,1,1,2,[(0,0),(10000,0)]),(2,1,2,3,[(10000,0),(20000,0)]),(3,2,2,4,[(10000,0),(10000,10000)]),(4,2,4,5,[(10000,10000),(10000,20000)])]
 coverage={}
 for wid,line,a,b,xy in ways:
  g=LineString(xy);idx.ways[wid]={'line':line,'nodes':(a,b),'geom':g,'length':g.length};coverage[wid]=[(0,g.length)]
  idx.graph[line][a].append((b,wid,g.length));idx.graph[line][b].append((a,wid,g.length))
 idx.station_names=list('ABCDE');idx.station_ids=list('ABCDE');idx.station_tags=[{}]*5
 idx.station_xy=np.array([[0,0],[10000,0],[20000,0],[10000,10000],[10000,20000]])
 idx.station_tree=cKDTree(idx.station_xy);idx.named_station_xy={n:[tuple(p)] for n,p in zip(idx.station_names,idx.station_xy)}
 idx.observations=[];idx.unresolved_lines=[]
 lines=idx.reference_sections(coverage,{'1':{'trip'},'2':{'trip'}})
 assert len(lines)==2
 sections={l['name']:l['intervals'][0] for l in lines}
 assert sections['横线']['stations']==['A','B','C']
 assert sections['竖线']['stations']==['B','D','E']


def test_old_coverage_schema_is_marked_stale(tmp_path):
 store=TripStore(tmp_path/'db.sqlite3');directory=tmp_path/'rail';directory.mkdir()
 (directory/'coverage.json').write_text(json.dumps({'lines':[],'fingerprint':'','coverage_version':1}))
 assert read_coverage(store)['stale']


def test_parallel_return_is_trimmed_without_cutting_a_real_bend():
 # A track diameter doubles back from B to A on a parallel track.
 axis=LineString([(10000,5),(0,5),(0,0),(20000,0)])
 trimmed=trim_parallel_return(axis,{'from':'A','to':'B'},{'A':[(0,0)],'B':[(20000,0)]})
 assert trimmed.length==20000
 # A station a few km before the physical end must not prevent removing
 # the other end's redundant return track, or remove the valid outer tail.
 with_tail=trim_parallel_return(axis,{'from':'A','to':'B'},{'A':[(0,0)],'B':[(17000,0)]})
 assert with_tail.length==20000
 bent=LineString([(10000,10000),(0,10000),(0,0),(20000,0)])
 assert trim_parallel_return(bent,{'from':'A','to':'B'},{'A':[(0,0)],'B':[(20000,0)]}) is bent


def test_neighboring_line_station_does_not_become_boundary():
 idx=object.__new__(RailIndex);idx.lines={1:{'name':'测试线','tags':{'from':'A','to':'C'},'station_nodes':{101,103}}};idx.graph=defaultdict(lambda:defaultdict(list))
 g=LineString([(0,0),(20000,0)]);idx.ways={1:{'line':1,'nodes':(1,2),'geom':g,'length':g.length}}
 idx.graph[1][1].append((2,1,g.length));idx.graph[1][2].append((1,1,g.length))
 idx.station_names=list('ABC');idx.station_ids=list('ABC');idx.station_nodes=[101,102,103];idx.station_tags=[{}]*3
 idx.station_xy=np.array([[0,0],[10000,10],[20000,0]]);idx.named_station_xy={n:[tuple(p)] for n,p in zip(idx.station_names,idx.station_xy)}
 idx.observations=[];idx.unresolved_lines=[]
 from shapely.strtree import STRtree
 idx.station_claims={102:{2}};idx.members={1:[1]};idx.ids=[1];idx.tree=STRtree([g])
 result=idx.reference_sections({1:[(0,20000)]},{'1':{'trip'}})
 assert result[0]['intervals'][0]['stations']==['A','C']


def test_shared_freight_station_joins_passenger_intervals():
 edge={'origin':'甲客运站','destination':'乙货运站','origin_id':'AAA','destination_id':'osm:123',
       'origin_kind':'客运站','destination_kind':'铁路站点','start':[0,0],'end':[1,0],
       'distance_km':5,'order_start':0,'order_end':5}
 other={**edge,'origin':'乙货运站','destination':'丙线路所','origin_id':'osm:123','destination_id':'osm:456',
        'origin_kind':'铁路站点','destination_kind':'线路所','start':[1,0],'end':[2,0],'order_start':5,'order_end':10}
 result=merge_station_edges([edge,other])
 assert len(result)==1 and result[0]['stations']==['甲客运站','乙货运站','丙线路所']
 assert result[0]['destination_kind']=='线路所'


def test_short_connector_is_accepted_without_weakening_mainline_threshold():
 from backend.rail_coverage import project
 from shapely.strtree import STRtree
 idx=object.__new__(RailIndex);g=LineString([project(116,39),project(116.1,39)])
 idx.ids=[1];idx.tree=STRtree([g]);idx.ways={1:{'line':1,'geom':g,'nodes':(1,2),'length':g.length}}
 idx.lines={1:{'name':'测试联络线'}}
 points=[{'longitude':116+i*.001,'latitude':39,'segment_id':'0','speed_kmh':60,'altitude_m':20} for i in range(20)]
 assert idx.match_trip({'id':'connector','mode':'other','points':points})[1]=={'1':'connector'}
 idx.lines[1]['name']='测试干线'
 assert idx.match_trip({'id':'main','mode':'other','points':points})[1]=={}


def test_length_threshold_is_strict_and_requires_continuous_evidence():
 from backend.passenger_sections import interval_evidence
 assert not interval_evidence(0,10000,[(0,1000)],[])['reached']
 assert interval_evidence(0,10000,[(0,1001)],[])['reached']
 e=interval_evidence(0,10000,[(0,2000),(8000,10000)],[])
 assert e['coverage_ratio']==.4 and not e['continuous'] and not e['reached']
 assert interval_evidence(0,10000,[(0,1500),(1000,2000)],[])['covered_distance_m']==2000


def test_density_threshold_and_empty_adjacent_passenger_block():
 from backend.passenger_sections import interval_evidence
 assert not interval_evidence(0,20000,[],[3000,7000])['reached']
 assert interval_evidence(0,20000,[],[3000,7000,15000],[0,10000,20000])['reached']
 e=interval_evidence(0,30000,[],[1000,2000,21000,22000],[0,10000,20000,30000])
 assert e['points_per_10km']>1 and not e['passenger_blocks_complete'] and not e['reached']


def test_sparse_points_join_nonadjacent_boundaries_only_with_all_passenger_blocks():
 from backend.passenger_sections import reached_station_bins
 stations=stops()
 assert reached_station_bins([],stations,[3000,5000,15000,25000,35000,37000])==[(0,4)]
 assert reached_station_bins([],stations,[3000,5000,25000,35000,37000])==[(0,1),(2,4)]
 # Freight stations do not add a density gap requirement.
 stations[1]['kind']='货运站';stations[2]['kind']='线路所'
 assert reached_station_bins([],stations,[3000,5000,25000,35000,37000])==[(0,4)]


def test_continuous_length_does_not_join_disconnected_recordings():
 from backend.passenger_sections import reached_station_bins
 assert reached_station_bins([(0,9000),(21000,40000)],stops(),[])==[(0,1),(2,4)]


def test_station_arrival_is_earliest_day_and_unobserved_station_remains_unknown():
 from backend.rail_coverage import record_station_arrivals
 idx=object.__new__(RailIndex)
 idx.passenger_indexes=np.array([0,1,2]);idx.passenger_tree=cKDTree([[0,0],[10000,0],[20000,0]])
 idx.station_ids=['A','B','C'];idx.station_arrivals={}
 record_station_arrivals(idx,[np.array([[100,0],[10100,0]])],'2024-04-30')
 record_station_arrivals(idx,[np.array([[50,0]])],'2017-08-04')
 record_station_arrivals(idx,[np.array([[100,0]])],'2026-01-01')
 assert idx.station_arrivals=={'A':'2017-08-04','B':'2024-04-30'}


def test_station_details_dates_and_kind_survive_shared_boundary_merge():
 rows=[]
 for i,(a,b) in enumerate(zip('ABC','BCD')):
  rows.append({'origin':a,'destination':b,'origin_id':a,'destination_id':b,'start':[i,0],'end':[i+1,0],
   'distance_km':10,'order_start':i,'order_end':i+1,
   'origin_station':{'id':a,'name':a,'kind':'客运站' if a!='B' else '线路所','first_arrival_date':'2024-01-01' if a=='A' else None},
   'destination_station':{'id':b,'name':b,'kind':'客运站' if b!='B' else '线路所','first_arrival_date':'2024-02-01' if b=='C' else None}})
 rows[1]['reference_gap_m']=2500
 section=merge_station_edges(rows)[0]
 assert section['reference_gaps']==[{'origin':'B','destination':'C','distance_m':2500}]
 assert section['stations']==list('ABCD')
 assert section['destination_station']['id']=='D'
 assert [(s['name'],s['kind'],s['first_arrival_date']) for s in section['station_details']]==[
  ('A','客运站','2024-01-01'),('B','线路所',None),('C','客运站','2024-02-01'),('D','客运站',None)]


def test_extra_disconnected_fragment_does_not_revoke_sufficient_continuous_run():
 from backend.passenger_sections import reached_station_bins
 stations=[{'position':0,'kind':'客运站'},{'position':10000,'kind':'客运站'}]
 assert reached_station_bins([(100,4000)],stations,[])==[(0,1)]
 assert reached_station_bins([(100,4000),(6000,6100)],stations,[])==[(0,1)]
 # Two individually insufficient fragments cannot pool length to cross 10%.
 assert reached_station_bins([(3000,3600),(6000,6600)],stations,[])==[]


def test_near_station_density_point_is_not_lost_to_endpoint_snap():
 from backend.passenger_sections import reached_station_bins
 stations=[{'position':0,'kind':'客运站'},{'position':4200,'kind':'线路所'}]
 assert reached_station_bins([],stations,[4100])==[(0,1)]


def test_density_connects_small_reference_hole_but_never_creates_length_evidence():
 from types import SimpleNamespace
 from backend.rail_density_links import density_links
 axes={(1,0):LineString([(0,0),(10000,0)]),(1,1):LineString([(12500,0),(40000,0)])}
 st={(1,0):[{'id':'A','name':'A','position':9000,'xy':(9000,0),'kind':'客运站'}],
     (1,1):[{'id':'B','name':'B','position':20000,'xy':(32500,0),'kind':'客运站'}]}
 idx=SimpleNamespace(point_observations=[(1,np.array([[9500,0],[14000,0],[23000,0],[30000,0]]))],station_arrivals={})
 links=density_links(idx,axes,st,{1:((1,0),axes[(1,0)])},{1:{'reverse':False}})[1]
 assert len(links)==1 and links[0]['origin']=='A' and links[0]['destination']=='B'
 assert links[0]['reference_gap_m']==2500 and '点密度' in links[0]['evidence_method']
 # A passenger station in the gap introduces an empty adjacent passenger block.
 st[(1,0)].append({'id':'C','name':'C','position':10000,'xy':(10000,0),'kind':'客运站'})
 idx.point_observations=[(1,np.array([[23000,0],[25000,0],[27000,0],[30000,0]]))]
 assert all(e['origin']!='A' for e in density_links(idx,axes,st,{1:((1,0),axes[(1,0)])},{1:{'reverse':False}})[1])


def test_passenger_station_inside_catalog_hole_is_not_omitted_from_density_check():
 from types import SimpleNamespace
 from backend.rail_density_links import density_links
 axes={(1,0):LineString([(0,0),(10000,0)]),(1,1):LineString([(12500,0),(40000,0)])}
 stops={(1,0):[{'id':'A','name':'A','position':9000,'xy':(9000,0),'kind':'客运站'}],
        (1,1):[{'id':'B','name':'B','position':20000,'xy':(32500,0),'kind':'客运站'}]}
 idx=SimpleNamespace(point_observations=[(1,np.array([[14000,0],[23000,0],[30000,0]]))],station_arrivals={},
  station_nodes=[5],station_xy=np.array([[11500,0]]),station_claims={5:{1}},station_ids=['C'],station_names=['C'],station_kinds=['客运站'])
 assert not density_links(idx,axes,stops,{1:((1,0),axes[(1,0)])},{1:{'reverse':False}})


@pytest.mark.parametrize('line_id,gap,reverse',[(77,400,False),(888,2800,True),(9001,4800,False)])
def test_reference_hole_rule_is_independent_of_line_identity(line_id,gap,reverse):
 from types import SimpleNamespace
 from backend.rail_density_links import density_links
 key0=(line_id,0);key1=(line_id,1)
 axes={key0:LineString([(0,0),(10000,0)]),key1:LineString([(10000+gap,0),(40000,0)])}
 stops={key0:[{'id':'A','name':'虚拟客运站A','position':9000,'xy':(9000,0),'kind':'客运站'}],
        key1:[{'id':'B','name':'虚拟客运站B','position':40000-(10000+gap),'xy':(40000,0),'kind':'客运站'}]}
 idx=SimpleNamespace(point_observations=[(line_id,np.array([[9500,0],[15000,0],[20000,0],[30000,0],[39500,0]]))],station_arrivals={})
 rows=density_links(idx,axes,stops,{line_id:(key0,axes[key0])},{line_id:{'reverse':reverse}})[line_id]
 assert len(rows)==1 and rows[0]['reference_gap_m']==gap
 assert (rows[0]['origin_id'],rows[0]['destination_id'])==(('B','A') if reverse else ('A','B'))


@pytest.mark.parametrize('points,junction', [([1000,3000,4000],5000),([16000,17000,19000],15000)])
def test_recorded_prefix_before_transfer_does_not_claim_far_station(points,junction):
 from backend.passenger_sections import reached_station_bins
 stations=[{'position':0,'kind':'客运站'},{'position':20000,'kind':'客运站'}]
 assert reached_station_bins([],stations,points)==[(0,1)]
 assert reached_station_bins([],stations,points,[junction])==[]


def test_continuous_length_also_cannot_extend_past_unrecorded_transfer_tail():
 from backend.passenger_sections import reached_station_bins
 stations=[{'position':0,'kind':'客运站'},{'position':20000,'kind':'客运站'}]
 assert reached_station_bins([(100,4100)],stations,[],[5000])==[]
 assert reached_station_bins([],stations,[1000,3000,15000],[5000])==[(0,1)]


def test_transfer_cut_rechecks_density_of_surviving_fragments():
 from backend.passenger_sections import reached_station_bins
 points=list(range(100,9900,200))+[10500,25000]
 assert reached_station_bins([],stops()[:4],points,[11000])==[(0,1)]


def test_reverse_parallel_edge_and_missing_station_shortcut_do_not_create_forks():
 def edge(a,b,length=10):
  return {'origin':a,'destination':b,'origin_id':a,'destination_id':b,'distance_km':length,'order_start':ord(a),'order_end':ord(b),'start':[ord(a),0],'end':[ord(b),0]}
 sections=merge_station_edges([edge('A','B'),edge('B','C'),edge('C','B'),edge('A','C',20),edge('C','D')])
 assert len(sections)==1 and sections[0]['stations']==list('ABCD') and sections[0]['distance_km']==30


def test_transfer_topology_includes_junction_inside_unsplit_way(tmp_path):
 from backend.rail_topology import transfer_nodes
 with sqlite3.connect(tmp_path/'topology.sqlite3') as db:
  db.execute('CREATE TABLE ways(id INTEGER,nodes TEXT,geometry TEXT)')
  db.executemany('INSERT INTO ways VALUES(?,?,?)',[(1,'[1,2,3]','[[116,39],[116.01,39],[116.02,39]]'),(2,'[2,4]','[[116.01,39],[116.01,39.01]]')])
  result=transfer_nodes(db,{1:[11],2:[22]})
 assert len(result[11])==len(result[22])==1


def test_separate_station_tracks_with_actual_divergence_supply_transfer_guard():
 from types import SimpleNamespace
 from backend.rail_topology import transfer_positions
 idx=SimpleNamespace(station_claims={1:{11,22}},ways={1:{'geom':LineString([(0,20),(5000,20),(10000,5000)])}},reference_way_ids={22:[1]})
 axis=LineString([(0,0),(20000,0)])
 positions=transfer_positions(idx,11,axis,[{'position':0,'node':1}])
 assert any(5000<p<5300 for p in positions)
 from backend.passenger_sections import reached_station_bins
 assert reached_station_bins([], [{'position':0},{'position':20000}], [1000,3000,4000], positions)==[]


def test_clipped_axis_cannot_supply_impossibly_short_station_distance():
 from backend.rail_topology import physically_valid_edge
 a={'xy':(0,0),'position':0};b={'xy':(10000,0),'position':1000}
 assert not physically_valid_edge(a,b)
 b['position']=10000
 assert physically_valid_edge(a,b)


def moving_index():
 from shapely.strtree import STRtree
 from backend.rail_coverage import project
 idx=object.__new__(RailIndex);g=LineString([project(116,39),project(116.1,39)])
 idx.ids=[1];idx.ways={1:{'geom':g,'line':11,'length':g.length,'nodes':(1,2)}};idx.members={1:[11,22]};idx.tree=STRtree([g]);idx.point_observations=[]
 return idx


def test_zero_reported_speed_is_recovered_only_from_original_motion_and_time():
 from datetime import datetime,timedelta,timezone
 idx=moving_index();start=datetime(2026,1,1,tzinfo=timezone.utc)
 points=[{'longitude':116+i*.001,'latitude':39,'speed_kmh':0,'segment_id':'s','timestamp':(start+timedelta(seconds=i)).isoformat()} for i in range(4)]
 _,matched=idx.match_trip({'id':'trip','mode':'other','points':points})
 assert set(matched)=={'11','22'}
 assert all(p['speed_kmh']==0 for p in points)
 # Stationary fixes count as density points, but create no measured length.
 for p in points:p['longitude']=116
 stationary=moving_index();coverage,matched=stationary.match_trip({'id':'trip','mode':'other','points':points})
 assert not coverage and set(matched)=={'11','22'}
 assert sum(len(xy) for _,xy in stationary.point_observations)==8


def test_sparse_recording_gap_bearing_is_not_treated_as_local_track_heading():
 idx=moving_index()
 points=[{'longitude':118,'latitude':38,'speed_kmh':100,'segment_id':'s'},{'longitude':116.02,'latitude':39,'speed_kmh':100,'segment_id':'s'}]
 assert idx.match_trip({'id':'trip','mode':'other','points':points})[1]


def test_unassigned_yard_siding_is_not_another_railway_exit(tmp_path):
 from backend.rail_topology import transfer_nodes
 with sqlite3.connect(tmp_path/'yard.sqlite3') as db:
  db.execute('CREATE TABLE ways(id INTEGER,nodes TEXT,geometry TEXT)')
  db.executemany('INSERT INTO ways VALUES(?,?,?)',[(1,'[1,2,3]','[[116,39],[116.01,39],[116.02,39]]'),(2,'[2,4]','[[116.01,39],[116.01,39.01]]')])
  assert not transfer_nodes(db,{1:[11]})


def test_slow_tail_presence_does_not_create_density_evidence():
 from backend.passenger_sections import reached_station_bins
 stations=[{'position':0,'kind':'客运站'},{'position':20000,'kind':'客运站'}]
 assert reached_station_bins([],stations,[1000,3000,4000],[5000],[6000])==[(0,1)]
 assert reached_station_bins([],stations,[],[5000],[1000,3000,6000])==[]


def test_near_station_continuation_prevents_false_transfer_cut():
 from backend.passenger_sections import reached_station_bins
 stations=[{'position':0,'kind':'客运站'},{'position':10000,'kind':'客运站'},{'position':20000,'kind':'客运站'}]
 assert reached_station_bins([],stations,[100,400,9200,11000,17000],[9300])==[(0,2)]


def test_station_switch_is_not_an_unrecorded_mid_block_exit():
 from backend.passenger_sections import reached_station_bins
 stations=[{'position':0,'kind':'客运站'},{'position':10000,'kind':'客运站'}]
 assert reached_station_bins([],stations,[1000,5000,9000],[9999.9])==[(0,1)]


def test_nested_railway_parts_are_inherited_once_including_stations():
 from backend.rail_relations import railway_members
 rels={1:{'members':[{'type':'way','ref':5},{'type':'relation','ref':2},{'type':'relation','ref':3}]},
       2:{'members':[{'type':'way','ref':6},{'type':'node','ref':7},{'type':'relation','ref':3}]},
       3:{'members':[{'type':'way','ref':6},{'type':'node','ref':8},{'type':'relation','ref':1}]}}
 assert railway_members(1,rels)==([5,6],[7,8],[])


def test_missing_nested_relation_is_reported_not_silently_ignored():
 from backend.rail_relations import railway_members
 assert railway_members(1,{1:{'members':[{'type':'relation','ref':2}]}})==([],[],[2])


def test_stationary_density_presence_does_not_require_a_motion_anchor():
 from datetime import datetime,timedelta,timezone
 idx=moving_index();idx.boundary_observations=[]
 start=datetime(2026,1,1,tzinfo=timezone.utc)
 def point(x,i,speed):return {'longitude':x,'latitude':39,'speed_kmh':speed,'segment_id':'s','timestamp':(start+timedelta(seconds=i)).isoformat()}
 idx.match_trip({'id':'trip','mode':'other','points':[point(116.01,0,100),point(116.011,1,100),point(116.011,2,0)]})
 assert sum(len(xy) for _,xy in idx.point_observations)==6
 assert not idx.boundary_observations
 idx=moving_index();idx.boundary_observations=[]
 idx.match_trip({'id':'trip','mode':'other','points':[point(116.011,0,0),point(116.011,1,0)]})
 assert not idx.boundary_observations
 assert sum(len(xy) for _,xy in idx.point_observations)==4


def test_parallel_track_choice_follows_nearby_clear_records_without_date_rules():
 from backend.rail_track_choice import choose_tracks
 from shapely.strtree import STRtree
 from types import SimpleNamespace
 from datetime import datetime,timedelta,timezone
 gs=[LineString([(0,0),(10000,0)]),LineString([(6000,80),(20000,80)])]
 idx=SimpleNamespace(ids=[1,2],tree=STRtree(gs),ways={1:{'geom':gs[0],'line':11},2:{'geom':gs[1],'line':22}},members={1:[11],2:[22]})
 xy=np.array([[1000,0],[2000,0],[3000,0],[4000,0],[7000,60],[8000,60],[15000,80]])
 t=datetime(2020,1,1,tzinfo=timezone.utc)
 points=[{'segment_id':'s','timestamp':(t+timedelta(seconds=20*i)).isoformat()} for i in range(len(xy))]
 matches=choose_tracks(idx,xy,points,list(range(len(xy))),{})
 assert matches[4][0]==matches[5][0]==11
 assert matches[6][0]==22
 # Without clear surrounding records, retain the nearest-track inference.
 uncertain=xy[4:6].copy();uncertain[:,1]=40
 assert choose_tracks(idx,uncertain,points[4:6],[0,1],{})
 clear=uncertain.copy();clear[:,1]=5
 assert all(m[0]==11 for m in choose_tracks(idx,clear,points[4:6],[0,1],{}).values())


def test_heading_is_checked_before_choosing_nearest_railway():
 from backend.rail_track_choice import choose_tracks
 from shapely.strtree import STRtree
 from types import SimpleNamespace
 gs=[LineString([(100,-1000),(100,1000)]),LineString([(0,30),(2000,30)])]
 idx=SimpleNamespace(ids=[1,2],tree=STRtree(gs),ways={1:{'geom':gs[0],'line':11},2:{'geom':gs[1],'line':22}},members={1:[11],2:[22]})
 assert choose_tracks(idx,np.array([[100,0]]),[{'segment_id':'s'}],[0],{0:np.array([100,0])})[0][0]==22


def test_cache_publication_preserves_unresolved_dates_and_is_readable(tmp_path):
 from types import SimpleNamespace
 from backend.rail_coverage import _cache_result, fingerprint
 store=TripStore(tmp_path/'footprint.sqlite3');(tmp_path/'rail').mkdir()
 idx=SimpleNamespace(unresolved_lines=[{'line_id':'1','name':'测试线路'}])
 _cache_result(store,idx,[],[{'id':'t','travel_date':'2021-01-02'}],fingerprint(store),{'1':{'t'}})
 data=read_coverage(store)
 assert not data['stale'] and data['unresolved_lines'][0]['dates']==['2021-01-02']


def test_density_pools_stationary_fixes_across_recording_segments():
 idx=moving_index()
 points=[{'longitude':116+.01*i,'latitude':39,'segment_id':str(i),'speed_kmh':0} for i in range(1,5)]
 coverage,matched=idx.match_trip({'id':'trip','mode':'other','points':points})
 assert not coverage and set(matched)=={'11','22'}
 assert sum(len(xy) for _,xy in idx.point_observations)==8


def test_density_cargo_boundary_checks_clipped_passenger_block():
 from backend.passenger_sections import interval_evidence
 e=interval_evidence(10000,30000,[],[22000,23000,24000],[0,20000,40000])
 assert e['points_per_10km']>1 and not e['reached']


def test_transfer_inside_freight_gap_uses_whole_containing_passenger_block():
 from backend.passenger_sections import reached_station_bins
 stations=[{'position':i*10000,'kind':'客运站' if i in (0,3) else '货运站'} for i in range(4)]
 points=list(range(100,9900,200))+[10500,25000]
 assert reached_station_bins([],stations,points,[11000])==[(0,3)]


def test_station_offsets_are_not_fabricated_axis_shortcuts():
 from backend.rail_topology import physically_valid_edge
 a={'xy':(0,0),'position':0,'distance':150};b={'xy':(8600,0),'position':8300,'distance':150}
 assert physically_valid_edge(a,b)
 b.update(xy=(20000,0),position=100)
 assert not physically_valid_edge(a,b)


def test_context_can_recover_candidate_rejected_by_jitter_bearing():
 from backend.rail_track_choice import choose_tracks
 from shapely.strtree import STRtree
 from types import SimpleNamespace
 from datetime import datetime,timedelta,timezone
 gs=[LineString([(0,0),(10000,0)]),LineString([(6000,80),(20000,80)])]
 idx=SimpleNamespace(ids=[1,2],tree=STRtree(gs),ways={1:{'geom':gs[0],'line':11},2:{'geom':gs[1],'line':22}},members={1:[11],2:[22]})
 xy=np.array([[1000,0],[2000,0],[3000,0],[4000,0],[7000,60]])
 t=datetime(2020,1,1,tzinfo=timezone.utc)
 points=[{'segment_id':str(i),'timestamp':(t+timedelta(seconds=20*i)).isoformat()} for i in range(5)]
 assert choose_tracks(idx,xy,points,list(range(5)),{4:np.array([0,100])})[4][0]==11


def test_low_accuracy_fixes_do_not_prove_railway_identity():
 idx=moving_index()
 points=[{'longitude':116+.01*i,'latitude':39,'segment_id':'s','speed_kmh':100,'_accuracy_m':2000} for i in range(4)]
 assert idx.match_trip({'id':'trip','mode':'other','points':points})==({},{})
 assert not idx.point_observations


def test_shared_station_aligns_components_instead_of_clipping_their_order():
 from backend.rail_station_order import station_orders
 from types import SimpleNamespace
 axes={(1,0):LineString([(10000,0),(20000,0)]),(1,1):LineString([(0,0),(10000,0)])}
 st={(1,0):[{'id':'B','name':'B','xy':(10000,0),'position':0},{'id':'C','name':'C','xy':(20000,0),'position':10000}],
     (1,1):[{'id':'A','name':'A','xy':(0,0),'position':0},{'id':'B','name':'B','xy':(10000,0),'position':10000}]}
 idx=SimpleNamespace(lines={1:{'name':'测试线','tags':{'from':'A','to':'C'}}},named_station_xy={'A':[(0,0)],'C':[(20000,0)]})
 orders,_=station_orders(idx,axes,st,{1:((1,0),axes[1,0])},{1:{'reverse':False}})
 assert orders[1,'A']<orders[1,'B']<orders[1,'C']


def test_long_density_terminal_snaps_cluster_back_to_reached_station():
 from backend.passenger_sections import reached_station_bins
 stations=[{'position':0,'kind':'客运站'},{'position':10000,'kind':'客运站'},{'position':11500,'kind':'货运站'}]
 assert reached_station_bins([],stations,[100,3000,9900,10100])==[(0,1)]
 # A valid continuous length edge still follows its own >10% rule.
 assert reached_station_bins([(10000,11200)],stations,[100,3000,9900,10100])==[(0,2)]


def test_geometrically_shared_only_line_needs_exclusive_observations_to_stay():
 from backend.rail_overlap import shared_only_candidates
 from shapely.strtree import STRtree
 from types import SimpleNamespace
 gs=[LineString([(0,0),(10000,0)]),LineString([(0,10),(30000,10)])]
 idx=SimpleNamespace(point_observations=[(1,np.array([[1000,0],[2000,0]])),(2,np.array([[1000,10],[12000,10],[25000,10]]))],
     reference_way_ids={1:[1],2:[2]},ways={1:{'geom':gs[0]},2:{'geom':gs[1]}},members={1:[1],2:[2]},ids=[1,2],tree=STRtree(gs),lines={1:{'name':'甲线'},2:{'name':'乙线'}})
 candidates=shared_only_candidates(idx)
 assert len(candidates)==1 and candidates[0]['line_id']=='1' and candidates[0]['attributed_to_id']=='2'
 assert len(idx.point_observations)==2  # Inference never deletes source observations.
 idx.point_observations[0]=(1,np.array([[1000,0],[2000,500]]))
 assert not shared_only_candidates(idx)


def test_national_catalog_line_does_not_require_optional_operator_tags(tmp_path,monkeypatch):
 import backend.rail_coverage as coverage
 path=tmp_path/'catalog.sqlite3'
 with sqlite3.connect(path) as db:
  db.execute('CREATE TABLE lines(id INTEGER,name TEXT,tags TEXT,ways TEXT,stations TEXT)')
  db.execute('CREATE TABLE ways(id INTEGER,nodes TEXT,geometry TEXT,tags TEXT)')
  db.execute('CREATE TABLE stations(id INTEGER,name TEXT,lon REAL,lat REAL,tags TEXT)')
  db.execute('INSERT INTO lines VALUES(1,?,?,?,?)',('测试高速线','{"route":"railway","ref":"3027"}','[1]','[]'))
  db.execute('INSERT INTO ways VALUES(1,?,?,?)',('[1,2]','[[116,39],[116.01,39]]','{}'))
  db.execute('INSERT INTO lines VALUES(2,?,?,?,?)',('新建客运铁路','{"route":"railway","from":"甲","to":"乙"}','[]','[]'))
 monkeypatch.setattr(coverage,'passenger_directory',lambda p:{'甲':'AAA'})
 index=RailIndex(path)
 assert 1 in index.lines and 2 in index.lines and 1 in index.ways


def test_repeated_station_oscillations_do_not_count_as_measured_travel():
 idx=moving_index();idx.lines={11:{'name':'测试干线'}}
 points=[{'longitude':116.01 if i%2 else 116.02,'latitude':39,'segment_id':'s','speed_kmh':100} for i in range(20)]
 coverage,matched=idx.match_trip({'id':'trip','mode':'other','points':points})
 assert not coverage and matched
 assert sum(len(xy) for _,xy in idx.point_observations)==40


def test_rail_quality_read_does_not_modify_saved_payload_or_csv(tmp_path):
 from backend.store import TripStore
 from backend.models import TripCreate
 from backend.rail_evidence import load_rail_trip
 store=TripStore(tmp_path/'footprint.sqlite3')
 record=store.create(TripCreate(title='测试',mode='other',travel_date='2026-01-01',points=[{'longitude':116,'latitude':39}]))
 raw='1767225600,1,116,39,0,2000,-1,0,1,0,0'
 with store.connect() as db:db.execute('INSERT INTO point_csv(trip_id,sequence,row_order,raw) VALUES(?,?,?,?)',(record['id'],0,0,raw))
 rail=load_rail_trip(store,record['id'])
 assert rail['points'][0]['_accuracy_m']==2000
 assert '_accuracy_m' not in store.get(record['id'])['points'][0]
 with store.connect() as db:assert db.execute('SELECT raw FROM point_csv').fetchone()['raw']==raw


def test_precise_roadside_fix_does_not_get_blanket_90m_rail_tolerance():
 from backend.rail_track_choice import choose_tracks
 from shapely.strtree import STRtree
 from types import SimpleNamespace
 g=LineString([(0,0),(10000,0)])
 idx=SimpleNamespace(ids=[1],tree=STRtree([g]),ways={1:{'geom':g,'line':11}},members={1:[11]})
 points=[{'segment_id':'s','_accuracy_m':2},{'segment_id':'s','_accuracy_m':30},{'segment_id':'s'}]
 matches=choose_tracks(idx,np.array([[1000,85],[2000,85],[3000,85]]),points,[0,1,2],{})
 assert 0 not in matches and 1 in matches and 2 in matches
