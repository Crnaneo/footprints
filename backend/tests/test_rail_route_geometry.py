"""Single-catalog-line route reconstruction (used when a source has no curve).

The rule is entirely claim/coordinate driven and applies to every line uniformly:
exactly one catalog line serving both endpoints yields that line's geometry, while
zero (disconnected) or several (ambiguous) shared lines yield no route. Nothing is
specific to any one station pair.
"""
import json
import sqlite3

import pytest

from backend import rail_route_geometry as rrg


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("FOOTPRINT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("backend.rail_coverage.passenger_directory", lambda _: {})
    monkeypatch.setattr("backend.passenger_sections.passenger_directory", lambda _: {})
    rail = tmp_path / "rail"
    rail.mkdir()
    # Two lines share 甲(10) and 乙(11); 丙(12) is only on line 1 and 丁(13) only
    # on line 2. Stations sit ~5 km apart so the 1.2 km match radius never bleeds
    # into a neighbour, keeping the shared-line count exact.
    nodes = {"甲": (10, 110.00, 35.00), "乙": (11, 110.05, 35.00),
             "丙": (12, 110.10, 35.00), "丁": (13, 110.05, 35.05)}
    with sqlite3.connect(rail / "catalog.sqlite3") as db:
        db.execute("CREATE TABLE lines(id INTEGER,name TEXT,tags TEXT,ways TEXT,stations TEXT)")
        db.execute("CREATE TABLE ways(id INTEGER,nodes TEXT,geometry TEXT,tags TEXT)")
        db.execute("CREATE TABLE stations(id INTEGER,name TEXT,lon REAL,lat REAL,tags TEXT)")
        for wid, a, b in [(100, "甲", "乙"), (101, "乙", "丙"), (102, "甲", "乙"), (103, "乙", "丁")]:
            db.execute("INSERT INTO ways VALUES(?,?,?,?)", (
                wid, json.dumps([nodes[a][0], nodes[b][0]]),
                json.dumps([[nodes[a][1], nodes[a][2]], [nodes[b][1], nodes[b][2]]]), json.dumps({})))
        tags = json.dumps({"network": "中国铁路"}, ensure_ascii=False)
        db.execute("INSERT INTO lines VALUES(?,?,?,?,?)", (1, "测试一线", tags, json.dumps([100, 101]), json.dumps([10, 11, 12])))
        db.execute("INSERT INTO lines VALUES(?,?,?,?,?)", (2, "测试二线", tags, json.dumps([102, 103]), json.dumps([10, 11, 13])))
        for name, (nid, lon, lat) in nodes.items():
            db.execute("INSERT INTO stations VALUES(?,?,?,?,?)", (
                nid, name, lon, lat, json.dumps({"railway": "station", "passenger": "yes"})))
    rrg._index.cache_clear()
    return nodes


def lonlat(nodes, name):
    return nodes[name][1], nodes[name][2]


def test_single_shared_line_returns_its_geometry(catalog):
    route = rrg.unique_line_route(lonlat(catalog, "甲"), lonlat(catalog, "丙"))
    assert route is not None
    coords, name = route
    assert name == "测试一线"
    assert len(coords) >= 2
    # Ordered from the origin to the destination along the physical line.
    assert list(coords[0]) == pytest.approx([110.00, 35.00], abs=1e-6)
    assert list(coords[-1]) == pytest.approx([110.10, 35.00], abs=1e-6)


def test_several_shared_lines_are_ambiguous_and_rejected(catalog):
    # 甲 and 乙 are served by both lines, so no single line describes the ride.
    assert rrg.unique_line_route(lonlat(catalog, "甲"), lonlat(catalog, "乙")) is None


def test_no_shared_line_is_rejected(catalog):
    # 丙 is only on line 1 and 丁 only on line 2: no line connects them.
    assert rrg.unique_line_route(lonlat(catalog, "丙"), lonlat(catalog, "丁")) is None


@pytest.mark.parametrize("reverse", [False, True])
def test_import_retains_small_curve_vertices_without_expanding_national_index(catalog, reverse):
    # These real source bends are below the statistics index's 8 m tolerance.
    # Imports must recover them from the catalog, not add points to its chord.
    path = rrg.catalog_path()
    raw = [[110, 35], [110.02, 35.00004], [110.03, 34.99996], [110.05, 35]]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE ways SET geometry=? WHERE id=100", (json.dumps(raw),))
    index = rrg._load_index()
    assert len(index.ways[100]['geom'].coords) == 2
    detailed = rrg.catalog_line_ways(index, 1)
    assert set(detailed) == {100, 101}  # no unrelated national ways loaded
    assert len(detailed[100]['geom'].coords) == len(raw)
    assert len(index.ways[100]['geom'].coords) == 2  # shared statistics index unchanged
    a, b = ("丙", "甲") if reverse else ("甲", "丙")
    coords, name = rrg.unique_line_route(lonlat(catalog, a), lonlat(catalog, b))
    expected = raw + [[110.10, 35]]
    if reverse:
        expected.reverse()
    assert len(coords) == len(expected)
    for got, want in zip(coords, expected):
        assert got == pytest.approx(want, abs=1e-9)


def test_sparse_source_is_not_filled_with_invented_points(catalog):
    coords, _ = rrg.unique_line_route(lonlat(catalog, "甲"), lonlat(catalog, "丙"))
    assert len(coords) == 3  # exactly the two ways' original endpoints/junction
