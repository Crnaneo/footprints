import copy
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.flight_overlay import attach_gps_overlay, update_target, record_fingerprint
from backend.map_points import register_view, tile, point_detail
from backend.models import TripCreate
from backend.tests.test_flight_local import store, gps, DAY, START, AUTO_IMPORT
from backend.tests.test_map_points import decode


def online(original, source='Flightradar24'):
    points = [dict(p, latitude=p['latitude'] + .001, altitude_m=10000, speed_kmh=750)
              for p in original['points'][2:13]]
    return {'title':'AB123 online', 'mode':'flight','travel_date':DAY.isoformat(),
            'service_number':'AB123','origin':'AAA','destination':'BBB','source':source,
            'geometry_kind':'observed','points':points,'metadata':{'flight':{'callsign':'TST123'}}}


@pytest.mark.parametrize('source', ['Flightradar24', 'adsb.lol', 'OpenSky'])
def test_online_points_and_original_gps_are_preserved_separately(store, source):
    original=gps(store); before=copy.deepcopy(original)
    draft=online(original,source); fixes=copy.deepcopy(draft['points'])
    attach_gps_overlay(store,draft)
    overlay=draft['metadata']['gps_overlay']
    assert draft['points']==fixes
    assert overlay['point_count']==11
    assert overlay['sources'][0]['trip_id']==original['id']
    assert overlay['sources'][0]['ranges']==[[2,12]]
    assert overlay['covered_distance_km'][original['id']]>0
    assert store.get(original['id'])==before


def test_filtered_map_is_union_of_online_and_only_matched_gps(store,tmp_path):
    original=gps(store); draft=attach_gps_overlay(store,online(original))
    saved=store.create(TripCreate.model_validate(draft))
    view=register_view(store,[saved['id']])
    assert view['union_only'] and view['point_count']==22
    assert view['online_point_count']==11 and view['gps_point_count']==11
    data,count=tile(store,view['id'],0,0,0)
    features=decode(data,tmp_path)
    assert count==len(features)==22
    details=[point_detail(store,f['id']) for f in features]
    gps_details=[d for d in details if d['trip_id']==original['id']]
    assert len(gps_details)==11
    assert all(d['source']=='csv' and d['point'] in original['points'][2:13] for d in gps_details)
    # Selecting both records must not add the linked original rows a second time.
    both=register_view(store,[saved['id'],original['id']])
    assert both['point_count']==tile(store,both['id'],0,0,0)[1]==26
    assert store.get(original['id'])==original


@pytest.mark.parametrize('change', ['delete','edit'])
def test_missing_or_changed_gps_does_not_show_stale_ordinal_references(store,change):
    original=gps(store)
    saved=store.create(TripCreate.model_validate(attach_gps_overlay(store,online(original))))
    if change=='delete':store.delete(original['id'])
    else:
        def edit(record):
            record['points'][5]['longitude']+=1
            return record
        store.update_record(original['id'],edit)
    view=register_view(store,[saved['id']])
    assert view['point_count']==11 and view['gps_point_count']==0
    assert tile(store,view['id'],0,0,0)[1]==11


def test_partial_gps_uses_time_and_location_not_all_same_day_points(store):
    original=gps(store); draft=online(original)
    store.delete(original['id'])
    partial=store.create(TripCreate(title='partial GPS',mode='other',travel_date=DAY,source='csv',
                        points=original['points'][4:10]))
    wrong=store.create(TripCreate(title='elsewhere',mode='other',travel_date=DAY,source='csv',
                      points=[dict(p,latitude=50) for p in original['points'][4:10]]))
    attach_gps_overlay(store,draft)
    refs=draft['metadata']['gps_overlay']['sources']
    assert {r['trip_id'] for r in refs}=={partial['id']}
    assert draft['metadata']['gps_overlay']['point_count']==6
    assert store.get(wrong['id'])==wrong


def test_online_update_preserves_original_gps_and_existing_flight_id(store):
    from backend.flight_local import recover_local_flight
    original=gps(store)
    old=store.create(TripCreate.model_validate(recover_local_flight(store,'AB123',DAY,'AAA','BBB')))
    draft=attach_gps_overlay(store,online(original)); update_target(store,draft)
    with TestClient(app) as client:
        response=client.post(f"/api/flight/{old['id']}/track",json=draft)
        assert response.status_code==200,response.text
        saved=response.json()
        assert saved['id']==old['id'] and saved['created_at']==old['created_at']
        assert saved['source']=='Flightradar24' and len(saved['points'])==11
        assert not saved['metadata'].get('local_recovery')
        assert saved['metadata']['gps_overlay']['point_count']==11
        assert client.post(f"/api/flight/{old['id']}/track",json=draft).status_code==409
    assert store.get(original['id'])==original and len(store.list())==2


def test_original_gps_flight_never_becomes_online_update_target(store):
    original=gps(store)
    def flight(record):
        record.update(mode='flight',service_number='AB123',origin='AAA',destination='BBB')
        return record
    original=store.update_record(original['id'],flight)
    draft=attach_gps_overlay(store,online(original)); update_target(store,draft)
    assert not draft['metadata'].get('online_update')
    draft['metadata']['online_update']={'trip_id':original['id'],'fingerprint':record_fingerprint(original)}
    with TestClient(app) as client:
        response=client.post(f"/api/flight/{original['id']}/track",json=draft)
    assert response.status_code==409
    assert store.get(original['id'])==original


def test_original_gps_holes_do_not_remove_unique_online_distance(store):
    original=gps(store)
    def gap(record):
        for point in record['points'][8:]:point['segment_id']='after-hole'
        return record
    original=store.update_record(original['id'],gap)
    draft=online(original)
    for point in draft['points']:point['segment_id']='online-continuous'
    attach_gps_overlay(store,draft)
    covered=draft['metadata']['gps_overlay']['covered_distance_km'][original['id']]
    from backend.providers import _haversine
    total=sum(_haversine((a['longitude'],a['latitude']),(b['longitude'],b['latitude'])) for a,b in zip(draft['points'],draft['points'][1:]))
    assert 0<covered<total


def test_map_views_reuse_source_fingerprints_until_revision_changes(store,monkeypatch):
 import backend.flight_overlay as overlay
 original=gps(store)
 saved=store.create(TripCreate.model_validate(attach_gps_overlay(store,online(original))))
 actual=overlay.fingerprint;calls=[]
 def counted(points):calls.append(len(points));return actual(points)
 monkeypatch.setattr(overlay,'fingerprint',counted)
 view=register_view(store,[saved['id']])
 assert view['gps_point_count']==11 and calls==[len(original['points'])]
 register_view(store,[original['id'],saved['id']])
 assert len(calls)==1
 def edit(record):
  record['points'][5]['longitude']+=1
  return record
 store.update_record(original['id'],edit)
 assert register_view(store,[saved['id']])['gps_point_count']==0
 assert len(calls)==2
