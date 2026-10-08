"""Density evidence across small holes in a line's public reference geometry.

The connector is a reference-distance estimate, never a recorded GPS segment
and never length evidence. Both ends must belong to the same railway, be
within 5 km, and every adjacent passenger block still needs aligned points.
"""
from collections import defaultdict
import numpy as np
import shapely
from shapely.geometry import LineString,Point
from shapely.ops import substring
from .passenger_sections import interval_evidence,reached_station_bins


def downward_order(axis,reverse,xy):
    p=Point(xy);value=axis.project(p)
    if axis.distance(p)>2000:
        if value<2000:value=-p.distance(Point(axis.coords[0]))
        elif axis.length-value<2000:value=axis.length+p.distance(Point(axis.coords[-1]))
    return axis.length-value if reverse else value


def density_links(index,metadata,stations,line_axes,directions):
    index.density_routes={}
    boundaries=defaultdict(list)
    for line,xy in getattr(index,'boundary_observations',[]):boundaries[line].append(xy)
    grouped=defaultdict(list);observations=defaultdict(list);result=defaultdict(list)
    for key,axis in metadata.items():
        if stations.get(key):grouped[key[0]].append((key,axis))
    for line,xy in getattr(index,'point_observations',[]):observations[line].append(xy)
    for line,axes in grouped.items():
        if len(axes)<2 or not observations[line]:continue
        xy=np.concatenate(observations[line]);points=shapely.points(xy)
        all_stops={s['id']:s for key,_ in axes for s in stations[key]}
        for i,(ka,ga) in enumerate(axes):
            for kb,gb in axes[i+1:]:
                gap,ea,eb=min((Point(ga.coords[a]).distance(Point(gb.coords[b])),a,b) for a in (0,-1) for b in (0,-1))
                if gap>5000:continue
                pa=0 if ea==0 else ga.length;pb=0 if eb==0 else gb.length
                a=min(stations[ka],key=lambda s:abs(s['position']-pa));b=min(stations[kb],key=lambda s:abs(s['position']-pb))
                if a['id']==b['id']:continue
                coords=list(substring(ga,a['position'],pa).coords)+list(substring(gb,pb,b['position']).coords)
                coords=[c for j,c in enumerate(coords) if j==0 or c!=coords[j-1]]
                if len(coords)<2:continue
                route=LineString(coords)
                # Restrict to aligned original observations close to this
                # reference. Sparse points from another line do not count.
                hits=shapely.distance(points,route)<=100
                positions=shapely.line_locate_point(route,points[hits])
                block=[]
                candidates=dict(all_stops)
                # A station in the middle of a catalog hole might have been
                # omitted from both component axes. It must still block a
                # density inference if either adjacent passenger block is empty.
                if hasattr(index,'station_nodes'):
                    distances=shapely.distance(shapely.points(index.station_xy),route)
                    for si in np.flatnonzero(distances<=150):
                        if line not in index.station_claims.get(index.station_nodes[si],set()):continue
                        identity=index.station_ids[si]
                        candidates.setdefault(identity,{'id':identity,'name':index.station_names[si],'xy':tuple(index.station_xy[si]),'kind':index.station_kinds[si]})
                for s in candidates.values():
                    point=Point(s['xy']);p=route.project(point)
                    if s['id'] in {a['id'],b['id']} or (route.distance(point)<=150 and 0<p<route.length):
                        block.append({**s,'position':p})
                block.sort(key=lambda s:s['position'])
                passenger=[s['position'] for s in block if s['kind']=='客运站']
                if not interval_evidence(0,route.length,[],positions,passenger)['reached']:continue
                from .rail_topology import transfer_positions,physically_valid_edge
                junctions=transfer_positions(index,line,route,block)
                boundary_positions=[]
                if boundaries[line]:
                    raw=shapely.points(np.concatenate(boundaries[line]))
                    near=shapely.distance(raw,route)<=100
                    boundary_positions=shapely.line_locate_point(route,raw[near])
                bins=reached_station_bins([],block,positions,junctions,boundary_positions)
                allowed={frozenset((block[i]['id'],block[i+1]['id'])) for lo,hi in bins for i in range(lo,hi)}
                global_axis=line_axes[line][1];reverse=directions[line]['reverse']
                order=lambda s:getattr(index,'station_orders',{}).get((line,s['id']),downward_order(global_axis,reverse,s['xy']))
                if order(a)>order(b):block.reverse()
                for origin,destination in zip(block,block[1:]):
                    if origin['id']==destination['id'] or frozenset((origin['id'],destination['id'])) not in allowed or not physically_valid_edge(origin,destination):continue
                    detail=lambda s:{'id':s['id'],'name':s['name'],'kind':s['kind'],'first_arrival_date':getattr(index,'station_arrivals',{}).get(s['id'])}
                    from .rail_coverage import unproject
                    result[line].append({'origin':origin['name'],'destination':destination['name'],
                     'origin_id':origin['id'],'destination_id':destination['id'],
                     'distance_km':abs(origin['position']-destination['position'])/1000,
                     'start':list(unproject(*origin['xy'])),'end':list(unproject(*destination['xy'])),
                     'order_start':order(origin),'order_end':order(destination),'component':f'density:{ka[1]}:{kb[1]}',
                     'origin_kind':origin['kind'],'destination_kind':destination['kind'],
                     'origin_station':detail(origin),'destination_station':detail(destination),
                     'reference_gap_m':round(gap),'evidence_method':'点密度（跨公共几何缺口，参考里程估算）'})
                    index.density_routes[line,origin['id'],destination['id'],f'density:{ka[1]}:{kb[1]}']=substring(route,origin['position'],destination['position'])
    return result
