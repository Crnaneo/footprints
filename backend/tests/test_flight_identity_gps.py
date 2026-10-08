from datetime import date, datetime, timedelta, timezone

import pytest

from backend import providers as p, flight_identity_gps as ig
from backend.flight_identity_gps import airport_hints_from_gps
from backend.flight_schedules import candidates_from_rows, prefer_requested_date
from backend.models import TripCreate
from backend.store import TripStore

DAY = date(2026, 2, 8)
START = datetime(2026, 2, 8, 10, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('FOOTPRINT_DATA_DIR',str(tmp_path))
    airports = {'KAAA': {'icao':'KAAA','iata':'AAA','lat':0.,'lon':0.,'tz':'UTC','elevation':0},
                'KBBB': {'icao':'KBBB','iata':'BBB','lat':0.,'lon':10.,'tz':'UTC','elevation':0}}
    airports.update({a['iata']:a for a in list(airports.values())})
    monkeypatch.setattr(p,'_airports',lambda:airports)
    monkeypatch.setattr(ig,'_airports',lambda:airports)
    return TripStore()


def gps(store, source='GPS', ground=True):
    points=[]
    for i in range(-2,14):
        air=1<=i<=11
        lon=0.1 if i==1 else 9.9 if i==11 else i-1 if air else 0. if i<=0 else 10.
        points.append({'longitude':lon,'latitude':0.,'timestamp':(START+timedelta(minutes=i*10)).isoformat(),
                       'altitude_m':(100 if i in (1,11) else 9000) if air else 0 if ground else None,
                       'speed_kmh':600 if air else 20})
    return store.create(TripCreate(title='original',mode='other',travel_date=DAY,source=source,
                                  geometry_kind='observed',points=points))


def candidate():
    return candidates_from_rows([{'ICAO_Hex':'aabbcc','Reg':'N123AB','Callsign':'ABC123',
        'Track_Origin_FL_Ft':'30000','Track_Destination_FL_Ft':'30000',
        'Track_Origin_DateTime_UTC':(START+timedelta(minutes=40)).isoformat(),
        'Track_Destination_DateTime_UTC':(START+timedelta(minutes=80)).isoformat(),
        'Track_Origin_Lon':3.,'Track_Origin_Lat':0.,'Track_Destination_Lon':7.,'Track_Destination_Lat':0.
        }],'AB123',DAY)[0]


def test_matches_both_adsb_anchors_and_preserves_original(setup):
    original=gps(setup)
    hints=airport_hints_from_gps(setup,candidate(),DAY)
    assert hints['departure_airport']=='KAAA' and hints['arrival_airport']=='KBBB'
    assert hints['date_confirmed']
    assert hints['airport_evidence']['source_trip_ids']==[original['id']]
    assert hints['airport_evidence']['matched_adsb_endpoints']==2
    assert setup.get(original['id'])==original
    assert len(setup.list())==1


def test_wrong_online_position_cannot_identify_gps(setup):
    gps(setup)
    c=candidate()
    c['flight_info']['identity_evidence']['track_endpoints'][1]['latitude']=30.
    assert airport_hints_from_gps(setup,c,DAY) is None


def test_other_day_cannot_confirm_departure_date(setup):
    gps(setup)
    assert airport_hints_from_gps(setup,candidate(),DAY+timedelta(days=1)) is None


def test_online_points_cannot_masquerade_as_original_gps(setup):
    gps(setup,source='adsb.lol')
    assert airport_hints_from_gps(setup,candidate(),DAY) is None


def test_no_low_ground_evidence_leaves_airports_empty(setup):
    gps(setup,ground=False)
    assert airport_hints_from_gps(setup,candidate(),DAY) is None


def test_conflicting_airport_not_overwritten(setup):
    gps(setup)
    c=candidate();c['flight_info']['departure_airport']='KBBB'
    assert airport_hints_from_gps(setup,c,DAY) is None


def test_same_day_observations_hide_adjacent_unconfirmed_date():
    current={'date_confirmed':False,'observation_date':DAY.isoformat()}
    other={'date_confirmed':False,'observation_date':(DAY+timedelta(days=1)).isoformat()}
    assert prefer_requested_date([other,current],DAY)==[current]


def test_midnight_observation_remains_explicit_fallback_if_no_same_day_hit():
    other={'date_confirmed':False,'observation_date':(DAY+timedelta(days=1)).isoformat()}
    assert prefer_requested_date([other],DAY)==[other]
    confirmed={'date_confirmed':True,'observation_date':other['observation_date']}
    assert prefer_requested_date([other,confirmed],DAY)==[confirmed]


def test_two_adsb_anchors_cannot_link_two_separate_gps_flights(setup):
    gps(setup)
    second=gps(setup)
    def later(record):
        for point in record['points']:
            point['timestamp']=(p._datetime(point['timestamp'])+timedelta(hours=3)).isoformat()
        return record
    setup.update_record(second['id'],later)
    c=candidate()
    anchor=c['flight_info']['identity_evidence']['track_endpoints'][1]
    anchor['timestamp']=(p._datetime(anchor['timestamp'])+timedelta(hours=3)).isoformat()
    assert airport_hints_from_gps(setup,c,DAY) is None


def test_flight_number_and_registration_are_not_special_case_inputs(setup):
    gps(setup)
    c=candidate()
    c['flight_info'].update(flight_number='ZZ9999',registration='UNRELATED-MARK',icao24='123abc')
    hints=airport_hints_from_gps(setup,c,DAY)
    assert hints['departure_airport']=='KAAA' and hints['arrival_airport']=='KBBB'
