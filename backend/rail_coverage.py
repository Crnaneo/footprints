"""Local geometry matching against cached OSM railway infrastructure.
Intervals are a union of covered pieces, so distant visits never fill gaps.
"""
from __future__ import annotations
import json, math, sqlite3, threading, time
import re
from collections import defaultdict
from pathlib import Path
import numpy as np
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree
from .store import TripStore
from .passenger_sections import passenger_directory, station_name, station_mileage, station_bins, merge_station_edges, downward_direction, NEAR_STATION_METERS, PASSENGER_SOURCE, PASSENGER_SUPPLEMENT, reached_station_bins

SCALE_X=111320*math.cos(math.radians(35)); SCALE_Y=111320
STATUS={'state':'idle','processed':0,'total':0}
LOCK=threading.Lock()
COVERAGE_VERSION=11

def project(x,y): return (x*SCALE_X,y*SCALE_Y)
def unproject(x,y): return (x/SCALE_X,y/SCALE_Y)
def record_station_arrivals(index, runs, date):
 for xy in runs:
  touched={int(index.passenger_indexes[i]) for hits in index.passenger_tree.query_ball_point(xy,NEAR_STATION_METERS) for i in hits}
  for si in touched:
   identity=index.station_ids[si];previous=index.station_arrivals.get(identity)
   if previous is None or date<previous:index.station_arrivals[identity]=date

def merge_intervals(intervals, tolerance=0.0):
 result=[]
 for lo,hi in sorted((min(a,b),max(a,b)) for a,b in intervals):
  if result and lo<=result[-1][1]+tolerance: result[-1][1]=max(result[-1][1],hi)
  else: result.append([lo,hi])
 return result

