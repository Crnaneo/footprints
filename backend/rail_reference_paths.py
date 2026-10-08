"""Cover reference branches instead of retaining only a component diameter."""
import heapq
from collections import defaultdict


def reference_spines(graph,ways,minimum=1000):
    pending=[graph];result=[]
    def distances(g,start):
        queue=[(0,start)];dist={start:0};previous={}
        while queue:
            d,n=heapq.heappop(queue)
            if d!=dist[n]:continue
            for to,w,length in g[n]:
                nxt=d+length
                if nxt<dist.get(to,float('inf')):
                    dist[to]=nxt;previous[to]=(n,w);heapq.heappush(queue,(nxt,to))
        return max(dist,key=dist.get),dist,previous
    while pending:
        g=pending.pop();seen=set()
        for start in g:
            if start in seen or not g[start]:continue
            far,connected,_=distances(g,start);seen.update(connected)
            end,dist,previous=distances(g,far)
            if dist[end]<minimum:continue
            used=set();parts=[];node=end
            while node!=far:
                prev,wid=previous[node];w=ways[wid];used.add(wid)
                coords=list(w['geom'].coords)
                if w['nodes'][0]!=prev:coords.reverse()
                parts.append(coords);node=prev
            parts.reverse();coords=[p for part in parts for p in part]
            coords=[p for i,p in enumerate(coords) if i==0 or p!=coords[i-1]]
            if len(coords)>1:result.append((dist[end],parts,coords))
            remaining=defaultdict(list)
            for n in connected:
                for edge in g[n]:
                    if edge[1] not in used:remaining[n].append(edge)
            if remaining:pending.append(remaining)
    return result
