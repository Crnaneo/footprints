"""Infer per-point flight/rail display modes for CSV-imported tracks.

CSV imports default to the generic "other" colour even when the recorded GPS
clearly follows a rail alignment or a flight profile. This module derives a
per-point display mode from two generic, source-agnostic signals only —
proximity to the local OSM rail catalog geometry and the recorded
altitude/speed profile (the same airborne context the rail coverage uses) —
so historical and future imports are coloured by one identical rule without
per-trip, per-line or per-point special cases.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from shapely import STRtree, distance as shapely_distance, points as shapely_points
from shapely.geometry import LineString, MultiLineString

from .map_points import COLORS
from .rail_flight_context import flight_point_mask

# Bump when the inference rule changes so stored trips are re-classified.
# v2: overseas ways come from the lazily indexed rail_way_buckets table.
# v3: bucket catalog grew (Overture deu/usa/rus + worldwide metro) — same rule,
# re-run so stored trips pick up the wider coverage.
CLASSIFIER_VERSION = 3
# Same corridor width as the rail-coverage reference-axis binning: wide enough
# for catalogue-vs-track survey offsets, narrow enough to skip paralleling roads.
RAIL_PROXIMITY_M = 100.0
# Above this speed a fix is aircraft territory (maglev tops out around 430).
RAIL_MAX_SPEED_KMH = 430.0
# Generic CSV provenances (csv_io default plus StepLife backups). Structured
# providers (RailGo/Flightradar24/OSM/manual) already carry a reliable mode.
CSV_SOURCES = {'csv', '一生足迹'}
# Shared equirectangular scales (see rail_coverage.py).
SCALE_X = 111320.0 * math.cos(math.radians(35))
SCALE_Y = 111320.0

TREE_LOCK = threading.Lock()
_TREE_CACHE: dict[tuple, tuple] = {}
# Overseas catalog granularity: a 2°×2° slice keeps one bucket's STRtree
# small enough to build lazily in well under a second, while a point only
# ever needs its own bucket plus (at most) the neighbours it borders.
BUCKET_DEG = 2
BUCKET_CACHE_SIZE = 32
BUCKET_TABLE = 'rail_way_buckets'
_BUCKET_CACHE: dict[tuple, '_BucketIndex'] = {}


def catalog_path_for_db(db: sqlite3.Connection) -> Path | None:
    """Resolve data/rail/catalog.sqlite3 next to the open trip database."""
    for row in db.execute('PRAGMA database_list'):
        if row[1] == 'main' and row[2]:
            return Path(row[2]).parent / 'rail' / 'catalog.sqlite3'
    return None


def _rail_tree(path: Path):
    """Cache the national way geometry in a projected STRtree per catalog build."""
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    with TREE_LOCK:
        cached = _TREE_CACHE.get(key)
        if cached is not None:
            return cached
        geoms: list[LineString] = []
        db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            for (geometry,) in db.execute('SELECT geometry FROM ways'):
                coords = json.loads(geometry)
                if len(coords) >= 2:
                    geoms.append(LineString([(lon * SCALE_X, lat * SCALE_Y) for lon, lat in coords]))
        finally:
            db.close()
        array = np.array(geoms, dtype=object)
        tree = STRtree(array) if geoms else None
        entry = (tree, array)
        _TREE_CACHE.clear()
        _TREE_CACHE[key] = entry
        return entry


def _projected_line(payload: str):
    """Catalog way geometry JSON -> projected shapely line (line or multi)."""
    coords = json.loads(payload)
    if coords and isinstance(coords[0][0], (int, float)):
        return LineString([(lon * SCALE_X, lat * SCALE_Y) for lon, lat in coords])
    return MultiLineString([LineString([(lon * SCALE_X, lat * SCALE_Y) for lon, lat in segment])
                            for segment in coords if len(segment) >= 2])


class _BucketIndex:
    """Lazily project rail_way_buckets slices into small STRtrees (LRU)."""

    def __init__(self, path: Path):
        self._path = path
        self._entries: OrderedDict[str, tuple | None] = OrderedDict()

    @classmethod
    def open(cls, path: Path):
        db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (BUCKET_TABLE,)).fetchone() is not None
        finally:
            db.close()
        return cls(path) if present else None

    def tree_for(self, bucket: str):
        """(tree, geoms) for a bucket label, or None when the slice is empty."""
        with TREE_LOCK:
            entry = self._entries.get(bucket)
            if entry is not None or bucket in self._entries:
                self._entries.move_to_end(bucket)
                return entry
        geoms: list = []
        db = sqlite3.connect(f'file:{self._path}?mode=ro', uri=True)
        try:
            for (payload,) in db.execute(
                    f'SELECT geometry FROM {BUCKET_TABLE} WHERE bucket=?', (bucket,)):
                geoms.append(_projected_line(payload))
        finally:
            db.close()
        array = np.array(geoms, dtype=object)
        entry = (STRtree(array), array) if geoms else None
        with TREE_LOCK:
            self._entries[bucket] = entry
            while len(self._entries) > BUCKET_CACHE_SIZE:
                self._entries.popitem(last=False)
        return entry


def _bucket_index(path: Path):
    """Cache one _BucketIndex per catalog build; absent tables degrade to None."""
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    with TREE_LOCK:
        index = _BUCKET_CACHE.get(key)
    if index is None:
        try:
            index = _BucketIndex.open(path)
        except sqlite3.Error:
            index = None
        with TREE_LOCK:
            _BUCKET_CACHE.clear()
            if index is not None:
                _BUCKET_CACHE[key] = index
    return index


def _point_buckets(point: dict) -> tuple[str, ...]:
    """The point's bucket plus neighbours within the rail corridor width."""
    lon = float(point['longitude'])
    lat = float(point['latitude'])
    cos_lat = max(math.cos(math.radians(lat)), 1e-3)
    lon_span = RAIL_PROXIMITY_M / (SCALE_Y * cos_lat)
    lat_span = RAIL_PROXIMITY_M / SCALE_Y
    base_lon = math.floor(lon / BUCKET_DEG) * BUCKET_DEG
    base_lat = math.floor(lat / BUCKET_DEG) * BUCKET_DEG
    lons = {base_lon}
    lats = {base_lat}
    if lon - base_lon < lon_span:
        lons.add(base_lon - BUCKET_DEG)
    if base_lon + BUCKET_DEG - lon < lon_span:
        lons.add(base_lon + BUCKET_DEG)
    if lat - base_lat < lat_span:
        lats.add(base_lat - BUCKET_DEG)
    if base_lat + BUCKET_DEG - lat < lat_span:
        lats.add(base_lat + BUCKET_DEG)
    return tuple(f'{la},{lo}' for la in sorted(lats) for lo in sorted(lons))


