"""Online flight observations and independent airframe enrichment.

FR24 is queried first. MrAirspace resolves dated ADS-B identities from flight
number/date when fields are missing; OpenSky and adsb.lol supply actual tracks.
The API falls back to original local GPS and then an information-only record.
No standing route, summary endpoint or registry lookup creates track points.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import math
import os
import re
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

import httpx

from .flight_schedules import lookup_dated_flight, split_flight

from .providers import (
    CHINA, ProviderError, UTC, _airport, _airport_codes, _airport_tz,
    _coordinate, _date, _datetime, _haversine, _iso, _normal, _number,
    _service, fetch_flight,
)

OPENSKY_BASE = "https://opensky-network.org/api"
OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network"
                     "/protocol/openid-connect/token")
ADSDB_BASE = "https://api.adsbdb.com/v0"
PLANESPOTTERS_BASE = "https://api.planespotters.net/pub/photos/hex"
# Their photo API rejects generic UAs; it must name the app with a contact URL.
PLANESPOTTERS_UA = "footprint-local/1.0 (+https://github.com/footprint-local)"
# Per-aircraft daily trace directory of adsb.lol's public readsb instance.
ADSBLOL_BASE = "https://adsb.lol/globe_history"
RUNWAYS_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"
NAVAIDS_URL = "https://davidmegginson.github.io/ourairports-data/navaids.csv"
# Cruise altitude below which fixes are ignored for the en-route sequence.
_CRUISE_MIN_ALT_M = 1200.0
# A navaid counts as "flown over" when the track passes this close to it.
_WAYPOINT_HIT_KM = 5.0
_ROUTE_MAX_POINTS = 40
# OpenSky /flights/all accepts at most a two-hour window per request.
FLIGHTS_ALL_WINDOW = timedelta(hours=2)
PARALLEL_QUERIES = 6


def fr24_date_available(day: date) -> bool:
    """Use the configured subscription window before making paid requests."""
    from .providers import _today
    try:days = max(1, min(1096, int(os.getenv("FR24_HISTORY_DAYS", "30"))))
    except ValueError:days = 30
    return bool(os.getenv("FR24_API_TOKEN", "").strip()) and _today() - timedelta(days=days - 1) <= day <= _today()

# IATA airline code → ICAO airline code for the operators most likely to be
# recorded here. Many other callsigns simply reuse the IATA prefix, so the
# raw flight number is always probed as a fallback candidate.
IATA_TO_ICAO = {
    "MU": "CES", "CA": "CCA", "CZ": "CSN", "HU": "CHH", "MF": "CXA",
    "SC": "CDG", "9C": "CQH", "HO": "DKH", "GS": "GCR", "ZH": "CSZ",
    "3U": "CSC", "FM": "CSH", "GJ": "CDC", "TV": "TBA", "PN": "CHB",
    "BK": "OKA", "KN": "CHN", "UQ": "CUA", "EU": "UEA", "DZ": "CSS",
    "AQ": "CGN", "GY": "CDY", "JR": "CRK", "CN": "AHP", "QW": "CQF",
    "NS": "DBH", "GT": "CGT", "KY": "KNA", "OQ": "CQN", "G5": "CGN",
    "CI": "CAL", "BR": "EVA", "CX": "CPA", "HX": "CRK", "UO": "TPA",
    "NX": "AMU", "JL": "JAL", "NH": "ANA", "KE": "KAL", "OZ": "AAR",
    "SQ": "SIA", "TG": "THA", "MH": "MAS", "GA": "GIA", "PR": "PAL",
    "VN": "HVN", "TR": "SLK", "AK": "AXM", "FD": "AIQ", "LJ": "LKY",
    "5J": "CEB", "BI": "RBA", "UL": "ALK", "BG": "BBC", "PK": "PIA",
    "AI": "AIC", "6E": "IGO", "EK": "UAE", "QR": "QTR", "EY": "ETD",
    "SV": "SVA", "MS": "MSR", "ET": "ETH", "KQ": "KQA", "TK": "THY",
    "SU": "AFL", "S7": "SBI", "U6": "SVR", "DP": "PBD", "5N": "UTA",
    "LO": "LOT", "OK": "CSA", "AY": "FIN", "SK": "SAS", "DY": "NOZ",
    "KL": "KLM", "AF": "AFR", "LH": "DLH", "OS": "AUA", "LX": "SWR",
    "SN": "BEL", "AZ": "ITY", "IB": "IBE", "VY": "VLG", "FR": "RYR",
    "U2": "EZY", "W6": "WZZ", "BA": "BAW", "VS": "VIR", "EI": "EIN",
    "TP": "TAP", "PC": "PGT", "WY": "OMA", "GF": "GFA", "RJ": "RJA",
    "FZ": "FDB", "G9": "ABY", "UA": "UAL", "AA": "AAL", "DL": "DAL",
    "WN": "SWA", "AS": "ASA", "B6": "JBU", "NK": "NKS", "F9": "FFT",
    "HA": "HAL", "AC": "ACA", "WS": "WJA", "AM": "AMX", "AV": "AVA",
    "LA": "LAN", "AR": "ARG", "QF": "QFA", "NZ": "ANZ", "VA": "VOZ",
    "JQ": "JST", "FJ": "FJI",
}

_opensky_token: dict = {"value": None, "expires_at": None}


def callsign_candidates(flight: str) -> list[str]:
    """IATA flight number → ICAO callsign candidates, most likely first."""
    prefix, suffix = split_flight(flight)
    return list(dict.fromkeys(([IATA_TO_ICAO[prefix] + suffix] if prefix in IATA_TO_ICAO else []) + [flight]))


async def _access_token(client: httpx.AsyncClient, force: bool = False) -> str | None:
    now = datetime.now(UTC)
    if not force and _opensky_token["value"] and _opensky_token["expires_at"] and now < _opensky_token["expires_at"]:
        return _opensky_token["value"]
    client_id = os.getenv("OPENSKY_CLIENT_ID", "").strip()
    client_secret = os.getenv("OPENSKY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    try:
        response = await client.post(OPENSKY_TOKEN_URL, data={
            "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
        })
    except httpx.RequestError:
        return None
    if response.status_code != 200:
        raise ProviderError(503, "OpenSky 凭证无效，请检查 .env 中的 OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET。")
    payload = response.json()
    _opensky_token["value"] = payload.get("access_token")
    _opensky_token["expires_at"] = now + timedelta(seconds=int(payload.get("expires_in", 1800)) - 60)
    return _opensky_token["value"]


async def _opensky_get(client: httpx.AsyncClient, path: str, params: dict) -> Any | None:
    response = None
    for attempt in (1, 2):
        headers = {}
        token = await _access_token(client, force=attempt == 2)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await client.get(f"{OPENSKY_BASE}{path}", params=params, headers=headers)
        except httpx.TimeoutException:
            raise ProviderError(504, "OpenSky 请求超时，请稍后重试。") from None
        except httpx.RequestError:
            raise ProviderError(502, "无法连接 OpenSky，请检查网络后重试。") from None
        if response.status_code != 401 or attempt == 2:
            break
    if response.status_code == 404:
        return None
    if response.status_code in (401, 403):
        raise ProviderError(403, "OpenSky 拒绝了本次查询。历史航班索引需要免费账号：在 opensky-network.org 注册后在 .env 配置 "
                                 "OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET 再重试；也可以配置 Flightradar24 密钥或直接导入 CSV。")
    if response.status_code == 429:
        raise ProviderError(429, "OpenSky 请求频率受限，请稍后再试（免费账号每日有额度）。")
    if response.status_code >= 400:
        raise ProviderError(502, f"OpenSky 服务暂时不可用（HTTP {response.status_code}）。")
    try:
        return response.json()
    except ValueError:
        raise ProviderError(502, "OpenSky 返回了无法解析的数据。") from None


def _search_window(day: date, origin: str | None) -> tuple[datetime, datetime, tzinfo]:
    """UTC [start, end] covering every takeoff whose local date is `day`."""
    zone = _airport_tz(origin) or CHINA
    start = datetime.combine(day, time.min, zone).astimezone(UTC) - timedelta(hours=6)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC) + timedelta(hours=6)
    return start, end, zone


def _flight_row(row: dict, requested_date: date, callsigns: list[str],
                origin: str | None, destination: str | None, zone: tzinfo) -> dict | None:
    row_callsign = _normal(row.get("callsign"))
    if row_callsign not in callsigns:
        return None
    first_seen = _datetime(row.get("firstSeen"))
    if first_seen is None or first_seen.astimezone(zone).date() != requested_date:
        return None
    departure = _normal(row.get("estDepartureAirport"))
    arrival = _normal(row.get("estArrivalAirport"))
    if origin and departure and not (_airport_codes(origin) & _airport_codes(departure)):
        return None
    if destination and arrival and not (_airport_codes(destination) & _airport_codes(arrival)):
        return None
    return {
        "icao24": str(row.get("icao24") or "").lower(),
        "callsign": row_callsign,
        "first_seen": int(row["firstSeen"]) if row.get("firstSeen") is not None else None,
        "last_seen": int(row["lastSeen"]) if row.get("lastSeen") is not None else None,
        "est_departure_airport": departure or None,
        "est_arrival_airport": arrival or None,
        "departure_local": _iso(first_seen.astimezone(zone)),
        "timezone": zone.key,
    }


def _departure_icao(origin: str | None) -> str | None:
    """The ICAO code behind a user-supplied airport, when we can resolve one."""
    airport = _airport(origin) if origin else None
    if airport and airport.get("icao"):
        return airport["icao"]
    return origin if origin and re.fullmatch(r"[A-Z]{4}", origin) else None


async def _query_flights_by_airport(client: httpx.AsyncClient, icao: str, callsigns: list[str],
                                    start: datetime, end: datetime, requested_date: date,
                                    origin: str | None, destination: str | None, zone: tzinfo) -> list[dict]:
    payload = await _opensky_get(client, "/flights/departure", {
        "airport": icao, "begin": int(start.timestamp()), "end": int(end.timestamp()),
    })
    found = [candidate for row in (payload if isinstance(payload, list) else []) if isinstance(row, dict)
             if (candidate := _flight_row(row, requested_date, callsigns, origin, destination, zone))]
    return sorted({f"{candidate['icao24']}-{candidate['first_seen']}": candidate for candidate in found}.values(),
                  key=lambda item: item["first_seen"] or 0)


async def _query_flights_globally(client: httpx.AsyncClient, callsigns: list[str], start: datetime,
                                  end: datetime, requested_date: date, origin: str | None,
                                  destination: str | None, zone: tzinfo) -> list[dict]:
    windows = []
    cursor = start
    while cursor < end:
        windows.append((cursor, min(cursor + FLIGHTS_ALL_WINDOW, end)))
        cursor += FLIGHTS_ALL_WINDOW
    found: dict[str, dict] = {}
    for index in range(0, len(windows), PARALLEL_QUERIES):
        batch = windows[index:index + PARALLEL_QUERIES]
        results = await asyncio.gather(*[
            _opensky_get(client, "/flights/all",
                         {"begin": int(window_start.timestamp()), "end": int(window_end.timestamp())})
            for window_start, window_end in batch
        ])
        for payload in results:
            for row in (payload if isinstance(payload, list) else []):
                if isinstance(row, dict) and (candidate := _flight_row(row, requested_date, callsigns, origin, destination, zone)):
                    found.setdefault(f"{candidate['icao24']}-{candidate['first_seen']}", candidate)
    return sorted(found.values(), key=lambda item: item["first_seen"] or 0)


async def query_opensky_flights(flight_number: str, travel_date: str | date, origin: str | None = None,
                                destination: str | None = None, icao24: str | None = None) -> list[dict]:
    """Locate the day's flights behind an IATA flight number via OpenSky."""
    flight, day = _service(flight_number, "flight"), _date(travel_date)
    origin, destination = _normal(origin) or None, _normal(destination) or None
    for code in (origin, destination):
        if code and not re.fullmatch(r"[A-Z0-9]{3,4}", code):
            raise ProviderError(422, "机场请填写三位 IATA 或四位 ICAO 代码，例如 PEK 或 ZBAA。")
    callsigns = callsign_candidates(flight)
    start, end, zone = _search_window(day, origin)
    icao = _departure_icao(origin)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
        if icao24:
            payload = await _opensky_get(client, "/flights/aircraft", {
                "icao24": icao24.lower(), "begin": int(start.timestamp()), "end": int(end.timestamp())})
            return [candidate for row in (payload if isinstance(payload, list) else []) if isinstance(row, dict)
                    if (candidate := _flight_row(row, day, callsigns, origin, destination, zone))]
        # Many mainland-China airports have no precomputed departure index on
        # OpenSky (thin ADS-B receiver coverage), so an empty airport lookup
        # falls back to the global time-window search, which still lists the
        # day's flights by callsign.
        found = (await _query_flights_by_airport(client, icao, callsigns, start, end, day, origin, destination, zone)
                 if icao else [])
        return found or await _query_flights_globally(client, callsigns, start, end, day, origin, destination, zone)


