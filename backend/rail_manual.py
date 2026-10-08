"""Persistent, reversible railway-statistic edits, separate from original GPS."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import heapq
import json
import math
import sqlite3
import threading
from uuid import uuid4

from .store import TripStore

INDEX_LOCK = threading.Lock()


class EditConflict(ValueError):
    pass


def section_id(line_id, section):
    identities = [section.get(k+'_id') or section.get(k+'_station', {}).get('id') or section[k]
                  for k in ('origin', 'destination')]
    return hashlib.sha256(json.dumps([str(line_id), *identities], ensure_ascii=False).encode()).hexdigest()[:24]


class ManualStore:
    def __init__(self, store):
        self.path = store.path.parent/'rail'/'manual.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS edits(line_id TEXT PRIMARY KEY, payload TEXT, revision TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS undo(token TEXT PRIMARY KEY, line_id TEXT, before_payload TEXT, before_revision TEXT, after_revision TEXT)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def rows(self):
        with self.connect() as db:
            return {row[0]: (json.loads(row[1]), row[2]) for row in db.execute('SELECT * FROM edits')}

    def change(self, line_id, payload, expected_revision):
        token = uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT payload,revision FROM edits WHERE line_id=?', (line_id,)).fetchone()
            current = previous[1] if previous else 'auto'
            if current != expected_revision:
                raise EditConflict('这条线路已被修改，请刷新后再保存。')
            db.execute('INSERT INTO undo VALUES(?,?,?,?,?)',
                       (token, line_id, previous[0] if previous else None, current, token))
            db.execute('INSERT OR REPLACE INTO edits VALUES(?,?,?)',
                       (line_id, json.dumps(payload, ensure_ascii=False), token))
        return token

    def undo(self, token):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT line_id,before_payload,before_revision,after_revision FROM undo WHERE token=?', (token,)).fetchone()
            if row is None:
                raise EditConflict('该操作已撤销或不存在。')
            current = db.execute('SELECT revision FROM edits WHERE line_id=?', (row[0],)).fetchone()
            if current is None or current[0] != row[3]:
                raise EditConflict('该线路之后已有其他修改，不能撤销旧操作。')
            if row[1] is None:
                db.execute('DELETE FROM edits WHERE line_id=?', (row[0],))
            else:
                db.execute('UPDATE edits SET payload=?,revision=? WHERE line_id=?', (row[1], row[2], row[0]))
            db.execute('DELETE FROM undo WHERE token=?', (token,))


def apply_manual(result, store):
    result = deepcopy(result)
    edits = ManualStore(store).rows()
    lines = {str(line['line_id']): line for line in result.get('lines', [])}
    hidden = []
    for identity, (payload, revision) in edits.items():
        if payload.get('automatic'):
            continue
        lines.pop(identity, None)
        if payload.get('hidden'):
            hidden.append({'line_id': identity, 'name': payload['name'], 'manual_revision': revision})
        else:
            lines[identity] = {**payload, 'manual': True, 'manual_revision': revision}
    touched = {identity for identity, (p, _) in edits.items() if not p.get('automatic')}
    for field in ('unresolved_lines', 'shared_only_lines', 'manual_exclusions'):
        result[field] = [row for row in result.get(field, []) if str(row['line_id']) not in touched]
    for identity, line in lines.items():
        line.setdefault('manual_revision', edits.get(identity, ({}, 'auto'))[1])
        for section in line['intervals']:
            section['interval_id'] = section_id(identity, section)
    result['lines'] = sorted(lines.values(), key=lambda line: -sum(s['distance_km'] for s in line['intervals']))
    result['hidden_lines'] = hidden
    return result


@lru_cache(maxsize=2)
def _index(path, mtime, size):
    from .rail_coverage import RailIndex
    return RailIndex(path)


def editor_index(store):
    path = store.path.parent/'rail'/'catalog.sqlite3'
    if not path.exists():
        raise FileNotFoundError('本地铁路目录尚未准备。')
    stat = path.stat()
    with INDEX_LOCK:
        return _index(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


def search_lines(store, query):
    # A catalog search does not load national geometries or scan personal GPS.
    from .passenger_sections import station_name
    path = store.path.parent/'rail'/'catalog.sqlite3'
    if not path.exists():
        raise FileNotFoundError('本地铁路目录尚未准备。')
    from .passenger_sections import passenger_directory
    directory = passenger_directory(path.parent/'passenger-stations.json')
    import re
    found = []
    with sqlite3.connect(path) as db:
        for identity, name, tags in db.execute('SELECT id,name,tags FROM lines'):
            tags = json.loads(tags)
            chinese = bool(re.search(r'[\u4e00-\u9fff]', name)) and name.endswith(('线','铁路','高铁'))
            national = chinese and (bool(re.fullmatch(r'\d{4,5}', tags.get('ref',''))) or any(station_name(tags.get(k,'')) in directory for k in ('from','to')))
            eligible = national or '中国' in tags.get('operator','') or '中国' in tags.get('network','') or tags.get('wikipedia','').startswith('zh:')
            if not eligible or any(word in name for word in ('地铁','废弃','原)','原）','规划')):
                continue
            if query.casefold() not in (name+' '+tags.get('ref','')).casefold():
                continue
            found.append({'line_id': str(identity), 'name': name, 'ref': tags.get('ref','')})
    found.sort(key=lambda row: (row['name'] != query, len(row['name']), row['name'], row['line_id']))
    return {'lines': found[:100], 'total': len(found)}


class LineCatalog:
    """The same public reference axes as inference, including unvisited branches."""
    def __init__(self, index, line_id):
        from .rail_coverage import unproject
        from .passenger_sections import downward_direction, station_mileage
        from .rail_station_order import station_orders
        from shapely.geometry import Point
        self.line_id = line_id
        self.line = index.lines[line_id]
        metadata, stations, _, _ = index.reference_catalog({line_id}, minimum=0)
        if not metadata:
            raise ValueError('该线路缺少可用的本地几何资料，无法生成站点区间。')
        # Declared member stations inside geometry holes must remain selectable.
        known = {s['id'] for stops in stations.values() for s in stops}
        for i, node in enumerate(index.station_nodes):
            if node not in self.line['station_nodes'] or index.station_ids[i] in known:
                continue
            point = Point(index.station_xy[i]); key = min(metadata, key=lambda k: metadata[k].distance(point))
            axis = metadata[key]
            stations.setdefault(key, []).append({'id': index.station_ids[i], 'name': index.station_names[i],
                'kind': index.station_kinds[i], 'node': node, 'xy': tuple(index.station_xy[i]),
                'position': axis.project(point), 'distance': axis.distance(point),
                'mileage': station_mileage(index.station_tags[i], self.line['name']), 'catalog_gap': True})
            known.add(index.station_ids[i])
        for stops in stations.values():
            stops.sort(key=lambda row: row['position'])
        root = max(metadata, key=lambda k: metadata[k].length)
        self.direction = downward_direction(metadata[root], stations.get(root, []), self.line['name'], self.line['tags'], index.named_station_xy)
        orders, _ = station_orders(index, metadata, stations, {line_id:(root, metadata[root])}, {line_id:self.direction})
        self.rows = {}; self.graph = {}; self.edges = {}
        for stops in stations.values():
            for station in stops:
                identity = station['id']; row = {**station, 'order': orders[line_id, identity], 'coordinates': list(unproject(*station['xy']))}
                if identity not in self.rows or row['distance'] < self.rows[identity]['distance']:
                    self.rows[identity] = row
            for a, b in zip(stops, stops[1:]):
                if a['id'] == b['id']:
                    continue
                chord = float(math.dist(a['xy'], b['xy']))
                length = abs(a['position']-b['position'])
                gap = a.get('catalog_gap') or b.get('catalog_gap') or length+200+a['distance']+b['distance'] < chord
                self.add_edge(a['id'], b['id'], max(length, chord) if gap else length, bool(gap))
        # Connect separate catalog components only as explicitly labelled estimates.
        # These edges are editor reference gaps, never observed GPS evidence.
        groups = []
        unseen = set(self.rows)
        while unseen:
            todo = [next(iter(unseen))]; group = set()
            while todo:
                node = todo.pop()
                if node in group:
                    continue
                group.add(node); todo.extend(self.graph.get(node, {}))
            unseen -= group; groups.append(group)
        while len(groups) > 1:
            best = min((math.dist(self.rows[a]['xy'], self.rows[b]['xy']), i, j, a, b)
                       for i in range(len(groups)) for j in range(i+1, len(groups)) for a in groups[i] for b in groups[j])
            distance, i, j, a, b = best
            self.add_edge(a, b, distance, True)
            groups[i] |= groups[j]; groups.pop(j)

    def add_edge(self, a, b, length, gap):
        length = max(1., length)
        key = tuple(sorted((a,b)))
        if key in self.edges and self.edges[key]['length'] <= length:
            return
        self.edges[key] = {'length': length, 'gap': gap}
        self.graph.setdefault(a, {})[b] = length
        self.graph.setdefault(b, {})[a] = length

    def path(self, a, b):
        if a == b:
            raise ValueError('起始和终止站点必须不同。')
        if a not in self.rows or b not in self.rows:
            raise ValueError('请从该线路的匹配项中选择两个站点或线路所。')
        if self.rows[a]['order'] > self.rows[b]['order']:
            a,b = b,a
        queue = [(0., a)]; distances = {a:0.}; previous = {}
        while queue:
            distance,node = heapq.heappop(queue)
            if distance != distances[node]:
                continue
            if node == b:
                chain = [b]
                while chain[-1] != a:
                    chain.append(previous[chain[-1]])
                return list(reversed(chain))
            for nxt,length in self.graph.get(node, {}).items():
                value = distance+length
                if value < distances.get(nxt, float('inf')):
                    distances[nxt] = value; previous[nxt] = node; heapq.heappush(queue,(value,nxt))
        raise ValueError('这两个站点之间缺少可用的线路资料。')

    def public(self):
        return {'line_id': str(self.line_id), 'name': self.line['name'], 'direction': self.direction,
                'stations': [{'id': s['id'], 'name': s['name'], 'kind': s['kind'], 'order': s['order']}
                             for s in sorted(self.rows.values(), key=lambda s: (s['order'], s['name']))]}


@lru_cache(maxsize=64)
def _line_catalog(path, mtime, size, line_id):
    return LineCatalog(_index(path, mtime, size), line_id)


def line_catalog(store, line_id):
    index = editor_index(store)
    try:
        identity = int(line_id)
    except ValueError:
        raise ValueError('铁路线编号无效。') from None
    if identity not in index.lines:
        raise ValueError('请选择本地铁路目录中的匹配线路。')
    stat = index.path.stat()
    with INDEX_LOCK:
        return _line_catalog(str(index.path.resolve()), stat.st_mtime_ns, stat.st_size, identity)


def arrival_dates(store, rows):
    """Actual dated raw points in a geodesic 1 km circle, excluding sandbox data."""
    from .rail_coverage import project
    from .rail_boundary_rules import geographic_distances, EARTH_RADIUS
    from .map_points import world_coordinate
    result = {}
    with store.connect() as db:
        for station in rows:
            lon,lat = station['coordinates']; dy = math.degrees(1000/EARTH_RADIUS)
            dx = dy/max(.01, math.cos(math.radians(lat)))
            x0,y1 = world_coordinate(lon-dx,lat-dy); x1,y0 = world_coordinate(lon+dx,lat+dy)
            candidates = db.execute('''SELECT p.longitude,p.latitude,t.travel_date FROM map_point_index i
                JOIN map_points p ON p.id=i.id JOIN trips t ON t.id=p.trip_id
                WHERE i.minx<=? AND i.maxx>=? AND i.miny<=? AND i.maxy>=?
                AND COALESCE(json_extract(t.summary,'$.metadata.sandbox'),0)=0''', (x1,x0,y1,y0)).fetchall()
            hits = geographic_distances([project(r['longitude'],r['latitude']) for r in candidates], station['xy']) <= 1000
            dates = [row['travel_date'] for row,hit in zip(candidates,hits) if hit]
            result[station['id']] = {'first_arrival_date': min(dates) if dates else None,
                                     'arrival_source': 'nearby_gps' if dates else None}
    return result


def preview_interval(store, line_id, origin_id, destination_id, catalog=None):
    catalog = catalog or line_catalog(store, line_id)
    chain = catalog.path(origin_id, destination_id)
    rows = [catalog.rows[identity] for identity in chain]
    dates = arrival_dates(store, rows)
    details = [{'id': s['id'], 'name': s['name'], 'kind': s['kind'], **dates[s['id']]} for s in rows]
    a,b = rows[0],rows[-1]; gaps = []; length = 0
    for left,right in zip(rows,rows[1:]):
        edge = catalog.edges[tuple(sorted((left['id'],right['id'])))]; length += edge['length']
        if edge['gap']:
            gaps.append({'origin':left['name'],'destination':right['name'],'distance_m':round(edge['length'],1)})
    section = {'origin':a['name'],'destination':b['name'],'origin_id':a['id'],'destination_id':b['id'],
               'origin_kind':a['kind'],'destination_kind':b['kind'],'origin_station':details[0], 'destination_station':details[-1],
               'order_start':a['order'],'order_end':b['order'],'distance_km':round(length/1000,1),
               'start':a['coordinates'],'end':b['coordinates'],'station_details':details,
               'station_count':len(details),'stations':[s['name'] for s in details],
               'reference_gaps':gaps,'manual':True}
    section['interval_id'] = section_id(line_id, section)
    return section


def _merge_sections(store, line_id, sections, catalog):
    """Union reached sections sharing station identities, including hub branches."""
    sections = list(sections)
    changed = True
    while changed:
        changed = False
        for i,a in enumerate(sections):
            for j in range(i+1,len(sections)):
                b = sections[j]
                sa = {s['id'] for s in a.get('station_details', [])}
                sb = {s['id'] for s in b.get('station_details', [])}
                if not sa & sb:
                    continue
                candidates = [a,b]
                left = min(candidates, key=lambda s:s['order_start'])
                right = max(candidates, key=lambda s:s['order_end'])
                merged = preview_interval(store,line_id,left['origin_id'],right['destination_id'],catalog)
                # Preserve reached branch stops as well as the trunk reference.
                details = {row['id']: row for s in (a,b,merged) for row in s.get('station_details',[])}
                extras = [catalog.rows[identity] for identity in details if identity not in {s['id'] for s in merged['station_details']} and identity in catalog.rows]
                dates = arrival_dates(store, extras)
                for row in extras:
                    details[row['id']] = {'id':row['id'],'name':row['name'],'kind':row['kind'],**dates[row['id']]}
                ordered = sorted(details.values(), key=lambda row: (catalog.rows.get(row['id'],{}).get('order',float('inf')),row['name']))
                merged['station_details'] = ordered
                merged['station_count'] = len(ordered)
                merged['stations'] = [row['name'] for row in ordered]
                shared = sa & sb
                if sa <= sb:
                    overlap = a['distance_km']
                elif sb <= sa:
                    overlap = b['distance_km']
                elif len(shared) >= 2 and shared <= catalog.rows.keys():
                    shared_order = sorted(shared,key=lambda identity:catalog.rows[identity]['order'])
                    first,last = shared_order[0],shared_order[-1]
                    overlap = preview_interval(store,line_id,first,last,catalog)['distance_km']
                else:
                    overlap = 0
                merged['distance_km'] = round(max(a['distance_km'],b['distance_km'],a['distance_km']+b['distance_km']-overlap),1)
                gaps = {tuple((g['origin'],g['destination'],g['distance_m'])):g for s in (a,b,merged) for g in s.get('reference_gaps',[])}
                merged['reference_gaps'] = list(gaps.values())
                sections[i] = merged; sections.pop(j); changed = True; break
            if changed:
                break
    return sorted(sections, key=lambda s:(s['order_start'],s['order_end']))


def save_interval(store, line_id, origin_id, destination_id, interval_id, expected_revision):
    from .rail_coverage import read_coverage
    current = next((line for line in read_coverage(store)['lines'] if line['line_id']==line_id), None)
    catalog = line_catalog(store,line_id)
    section = preview_interval(store,line_id,origin_id,destination_id,catalog)
    line = deepcopy(current) if current else {'line_id':line_id,'name':catalog.line['name'],'intervals':[],
        'trip_count':0,'source_url':f'https://www.openstreetmap.org/relation/{line_id}','direction':catalog.direction}
    if interval_id and not any(s['interval_id']==interval_id for s in line['intervals']):
        raise EditConflict('该区间已变化或被删除，请刷新后重新编辑。')
    line['intervals'] = _merge_sections(store,line_id,[s for s in line['intervals'] if s['interval_id']!=interval_id]+[section],catalog)
    # Manual corrections are line snapshots, so future automatic rebuilds cannot
    # resurrect removed stations/sections. Restoring automatic is reversible too.
    line['manual'] = True
    line.pop('manual_revision', None)
    return ManualStore(store).change(line_id,line,expected_revision)


def delete_record(store, line_id, interval_id, expected_revision):
    from .rail_coverage import read_coverage
    line = next((line for line in read_coverage(store)['lines'] if line['line_id']==line_id), None)
    if line is None:
        raise EditConflict('线路已被删除或改变，请刷新列表。')
    original = deepcopy(line)
    if interval_id:
        if not any(s['interval_id']==interval_id for s in line['intervals']):
            raise EditConflict('区间已被删除或改变，请刷新列表。')
        line['intervals'] = [s for s in line['intervals'] if s['interval_id']!=interval_id]
    if not interval_id or not line['intervals']:
        line = {'hidden':True,'line_id':line_id,'name':line['name'],'saved_line':original}
    return ManualStore(store).change(line_id,line,expected_revision)