def _mark_rail(tree, geoms, indexes: list[int], points: list[dict],
               modes: list[str | None]) -> list[int]:
    """Flag candidates within the corridor; return those still unresolved."""
    indexes = [index for index in indexes if modes[index] is None]
    if tree is None or not indexes:
        return indexes
    xy = np.array([(points[i]['longitude'] * SCALE_X, points[i]['latitude'] * SCALE_Y)
                   for i in indexes])
    query = shapely_points(xy)
    inputs, hits = tree.query_nearest(query)
    if not len(inputs):
        return indexes
    gaps = shapely_distance(query[inputs], geoms[hits])
    for position, gap in zip(inputs, gaps):
        if gap > RAIL_PROXIMITY_M:
            continue
        index = indexes[int(position)]
        speed = points[index].get('speed_kmh')
        if speed is None or speed <= RAIL_MAX_SPEED_KMH:
            modes[index] = 'rail'
    return [index for index in indexes if modes[index] is None]


def infer_point_modes(points: list[dict], catalog_path: Path | None) -> list[str | None]:
    """Return flight/rail/None per point; None keeps the trip's declared colour."""
    modes: list[str | None] = [None] * len(points)
    if not points:
        return modes
    # Reuse the coverage airborne context: repeated fast/high fixes condemn the
    # surrounding ±5 min window, low-altitude recording artefacts do not.
    for index in np.flatnonzero(flight_point_mask(points)):
        modes[int(index)] = 'flight'
    candidates = [i for i, mode in enumerate(modes) if mode is None]
    if candidates and catalog_path is not None and Path(catalog_path).exists():
        try:
            base_tree, base_geoms = _rail_tree(Path(catalog_path))
            buckets = _bucket_index(Path(catalog_path))
        except (OSError, sqlite3.Error, ValueError):
            base_tree, base_geoms, buckets = None, None, None
        if base_tree is not None or buckets is not None:
            # The national ways table and the overseas bucket slices are
            # disjoint id sets, so the nearest way is the min over both.
            remaining = _mark_rail(base_tree, base_geoms, candidates, points, modes)
            if buckets is not None and remaining:
                groups: dict[tuple[str, ...], list[int]] = {}
                for index in remaining:
                    groups.setdefault(_point_buckets(points[index]), []).append(index)
                for key, indexes in groups.items():
                    for bucket in key:
                        entry = buckets.tree_for(bucket)
                        if entry is not None:
                            indexes = _mark_rail(entry[0], entry[1], indexes, points, modes)
                            if not indexes:
                                break
    return modes


