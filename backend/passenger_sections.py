"""Passenger-station boundaries, downward ordering and continuity rules."""
from __future__ import annotations

import bisect
import json
import math
import re
from collections import defaultdict
from pathlib import Path

NEAR_STATION_METERS = 2000
PASSENGER_SOURCE = 'https://kyfw.12306.cn/otn/resources/js/framework/station_name.js'
# Passenger stations missing from the national ticketing selector. Verified
# against Shanghai government's 2026-03-13 station operating-hours directory.
# https://www.shanghai.gov.cn/nw17239/20260313/cc9fe6e3e47a47aca3a55b29cd0fb089.html
PASSENGER_SUPPLEMENT = {'春申','新桥','车墩','叶榭','亭林','金山园区','金山卫','莘庄'}


def interval_evidence(start, end, ranges, points, passenger_positions=()):
    """Length requires continuous evidence; density requires no empty passenger block."""
    import numpy as np
    lo,hi=sorted((start,end));length=hi-lo
    if length<=0:return {'reached':False,'coverage_ratio':0,'covered_distance_m':0,'points_per_10km':0,'point_count':0,'continuous':False,'passenger_blocks_complete':False}
    merged=[]
    for a,b in sorted((max(lo,min(a,b)),min(hi,max(a,b))) for a,b in ranges):
        if b<=a:continue
        if merged and a<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],b)
        else:merged.append([a,b])
    covered=sum(b-a for a,b in merged)
    positions=np.asarray(points);count=int(np.sum((positions>=lo)&(positions<=hi)))
    ratio=covered/length;density=count*10000/length
    # Include clipped pieces of passenger blocks containing a freight/junction
    # boundary too. Otherwise A(cargo)--C could bypass an empty A--B tail.
    passenger=sorted(passenger_positions)
    blocks=[(max(lo,a),min(hi,b)) for a,b in zip(passenger,passenger[1:]) if max(lo,a)<min(hi,b)]
    complete=all(np.any((positions>=a)&(positions<=b)) for a,b in blocks)
    continuous=len(merged)==1
    return {'reached':(continuous and ratio>.1) or (density>1 and complete),'coverage_ratio':ratio,
            'covered_distance_m':covered,'point_count':count,'points_per_10km':density,
            'continuous':continuous,'passenger_blocks_complete':bool(complete)}


