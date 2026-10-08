from types import SimpleNamespace
import numpy as np
from scipy.spatial import cKDTree
from backend.rail_aliases import canonical_control_station_ids, collapse_line_aliases


def test_control_facility_duplicate_preserves_distinct_official_markers():
    xy=np.array([[0,0],[1400,0],[1800,0],[18000,0]])
    index=SimpleNamespace(station_tree=cKDTree(xy),station_ids=['osm:1','osm:2','osm:3','osm:4'],
        station_nodes=[1,2,3,4],station_names=['甲线路所']*4,station_kinds=['线路所']*4,
        station_claims={1:{10,20},2:{10,20},3:{10},4:{10}},
        station_tags=[{'railway:position':'10'},{'railway:position':'10'},{'railway:position':'12'},{}])
    ids=canonical_control_station_ids(index)
    assert ids[0]==ids[1]
    assert len({ids[0],ids[2],ids[3]})==3


def test_line_alias_requires_same_identifier_physical_members_and_station_subset():
    index=SimpleNamespace(lines={1:{'tags':{'ref':'3001'}},2:{'tags':{'ref':'3001'}},3:{'tags':{'ref':'3002'}},4:{'tags':{'ref':'3001'}}},
        reference_way_ids={1:list(range(10)),2:list(range(8)),3:list(range(8)),4:list(range(20,30))})
    def row(i):return {'line_id':str(i),'name':str(i),'intervals':[{'station_details':[{'id':'A'},{'id':'B'}]}]}
    lines=collapse_line_aliases(index,[row(i) for i in range(1,5)])
    assert {r['line_id'] for r in lines}=={'1','3','4'}
    assert lines[0]['aliases'][0]['line_id']=='2'
    extra=row(2);extra['intervals'][0]['station_details'].append({'id':'C'})
    assert len(collapse_line_aliases(index,[row(1),extra]))==2


def test_shared_start_uses_qualifying_line_even_if_another_has_more_raw_points():
    from shapely.geometry import LineString
    from shapely.strtree import STRtree
    from backend.rail_overlap import shared_only_candidates
    gs=[LineString([(0,0),(10000,0)]),LineString([(0,10),(30000,10)]),LineString([(0,20),(30000,20)])]
    index=SimpleNamespace(point_observations=[(1,np.array([[1000,0],[2000,0]])),
        (2,np.array([[1000,10],[12000,10],[19000,10],[25000,10]])),(3,np.array([[1000,20],[12000,20],[25000,20]]))],
        reference_way_ids={1:[1],2:[2],3:[3]},ways={i+1:{'geom':g} for i,g in enumerate(gs)},
        members={1:[1],2:[2],3:[3]},ids=[1,2,3],tree=STRtree(gs),lines={i:{'name':str(i)} for i in range(1,4)})
    result=shared_only_candidates(index,{'3':20})
    assert next(r for r in result if r['line_id']=='1')['attributed_to_id']=='3'
    assert not shared_only_candidates(index,{})


def test_connected_reference_fragments_do_not_interleave_station_order():
    from backend.rail_station_order import order_reached_stations
    index=SimpleNamespace(station_orders={(1,'A'):0,(1,'B'):10000,(1,'C'):5000,(1,'D'):15000,(1,'X'):100000,(1,'Y'):110000})
    def edge(a,b,km):return {'origin_id':a,'destination_id':b,'distance_km':km}
    edges={1:[edge('A','B',10),edge('B','C',5),edge('C','D',10),edge('X','Y',10)]}
    order_reached_stations(index,edges)
    assert [index.station_orders[1,s] for s in 'ABCD']==[0,10000,15000,25000]
    assert index.station_orders[1,'X']==100000  # Separate reached components retain their location.


def test_umbrella_duplicate_needs_same_recorded_points_and_physical_members():
    index=SimpleNamespace(lines={1:{'tags':{'ref':'甲'}},2:{'tags':{'ref':'乙'}}},
        reference_way_ids={1:list(range(10)),2:list(range(20))},
        point_sources=[(1,'t',np.array([[0,0],[100,0]])),(2,'t',np.array([[0,0],[100,0]]))])
    def row(i):return {'line_id':str(i),'name':str(i),'intervals':[{'station_details':[{'id':'A'},{'id':'B'}]}]}
    result=collapse_line_aliases(index,[row(1),row(2)])
    assert len(result)==1 and result[0]['line_id']=='1'
    index.point_sources[1]=(2,'another-day',np.array([[0,0],[100,0]]))
    assert len(collapse_line_aliases(index,[row(1),row(2)]))==2


def test_recovered_origin_branch_uses_established_down_direction():
    from backend.rail_station_order import order_reached_stations
    index=SimpleNamespace(station_orders={(1,'A'):9000,(1,'J'):0,(1,'B'):10000})
    edges={1:[{'origin_id':'A','destination_id':'J','origin':'起点','destination':'接点','distance_km':9},
        {'origin_id':'J','destination_id':'B','origin':'接点','destination':'终点','distance_km':10}]}
    order_reached_stations(index,edges,{1:{'from':'起点','to':'终点'}})
    assert index.station_orders[1,'A']<index.station_orders[1,'J']<index.station_orders[1,'B']
