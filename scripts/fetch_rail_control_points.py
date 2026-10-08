"""Cache missing public station/junction members of the national catalog.

Requests depend only on public catalog IDs, never on personal trajectories.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import httpx

ROOT=Path(__file__).resolve().parents[1]


def supplement(directory):
    catalog=directory/'catalog.sqlite3'
    cache=directory/'control-points.json'
    with sqlite3.connect(catalog) as db:
        known={r[0] for r in db.execute('SELECT id FROM stations')}
        members={n for row in db.execute('SELECT stations FROM lines') for n in json.loads(row[0])}
    saved=json.loads(cache.read_text())['elements'] if cache.exists() else []
    cached={e['id']:e for e in saved}
    missing=sorted(members-known-cached.keys())
    batches=[missing[i:i+200] for i in range(0,len(missing),200)]

    def fetch(nodes):
        response=httpx.get('https://api.openstreetmap.org/api/0.6/nodes.json',
            params={'nodes':','.join(map(str,nodes))},timeout=45)
        response.raise_for_status()
        return [{k:e[k] for k in ('type','id','lon','lat','tags') if k in e}
                for e in response.json()['elements']]

    with ThreadPoolExecutor(max_workers=3) as pool:
        for i,elements in enumerate(pool.map(fetch,batches),1):
            cached.update({e['id']:e for e in elements})
            print(f'Public catalog nodes: batch {i}/{len(batches)}',flush=True)
    temporary=cache.with_suffix('.tmp')
    temporary.write_text(json.dumps({'source':'OpenStreetMap API v0.6','elements':list(cached.values())},ensure_ascii=False))
    temporary.replace(cache)
    rows=[]
    for e in cached.values():
        tags=e.get('tags',{})
        name=tags.get('name:zh-Hans') or tags.get('name:zh') or tags.get('name')
        if not name or 'lon' not in e or not (18<=e['lat']<=54 and 73<=e['lon']<=136):continue
        if tags.get('railway') not in {'station','halt','junction','signal_box'} and not name.endswith('线路所'):continue
        if tags.get('station') in {'subway','light_rail','monorail'}:continue
        rows.append((e['id'],name,e['lon'],e['lat'],json.dumps(tags,ensure_ascii=False)))
    with sqlite3.connect(catalog) as db:
        db.executemany('INSERT OR REPLACE INTO stations VALUES (?,?,?,?,?)',rows)
    print(f'Supplemented {len(rows)} public railway station/control nodes',flush=True)


if __name__=='__main__':supplement(ROOT/'data'/'rail')