def reached_station_bins(ranges, stations, points, junctions=(), boundary_points=()):
    """Pool point existence across records; length qualifies adjacent stops only."""
    import numpy as np
    if len(stations)<2:return []
    positions=np.sort(np.asarray(points,dtype=float))
    merged=[]
    for a,b in sorted((min(a,b),max(a,b)) for a,b in ranges):
        if b<=a:continue
        if merged and a<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],b)
        else:merged.append([a,b])
    ranges=merged
    extent=[p for pair in ranges for p in pair]+positions.tolist()
    if not extent:return []
    def count(a,b):return int(np.searchsorted(positions,b,side='right')-np.searchsorted(positions,a))
    passenger=[i for i,s in enumerate(stations) if s.get('kind','客运站')=='客运站']
    passenger_positions=[stations[i]['position'] for i in passenger]
    candidates=set()
    # Density does not use segment IDs, dates, speed, or connected length.
    # Fully empty passenger blocks are genuine breaks, including at cargo ends.
    left,right=min(extent),max(extent)
    for lo,hi in station_bins([(left,right)],stations):
        start=lo
        for a,b in zip(passenger,passenger[1:]):
            if b<=lo or a>=hi:continue
            if not count(max(stations[lo]['position'],stations[a]['position']),min(stations[hi]['position'],stations[b]['position'])):
                end=max(lo,a)
                if start<end:candidates.add((start,end))
                start=min(hi,b)
        if start<hi:candidates.add((start,hi))
    candidates.update((i,i+1) for i in range(len(stations)-1)
        if count(stations[i]['position'],stations[i+1]['position']))
    reached={(a,b) for a,b in candidates if interval_evidence(stations[a]['position'],stations[b]['position'],[],positions,passenger_positions)['reached']}
    # Only consecutive all-station boundaries qualify with continuous length.
    for i in range(len(stations)-1):
        lo,hi=stations[i]['position'],stations[i+1]['position']
        if any(interval_evidence(lo,hi,[r],[])['reached'] for r in ranges):reached.add((i,i+1))
    # Evaluate an exit against the containing passenger block. An interior
    # freight gap may be supported beyond the switch on any date, whereas a
    # record in the next passenger block cannot prove this block's missing tail.
    blocked=set()
    if junctions:
        evidence=sorted(extent+list(boundary_points))
        for i,(a,b) in enumerate(zip(stations,stations[1:])):
            lo,hi=a['position'],b['position']
            local=[p for p in evidence if lo-NEAR_STATION_METERS<=p<=hi+NEAR_STATION_METERS]
            if not local:continue
            near_lo=any(abs(p-lo)<=NEAR_STATION_METERS for p in local)
            near_hi=any(abs(p-hi)<=NEAR_STATION_METERS for p in local)
            for j in junctions:
                if not lo+50<j<hi-50:continue
                before=[p for p in passenger_positions if p<=lo]
                after=[p for p in passenger_positions if p>=hi]
                outer_lo=max(before) if before else lo
                outer_hi=min(after) if after else hi
                if outer_lo==lo and outer_hi==hi:support=local
                else:support=[p for p in evidence if outer_lo<=p<=outer_hi]
                # A switch inside the station's own 2 km arrival catchment
                # does not negate an observed arrival near that terminal.
                if support and max(local)<=j+50 and max(support)<=j+50 and not near_hi:blocked.add(i)
                if support and min(local)>=j-50 and min(support)>=j-50 and not near_lo:blocked.add(i)
    if blocked:
        clipped=set()
        for a,b in reached:
            start=a
            for i in range(a,b):
                if i in blocked:
                    if start<i:clipped.add((start,i))
                    start=i+1
            if start<b:clipped.add((start,b))
        def qualifies(a,b):
            lo,hi=stations[a]['position'],stations[b]['position']
            return (b==a+1 and any(interval_evidence(lo,hi,[r],[])['reached'] for r in ranges)) or interval_evidence(lo,hi,[],positions,passenger_positions)['reached']
        reached={pair for pair in clipped if qualifies(*pair)}
    joined=[]
    for a,b in sorted(reached):
        if joined and a<=joined[-1][1]:joined[-1][1]=max(joined[-1][1],b)
        else:joined.append([a,b])
    result=[]
    for a,b in joined:
        if b-a>1:
            # A dense cluster just outside a reached station does not move a
            # long section's terminal to the next station. Snap actual terminal
            # fixes before formatting, while preserving qualifying length edges.
            evidence=[p for p in extent if stations[a]['position']<=p<=stations[b]['position']]
            if evidence:
                for end,value in [('start',min(evidence)),('end',max(evidence))]:
                    nearest=min(range(a,b+1),key=lambda i:abs(stations[i]['position']-value))
                    if abs(stations[nearest]['position']-value)>NEAR_STATION_METERS:continue
                    lo,hi=(a,a+1) if end=='start' else (b-1,b)
                    if any(interval_evidence(stations[lo]['position'],stations[hi]['position'],[r],[])['reached'] for r in ranges):continue
                    if end=='start' and a<nearest<b:a=nearest
                    if end=='end' and a<nearest<b:b=nearest
        if a<b:result.append((a,b))
    return result


def trim_parallel_return(axis, tags, named_coords):
    """Remove a diameter's redundant return on a parallel track.

    Endpoint projection alone cannot cut a real bend. Every discarded part
    must predominantly sit alongside the retained route, within 150 metres.
    """
    import numpy as np
    import shapely
    from shapely.geometry import Point
    from shapely.ops import substring
    positions = []
    for key in ('from', 'to'):
        choices = named_coords.get(station_name(tags.get(key, '')), [])
        point = min((Point(p) for p in choices), key=axis.distance, default=None)
        if point is None: return axis
        positions.append(axis.project(point))
    lo, hi = sorted(positions)
    if hi-lo < 1000: return axis
    discarded = [(a,b) for a,b in [(0,lo),(hi,axis.length)] if b-a > 1000]
    if not discarded: return axis
    kept = substring(axis,lo,hi)
    cut_start, cut_end = 0, axis.length
    for a,b in discarded:
        samples = shapely.line_interpolate_point(axis,np.linspace(a,b,max(8,int((b-a)/3000))))
        if np.mean(shapely.distance(samples,kept) <= 150) >= .8:
            if a == 0: cut_start = b
            else: cut_end = a
    return substring(axis,cut_start,cut_end) if cut_start or cut_end < axis.length else axis


def station_name(name: str) -> str:
    return re.sub(r'\s+', '', name).removesuffix('站')


