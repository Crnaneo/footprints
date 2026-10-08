from types import SimpleNamespace
from collections import defaultdict
import numpy as np
from shapely.geometry import LineString
from backend.rail_coverage import project
from backend.rail_boundary_rules import BoundaryEvidence,geographic_distances


BASE=np.asarray(project(120,35))


def station(name,x,kind='客运站'):
    return {'id':name,'name':name,'kind':kind,'position':x,'xy':tuple(BASE+[x,0]),'distance':0,'node':name}


def setup(stops,points,raw=None,other=None):
    axis=LineString([stops[0]['xy'],stops[-1]['xy']])
    st={(1,0):stops};axes={(1,0):axis}
    idx=SimpleNamespace(point_sources=[(1,'record',np.asarray([BASE+p for p in points]).reshape(-1,2))],
        trip_dates={'record':'2020-01-01'},station_claims=defaultdict(set))
    if other:
        axes[2,0]=LineString([s['xy'] for s in other]);st[2,0]=other
    policy=BoundaryEvidence(idx,axes,st,raw_counter=raw or (lambda s:{'count':0,'first_arrival_date':None}))
    edges=[]
    for a,b in zip(stops,stops[1:]):
        edges.append({'origin':a['name'],'destination':b['name'],'origin_id':a['id'],'destination_id':b['id'],
            'origin_kind':a['kind'],'destination_kind':b['kind'],'origin_station':dict(a),'destination_station':dict(b),
            'distance_km':(b['position']-a['position'])/1000,'order_start':a['position'],'order_end':b['position'],
            'start':a['xy'],'end':b['xy'],'component':0})
        policy.routes[1,a['id'],b['id'],0]=LineString([a['xy'],b['xy']])
    return policy,edges


def test_recursive_terminal_pruning_rechecks_new_ends():
    stops=[station(n,i*2500) for i,n in enumerate('ABCDE')]
    policy,edges=setup(stops,[(5000,0),(5100,0),(7400,0),(7500,0)])
    kept,sections=policy.filter_edges(1,edges)
    assert [(s['origin'],s['destination']) for s in sections]==[('C','D')]
    assert len(kept)==1
    assert {r['station'] for r in policy.rejections}>={'A','B','E'}
    assert all(s['first_arrival_date']=='2020-01-01' for s in sections[0]['station_details'])


def test_single_remaining_supported_station_is_not_an_interval():
    stops=[station(n,i*2500) for i,n in enumerate('ABCDE')]
    policy,edges=setup(stops,[(5000,0),(5100,0)])
    assert policy.filter_edges(1,edges)==([],[])


def test_terminal_exact_thresholds_and_required_date():
    stops=[station('A',0),station('B',5000)]
    for count,reached in [(9,False),(10,True)]:
        policy,_=setup(stops,[],raw=lambda s:{'count':count,'first_arrival_date':'2019-01-01'})
        assert policy.terminal(1,stops[0],[])['reached']==reached
    policy,_=setup(stops,[(0,0)])
    assert not policy.terminal(1,stops[0],[])['reached']
    policy,_=setup(stops,[(0,0),(100,0)])
    assert policy.terminal(1,stops[0],[])['reached']
    policy.index.trip_dates.clear();policy.sources[1][3][:]=[None,None]
    assert not policy.terminal(1,stops[0],[])['reached']


def test_terminal_counts_only_displayed_route_not_other_branch():
    stops=[station('A',0),station('B',5000)]
    policy,edges=setup(stops,[(0,500),(0,600)])
    route=policy.routes[1,'A','B',0]
    assert not policy.terminal(1,stops[0],[route])['reached']


def test_geographic_circle_uses_real_distance_at_northern_latitude():
    origin=project(120,55)
    xy=[project(120+np.degrees(900/(6371008.8*np.cos(np.radians(55)))),55),
        project(120,55+np.degrees(1100/6371008.8))]
    assert np.allclose(geographic_distances(xy,origin),[900,1100],atol=.1)


