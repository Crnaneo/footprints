"""Identify missing flight airports using corroborated original GPS only."""
from __future__ import annotations

import math
from datetime import timedelta

from .flight_overlay import _gps_records
from .providers import _airports, _airport_tz, _datetime, _haversine, _iso, _number


def airport_hints_from_gps(store, candidate: dict, day) -> dict | None:
    """Require both dated ADS-B endpoints to match one validated GPS flight.

    Nearby ground samples nominate airports. The existing local flight matcher
    must then confirm a continuous airborne segment between those airports.
    Nothing is copied, saved, interpolated, or inferred from standing routes.
    """
    info = candidate["flight_info"]
    evidence = info.get("identity_evidence", {})
    anchors = evidence.get("track_endpoints") or []
    if len(anchors) != 2 or any(_datetime(a.get('timestamp')) is None
                               or a.get('longitude') is None or a.get('latitude') is None for a in anchors):
        return None
    start, end = [_datetime(a['timestamp']) for a in anchors]
    if not start < end:return None
    records = _gps_records(store, day)
    observations = sorted(((stamp, point, record['id']) for record in records for point in record.get('points', [])
                          if (stamp := _datetime(point.get('timestamp')))
                          and start - timedelta(hours=18) <= stamp <= end + timedelta(hours=18)), key=lambda entry: entry[0])
    if not observations:return None

    def matches(entries):
        for anchor in anchors:
            stamp = _datetime(anchor['timestamp'])
            if not any(abs((entry[0] - stamp).total_seconds()) <= 120
                       and _haversine((entry[1]['longitude'], entry[1]['latitude']),
                                      (anchor['longitude'], anchor['latitude'])) <= 5 for entry in entries):
                return False
        return True
    # An aircraft identifier alone must never associate an unrelated GPS day.
    if not matches(observations):return None
    grid = {}
    for code, airport in _airports().items():
        if len(code) != 4 or airport.get('icao') != code:continue
        lon, lat = _number(airport.get('lon')), _number(airport.get('lat'))
        if lon is not None and lat is not None:
            grid.setdefault((math.floor(lon), math.floor(lat)), []).append(airport)

    def ground_airports(entry):
        point = entry[1]
        altitude, speed = point.get('altitude_m'), point.get('speed_kmh')
        if altitude is None or speed is not None and speed >= 180:return []
        x, y = math.floor(point['longitude']), math.floor(point['latitude'])
        found = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for airport in grid.get((x + dx, y + dy), []):
                    elevation = (_number(airport.get('elevation')) or 0) * 0.3048
                    if altitude <= elevation + 500 and _haversine(
                            (point['longitude'], point['latitude']), (airport['lon'], airport['lat'])) <= 4:
                        found.append(airport['icao'])
        return found

    before = [entry for entry in observations if entry[0] < start]
    after = [entry for entry in observations if entry[0] > end]
    def nearest_ground(entries):
        for entry in entries:
            codes = ground_airports(entry)
            if codes:return codes[0] if len(codes) == 1 else None
        return None
    dep, arr = nearest_ground(reversed(before)), nearest_ground(after)
    if not dep or not arr or dep == arr:return None
    if info.get('departure_airport') and info['departure_airport'] != dep:return None
    if info.get('arrival_airport') and info['arrival_airport'] != arr:return None
    # Reuse all speed, continuity, endpoint and local-date checks from GPS
    # recovery instead of inventing a second permissive flight detector.
    from .flight_local import local_flight_candidates
    found = [row for row in local_flight_candidates(store, day, dep, arr) if matches(row['observations'])]
    if len(found) != 1:return None
    selected = found[0]
    zone = _airport_tz(dep)
    first = selected['observations'][0][0]
    return {'departure_airport': dep, 'arrival_airport': arr, 'departure_timezone': zone.key if zone else None,
            'date_confirmed': True, 'departure_local': first.astimezone(zone).isoformat() if zone else _iso(first),
            'airport_evidence': {'source': 'local-gps', 'candidate_id': selected['local_candidate_id'],
                'source_trip_ids': sorted({entry[2] for entry in selected['observations']}),
                'flight_start_utc': _iso(first), 'flight_end_utc': _iso(selected['observations'][-1][0]),
                'matched_adsb_endpoints': 2}}
