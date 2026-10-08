"""Import a global overseas rail catalog from HOT OSM country exports.

The national catalog (download_rail_catalog.py) only carries OSM ways that
touch the China bbox, so tracks recorded anywhere else stay unclassified.
Rather than chasing the user's current footprints, this script fills the gap
worldwide: it streams every HOT OSM per-country railway GeoJSON export
(HDX production-raw-data-api, the same OpenStreetMap source as the national
catalog) and buckets the way geometry into the catalog database in a
`rail_way_buckets` table. The ways table (used by RailIndex) stays untouched;
domestic OSM ways are skipped because the national catalog already covers
them with a newer snapshot.

backend/point_modes.py reads the bucket table lazily (a 2° STRtree per
bucket), so trips recorded anywhere on the planet are coloured by the exact
same proximity rule — no per-region or per-point special cases.

Usage:
  .venv/bin/python scripts/build_global_rail.py            # all datasets
  .venv/bin/python scripts/build_global_rail.py jpn sgp    # subset by ISO3

The run is idempotent (INSERT OR IGNORE) and resumable: downloaded archives
are cached under data/rail/global/zips/, so re-running only fetches missing
or failed countries.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

import httpx
import ijson

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'data' / 'rail' / 'catalog.sqlite3'
DATASETS = ROOT / 'data' / 'rail' / 'global' / 'datasets.json'
ZIPDIR = ROOT / 'data' / 'rail' / 'global' / 'zips'
# 2° index buckets: one bucket holds at most ~tens of thousands of ways, so a
# lazily built STRtree stays sub-second while a query only opens 1-4 buckets.
BUCKET_DEG = 2
MAX_BUCKETS_PER_WAY = 64
# 与国内目录同口径：不含 tram（贴街轨道会把步行点误判成铁路）。
KEEP_RAILWAY = {'rail', 'narrow_gauge', 'light_rail', 'subway', 'monorail'}
# 国内 ways 表已由 download_rail_catalog.py 覆盖（且快照更新），跳过防冗余。
SKIP_ISO = {'chn'}

BUCKET_TABLE_SQL = (
    'CREATE TABLE IF NOT EXISTS rail_way_buckets('
    'bucket TEXT NOT NULL, way_id INTEGER NOT NULL, geometry TEXT NOT NULL, '
    'PRIMARY KEY(bucket, way_id)) WITHOUT ROWID')


def bucket_labels(min_lon, min_lat, max_lon, max_lat):
    """All 2° buckets whose rectangle intersects the way bbox (row-major)."""
    west = int(math.floor(min_lon / BUCKET_DEG)) * BUCKET_DEG
    east = int(math.floor(max_lon / BUCKET_DEG)) * BUCKET_DEG
    south = int(math.floor(min_lat / BUCKET_DEG)) * BUCKET_DEG
    north = int(math.floor(max_lat / BUCKET_DEG)) * BUCKET_DEG
    labels = [f'{lat},{lon}'
              for lat in range(south, north + 1, BUCKET_DEG)
              for lon in range(west, east + 1, BUCKET_DEG)]
    return labels[:MAX_BUCKETS_PER_WAY]


def feature_rows(payload, existing_ids):
    """Yield (bucket, way_id, geometry_json) rows for one GeoJSON feature."""
    props = payload.get('properties') or {}
    if props.get('railway') not in KEEP_RAILWAY:
        return
    geometry = payload.get('geometry') or {}
    kind = geometry.get('type')
    if kind == 'LineString':
        lines = [geometry.get('coordinates') or []]
    elif kind == 'MultiLineString':
        lines = geometry.get('coordinates') or []
    else:
        return
    way_id = props.get('osm_id')
    if isinstance(way_id, str):
        try:
            way_id = int(way_id)
        except ValueError:
            way_id = None
    if not isinstance(way_id, int) or way_id in existing_ids:
        return
    if kind == 'LineString':
        coords = []
        for point in lines[0]:
            try:
                coords.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError, IndexError):
                return
        if len(coords) < 2:
            return
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        geometry_json = json.dumps(coords, separators=(',', ':'))
    else:
        segments = []
        for line in lines:
            segment = []
            for point in line:
                try:
                    segment.append((float(point[0]), float(point[1])))
                except (TypeError, ValueError, IndexError):
                    break
            if len(segment) >= 2:
                segments.append(segment)
        if not segments:
            return
        flat = [c for segment in segments for c in segment]
        lons = [c[0] for c in flat]
        lats = [c[1] for c in flat]
        geometry_json = json.dumps(segments, separators=(',', ':'))
    for bucket in bucket_labels(min(lons), min(lats), max(lons), max(lats)):
        yield bucket, way_id, geometry_json


def download(client, iso, url):
    """Fetch the country archive once; later runs reuse the cached zip."""
    target = ZIPDIR / f'{iso}.zip'
    if target.exists() and target.stat().st_size > 0:
        return target, False
    part = target.with_suffix('.zip.part')
    with client.stream('GET', url, timeout=httpx.Timeout(120, connect=30)) as response:
        response.raise_for_status()
        with part.open('wb') as output:
            for chunk in response.iter_bytes(1 << 20):
                output.write(chunk)
    part.replace(target)
    return target, True


def import_zip(db, existing_ids, path):
    seen_ways = 0
    inserted = 0
    batch = []
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.endswith('.geojson')]
        if not members:
            raise ValueError('archive carries no .geojson member')
        with archive.open(members[0]) as stream:
            for payload in ijson.items(stream, 'features.item'):
                rows = list(feature_rows(payload, existing_ids))
                if rows:
                    seen_ways += 1
                    batch.extend(rows)
                if len(batch) >= 20000:
                    inserted += flush(db, batch)
                    batch = []
    inserted += flush(db, batch)
    return seen_ways, inserted


def flush(db, batch):
    if not batch:
        return 0
    before = db.total_changes
    db.executemany('INSERT OR IGNORE INTO rail_way_buckets VALUES(?,?,?)', batch)
    return db.total_changes - before


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('iso', nargs='*', help='subset of ISO3 codes (default: all)')
    args = parser.parse_args()
    if not DATASETS.exists():
        seed = ROOT / 'resources' / 'data-sources' / 'rail-datasets.json'
        DATASETS.parent.mkdir(parents=True, exist_ok=True)
        DATASETS.write_text(seed.read_text())
    if not CATALOG.exists():
        raise SystemExit('本地目录不存在，请先运行 scripts/download_rail_catalog.py')
    wanted = {code.lower() for code in args.iso}
    entries = []
    for entry in json.loads(DATASETS.read_text()):
        iso = entry['name'].split('_')[1]
        if iso in SKIP_ISO or wanted and iso not in wanted:
            continue
        if entry.get('geojson'):
            entries.append((iso, entry['geojson']))
    ZIPDIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(CATALOG, timeout=120)
    started = time.time()
    try:
        db.execute(BUCKET_TABLE_SQL)
        db.commit()
        existing_ids = {row[0] for row in db.execute('SELECT id FROM ways')}
        client = httpx.Client(follow_redirects=True)
        failures = []
        for index, (iso, url) in enumerate(entries, 1):
            try:
                path, fetched = download(client, iso, url)
                ways, inserted = import_zip(db, existing_ids, path)
                db.commit()
                state = 'downloaded' if fetched else 'cached'
                print(f'[{index}/{len(entries)}] {iso}: {ways} ways, +{inserted} bucket rows'
                      f' ({state}) [{time.time() - started:.0f}s]', flush=True)
            except Exception as exc:  # noqa: BLE001 - keep importing other countries
                db.rollback()
                failures.append(f'{iso}: {type(exc).__name__}: {exc}')
                print(f'[{index}/{len(entries)}] {iso}: FAILED {exc}', flush=True)
        buckets, rows, ways = db.execute(
            'SELECT COUNT(DISTINCT bucket), COUNT(*), COUNT(DISTINCT way_id) '
            'FROM rail_way_buckets').fetchone()
        print(f'rail_way_buckets: {ways} ways in {buckets} buckets ({rows} rows)'
              f' [{time.time() - started:.0f}s total]', flush=True)
        if failures:
            print(f'{len(failures)} datasets failed; re-run the script to retry:')
            for line in failures:
                print(f'  {line}', flush=True)
            sys.exit(1)
    finally:
        db.close()


if __name__ == '__main__':
    main()