async def query_opensky_track(icao24: str, when: int) -> list | None:
    """The actual flown path from OpenSky's track endpoint, or None if absent."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
        payload = await _opensky_get(client, "/tracks", {"icao24": icao24.lower(), "time": int(when)})
    if not isinstance(payload, dict):
        return None
    path = payload.get("path")
    return path if isinstance(path, list) else None


async def fetch_adsbdb_aircraft(icao24: str) -> dict:
    """Best-effort registration / type enrichment; failures stay silent."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10)) as client:
            response = await client.get(f"{ADSDB_BASE}/aircraft/{icao24.upper()}")
        if response.status_code != 200:
            return {}
        payload = response.json()
    except (httpx.RequestError, ValueError):
        return {}
    # The live API wraps the record in {"response": {...}}; tests/mock use it bare.
    envelope = payload.get("response", payload) if isinstance(payload, dict) else None
    aircraft = envelope.get("aircraft") if isinstance(envelope, dict) else None
    if not isinstance(aircraft, dict):
        return {}
    return {key: aircraft[key] for key in (
        "registration", "type", "manufacturer", "registered_owner",
        "icao_type", "registered_owner_country_name", "url_photo", "url_photo_thumbnail",
    ) if aircraft.get(key)}


def normalize_registration(registration: str) -> str:
    """Normalize spacing and known civil-mark formats without guessing aircraft."""
    value = re.sub(r"\s+", "", registration).upper().replace("－", "-").replace("–", "-")
    if re.fullmatch(r"B[0-9A-Z]{4}", value):
        value = f"B-{value[1:]}"
    elif re.fullmatch(r"VN[A-Z][0-9]{3}", value):
        value = f"VN-{value[2:]}"
    return value


