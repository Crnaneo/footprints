"""Fetch DE/US/RU railway segments from the Overture Maps release.

HDX carries HOT OSM railway exports for 232 countries but not for Germany,
the United States or Russia. Those three come from the Overture Maps
Foundation transportation theme instead: the same OpenStreetMap alignments,
distributed as GeoParquet whose per-row bbox allows row-group level pruning
(row-group bboxes are ~1° wide, so a country bbox only pulls the row groups
it actually intersects, ~3MB of geometry column each).

Segments are filtered to subtype=rail with the same taxonomy as the rest of
the catalog (standard/narrow gauge, light rail, subway, monorail, plus the
unclassified gauge bucket; tram and funicular stay out). GERS ids are not
OSM way ids, so each segment gets a stable 62-bit hash id that cannot
collide with OSM ids in ways. Results merge into the same rail_way_buckets
table that backend/point_modes.py reads, so the overseas rule set stays
identical everywhere on the planet.

Usage:
  .venv/bin/python scripts/fetch_overture_rail.py          # deu, usa, rus
  .venv/bin/python scripts/fetch_overture_rail.py deu      # single country

The run is idempotent (INSERT OR IGNORE) and resumable via
data/rail/global/overture-progress.json; re-running skips file/country
pairs that already completed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pyarrow.parquet as pq
import shapely
from pyarrow.fs import S3FileSystem

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from build_global_rail import BUCKET_TABLE_SQL, flush  # noqa: E402 - shared bucket helpers  # noqa: E402
from build_global_rail import bucket_labels  # noqa: E402

CATALOG = ROOT / 'data' / 'rail' / 'catalog.sqlite3'
FILES = ROOT / 'data' / 'rail' / 'global' / 'overture-files.txt'
PROGRESS = ROOT / 'data' / 'rail' / 'global' / 'overture-progress.json'
# 与国内/HDX 目录同口径：不含 tram（贴街轨道会把步行点误判成铁路）与 funicular。
# unknown = railway 轨道但轨距未标注（OSM 上大量干线如此），必须保留。
KEEP_OVERTURE = {'standard_gauge', 'narrow_gauge', 'light_rail', 'subway', 'monorail', 'unknown'}
COUNTRIES = {
    'deu': (5.87, 47.24, 15.06, 55.07),
    'usa': (-125.0, 24.4, -66.8, 49.5),
    'rus': (18.9, 41.1, 180.0, 82.0),
}


def hash_id(gers_id: str) -> int:
    """Stable 62-bit id for a GERS uuid; never collides with OSM way ids."""
    digest = hashlib.blake2b(gers_id.encode(), digest_size=8).digest()
    return int.from_bytes(digest, 'big') >> 2


def row_group_stats(metadata, index: int):
    """bbox + subtype min/max from footer stats; zero extra downloads."""
    group = metadata.row_group(index)
    stats = {}
    for column in range(group.num_columns):
        leaf = group.column(column)
        name = leaf.path_in_schema
        if name.startswith('bbox.') and leaf.statistics:
            stats[name.split('.')[1]] = leaf.statistics
        elif name == 'subtype' and leaf.statistics:
            stats['subtype'] = leaf.statistics
    if len(stats) != 5:
        return None
    return (stats['xmin'].min, stats['xmin'].max, stats['ymin'].min, stats['ymin'].max,
            stats['xmax'].min, stats['xmax'].max, stats['ymax'].min, stats['ymax'].max,
            stats['subtype'].min, stats['subtype'].max)


def row_group_usable(stats, bbox) -> bool:
    """Skip row groups outside the bbox or without any rail subtype.

    subtype sorts as rail < road < water, so an interval that excludes 'rail'
    cannot contain a rail row — checked straight from footer statistics.
    """
    if stats is None:
        # 行组缺统计时宁可多读（行级仅按类型过滤），不能静默漏掉。
        return True
    west, south, east, north = bbox
    (xmin_min, xmin_max, ymin_min, ymin_max,
     xmax_min, xmax_max, ymax_min, ymax_max, sub_min, sub_max) = stats
    if xmax_max < west or xmin_min > east or ymax_max < south or ymin_min > north:
        return False
    return sub_min <= 'rail' <= sub_max


def segment_rows(way_id: int, geom) -> list[tuple[str, int, str]]:
    """Bucket rows for one Overture segment (line or multi), HDX-compatible."""
    if geom.geom_type == 'LineString':
        lines = [[(round(coord[0], 7), round(coord[1], 7)) for coord in geom.coords]]
    elif geom.geom_type == 'MultiLineString':
        lines = [[(round(coord[0], 7), round(coord[1], 7)) for coord in part.coords]
                 for part in geom.geoms]
    else:
        return []
    lines = [line for line in lines if len(line) >= 2]
    if not lines:
        return []
    payload = json.dumps(lines[0] if len(lines) == 1 else lines, separators=(',', ':'))
    flat = [coord for line in lines for coord in line]
    lons = [coord[0] for coord in flat]
    lats = [coord[1] for coord in flat]
    return [(bucket, way_id, payload)
            for bucket in bucket_labels(min(lons), min(lats), max(lons), max(lats))]


def harvest(pf, bbox) -> tuple[int, list[tuple[str, int, str]]]:
    """Parse every rail segment inside bbox; rows are returned, not written."""
    seen_ways = 0
    rows: list[tuple[str, int, str]] = []
    metadata = pf.metadata
    for rg in range(metadata.num_row_groups):
        stats = row_group_stats(metadata, rg)
        if not row_group_usable(stats, bbox):
            continue
        # Two-phase read: the small columns decide whether the geometry
        # chunk (a few MB) is worth pulling at all — road-only row groups
        # outnumber rail ones a hundredfold.
        probe = pf.read_row_group(rg, columns=['subtype', 'class'])
        subtypes = probe.column('subtype').to_pylist()
        classes = probe.column('class').to_pylist()
        rail_positions = [position for position, subtype in enumerate(subtypes)
                          if subtype == 'rail' and classes[position] in KEEP_OVERTURE]
        if not rail_positions:
            continue
        table = pf.read_row_group(rg, columns=['id', 'geometry'])
        ids = table.column('id').to_pylist()
        geometries = table.column('geometry').to_pylist()
        for position in rail_positions:
            segment = segment_rows(hash_id(ids[position]), shapely.from_wkb(geometries[position]))
            if segment:
                seen_ways += 1
                rows.extend(segment)
    return seen_ways, rows


def process_file(key: str, countries: dict) -> dict:
    """Download/parse one parquet file for every pending country (no DB writes).

    TLS errors and slow-link timeouts are routine on long anonymous S3 reads,
    so each file gets three attempts with a widening backoff before giving up.
    """
    last: Exception | None = None
    for attempt in range(3):
        try:
            s3 = S3FileSystem(anonymous=True, region='us-west-2',
                              connect_timeout=30.0, request_timeout=90.0)
            result = {}
            parquet = pq.ParquetFile(s3.open_input_file(f'overturemaps-us-west-2/{key}'))
            try:
                for code, bbox in countries.items():
                    ways, rows = harvest(parquet, bbox)
                    result[code] = (ways, rows)
            finally:
                parquet.close()
            return result
        except Exception as exc:  # noqa: BLE001 - retry transient network failures
            last = exc
            time.sleep(10 * (attempt + 1))
    raise last


def main():
    wanted = {code.lower() for code in sys.argv[1:]} or set(COUNTRIES)
    countries = {code: bbox for code, bbox in COUNTRIES.items() if code in wanted}
    if not FILES.exists():
        seed = ROOT / 'resources' / 'data-sources' / 'overture-files.txt'
        FILES.parent.mkdir(parents=True, exist_ok=True)
        FILES.write_text(seed.read_text())
    if not CATALOG.exists():
        raise SystemExit('请先运行 scripts/download_rail_catalog.py 准备铁路目录')
    keys = FILES.read_text().split()
    progress = json.loads(PROGRESS.read_text()) if PROGRESS.exists() else {}
    db = sqlite3.connect(CATALOG, timeout=120)
    started = time.time()
    try:
        db.execute(BUCKET_TABLE_SQL)
        db.commit()
        # Downloads dominate: parallel files share the link while the main
        # thread alone writes the catalog and the progress file.
        queue = [key for key in keys
                 if any(key.rsplit('/', 1)[-1] not in progress.get(code, []) for code in countries)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(process_file, key, countries): key for key in queue}
            for done, future in enumerate(as_completed(futures), 1):
                key = futures[future]
                name = key.rsplit('/', 1)[-1]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - one file failing must not stop the rest
                    print(f'[{done}/{len(queue)}] {name[:16]}… FAILED {type(exc).__name__}: {exc}'
                          f' [{time.time() - started:.0f}s]', flush=True)
                    continue
                report = []
                for code, (ways, rows) in result.items():
                    inserted = flush(db, rows)
                    report.append(f'{code}: +{ways} ways/+{inserted} rows')
                    progress.setdefault(code, []).append(name)
                db.commit()
                PROGRESS.write_text(json.dumps(progress))
                print(f'[{done}/{len(queue)}] {name[:16]}… {" | ".join(report)}'
                      f' [{time.time() - started:.0f}s]', flush=True)
        buckets, rows, ways = db.execute(
            'SELECT COUNT(DISTINCT bucket), COUNT(*), COUNT(DISTINCT way_id) '
            'FROM rail_way_buckets').fetchone()
        print(f'rail_way_buckets now: {ways} ways in {buckets} buckets ({rows} rows)', flush=True)
    finally:
        db.close()


if __name__ == '__main__':
    main()
