"""Reconstruct a ride from the local OSM catalog when a data source supplies
station stops but no intermediate curve geometry.

Only a single catalog line relation that serves both endpoints is accepted. A
missing connection (no shared line) or an ambiguous one (several shared lines,
e.g. a hub with parallel routes) yields no route, so the caller rejects the
import instead of inventing a path. Every rule is coordinate/claim driven and
applies uniformly to all lines in the national catalog — nothing is specific to
any one station pair.
"""
from __future__ import annotations

from functools import lru_cache
import json
import math
import sqlite3
import threading

INDEX_LOCK = threading.Lock()

# A station node can sit a few hundred metres from the platform centre used by
# the schedule source; this radius absorbs that offset without reaching the next
# station on the same corridor.
STATION_MATCH_METERS = 1200


def catalog_path():
    from .store import default_database_path
    return default_database_path().parent / "rail" / "catalog.sqlite3"


@lru_cache(maxsize=2)
def _index(path, mtime, size):
    from .rail_coverage import RailIndex
    return RailIndex(path)


def _load_index():
    path = catalog_path()
    if not path.exists():
        return None
    stat = path.stat()
    with INDEX_LOCK:
        return _index(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


def _claimed_lines(index, xy, tolerance):
    """Catalog line relations serving any station node within tolerance of xy."""
    hits = index.station_tree.query_ball_point(xy, tolerance)
    lines = set()
    for i in hits:
        lines |= index.station_claims.get(index.station_nodes[i], set())
    return lines


def _nearest_row(rows, xy, tolerance):
    best = None
    best_distance = float("inf")
    for row in rows:
        distance = math.dist(row["xy"], xy)
        if distance < best_distance:
            best, best_distance = row, distance
    return best if best is not None and best_distance <= tolerance else None


def catalog_station_coordinate(name):
    """Coordinate for a uniquely located catalog station missing from mapLine."""
    index = _load_index()
    if index is None:
        return None
    from .rail_coverage import station_name, unproject
    matches = index.named_station_xy.get(station_name(name), [])
    if not matches or any(math.dist(matches[0], xy) > STATION_MATCH_METERS for xy in matches[1:]):
        return None
    return unproject(*matches[0])


def catalog_line_ways(index, line_id):
    """Load unsimplified source vertices only for the line being imported.

    The national matching index intentionally simplifies ways for statistics.
    Footprint imports must retain the catalog's actual curve vertices instead;
    neither resampling simplified edges nor interpolating gaps restores them.
    Keep this local to the requested line so browsing/statistics need no larger
    national index.
    """
    from .rail_coverage import project
    from shapely.geometry import LineString

    ids = sorted({wid for wid in index.reference_way_ids[line_id] if wid in index.ways})
    ways = {}
    with sqlite3.connect(index.path) as db:
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            for wid, raw in db.execute(f"SELECT id,geometry FROM ways WHERE id IN ({placeholders})", batch):
                geometry = LineString([project(*coord) for coord in json.loads(raw)])
                ways[wid] = {**index.ways[wid], "geom": geometry, "length": geometry.length}
    return ways


def unique_line_route(origin_lonlat, destination_lonlat, tolerance=STATION_MATCH_METERS):
    """Return (coords, line_name) for the single catalog line linking the two
    WGS84 station points, or None when the connection is missing or ambiguous.

    coords is an ordered list of (lon, lat) WGS84 tuples running from origin to
    destination along that line's physical geometry.
    """
    index = _load_index()
    if index is None:
        return None
    from .rail_coverage import project, unproject
    from shapely.ops import substring

    origin_xy = project(*origin_lonlat)
    destination_xy = project(*destination_lonlat)
    common = _claimed_lines(index, origin_xy, tolerance) & _claimed_lines(index, destination_xy, tolerance)
    if len(common) != 1:
        # 0 shared lines: the endpoints are only joined via hub connectors, so
        # no single line describes the ride. >1: the via is ambiguous.
        return None
    line = next(iter(common))
    ways = catalog_line_ways(index, line)
    metadata, stations, _, _ = index.reference_catalog({line}, minimum=0, reference_ways=ways)
    for key, rows in stations.items():
        if key[0] != line:
            continue
        origin_row = _nearest_row(rows, origin_xy, tolerance)
        destination_row = _nearest_row(rows, destination_xy, tolerance)
        if origin_row is None or destination_row is None:
            continue
        axis = metadata[key]
        start = origin_row["position"]
        end = destination_row["position"]
        if start == end:
            continue
        segment = substring(axis, min(start, end), max(start, end))
        coords = [unproject(*point) for point in segment.coords]
        if len(coords) >= 2:
            if start > end:
                coords.reverse()
            return coords, index.lines[line]["name"]
    return None
