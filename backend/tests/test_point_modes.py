import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess

from backend.map_points import COLORS, register_view, tile
from backend.models import TripCreate
from backend.point_modes import backfill, effective_modes, infer_point_modes
from backend.store import TripStore


def build_catalog(path: Path, ways):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE ways(id INTEGER PRIMARY KEY, nodes TEXT, geometry TEXT, tags TEXT)')
    for index, coords in enumerate(ways):
        db.execute('INSERT INTO ways VALUES(?,?,?,?)', (index + 1, '[]', json.dumps(coords), '{}'))
    db.commit()
    db.close()


def track_way():
    # ~90 m of catalogue track through (100, 30) in projected metres.
    return [[99.9995, 30.0], [100.0005, 30.0]]


def on_track(offset=0.0, **fields):
    point = {'longitude': 100.0 + offset, 'latitude': 30.0 + offset,
             'speed_kmh': 300, 'altitude_m': 40, 'timestamp': None}
    point.update(fields)
    return point


def decode(data, tmp_path):
    p = tmp_path / 'modes.pbf'
    p.write_bytes(data)
    script = ("import fs from 'node:fs';import {VectorTile} from '@mapbox/vector-tile';"
              "import {PbfReader} from 'pbf';const t=new VectorTile(new PbfReader(fs.readFileSync(process.argv[1])));"
              "const l=t.layers.points;const out=[];for(let i=0;i<l.length;i++){const f=l.feature(i);"
              "out.push({id:f.id,properties:f.properties})}process.stdout.write(JSON.stringify(out));")
    return json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script, str(p)],
                                              cwd=Path(__file__).resolve().parents[2]))


def build_bucket_catalog(path: Path, rows):
    """Catalog with an empty ways table plus overseas rail_way_buckets rows."""
    build_catalog(path, [])
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE rail_way_buckets(bucket TEXT NOT NULL, way_id INTEGER NOT NULL,'
               ' geometry TEXT NOT NULL, PRIMARY KEY(bucket, way_id)) WITHOUT ROWID')
    db.executemany('INSERT INTO rail_way_buckets VALUES(?,?,?)', rows)
    db.commit()
    db.close()


