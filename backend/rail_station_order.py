"""Align local reference components by shared station identities, not map projection."""
from collections import defaultdict
import numpy as np
from .passenger_sections import downward_direction,station_name
from .rail_density_links import downward_order


def order_reached_stations(index, edges, directions=None):
    """Order a reached component along its connected station graph.

    Local reference axes can be geographically disconnected while a verified
    density bridge connects their station boundaries. Projecting a northern
    fragment onto a southern axis clips all its stations to that axis's end.
    Keep the inferred down direction/component offset, but use actual station
    edge distances inside each reached component.
    """
    import heapq
    for line, rows in edges.items():
        graph=defaultdict(dict);names={}
        for e in rows:
            a,b=e['origin_id'],e['destination_id']
            names[a]=e.get('origin','');names[b]=e.get('destination','')
            if a==b:continue
            d=e['distance_km']*1000
            graph[a][b]=min(graph[a].get(b,float('inf')),d)
            graph[b][a]=min(graph[b].get(a,float('inf')),d)
        seen=set()
        for seed in graph:
            if seed in seen:continue
            nodes=set();todo=[seed]
            while todo:
                node=todo.pop()
                if node in nodes:continue
                nodes.add(node);todo.extend(graph[node].keys()-nodes)
            seen.update(nodes)
            first=station_name((directions or {}).get(line,{}).get('from',''))
            terminals=[n for n in nodes if first and station_name(names[n])==first]
            origin=min(terminals or nodes,key=lambda s:index.station_orders[line,s])
            offset=index.station_orders[line,origin]
            distances={origin:0};queue=[(0,origin)]
            while queue:
                d,node=heapq.heappop(queue)
                if d!=distances[node]:continue
                for nxt,length in graph[node].items():
                    nd=d+length
                    if nd<distances.get(nxt,float('inf')):
                        distances[nxt]=nd;heapq.heappush(queue,(nd,nxt))
            for node,d in distances.items():index.station_orders[line,node]=offset+d
        for e in rows:
            e['order_start']=index.station_orders[line,e['origin_id']]
            e['order_end']=index.station_orders[line,e['destination_id']]


def station_orders(index,metadata,stations,line_axes,directions):
    grouped=defaultdict(list);local={};reverses={}
    for key,axis in metadata.items():
        grouped[key[0]].append(key)
        d=downward_direction(axis,stations.get(key,[]),index.lines[key[0]]['name'],index.lines[key[0]].get('tags',{}),getattr(index,'named_station_xy',{}))
        reverses[key]=d['reverse']
        local[key]={s['id']:axis.length-s['position'] if d['reverse'] else s['position'] for s in stations.get(key,[])}
    orders={}
    for line,keys in grouped.items():
        root,axis=line_axes[line];values=defaultdict(list)
        for identity,p in local[root].items():values[identity].append(p)
        pending=set(keys)-{root}
        while pending:
            available=[k for k in pending if set(local[k])&values.keys()]
            if not available:break
            key=max(available,key=lambda k:len(set(local[k])&values.keys()))
            shared=set(local[key])&values.keys()
            # Relations without their own kilometer tags can have misleading
            # endpoint projections. Shared stations establish their orientation.
            if len(shared)>=2:
                x=np.array([local[key][s] for s in shared]);y=np.array([np.median(values[s]) for s in shared])
                if np.ptp(x)>1000 and np.ptp(y)>1000 and np.corrcoef(x,y)[0,1]<-.9:
                    local[key]={s:metadata[key].length-p for s,p in local[key].items()};reverses[key]=not reverses[key]
            offset=float(np.median([np.median(values[s])-local[key][s] for s in shared]))
            for identity,p in local[key].items():values[identity].append(p+offset)
            pending.remove(key)
        for key in pending:
            ss=stations.get(key,[])
            if not ss:continue
            projected=[downward_order(axis,directions[line]['reverse'],s['xy']) for s in ss]
            offset=float(np.median([p-local[key][s['id']] for p,s in zip(projected,ss)]))
            for identity,p in local[key].items():values[identity].append(p+offset)
        for identity,vs in values.items():orders[line,identity]=float(np.median(vs))
    return orders,reverses
