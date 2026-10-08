from collections import defaultdict
from shapely.geometry import LineString
from backend.rail_reference_paths import reference_spines


def test_terminal_branch_remains_a_reference_path():
    graph=defaultdict(list);ways={}
    for wid,a,b,coords in [(1,'A','J',[(0,0),(10000,0)]),(2,'J','B',[(10000,0),(20000,0)]),(3,'J','C',[(10000,0),(10000,8000)])]:
        g=LineString(coords);ways[wid]={'nodes':(a,b),'geom':g}
        graph[a].append((b,wid,g.length));graph[b].append((a,wid,g.length))
    spines=reference_spines(graph,ways)
    assert len(spines)==2
    assert {tuple(p) for _,_,coords in spines for p in coords}>={(0,0),(20000,0),(10000,8000)}
    assert sum(length for length,_,_ in spines)==28000


def test_urban_only_station_is_not_a_national_rail_boundary(tmp_path,monkeypatch):
    import sqlite3,json
    import backend.rail_coverage as coverage
    path=tmp_path/'catalog.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE lines(id INTEGER,name TEXT,tags TEXT,ways TEXT,stations TEXT)')
        db.execute('CREATE TABLE ways(id INTEGER,nodes TEXT,geometry TEXT,tags TEXT)')
        db.execute('CREATE TABLE stations(id INTEGER,name TEXT,lon REAL,lat REAL,tags TEXT)')
        for i,name,tags in [(1,'机场单轨站',{'monorail':'yes'}),(2,'地铁站',{'subway':'yes'}),(3,'国铁枢纽',{'subway':'yes','train':'yes'})]:
            db.execute('INSERT INTO stations VALUES(?,?,?,?,?)',(i,name,116+i*.01,39,json.dumps(tags)))
    monkeypatch.setattr(coverage,'passenger_directory',lambda p:{})
    index=coverage.RailIndex(path)
    assert index.station_names==['国铁枢纽']
