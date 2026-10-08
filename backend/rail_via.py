"""Build a rail footprint from a hand-entered sequence of via lines/stations.

Unlike the RailGo train-number import, this reconstructs a route purely from the
local OSM catalog: the user chains lines and stations, and the geometry follows
each line's reference axis. Optional per-station times can be filled manually or
computed from the line's highest running speed.
"""
from __future__ import annotations

import heapq
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from shapely.geometry import LineString, Point
from shapely.ops import substring

from .rail_coverage import unproject
from .rail_manual import editor_index
from .rail_route_geometry import catalog_line_ways
from .store import TripStore

CHINA_OFFSET = timedelta(hours=8)
DEFAULT_HIGH_SPEED = 250.0
DEFAULT_CONVENTIONAL = 120.0
_SPEED_RE = re.compile(r"(\d+(?:\.\d+)?)")
# OSM models junction crossovers as separate parallel-track ways whose ends
# nearly touch (measured: 5 m in 济南枢纽) without sharing a node; treat such
# gaps as connected. Wider gaps stay broken so unconnected stubs never bridge.
_JUNCTION_TOLERANCE = 15.0
# Some junctions have genuine data gaps instead of near-touching ends
# (measured: 79 m of missing track in the 黄台 crossover), retried with this
# looser bridge. Larger gaps are retained as independent pieces.
_LOOSE_JUNCTION_TOLERANCE = 120.0
# Only these junction-scale fragments are bridged into one continuous run.
# Wider gaps (a whole missing relation segment, measured 507 m) stay broken:
# the run is exported as separate pieces, each with its own geometry and time,
# rather than drawing an invented straight line across the missing track.
_BRIDGE_TOLERANCES = (_JUNCTION_TOLERANCE, _LOOSE_JUNCTION_TOLERANCE)
# A station point may sit up to a few kilometres from its line's tracks inside
# a large junction (measured: 黄台 1.85 km, 昌平 2.7 km from their tracks).
_STATION_SNAP_METERS = 3000.0


class ViaError(ValueError):
    """A user-facing error while resolving a via route."""


