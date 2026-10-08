"""Fetch public railway geometry for a fixed national bbox, cache locally."""
from pathlib import Path
import gzip,json,subprocess,sys
import httpx
ROOT=Path(__file__).resolve().parents[1]
directory=ROOT/'data'/'rail';directory.mkdir(parents=True,exist_ok=True)
archive=directory/'china-osm.json.gz'
# Public passenger names/telecodes; no personal itinerary is transmitted.
station_url='https://kyfw.12306.cn/otn/resources/js/framework/station_name.js'
response=httpx.get(station_url,timeout=30,follow_redirects=True);response.raise_for_status()
rows=[r.split('|') for r in response.text.split('@')[1:]]
stations=[{'name':r[1],'telecode':r[2]} for r in rows if len(r)>=3]
if len(stations)<2000:raise ValueError('12306 passenger directory response is incomplete')
station_tmp=directory/'passenger-stations.tmp'
station_tmp.write_text(json.dumps({'source_url':station_url,'stations':stations},ensure_ascii=False))
station_tmp.replace(directory/'passenger-stations.json')
query='[out:json][timeout:180];relation["type"="route"]["route"="railway"](18,73,54,136);out body;>>;out geom;node["railway"="station"](18,73,54,136);out body;'
if not archive.exists():
 temp=archive.with_suffix('.downloading')
 try:
  with httpx.stream('POST','https://overpass-api.de/api/interpreter',data={'data':query},timeout=600) as response:
   response.raise_for_status()
   with gzip.open(temp,'wb',compresslevel=5) as output:
    for chunk in response.iter_bytes():output.write(chunk)
  temp.replace(archive)
 except BaseException:
  temp.unlink(missing_ok=True);raise
subprocess.run([sys.executable,str(ROOT/'scripts'/'build_rail_catalog.py'),str(archive)],cwd=ROOT,check=True)
subprocess.run([sys.executable,str(ROOT/'scripts'/'fetch_rail_control_points.py')],cwd=ROOT,check=True)
print('Railway catalog ready. Open the app and select railway footprints to rebuild.')
