"""All original points in local MVT tiles; no sampling, clustering or line geometry.

Point-only encoder follows the public MVT 2.1 protobuf schema. Point metadata
is read separately by stable local ID to keep million-point tiles compact.
"""
from __future__ import annotations
import gzip
import hashlib
import json
import math
import threading
from copy import deepcopy
from collections import OrderedDict

EXTENT=4096
COLORS={'flight':'#61dce5','rail':'#f3bf69','other':'#a7b6cd'}
VIEWS=OrderedDict()
VIEW_LOCK=threading.Lock()
TILE_CACHE=OrderedDict()
TILE_LOCK=threading.Lock()
TILE_CACHE_BYTES=0
TILE_CACHE_LIMIT=48*1024*1024
VIEW_RESULTS=OrderedDict()
FINGERPRINTS=OrderedDict()
TILE_REVISIONS={}
BUILD_LOCKS=[threading.Lock() for _ in range(32)]


def world_coordinate(lon,lat):
    latitude=max(-85.05112878,min(85.05112878,lat))
    return (lon+180)/360,(1-math.asinh(math.tan(math.radians(latitude)))/math.pi)/2


def create_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS map_points(id INTEGER PRIMARY KEY, trip_id TEXT REFERENCES trips(id) ON DELETE CASCADE, ordinal INTEGER, longitude REAL, latitude REAL, x REAL, y REAL, mode TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS point_classification(trip_id TEXT PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE, version INTEGER NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS map_points_trip ON map_points(trip_id)')
    db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS map_point_index USING rtree(id,minx,maxx,miny,maxy)')
    db.execute('CREATE TABLE IF NOT EXISTS map_indexed(trip_id TEXT PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE)')
    db.execute('CREATE TABLE IF NOT EXISTS map_bounds(trip_id TEXT PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE, point_count INTEGER, west REAL, east REAL, south REAL, north REAL, shifted_west REAL, shifted_east REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS map_revision(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO map_revision VALUES(1,0)')
    db.execute('CREATE TRIGGER IF NOT EXISTS map_revision_insert AFTER INSERT ON map_indexed BEGIN UPDATE map_revision SET revision=revision+1 WHERE id=1; END')
    db.execute('CREATE TRIGGER IF NOT EXISTS map_revision_delete AFTER DELETE ON map_indexed BEGIN UPDATE map_revision SET revision=revision+1 WHERE id=1; END')
    db.execute('CREATE TRIGGER IF NOT EXISTS map_point_insert AFTER INSERT ON map_points BEGIN INSERT INTO map_point_index VALUES(new.id,new.x,new.x,new.y,new.y); END')
    db.execute('CREATE TRIGGER IF NOT EXISTS map_point_delete AFTER DELETE ON map_points BEGIN DELETE FROM map_point_index WHERE id=old.id; END')
    if 'mode' not in {row[1] for row in db.execute('PRAGMA table_info(map_points)')}:
        # One-time migration: existing points keep their trip colour until the
        # point-mode backfill refines CSV records.
        db.execute('ALTER TABLE map_points ADD COLUMN mode TEXT')
        db.execute('UPDATE map_points SET mode=(SELECT t.mode FROM trips t WHERE t.id=map_points.trip_id) WHERE mode IS NULL')


def index_record(db,record):
    # Imported here: point_modes resolves colours from this module's constants.
    from .point_modes import catalog_path_for_db, effective_modes
    db.execute('DELETE FROM map_points WHERE trip_id=?',(record['id'],))
    modes=effective_modes(record,catalog_path_for_db(db))
    rows=[]
    for ordinal,p in enumerate(record['points']):
        lon,lat=p['longitude'],p['latitude'];x,y=world_coordinate(lon,lat)
        rows.append((record['id'],ordinal,lon,lat,x,y,modes[ordinal]))
    db.executemany('INSERT INTO map_points(trip_id,ordinal,longitude,latitude,x,y,mode) VALUES(?,?,?,?,?,?,?)',rows)
    # Aggregate once on import, never scan all original coordinates on map load.
    bounds_for_record(db,record['id'])
    db.execute('INSERT OR REPLACE INTO map_indexed VALUES(?)',(record['id'],))


def bounds_for_record(db,trip_id):
    db.execute('INSERT OR REPLACE INTO map_bounds SELECT ?,count(*),min(longitude),max(longitude),min(latitude),max(latitude),min(CASE WHEN longitude<0 THEN longitude+360 ELSE longitude END),max(CASE WHEN longitude<0 THEN longitude+360 ELSE longitude END) FROM map_points WHERE trip_id=?',(trip_id,trip_id))


def migrate_index(db):
    missing=db.execute('SELECT id FROM trips WHERE id NOT IN (SELECT trip_id FROM map_indexed)').fetchall()
    for row in missing:
        record=json.loads(db.execute('SELECT payload FROM trips WHERE id=?',(row[0],)).fetchone()[0])
        index_record(db,record)
    for row in db.execute('SELECT trip_id FROM map_indexed WHERE trip_id NOT IN (SELECT trip_id FROM map_bounds)').fetchall():
        bounds_for_record(db,row[0])


def register_view(store,trip_ids):
    ids=sorted(set(trip_ids));id_set=set(ids);path=str(store.path.resolve())
    from .flight_overlay import fingerprint, ONLINE_SOURCES
    with store.connect() as db:
        db.execute('BEGIN')  # Keep revision and point rows in the same WAL snapshot.
        revision=db.execute('SELECT revision FROM map_revision WHERE id=1').fetchone()[0]
        key=hashlib.sha256((path+'|'+str(revision)+'|'+ '|'.join(ids)).encode()).hexdigest()[:24]
        with VIEW_LOCK:
            if key in VIEW_RESULTS and key in VIEWS:
                VIEW_RESULTS.move_to_end(key);VIEWS.move_to_end(key)
                return deepcopy(VIEW_RESULTS[key])
        summaries={r['id']:r for r in db.execute('SELECT t.id,b.* FROM trips t JOIN map_bounds b ON b.trip_id=t.id')}
        records={r['id']:json.loads(r['summary']) for r in db.execute('SELECT id,summary FROM trips') if r['id'] in id_set}
        overlays, related, validated = {}, {}, {}
        for trip_id,record in records.items():
            if record.get('source') not in ONLINE_SOURCES:continue
            for reference in record.get('metadata',{}).get('gps_overlay',{}).get('sources',[]):
                source_id=reference.get('trip_id')
                if source_id not in summaries:continue
                if source_id not in validated:
                    cache_key=(path,revision,source_id)
                    with VIEW_LOCK:
                        cached=FINGERPRINTS.get(cache_key)
                    if cached is None:
                        raw=db.execute('SELECT payload FROM trips WHERE id=?',(source_id,)).fetchone()
                        points=json.loads(raw[0])['points']
                        cached=(fingerprint(points),len(points))
                        del points
                        with VIEW_LOCK:
                            FINGERPRINTS[cache_key]=cached
                            while len(FINGERPRINTS)>64:FINGERPRINTS.popitem(last=False)
                    validated[source_id]=cached
                digest,count=validated[source_id]
                if digest!=reference.get('fingerprint'):continue
                intervals=overlays.setdefault(source_id,[])
                for bounds in reference.get('ranges',[]):
                    if (isinstance(bounds,list) and len(bounds)==2 and all(type(v) is int for v in bounds)
                            and 0<=bounds[0]<=bounds[1]<count):
                        intervals.append(bounds)
                related.setdefault(trip_id,[]).append(source_id)
        overlays={source_id:merge_ranges(value) for source_id,value in overlays.items()}
        ranges={source_id:value for source_id,value in overlays.items() if source_id not in id_set}
        extras=[]
        for source_id,selection in ranges.items():
            condition=' OR '.join('(ordinal BETWEEN ? AND ?)' for _ in selection)
            if not condition:continue
            args=[source_id]+[v for bounds in selection for v in bounds]
            extras.append(db.execute('SELECT count(*) point_count,min(longitude) west,max(longitude) east,min(latitude) south,max(latitude) north,min(CASE WHEN longitude<0 THEN longitude+360 ELSE longitude END) shifted_west,max(CASE WHEN longitude<0 THEN longitude+360 ELSE longitude END) shifted_east FROM map_points WHERE trip_id=? AND ('+condition+')',args).fetchone())
    if any(t not in summaries for t in ids):raise ValueError('地图包含不存在的记录，请刷新列表')
    selected=[summaries[t] for t in ids]+extras;bounds=None
    nonempty=[r for r in selected if r['point_count']]
    if nonempty:
        west=min(r['west'] for r in nonempty);east=max(r['east'] for r in nonempty)
        shifted_west=min(r['shifted_west'] for r in nonempty);shifted_east=max(r['shifted_east'] for r in nonempty)
        if shifted_east-shifted_west<east-west:west,east=shifted_west,shifted_east
        bounds=[[west,max(-85.05112878,min(r['south'] for r in nonempty))],[east,min(85.05112878,max(r['north'] for r in nonempty))]]
    result={'id':key,'point_count':sum(r['point_count'] for r in selected),'trip_count':len(ids),'bounds':bounds,
            'gps_point_count':sum(end-start+1 for ranges in overlays.values() for start,end in ranges),
            'online_point_count':sum(r.get('point_count',0) for r in records.values() if r.get('source') in ONLINE_SOURCES),
            'union_only':bool(related) and all(r.get('source') in ONLINE_SOURCES for r in records.values()),
            'related_trip_ids':related}
    with VIEW_LOCK:
        VIEWS[key]=(path,tuple(ids),revision,len(ids)==len(summaries),ranges)
        VIEW_RESULTS[key]=deepcopy(result)
        VIEWS.move_to_end(key)
        while len(VIEWS)>256:
            expired,_=VIEWS.popitem(last=False);VIEW_RESULTS.pop(expired,None)
    return result


def merge_ranges(ranges):
    """Union inclusive ordinal intervals without allocating one entry per point."""
    merged=[]
    for start,end in sorted(ranges):
        if merged and start<=merged[-1][1]+1:
            merged[-1][1]=max(merged[-1][1],end)
        else:merged.append([start,end])
    return merged


def view_revision(store,key,z,x,y):
    if not 0<=z<=19 or not 0<=x<2**z or not 0<=y<2**z:raise ValueError('无效瓦片坐标')
    with VIEW_LOCK:context=VIEWS.get(key)
    if context is None or context[0]!=str(store.path.resolve()):raise KeyError('地图视图已过期，请刷新')
    with store.connect() as db:
        return db.execute('SELECT revision FROM map_revision WHERE id=1').fetchone()[0]


def varint(n):
    data=bytearray()
    while n>127:data.append((n&127)|128);n>>=7
    data.append(n);return bytes(data)


def blob(field,data):return varint((field<<3)|2)+varint(len(data))+data

def integer(field,value):return varint(field<<3)+varint(value)


def encode_points(rows,z,x,y):
    """One feature per original row; write protobuf directly without tiny blobs.

    Rows carry (id, trip_id, x, y, mode) so a mixed CSV trip can colour its
    rail/flight points independently of the trip-level mode."""
    scale=EXTENT*(2**z);offset_x=x*EXTENT;offset_y=y*EXTENT
    values=[];lookup={};features=bytearray();count=0
    def value(s):
        if s not in lookup:lookup[s]=len(values);values.append(s)
        return lookup[s]
    tags={}
    def tags_for(tid,mode):
        key=(tid,mode)
        encoded=tags.get(key)
        if encoded is None:
            encoded=blob(2,b'\x00'+varint(value(tid))+b'\x01'+varint(value(COLORS[mode])))+b'\x18\x01'
            tags[key]=encoded
        return encoded
    append=features.append;extend=features.extend
    for pid,tid,wx,wy,mode in rows:
        px=round(wx*scale-offset_x);py=round(wy*scale-offset_y)
        zx=px*2 if px>=0 else -px*2-1;zy=py*2 if py>=0 else -py*2-1
        # MVT point feature: id, packed tags, Point type, MoveTo coordinates.
        id_size=(pid.bit_length()+6)//7 or 1
        geometry_size=1+((zx.bit_length()+6)//7 or 1)+((zy.bit_length()+6)//7 or 1)
        feature_tags=tags_for(tid,mode if mode in COLORS else 'other')
        length=1+id_size+len(feature_tags)+2+geometry_size
        append(18)
        while length>127:append((length&127)|128);length>>=7
        append(length);append(8)
        while pid>127:append((pid&127)|128);pid>>=7
        append(pid);extend(feature_tags);append(34);append(geometry_size);append(9)
        while zx>127:append((zx&127)|128);zx>>=7
        append(zx)
        while zy>127:append((zy&127)|128);zy>>=7
        append(zy);count+=1
    layer=bytearray(integer(15,2)+blob(1,b'points'))
    layer.extend(features);layer.extend(blob(3,b'tripId')+blob(3,b'color'))
    for s in values:layer.extend(blob(4,blob(1,s.encode())))
    layer.extend(integer(5,EXTENT))
    return blob(3,bytes(layer)),count


def tile_entry(store,key,z,x,y):
    """Bounded cache shared by concurrent requests, invalidated by data revision."""
    if not 0<=z<=19 or not 0<=x<2**z or not 0<=y<2**z:raise ValueError('无效瓦片坐标')
    path=str(store.path.resolve())
    with VIEW_LOCK:context=VIEWS.get(key)
    if context is None or context[0]!=path:raise KeyError('地图视图已过期，请刷新')
    with store.connect() as db:
        db.execute('BEGIN')  # Keep revision and point rows in the same WAL snapshot.
        revision=db.execute('SELECT revision FROM map_revision WHERE id=1').fetchone()[0]
        global TILE_CACHE_BYTES
        with TILE_LOCK:
            if TILE_REVISIONS.get(path)!=revision:
                # Old views remain valid, but old encoded bytes can never be
                # served at the new revision. Release them immediately.
                for old_key in list(TILE_CACHE):
                    if old_key[0]==path and old_key[1]!=revision:
                        old=TILE_CACHE.pop(old_key);TILE_CACHE_BYTES-=len(old[0])+len(old[2])
                TILE_REVISIONS[path]=revision
        cache_key=(path,revision,key,z,x,y)
        # Coalesce duplicate requests for the same tile without a global build lock.
        with BUILD_LOCKS[hash(cache_key)%len(BUILD_LOCKS)]:
            with TILE_LOCK:
                cached=TILE_CACHE.get(cache_key)
                if cached is not None:TILE_CACHE.move_to_end(cache_key);return cached
            trip_ids=context[1];scale=2**z;margin=16/512/scale
            lo,hi=x/scale-margin,(x+1)/scale+margin;top,bottom=y/scale-margin,(y+1)/scale+margin
            if not trip_ids:rows=[]
            else:
                # At world zoom a sequential table scan beats visiting every RTree node.
                if z==0:
                    sql='SELECT p.id,p.trip_id,p.x,p.y,p.mode FROM map_points p WHERE p.x>=? AND p.x<=? AND p.y>=? AND p.y<=?'
                    args=[lo,hi,top,bottom]
                else:
                    sql='SELECT p.id,p.trip_id,p.x,p.y,p.mode FROM map_point_index r CROSS JOIN map_points p ON p.id=r.id WHERE r.minx<=? AND r.maxx>=? AND r.miny<=? AND r.maxy>=? AND p.x>=? AND p.x<=? AND p.y>=? AND p.y<=?'
                    args=[hi,lo,bottom,top,lo,hi,top,bottom]
                if not(context[3] and context[2]==revision):
                    filters=['p.trip_id IN ('+','.join('?' for _ in trip_ids)+')'];args.extend(trip_ids)
                    for source_id,ranges in context[4].items():
                        if not ranges:continue
                        filters.append('(p.trip_id=? AND ('+' OR '.join('p.ordinal BETWEEN ? AND ?' for _ in ranges)+'))')
                        args.append(source_id);args.extend(v for bounds in ranges for v in bounds)
                    sql+=' AND ('+' OR '.join(filters)+')'
                rows=db.execute(sql,args)
            data,count=encode_points(rows,z,x,y)
            compressed=gzip.compress(data,compresslevel=1,mtime=0)
            entry=(data,count,compressed,revision)
            size=len(data)+len(compressed)
            with TILE_LOCK:
                if size<=TILE_CACHE_LIMIT:
                    TILE_CACHE[cache_key]=entry;TILE_CACHE_BYTES+=size
                    while TILE_CACHE_BYTES>TILE_CACHE_LIMIT:
                        _,old=TILE_CACHE.popitem(last=False);TILE_CACHE_BYTES-=len(old[0])+len(old[2])
            return entry


def tile(store,key,z,x,y):
    data,count,_,_=tile_entry(store,key,z,x,y)
    return data,count


def point_detail(store,point_id):
    with store.connect() as db:
        row=db.execute('SELECT p.ordinal,t.payload FROM map_points p JOIN trips t ON t.id=p.trip_id WHERE p.id=?',(point_id,)).fetchone()
    if row is None:return None
    record=json.loads(row['payload'])
    return {'trip_id':record['id'],'title':record['title'],'source':record.get('source'),'point':record['points'][row['ordinal']]}
