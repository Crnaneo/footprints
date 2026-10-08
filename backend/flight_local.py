"""Match flights to original local observations, without an aircraft identifier.

Airport endpoint evidence is mandatory. Flight numbers label a user-confirmed
match; they cannot be inferred from a phone's GPS receiver.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta

from .providers import CHINA, UTC, ProviderError, _airport, _airport_tz, _date, _datetime, _haversine, _iso, _normal


def departure_hint(day, origin, value):
    if not value:
        return None
    try:
        local_time = time.fromisoformat(value)
    except ValueError:
        raise ProviderError(422, "起飞时间请填写 HH:MM。") from None
    zone = _airport_tz(origin)
    if zone is None:
        raise ProviderError(422, "请填写可识别的起飞机场，才能确定起飞时间的当地时区。")
    return datetime.combine(_date(day), local_time, zone).astimezone(UTC)


def local_flight_candidates(store, day, origin, destination, departure_time=None):
    day = _date(day)
    airports = [_airport(_normal(code)) if code else None for code in (origin, destination)]
    if not all(airports):
        return []
    zone = _airport_tz(origin) or CHINA
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = start + timedelta(days=2)  # permit overnight flights and UTC date changes
    hint = departure_hint(day, origin, departure_time)
    records = store.observations_between(start.date() - timedelta(days=1), end.date() + timedelta(days=1))
    observations = []
    seen = set()
    for record in records:
        metadata = record.get("metadata") or {}
        if (record.get("geometry_kind") not in ("observed", "imported")
                or metadata.get("sandbox") or metadata.get("local_recovery")
                or "sandbox_sample" in record.get("quality_flags", [])
                or record.get("source") not in ("一生足迹", "csv", "GPS", "manual")):
            continue
        for point in record.get("points", []):
            stamp = _datetime(point.get("timestamp"))
            if stamp is None or not start <= stamp < end:
                continue
            identity = (stamp, point["longitude"], point["latitude"])
            if identity in seen:
                continue
            seen.add(identity)
            observations.append((stamp, point, record["id"]))
    observations.sort(key=lambda entry: entry[0])
    # Candidate flight identity and drawable segments have different boundaries:
    # logger segments / reception holes may split one flight's observations.
    # Keep those holes in output geometry, while matching the observed endpoints.
    runs, run, motion, previous_index = [], [], {}, None
    for index, entry in enumerate(observations):
        stamp, point, trip_id = entry
        speed = point.get("speed_kmh")
        if speed is None and index:
            before = observations[index - 1]
            delta = (stamp - before[0]).total_seconds()
            if 0 < delta <= 900:
                speed = _haversine((point["longitude"], point["latitude"]),
                                   (before[1]["longitude"], before[1]["latitude"])) * 3600 / delta
        motion[id(point)] = speed or 0
        if not 180 <= (speed or 0) <= 1300:
            continue
        if run:
            previous = run[-1]
            gap = (stamp - previous[0]).total_seconds()
            between = observations[previous_index + 1:index]
            ground = [entry for entry in between if motion.get(id(entry[1]), 0) < 180
                      and (entry[1].get("altitude_m") is None or entry[1]["altitude_m"] < 1200)]
            stopped = bool(ground and (ground[-1][0] - ground[0][0]).total_seconds() >= 300)
            def airborne(entry):
                altitude = entry[1].get("altitude_m")
                return ((altitude is not None and altitude >= 1200)
                        or motion.get(id(entry[1]), 0) >= 450)
            gap_distance = _haversine((previous[1]["longitude"], previous[1]["latitude"]),
                                      (point["longitude"], point["latitude"]))
            plausible_gap = (gap <= 7200 and airborne(previous) and airborne(entry)
                             and gap_distance * 3600 / max(gap, 1) <= 1300)
            if stopped or (gap > 900 and not plausible_gap):
                runs.append(run)
                run = []
        run.append(entry)
        previous_index = index
    if run:
        runs.append(run)
    found = []
    for run in runs:
        first, last = run[0], run[-1]
        if len(run) < 3 or (last[0] - first[0]).total_seconds() < 600:
            continue
        if first[0].astimezone(zone).date() != day:
            continue
        if hint and abs((first[0] - hint).total_seconds()) > 7200:
            continue
        # A train near an airport is insufficient evidence of a flight.
        speeds = [motion[id(entry[1])] for entry in run]
        if sum(speed >= 450 for speed in speeds) < 3:
            continue
        endpoints = (first[1], last[1])
        if any(_haversine((point["longitude"], point["latitude"]), (airport["lon"], airport["lat"])) > 30
               for point, airport in zip(endpoints, airports)):
            continue
        # Include intermediate observations (also those without speed) but
        # never the taxi / city journey outside the detected flight interval.
        selected = [entry for entry in observations if first[0] <= entry[0] <= last[0]]
        identity = json.dumps([(entry[2], entry[1]) for entry in selected], sort_keys=True, ensure_ascii=False)
        found.append({
            "local_candidate_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
            "departure_local": first[0].astimezone(zone).isoformat(),
            "first_seen": int(first[0].timestamp()), "last_seen": int(last[0].timestamp()),
            "origin": _normal(origin), "destination": _normal(destination),
            "point_count": len(selected), "observations": selected,
        })
    return found


def recover_local_flight(store, flight, day, origin, destination, candidate_id=None, departure_time=None, base=None):
    from .flight_reconstruction import _flight_track_stats, _infer_flight_geometry
    candidates = local_flight_candidates(store, day, origin, destination, departure_time)
    if candidate_id:
        candidates = [row for row in candidates if row["local_candidate_id"] == candidate_id]
        if not candidates:
            raise ProviderError(409, "所选本地轨迹已变化，请重新查找。")
    if not candidates:
        return None
    if len(candidates) > 1:
        raise ProviderError(409, "本地记录有多个匹配航段，请选择本次乘坐的航段。", {
            "candidates": [{key: value for key, value in row.items() if key != "observations"} for row in candidates]})
    selected = candidates[0]
    points, segment, previous = [], 0, None
    source_ids = sorted({entry[2] for entry in selected["observations"]})
    covered_distance = {}
    for stamp, point, trip_id in selected["observations"]:
        if previous and ((stamp - previous[0]).total_seconds() > 900
                         or (trip_id == previous[2] and point.get("segment_id") != previous[1].get("segment_id"))):
            segment += 1
        points.append({**point, "sequence": len(points), "segment_id": str(segment),
                       "source": point.get("source") or "个人 GPS", "source_crs": "WGS84"})
        previous = (stamp, point, trip_id)
    for a, b in zip(selected["observations"], selected["observations"][1:]):
        if (a[2] == b[2] and a[1].get("segment_id") == b[1].get("segment_id")
                and a[1].get("sequence") is not None and b[1].get("sequence") == a[1]["sequence"] + 1
                and (b[0] - a[0]).total_seconds() <= 900):
            covered_distance[a[2]] = covered_distance.get(a[2], 0) + _haversine(
                (a[1]["longitude"], a[1]["latitude"]), (b[1]["longitude"], b[1]["latitude"]))
    # Preserve airframe identity only; timing, geometry and altitude provenance
    # come from these observations, not from an unrelated upstream summary.
    identity_fields = ("registration", "icao24", "aircraft_type", "aircraft_manufacturer", "manufacturer",
                       "registered_owner", "owner_country", "registered_owner_country", "icao_type",
                       "photo_url", "photo_thumbnail", "photo_photographer", "photo_link", "airline_name", "callsign",
                       "identity_source", "identity_evidence", "identity_lookup_note")
    previous_info = (base or {}).get("metadata", {}).get("flight", {})
    info = {key: previous_info[key] for key in identity_fields if previous_info.get(key)}
    info.update({"flight_number": flight, "departure_airport": _normal(origin), "arrival_airport": _normal(destination),
                 "takeoff_time": points[0]["timestamp"], "landing_time": points[-1]["timestamp"],
                 "times_inferred": True, "track_source": "local-gps", "travel_date": _date(day).isoformat(),
                 "track_stats": _flight_track_stats(points)})
    draft = {
        "title": f"{flight} · {_normal(origin)} → {_normal(destination)}", "mode": "flight",
        "travel_date": _date(day).isoformat(), "service_number": flight,
        "origin": _normal(origin), "destination": _normal(destination),
        "source": "个人 GPS", "source_crs": "WGS84", "geometry_crs": "WGS84", "geometry_kind": "observed",
        "points": points, "quality_flags": ["local_gps_recovery", "flight_times_inferred"],
        "metadata": {"flight": info, "altitude_kind": "source_recorded", "date_timezone": (_airport_tz(origin) or CHINA).key,
                     "local_recovery": {"source_trip_ids": source_ids, "candidate_id": selected["local_candidate_id"],
                                        "covered_distance_km": covered_distance,
                                        "start": points[0]["timestamp"], "end": points[-1]["timestamp"]},
                     "notes": ["轨迹、高度与速度来自已有个人 GPS 记录；航班号由用户关联。起降时刻取飞行记录边界，仅为推断。原始记录保留，来源记录仍在时不重复累计里程。"]},
    }
    if segment:
        draft["quality_flags"].append("partial_observed_track")
        draft["metadata"]["notes"].append("本地观测包含多个区段或记录空缺；缺失部分保持断线，不插值、不补造轨迹，里程仅统计各观测区段。")
    _infer_flight_geometry(draft, info.get("icao24"))
    return draft