def effective_modes(record: dict, catalog_path: Path | None) -> list[str]:
    """Per-point display mode: inferred where confident, else the declared mode."""
    points = record.get('points') or []
    default = record.get('mode') if record.get('mode') in COLORS else 'other'
    if not points or record.get('source') not in CSV_SOURCES:
        return [default] * len(points)
    inferred = infer_point_modes(points, catalog_path)
    return [mode if mode in COLORS else default for mode in inferred]


def backfill(store) -> int:
    """Classify stored CSV trips once per rule version; return refined trips."""
    with store.connect() as db:
        catalog = catalog_path_for_db(db)
    if catalog is None or not catalog.exists():
        return 0
    with store.connect() as db:
        pending = [row[0] for row in db.execute(
            'SELECT t.id FROM trips t WHERE t.id NOT IN '
            '(SELECT trip_id FROM point_classification WHERE version=?)', (CLASSIFIER_VERSION,))]
    refined = 0
    for index, trip_id in enumerate(pending, 1):
        with store.connect() as db:
            row = db.execute('SELECT payload,summary FROM trips WHERE id=?', (trip_id,)).fetchone()
            if row is None:
                continue
            if json.loads(row['summary']).get('source') not in CSV_SOURCES:
                # Structured providers keep their declared colour on every point.
                db.execute('INSERT OR REPLACE INTO point_classification VALUES(?,?)',
                           (trip_id, CLASSIFIER_VERSION))
                continue
        record = json.loads(row['payload'])
        modes = effective_modes(record, catalog)
        with store.connect() as db:
            current = {ordinal: mode for ordinal, mode in db.execute(
                'SELECT ordinal,mode FROM map_points WHERE trip_id=?', (trip_id,))}
            updates = [(mode, trip_id, ordinal) for ordinal, mode in enumerate(modes)
                       if current.get(ordinal) != mode]
            if updates:
                db.executemany('UPDATE map_points SET mode=? WHERE trip_id=? AND ordinal=?', updates)
                db.execute('UPDATE map_revision SET revision=revision+1 WHERE id=1')
            db.execute('INSERT OR REPLACE INTO point_classification VALUES(?,?)',
                       (trip_id, CLASSIFIER_VERSION))
        refined += bool(updates)
        if index % 200 == 0:
            print(f'[point-modes] classified {index}/{len(pending)} trips ({refined} recoloured)', flush=True)
    if pending:
        print(f'[point-modes] backfill done: {len(pending)} trips scanned, {refined} recoloured', flush=True)
    return refined