class RailIndex:
 def __init__(self,path):
  self.path=Path(path); self.observations=[];self.point_observations=[];self.point_sources=[];self.boundary_observations=[];self.station_arrivals={};self.trip_dates={};self.enforce_boundary_rules=True; self.ways={}; self.lines={}; self.members=defaultdict(list)
  directory=passenger_directory(self.path.parent/'passenger-stations.json')
  with sqlite3.connect(path) as db:
   line_columns={r[1] for r in db.execute('PRAGMA table_info(lines)')}
   line_rows=db.execute('SELECT id,name,tags,ways'+(',stations' if 'stations' in line_columns else '')+' FROM lines')
   for row in line_rows:
    rid,name,tags,ways=row[:4]
    member_stations=json.loads(row[4]) if len(row)>4 else []
    tags=json.loads(tags)
    chinese_name=bool(re.search(r'[\u4e00-\u9fff]',name)) and name.endswith(('线','铁路','高铁'))
    national_ref=chinese_name and (bool(re.fullmatch(r'\d{4,5}',tags.get('ref',''))) or any(station_name(tags.get(k,'')) in directory for k in ('from','to')))
    if not (national_ref or '中国' in tags.get('operator','') or '中国' in tags.get('network','') or tags.get('wikipedia','').startswith('zh:')): continue
    if any(w in name for w in ('地铁','废弃','原)','原）','规划')): continue
    ids=json.loads(ways)
    self.lines[rid]={'id':rid,'name':name,'size':len(ids),'tags':tags,'station_nodes':set(member_stations)}
    for wid in ids: self.members[wid].append(rid)
   self.reference_way_ids={rid:json.loads(ws) for rid,ws in db.execute('SELECT id,ways FROM lines') if rid in self.lines}
   from .rail_topology import transfer_nodes
   self.transfer_nodes=transfer_nodes(db,self.members)
   self.transfer_geometry={}
   # Prefer the specific infrastructure line over a longer umbrella corridor.
   self.owner={w:min(ids,key=lambda r:(self.lines[r]['size'],r)) for w,ids in self.members.items()}
   self.station_claims=defaultdict(set)
   for rid,line in self.lines.items():
    for node in line['station_nodes']:self.station_claims[node].add(rid)
   for wid,nodes,geom,tags in db.execute('SELECT id,nodes,geometry,tags FROM ways'):
    if wid not in self.owner: continue
    nodes=json.loads(nodes); coords=json.loads(geom)
    geometry=LineString([project(*p) for p in coords]).simplify(8)
    if geometry.length<1: continue
    self.ways[wid]={'nodes':(nodes[0],nodes[-1]),'geom':geometry,'length':geometry.length,'line':self.owner[wid]}
   columns={r[1] for r in db.execute('PRAGMA table_info(stations)')}
   rows=list(db.execute('SELECT id,name,lon,lat'+(',tags' if 'tags' in columns else '')+' FROM stations'))
  self.named_station_xy=defaultdict(list)
  stations=[]
  for row in rows:
   name=station_name(row[1]);xy=project(row[2],row[3]);tags=json.loads(row[4]) if len(row)>4 else {}
   self.named_station_xy[name].append(xy)
   urban_only=tags.get('train')!='yes' and (tags.get('station') in {'subway','light_rail','monorail'} or any(tags.get(mode)=='yes' for mode in ('subway','light_rail','monorail','tram')))
   if not urban_only and tags.get('train')!='no' and tags.get('construction:railway') not in {'station','halt'}:
    identity=directory[name] if name in directory and tags.get('passenger')!='no' else f'osm:{row[0]}'
    stations.append((name,xy,tags,identity,row[0]))
  self.station_nodes=[s[4] for s in stations]
  self.station_names=[s[0] for s in stations]
  self.station_ids=[s[3] for s in stations];self.station_tags=[s[2] for s in stations]
  self.station_kinds=['客运站' if not s[3].startswith('osm:') or (s[0] in PASSENGER_SUPPLEMENT or s[2].get('passenger')=='yes') and s[2].get('passenger')!='no' else ('线路所' if s[0].endswith('线路所') or s[2].get('railway') in {'junction','signal_box'} else ('货运站' if s[2].get('passenger')=='no' or s[2].get('freight')=='yes' else '铁路站点')) for s in stations]
  self.station_xy=np.array([s[1] for s in stations]).reshape(-1,2)
  self.station_tree=cKDTree(self.station_xy)
  # OSM can describe a single non-passenger station with duplicate nodes.
  # Resolve only exact-name, same-kind nodes within 50 m in the catalog;
  # similarly named stations elsewhere retain separate identities.
  for a,b in sorted(self.station_tree.query_pairs(50)):
   if self.station_names[a]==self.station_names[b] and self.station_kinds[a]==self.station_kinds[b] and self.station_ids[a].startswith('osm:') and self.station_ids[b].startswith('osm:'):
    old,new=self.station_ids[b],self.station_ids[a]
    self.station_ids=[new if identity==old else identity for identity in self.station_ids]
  from .rail_aliases import canonical_control_station_ids
  self.station_ids=canonical_control_station_ids(self)
  self.passenger_indexes=np.array([i for i,kind in enumerate(self.station_kinds) if kind=='客运站'],dtype=int)
  self.passenger_tree=cKDTree(self.station_xy[self.passenger_indexes])
  self.unresolved_lines=[]
  self.line_lengths=defaultdict(float)
  for w in self.ways.values():self.line_lengths[w['line']]+=w['length']
  self.ids=list(self.ways); self.geometries=[self.ways[w]['geom'] for w in self.ids]; self.tree=STRtree(self.geometries)
  self.graph=defaultdict(lambda:defaultdict(list));self.reference_graph=defaultdict(lambda:defaultdict(list))
  for wid,w in self.ways.items():
   a,b=w['nodes']; g=self.graph[w['line']]
   g[a].append((b,wid,w['length']));g[b].append((a,wid,w['length']))
   for line in self.members[wid]:
    rg=self.reference_graph[line];rg[a].append((b,wid,w['length']));rg[b].append((a,wid,w['length']))
 def endpoint_label(self,xy):
  d,i=self.station_tree.query(xy)
  if d<=1800: return self.station_names[i].removesuffix('站')+('附近' if d>250 else '')
  if d<=25000: return f"近{self.station_names[i].removesuffix('站')}（距站约{d/1000:.1f}km）"
  lon,lat=unproject(*xy)
  return f'{lon:.3f}°E, {lat:.3f}°N'
 def short_path(self,line,w1,w2,limit):
  import heapq
  if w1==w2: return []
  ends=set(self.ways[w2]['nodes']); queue=[(0,n,[]) for n in self.ways[w1]['nodes']];heapq.heapify(queue);seen=set()
  while queue:
   d,n,path=heapq.heappop(queue)
   if n in seen: continue
   seen.add(n)
   if n in ends: return path
   for to,w,l in self.graph[line].get(n,[]):
    if to not in seen and d+l<=limit: heapq.heappush(queue,(d+l,to,path+[w]))
  return None
 def match_trip(self,trip):
  if hasattr(self,'trip_dates') and trip.get('travel_date'):self.trip_dates[trip['id']]=trip['travel_date']
  points=trip['points']; xy=np.array([project(p['longitude'],p['latitude']) for p in points])
  from .rail_flight_context import flight_point_mask
  airborne=flight_point_mask(points)
  candidates=[];vectors={}
  for i,p in enumerate(points):
   speed=p.get('speed_kmh');alt=p.get('altitude_m')
   if airborne[i] or trip['mode']=='flight' or (alt is not None and alt>6000) or p.get('_accuracy_m',0)>90:continue
   local=False;vector=None;distance=None;dt=None
   if i and points[i-1]['segment_id']==p['segment_id']:
    vector=xy[i]-xy[i-1];distance=float(np.linalg.norm(vector))
    if p.get('timestamp') and points[i-1].get('timestamp'):
     from datetime import datetime
     dt=(datetime.fromisoformat(p['timestamp'].replace('Z','+00:00'))-datetime.fromisoformat(points[i-1]['timestamp'].replace('Z','+00:00'))).total_seconds()
    # Some historical exports report zero speed throughout a moving ride.
    # Only recover it from original position/time; never replace the CSV value.
    if speed==0 and dt and dt>0:speed=distance/dt*3.6
    local=distance<=1800 and (dt is None or 0<dt<=180)
   if speed is not None and not 35<=speed<=380:
    continue
   if local:
    if distance<8:continue
    vectors[i]=vector
   candidates.append(i)
  from .rail_track_choice import choose_tracks
  matches=choose_tracks(self,xy,points,candidates,vectors)
  # Point-density evidence is independent of measured-motion evidence. Stops,
  # low-speed fixes and fixes in different recording segments count as points.
  density_candidates=[i for i,p in enumerate(points) if not airborne[i] and trip['mode']!='flight'
      and (p.get('altitude_m') is None or p['altitude_m']<=6000)
      and (p.get('speed_kmh') is None or p['speed_kmh']<=450) and p.get('_accuracy_m',0)<=90]
  density_vectors={}
  for i in density_candidates:
   if not i:continue
   vector=xy[i]-xy[i-1];distance=float(np.linalg.norm(vector))
   if not 8<=distance<=1800:continue
   if points[i].get('timestamp') and points[i-1].get('timestamp'):
    from datetime import datetime
    dt=(datetime.fromisoformat(points[i]['timestamp'].replace('Z','+00:00'))-datetime.fromisoformat(points[i-1]['timestamp'].replace('Z','+00:00'))).total_seconds()
    if not 0<dt<=180:continue
   density_vectors[i]=vector
  # Bearing selects which infrastructure a moving fix belongs to; it does
  # not demand a continuous ride or a minimum speed for the density rule.
  density_matches=choose_tracks(self,xy,points,density_candidates,density_vectors)
  sparse=defaultdict(list)
  for i,(line,wid,_) in density_matches.items():
   for applicable in set(getattr(self,'members',{}).get(wid,[line])):sparse[applicable].append(i)
  if hasattr(self,'point_observations'):
   for line,indices in sparse.items():self.point_observations.append((line,xy[indices]))
  if hasattr(self,'point_sources'):
   for line,indices in sparse.items():self.point_sources.append((line,trip['id'],xy[indices]))
  if hasattr(self,'passenger_tree') and trip.get('travel_date'):
   record_station_arrivals(self,[xy[indices] for indices in sparse.values()],trip['travel_date'])
  # Sustained runs provide measured-length evidence; isolated points never
  # create an artificial connecting length across a recording gap.
  runs=[];current=[]
  for i,m in matches.items():
   if current:
    last=current[-1];a=points[last];b=points[i]
    dt=None
    if a.get('timestamp') and b.get('timestamp'):
     from datetime import datetime
     dt=(datetime.fromisoformat(b['timestamp'].replace('Z','+00:00'))-datetime.fromisoformat(a['timestamp'].replace('Z','+00:00'))).total_seconds()
    if m[0]!=matches[last][0] or a['segment_id']!=b['segment_id'] or np.linalg.norm(xy[i]-xy[last])>1800 or (dt is not None and (dt<0 or dt>180)):
     runs.append(current);current=[]
   current.append(i)
  if current:runs.append(current)
  coverage=defaultdict(list);line_ids=set(sparse) if hasattr(self,'point_observations') else set()
  for run in runs:
   if len(run)<8:continue
   span=sum(np.linalg.norm(xy[b]-xy[a]) for a,b in zip(run,run[1:]))
   line=matches[run[0]][0]
   name=self.lines[line]['name']
   connector=any(word in name for word in ('联络','环到','附属','A3','B3')) or getattr(self,'line_lengths',{}).get(line,float('inf'))<10000
   if span<(800 if connector else 5000):continue
   run_coverage=defaultdict(list)
   for a,b in zip(run,run[1:]):
    _,wa,pa=matches[a];_,wb,pb=matches[b]
    if wa==wb: run_coverage[wa].append((pa,pb));continue
    gap=np.linalg.norm(xy[b]-xy[a]);path=self.short_path(line,wa,wb,min(2200,gap*1.5+250))
    if path is None:continue
    # Connect only short, topologically verified gaps between observations.
    for w in path: run_coverage[w].append((0,self.ways[w]['length']))
    target=self.ways[wb]['geom'].interpolate(pb);start=self.ways[wa]['geom'].interpolate(pa)
    ga=self.ways[wa]['geom'];gb=self.ways[wb]['geom']
    ea=min((0,ga.length),key=lambda x:ga.interpolate(x).distance(target))
    eb=min((0,gb.length),key=lambda x:gb.interpolate(x).distance(start))
    run_coverage[wa].append((pa,ea));run_coverage[wb].append((eb,pb))
   # Repeated oscillations over a station/parallel tracks do not constitute
   # kilometres of sustained travel. Count the union of physical way ranges.
   unique_length=sum(sum(b-a for a,b in merge_intervals(rs)) for rs in run_coverage.values())
   if unique_length<(800 if connector else 5000):continue
   line_ids.add(line)
   if hasattr(self,'observations'):self.observations.append((line,xy[run]))
   for wid,rs in run_coverage.items():coverage[wid].extend(rs)
  return coverage,{str(line):trip['id'] for line in line_ids}
 def reference_catalog(self,active,minimum=1000,reference_ways=None):
  """Public line axes and all eligible station rows; no personal evidence."""
  from .rail_reference_paths import reference_spines
  references=[]; metadata={}
  for line in active:
   graph=getattr(self,'reference_graph',self.graph)[line]
   spines=reference_spines(graph,self.ways if reference_ways is None else reference_ways,minimum=minimum)
   chosen=[]
   for length,parts,coords in sorted(spines,reverse=True,key=lambda p:p[0]):
    duplicate=False
    for old_length,_,old_coords in chosen:
     a,b=np.array(coords[0]),np.array(coords[-1]);c,d=np.array(old_coords[0]),np.array(old_coords[-1])
     endpoint_error=min(max(np.linalg.norm(a-c),np.linalg.norm(b-d)),max(np.linalg.norm(a-d),np.linalg.norm(b-c)))
     if endpoint_error<500 and abs(length-old_length)/old_length<.02:
      duplicate=True;break
    if not duplicate:chosen.append((length,parts,coords))
   for comp,(length,parts,coords) in enumerate(chosen):
    key=(line,comp);axis=LineString(coords)
    # A connected pair of parallel tracks can produce an out-and-back
    # diameter. Trim only the redundant portion that hugs the retained track.
    from .passenger_sections import trim_parallel_return
    trimmed=trim_parallel_return(axis,self.lines[line].get('tags',{}),getattr(self,'named_station_xy',{}))
    if trimmed is not axis:
     axis=trimmed;coords=list(axis.coords);parts=[coords[i:i+2] for i in range(len(coords)-1)]
    metadata[key]=axis;offset=0
    for cs in parts:
     g=LineString(cs)
     references.append((key,offset,g));offset+=g.length
  if not references:return {}, {}, {}, {}
  tree=STRtree([r[2] for r in references])
  per_line=defaultdict(list)
  for i,ref in enumerate(references):per_line[ref[0][0]].append(i)
  line_trees={line:(STRtree([references[i][2] for i in indices]),indices) for line,indices in per_line.items()}
  # A transfer station belongs to every applicable line, not only its
  # nearest line. Keep the closest projection per passenger identity/axis.
  stations=defaultdict(dict)
  station_points=shapely.points(self.station_xy)
  station_hits=tree.query(station_points,predicate='dwithin',distance=2000)
  for si,ri in station_hits.T:
   key,offset,g=references[ri]
   members=self.lines[key[0]].get('station_nodes',set())
   distance=g.distance(station_points[si]);name=station_name(self.station_names[si])
   if hasattr(self,'station_nodes'):
    node=self.station_nodes[si];declared=node in members
    endpoint=name in {station_name(self.lines[key[0]].get('tags',{}).get(k,'')) for k in ('from','to')}
    if not declared and not endpoint and station_mileage(self.station_tags[si],self.lines[key[0]]['name']) is None:
     if distance>150:continue
     claims=getattr(self,'station_claims',{}).get(node,set())
     if claims:
      ways=self.tree.query(station_points[si],predicate='dwithin',distance=150)
      if not any(key[0] in self.members[self.ids[wi]] and bool(claims.intersection(self.members[self.ids[wi]])) for wi in ways):continue
     elif distance>80:continue
   identity=self.station_ids[si] if hasattr(self,'station_ids') else name
   tags=self.station_tags[si] if hasattr(self,'station_tags') else {}
   row={'node':self.station_nodes[si] if hasattr(self,'station_nodes') else None,'id':identity,'name':name,'position':offset+g.project(station_points[si]),
        'xy':tuple(self.station_xy[si]),'distance':distance,
        'mileage':station_mileage(tags,self.lines[key[0]]['name']),
        'kind':self.station_kinds[si] if hasattr(self,'station_kinds') else '客运站'}
   old=stations[key].get(identity)
   if old is None or distance<old['distance']:stations[key][identity]=row
  stations={key:sorted(rows.values(),key=lambda r:r['position']) for key,rows in stations.items()}
  return metadata,stations,line_trees,references

 def reference_sections(self,coverage,visits):
  """Project parallel tracks onto the same topologically derived line axis.
  Connected reference paths include leftover branches after the main axis;
  catalog geometry is kept separate from recorded-coverage evidence.
  """
  active={self.ways[w]['line'] for w in coverage}|{line for line,_ in getattr(self,'point_observations',[])}
  metadata,stations,line_trees,references=self.reference_catalog(active)
  if not references:return []
  pieces=defaultdict(list);point_positions=defaultdict(list);boundary_positions=defaultdict(list)
  for observations,target in [(getattr(self,'point_observations',[]),point_positions),(getattr(self,'boundary_observations',[]),boundary_positions)]:
   for line,xy in observations:
    if line not in line_trees:continue
    own_tree,own_refs=line_trees[line]
    query=shapely.points(xy);hits=own_tree.query(query,predicate='dwithin',distance=100)
    # A point can support either parallel reference, but only once per component.
    located={}
    for qi,ri in hits.T:
     key,offset,g=references[own_refs[ri]];distance=g.distance(query[qi]);identity=(qi,key)
     if identity not in located or distance<located[identity][0]:located[identity]=(distance,offset+g.project(query[qi]))
    for (_,key),(_,position) in located.items():target[key].append(position)
  # Aligned observations already passed the sustained-movement checks. Join
  # adjacent observations only on one reference component and only when the
  # along-line distance agrees with their short geographic displacement.
  for line,xy in getattr(self,'observations',[]):
   query=shapely.points(xy);own_tree,own_refs=line_trees[line];hits=own_tree.query_nearest(query,max_distance=100,all_matches=False);located={}
   for qi,ri in hits.T:
    key,offset,g=references[own_refs[ri]]
    if key[0]==line:located[qi]=(key,offset+g.project(query[qi]))
   for i in range(1,len(xy)):
    if i not in located or i-1 not in located:continue
    a,pa=located[i-1];b,pb=located[i]
    if a==b and abs(pa-pb)<=np.linalg.norm(xy[i]-xy[i-1])*1.5+200:
     pieces[a].append((min(pa,pb),max(pa,pb)))
  for wid,ranges in coverage.items():
   w=self.ways[wid]
   for lo,hi in merge_intervals(ranges,tolerance=30):
    if hi-lo<5:continue
    q=[w['geom'].interpolate(x) for x in (lo,(lo+hi)/2,hi)]
    own_tree,own_refs=line_trees[w['line']]
    matches=own_tree.query_nearest(q,max_distance=100,all_matches=False)
    located=defaultdict(list)
    for qi,ri in matches.T:
     key,offset,g=references[own_refs[ri]]
     if key[0]!=w['line']:continue
     located[key].append(offset+g.project(q[qi]))
    for key,positions in located.items():
     if len(positions)>=2:pieces[key].append((min(positions),max(positions)))
  edges=defaultdict(list);directions={};line_axes={}
  for key,axis in metadata.items():
   if key[0] not in line_axes or axis.length>line_axes[key[0]][1].length:line_axes[key[0]]=(key,axis)
  anchors=getattr(self,'named_station_xy',{})
  for line,(key,axis) in line_axes.items():
   directions[line]=downward_direction(axis,stations.get(key,[]),self.lines[line]['name'],self.lines[line].get('tags',{}),anchors)
  from .rail_station_order import station_orders
  orders,reverses=station_orders(self,metadata,stations,line_axes,directions)
  self.station_orders=orders
  from .rail_boundary_rules import BoundaryEvidence
  policy=BoundaryEvidence(self,metadata,stations) if getattr(self,'enforce_boundary_rules',False) else None
  def append_edge(key,a,b,method=None):
   from .rail_topology import physically_valid_edge
   if not physically_valid_edge(a,b):return
   edge={'origin':a['name'],'destination':b['name'],'origin_id':a['id'],'destination_id':b['id'],
       'distance_km':abs(a['position']-b['position'])/1000,
       'start':list(unproject(*a['xy'])),'end':list(unproject(*b['xy'])),
       'order_start':orders[key[0],a['id']],'order_end':orders[key[0],b['id']],'component':key[1],
       'origin_kind':a['kind'],'destination_kind':b['kind'],
       'origin_station':{'id':a['id'],'name':a['name'],'kind':a['kind'],'first_arrival_date':getattr(self,'station_arrivals',{}).get(a['id'])},
       'destination_station':{'id':b['id'],'name':b['name'],'kind':b['kind'],'first_arrival_date':getattr(self,'station_arrivals',{}).get(b['id'])}}
   if method:edge['evidence_method']=method
   edges[key[0]].append(edge)
   if policy:
    from shapely.ops import substring
    policy.routes[key[0],a['id'],b['id'],key[1]]=substring(metadata[key],a['position'],b['position'])
    if method:policy.hub_edges.append({'line_id':str(key[0]),'origin':a['name'],'destination':b['name'],'reason':method})
  for key in pieces.keys()|point_positions.keys():
   ranges=pieces.get(key,[])
   axis=metadata[key];stops=stations.get(key,[])
   if len(stops)<2:continue
   direction={'reverse':reverses[key]}
   ranges=merge_intervals(ranges,tolerance=0)
   from .rail_topology import transfer_positions
   junctions=transfer_positions(self,key[0],axis,stops)
   for lo,hi in reached_station_bins(ranges,stops,point_positions.get(key,[]),junctions,boundary_positions.get(key,[])):
    block=stops[lo:hi+1]
    if direction['reverse']:block=list(reversed(block))
    for a,b in zip(block,block[1:]):
     append_edge(key,a,b)
  from .rail_density_links import density_links
  for line,links in density_links(self,metadata,stations,line_axes,directions).items():edges[line].extend(links)
  if policy:
   policy.routes.update(getattr(self,'density_routes',{}))
   # Hub rules inspect the baseline outer records, so one inferred edge cannot
   # bootstrap another inference on a different branch or reference.
   baseline={line:list(rows) for line,rows in edges.items()}
   known={(line,frozenset((e['origin_id'],e['destination_id']))) for line,rows in baseline.items() for e in rows}
   additions={}
   for key,stops in stations.items():
    for (a,b),method in policy.hub_pairs(key,stops,point_positions.get(key,[]),baseline.get(key[0],[])).items():
     pair=(key[0],frozenset((stops[a]['id'],stops[b]['id'])))
     if pair in known:continue
     distance=abs(stops[a]['position']-stops[b]['position'])
     previous=additions.get(pair)
     from .rail_topology import physically_valid_edge
     if physically_valid_edge(stops[a],stops[b]) and (previous is None or distance<previous[0]):
      additions[pair]=(distance,key,stops[a],stops[b],method)
   for _,key,a,b,method in additions.values():append_edge(key,a,b,method)
  from .rail_station_order import order_reached_stations
  order_reached_stations(self,edges,directions)
  if getattr(self,'audit_callback',None):self.audit_callback(metadata,stations,point_positions,pieces,line_axes,directions,edges,boundary_positions)
  result=[]
  for line in sorted(active):
   if policy:edges[line],sections=policy.filter_edges(line,edges[line])
   else:sections=merge_station_edges(edges[line])
   if not sections:
    if hasattr(self,'unresolved_lines'):self.unresolved_lines.append({'line_id':str(line),'name':self.lines[line]['name'],'reason':'未形成两个可用铁路站点边界之间的区间（含客运站、货运站、线路所）'})
    continue
   result.append({'line_id':str(line),'name':self.lines[line]['name'],'intervals':sections,
                  'direction':directions[line], 'trip_count':len(visits.get(str(line),set())),
                  'source_url':f'https://www.openstreetmap.org/relation/{line}'})
  self.boundary_rejections=policy.rejections if policy else []
  self.hub_inferences=policy.hub_edges if policy else []
  from .rail_aliases import collapse_line_aliases
  if hasattr(self,'reference_way_ids'):result=collapse_line_aliases(self,result)
  # Suppression requires an actual long, qualifying interval on the other
  # line, not merely widely spaced raw observations near it.
  if hasattr(self,'reference_way_ids'):
   from .rail_overlap import shared_only_candidates
   lengths={r['line_id']:sum(s['distance_km'] for s in r['intervals']) for r in result}
   self.shared_only_lines=shared_only_candidates(self,lengths)
   excluded={p['line_id'] for p in self.shared_only_lines}
   result=[r for r in result if r['line_id'] not in excluded]
   self.unresolved_lines=[r for r in self.unresolved_lines if r['line_id'] not in excluded]
  return sorted(result,key=lambda r:-sum(i['distance_km'] for i in r['intervals']))

 def summarize(self,coverage,visits):
  # Union pieces of each physical way, then join only shared topology nodes.
  edges=defaultdict(list); coords={};serial=0
  for wid,intervals in coverage.items():
   w=self.ways[wid];line=w['line'];geom=w['geom']
   for lo,hi in merge_intervals(intervals,tolerance=30):
    if hi-lo<5:continue
    serial+=1
    a=('node',w['nodes'][0]) if lo<=30 else ('piece',serial,0)
    b=('node',w['nodes'][1]) if hi>=w['length']-30 else ('piece',serial,1)
    coords[(line,a)]=geom.interpolate(lo).coords[0];coords[(line,b)]=geom.interpolate(hi).coords[0]
    edges[line].append((a,b,hi-lo))
  result=[]
  for line,pieces in edges.items():
   adjacency=defaultdict(list)
   for i,(a,b,length) in enumerate(pieces): adjacency[a].append((i,b));adjacency[b].append((i,a))
   seen=set(); intervals=[]
   # Each continuous chain becomes one interval; a fork remains explicit.
   starts=[n for n,neighbors in adjacency.items() if len(neighbors)!=2]+list(adjacency)
   for start in starts:
    for edge,n in adjacency[start]:
     if edge in seen:continue
     seen.add(edge);length=pieces[edge][2];end=n
     while len(adjacency[end])==2:
      nxt=[p for p in adjacency[end] if p[0] not in seen]
      if not nxt:break
      edge,n=nxt[0];seen.add(edge);length+=pieces[edge][2];end=n
     if length<1000:continue
     a=coords[(line,start)];b=coords[(line,end)]
     intervals.append({'origin':self.endpoint_label(a),'destination':self.endpoint_label(b),'distance_km':round(length/1000,1),'start':list(unproject(*a)),'end':list(unproject(*b))})
   if intervals:
    # Eliminate duplicate up/down-track labels without bridging distinct gaps.
    unique={}
    for section in intervals:
     key=tuple(sorted((section['origin'],section['destination'])))
     if key not in unique or section['distance_km']>unique[key]['distance_km']:unique[key]=section
    result.append({'line_id':str(line),'name':self.lines[line]['name'],'intervals':sorted(unique.values(),key=lambda x:-x['distance_km']), 'trip_count':len(visits.get(str(line),set())), 'source_url':f'https://www.openstreetmap.org/relation/{line}'})
  return sorted(result,key=lambda r:-sum(i['distance_km'] for i in r['intervals']))

