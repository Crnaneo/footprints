"""Recorded terminal support, connected freight sections and hub gap rules.

These rules operate on candidate station edges before publishing merged sections.
Original GPS and public geometry are read-only; circle support never becomes a
measured railway segment or a new GPS point.
"""
from collections import Counter, defaultdict
import math
import numpy as np
import shapely
from scipy.spatial import cKDTree
from .passenger_sections import merge_station_edges

EARTH_RADIUS=6371008.8


def geographic_distances(xy, centre):
    from .rail_coverage import SCALE_X,SCALE_Y
    points=np.asarray(xy,dtype=float).reshape(-1,2)
    lon,lat=np.radians(points/np.array([SCALE_X,SCALE_Y])).T
    lon0,lat0=np.radians(np.asarray(centre)/[SCALE_X,SCALE_Y])
    h=np.sin((lat-lat0)/2)**2+np.cos(lat)*np.cos(lat0)*np.sin((lon-lon0)/2)**2
    return 2*EARTH_RADIUS*np.arcsin(np.sqrt(np.clip(h,0,1)))


class BoundaryEvidence:
    def __init__(self,index,metadata,stations,raw_counter=None):
        self.index=index;self.metadata=metadata;self.stations=stations
        self.raw_counter=raw_counter;self.raw_cache={};self.hub_cache={}
        self.rows={};self.station_lines=defaultdict(set);self.by_line=defaultdict(list)
        self.catalog_rows={identity:{'id':identity,'name':index.station_names[i],'kind':index.station_kinds[i],
            'xy':tuple(index.station_xy[i]),'node':index.station_nodes[i]}
            for i,identity in enumerate(getattr(index,'station_ids',[]))}
        for key,stops in stations.items():
            self.by_line[key[0]].append((key,stops))
            for s in stops:
                self.station_lines[s['id']].add(key[0])
                old=self.rows.get((key[0],s['id']))
                if old is None or s.get('distance',0)<old.get('distance',0):self.rows[key[0],s['id']]=s
        grouped=defaultdict(list);dates=getattr(index,'trip_dates',{})
        for line,tid,xy in getattr(index,'point_sources',[]):
            grouped[line].append((tid,xy,dates.get(tid)))
        self.sources={}
        for line,runs in grouped.items():
            xy=np.concatenate([r[1] for r in runs])
            self.sources[line]=(xy,cKDTree(xy),[tid for tid,ps,_ in runs for _ in ps],
                [date for _,ps,date in runs for _ in ps])
        self.routes={};self.rejections=[];self.hub_edges=[]

    def station_row(self,line,identity,edge,prefix):
        row=self.rows.get((line,identity))
        if row is None:
            # Density links can introduce a declared stop inside a catalog hole.
            from .rail_coverage import project
            row={'id':identity,'name':edge[prefix],'kind':edge[prefix+'_kind'],
                'xy':project(*edge['start' if prefix=='origin' else 'end'])}
            row.update(self.catalog_rows.get(identity,{}))
            self.rows[line,identity]=row
        return row

    def _near(self,line,station):
        source=self.sources.get(line)
        if source is None:return np.array([],dtype=int)
        from .rail_coverage import SCALE_Y
        lat=station['xy'][1]/SCALE_Y
        radius=1000*max(1,math.cos(math.radians(35))/max(.01,math.cos(math.radians(lat))))*1.02
        found=np.asarray(source[1].query_ball_point(station['xy'],radius),dtype=int)
        if not len(found):return found
        return found[geographic_distances(source[0][found],station['xy'])<=1000]

    def raw_near(self,station):
        identity=station['id']
        if identity in self.raw_cache:return self.raw_cache[identity]
        if self.raw_counter:
            result=self.raw_counter(station)
        elif getattr(self.index,'raw_store',None) is not None:
            from .rail_coverage import unproject,project
            from .map_points import world_coordinate
            store=self.index.raw_store;lon,lat=unproject(*station['xy'])
            dy=math.degrees(1000/EARTH_RADIUS)
            dx=dy/max(.01,math.cos(math.radians(lat)))
            x0,y1=world_coordinate(lon-dx,lat-dy);x1,y0=world_coordinate(lon+dx,lat+dy)
            with store.connect() as db:
                rows=db.execute('SELECT p.longitude,p.latitude,t.travel_date,t.id FROM map_point_index i JOIN map_points p ON p.id=i.id JOIN trips t ON t.id=p.trip_id WHERE i.minx<=? AND i.maxx>=? AND i.miny<=? AND i.maxy>=?',(x1,x0,y1,y0)).fetchall()
            excluded=getattr(self.index,'sandbox_trip_ids',set())
            rows=[r for r in rows if r['id'] not in excluded]
            hits=geographic_distances([project(r['longitude'],r['latitude']) for r in rows],station['xy'])<=1000
            dates=[r['travel_date'] for r,hit in zip(rows,hits) if hit]
            result={'count':len(dates),'first_arrival_date':min(dates) if dates else None}
        else:result={'count':0,'first_arrival_date':None}
        self.raw_cache[identity]=result
        return result

    def track_near(self,line,station,route=None):
        found=self._near(line,station);source=self.sources.get(line)
        if route is not None and len(found):
            found=found[shapely.distance(shapely.points(source[0][found]),route)<=100]
        dates=[source[3][i] for i in found if source[3][i]] if source else []
        return {'count':len(found),'first_arrival_date':min(dates) if dates else None}

    def terminal(self,line,station,routes):
        # Count a source fix once even when parallel axes describe the same end.
        route=shapely.GeometryCollection(routes) if routes else None
        track=self.track_near(line,station,route)
        raw=self.raw_near(station) if track['count']<2 or not track['first_arrival_date'] else {'count':0,'first_arrival_date':None}
        dates=[r['first_arrival_date'] for r in (track,raw) if r['first_arrival_date']]
        date=min(dates) if dates else None
        return {'reached':bool(date and (track['count']>=2 or raw['count']>=10)),
            'rail_points_within_1km':track['count'],'circle_points_within_1km':raw['count'],
            'first_arrival_date':date,'circle_checked':bool(raw['count'] or track['count']<2 or not track['first_arrival_date'])}

    def connections(self,line,station):
        other=set(self.station_lines[station['id']])
        other.update(getattr(self.index,'station_claims',{}).get(station.get('node'),set()))
        other.discard(line)
        members=getattr(self.index,'reference_way_ids',{})
        own=set(members.get(line,[]))
        # Umbrella relations containing the same tracks are not a physical exit.
        return {n for n in other if not own or not members.get(n) or
            (own-set(members[n]) and set(members[n])-own)}

    def hub_rail_count(self,station,line):
        key=(line,station['id'])
        if key not in self.hub_cache:
            # A shared recorded fix has multiple memberships, not multiple votes.
            union=Counter()
            for other in self.connections(line,station)|{line}:
                source=self.sources.get(other)
                if source is None:continue
                rows=self._near(other,station)
                votes=Counter((source[2][i],tuple(source[0][i])) for i in rows)
                union|=votes
            self.hub_cache[key]=sum(union.values())
        return self.hub_cache[key]

    def external_support(self,line,station):
        """Require an actual point between this boundary and an other-line stop."""
        for other in self.connections(line,station):
            source=self.sources.get(other)
            if source is None:continue
            for key,stops in self.by_line.get(other,[]):
                for i,s in enumerate(stops):
                    if s['id']!=station['id']:continue
                    for j in (i-1,i+1):
                        if not 0<=j<len(stops):continue
                        from shapely.ops import substring
                        route=substring(self.metadata[key],s['position'],stops[j]['position'])
                        if route.length<=1:continue
                        hits=shapely.distance(shapely.points(source[0]),route)<=100
                        positions=shapely.line_locate_point(route,shapely.points(source[0][hits]))
                        if np.any((positions>0)&(positions<route.length)):return True
        return False

    def hub_pairs(self,key,stops,points,edges):
        """Additional rules only; existence can come from any recording day."""
        line=key[0];positions=np.asarray(points);pairs={}
        incident=defaultdict(set)
        for e in edges:
            incident[e['origin_id']].add(e['destination_id']);incident[e['destination_id']].add(e['origin_id'])
        for i,a in enumerate(stops):
            if not self.connections(line,a):continue
            if self.hub_rail_count(a,line)>5:
                for j in (i-1,i+1):
                    if not 0<=j<len(stops) or stops[j]['kind']=='客运站':continue
                    lo,hi=sorted((a['position'],stops[j]['position']))
                    if np.any((positions>lo)&(positions<hi)):
                        pairs[tuple(sorted((i,j))) ]='枢纽短段：近站有效铁路点>5且B向A有点'
            if not 0<i<len(stops)-1:continue
            c,b=stops[i-1],stops[i+1]
            left=bool(incident[c['id']]-{a['id'],b['id']})
            right=bool(incident[b['id']]-{a['id'],c['id']})
            if not left or not right:continue
            ca=np.any((positions>c['position'])&(positions<a['position']))
            ba=np.any((positions>a['position'])&(positions<b['position']))
            unbranched=c['kind']!='客运站' and b['kind']!='客运站' and not self.connections(line,c) and not self.connections(line,b)
            if ca and ba or unbranched:
                reason='枢纽贯通：外侧有记录且两侧向A有点' if ca and ba else '枢纽贯通：外侧有记录且C/B非客运无其它连接'
                pairs[i-1,i]=reason;pairs[i,i+1]=reason
        return pairs

    def filter_edges(self,line,edges):
        """Trim unsupported displayed ends, remerge, then reject isolated cargo."""
        remaining=list(edges)
        while remaining:
            sections=merge_station_edges(remaining);remove=set();supported={}
            for section in sections:
                for prefix in ('origin','destination'):
                    identity=section[prefix+'_id'];station=self.station_row(line,identity,section,prefix)
                    routes=[self.routes.get((line,e['origin_id'],e['destination_id'],e['component'])) for e in remaining
                        if identity in (e['origin_id'],e['destination_id'])]
                    proof=self.terminal(line,station,[r for r in routes if r is not None])
                    supported[identity]=proof
                    if not proof['reached']:
                        remove.add(identity)
                        self.rejections.append({'line_id':str(line),'station':station['name'],'reason':'合并边界缺少1公里点证据或到达日期',**proof})
            if remove:
                remaining=[e for e in remaining if not remove.intersection((e['origin_id'],e['destination_id']))]
                continue
            rejected=set()
            for section in sections:
                a=self.rows[line,section['origin_id']];b=self.rows[line,section['destination_id']]
                internal=[self.rows.get((line,s['id']),self.catalog_rows.get(s['id'])) for s in section['station_details'][1:-1]]
                connected_inside=any(self.connections(line,s) for s in internal if s is not None)
                if a['kind']!='客运站' and b['kind']!='客运站' and not connected_inside and not self.external_support(line,a) and not self.external_support(line,b):
                    rejected.update(s['id'] for s in section['station_details'])
                    self.rejections.append({'line_id':str(line),'origin':a['name'],'destination':b['name'],'reason':'两端非客运且无有记录的其它线路衔接'})
            if rejected:
                remaining=[e for e in remaining if not rejected.intersection((e['origin_id'],e['destination_id']))]
                continue
            for section in sections:
                section['terminal_evidence']={}
                for prefix in ('origin','destination'):
                    identity=section[prefix+'_id'];proof=supported[identity]
                    section['terminal_evidence'][prefix]=proof
                    section[prefix+'_station']['first_arrival_date']=proof['first_arrival_date']
                    for detail in section['station_details']:
                        if detail['id']==identity:detail['first_arrival_date']=proof['first_arrival_date']
            return remaining,sections
        return [],[]