def passenger_directory(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding='utf-8'))
    entries = {station_name(s['name']): s['telecode'] for s in data['stations']}
    if len(entries) < 2000:
        raise ValueError('12306 客运站名录不完整，请重新下载铁路目录')
    return entries


def line_name(name: str) -> str:
    name = re.sub(r'\([^)]*\)|（[^）]*）', '', name)
    return name.replace('高速铁路', '高铁').replace('高速线', '高铁').replace('高速', '高铁').removesuffix('铁路').removesuffix('线')


def station_mileage(tags: dict, railway: str) -> float | None:
    values = []
    for key, value in tags.items():
        if not key.startswith('railway:position:exact:'):
            continue
        suffix = key.split('railway:position:exact:', 1)[1].split(':', 1)[0]
        if line_name(suffix) != line_name(railway):
            continue
        text = str(value).strip()
        try:
            if re.fullmatch(r'-?\d+\+\d+', text):
                km, meters = text.split('+'); number = float(km) + float(meters) / 1000
            else:
                number = float(text)
            if math.isfinite(number): values.append(number)
        except ValueError:
            continue
    return sum(values) / len(values) if values else None


def station_bins(ranges, stations, nearby=NEAR_STATION_METERS):
    """Cover touched station-to-station blocks, snapping near ends first.

    Missing whole station blocks are retained as gaps. A gap inside the same
    station block is consolidated intentionally: this is a station-boundary
    summary, not a claim that every meter was observed.
    """
    positions = [s['position'] for s in stations]
    if len(positions) < 2:
        return []

    def snap(position):
        insert = bisect.bisect_left(positions, position)
        candidates = [i for i in (insert-1, insert) if 0 <= i < len(positions)]
        index = min(candidates, key=lambda i: abs(positions[i]-position))
        return index if abs(positions[index]-position) <= nearby else None

    result = []
    for a, b in ranges:
        lo, hi = sorted((a, b))
        # Do not extend an entirely exterior fragment to an unseen station pair.
        if hi <= positions[0] or lo >= positions[-1]:
            continue
        start, end = snap(lo), snap(hi)
        if start is None: start = max(0, bisect.bisect_right(positions, lo)-1)
        if end is None: end = min(len(positions)-1, bisect.bisect_left(positions, hi))
        if start < end:
            result.append((start, end))
    return result