async def fetch_adsbdb_registration(registration: str) -> dict:
    """Look up aircraft details by registration (tail number); silent on misses.
    adsbdb's /v0/aircraft/ endpoint accepts both hex (mode_s) and registration."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10)) as client:
            response = await client.get(f"{ADSDB_BASE}/aircraft/{normalize_registration(registration)}")
        if response.status_code != 200:
            return {}
        payload = response.json()
    except (httpx.RequestError, ValueError):
        return {}
    envelope = payload.get("response", payload) if isinstance(payload, dict) else None
    aircraft = envelope.get("aircraft") if isinstance(envelope, dict) else None
    if not isinstance(aircraft, dict):
        return {}
    return {key: aircraft[key] for key in (
        "registration", "type", "manufacturer", "registered_owner",
        "icao_type", "registered_owner_country_name", "url_photo", "url_photo_thumbnail",
        "mode_s",  # hex (ICAO24)
    ) if aircraft.get(key)}


def merge_registration(info: dict, aircraft: dict) -> None:
    """Fill details only for the same airframe as an observed flight."""
    if (info.get("icao24") and aircraft.get("mode_s")
            and _normal(info["icao24"]) != _normal(aircraft["mode_s"])):
        return
    fields = {"registration": "registration", "type": "aircraft_type",
              "manufacturer": "aircraft_manufacturer", "registered_owner": "registered_owner",
              "icao_type": "icao_type", "registered_owner_country_name": "owner_country",
              "url_photo": "photo_url", "url_photo_thumbnail": "photo_thumbnail", "mode_s": "icao24"}
    for source, target in fields.items():
        if aircraft.get(source) and not info.get(target):
            info[target] = aircraft[source]


async def fetch_planespotters_photo(icao24: str | None = None, registration: str | None = None) -> dict:
    """Best-effort airframe photo; adsbdb misses most Chinese registrations."""
    if not icao24 and not registration:
        return {}
    url = (f"{PLANESPOTTERS_BASE}/{icao24.upper()}" if icao24 else
           f"{PLANESPOTTERS_BASE.rsplit('/', 1)[0]}/reg/{normalize_registration(registration)}")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10),
                                     headers={"User-Agent": PLANESPOTTERS_UA}) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return {}
        photos = response.json().get("photos")
    except (httpx.RequestError, ValueError):
        return {}
    if not isinstance(photos, list) or not photos or not isinstance(photos[0], dict):
        return {}
    photo = photos[0]
    large = (photo.get("thumbnail_large") or {}).get("src")
    thumb = (photo.get("thumbnail") or {}).get("src")
    info = {"photo_url": large or thumb} if (large or thumb) else {}
    if thumb:
        info["photo_thumbnail"] = thumb
    if photo.get("photographer"):
        info["photo_photographer"] = photo["photographer"]
    if photo.get("link"):
        info["photo_link"] = photo["link"]
    return info


def _track_points(path: list) -> list[dict]:
    """OpenSky waypoints [time, lat, lon, baro_alt, true_track, on_ground]."""
    points, segment, previous_time = [], 0, None
    for row in path:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        instant, coord = _datetime(row[0]), _coordinate([row[2], row[1]])
        if instant is None or coord is None:
            continue
        if previous_time and ((instant - previous_time).total_seconds() > 900 or (len(row) > 7 and row[7])):
            segment += 1
        points.append({
            "sequence": len(points), "segment_id": str(segment),
            "longitude": coord[0], "latitude": coord[1], "timestamp": _iso(instant),
            "altitude_m": _number(row[3]) if len(row) > 3 else None,
            "speed_kmh": _number(row[6]) if len(row) > 6 else None,
            "source": "adsb.lol" if len(row) > 6 else "OpenSky", "source_crs": "WGS84",
            "original_longitude": coord[0], "original_latitude": coord[1],
        })
        previous_time = instant
    return points


def _path_rows(path: list) -> list[dict]:
    rows = []
    for row in path:
        if not isinstance(row, (list, tuple)) or len(row) < 4:
            continue
        instant, latitude, longitude, altitude = _datetime(row[0]), _number(row[1]), _number(row[2]), _number(row[3])
        if instant is None or latitude is None or longitude is None:
            continue
        rows.append({"time": instant, "lat": latitude, "lon": longitude, "alt": altitude,
                     "track": _number(row[4]) if len(row) > 4 else None,
                     "on_ground": bool(row[5]) if len(row) > 5 and row[5] is not None else None})
    rows.sort(key=lambda row: row["time"])
    return rows


_COURSE_MIN_METERS = 5.0


def _fix_distance(a: list, b: list) -> float:
    """Approximate ground distance between two consecutive fixes, in meters."""
    dlat = (b[1] - a[1]) * 111320.0
    dlon = (b[2] - a[2]) * 111320.0 * math.cos(math.radians(a[1]))
    return math.hypot(dlat, dlon)


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Course from one fix to the next on a local sphere, degrees clockwise from north."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_lon = math.radians(lon2 - lon1)
    y = math.sin(delta_lon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lon)
    return math.degrees(math.atan2(y, x)) % 360


def _waypoints_from_points(points: list[dict]) -> list:
    """TrackPoint dicts → path rows for runway inference.

    The FR24 track omits a course column, so the heading is derived from
    consecutive fixes; ground speed and ground flag are not needed.
    """
    rows = []
    for point in points:
        instant = _datetime(point.get("timestamp"))
        latitude, longitude = _number(point.get("latitude")), _number(point.get("longitude"))
        if instant is None or latitude is None or longitude is None:
            continue
        rows.append([int(instant.timestamp()), latitude, longitude,
                     _number(point.get("altitude_m")), None, None])
    for index, row in enumerate(rows):
        ahead = rows[index + 1] if index + 1 < len(rows) else None
        behind = rows[index - 1] if index > 0 else None
        course = None
        # A fix that barely moved has no direction of its own (atan2(0,0)
        # would fabricate one), so only real displacement yields a course.
        if ahead is not None and _fix_distance(row, ahead) >= _COURSE_MIN_METERS:
            course = _bearing(row[1], row[2], ahead[1], ahead[2])
        elif behind is not None and _fix_distance(behind, row) >= _COURSE_MIN_METERS:
            course = _bearing(behind[1], behind[2], row[1], row[2])
        row[4] = round(course, 1) if course is not None else None
    return rows


_AIRPORT_INDEX: dict[str, dict] | None = None


def _airports_index() -> dict[str, dict]:
    """ICAO → {lon, lat} for trajectory-based airport matching."""
    global _AIRPORT_INDEX
    if _AIRPORT_INDEX is None:
        from .providers import _airports
        index = {}
        for code, airport in _airports().items():
            longitude, latitude = _number(airport.get("lon")), _number(airport.get("lat"))
            if len(code) == 4 and longitude is not None and latitude is not None:
                index[code] = {"lon": longitude, "lat": latitude}
        _AIRPORT_INDEX = index
    return _AIRPORT_INDEX


def _nearest_airport(lon: float, lat: float, max_km: float = 15) -> str | None:
    best, best_km = None, max_km
    for code, airport in _airports_index().items():
        distance = _haversine((lon, lat), (airport["lon"], airport["lat"]))
        if distance < best_km:
            best, best_km = code, distance
    return best


def _infer_airports(path: list | None, candidate_departure: str | None,
                    candidate_arrival: str | None) -> tuple[str | None, str | None]:
    """Keep the index's estimates, then fall back to nearest-airport matching."""
    departure, arrival = candidate_departure, candidate_arrival
    points = _track_points(path or [])
    if len(points) >= 2:
        first, last = points[0], points[-1]
        if not _normal(departure or "") and first.get("altitude_m") is not None and first["altitude_m"] <= 1000:
            departure = _nearest_airport(first["longitude"], first["latitude"])
        if not _normal(arrival or "") and last.get("altitude_m") is not None and last["altitude_m"] <= 1000:
            arrival = _nearest_airport(last["longitude"], last["latitude"])
    return departure, arrival


# ------------------------------------------------- adsb.lol daily archive

# A reception gap longer than this separates two flights of the same aircraft
# on one day; the archive's daily file is cut apart on these gaps.
_GAP_SPLIT_SECONDS = 1800.0


def _readsb_path(payload: dict, first_seen: int, last_seen: int | None, expected_callsigns: list[str] | None = None) -> list:
    """readsb trace_full rows → OpenSky-path rows [time, lat, lon, alt_m, track, on_ground].

    Row layout: [relative_seconds, lat, lon, alt_ft, ground_speed_kt, track_deg,
    flags, vertical_rate, aircraft_details, source, ...]. Ground is encoded
    as altitude="ground"; flags & 2 marks a new leg, & 8 geometric altitude.
    An internal seventh path column carries observed ground speed in km/h.
    The flight's own window is [first_seen - 900, last_seen + 900]; without a
    known landing time only the continuous run of fixes seeded at first_seen
    is kept, cut at the first reception gap over 30 minutes, so the aircraft's
    later flights on that day never bleed into this trip.
    """
    trace = payload.get("trace") if isinstance(payload, dict) else None
    base = _number(payload.get("timestamp")) if isinstance(payload, dict) else None
    if not isinstance(trace, list) or base is None:
        return []
    rows = []
    for row in trace:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        relative = _number(row[0])
        if relative is None:
            continue
        when = base + relative
        latitude, longitude = _number(row[1]), _number(row[2])
        if latitude is None or longitude is None:
            continue
        altitude = _number(row[3])
        flags = row[6] if len(row) > 6 and isinstance(row[6], int) else 0
        ground = row[3] == "ground"
        speed = _number(row[4])
        rows.append([when, latitude, longitude,
                     0.0 if ground else (altitude * 0.3048 if altitude is not None and not flags & 8 else None),
                     _number(row[5]),
                     True if ground else (False if altitude is not None else None),
                     speed * 1.852 if speed is not None and speed >= 0 else None,
                     bool(flags & 2),
                     _normal((row[8] or {}).get("flight")) if len(row) > 8 and isinstance(row[8], dict) else ""])
    rows.sort(key=lambda row: row[0])
    end = (last_seen or first_seen + 12 * 3600) + 900
    window = [row for row in rows if first_seen - 900 <= row[0] <= end]
    verified = False
    if expected_callsigns:
        expected = {_normal(value) for value in expected_callsigns}
        groups, group, label = [], [], ""
        for row in rows:
            if group and (row[0] - group[-1][0] > _GAP_SPLIT_SECONDS or row[7]
                          or (row[8] and label and row[8] != label)):
                groups.append((label, group));group, label = [], ""
            group.append(row)
            label = row[8] or label
        if group:groups.append((label, group))
        matching = [row for call, run in groups if call in expected or (not call and last_seen is not None) for row in run
                    if first_seen - 900 <= row[0] <= end]
        if matching:
            window, verified = matching, True
        elif any(row[8] for row in window):
            return []
    if window and not verified:
        # No landing time known: keep only the continuous activity seeded at
        # takeoff, cut at the first long reception gap.
        seed = min(window, key=lambda row: abs(row[0] - first_seen))
        if abs(seed[0] - first_seen) > 1800:
            return []
        index = window.index(seed)
        kept = [seed]
        for row in window[index + 1:]:
            if (last_seen is None and row[0] - kept[-1][0] > _GAP_SPLIT_SECONDS) or row[7]:
                break
            kept.append(row)
        for row in reversed(window[:index]):
            if (last_seen is None and kept[0][0] - row[0] > _GAP_SPLIT_SECONDS) or kept[0][7]:
                break
            kept.insert(0, row)
        window = kept
    for row in window:
        row[0] = int(row[0])
    return [row[:8] for row in window]