def test_nonpassenger_section_needs_other_line_segment_evidence():
    stops=[station('A',0,'货运站'),station('B',5000,'线路所')]
    policy,edges=setup(stops,[(0,0),(100,0),(4900,0),(5000,0)])
    assert policy.filter_edges(1,edges)==([],[])
    other=[stops[0],station('Z',10000)]
    other[1]['xy']=tuple(BASE+[0,10000])
    policy,edges=setup(stops,[(0,0),(100,0),(4900,0),(5000,0)],other=other)
    xy=np.asarray([BASE+[0,1000]])
    from scipy.spatial import cKDTree
    policy.sources[2]=(xy,cKDTree(xy),['another'],['2020-02-01'])
    assert policy.filter_edges(1,edges)[1][0]['origin']=='A'
    # A point exactly at the shared station cannot prove its outgoing segment.
    policy.sources[2]=(np.asarray([BASE]),cKDTree([BASE]),['another'],['2020-02-01'])
    assert not policy.external_support(1,stops[0])


def hub_setup(count=6,passenger_b=False):
    stops=[station('C',0,'货运站'),station('A',5000),station('B',10000,'客运站' if passenger_b else '线路所')]
    other=[stops[1],station('Z',15000)];other[1]['xy']=tuple(BASE+[5000,10000])
    return setup(stops,[(5000+i*10,0) for i in range(count)],other=other),stops


def test_hub_short_edge_requires_more_than_five_and_inward_point():
    (policy,_),stops=hub_setup()
    assert (1,2) in policy.hub_pairs((1,0),stops,[9000],[])
    assert not policy.hub_pairs((1,0),stops,[],[])
    (policy,_),stops=hub_setup(count=5)
    assert not policy.hub_pairs((1,0),stops,[9000],[])
    (policy,_),stops=hub_setup(passenger_b=True)
    assert not policy.hub_pairs((1,0),stops,[9000],[])


def test_hub_bridge_requires_both_outer_records_and_both_inward_sides():
    (policy,_),stops=hub_setup(count=0,passenger_b=True)
    outer=[{'origin_id':'X','destination_id':'C'},{'origin_id':'B','destination_id':'Y'}]
    assert set(policy.hub_pairs((1,0),stops,[2000,8000],outer))=={(0,1),(1,2)}
    assert not policy.hub_pairs((1,0),stops,[2000],outer)
    assert not policy.hub_pairs((1,0),stops,[2000,8000],outer[:1])
    (policy,_),stops=hub_setup(count=0)
    assert set(policy.hub_pairs((1,0),stops,[],outer))=={(0,1),(1,2)}
    policy.index.station_claims['B']={3}
    assert not policy.hub_pairs((1,0),stops,[],outer)


def test_parent_relation_is_not_a_distinct_hub_connection():
    (policy,_),stops=hub_setup()
    policy.index.reference_way_ids={1:[1,2],2:[1,2,3,4]}
    assert not policy.connections(1,stops[1])


def test_nonpassenger_boundaries_with_internal_hub_are_not_isolated():
    (policy,edges),stops=hub_setup()
    # The independent connection is inside C-A-B, at A, not at C or B.
    policy.raw_counter=lambda s:{'count':10,'first_arrival_date':'2020-01-01'}
    _,sections=policy.filter_edges(1,edges)
    assert [(s['origin'],s['destination']) for s in sections]==[('C','B')]


def test_raw_circle_reads_off_rail_gps_date_without_mutating_store(tmp_path):
    from backend.store import TripStore
    from backend.models import TripCreate
    store=TripStore(tmp_path/'points.sqlite3')
    record=store.create(TripCreate(title='附近步行',mode='other',travel_date='2018-01-01',
        points=[{'longitude':120,'latitude':35.004} for _ in range(10)]))
    stops=[station('A',0),station('B',5000)]
    policy,_=setup(stops,[]);policy.raw_counter=None;policy.index.raw_store=store
    proof=policy.terminal(1,stops[0],[])
    assert proof['reached'] and proof['circle_points_within_1km']==10
    assert proof['first_arrival_date']=='2018-01-01'
    assert store.get(record['id'])['point_count']==10
