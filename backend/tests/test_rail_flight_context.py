from backend.rail_flight_context import flight_point_mask
from backend.passenger_sections import reached_station_bins


def test_flight_context_excludes_approach_but_preserves_later_train():
    from datetime import datetime,timedelta,timezone
    start=datetime(2026,1,1,tzinfo=timezone.utc)
    points=[{'timestamp':(start+timedelta(seconds=t)).isoformat(),'speed_kmh':s,'altitude_m':a,'_accuracy_m':5}
        for t,s,a in [(0,800,9000),(30,700,7000),(60,600,5000),(90,420,3000),(120,370,400),(2000,300,400)]]
    assert flight_point_mask(points).tolist()==[True,True,True,True,True,False]
    assert not flight_point_mask([{'speed_kmh':300,'altitude_m':400}]).any()


def test_low_altitude_speed_spike_is_not_an_airborne_seed():
    from datetime import datetime,timedelta,timezone
    start=datetime(2024,3,23,tzinfo=timezone.utc)
    # A 306 km/h ground run interrupted by 453–494 km/h fixes at 110–220 m is
    # below the civil 250 kt floor; the spikes must not condemn the ride.
    rows=[(0,306,41),(30,306,41),(60,494,136),(64,481,127),(68,474,127),(72,461,117),
        (90,306,43),(120,305,42),(150,304,43),(180,466,213),(184,470,213),(188,305,50),(200,306,49)]
    points=[{'timestamp':(start+timedelta(seconds=t)).isoformat(),'speed_kmh':s,'altitude_m':a,'_accuracy_m':10}
        for t,s,a in rows]
    assert not flight_point_mask(points).any()
    # Above 300 m the same spike stays airborne and still condemns the window.
    climbed=[dict(p,altitude_m=1200) if p['speed_kmh']>450 else p for p in points]
    assert flight_point_mask(climbed).sum()>=3
    # A missing altitude keeps the previous trusted-fast behaviour.
    unknown=[dict(p,altitude_m=None) if p['speed_kmh']>450 else dict(p,altitude_m=None) for p in points]
    assert flight_point_mask(unknown).sum()>=3



def test_near_station_arrival_is_not_cut_by_switch_before_platform():
    stops=[{'position':0,'kind':'客运站'},{'position':20000,'kind':'客运站'}]
    assert reached_station_bins([],stops,[100,5000,10000,18500],junctions=[19000])==[(0,1)]
    assert reached_station_bins([],stops,[100,5000,10000,17000],junctions=[19000])==[]