def _parse_speed(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in {"signals", "none", "no"}:
        return None
    match = _SPEED_RE.search(text)
    if not match:
        return None
    number = float(match.group(1))
    if "mph" in text:
        number *= 1.60934
    return number if 0 < number <= 1000 else None


@lru_cache(maxsize=256)
def _line_speed(path: str, mtime: int, size: int, line_id: int) -> tuple[float, str]:
    """Highest running speed of a line from member ways, with a name fallback."""
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT name, ways FROM lines WHERE id=?", (line_id,)).fetchone()
        if row is None:
            raise ViaError("线路不存在于本地铁路目录。")
        name, ways = row[0], json.loads(row[1] or "[]")
        speeds: list[float] = []
        if ways:
            placeholders = ",".join("?" * len(ways))
            for (tags,) in db.execute(f"SELECT tags FROM ways WHERE id IN ({placeholders})", ways):
                parsed = _parse_speed(json.loads(tags).get("maxspeed"))
                if parsed:
                    speeds.append(parsed)
    if speeds:
        return max(speeds), "osm"
    high = any(word in name for word in ("高铁", "高速", "客专", "城际", "动车"))
    return (DEFAULT_HIGH_SPEED if high else DEFAULT_CONVENTIONAL), "estimate"


def line_max_speed(store: TripStore, line_id: str) -> tuple[float, str]:
    index = editor_index(store)
    try:
        identity = int(line_id)
    except (TypeError, ValueError):
        raise ViaError("铁路线编号无效。") from None
    if identity not in index.lines:
        raise ViaError("请选择本地铁路目录中的线路。")
    stat = index.path.stat()
    return _line_speed(str(index.path.resolve()), stat.st_mtime_ns, stat.st_size, identity)


@lru_cache(maxsize=8)
def _station_table(path: str, mtime: int, size: int) -> tuple[list[tuple[str, str, str, float, float]], dict[str, int]]:
    """Pre-built station lookup tables, computed once per catalog revision.

    Returns a passenger-first, name-sorted row list (id, name, kind, lon, lat)
    so combobox queries can slice/early-stop instead of rebuilding and sorting
    the whole catalog on every keystroke, plus a station_id -> index position map
    so ``station_lines`` skips the linear scan over ``index.station_ids``.
    """
    from .rail_manual import _index, INDEX_LOCK
    with INDEX_LOCK:
        index = _index(path, mtime, size)
    rows: list[tuple[str, str, str, float, float]] = []
    for i, name in enumerate(index.station_names):
        lon, lat = unproject(*index.station_xy[i])
        rows.append((index.station_ids[i], name, index.station_kinds[i], lon, lat))
    rows.sort(key=lambda row: (row[2] != "客运站", len(row[1]), row[1]))
    id_map: dict[str, int] = {}
    for i, identity in enumerate(index.station_ids):
        id_map.setdefault(identity, i)
    return rows, id_map


def _tables(store: TripStore) -> tuple[list[tuple[str, str, str, float, float]], dict[str, int]]:
    index = editor_index(store)
    stat = index.path.stat()
    return _station_table(str(index.path.resolve()), stat.st_mtime_ns, stat.st_size)


def search_stations(store: TripStore, query: str, limit: int = 40) -> list[dict[str, Any]]:
    """Catalog stations whose name contains the query, passengers ranked first.

    The row list is pre-sorted (passenger-first, then shorter names, then name),
    so an empty query returns the leading slice and a substring query stops as
    soon as ``limit`` matches are found — no full-catalog dict build or sort.
    """
    table, _ = _tables(store)
    needle = (query or "").strip()
    results: list[dict[str, Any]] = []
    for sid, name, kind, lon, lat in table:
        if needle and needle not in name:
            continue
        results.append({"id": sid, "name": name, "kind": kind, "longitude": lon, "latitude": lat})
        if len(results) >= limit:
            break
    return results


def station_lines(store: TripStore, station_id: str) -> list[dict[str, Any]]:
    """Lines physically serving a station, from declared members and nearby ways."""
    index = editor_index(store)
    _, id_map = _tables(store)
    si = id_map.get(station_id)
    if si is None:
        raise ViaError("该车站不在本地铁路目录中，请重新选择。")
    node = index.station_nodes[si]
    line_ids = set(index.station_claims.get(node, set()))
    from shapely.geometry import Point
    point = Point(index.station_xy[si])
    for wi in index.tree.query(point, predicate="dwithin", distance=120):
        way_id = index.ids[wi]
        line_ids.update(index.members[way_id])
    found = [{"line_id": str(lid), "name": index.lines[lid]["name"]}
             for lid in line_ids if lid in index.lines]
    found.sort(key=lambda row: (len(row["name"]), row["name"], row["line_id"]))
    if not found:
        raise ViaError("该车站没有可用的接入线路资料。")
    return found


class _LineAxis:
    """Cached reference axis and station positions for one line."""

    def __init__(self, index, line_id: int):
        self.ways = catalog_line_ways(index, line_id)
        metadata, stations, _, _ = index.reference_catalog({line_id}, minimum=0, reference_ways=self.ways)
        if not metadata:
            raise ViaError("该线路缺少可用的本地几何资料。")
        self.metadata = metadata
        self.index = index
        self.line_id = line_id
        self.declared_station_ids = {
            sid for sid, node in zip(index.station_ids, index.station_nodes)
            if node in index.lines[line_id]["station_nodes"]
        }
        self.positions: dict[str, list[tuple[Any, float]]] = {}
        self.station_xy: dict[str, tuple[float, float]] = {}
        for key, rows in stations.items():
            for row in rows:
                self.positions.setdefault(row["id"], []).append((key, row["position"]))
                self.station_xy.setdefault(row["id"], row["xy"])

    def _catalog_station_xy(self, station_id: str) -> tuple[float, float] | None:
        """Reference stations miss platforms far from the line's tracks (measured:
        昌平 2.7 km off the 京通线 main run), so fall back to the full catalog."""
        cached = getattr(self, "_all_xy", None)
        if cached is None:
            cached = self._all_xy = {
                sid: (float(xy[0]), float(xy[1]))
                for sid, xy in zip(self.index.station_ids, self.index.station_xy)
            }
        return cached.get(station_id)

    def segment(self, origin_id: str, via_id: str) -> list[tuple[list[tuple[float, float]], float]]:
        """Geometry between two stations as one or more continuous pieces.

        A normal run is a single piece. When the line's ways break into
        disconnected components (missing OSM relation segments), the run is
        exported as separate pieces instead of one invented straight line;
        the caller splits the track there and keeps every piece's own time.
        """
        if origin_id == via_id:
            raise ViaError("同一区段的始发站与经由站不能相同。")
        shared = {key for key, _ in self.positions.get(origin_id, [])} & \
                 {key for key, _ in self.positions.get(via_id, [])}
        if not shared:
            # Diameter-based spine extraction splits every line with junction
            # crossovers into several spines, so two declared stations of one
            # line often end up on different ones. Rebuild the run from the
            # line's own real ways instead of failing outright.
            return self._cross_axis_pieces(origin_id, via_id)
        key = max(shared, key=lambda k: self.metadata[k].length)
        axis = self.metadata[key]
        start = dict(self.positions[origin_id])[key]
        end = dict(self.positions[via_id])[key]
        geometry = substring(axis, min(start, end), max(start, end))
        coords = list(geometry.coords)
        if start > end:
            coords.reverse()
        if len(coords) < 2:
            raise ViaError("这两站之间缺少可用的线路几何。")
        return [([unproject(*xy) for xy in coords], abs(end - start))]

    def _cross_axis_pieces(self, origin_id: str, via_id: str) -> list[tuple[list[tuple[float, float]], float]]:
        start_xy = self.station_xy.get(origin_id) or self._catalog_station_xy(origin_id)
        end_xy = self.station_xy.get(via_id) or self._catalog_station_xy(via_id)
        if start_xy is None or end_xy is None:
            raise ViaError("所选线路上找不到这两站之间的线路资料。")
        first = self._nearest_line_way(start_xy, origin_id)
        last = self._nearest_line_way(end_xy, via_id)
        if first is None or last is None:
            raise ViaError("所选线路上找不到这两站之间的线路资料。")
        chain = None
        for tolerance in _BRIDGE_TOLERANCES:
            chain = self._way_chain(first, last, tolerance=tolerance)
            if chain is not None:
                break
        raw = [(self._chain_coords(chain, start_xy), 0.0)] if chain is not None \
            else self._component_pieces(first, last, start_xy)
        pieces: list[tuple[list[tuple[float, float]], float]] = []
        for i, (coords, _) in enumerate(raw):
            # Ways can extend well beyond the chosen stations. Clip the first
            # and last piece to station projections, including reversed runs.
            geometry = LineString(coords)
            start = geometry.project(Point(start_xy)) if i == 0 else 0.0
            end = geometry.project(Point(end_xy)) if i == len(raw) - 1 else geometry.length
            if start == end:
                continue
            coords = list(substring(geometry, start, end).coords)
            length = sum(math.dist(a, b) for a, b in zip(coords, coords[1:]))
            if len(coords) < 2:
                raise ViaError("所选线路上找不到这两站之间的线路资料。")
            pieces.append(([unproject(*xy) for xy in coords], length))
        if not pieces:
            raise ViaError("所选线路上找不到这两站之间的线路资料。")
        return pieces

    def _component_pieces(self, first: int, last: int,
                          start_xy: tuple[float, float]) -> list[tuple[list[tuple[float, float]], float]]:
        """Export a broken run as one piece per connected component.

        Components are ordered along the run by the nearest endpoint pair
        between two components; those gap endpoints stay unconnected (no
        straight line is drawn across missing track) and carry no distance.
        """
        adjacency = self._way_adjacency(_LOOSE_JUNCTION_TOLERANCE)
        parent = {wid: wid for wid in adjacency}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for wid, neighbours in adjacency.items():
            for other in neighbours:
                ra, rb = find(wid), find(other)
                if ra != rb:
                    parent[ra] = rb
        if find(first) == find(last):
            # Same component but no chain above cannot happen; keep a guard.
            chain = self._way_chain(first, last, tolerance=_LOOSE_JUNCTION_TOLERANCE)
            if chain is None:
                raise ViaError("所选线路上找不到这两站之间的线路资料。")
            return [(self._chain_coords(chain, start_xy), 0.0)]
        members: dict[int, list[int]] = defaultdict(list)
        for wid in adjacency:
            members[find(wid)].append(wid)
        comp_xy: dict[int, list[tuple[tuple[float, float], int]]] = {}
        for root, group in members.items():
            points: list[tuple[tuple[float, float], int]] = []
            for wid in group:
                coords = self.ways[wid]["geom"].coords
                points.append((coords[0], wid))
                points.append((coords[-1], wid))
            comp_xy[root] = points
        # Nearest endpoint pair between every ordered pair of components.
        from scipy.spatial import cKDTree
        trees: dict[int, cKDTree] = {}

        def tree_of(root: int) -> cKDTree:
            built = trees.get(root)
            if built is None:
                built = trees[root] = cKDTree([xy for xy, _ in comp_xy[root]])
            return built

        edges: dict[tuple[int, int], tuple[tuple[float, float], tuple[float, float], int, int, float]] = {}
        for a in comp_xy:
            own = [xy for xy, _ in comp_xy[a]]
            for b in comp_xy:
                if a == b:
                    continue
                distances, indexes = tree_of(b).query(own, k=1)
                j = int(distances.argmin())
                pa, wa = comp_xy[a][j]
                pb, wb = comp_xy[b][int(indexes[j])]
                edges[(a, b)] = (pa, pb, wa, wb, float(distances[j]))
        start_root, goal_root = find(first), find(last)
        queue = [(0.0, start_root, [start_root], [])]
        heapq.heapify(queue)
        best: dict[int, float] = {start_root: 0.0}
        plan: tuple[list[int], list[tuple[tuple[float, float], tuple[float, float], int, int, float]]] | None = None
        while queue:
            cost, comp, comps, bridges = heapq.heappop(queue)
            if comp == goal_root:
                plan = (comps, bridges)
                break
            for (a, b), edge in edges.items():
                if a != comp:
                    continue
                total = cost + edge[4]
                if total < best.get(b, float("inf")):
                    best[b] = total
                    heapq.heappush(queue, (total, b, comps + [b], bridges + [edge]))
        if plan is None:
            raise ViaError("所选线路上找不到这两站之间的线路资料。")
        comps, bridges = plan
        pieces: list[tuple[list[tuple[float, float]], float]] = []
        cursor_xy = start_xy
        entry_way = first
        for k, _comp in enumerate(comps):
            exit_way = bridges[k][2] if k < len(bridges) else last
            chain = self._way_chain(entry_way, exit_way, tolerance=_LOOSE_JUNCTION_TOLERANCE)
            if chain is None:
                raise ViaError("所选线路上找不到这两站之间的线路资料。")
            pieces.append((self._chain_coords(chain, cursor_xy), 0.0))
            if k < len(bridges):
                cursor_xy = bridges[k][1]  # far side of the gap: next piece starts here
                entry_way = bridges[k][3]
        return pieces

    def _nearest_line_way(self, xy: tuple[float, float], station_id: str | None = None) -> int | None:
        """The catalog way of this line closest to a station's projected point."""
        from shapely.geometry import Point
        point = Point(*xy)
        best: tuple[float, int] | None = None
        for wi in self.index.tree.query(point, predicate="dwithin", distance=_STATION_SNAP_METERS):
            way_id = self.index.ids[wi]
            if self.line_id not in self.index.members.get(way_id, []):
                continue
            distance = point.distance(self.ways[way_id]["geom"])
            if best is None or distance < best[0]:
                best = (distance, way_id)
        if best is not None:
            return best[1]
        # The public catalog retains explicitly declared stations even inside
        # geometry holes. They must also work as via endpoints: clip to the
        # closest available track, without drawing or counting the missing
        # section. Proximity alone must not attach unrelated stations.
        if station_id in self.declared_station_ids and self.ways:
            return min(self.ways, key=lambda wid: point.distance(self.ways[wid]["geom"]))
        return None

    def _way_adjacency(self, tolerance: float = _JUNCTION_TOLERANCE) -> dict[int, dict[int, float]]:
        """Way-level adjacency; each edge carries the bridge distance it needs."""
        cache = getattr(self, "_adjacency_cache", None)
        if cache is None:
            cache = self._adjacency_cache = {}
        cached = cache.get(tolerance)
        if cached is not None:
            return cached
        ways = [wid for wid in self.index.reference_way_ids.get(self.line_id, [])
                if wid in self.ways]
        adjacency: dict[int, dict[int, float]] = {wid: {} for wid in ways}
        node_ways: dict[Any, set[int]] = defaultdict(set)
        for wid in ways:
            for node in self.ways[wid]["nodes"]:
                node_ways[node].add(wid)
        for group in node_ways.values():
            for wid in group:
                for other in group:
                    if other != wid:
                        adjacency[wid][other] = 0.0
        # Bridge near-touching endpoints OSM modeled without a shared node,
        # keeping the smallest bridge when two ways could connect twice.
        grid: dict[tuple[int, int], list[tuple[tuple[float, float], int]]] = defaultdict(list)
        for wid in ways:
            coords = self.ways[wid]["geom"].coords
            for xy in (coords[0], coords[-1]):
                grid[(int(xy[0] // tolerance), int(xy[1] // tolerance))].append((xy, wid))
        for (cx, cy), cell in grid.items():
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for a, wa in cell:
                        for b, wb in grid.get((cx + dx, cy + dy), []):
                            if wa == wb:
                                continue
                            bridge = math.dist(a, b)
                            if bridge <= tolerance and (wb not in adjacency[wa] or bridge < adjacency[wa][wb]):
                                adjacency[wa][wb] = bridge
                                adjacency[wb][wa] = bridge
        cache[tolerance] = adjacency
        return adjacency

    def _way_chain(self, first: int, last: int, tolerance: float = _JUNCTION_TOLERANCE) -> list[int] | None:
        """Cheapest way sequence between two ways, bridges included in the cost."""
        adjacency = self._way_adjacency(tolerance)
        queue = [(0.0, first, [first])]
        heapq.heapify(queue)
        best: dict[int, float] = {}
        while queue:
            cost, wid, chain = heapq.heappop(queue)
            if cost >= best.get(wid, float("inf")):
                continue
            best[wid] = cost
            if wid == last:
                return chain
            for other, bridge in adjacency.get(wid, {}).items():
                heapq.heappush(queue, (cost + bridge + self.ways[other]["length"], other, chain + [other]))
        return None

    def _chain_coords(self, chain: list[int], start_xy: tuple[float, float]) -> list[tuple[float, float]]:
        """Orient each way along the chain; a bridged gap becomes a straight link."""
        coords: list[tuple[float, float]] = []
        for i, wid in enumerate(chain):
            points = list(self.ways[wid]["geom"].coords)
            tail = coords[-1] if coords else start_xy
            # Chain neighbours need not share nodes (bridged gaps do not), so
            # orientation is decided by endpoint distance, not node identity.
            if math.dist(points[-1], tail) < math.dist(points[0], tail):
                points.reverse()
            if coords and math.dist(points[0], tail) > 1.0:
                coords.extend(points)
            else:
                coords.extend(points[1:] if coords else points)
        return coords


@lru_cache(maxsize=16)
def _line_axis(path: str, mtime: int, size: int, line_id: int) -> _LineAxis:
    from .rail_manual import _index, INDEX_LOCK
    with INDEX_LOCK:
        return _LineAxis(_index(path, mtime, size), line_id)


def _axis(store: TripStore, line_id: str) -> _LineAxis:
    index = editor_index(store)
    try:
        identity = int(line_id)
    except (TypeError, ValueError):
        raise ViaError("铁路线编号无效。") from None
    if identity not in index.lines:
        raise ViaError("请选择本地铁路目录中的线路。")
    stat = index.path.stat()
    return _line_axis(str(index.path.resolve()), stat.st_mtime_ns, stat.st_size, identity)


def _clock(value: str | None, label: str) -> timedelta | None:
    if value is None or value == "":
        return None
    # A trailing "+1" (e.g. "23:40+1") pushes the time to the next day, for
    # trains that cross midnight; the day offset flows into the time fields.
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?:\s*\+\s*(\d{1,2}))?\s*", value)
    if not match:
        raise ViaError(f"{label}时间格式应为 HH:MM，跨零点可写 HH:MM+1 表示次日。")
    hour, minute = int(match.group(1)), int(match.group(2))
    day = int(match.group(3) or 0)
    if hour > 23 or minute > 59 or day > 30:
        raise ViaError(f"{label}时间超出有效范围。")
    return timedelta(days=day, hours=hour, minutes=minute)


def build_route(store: TripStore, travel_date: str, rows: list[dict[str, Any]],
                origin_id: str | None = None, destination_id: str | None = None) -> dict[str, Any]:
    """Resolve geometry, distances, speeds and times for a chained via route.

    Each row: origin_id, line_id, via_id, departure {mode,value}, arrival {mode,value}.
    The first row must start at the title origin and the last row must end at the
    title destination; intermediate rows may not use the destination as via.
    Returns a TripCreate-compatible draft plus a per-segment summary for the UI.
    """
    if not rows:
        raise ViaError("请至少填写一段经由。")
    if origin_id is not None and str(rows[0].get("origin_id")) != str(origin_id):
        raise ViaError("第一段的始发站必须是标题中的始发站。")
    if destination_id is not None:
        for i, row in enumerate(rows[:-1]):
            if str(row.get("via_id")) == str(destination_id):
                raise ViaError(f"第 {i + 1} 段的经由站不能是终到站，请把它放到最后一段。")
        if str(rows[-1].get("via_id")) != str(destination_id):
            raise ViaError("最后一段的经由站必须是标题中的终到站。")
    base_date = datetime.fromisoformat(travel_date)

    segments: list[dict[str, Any]] = []
    coords_all: list[tuple[float, float]] = []
    row_spans: list[list[tuple[int, int]]] = []  # per row: index ranges into coords_all
    piece_breaks: set[int] = set()  # index whose previous pair is a broken gap
    durations: list[float | None] = []

    for i, row in enumerate(rows):
        axis = _axis(store, str(row["line_id"]))
        pieces = axis.segment(str(row["origin_id"]), str(row["via_id"]))
        speed, source = line_max_speed(store, str(row["line_id"]))
        length_m = sum(length for _, length in pieces)
        distance_km = length_m / 1000.0
        duration = (length_m / (speed / 3.6)) if speed else None
        spans: list[tuple[int, int]] = []
        for j, (coords, _length) in enumerate(pieces):
            # Keep both endpoints at row boundaries: each row has its own
            # segment ID, and dropping its first point would lose its first
            # edge (or the whole row when it only has two coordinates).
            start = len(coords_all)
            disconnected = bool(coords_all and coords and not _same_point(coords_all[-1], coords[0]))
            coords_all.extend(coords)
            end = len(coords_all)
            if end > start:
                spans.append((start, end))
                if start and (j or disconnected):
                    piece_breaks.add(start)
        row_spans.append(spans)
        durations.append(duration)
        segments.append({
            "index": i, "line_id": str(row["line_id"]), "line_name": row.get("line_name") or "",
            "origin_name": row.get("origin_name") or "", "via_name": row.get("via_name") or "",
            "distance_km": round(distance_km, 1), "max_speed_kmh": round(speed, 1),
            "speed_source": source, "duration_seconds": duration,
        })

    cumulative = [0.0]
    for idx, (a, b) in enumerate(zip(coords_all, coords_all[1:]), start=1):
        # A broken gap between pieces carries no distance: the missing track is
        # not walked, so neither geometry nor time invents it.
        step = 0.0 if idx in piece_breaks else _haversine(a, b)
        cumulative.append(cumulative[-1] + step)
    station_pos = [0.0]
    for i in range(len(rows)):
        spans = row_spans[i]
        station_pos.append(cumulative[spans[-1][1] - 1] if spans else station_pos[-1])
    station_dep, station_arr, anchors = _resolve_times(base_date, rows, durations, segments, station_pos)
    points = _build_points(segments, row_spans, coords_all, cumulative, anchors, station_dep, station_arr)
    origin_name = segments[0]["origin_name"]
    destination_name = segments[-1]["via_name"]
    total_km = round(sum(s["distance_km"] for s in segments), 1)
    speeds = [s["max_speed_kmh"] for s in segments if s.get("max_speed_kmh")]
    flags = ["via_reconstruction", "no_observed_speed"]
    if piece_breaks:
        # Some rows were split at disconnected components (missing OSM track);
        # the map shows separate pieces there instead of an invented straight line.
        flags.append("via_gap_split")
    draft = {
        "title": f"{origin_name} → {destination_name} · {travel_date}",
        "mode": "rail", "travel_date": travel_date, "service_number": None,
        "origin": origin_name, "destination": destination_name,
        "source": "本地目录经由", "source_crs": "WGS84", "geometry_crs": "WGS84",
        "geometry_kind": "reconstructed",
        "design_speed_kmh": max(speeds) if speeds else None,
        "line_speed_limit_kmh": max(speeds) if speeds else None,
        "quality_flags": flags,
        "points": points,
        "metadata": {
            "via_segments": [{k: s[k] for k in ("line_id", "line_name", "origin_name", "via_name",
                              "distance_km", "max_speed_kmh", "speed_source", "departure", "arrival")}
                             for s in segments],
            "total_distance_km": total_km,
            "notes": ["经由重建：按你在本地铁路目录中选择的线路与车站拼接路径，并非列车实测 GPS。",
                      "保留本地 OSM 原始线路节点，未简化或插值补点；缺失线路几何不会补画。",
                      "两处手动时间之间的路段按实际用时匀速填充；其余路段按线路最高运行速度估算。"] +
                     (["线路几何不连续处已拆为独立轨迹段，缺口不连线、不计里程，时间连续。"] if piece_breaks else []),
        },
    }
    public_segments = [{k: s[k] for k in ("index", "line_name", "origin_name", "via_name",
                        "distance_km", "max_speed_kmh", "speed_source", "departure", "arrival")}
                       for s in segments]
    return {"trip": draft, "segments": public_segments, "total_distance_km": total_km}


def _same_point(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) < 1e-7 and abs(a[1] - b[1]) < 1e-7


def _resolve_times(base_date: datetime, rows: list[dict[str, Any]], durations: list[float | None],
                   segments: list[dict[str, Any]], station_pos: list[float]
                   ) -> tuple[list[datetime], list[datetime], list[tuple[float, str, datetime]]]:
    """Resolve every station time from the manual anchors.

    Anchors are the manually entered times. Between two neighbouring anchors the
    chain is filled at the observed average speed (track length / elapsed time),
    crossing stopovers and line changes; before the first and after the last
    anchor the chain is propagated at each line's top running speed.
    """
    n = len(rows)

    def manual_clock(spec: dict[str, Any] | None, label: str) -> datetime | None:
        if (spec or {}).get("mode") != "manual":
            return None
        delta = _clock(spec.get("value"), label)
        if delta is None:
            raise ViaError(f"{label}时间设为手动，但未填写时间。")
        return base_date + delta

    anchors: list[tuple[float, str, datetime]] = []  # (track position, kind, instant)
    first = manual_clock(rows[0].get("departure"), "第 1 行始发")
    if first is not None:
        anchors.append((station_pos[0], "dep", first))
    for i, row in enumerate(rows):
        arrival = manual_clock(row.get("arrival"), f"第 {i + 1} 行经由")
        if arrival is not None:
            anchors.append((station_pos[i + 1], "arr", arrival))
        if i:
            departure = manual_clock(row.get("departure"), f"第 {i + 1} 行始发")
            if departure is not None:
                anchors.append((station_pos[i], "dep", departure))
    if not anchors:
        raise ViaError("至少手动设置一个时间，其余时间才能自动计算。")
    anchors.sort(key=lambda a: (a[0], 0 if a[1] == "arr" else 1))
    for left, right in zip(anchors, anchors[1:]):
        if right[2] < left[2] or (right[0] > left[0] and right[2] == left[2]):
            raise ViaError("手动时间与经由顺序冲突：后方车站的时间不晚于前方车站，请检查各行时间。")

    def event_time(pos: float, kind: str, label: str) -> datetime:
        left = right = None
        for anchor in anchors:
            if anchor[0] <= pos:
                left = anchor
            if right is None and anchor[0] >= pos:
                right = anchor
        if left is not None and right is not None and left[0] < right[0]:
            fraction = (pos - left[0]) / (right[0] - left[0])
            return left[2] + (right[2] - left[2]) * fraction
        if left is not None and right is not None:  # arrival/departure pair at one station
            for anchor in (right, left):
                if anchor[1] == kind:
                    return anchor[2]
            return left[2]
        if left is None:  # before the first anchor: roll back at line top speed
            head_pos, _, head_time = anchors[0]
            total = 0.0
            for j in range(n):
                if station_pos[j] >= pos - 1e-6 and station_pos[j + 1] <= head_pos + 1e-6:
                    if durations[j] is None:
                        raise ViaError(f"无法计算{label}：缺少相邻已知时间或该线路最高运行速度。")
                    total += durations[j]
            return head_time - timedelta(seconds=total)
        tail_pos, _, tail_time = anchors[-1]  # after the last anchor
        total = 0.0
        for j in range(n):
            if station_pos[j] >= tail_pos - 1e-6 and station_pos[j + 1] <= pos + 1e-6:
                if durations[j] is None:
                    raise ViaError(f"无法计算{label}：缺少相邻已知时间或该线路最高运行速度。")
                total += durations[j]
        return tail_time + timedelta(seconds=total)

    station_dep: list[datetime] = []
    station_arr: list[datetime] = []
    for j in range(n):
        station_dep.append(event_time(station_pos[j], "dep", f"第 {j + 1} 行始发时间"))
        station_arr.append(event_time(station_pos[j + 1], "arr", f"第 {j + 1} 行经由时间"))
    for j in range(n):
        if station_arr[j] < station_dep[j]:
            raise ViaError(f"第 {j + 1} 行经由时间早于始发时间，请检查填写的时间。")
        segments[j]["departure"] = _time_field(station_dep[j], base_date)
        segments[j]["arrival"] = _time_field(station_arr[j], base_date)
    return station_dep, station_arr, anchors


def _time_field(instant: datetime, base_date: datetime) -> dict[str, Any]:
    offset = instant - base_date
    day = offset.days
    seconds = offset.seconds
    iso = (instant - CHINA_OFFSET).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"time": f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}",
            "day_offset": day, "iso": iso}


def _build_points(segments: list[dict[str, Any]], row_spans: list[list[tuple[int, int]]],
                  coords_all: list[tuple[float, float]], cumulative: list[float],
                  anchors: list[tuple[float, str, datetime]],
                  station_dep: list[datetime], station_arr: list[datetime]) -> list[dict[str, Any]]:
    """Timestamp each point: observed average speed between anchors, else per segment.

    A row whose line breaks into disconnected components contributes several
    pieces; each piece gets its own segment_id so the map does not draw a line
    across the missing track, while the timeline stays continuous.
    """
    points: list[dict[str, Any]] = []
    for i, segment in enumerate(segments):
        spans_row = row_spans[i]
        if not spans_row:
            continue
        dep, arr = station_dep[i], station_arr[i]
        # Time flows over the whole row: split pieces resume exactly where the
        # previous piece stopped, so the timeline never jumps at a gap.
        row_start = spans_row[0][0]
        row_length = cumulative[spans_row[-1][1] - 1] - cumulative[row_start] or 1.0
        for j, (start, end) in enumerate(spans_row):
            if start >= end:
                continue
            segment_id = str(i) if j == 0 else f"{i}/{j}"
            for idx in range(start, end):
                pos = cumulative[idx]
                if idx == row_start:
                    instant = dep
                elif idx == end - 1 and j == len(spans_row) - 1:
                    instant = station_arr[i]
                else:
                    left = right = None
                    for anchor in anchors:
                        if anchor[0] <= pos:
                            left = anchor
                        if right is None and anchor[0] >= pos:
                            right = anchor
                    if left is not None and right is not None and left[0] < right[0]:
                        fraction = (pos - left[0]) / (right[0] - left[0])
                        instant = left[2] + (right[2] - left[2]) * fraction
                    elif left is not None and right is not None:
                        # Segment start exactly at a station holding both anchors.
                        instant = left[2] if left[1] == "dep" else right[2]
                    else:
                        local = (pos - cumulative[row_start]) / row_length
                        instant = dep + (arr - dep) * local
                points.append({"sequence": len(points), "segment_id": segment_id,
                               "longitude": coords_all[idx][0], "latitude": coords_all[idx][1],
                               "timestamp": (instant - CHINA_OFFSET).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "altitude_m": None, "speed_kmh": None,
                               "source": "本地目录", "source_crs": "WGS84"})
    if len(points) < 2:
        raise ViaError("经由路径缺少有效的轨迹点。")
    return points


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    value = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371008.8 * 2 * math.asin(min(1.0, math.sqrt(value)))