def merge_station_edges(edges):
    """Union station blocks and join shared boundary stations on one line.

    Shared station identities connect branches into one reached component;
    different lines call this function separately.
    """
    unique = {}
    for edge in edges:
        if edge['order_start']>edge['order_end']:
            edge=dict(edge)
            for a,b in [('origin','destination'),('origin_id','destination_id'),('origin_kind','destination_kind'),('origin_station','destination_station'),('start','end'),('order_start','order_end')]:
                if a in edge and b in edge:edge[a],edge[b]=edge[b],edge[a]
        key = (edge['origin_id'], edge['destination_id'])
        if key[0] == key[1]: continue
        if key not in unique or edge['distance_km'] < unique[key]['distance_km']:
            unique[key] = edge
    # Parallel references may skip a station, producing A--C beside A--B--C.
    # Keep the station chain when it supports the same-length physical route.
    adjacency=defaultdict(list)
    for (a,b),edge in unique.items():adjacency[a].append((b,edge['distance_km']))
    redundant=[]
    import heapq
    for pair,edge in unique.items():
        origin,destination=pair;queue=[(0,origin)];seen=set()
        while queue:
            distance,node=heapq.heappop(queue)
            if node==destination:
                if distance<=edge['distance_km']*1.25+.2:redundant.append(pair)
                break
            if node in seen:continue
            seen.add(node)
            for nxt,length in adjacency[node]:
                if (node,nxt)==pair or nxt in seen:continue
                if distance+length<=edge['distance_km']*1.25+.2:heapq.heappush(queue,(distance+length,nxt))
    for pair in redundant:unique.pop(pair)
    # Every covered edge incident on the same station is continuous. A hub
    # branch must not break the trunk into several arbitrary display intervals.
    graph=defaultdict(set)
    for a,b in unique:graph[a].add(b);graph[b].add(a)
    seen=set();result=[]
    for seed in graph:
        if seed in seen:continue
        nodes=set();queue=[seed]
        while queue:
            node=queue.pop()
            if node in nodes:continue
            nodes.add(node);queue.extend(graph[node]-nodes)
        seen.update(nodes)
        component=[e for (a,b),e in unique.items() if a in nodes]
        details={};order={}
        for e in component:
            for prefix in ('origin','destination'):
                identity=e[prefix+'_id'];detail=dict(e.get(prefix+'_station',{'id':identity,'name':e[prefix],'kind':e.get(prefix+'_kind','客运站'),'first_arrival_date':None}))
                dates=[d for d in [detail.get('first_arrival_date'),details.get(identity,{}).get('first_arrival_date')] if d]
                if dates:detail['first_arrival_date']=min(dates)
                details[identity]=detail
                order[identity]=e['order_start' if prefix=='origin' else 'order_end']
        ordered=sorted(nodes,key=lambda n:(order[n],details[n]['name']))
        first_id,last_id=ordered[0],ordered[-1]
        first=next(e for e in component if first_id in (e['origin_id'],e['destination_id']))
        coords={n:e[prefix] for e in component for n,prefix in [(e['origin_id'],'start'),(e['destination_id'],'end')]}
        # Count the covered network once, removing alternate reference loops.
        parents={n:n for n in nodes}
        def root(n):
            while parents[n]!=n:parents[n]=parents[parents[n]];n=parents[n]
            return n
        length=0;gaps=[]
        for e in sorted(component,key=lambda e:e['distance_km']):
            a,b=root(e['origin_id']),root(e['destination_id'])
            if a==b:continue
            parents[a]=b;length+=e['distance_km']
            if e.get('reference_gap_m'):gaps.append({'origin':e['origin'],'destination':e['destination'],'distance_m':e['reference_gap_m']})
        result.append({**first,'origin':details[first_id]['name'],'destination':details[last_id]['name'],
            'origin_id':first_id,'destination_id':last_id,'origin_kind':details[first_id]['kind'],'destination_kind':details[last_id]['kind'],
            'origin_station':details[first_id],'destination_station':details[last_id],
            'start':coords[first_id],'end':coords[last_id],'order_start':order[first_id],'order_end':order[last_id],
            'distance_km':round(length,1),'stations':[details[n]['name'] for n in ordered],
            'station_count':len(ordered),'station_details':[details[n] for n in ordered],'reference_gaps':gaps})
    return sorted(result,key=lambda s:(s['order_start'],s['order_end'],s['origin']))


def downward_direction(axis, stations, railway, tags, named_coords):
    """Prefer line-specific kilometer markers over relation endpoint order."""
    import numpy as np
    from shapely.geometry import Point
    marked = [(s['position'], s['mileage']) for s in stations if s.get('mileage') is not None]
    reverse = None
    if len(marked) >= 2:
        positions, miles = np.array(marked).T
        if np.ptp(positions) > 1000 and np.ptp(miles) > 1:
            correlation = float(np.corrcoef(positions, miles)[0, 1])
            if abs(correlation) >= .9:
                reverse = correlation < 0
    if reverse is not None:
        source = '线路公里标递增'
    else:
        source = '方向未核对'
        endpoints = []
        for key in ('from', 'to'):
            choices = named_coords.get(station_name(tags.get(key, '')), [])
            point = min((Point(p) for p in choices), key=axis.distance, default=None)
            endpoints.append(axis.project(point) if point is not None else None)
        start, end = endpoints
        if start is not None and end is not None and abs(end-start) > 100:
            reverse = end < start; source = '线路起终点（推定）'
        elif start is not None and min(start, axis.length-start) < 2000:
            reverse = start > axis.length / 2; source = '线路起点（推定）'
        elif end is not None and min(end, axis.length-end) < 2000:
            reverse = end < axis.length / 2; source = '线路终点（推定）'
    known = reverse is not None
    reverse = bool(reverse)
    ordered = sorted(stations, key=lambda s: s['position'], reverse=reverse)
    origin, destination = tags.get('from'), tags.get('to')
    if source == '线路公里标递增' and origin and destination:
        projected = []
        for name in (origin, destination):
            candidates = named_coords.get(station_name(name), [])
            point = min((Point(p) for p in candidates), key=axis.distance, default=None)
            projected.append(axis.project(point) if point is not None else None)
        if all(p is not None for p in projected) and (projected[1] < projected[0]) != reverse:
            origin = destination = None
    return {'reverse': reverse, 'known': known, 'source': source,
            'from': origin or (ordered[0]['name'] if ordered else ''),
            'to': destination or (ordered[-1]['name'] if ordered else '')}
