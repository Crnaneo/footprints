"""One-time cached national railway catalog from an Overpass response.
No user locations are included in the download query.
"""
import gzip, json, sqlite3, sys, re
from pathlib import Path
import ijson
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.rail_relations import railway_members
source=Path(sys.argv[1] if len(sys.argv)>1 else 'data/rail/china-osm.json')
target=source.parent/'catalog.sqlite3'
relations=[]; stations=[]; allowed=set(); raw_relations={}
with (gzip.open(source,'rb') if source.suffix=='.gz' else source.open('rb')) as f:
 for e in ijson.items(f,'elements.item',use_float=True):
  tags=e.get('tags',{})
  if e['type']=='relation':
   raw_relations[e['id']]=e
  elif e['type']=='node' and tags.get('railway') in {'station','halt','junction','signal_box'} and 18<=e['lat']<=54 and 73<=e['lon']<=136:
   name=tags.get('name:zh-Hans') or tags.get('name:zh') or tags.get('name')
   if name and tags.get('station') not in ('subway','light_rail'):
    stations.append((e['id'],name,e['lon'],e['lat'],json.dumps(tags,ensure_ascii=False)))
for rid,e in raw_relations.items():
 tags=e.get('tags',{})
 name=tags.get('name:zh-Hans') or tags.get('name:zh') or tags.get('name','')
 if not re.search('[\u4e00-\u9fff]',name) or any(x in tags.get('operator','') for x in ('JR','日本','九州','東日本','西日本','俄罗斯','Росс')):continue
 if not ('中国' in str(tags) or re.search('(铁路|高铁|客专|城际|线)$',name)):continue
 ways,nodes,missing=railway_members(rid,raw_relations)
 if missing:print('missing relation parts',rid,missing,flush=True)
 if not ways:continue
 relations.append((rid,name,json.dumps(tags,ensure_ascii=False),json.dumps(ways),json.dumps(nodes)))
 allowed.update(ways)
print('relations',len(relations),'candidate ways',len(allowed),'stations',len(stations),flush=True)
tmp=target.with_suffix('.building')
tmp.unlink(missing_ok=True)
with sqlite3.connect(tmp) as db:
 db.execute('CREATE TABLE lines(id INTEGER PRIMARY KEY,name TEXT,tags TEXT,ways TEXT,stations TEXT)')
 db.execute('CREATE TABLE ways(id INTEGER PRIMARY KEY,nodes TEXT,geometry TEXT,tags TEXT)')
 db.execute('CREATE TABLE stations(id INTEGER PRIMARY KEY,name TEXT,lon REAL,lat REAL,tags TEXT)')
 db.executemany('INSERT INTO lines VALUES (?,?,?,?,?)',relations)
 db.executemany('INSERT OR REPLACE INTO stations VALUES (?,?,?,?,?)',stations)
 count=0; pending=[]
 with (gzip.open(source,'rb') if source.suffix=='.gz' else source.open('rb')) as f:
  for e in ijson.items(f,'elements.item',use_float=True):
   if e['type']!='way' or e['id'] not in allowed or len(e.get('geometry',[]))<2: continue
   tags=e.get('tags',{})
   if tags.get('railway')!='rail' or tags.get('service') in ('siding','yard','spur') or tags.get('usage') in ('industrial','military'): continue
   coords=[[p['lon'],p['lat']] for p in e['geometry']]
   pending.append((e['id'],json.dumps(e['nodes']),json.dumps(coords),json.dumps(tags,ensure_ascii=False))); count+=1
   if len(pending)>=2000: db.executemany('INSERT OR IGNORE INTO ways VALUES (?,?,?,?)',pending);pending=[]
  db.executemany('INSERT OR IGNORE INTO ways VALUES (?,?,?,?)',pending)
 db.commit()
 print('usable ways',count,flush=True)
tmp.replace(target)
print('saved',target,target.stat().st_size,flush=True)