def _cached_adsblol_day(day: date, icao24: str) -> dict | None:
    """Use already retrieved official single-aircraft archives with verified hashes."""
    from .store import default_database_path
    if not re.fullmatch(r"[0-9a-fA-F]{6}", icao24):return None
    path = default_database_path().parent / "flight_archive_queries" / day.isoformat() / f"trace_full_{icao24.lower()}.json"
    try:
        if path.stat().st_size > 20 * 1024 * 1024:return None
        raw = path.read_bytes()
        provenance = json.loads(path.with_suffix(".source.json").read_text())
        if not isinstance(provenance, dict):return None
        prefix = f"https://github.com/adsblol/globe_history_{day.year}/releases/download/v{day:%Y.%m.%d}-"
        if (not str(provenance.get("source", "")).startswith(prefix)
                or provenance.get("sha256") != hashlib.sha256(raw).hexdigest()):return None
        daily = json.loads(raw)
        return daily if isinstance(daily, dict) and _normal(daily.get("icao")) == _normal(icao24) else None
    except (OSError, ValueError, TypeError):return None


async def _fetch_adsblol_path(selected: dict) -> tuple[list, dict] | None:
    """One aircraft's daily trace from adsb.lol's public readsb archive.

    Try small per-aircraft files for the flight's UTC dates. These shortcut
    URLs may be unavailable even if the publisher's full daily release exists.
    Return path rows and the trace's airframe metadata; never download a bulk
    daily release as an implicit fallback.
    """
    icao24, first_seen = selected.get("icao24"), selected.get("first_seen")
    if not icao24 or first_seen is None:
        return None
    # Traces are filed under the UTC day the fixes belong to, which differs
    # from the departure-airport local date on evening departures.
    archive_day = datetime.fromtimestamp(first_seen - 900, UTC).date()
    end = selected.get("last_seen") or first_seen + 12 * 3600
    final_day = datetime.fromtimestamp(end + 900, UTC).date()
    payloads = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
        # A flight can cross UTC midnight. At most three small daily files;
        # no automatic multi-GB release download on a missing shortcut URL.
        for offset in range(min(3, (final_day - archive_day).days + 1)):
            day = archive_day + timedelta(days=offset)
            url = (f"{ADSBLOL_BASE}/{day.year}/{day.month:02d}/{day.day:02d}"
                   f"/traces/{icao24[-2:]}/trace_full_{icao24}.json")
            try:
                response = await client.get(url, headers={"x-requested-with": "XMLHttpRequest"})
                daily = None
                if response.status_code == 200:
                    raw = response.content
                    if raw[:2] == b"\x1f\x8b":raw = gzip.decompress(raw)
                    daily = json.loads(raw)
            except (httpx.RequestError, ValueError, OSError):
                daily = None
            if daily is None:daily = _cached_adsblol_day(day, icao24)
            if (isinstance(daily, dict) and isinstance(daily.get("trace"), list)
                    and (not daily.get("icao") or _normal(daily["icao"]) == _normal(icao24))):
                payloads.append(daily)
    if not payloads:
        return None
    payload = {**payloads[0], "timestamp": 0, "trace": []}
    for daily in payloads:
        base = _number(daily.get("timestamp"))
        if base is None:
            continue
        for row in daily["trace"]:
            if isinstance(row, list) and row and _number(row[0]) is not None:
                payload["trace"].append([base + float(row[0]), *row[1:]])
    path = _readsb_path(payload, int(first_seen), selected.get("last_seen"), selected.get("expected_callsigns"))
    path = list({tuple(row): row for row in path}.values())
    if len(path) < 2:
        return None
    aircraft = {}
    if isinstance(payload, dict):
        if payload.get("r"):
            aircraft["registration"] = str(payload["r"])
        if payload.get("t"):
            aircraft["type"] = str(payload["t"])
    return path, aircraft


# ---------------------------------------------------------------- runways

_runways_cache: dict[str, list[dict]] | None = None


def _runways_file():
    from .store import default_database_path
    directory = default_database_path().parent / "airports"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "runways.csv"


def _parse_runways(text: str) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for row in csv.DictReader(text.splitlines()):
        try:
            if str(row.get("closed", "")).strip().lower() in ("1", "true"):
                continue
            # The CSV exposes the magnetic heading of each end separately
            # (le_heading_degT / he_heading_degT); the reference point is the
            # runway centre between the two thresholds.
            heading = _number(row.get("le_heading_degT"))
            if heading is None:
                opposite = _number(row.get("he_heading_degT"))
                heading = (opposite + 180) % 360 if opposite is not None else None
            if heading is None:
                continue
            le_lat, le_lon = float(row["le_latitude_deg"]), float(row["le_longitude_deg"])
            he_lat, he_lon = float(row["he_latitude_deg"]), float(row["he_longitude_deg"])
            runway = {
                "le_ident": _normal(row.get("le_ident")),
                "he_ident": _normal(row.get("he_ident")),
                "latitude": (le_lat + he_lat) / 2, "longitude": (le_lon + he_lon) / 2,
                "heading": heading % 360,
                "length_m": float(row["length_ft"]) * 0.3048 if row.get("length_ft") else 3000.0,
            }
        except (KeyError, TypeError, ValueError):
            continue
        ident = _normal(row.get("airport_ident"))
        if ident and runway["le_ident"] and runway["he_ident"]:
            index.setdefault(ident, []).append(runway)
    return index


def load_runways() -> dict[str, list[dict]]:
    """OurAirports public-domain runway geometry, downloaded once and cached."""
    global _runways_cache
    if _runways_cache is not None:
        return _runways_cache
    path = _runways_file()
    try:
        if not path.exists():
            with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=True) as client:
                response = client.get(RUNWAYS_URL)
                response.raise_for_status()
                path.write_bytes(response.content)
        _runways_cache = _parse_runways(path.read_text(encoding="utf-8"))
    except (httpx.HTTPError, OSError, ValueError):
        _runways_cache = {}
    return _runways_cache


def _lateral_offset(runway_heading: float, runway_lon: float, runway_lat: float,
                    lon: float, lat: float) -> tuple[float, float]:
    """Metres (along-track, cross-track) from the runway reference point."""
    px = (lon - runway_lon) * 111320.0 * math.cos(math.radians(runway_lat))
    py = (lat - runway_lat) * 110540.0
    heading = math.radians(runway_heading)
    along = px * math.sin(heading) + py * math.cos(heading)
    cross = px * math.cos(heading) - py * math.sin(heading)
    return along, cross