def paths(store):
 directory=store.path.parent/'rail';return directory/'catalog.sqlite3',directory/'coverage.json'
def fingerprint(store):
 return '|'.join(t['id']+':'+str(t['point_count']) for t in store.list())
def read_coverage(store=None):
 store=store or TripStore();catalog,cache=paths(store)
 if cache.exists():
  result=json.loads(cache.read_text(encoding='utf-8'))
  legacy=result.get('coverage_version')!=COVERAGE_VERSION
  result['stale']=result.get('fingerprint')!=fingerprint(store) or legacy
  if legacy:result['lines']=[];result['unresolved_lines']=[]
  result['progress']=STATUS.copy();result.pop('fingerprint',None)
  from .rail_exclusions import apply_exclusions
  from .rail_manual import apply_manual
  return apply_manual(apply_exclusions(result,catalog.parent),store)
 from .rail_manual import apply_manual
 return apply_manual({'lines':[], 'stale':True,'catalog_available':catalog.exists(),'progress':STATUS.copy()},store)
def rebuild(store=None):
 store=store or TripStore()
 retry=False
 if not LOCK.acquire(blocking=False):return
 try:
  catalog,cache=paths(store)
  if not catalog.exists():raise ValueError('尚未准备本地 OSM 铁路线路目录')
  summaries=store.list();stamp=fingerprint(store)
  STATUS.update(state='running',processed=0,total=len(summaries),error=None)
  index=RailIndex(catalog);index.raw_store=store;index.sandbox_trip_ids={t['id'] for t in summaries if t.get('metadata',{}).get('sandbox')};coverage=defaultdict(list);visits=defaultdict(set)
  for t in summaries:
   if t.get('metadata',{}).get('sandbox'):
    STATUS['processed']+=1;continue
   from .rail_evidence import load_rail_trip
   trip=load_rail_trip(store,t['id'])
   if trip is None:
    STATUS['processed']+=1;continue
   covered,matched=index.match_trip(trip)
   for w,intervals in covered.items():coverage[w].extend(intervals)
   for line,trip_id in matched.items():visits[line].add(trip_id)
   STATUS['processed']+=1
  lines=index.reference_sections(coverage,visits)
  _cache_result(store,index,lines,summaries,stamp,visits)
  retry=fingerprint(store)!=stamp
  STATUS.update(state='complete')
 except Exception as e:
  STATUS.update(state='error',error=str(e));raise
 finally:LOCK.release()
 if retry:rebuild(store)

