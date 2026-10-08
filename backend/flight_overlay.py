"""Link online flight observations to original GPS without copying or editing it."""
from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from datetime import timedelta

from .providers import _datetime, _date, _haversine

ONLINE_SOURCES = {'Flightradar24', 'OpenSky', 'adsb.lol'}
GPS_SOURCES = {'一生足迹', 'csv', 'GPS', 'manual'}


def fingerprint(points):
    return hashlib.sha256(json.dumps(points, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def record_fingerprint(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def point_key(point):
    return (_datetime(point.get('timestamp')), point['longitude'], point['latitude'])


def ordinal_ranges(ordinals):
    ranges = []
    for index in sorted(set(ordinals)):
        if ranges and index == ranges[-1][1] + 1:
            ranges[-1][1] = index
        else:
            ranges.append([index, index])
    return ranges


def _bounds(points):
    lons = [p['longitude'] for p in points]
    shifted = [lon + 360 if lon < 0 else lon for lon in lons]
    if max(shifted) - min(shifted) < max(lons) - min(lons):
        lons = shifted
    return [[min(lons), min(p['latitude'] for p in points)],
            [max(lons), max(p['latitude'] for p in points)]]


def _gps_records(store, day):
    return [r for r in store.observations_between(day - timedelta(days=2), day + timedelta(days=2))
            if r.get('source') in GPS_SOURCES and r.get('geometry_kind') in ('observed', 'imported')
            and not r.get('metadata', {}).get('local_recovery') and not r.get('metadata', {}).get('sandbox')]


def _partial_matches(records, online):
    """Match partial GPS only where time and location agree with online fixes."""
    timed = sorted(((_datetime(p.get('timestamp')), p) for p in online if _datetime(p.get('timestamp'))), key=lambda row: row[0])
    if len(timed) < 2:
        return {}
    stamps = [t for t, _ in timed]
    matched = {}
    for record in records:
        indices = []
        for index, p in enumerate(record['points']):
            t = _datetime(p.get('timestamp'))
            if not t or not stamps[0] <= t <= stamps[-1]:
                continue
            at = bisect_left(stamps, t)
            if at == 0:
                predicted = timed[0][1]
            else:
                a, b = timed[at - 1], timed[min(at, len(timed) - 1)]
                elapsed = (b[0] - a[0]).total_seconds()
                if (elapsed > 900 or a[1].get('segment_id') != b[1].get('segment_id')):
                    continue
                fraction = (t - a[0]).total_seconds() / elapsed if elapsed else 0
                delta_lon = (b[1]['longitude'] - a[1]['longitude'] + 180) % 360 - 180
                predicted = {'longitude': (a[1]['longitude'] + delta_lon * fraction + 180) % 360 - 180,
                             'latitude': a[1]['latitude'] + (b[1]['latitude'] - a[1]['latitude']) * fraction}
            if _haversine((p['longitude'], p['latitude']), (predicted['longitude'], predicted['latitude'])) <= 20:
                indices.append(index)
        if len(indices) >= 2:
            matched[record['id']] = indices
    return matched


def attach_gps_overlay(store, draft):
    """Online points stay in draft.points; local GPS is referenced by ordinal."""
    if draft.get('mode') != 'flight' or draft.get('source') not in ONLINE_SOURCES or len(draft.get('points', [])) < 2:
        return draft
    from .flight_local import local_flight_candidates
    day = _date(draft['travel_date'])
    records = _gps_records(store, day)
    if not records:
        draft.setdefault('metadata', {}).pop('gps_overlay', None)
        return draft
    by_id = {r['id']: r for r in records}
    candidates = local_flight_candidates(store, day, draft.get('origin'), draft.get('destination'))
    times = [_datetime(p.get('timestamp')) for p in draft['points']]
    times = [t for t in times if t]
    # A full airport-to-airport GPS match can include parts absent online.
    viable = []
    for candidate in candidates:
        overlap = sum(candidate['first_seen'] - 900 <= t.timestamp() <= candidate['last_seen'] + 1800 for t in times)
        if times and overlap / len(times) >= .6:
            viable.append(candidate)
    selected = {}
    if len(viable) == 1:
        keys = {}
        for _, p, source_id in viable[0]['observations']:
            keys.setdefault(source_id, set()).add(point_key(p))
        for source_id, wanted in keys.items():
            if source_id in by_id:
                selected[source_id] = [i for i, p in enumerate(by_id[source_id]['points']) if point_key(p) in wanted]
    else:
        # Never guess among ambiguous complete flights; use only corroborated fixes.
        selected = _partial_matches(records, draft['points'])
    metadata = draft.setdefault('metadata', {})
    metadata.pop('gps_overlay', None)
    if not selected:
        return draft
    sources, gps_points, intervals = [], [], []
    for source_id, indices in selected.items():
        record = by_id[source_id]
        points = record['points']
        sources.append({'trip_id': source_id, 'ranges': ordinal_ranges(indices),
                        'fingerprint': fingerprint(points), 'point_count': len(indices)})
        gps_points.extend(points[i] for i in indices)
        # Only original consecutive edges cover mileage. Reception/logger holes do not.
        for left, right in zip(indices, indices[1:]):
            a, b = points[left], points[right]
            ta, tb = _datetime(a.get('timestamp')), _datetime(b.get('timestamp'))
            if (right == left + 1 and a.get('segment_id') == b.get('segment_id') and ta and tb
                    and 0 < (tb - ta).total_seconds() <= 900):
                intervals.append((ta.timestamp(), tb.timestamp(), source_id))
    intervals.sort()
    # Attribute each overlapping portion once, even with duplicated GPS sources.
    covered = {}
    for a, b in zip(draft['points'], draft['points'][1:]):
        ta, tb = _datetime(a.get('timestamp')), _datetime(b.get('timestamp'))
        if not ta or not tb or a.get('segment_id') != b.get('segment_id') or not 0 < (tb - ta).total_seconds() <= 900:
            continue
        start, end = ta.timestamp(), tb.timestamp()
        distance = _haversine((a['longitude'], a['latitude']), (b['longitude'], b['latitude']))
        cursor = start
        for lo, hi, source_id in intervals:
            if hi <= cursor:
                continue
            if lo >= end:
                break
            left, right = max(cursor, lo), min(end, hi)
            if right > left:
                covered[source_id] = covered.get(source_id, 0) + distance * (right - left) / (end - start)
                cursor = right
    metadata['gps_overlay'] = {'sources': sources, 'point_count': len(gps_points),
        'bounds': _bounds(draft['points'] + gps_points), 'covered_distance_km': covered}
    return draft


def matching_update_records(store, flight, day, origin=None, destination=None):
    from .providers import _airport_codes
    return [r for r in store.list(mode='flight')
               if r.get('service_number') == flight and r['travel_date'] == str(day)
               and (r.get('source') in ONLINE_SOURCES or r.get('metadata', {}).get('local_recovery') or not r.get('point_count'))
               and all(not r.get(k) or not value or _airport_codes(r[k]) & _airport_codes(value)
                       for k,value in (('origin',origin),('destination',destination)))]


def update_target(store, draft):
    """Reimport can update a prior online/recovered flight, never original GPS."""
    if draft.get('source') not in ONLINE_SOURCES:
        return
    matches = matching_update_records(store,draft.get('service_number'),draft['travel_date'],draft.get('origin'),draft.get('destination'))
    if len(matches) == 1:
        current = store.get(matches[0]['id'])
        draft.setdefault('metadata', {})['online_update'] = {'trip_id': current['id'], 'fingerprint': record_fingerprint(current)}