def _phase_window(rows: list[dict], phase: str) -> tuple[list[dict], datetime]:
    """Points around wheels-off (takeoff) or the final approach (landing)."""
    threshold = 150.0
    if phase == "takeoff":
        anchor = next((row for row in rows if (row["alt"] or 0) > threshold and row["on_ground"] is not True), None)
        if anchor is None:
            return [], rows[0]["time"] if rows else datetime.now(UTC)
        window_start = anchor["time"] - timedelta(seconds=75)
        limit = anchor["time"] + timedelta(seconds=90)
        points = [row for row in rows if window_start <= row["time"] <= limit and (row["alt"] or 0) <= 350]
        return points, anchor["time"]
    anchor = next((row for row in reversed(rows) if (row["alt"] or 0) > threshold), None)
    if anchor is None:
        return [], rows[-1]["time"] if rows else datetime.now(UTC)
    # 900 m instead of ~550 m: FR24 tracks often stop short of the ground (the
    # last fixes sit at 800–900 m on final approach), and those fixes are still
    # aligned with the runway centreline. The window also reaches 120 s behind
    # the anchor because a truncated track has no fixes after it.
    points = [row for row in rows
              if row["time"] >= anchor["time"] - timedelta(seconds=120)
              and (row["alt"] or 0) <= 900]
    return points, anchor["time"]