def _cache_result(store,index,lines,summaries,stamp,visits):
 catalog,cache=paths(store)
 record_dates={t['id']:t['travel_date'] for t in summaries}
 for line in index.unresolved_lines:line['dates']=sorted({record_dates[tid] for tid in visits[line['line_id']] if tid in record_dates})
 for line in getattr(index,'shared_only_lines',[]):line['dates']=sorted({record_dates[tid] for tid in visits[line['line_id']] if tid in record_dates})
 result={'lines':lines,'coverage_version':COVERAGE_VERSION,'shared_only_lines':getattr(index,'shared_only_lines',[]),'unresolved_lines':index.unresolved_lines,'station_nearby_m':NEAR_STATION_METERS,'passenger_source_url':PASSENGER_SOURCE,'fingerprint':stamp,'stale':False,'catalog_available':True,'generated_at':time.strftime('%Y-%m-%d %H:%M:%S'), 'source':'OpenStreetMap / Overpass · ODbL','method':'WGS84 本机匹配。目录递归展开，主参考轴之外的支段继续拆解保留，不丢弃已有线路成员；单轨/地铁等纯城市轨道站不作为国铁边界。纳入具有中文名称且有国内线路编号或已知客运端点、但缺少可选运营标签的线路。距轨上限为 min(90米, max(15米, 3×CSV定位精度))，无精度数据时上限90米；以局部方位和邻线前后15分钟/20公里内明确点证据判定归属；候选轨道检索扩至250米，局部抖动的方向不能先行排除正确邻线；明确支持至少4个点且不少于次高支持的2倍才改判，缺少依据仍为几何推断。混合日记录中，前后5分钟至少3个可信高速（>450km/h且高度缺失或高于300米，民航在此高度以下限速250kt，低空超速尖峰视为记录伪影不作飞行种子）或高空（>6000m）点确定起降飞行背景，该背景的低速点也不确认铁路身份。CSV accuracy超过90米的点不用于确认铁路身份，原始地图与CSV完整保留。静止、低速与不同记录段的已归属点均参与密度，汇总全部日期，不要求连续或同一记录。长度仅判断相邻所有铁路站点，连续覆盖严格超过10%，反复往返同一小段不累计为长途移动；密度严格超过每10公里1点，并逐一检查候选边界内或与边界相交的相邻客运站间有点。货运站、线路所可为边界，近站2公里内端点归站；站前换线节点不能否定已观察到的近站到达；换线裁剪检查当前站间及其所在客运站间的两侧证据，既不外推无记录尾段，也不因内部货运站间断档拆开已支持的客运区间。车站中心偏离轨道的已知距离纳入边界几何误差，不把有效枢纽端点误删。参考片段用共享站点校准方向，已连接站间图按实际边距离排序，按下行合并所有共享边界的连通区间，重复参考边与回路只计一次。只有共用/近乎重合路段有点且另一线路已形成长区间记录的候选不单独统计；可展开查看被归并的候选。同编号且参考轨道与显示车站均被包含的目录别名合并展示；不同名称/编号的父子目录只有物理成员基本包含、显示站点相同且实际点多重集完全相同才去重；同名且共享线路声明、无冲突官方标识的近邻线路所节点采用同一边界身份。公共几何缺口端点不超过5公里时可按密度验证，缺口不计为实测轨迹。客运站首次日期为近站2公里内最早已归属记录日，无近站证据不推测。显示区间不等于逐米记录，也不表示上下车站。历史名称和邻线推断仍需核对。'}
 from .rail_exclusions import apply_exclusions
 result=apply_exclusions(result,catalog.parent)
 result['boundary_rejections']=getattr(index,'boundary_rejections',[])
 result['hub_inferences']=getattr(index,'hub_inferences',[])
 result['method']+=' 合并后递归校验显示两端：站中心1公里内、本区间线路有效点至少2个，或真实半径1公里圆内原始点至少10个，且有到达日期；删除端站后继续检查新边界，剩单站不形成区间。区间内部无其它线路连接、两端非客运且无有记录的其它线路邻站衔接不计区间。枢纽A最近非客运B，在A的1公里内关联铁路有效点>5且B向A本线有点时补判A-B；C-A-B两侧已有X-C及B-Y记录，且两侧向A有本线点，或C/B均非客运且无其它线路连接时可贯通。父子目录不作为独立换线连接。'
 tmp=cache.with_suffix('.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8');tmp.replace(cache)
