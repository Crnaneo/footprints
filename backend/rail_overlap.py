"""Resolve lines supported only by a shared start/corridor, using local evidence."""
from collections import defaultdict
import numpy as np
import shapely
from shapely.strtree import STRtree


def shared_only_candidates(index, qualified_lengths=None):
    observations=defaultdict(list)
    for line,xy in index.point_observations:observations[line].append(xy)
    grouped={line:np.concatenate(rows) for line,rows in observations.items()}
    suppressed={}
    line_ways={line:set(index.reference_way_ids.get(line,[])) for line in grouped}
    # OSM may map a shared corridor as separate up/down ways. Compare
    # recorded positions against actual line geometry as well as member IDs.
    for line,xy in grouped.items():
        raw=shapely.points(xy)
        hits=index.tree.query(raw,predicate='dwithin',distance=100)
        support=defaultdict(set)
        for qi,gi in hits.T:
            for other in index.members[index.ids[gi]]:
                if other!=line and other in grouped:support[other].add(int(qi))
        candidates=[other for other,qs in support.items() if len(qs)==len(xy)
            and (qualified_lengths is None or qualified_lengths.get(str(other),0)>5)]
        own=[index.ways[w]['geom'] for w in line_ways[line] if w in index.ways]
        if not own:continue
        own_tree=STRtree(own)
        for other in sorted(candidates,key=lambda l:-len(grouped[l])):
            other_xy=grouped[other]
            other_hits=own_tree.query(shapely.points(other_xy),predicate='dwithin',distance=100)
            exclusive=np.ones(len(other_xy),dtype=bool);exclusive[other_hits[0]]=False
            outside=other_xy[exclusive]
            if len(outside)<2 or np.linalg.norm(np.ptp(outside,axis=0))<5000:continue
            suppressed[line]={'line_id':str(line),'name':index.lines[line]['name'],
                'attributed_to':index.lines[other]['name'],'attributed_to_id':str(other),
                'reason':'仅共用路段有轨迹，另一线路在共用路段外有超过5公里的记录',
                'point_count':len(xy)}
            break
    return list(suppressed.values())
