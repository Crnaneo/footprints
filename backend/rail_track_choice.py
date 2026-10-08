"""Choose aligned tracks using nearby unambiguous observations as context."""
from collections import Counter, defaultdict
from datetime import datetime
import math
import numpy as np
import shapely
from shapely.geometry import Point


def choose_tracks(index, xy, points, candidates, vectors):
    if not candidates:return {}
    limits={i:min(90,max(15,3*points[i]['_accuracy_m'])) if '_accuracy_m' in points[i] else 90 for i in candidates}
    # A neighbouring track may lie outside the acceptance radius but still
    # explain why a noisy fix fell closer to the wrong parallel railway.
    hits=index.tree.query(shapely.points(xy[candidates]),predicate='dwithin',distance=250)
    options=defaultdict(list)
    for qi,gi in hits.T:
        i=candidates[qi];wid=index.ids[gi];way=index.ways[wid];g=way['geom']
        point=Point(xy[i]);position=g.project(point)
        aligned=True
        if i in vectors:
            a=g.interpolate(max(0,position-15));b=g.interpolate(min(g.length,position+15))
            tangent=np.array([b.x-a.x,b.y-a.y]);vector=vectors[i]
            norm=np.linalg.norm(tangent)*np.linalg.norm(vector)
            aligned=bool(norm and abs(float(tangent@vector))/norm>=math.cos(math.radians(25)))
        lines=set(getattr(index,'members',{}).get(wid,[way['line']]))
        options[i].append((point.distance(g),wid,position,lines,aligned))
    anchors=defaultdict(list);ambiguous=set();matches={}
    times={i:datetime.fromisoformat(points[i]['timestamp'].replace('Z','+00:00')).timestamp()
           for i in options if points[i].get('timestamp')}
    for i,rows in options.items():
        rows.sort(key=lambda row:(row[0],row[1]))
        near=[r for r in rows if r[0]<=limits[i] and r[4]]
        if not near:
            ambiguous.add(i);continue
        _,wid,p,_,_=near[0]
        matches[i]=(index.ways[wid]['line'],wid,p)
        common=set.intersection(*(row[3] for row in near))
        competing=[r for r in rows if not r[3].intersection(rows[0][3])]
        if not common:
            ambiguous.add(i)
        else:
            if i in times:anchors['all'].append((times[i],i,common))
        # Close competing railways still require context, even when only one
        # falls within 90 m. Point existence never depends on segment identity.
        if competing:ambiguous.add(i)
    for segment,rows in anchors.items():rows.sort()
    anchor_times={segment:np.array([r[0] for r in rows]) for segment,rows in anchors.items()}
    for i in sorted(ambiguous):
        if i not in times:continue
        rows=anchors['all']
        ts=anchor_times.get('all',np.array([]))
        lo=np.searchsorted(ts,times[i]-900);hi=np.searchsorted(ts,times[i]+900,side='right')
        possible=set().union(*(r[3] for r in options[i]));votes=Counter()
        for _,j,lines in rows[lo:hi]:
            if np.linalg.norm(xy[i]-xy[j])<=20000:votes.update(lines & possible)
        ranking=votes.most_common()
        if not ranking or ranking[0][1]<4:continue
        top=ranking[0][1];favored={line for line,n in ranking if n==top}
        choices=[r for r in options[i] if r[3]&favored]
        # Override the nearest-track inference only with clear local support.
        # Shared physical members retain their multiple line memberships.
        if not choices or not set.intersection(*(r[3] for r in choices)):continue
        if any(n>top*.5 for line,n in ranking if line not in favored):continue
        _,wid,p,_,_=choices[0];matches[i]=(index.ways[wid]['line'],wid,p)
    return {i:m for i,m in sorted(matches.items()) if index.ways[m[1]]['geom'].distance(Point(xy[i]))<=limits[i]}
