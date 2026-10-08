"""Extend the local rail catalog with overseas OSM track geometry.

The national download only carries members of route relations that touch the
China bbox, so trips abroad stay unclassified. This script pulls
way["railway"]=... geometry for given bboxes straight from Overpass and merges
them into data/rail/catalog.sqlite3 (idempotent INSERT OR IGNORE on OSM way
ids). It then clears the point-mode classification markers so the backend
warmup reclassifies every stored trip against the enlarged catalog.

Usage:
  .venv/bin/python scripts/extend_rail_catalog.py            # all built-in regions
  .venv/bin/python scripts/extend_rail_catalog.py 日本关东 新西兰  # subset by name
  .venv/bin/python scripts/extend_rail_catalog.py --bbox s w n e [--bbox ...] # custom boxes
"""
from pathlib import Path
import json
import sqlite3
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'data' / 'rail' / 'catalog.sqlite3'
TRIPS = ROOT / 'data' / 'footprint.sqlite3'
# 与国内目录同口径：干线/窄轨/轻轨/地铁/单轨，不含 tram（贴街轨道会把步行点误判成铁路）。
# (south, west, north, east)
REGIONS = {
    '日本关东': (34.9, 138.5, 36.5, 141.2),
    '日本东海-关西': (33.3, 134.8, 35.8, 137.8),
    '澳大利亚东南-维州': (-44.0, 139.5, -34.5, 146.5),
    '澳大利亚东南-新州昆州': (-38.5, 146.0, -27.5, 153.9),
    '新西兰': (-47.3, 166.0, -34.2, 178.9),
    '阿联酋': (22.6, 51.0, 26.6, 56.7),
    '新加坡': (0.8, 103.3, 2.0, 104.5),
}
QUERY = ('[out:json][timeout:600];way["railway"~"^(rail|narrow_gauge|light_rail|subway|monorail)$"]'
         '({:.4f},{:.4f},{:.4f},{:.4f});out geom;')
# overpass-api.de 在部分网络出口被前置防火墙拦截（HTTP 406），kumi 对稍大查询网关超时，
# mail.ru 镜像实测稳定且数据更新，作为首选回退链。
ENDPOINTS = ('https://maps.mail.ru/osm/tools/overpass/api/interpreter',
             'https://overpass.kumi.systems/api/interpreter',
             'https://overpass.osm.ch/api/interpreter')


def fetch(bbox):
    last_error = None
    for attempt in (1, 2, 3):
        for endpoint in ENDPOINTS:
            try:
                with httpx.stream('POST', endpoint,
                                  data={'data': QUERY.format(*bbox)}, timeout=900) as response:
                    response.raise_for_status()
                    return json.loads(response.read())
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                print(f'  {endpoint} 请求失败（{exc}）', flush=True)
        if attempt < 3:
            print(f'  {attempt * 30} 秒后重试', flush=True)
            time.sleep(attempt * 30)
    raise RuntimeError(f'Overpass 请求连续失败：{last_error}')


def merge_rows(payload):
    rows = []
    for element in payload.get('elements', []):
        if element.get('type') != 'way':
            continue
        geometry = [(point['lon'], point['lat']) for point in element.get('geometry') or []]
        if len(geometry) < 2:
            continue
        rows.append((element['id'], '[]',
                     json.dumps(geometry, separators=(',', ':')),
                     json.dumps(element.get('tags') or {}, ensure_ascii=False, separators=(',', ':'))))
    return rows


def main():
    if not CATALOG.exists():
        raise SystemExit('本地目录不存在，请先运行 scripts/download_rail_catalog.py')
    args = sys.argv[1:]
    boxes = []
    if '--bbox' in args:
        numbers = [float(value) for value in args[args.index('--bbox') + 1:]]
        boxes = [(numbers[i], numbers[i + 1], numbers[i + 2], numbers[i + 3])
                 for i in range(0, len(numbers) - 3, 4)]
    else:
        wanted = args or list(REGIONS)
        unknown = [name for name in wanted if name not in REGIONS]
        if unknown:
            raise SystemExit(f'未知区域 {unknown}；可用区域：{list(REGIONS)}')
        boxes = [REGIONS[name] for name in wanted]
    db = sqlite3.connect(CATALOG, timeout=60)
    try:
        for index, (name, bbox) in enumerate(zip((args or list(REGIONS)), boxes) if '--bbox' not in args
                                             else zip(['自定义'] * len(boxes), boxes), 1):
            print(f'[{index}/{len(boxes)}] {name} {bbox}：请求 Overpass…', flush=True)
            rows = merge_rows(fetch(bbox))
            with db:
                before = db.total_changes
                db.executemany('INSERT OR IGNORE INTO ways(id,nodes,geometry,tags) VALUES(?,?,?,?)', rows)
                added = db.total_changes - before
            print(f'  获取 {len(rows)} 条 way，合并新增 {added} 条', flush=True)
    finally:
        db.close()
    if TRIPS.exists():
        with sqlite3.connect(TRIPS, timeout=60) as trips:
            cleared = trips.execute('DELETE FROM point_classification').rowcount
        print(f'已清除 {cleared} 条旅程的逐点分类标记；重启服务后预热线程会按扩大后的目录重新判定。', flush=True)
    else:
        print('未找到足迹数据库，跳过分类标记清理。', flush=True)


if __name__ == '__main__':
    main()
