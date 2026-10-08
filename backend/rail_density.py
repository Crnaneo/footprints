"""Restore source railway vertices on saved routes, without querying new schedules."""
from copy import deepcopy
from functools import lru_cache
import hashlib
import math
from datetime import datetime, timezone

from shapely.geometry import LineString, Point
from shapely.ops import substring
from shapely.strtree import STRtree

from .models import TripCreate, canonical_trip
from .rail_coverage import project, unproject
from .rail_manual import editor_index
from .rail_route_geometry import catalog_line_ways

VERSION = 1
# Saved WGS84 coordinates round-trip through the same projection to sub-mm.
EDGE_TOLERANCE_M = 0.005


def payload_hash(payload):
    return hashlib.sha256(payload.encode()).hexdigest()


def _key(coord):
    return round(coord[0], 6), round(coord[1], 6)


class LineSource:
    def __init__(self, index, line_id):
        self.raw = catalog_line_ways(index, line_id)
        self.ids = list(self.raw)
        self.old = [index.ways[wid]['geom'] for wid in self.ids]
        self.tree = STRtree(self.old)

    def restore_edge(self, a, b):
        """Match the old simplified source edge, then retain its raw vertices.

        Matching against the old geometry (not merely a nearby parallel track)
        prevents a different station/branch choice from changing the saved ride.
        Unmatched junction gaps keep their endpoints and receive no new points.
        """
        pa, pb = Point(a), Point(b)
        if pa.distance(pb) < 1e-6:
            return [a, b], False
        edge = LineString([a, b])
        candidates = []
        for wi in self.tree.query(pa, predicate='dwithin', distance=EDGE_TOLERANCE_M):
            old = self.old[wi]
            if old.distance(pb) > EDGE_TOLERANCE_M:
                continue
            old_a, old_b = old.project(pa), old.project(pb)
            clipped = substring(old, old_a, old_b)
            if clipped.geom_type != 'LineString' or clipped.hausdorff_distance(edge) > EDGE_TOLERANCE_M:
                continue
            raw = self.raw[self.ids[wi]]['geom']
            raw_a, raw_b = raw.project(pa), raw.project(pb)
            if (old_b - old_a) * (raw_b - raw_a) <= 0:
                continue
            restored = substring(raw, raw_a, raw_b)
            if restored.geom_type != 'LineString' or restored.hausdorff_distance(edge) > 9:
                continue
            coords = list(restored.coords)
            candidates.append(coords)
        if not candidates:
            return [a, b], False
        unique = {tuple(_key(coord) for coord in coords): coords for coords in candidates}
        if len(unique) != 1:
            # The old simplification cannot identify which of two different
            # curves with the same chord was used. Do not invent that choice.
            return [a, b], False
        return next(iter(unique.values())), True


@lru_cache(maxsize=16)
def _line_source(index, line_id):
    return LineSource(index, line_id)


def _instant(stamp):
    return datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(timezone.utc)


def _restore_piece(source, old):
    points = [deepcopy(old[0])]
    matched = unmatched = 0
    for a, b in zip(old, old[1:]):
        coords, found = source.restore_edge(project(a['longitude'], a['latitude']),
                                             project(b['longitude'], b['latitude']))
        matched += int(found)
        unmatched += int(not found)
        cumulative = [0.0]
        for left, right in zip(coords, coords[1:]):
            cumulative.append(cumulative[-1] + math.dist(left, right))
        departure, arrival = _instant(a['timestamp']), _instant(b['timestamp'])
        if arrival < departure:
            raise ValueError('原轨迹时间顺序不一致')
        # All interior saved points are original retained vertices. Only the
        # first/last station cuts can move slightly onto the unsimplified curve.
        if found and len(points) == 1:
            points[0].update(zip(('longitude', 'latitude'), unproject(*coords[0])))
        for coord, distance in zip(coords[1:-1], cumulative[1:-1]):
            instant = departure + (arrival - departure) * (distance / cumulative[-1])
            lon, lat = unproject(*coord)
            points.append({**a, 'longitude': lon, 'latitude': lat,
                           'timestamp': instant.strftime('%Y-%m-%dT%H:%M:%SZ')})
        tail = deepcopy(b)
        if found and b is old[-1]:
            tail.update(zip(('longitude', 'latitude'), unproject(*coords[-1])))
        points.append(tail)
    return points, matched, unmatched


def upgrade_record(store, record):
    if record['mode'] != 'rail' or record['geometry_kind'] != 'reconstructed':
        raise ValueError('仅升级重建的铁路导入，不能改写实测轨迹')
    metadata = record.get('metadata', {})
    if metadata.get('rail_point_density', {}).get('version') == VERSION:
        return deepcopy(record)
    # These new imports already retain every available source vertex.
    if any('保留本地 OSM 原始线路节点' in note for note in metadata.get('notes', [])):
        return deepcopy(record)
    if record['source'] == 'RailGo' and metadata.get('source_section_count', 0) > 0:
        return deepcopy(record)
    if record['source'] != '本地目录经由' or not metadata.get('via_segments'):
        raise ValueError('缺少可离线恢复的原经由信息')
    index = editor_index(store)
    replacement = deepcopy(record)
    points = []
    matched = unmatched = 0
    for i, segment in enumerate(metadata['via_segments']):
        row = [p for p in record['points'] if p['segment_id'].split('/')[0] == str(i)]
        if not row or any(p.get('timestamp') is None or p.get('speed_kmh') is not None
                          or p.get('altitude_m') is not None for p in row):
            raise ValueError('经由点列缺失或包含实测字段，不能自动替换')
        source = _line_source(index, int(segment['line_id']))
        groups = {}
        for point in row:
            groups.setdefault(point['segment_id'], []).append(point)
        for identity, old in groups.items():
            restored, found, missing = _restore_piece(source, old)
            matched += found; unmatched += missing
            for point in restored:
                points.append({**point, 'sequence': len(points), 'segment_id': identity})
    if len(points) <= len(record['points']):
        raise ValueError('未找到可恢复的额外原始节点，需检查源几何')
    replacement['points'] = points
    replacement['metadata'].setdefault('notes', []).append('已按原点列所在的线路边恢复 OSM 原始节点；未空间插值补点，原经由、断段及时间锚点保留。')
    replacement['metadata']['rail_point_density'] = {
        'version': VERSION, 'simplification_m': 0, 'previous_point_count': len(record['points']),
        'point_count': len(points), 'preserved_timestamps': True,
        'matched_source_edges': matched, 'edges_without_extra_geometry': unmatched,
    }
    return {**replacement, **canonical_trip(TripCreate.model_validate(replacement))}


def apply_plan(store, changes):
    """Check every original payload before atomically updating indexed records."""
    with store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        for change in changes:
            row = connection.execute('SELECT payload FROM trips WHERE id=?', (change['record']['id'],)).fetchone()
            if row is None or payload_hash(row['payload']) != change['before_sha256']:
                raise ValueError('记录在规划后发生变化，已取消整批更新')
        for change in changes:
            store.replace_record(connection, change['record'])
