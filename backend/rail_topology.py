"""Public transfer positions and safeguards against projecting a ride past an exit."""
from collections import defaultdict
import json
import numpy as np
import shapely
from .rail_coverage import project


def transfer_nodes(db,members):
    """Include forks inside unsplit OSM ways, not just way endpoints."""
    rows=[(wid,json.loads(ns),json.loads(cs)) for wid,ns,cs in db.execute('SELECT id,nodes,geometry FROM ways')]
    candidates={n for _,ns,_ in rows if len(ns)>1 for n in (ns[0],ns[-1])}
    links=defaultdict(set);neighbors=defaultdict(set);xy={}
    for wid,ns,cs in rows:
        for i,n in enumerate(ns):
            if n not in candidates:continue
            links[n].add(wid)
            if i:neighbors[n].add(ns[i-1])
            if i+1<len(ns):neighbors[n].add(ns[i+1])
            if i<len(cs):xy[n]=project(*cs[i])
    result=defaultdict(list)
    for n,ways in links.items():
        if len(neighbors[n])<3 or n not in xy:continue
        for line in set().union(*(members.get(w,[]) for w in ways)):
            # An unassigned yard/siding is not evidence of changing railway.
            if any(members.get(w) and line not in members[w] for w in ways):result[line].append(xy[n])
    return dict(result)


def transfer_positions(index,line,axis,stops):
    """Topology first; also handle separate station tracks sharing a corridor.

    Different route relations sometimes use entirely separate platform tracks,
    so no node IDs are shared. A declared shared station plus a corridor that
    actually diverges supplies a geometric exit estimate, not a new station.
    """
    positions=[]
    nodes=getattr(index,'transfer_nodes',{}).get(line,[])
    if nodes:
        pts=shapely.points(nodes);near=shapely.distance(pts,axis)<=100
        positions.extend(shapely.line_locate_point(axis,pts[near]).tolist())
    related=defaultdict(list)
    for s in stops:
        for other in getattr(index,'station_claims',{}).get(s.get('node'),[]):
            if other!=line:related[other].append(s['position'])
    geometries=getattr(index,'transfer_geometry',{})
    for other,anchors in related.items():
        if set(index.reference_way_ids.get(line,[])).intersection(index.reference_way_ids.get(other,[])):continue
        cache_key=(line,other,tuple(round(a) for a in anchors))
        if cache_key not in geometries:
            if hasattr(index,'tree'):
                pts=shapely.points([axis.interpolate(a).coords[0] for a in anchors])
                hits=index.tree.query(pts,predicate='dwithin',distance=20000)
                ids={index.ids[i] for i in hits[1] if other in index.members[index.ids[i]]}
            else:ids=set(index.reference_way_ids.get(other,[]))
            gs=[index.ways[w]['geom'] for w in ids if w in index.ways]
            if not gs:continue
            from shapely.strtree import STRtree
            geometries[cache_key]=(gs,STRtree(gs))
        gs,tree=geometries[cache_key]
        def close(position):return bool(len(tree.query(axis.interpolate(position),predicate='dwithin',distance=100)))
        # Sample only shared-station neighborhoods. Refine the overlap boundary
        # by bisection; avoid buffering/unioning entire national railway routes.
        ranges=[]
        for a in sorted(anchors):
            lo,hi=max(0,a-20000),min(axis.length,a+20000)
            if ranges and lo<=ranges[-1][1]:ranges[-1][1]=max(hi,ranges[-1][1])
            else:ranges.append([lo,hi])
        for lo,hi in ranges:
            samples=np.linspace(lo,hi,max(2,int((hi-lo)/100)+1))
            pts=shapely.line_interpolate_point(axis,samples)
            hits=tree.query(pts,predicate='dwithin',distance=100)
            mask=np.zeros(len(samples),dtype=bool);mask[hits[0]]=True
            for i in range(1,len(samples)):
                if mask[i]==mask[i-1]:continue
                left,right=samples[i-1],samples[i];side=mask[i-1]
                while right-left>1:
                    mid=(left+right)/2
                    if close(mid)==side:left=mid
                    else:right=mid
                p=(left+right)/2;endpoint=axis.interpolate(p)
                near=tree.query(endpoint,predicate='dwithin',distance=2000)
                candidates=[g.interpolate(x) for gi in near for g in [gs[gi]] for x in np.linspace(0,g.length,max(2,int(g.length/100)+1))]
                nearby=[point for point in candidates if point.distance(endpoint)<=2000]
                if nearby and max(shapely.distance(np.asarray(nearby,dtype=object),axis))>200:positions.append(p)
    index.transfer_geometry=geometries
    return sorted(set(round(p,3) for p in positions))


def physically_valid_edge(a,b):
    """A clipped reference axis cannot shorten a station pair below its chord."""
    chord=np.linalg.norm(np.asarray(a['xy'])-np.asarray(b['xy']))
    # Station nodes mark buildings/centres, not their projected track points.
    # Their known lateral offsets must be included in the error budget.
    return abs(a['position']-b['position'])+a.get('distance',0)+b.get('distance',0)+200>=chord