def infer_runway(path: list, airport_icao: str | None, phase: str) -> dict | None:
    """Match the takeoff/landing phase against the airport's runway geometry.

    Both runway directions are probed as separate hypotheses; the flown track
    heading selects the direction, so the reported ident is the one the
    aircraft actually used. Purely inferred from the observed path; returns
    None when coverage or the runway database is insufficient.
    """
    if not airport_icao:
        return None
    runways = load_runways().get(airport_icao)
    rows = _path_rows(path)
    if not runways or len(rows) < 3:
        return None
    window, _anchor = _phase_window(rows, phase)
    window = [row for row in window if row["track"] is not None]
    if len(window) < 3:
        return None
    best = None
    for runway in runways:
        for direction, ident in ((0, runway["le_ident"]), (180, runway["he_ident"])):
            heading = (runway["heading"] + direction) % 360
            # Along-track coordinates are signed against the flow direction:
            # takeoff moves from the threshold outwards, while the landing
            # approach approaches from behind the reference point.
            if phase == "landing":
                # The far reach scales with the highest fix in the window:
                # a 900 m truncated-approach fix can legitimately sit ~24 km
                # behind the threshold (shallow 2° descent or QNH drift).
                window_alt = max((row["alt"] or 0) for row in window) if window else 0
                low = -(runway["length_m"] / 2
                        + max(20000.0, window_alt / math.tan(math.radians(3.0)) * 1.4))
                high = runway["length_m"] / 2 + 800
            else:
                low, high = -500.0, runway["length_m"] / 2 + 6000
            samples = []
            for row in window:
                along, cross = _lateral_offset(heading, runway["longitude"], runway["latitude"], row["lon"], row["lat"])
                if not (low <= along <= high):
                    continue
                heading_diff = abs((row["track"] - heading + 180) % 360 - 180)
                samples.append((abs(cross), heading_diff))
            if len(samples) < 3:
                continue
            mean_cross = sum(sample[0] for sample in samples) / len(samples)
            median_diff = sorted(sample[1] for sample in samples)[len(samples) // 2]
            if median_diff > 40 or mean_cross > 250:
                continue
            if best is None or mean_cross < best["mean_offset_m"]:
                best = {"runway": ident, "mean_offset_m": round(mean_cross, 1),
                        "heading_diff_deg": round(median_diff, 1)}
    return best


# ---------------------------------------------------------------- navaids

_navaids_cache: dict[tuple[int, int], list[dict]] | None = None


def _navaids_file():
    from .store import default_database_path
    directory = default_database_path().parent / "airports"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "navaids.csv"


def _parse_navaids(text: str) -> dict[tuple[int, int], list[dict]]:
    """OurAirports navaids bucketed by a 0.25° grid for cheap proximity hits."""
    index: dict[tuple[int, int], list[dict]] = {}
    for row in csv.DictReader(text.splitlines()):
        try:
            lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
        except (KeyError, TypeError, ValueError):
            continue
        ident, kind = _normal(row.get("ident")), _normal(row.get("type"))
        if not ident or not kind or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        cell = (int((lon + 180) // 0.25), int((lat + 90) // 0.25))
        index.setdefault(cell, []).append({
            "ident": ident, "name": _normal(row.get("name")) or ident,
            "type": kind, "latitude": lat, "longitude": lon,
        })
    return index


def load_navaids() -> dict[tuple[int, int], list[dict]]:
    """OurAirports public-domain navaid list, downloaded once and cached."""
    global _navaids_cache
    if _navaids_cache is not None:
        return _navaids_cache
    path = _navaids_file()
    try:
        if not path.exists():
            with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=True) as client:
                response = client.get(NAVAIDS_URL)
                response.raise_for_status()
                path.write_bytes(response.content)
        _navaids_cache = _parse_navaids(path.read_text(encoding="utf-8"))
    except (httpx.HTTPError, OSError, ValueError):
        _navaids_cache = {}
    return _navaids_cache


def _nearest_navaid(lon: float, lat: float) -> dict | None:
    navaids = load_navaids()
    if not navaids:
        return None
    cell = (int((lon + 180) // 0.25), int((lat + 90) // 0.25))
    best: tuple[float, dict] | None = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for candidate in navaids.get((cell[0] + dx, cell[1] + dy), []):
                distance = _haversine((lon, lat), (candidate["longitude"], candidate["latitude"]))
                if best is None or distance < best[0]:
                    best = (distance, candidate)
    return best[1] if best and best[0] <= _WAYPOINT_HIT_KM else None


def infer_route_waypoints(points: list[dict]) -> list[dict] | None:
    """En-route navaid sequence flown over during cruise.

    Track fixes above cruise altitude are matched against the OurAirports
    navaid list; fixes hitting the same navaid collapse into one entry.
    Purely inferred from the observed path; returns None when the navaid
    database is missing or nothing was flown over.
    """
    sequence: list[dict] = []
    seen: set[str] = set()
    for point in points:
        altitude = point.get("altitude_m")
        if altitude is None or altitude < _CRUISE_MIN_ALT_M:
            continue
        hit = _nearest_navaid(point["longitude"], point["latitude"])
        if hit is None or hit["ident"] in seen:
            continue
        seen.add(hit["ident"])
        sequence.append({"ident": hit["ident"], "name": hit["name"], "type": hit["type"]})
        if len(sequence) >= _ROUTE_MAX_POINTS:
            break
    return sequence or None


# ------------------------------------------------------------ main entry

def _airport_name(code: str | None) -> str | None:
    airport = _airport(code) if code else None
    return airport.get("name") if airport else None


def _median(values: list) -> float | None:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else None


def _flight_track_stats(points: list[dict]) -> dict | None:
    """Cruise-level statistics computed purely from the observed points."""
    alts = [p.get("altitude_m") for p in points if p.get("altitude_m") is not None]
    if not alts or len(points) < 2:
        return None
    peak = max(alts)
    # The cruise band is everything within 15% of the flight's ceiling.
    band = [a for a in alts if a >= peak * 0.85] or alts
    cruise_speeds = [p["speed_kmh"] for p in points if p.get("speed_kmh") is not None
                     and (p.get("altitude_m") or 0) >= peak * 0.85]
    flown = sum(_haversine((a["longitude"], a["latitude"]), (b["longitude"], b["latitude"]))
                for a, b in zip(points, points[1:]) if a.get("segment_id") == b.get("segment_id"))
    speeds = [p.get("speed_kmh") for p in points if p.get("speed_kmh") is not None]
    return {
        "max_altitude_m": round(peak),
        "cruise_altitude_m": round(_median(band)),
        "cruise_speed_kmh": round(_median(cruise_speeds)) if cruise_speeds else None,
        "max_speed_kmh": round(max(speeds)) if speeds else None,
        "flown_distance_km": round(flown, 1),
    }


def _unified_flight_info(flight: str, day: date, candidate: dict, path: list | None,
                         aircraft: dict, departure: str | None, arrival: str | None,
                         track_source: str) -> dict:
    first_seen = _datetime(candidate.get("first_seen"))
    last_seen = _datetime(candidate.get("last_seen"))
    points = _track_points(path or [])
    times = [point["timestamp"] for point in points if point["timestamp"]]
    if first_seen is None and times:
        first_seen = _datetime(times[0])
    if last_seen is None and times:
        last_seen = _datetime(times[-1])
    departure_runway = infer_runway(path, departure, "takeoff") if path else None
    arrival_runway = infer_runway(path, arrival, "landing") if path else None
    track_points = _track_points(path) if path else []
    return {
        "flight_number": flight,
        "callsign": candidate.get("callsign"),
        "icao24": candidate.get("icao24"),
        "registration": aircraft.get("registration"),
        "aircraft_type": aircraft.get("type"),
        "aircraft_manufacturer": aircraft.get("manufacturer"),
        "departure_airport": departure,
        "arrival_airport": arrival,
        "departure_airport_name": _airport_name(departure),
        "arrival_airport_name": _airport_name(arrival),
        "takeoff_time": _iso(first_seen),
        "landing_time": _iso(last_seen),
        "takeoff_runway": departure_runway["runway"] if departure_runway else None,
        "landing_runway": arrival_runway["runway"] if arrival_runway else None,
        "runway_evidence": {"takeoff": departure_runway, "landing": arrival_runway},
        "route_waypoints": infer_route_waypoints(track_points) or None,
        "track_stats": _flight_track_stats(track_points),
        # adsbdb enrichment (present only when the registry answered).
        "icao_type": aircraft.get("icao_type"),
        "registered_owner": aircraft.get("registered_owner"),
        "owner_country": aircraft.get("registered_owner_country_name"),
        "photo_url": aircraft.get("url_photo"),
        "photo_thumbnail": aircraft.get("url_photo_thumbnail"),
        "track_source": track_source,
        "travel_date": day.isoformat(),
        # SID/STAR/approach/airway data has no free programmatic source; the
        # keys are simply absent so the UI hides those rows entirely.
    }


def _unify_fr24_flight(draft: dict) -> dict:
    """Give the Flightradar24 fallback draft the same metadata.flight shape."""
    selected = draft.get("metadata", {}).get("flight", {})
    summary = selected.get("summary", {}) if isinstance(selected, dict) else {}
    runway_takeoff = _normal(summary.get("runway_takeoff")) or None
    runway_landed = _normal(summary.get("runway_landed")) or None
    draft.setdefault("metadata", {})["flight"] = {
        "flight_number": draft.get("service_number"),
        "callsign": _normal(summary.get("callsign")) or None,
        "icao24": selected.get("icao24") or _normal(summary.get("hex")) or None,
        "registration": summary.get("reg"),
        "aircraft_type": summary.get("type"),
        "departure_airport": draft.get("origin"),
        "arrival_airport": draft.get("destination"),
        "departure_airport_name": _airport_name(draft.get("origin")),
        "arrival_airport_name": _airport_name(draft.get("destination")),
        "takeoff_time": selected.get("departure"),
        "landing_time": selected.get("arrival"),
        "takeoff_runway": runway_takeoff,
        "landing_runway": runway_landed,
        "runway_takeoff_official": bool(runway_takeoff),
        "runway_landed_official": bool(runway_landed),
        "runway_evidence": {},
        "track_source": "flightradar24",
        "travel_date": draft.get("travel_date"),
        # flight-summary/full extras (absent on the light response or OpenSky).
        "operating_as": summary.get("operating_as") or None,
        "painted_as": summary.get("painted_as") or None,
        "aircraft_category": summary.get("category") or None,
        "departure_iata": summary.get("orig_iata") or None,
        "arrival_iata": summary.get("dest_iata") or None,
        "arrival_airport_actual": summary.get("dest_icao_actual") or None,
        "arrival_iata_actual": summary.get("dest_iata_actual") or None,
        "flight_time_s": summary.get("flight_time"),
        "actual_distance_km": summary.get("actual_distance"),
        "circle_distance_km": summary.get("circle_distance"),
        "track_stats": _flight_track_stats(draft.get("points") or []),
    }
    return draft


async def _enrich_with_registry(info: dict) -> None:
    """Fill owner / detailed type / photo from adsbdb for the flown airframe."""
    if not info.get("icao24"):
        return
    aircraft = await fetch_adsbdb_aircraft(info["icao24"])
    for key, source in (("icao_type", "icao_type"), ("registered_owner", "registered_owner"),
                        ("owner_country", "registered_owner_country_name"),
                        ("photo_url", "url_photo"), ("photo_thumbnail", "url_photo_thumbnail")):
        if aircraft.get(source) and not info.get(key):
            info[key] = aircraft[source]
    if not info.get("photo_url"):
        # adsbdb's photo index covers only a slice of the fleet; the
        # planespotters public API resolves nearly any airframe by hex.
        await _fill_missing_photo(info)


def _infer_flight_geometry(draft: dict, selected_icao24: str | None) -> None:
    """Run runway + en-route inference over an FR24 draft in place.

    Flightradar24's own runway idents outrank our geometric inference, so only
    runways missing from the official summary are inferred here.
    """
    info = draft.setdefault("metadata", {}).setdefault("flight", {})
    if selected_icao24 and not info.get("icao24"):
        info["icao24"] = selected_icao24
    waypoints = _waypoints_from_points(draft.get("points") or [])
    inferred = {}
    if len(waypoints) >= 3:
        if not info.get("takeoff_runway"):
            takeoff = infer_runway(waypoints, draft.get("origin"), "takeoff")
            if takeoff:
                info["takeoff_runway"] = takeoff["runway"]
                inferred["takeoff"] = takeoff
        if not info.get("landing_runway"):
            landing = infer_runway(waypoints, draft.get("destination"), "landing")
            if landing:
                info["landing_runway"] = landing["runway"]
                inferred["landing"] = landing
    info["route_waypoints"] = infer_route_waypoints(draft.get("points") or [])
    if inferred:
        info["runway_evidence"] = inferred
        draft.setdefault("quality_flags", []).append("runway_inferred")
    elif not info.get("runway_evidence"):
        info["runway_evidence"] = {}


async def finish_fr24_summary_draft(draft: dict, icao24: str | None = None) -> dict:
    """Bring a providers.fetch_flight summary draft up to the unified shape.

    The import/preview endpoints hand the FR24 draft to the store as-is, so
    fold in the official summary extras, runway + en-route inference and the
    adsbdb registry block here to keep every flight payload the same shape.
    """
    draft = _unify_fr24_flight(draft)
    _infer_flight_geometry(draft, icao24)
    await _enrich_with_registry(draft["metadata"]["flight"])
    return draft


async def _fill_missing_photo(info: dict) -> None:
    """adsbdb's photo index covers only part of the fleet; planespotters
    resolves nearly any airframe by hex. Never overwrites an existing photo."""
    if info.get("photo_url"):
        return
    photo = await fetch_planespotters_photo(info["icao24"]) if info.get("icao24") else {}
    if not photo and info.get("registration"):
        photo = await fetch_planespotters_photo(registration=info["registration"])
    for key, value in photo.items():
        if not info.get(key):
            info[key] = value


async def lookup_aircraft_info(registration: str | None = None, icao24: str | None = None) -> dict:
    """Aircraft registry and photos are independent of dated flight coverage."""
    if icao24 and not re.fullmatch(r"[0-9a-fA-F]{6}", icao24):
        raise ProviderError(422, "hex / ICAO24 应为六位十六进制字符。")
    aircraft = (await fetch_adsbdb_registration(registration) if registration else
                await fetch_adsbdb_aircraft(icao24) if icao24 else {})
    if (icao24 and aircraft.get("mode_s")
            and _normal(icao24) != _normal(aircraft["mode_s"])):
        raise ProviderError(422, "填写的 hex 与注册号查询结果不一致，请核对当日飞机资料。")
    info = {"icao24": icao24} if icao24 else {}
    merge_registration(info, aircraft)
    if registration and not info.get("registration"):
        info["registration"] = normalize_registration(registration)
    await _fill_missing_photo(info)
    return info


def merge_aircraft_info(info: dict, aircraft_info: dict) -> None:
    """Observed airframe identity wins over conflicting user hints."""
    if (info.get("icao24") and aircraft_info.get("icao24")
            and _normal(info["icao24"]) != _normal(aircraft_info["icao24"])):
        return
    for key, value in aircraft_info.items():
        if value and not info.get(key):
            info[key] = value


async def fetch_flight_unified(flight_number: str, travel_date: str | date,
                               origin: str | None = None, destination: str | None = None,
                               fr24_id: str | None = None, icao24: str | None = None,
                               first_seen: int | None = None,
                               registration: str | None = None,
                               last_seen: int | None = None,
                               schedule_candidate_id: str | None = None) -> dict:
    """Single entry behind the flight import tab.

    Flightradar24 carries the richest official data, so it goes first; when it
    cannot serve the date under the configured plan or lacks coverage
    the free observation sources take over. Without an online track, the import
    endpoint tries local GPS and otherwise returns no data. 409 always surfaces.
    If a registration (tail number) is provided, aircraft details are fetched
    from adsbdb and merged (best-effort, never overwrites existing fields).
    """
    flight, day = _service(flight_number, "flight"), _date(travel_date)
    if icao24 and not re.fullmatch(r"[0-9a-fA-F]{6}", icao24):
        raise ProviderError(422, "hex / ICAO24 应为六位十六进制字符。")
    flight_info = {"flight_number": flight, "travel_date": day.isoformat(),
                   "departure_airport": _normal(origin) or None,
                   "arrival_airport": _normal(destination) or None}
    draft = None
    # FR24 always gets the first track attempt, including explicit hex inputs.
    if fr24_date_available(day):
        try:
            result = await fetch_flight(flight, day, origin, destination, fr24_id)
        except ProviderError as exc:
            if exc.status_code in (409, 422):
                raise
            selected = (exc.details or {}).get("fr24_flight") if isinstance(exc.details, dict) else None
            if selected:
                summary = _unify_fr24_flight({"service_number": flight, "travel_date": day.isoformat(),
                    "origin": selected.get("origin"), "destination": selected.get("destination"),
                    "points": [], "metadata": {"flight": selected}})
                flight_info.update(summary["metadata"]["flight"])
        else:
            result = await finish_fr24_summary_draft(result)
            flight_info.update(result["metadata"]["flight"])
            if len(result.get("points") or []) >= 2:
                draft = result

    # Resolve the dated airframe/airports before requesting an ADS-B trace.
    # A complete FR24 identity or complete manual hints do not need a quarter
    # download. Dated observations fill gaps; standing route data never does.
    complete_identity = ((flight_info.get("registration") or registration or icao24)
                         and (origin or flight_info.get("departure_airport"))
                         and (destination or flight_info.get("arrival_airport")))
    if draft is None and (schedule_candidate_id or not complete_identity):
        dated = await lookup_dated_flight(flight, day, IATA_TO_ICAO,
            origin=origin or flight_info.get("departure_airport"),
            destination=destination or flight_info.get("arrival_airport"),
            registration=flight_info.get("registration") or registration,
            icao24=flight_info.get("icao24") or icao24, first_seen=first_seen,
            schedule_candidate_id=schedule_candidate_id)
        merge_aircraft_info(flight_info, dated)

    # Registry/photos do not depend on any provider returning a track.
    aircraft_info = await lookup_aircraft_info(
        registration or (None if icao24 else flight_info.get("registration")),
        icao24 or (None if registration else flight_info.get("icao24")))
    merge_aircraft_info(flight_info, aircraft_info)
    resolved_hex = flight_info.get("icao24") or icao24 or aircraft_info.get("icao24")
    if resolved_hex and not re.fullmatch(r"[0-9a-fA-F]{6}", resolved_hex):
        resolved_hex = None
    if draft is None:
        takeoff = _datetime(flight_info.get("takeoff_time"))
        landing = _datetime(flight_info.get("landing_time"))
        kwargs = {"icao24": resolved_hex,
                  "first_seen": int(takeoff.timestamp()) if takeoff else first_seen,
                  "skip_fr24_summary": True}
        if flight_info.get("identity_source") == "mrairspace":
            kwargs["matched_callsign"] = flight_info.get("callsign")
        if landing:
            kwargs["last_seen"] = int(landing.timestamp())
        elif last_seen is not None:
            kwargs["last_seen"] = last_seen
        try:
            draft = await fetch_flight_reconstruction(flight, day,
                origin or flight_info.get("departure_airport"),
                destination or flight_info.get("arrival_airport"), **kwargs)
        except ProviderError as exc:
            details = dict(exc.details) if isinstance(exc.details, dict) else {}
            observed_info = details.get("flight_info") or {}
            merge_aircraft_info(flight_info, observed_info)
            if not aircraft_info.get("icao24") and observed_info.get("icao24"):
                aircraft_info = await lookup_aircraft_info(icao24=observed_info["icao24"])
                merge_aircraft_info(flight_info, aircraft_info)
            # Keep the compatibility key for existing consumers; flight_info
            # also carries dated flight identity from FR24 / OpenSky.
            details.update(flight_info=flight_info, aircraft_info=flight_info)
            raise ProviderError(exc.status_code, exc.message, details) from exc
    info = draft.setdefault("metadata", {}).setdefault("flight", {})
    merge_aircraft_info(info, flight_info)
    if flight_info.get("track_source") == "flightradar24":
        for key in ("takeoff_time", "landing_time"):
            if flight_info.get(key):info[key] = flight_info[key]
        info["times_inferred"] = False
    await _fill_missing_photo(info)
    return draft


async def fetch_flight_reconstruction(flight_number: str, travel_date: str | date, origin: str | None = None,
                                      destination: str | None = None, icao24: str | None = None,
                                      first_seen: int | None = None,
                                      require_track: bool = True,
                                      skip_fr24_summary: bool = False,
                                      last_seen: int | None = None,
                                      matched_callsign: str | None = None) -> dict:
    """Build a TripDraft from flight number + date, without any paid API."""
    flight, day = _service(flight_number, "flight"), _date(travel_date)
    if icao24 and first_seen is not None:
        selected = {
            "icao24": _normal(icao24).lower(), "callsign": matched_callsign,
            "first_seen": int(first_seen), "last_seen": last_seen,
            "identity_from_hint": True,
            "est_departure_airport": _normal(origin) or None,
            "est_arrival_airport": _normal(destination) or None,
            "departure_local": None, "timezone": (_airport_tz(origin).key if _airport_tz(origin) else CHINA.key),
        }
    else:
        try:
            candidates = (await query_opensky_flights(flight, day, origin, destination, icao24=icao24)
                          if icao24 else await query_opensky_flights(flight, day, origin, destination))
            if icao24:
                candidates = [row for row in candidates if _normal(row.get("icao24")) == _normal(icao24)]
            if len(candidates) > 1:
                raise ProviderError(409, "该日期有多个匹配航班，请选择具体航班后再导入。",
                                    {"candidates": candidates})
            selected = candidates[0] if candidates else None
        except ProviderError as exc:
            # A throttled or unauthenticated OpenSky account must not block the
            # configured FR24 chain, which resolves identity and track alone.
            if exc.status_code in (409, 422) or not fr24_date_available(day):
                raise
            if exc.status_code in (403, 404, 429):
                selected = None
            else:
                raise
        if selected is None and not fr24_date_available(day):
            raise ProviderError(404, "OpenSky 没有找到该航班号在出发机场当地日期的记录；请确认航班号与日期，"
                                    "或填写出发机场以加速查询。本次没有保存轨迹。")
        if selected is not None:
            # The global index often leaves mainland airports unestimated; the
            # user-supplied airports stand in for the missing estimates.
            selected["est_departure_airport"] = selected.get("est_departure_airport") or _normal(origin) or None
            selected["est_arrival_airport"] = selected.get("est_arrival_airport") or _normal(destination) or None
    path, path_source, trace_aircraft = None, None, {}
    if selected:
        selected["expected_callsigns"] = callsign_candidates(flight) + ([selected["callsign"]] if selected.get("callsign") else [])
    if selected and selected.get("icao24") and selected.get("first_seen") is not None:
        try:
            path = await query_opensky_track(selected["icao24"], selected["first_seen"])
        except ProviderError:
            # A throttled or unauthenticated track endpoint must not kill the
            # whole chain; FR24 and adsb.lol still have their own coverage.
            path = None
    if not path or len(path) < 2:
        path = None
    if path is None and require_track:
        # Try the configured FR24 history range, then small adsb.lol traces.
        # skip_fr24_summary lets the unified entry point avoid a second,
        # already-doomed summary request when FR24 was just tried and missed.
        if not skip_fr24_summary and fr24_date_available(day):
            try:
                draft = await finish_fr24_summary_draft(
                    await fetch_flight(flight, day, origin, destination),
                    selected.get("icao24") if selected else None)
            except ProviderError as exc:
                # A missing record (past the track window or never covered)
                # and transient throttling/outages fall through to adsb.lol;
                # 409/422 need the user's input first and must surface.
                if exc.status_code in (409, 422):
                    raise
            else:
                return draft
        found = await _fetch_adsblol_path(selected) if selected else None
        if found is not None:
            path, path_source = found[0], "adsblol"
            trace_aircraft = found[1]
        if path is None:
            identity = _unified_flight_info(flight, day, selected, None, {},
                selected.get("est_departure_airport"), selected.get("est_arrival_airport"), "") if selected else {}
            if identity and selected.get("identity_from_hint"):
                # A query window is not an observed/official flight time.
                # The unified caller retains any actual FR24 times separately.
                identity.pop("takeoff_time", None)
                identity.pop("landing_time", None)
            elif identity:
                identity["times_inferred"] = True
            raise ProviderError(404, "未获取到该航班当日的在线实际轨迹，历史范围取决于套餐、接收覆盖及存档可用性。",
                                {"flight_info": identity} if identity else None)
    if not require_track and fr24_date_available(day):
        # Identity-only enrichment: the FR24 full summary is the richest source
        # (callsign, hex, official runways, times, distances) whether or not
        # OpenSky resolved an identity, so try it before the OpenSky fallback.
        try:
            return await finish_fr24_summary_draft(
                await fetch_flight(flight, day, origin, destination))
        except ProviderError as exc:
            if exc.status_code in (409, 422):
                raise
    if selected is None:
        raise ProviderError(404, "未能定位该航班号当日的航班记录；请确认航班号与日期后重试。")
    departure, arrival = _infer_airports(path, selected.get("est_departure_airport"), selected.get("est_arrival_airport"))
    aircraft = await fetch_adsbdb_aircraft(selected["icao24"]) if selected["icao24"] else {}
    # The trace file's own registration/type outrank the registry lookup.
    aircraft = {**aircraft, **trace_aircraft}
    title_departure = departure or "??"
    title_arrival = arrival or "??"
    if path_source == "adsblol":
        source_name, track_source = "adsb.lol", "adsblol-globe-history"
        notes = ["实际飞行路径来自 adsb.lol 公开存档的原始 ADS-B 接收点，分辨率高于 OpenSky；仅覆盖地面站接收到的时段。",
                 "跑道为实际轨迹与机场跑道几何匹配的推断结果，仅供参考。"]
    else:
        source_name, track_source = "OpenSky", "opensky-tracks"
        notes = ["实际飞行路径来自 OpenSky 历史轨迹（约每 15 分钟及航向/高度变化处记录），并非原始逐秒 ADS-B 数据。",
                 "跑道为实际轨迹与机场跑道几何匹配的推断结果，仅供参考。"]
    draft = {
        "title": f"{flight} · {title_departure} → {title_arrival}",
        "mode": "flight", "travel_date": day.isoformat(), "service_number": flight,
        "origin": departure, "destination": arrival,
        "source": source_name, "source_crs": "WGS84", "geometry_crs": "WGS84",
        "geometry_kind": "observed",
        "points": _track_points(path) if path else [],
        "metadata": {
            "flight": _unified_flight_info(flight, day, selected, path, aircraft, departure, arrival, track_source),
            "altitude_kind": "barometric_pressure",
            "fetched_at": _iso(datetime.now(UTC)),
            "date_timezone": selected.get("timezone") or CHINA.key,
            "notes": notes,
        },
    }
    if path and selected.get("identity_from_hint"):
        draft["metadata"]["flight"].update(takeoff_time=_iso(_datetime(path[0][0])),
            landing_time=_iso(_datetime(path[-1][0])), times_inferred=True)
    # adsbdb's photo index misses most Chinese marks; fill from planespotters.
    await _fill_missing_photo(draft["metadata"]["flight"])
    flags = ["adsblol_reconstruction" if path_source == "adsblol" else "opensky_reconstruction"]
    if path and path_source == "opensky":
        flags.append("low_resolution_track")
    if draft["metadata"]["flight"]["takeoff_runway"] or draft["metadata"]["flight"]["landing_runway"]:
        flags.append("runway_inferred")
    draft["quality_flags"] = flags
    if path and (path[0][0] - selected["first_seen"] > 1800
                 or len({p["segment_id"] for p in draft["points"]}) > 1):
        flags.append("partial_observed_track")
        draft["metadata"]["notes"].append("在线观测仅覆盖部分航段；接收缺口保留分段，不插值或补造轨迹。")
    if not draft["points"]:
        draft["metadata"]["notes"].append("未能获取实际轨迹点；本次仅记录航班信息。")
    return draft


async def enrich_trip(store, trip_id: str, flight_number: str, travel_date: str | date,
                      origin: str | None = None, destination: str | None = None,
                      registration: str | None = None, icao24: str | None = None,
                      schedule_candidate_id: str | None = None,
                      first_seen: int | None = None, last_seen: int | None = None) -> dict | None:
    """Manually attach flight identity to an existing (e.g. CSV) trip.

    The observed points are never replaced; only missing metadata is filled
    from the reconstruction chain, so a wrong match cannot rewrite geometry.
    """
    flight, day = _service(flight_number, "flight"), _date(travel_date)
    base = store.get(trip_id)
    if base is None:
        return None
    previous = base.get("metadata", {}).get("flight", {})
    # With a supplied / saved airframe identity, do not require historical
    # track or flight-number APIs merely to retrieve its registry and photo.
    mark = registration or (None if icao24 else previous.get("registration"))
    hex_hint = icao24 or (None if registration else previous.get("icao24"))
    if (mark or hex_hint) and not (schedule_candidate_id or first_seen is not None):
        info = await lookup_aircraft_info(mark, hex_hint)
        info["flight_number"] = flight
    else:
        try:
            result = await fetch_flight_unified(flight, day, origin, destination, schedule_candidate_id=schedule_candidate_id,
                                               icao24=icao24, first_seen=first_seen, last_seen=last_seen)
            info = result["metadata"]["flight"]
        except ProviderError as exc:
            info = exc.details.get("flight_info", {}) if isinstance(exc.details, dict) else {}
            if exc.status_code in (202, 409, 422) or not any(info.get(key) for key in (
                    "registration", "icao24", "callsign", "takeoff_time", "aircraft_type", "photo_url")):
                raise

    def transform(record: dict) -> dict:
        metadata = record.setdefault("metadata", {})
        previous = metadata.get("flight") if isinstance(metadata.get("flight"), dict) else {}
        changed_airframe = ((info.get("icao24") and previous.get("icao24")
                             and _normal(info["icao24"]) != _normal(previous["icao24"]))
                            or (info.get("registration") and previous.get("registration")
                                and normalize_registration(info["registration"]) != normalize_registration(previous["registration"])))
        if changed_airframe:
            # Never keep a previous airframe's photo/type when the user corrects
            # the registration and the new registry has incomplete coverage.
            fields = {"registration", "icao24", "aircraft_type", "icao_type", "aircraft_manufacturer",
                      "registered_owner", "owner_country", "photo_url", "photo_thumbnail",
                      "photo_photographer", "photo_link", "identity_source", "identity_evidence"}
            previous = {key: value for key, value in previous.items() if key not in fields}
        elif info.get("photo_url") and info["photo_url"] != previous.get("photo_url"):
            # A replacement image must not inherit another image's attribution.
            previous = {key: value for key, value in previous.items()
                        if key not in {"photo_thumbnail", "photo_photographer", "photo_link"}}
        metadata["flight"] = {key: value for key, value in previous.items() if value not in (None, "")}
        metadata["flight"].update({key: value for key, value in info.items() if value not in (None, "")})
        if (mark or hex_hint) and previous.get("track_source") == "adsbdb-route":
            # Replace obsolete generated standing-route notices after an
            # independent aircraft lookup; retain user notes and geometry.
            notes = [note for note in metadata.get("notes", []) if "固定航线" not in note]
            message = "本次按提供的飞机标识补充飞机资料及可用照片；当日实际轨迹仍无数据。"
            if not record.get("points") and message not in notes:
                notes.append(message)
            metadata["notes"] = notes
        if not record.get("service_number"):
            record["service_number"] = flight
        if not record.get("origin") and info.get("departure_airport"):
            record["origin"] = info["departure_airport"]
        if not record.get("destination") and info.get("arrival_airport"):
            record["destination"] = info["arrival_airport"]
        return record

    return store.update_record(trip_id, transform)