def load_builder():
    spec = importlib.util.spec_from_file_location(
        'build_global_rail',
        Path(__file__).resolve().parents[2] / 'scripts' / 'build_global_rail.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rail_proximity_and_low_altitude_artefacts(tmp_path):
    catalog = tmp_path / 'rail' / 'catalog.sqlite3'
    build_catalog(catalog, [track_way()])
    # No airborne seeds: a genuine ground segment beside the catalog track.
    points = [on_track(speed_kmh=306),
              on_track(0.02, speed_kmh=8, altitude_m=None),  # ~2 km away
              on_track(0.001, speed_kmh=520, altitude_m=120)]  # artefact stays other
    assert infer_point_modes(points, catalog) == ['rail', None, None]


def test_speed_gate_blocks_fast_fixes_on_the_track(tmp_path):
    catalog = tmp_path / 'rail' / 'catalog.sqlite3'
    build_catalog(catalog, [track_way()])
    assert infer_point_modes([on_track(speed_kmh=900, altitude_m=40)], catalog) == [None]
    # Missing speed cannot disprove rail proximity on legacy imports.
    assert infer_point_modes([on_track(speed_kmh=None, altitude_m=None)], catalog) == ['rail']


def test_airborne_context_marks_approach_points_flight(tmp_path):
    catalog = tmp_path / 'rail' / 'catalog.sqlite3'
    build_catalog(catalog, [track_way()])
    seeds = [{'longitude': 101.0 + i * 0.01, 'latitude': 30.5, 'speed_kmh': 820,
              'altitude_m': 10500, 'timestamp': f'2026-01-01T00:0{i}:00Z'} for i in range(4)]
    approach = {'longitude': 100.0, 'latitude': 30.0, 'speed_kmh': 280,
                'altitude_m': 900, 'timestamp': '2026-01-01T00:04:00Z'}
    assert infer_point_modes(seeds + [approach], catalog) == ['flight'] * 4 + ['flight']


def test_effective_modes_only_overlay_csv_sources(tmp_path):
    catalog = tmp_path / 'rail' / 'catalog.sqlite3'
    build_catalog(catalog, [track_way()])
    points = [on_track(), on_track(0.02, speed_kmh=8, altitude_m=None)]
    csv_record = {'mode': 'other', 'source': 'csv', 'points': points}
    assert effective_modes(csv_record, catalog) == ['rail', 'other']
    step_record = {'mode': 'other', 'source': '一生足迹', 'points': points}
    assert effective_modes(step_record, catalog) == ['rail', 'other']
    for provider in ('RailGo', 'Flightradar24', 'OSM', '本地目录经由', 'manual'):
        record = {'mode': 'other', 'source': provider, 'points': points}
        assert effective_modes(record, catalog) == ['other', 'other']
    declared = {'mode': 'rail', 'source': 'csv', 'points': points}
    assert effective_modes(declared, catalog) == ['rail', 'rail']
    # A missing catalog degrades to the declared colour instead of failing.
    assert effective_modes(csv_record, None) == ['other', 'other']


def test_new_csv_trips_get_per_point_tile_colours(tmp_path):
    catalog = tmp_path / 'rail' / 'catalog.sqlite3'
    build_catalog(catalog, [track_way()])
    store = TripStore(tmp_path / 'db.sqlite3')
    saved = store.create(TripCreate(title='混合导入', mode='other', travel_date='2026-01-01', source='csv',
                                    points=[on_track(), on_track(0.02, speed_kmh=8, altitude_m=None)]))
    view = register_view(store, [saved['id']])
    features = decode(tile(store, view['id'], 0, 0, 0)[0], tmp_path)
    assert sorted(f['properties']['color'] for f in features) == sorted([COLORS['rail'], COLORS['other']])


def test_backfill_recolours_existing_csv_trips_once(tmp_path):
    store = TripStore(tmp_path / 'db.sqlite3')
    csv_trip = store.create(TripCreate(title='旧导入', mode='other', travel_date='2026-01-01', source='一生足迹',
                                       points=[on_track(), on_track(0.02, speed_kmh=8, altitude_m=None)]))
    provider = store.create(TripCreate(title='结构化来源', mode='other', travel_date='2026-01-01', source='RailGo',
                                       points=[on_track()]))
    view = register_view(store, [csv_trip['id'], provider['id']])
    initial = decode(tile(store, view['id'], 0, 0, 0)[0], tmp_path)
    assert len(initial) == 3 and all(f['properties']['color'] == COLORS['other'] for f in initial)
    # The catalog arrives after import: only the backfill can colour history.
    build_catalog(tmp_path / 'rail' / 'catalog.sqlite3', [track_way()])
    assert backfill(store) == 1
    # A fresh view resolves the new revision; tiles re-read per-point modes.
    fresh = register_view(store, [csv_trip['id']])
    csv_colours = sorted(f['properties']['color'] for f in decode(tile(store, fresh['id'], 0, 0, 0)[0], tmp_path))
    assert csv_colours == sorted([COLORS['rail'], COLORS['other']])
    provider_view = register_view(store, [provider['id']])
    assert all(f['properties']['color'] == COLORS['other']
               for f in decode(tile(store, provider_view['id'], 0, 0, 0)[0], tmp_path))
    assert backfill(store) == 0  # versioned marker keeps the pass idempotent


def test_bucket_table_classifies_overseas_tracks(tmp_path):
    catalog = tmp_path / 'rail' / 'catalog.sqlite3'
    build_bucket_catalog(catalog, [('34,140', 500, json.dumps([[139.9995, 35.0], [140.0005, 35.0]]))])
    overseas = {'longitude': 140.0, 'latitude': 35.0, 'speed_kmh': 306,
                'altitude_m': 40, 'timestamp': None}
    far = {'longitude': 140.02, 'latitude': 35.0, 'speed_kmh': 8,
           'altitude_m': None, 'timestamp': None}
    # (100, 30) is domestic-empty, the Tokyo point rides the bucketed way.
    assert infer_point_modes([on_track(), overseas, far], catalog) == [None, 'rail', None]


def test_boundary_points_search_neighbour_buckets(tmp_path):
    # 100 m ≈ 0.001°, so a fix this close to a bucket border must reach the
    # neighbouring slice even when the way is not stored in its own bucket.
    catalog = tmp_path / 'rail' / 'catalog.sqlite3'
    build_bucket_catalog(catalog, [('34,138', 501, json.dumps([[139.9995, 35.0], [140.0005, 35.0]]))])
    east_of_border = {'longitude': 140.00004, 'latitude': 35.0, 'speed_kmh': 200,
                      'altitude_m': 40, 'timestamp': None}
    assert infer_point_modes([east_of_border], catalog) == ['rail']
    mirrored = tmp_path / 'rail-mirror' / 'catalog.sqlite3'
    build_bucket_catalog(mirrored, [('34,140', 502, json.dumps([[139.9995, 35.0], [140.0005, 35.0]]))])
    west_of_border = {'longitude': 139.99996, 'latitude': 35.0, 'speed_kmh': 200,
                      'altitude_m': 40, 'timestamp': None}
    assert infer_point_modes([west_of_border], mirrored) == ['rail']


def test_importer_filters_and_buckets_features():
    builder = load_builder()
    feature = {'type': 'Feature', 'properties': {'railway': 'rail', 'osm_id': 700},
               'geometry': {'type': 'LineString',
                            'coordinates': [[139.9995, 34.9995], [140.0005, 35.0005]]}}
    rows = list(builder.feature_rows(feature, set()))
    # The way bbox straddles the 138/140 meridian: one row per covered bucket.
    assert {row[0] for row in rows} == {'34,138', '34,140'}
    assert {row[1] for row in rows} == {700}
    # Tram stays out (same taxonomy as the national catalog).
    tram = {**feature, 'properties': {'railway': 'tram', 'osm_id': 701}}
    assert list(builder.feature_rows(tram, set())) == []
    # Domestic catalog ids are excluded, string ids still parse.
    assert list(builder.feature_rows(feature, {700})) == []
    assert {row[1] for row in builder.feature_rows(
        {**feature, 'properties': {'railway': 'rail', 'osm_id': '702'}}, set())} == {702}
    # MultiLineString keeps its nested shape so every segment survives.
    multi = {**feature, 'properties': {'railway': 'rail', 'osm_id': 703},
             'geometry': {'type': 'MultiLineString',
                          'coordinates': [[[139.9, 34.9], [139.95, 34.95]],
                                          [[140.1, 35.1], [140.15, 35.15]]]}}
    multi_rows = list(builder.feature_rows(multi, set()))
    assert multi_rows and json.loads(multi_rows[0][2])[0][0] == [139.9, 34.9]
    assert {row[0] for row in multi_rows} == {'34,138', '34,140'}
