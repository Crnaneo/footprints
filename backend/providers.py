"""On-demand data adapters. Credentials stay in the local Python process.

RailGo supplies a current route reconstruction, not observed train GPS. Historical
boarding dates use the current route (flagged). When mapLine carries no curve,
each adjacent timetable stop pair is rebuilt from its unique shared local OSM
catalog line. Missing or ambiguous pairs are rejected; no station pair is
special-cased and gaps between legs remain separate segments.
"""
from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .rail_route_geometry import catalog_station_coordinate, unique_line_route

FR24_BASE = "https://fr24api.flightradar24.com/api"
RAILGO_BASE = "https://rg-api.zenglingkun.cn/api/v2"
UTC = timezone.utc
CHINA = ZoneInfo("Asia/Shanghai")
TripDraft = dict[str, Any]


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details


def _today() -> date:
    return datetime.now(CHINA).date()


def _date(value: str | date) -> date:
    try:
        return value if isinstance(value, date) else date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ProviderError(422, "日期格式应为 YYYY-MM-DD。") from None


def _normal(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _service(value: str, kind: str) -> str:
    value = _normal(value)
    if kind == "rail":
        return _rail_numbers(value)[0]
    pattern = r"[A-Z0-9]{2,3}\d{1,5}[A-Z]?" if kind == "flight" else r"[A-Z]?\d{1,5}"
    if not re.fullmatch(pattern, value):
        raise ProviderError(422, "请输入有效的航班号。" if kind == "flight" else "请输入有效的车次号。")
    return value


def _rail_numbers(value: str) -> list[str]:
    """Expand timetable aliases such as G2082/3 or K123/122."""
    parts = _normal(value).split("/")
    match = re.fullmatch(r"([A-Z]?)(\d{1,5})", parts[0])
    if not match or len(parts) > 4:
        raise ProviderError(422, "请输入有效的车次号。")
    prefix, digits = match.groups()
    numbers = [parts[0]]
    for part in parts[1:]:
        alias = re.fullmatch(r"([A-Z]?)(\d{1,5})", part)
        if not alias:
            raise ProviderError(422, "请输入有效的车次号。")
        code, suffix = alias.groups()
        numbers.append((code or prefix) + (digits[:-len(suffix)] + suffix if not code else suffix))
    return numbers


def _datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "false"):
            return normalized == "true"
    return None


def _coordinate(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (tuple, list)) or len(value) < 2:
        return None
    lon, lat = _number(value[0]), _number(value[1])
    if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


async def _get(client: httpx.AsyncClient, url: str, params: dict, provider: str) -> Any:
    try:
        response = await client.get(url, params=params)
    except httpx.TimeoutException:
        raise ProviderError(504, f"{provider} 请求超时，请稍后重试。") from None
    except httpx.RequestError:
        raise ProviderError(502, f"无法连接 {provider}，请检查网络后重试。") from None
    code = response.status_code
    if code == 429:
        raise ProviderError(429, f"{provider} 请求频率受限，请稍后再试。")
    if code in (401, 403):
        if provider == "Flightradar24 Sandbox":
            raise ProviderError(403, "沙箱密钥无效或无权访问，请使用 Key management 的 Sandbox API key 重新测试。")
        raise ProviderError(403, "Flightradar24 密钥无效或套餐无权查询该日期。请点击左上设置，检查官方 API 密钥并保存后重试，同时确认套餐权限与积分额度；也可直接导入航班 CSV。"
                            if provider == "Flightradar24" else f"{provider} 拒绝访问，请稍后重试。")
    if code == 402:
        raise ProviderError(402, "Flightradar24 积分不足，请在官方账户中检查额度。")
    if code in (400, 404):
        raise ProviderError(404, f"{provider} 未提供所查询日期的记录，可能无数据、当日未开行或超出套餐范围。")
    if code >= 400:
        raise ProviderError(502, f"{provider} 服务暂时不可用（HTTP {code}）。")
    try:
        payload = response.json()
    except ValueError:
        raise ProviderError(502, f"{provider} 返回了无法解析的数据。") from None
    if isinstance(payload, dict) and payload.get("success") is False:
        raise ProviderError(404, f"{provider} 没有匹配数据；本次没有保存轨迹。")
    return payload


@lru_cache(maxsize=1)
def _airports() -> dict[str, dict]:
    try:
        import airportsdata
        icao = airportsdata.load("ICAO")
        result = dict(icao)
        for airport in icao.values():
            if airport.get("iata"):
                result[airport["iata"]] = airport
        return result
    except ImportError:
        return {}


def _airport(value: Any) -> dict | None:
    return _airports().get(_normal(value))


def _airport_codes(value: Any) -> set[str]:
    code = _normal(value)
    data = _airport(code) or {}
    return {item for item in (code, data.get("icao"), data.get("iata")) if item}


def _airport_tz(value: Any) -> ZoneInfo | None:
    name = (_airport(value) or {}).get("tz")
    try:
        return ZoneInfo(name) if name else None
    except (ValueError, KeyError):
        return None


def _field(row: dict, *names: str) -> Any:
    return next((row[name] for name in names if row.get(name) is not None), None)


def _flight_candidate(row: dict, requested_date: date, flight: str,
                      origin: str | None, destination: str | None) -> dict | None:
    number = _normal(row.get("flight"))
    if number and flight not in re.split(r"[,/]", number):
        return None
    orig = _field(row, "orig_icao", "origin_icao", "orig_iata", "origin_iata")
    dest = _field(row, "dest_icao", "destination_icao", "dest_iata", "destination_iata")
    if origin and not (_airport_codes(origin) & _airport_codes(orig)):
        return None
    if destination and not (_airport_codes(destination) & _airport_codes(dest)):
        return None
    zone = _airport_tz(orig) or _airport_tz(origin)
    departure = _datetime(_field(row, "datetime_takeoff", "actual_off", "first_seen"))
    if departure is None or not row.get("fr24_id"):
        return None
    if zone is None:
        # Never silently interpret an unknown departure airport in Beijing time.
        raise ProviderError(422, "无法确认出发机场时区，不能可靠匹配当地日期。请填写正确的出发机场 IATA 或 ICAO 代码。")
    local = departure.astimezone(zone)
    if local.date() != requested_date:
        return None
    return {
        "fr24_id": str(row["fr24_id"]), "flight": row.get("flight") or flight,
        "origin": str(orig or origin or ""), "destination": str(dest or destination or ""),
        "icao24": _normal(row.get("hex")) or None,
        "departure": _iso(departure),
        "arrival": _iso(_datetime(_field(row, "datetime_landed", "actual_on"))),
        "departure_local": local.isoformat(), "timezone": zone.key,
        "flight_ended": _boolean(row.get("flight_ended")),
        "date_basis": "takeoff" if row.get("datetime_takeoff") or row.get("actual_off") else "first_seen",
        "summary": {key: row[key] for key in (
            "callsign", "reg", "type", "first_seen", "last_seen", "dest_icao_actual", "dest_iata_actual",
            # flight-summary/full extras: official runway, airline paint codes, IATA
            # airports, duration, flown vs great-circle distance and the category.
            "operating_as", "painted_as", "hex", "orig_iata", "dest_iata",
            "runway_takeoff", "runway_landed", "flight_time", "actual_distance", "circle_distance", "category",
        ) if key in row},
    }


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    value = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(value)))


def _flight_track_records(payload: Any) -> list:
    """Extract records from OpenAPI's object and the official SDK's array."""
    if isinstance(payload, dict) and "fr24_id" in payload:
        records = [payload]
    elif isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and "data" in payload:
        data = payload["data"]
        records = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        if data is not None and not isinstance(data, (list, dict)):
            raise ProviderError(502, "Flightradar24 轨迹响应格式不正确。")
    else:
        raise ProviderError(502, "Flightradar24 返回的轨迹与选定航班实例不匹配。")
    if not records:
        raise ProviderError(404, "Flightradar24 未提供该航班实例的轨迹；本次没有保存轨迹。")
    return records


def _normalize_flight_tracks(payload: Any, selected_id: str) -> list:
    """Normalize envelopes and match the exact summary-validated instance.

    The OpenAPI schema describes a single FlightTracks object, whereas the
    official Python SDK wraps response.json() as list[FlightTracks]. Both are
    supported, along with the common data-array envelope. Array order never
    determines which flight's positions are imported.
    """
    records = _flight_track_records(payload)
    if any(not isinstance(record, dict) or not record.get("fr24_id") for record in records):
        raise ProviderError(502, "Flightradar24 轨迹响应缺少有效航班实例标识。")
    matches = [record for record in records if str(record["fr24_id"]) == selected_id]
    if len(matches) != 1:
        raise ProviderError(502, "Flightradar24 返回的轨迹与选定航班实例不匹配或重复。")
    tracks = matches[0].get("tracks")
    if tracks is None:
        return []
    if not isinstance(tracks, list):
        raise ProviderError(502, "Flightradar24 轨迹响应格式不正确。")
    return tracks


def _flight_points(tracks: list) -> tuple[list[dict], list[str], dict]:
    if not isinstance(tracks, list):
        raise ProviderError(502, "Flightradar24 轨迹响应格式不正确。")
    rows = sorted((row for row in tracks if isinstance(row, dict)),
                  key=lambda row: _datetime(row.get("timestamp")) or datetime.max.replace(tzinfo=UTC))
    points, flags, gaps = [], set(), []
    segment, invalid, previous_time = 0, 0, None
    previous_coord = None
    for row in rows:
        coord = _coordinate([row.get("lon"), row.get("lat")])
        if coord is None:
            invalid += 1
            segment += 1
            previous_coord, previous_time = None, None
            continue
        instant = _datetime(row.get("timestamp"))
        if instant is None:
            # A missing source timestamp cannot establish chronology. Retain
            # the observed coordinate as an isolated point, rather than drawing
            # a fabricated connection after sorting all timestamped positions.
            segment += 1
            flags.add("missing_timestamps")
        if previous_time and instant and previous_coord:
            elapsed = (instant - previous_time).total_seconds()
            distance = _haversine(previous_coord, coord)
            if elapsed > 900 or (elapsed > 0 and distance / elapsed * 3600 > 1800) or (elapsed <= 0 and distance > 5):
                segment += 1
                gaps.append({"after_sequence": len(points) - 1, "seconds": elapsed, "distance_km": round(distance, 3)})
                flags.add("source_gaps")
        source = str(row.get("source") or "unknown")
        if "EST" in source.upper():
            flags.add("estimated_positions")
        altitude, speed = _number(row.get("alt")), _number(row.get("gspeed"))
        if speed is not None and speed < 0:
            speed = None
            flags.add("invalid_source_speed")
        points.append({
            "sequence": len(points), "segment_id": str(segment),
            "longitude": coord[0], "latitude": coord[1], "timestamp": _iso(instant),
            "altitude_m": altitude * 0.3048 if altitude is not None else None,
            "speed_kmh": speed * 1.852 if speed is not None else None,
            "source": source, "source_crs": "WGS84",
            "original_longitude": coord[0], "original_latitude": coord[1],
        })
        if instant is None:
            flags.add("missing_timestamps")
        previous_coord, previous_time = coord, instant
    if invalid:
        flags.add("invalid_source_points_skipped")
    if len(points) < 2:
        raise ProviderError(404, "未找到至少两个有效航班轨迹点；本次没有保存轨迹。")
    return points, sorted(flags), {"gaps": gaps, "invalid_point_count": invalid}


async def fetch_flight(flight_number: str, travel_date: str | date, origin: str | None = None,
                       destination: str | None = None, fr24_id: str | None = None) -> TripDraft:
    flight, day = _service(flight_number, "flight"), _date(travel_date)
    token = os.getenv("FR24_API_TOKEN", "").strip()
    if not token:
        raise ProviderError(503, "尚未配置 Flightradar24 API 密钥。请点击左上设置，粘贴 Flightradar24 官方 API 密钥并保存后重试；也可直接导入航班 CSV。")
    today = _today()
    try:
        earliest = today.replace(year=today.year - 3)
    except ValueError:
        earliest = today.replace(year=today.year - 3, day=28)
    if day < earliest:
        raise ProviderError(404, "该日期超出 Flightradar24 完整轨迹接口的三年范围。更早位置快照尚未自动采集，请使用已有 CSV；本次没有保存轨迹。")
    origin, destination = _normal(origin) or None, _normal(destination) or None
    for code in (origin, destination):
        if code and not re.fullmatch(r"[A-Z0-9]{3,4}", code):
            raise ProviderError(422, "机场请填写三位 IATA 或四位 ICAO 代码，例如 PEK 或 ZBAA。")
    zone = _airport_tz(origin)
    if zone:
        start = datetime.combine(day, time.min, zone).astimezone(UTC) - timedelta(hours=6)
        end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC) + timedelta(hours=6)
    else:
        # A broad UTC window covers all departure-airport local dates. Every
        # result is subsequently filtered using that airport's own time zone.
        start = datetime.combine(day - timedelta(days=1), time.min, UTC)
        end = datetime.combine(day + timedelta(days=2), time.min, UTC)
    headers = {"Authorization": f"Bearer {token}", "Accept-Version": "v1", "Accept": "application/json"}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
        payload = await _get(client, f"{FR24_BASE}/flight-summary/full", {
            # full over light: same resource plus official runways, flight time,
            # flown/great-circle distance, IATA codes and the flight category.
            "flights": flight, "flight_datetime_from": _iso(start),
            "flight_datetime_to": _iso(end), "limit": 20,
        }, "Flightradar24")
        rows = payload.get("data", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ProviderError(502, "Flightradar24 航班摘要响应格式不正确。")
        candidates = []
        for row in rows:
            if isinstance(row, dict):
                candidate = _flight_candidate(row, day, flight, origin, destination)
                if candidate and all(candidate["fr24_id"] != item["fr24_id"] for item in candidates):
                    candidates.append(candidate)
        if fr24_id:
            candidates = [candidate for candidate in candidates if candidate["fr24_id"] == str(fr24_id)]
        if not candidates:
            raise ProviderError(404, "未找到符合航班号、出发机场当地日期及到发机场的航班实例；本次没有保存轨迹。")
        if len(candidates) > 1:
            raise ProviderError(409, "该日期有多个匹配航段，请选择具体航班后再导入。", {"candidates": candidates})
        selected = candidates[0]
        try:
            payload = await _get(client, f"{FR24_BASE}/flight-tracks", {"flight_id": selected["fr24_id"]}, "Flightradar24")
            points, flags, track_metadata = _flight_points(_normalize_flight_tracks(payload, selected["fr24_id"]))
        except ProviderError as exc:
            # Identity survives missing/forbidden/invalid track responses. The
            # next provider can use its hex and actual flight window.
            details = dict(exc.details) if isinstance(exc.details, dict) else {}
            details["fr24_flight"] = selected
            raise ProviderError(exc.status_code, exc.message, details) from exc
    if selected["flight_ended"] is False:
        flags.append("partial_live_track")
    if selected["date_basis"] == "first_seen":
        flags.append("date_from_first_seen")
    return {
        "title": f"{flight} · {selected['origin']} → {selected['destination']}",
        "mode": "flight", "travel_date": day.isoformat(), "service_number": flight,
        "origin": selected["origin"], "destination": selected["destination"],
        "source": "Flightradar24", "source_crs": "WGS84", "geometry_crs": "WGS84",
        "geometry_kind": "observed", "quality_flags": flags, "points": points,
        "metadata": {"flight": selected, "track": track_metadata, "altitude_kind": "barometric_pressure",
                     "fetched_at": _iso(datetime.now(UTC)), "date_timezone": selected["timezone"],
                     "notes": ["轨迹仅包含数据源提供的位置；分段保留观测缺口。"]},
    }


async def fetch_flight_sandbox(token: str) -> TripDraft:
    """Fetch one explicit, clearly marked official sandbox position specimen.

    Sandbox endpoints return fixed, unrelated per-endpoint examples. In
    particular, the summary's SK1415 ID does not match the THY5LK tracks ID,
    so this path intentionally uses only tracks and invents no flight number
    or airports. It never reads or changes the production token environment.
    """
    token = token.strip()
    if not token or not token.isascii() or any(character in token for character in "\r\n"):
        raise ProviderError(422, "请输入有效的 Flightradar24 官方沙箱 API 密钥。")
    headers = {"Authorization": f"Bearer {token}", "Accept-Version": "v1", "Accept": "application/json"}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
        # This is an official fixed specimen's valid ID, not a requested trip.
        # Sandbox responses may ignore it; the returned ID remains authoritative.
        payload = await _get(client, f"{FR24_BASE}/flight-tracks", {"flight_id": "35dc6f7d"}, "Flightradar24 Sandbox")
    samples = []
    for record in _flight_track_records(payload):
        if not isinstance(record, dict):
            continue
        sample_id = str(record.get("fr24_id") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{6,16}", sample_id):
            continue
        try:
            points, flags, track_metadata = _flight_points(record.get("tracks"))
        except ProviderError:
            continue
        first_instant = next((_datetime(point["timestamp"]) for point in points if point["timestamp"]), None)
        if first_instant is None:
            continue
        samples.append((record, sample_id, points, flags, track_metadata, first_instant))
    if not samples:
        raise ProviderError(404, "官方沙箱没有返回至少两个有效位置点及可用时间戳；本次没有保存示例。")
    if len(samples) != 1:
        raise ProviderError(409, "官方沙箱返回多个有效轨迹样例，无法唯一确定示例；本次没有混合或保存轨迹。")
    record, sample_id, points, flags, track_metadata, first_instant = samples[0]
    callsigns = sorted({_normal(row.get("callsign")) for row in record["tracks"]
                       if isinstance(row, dict) and _normal(row.get("callsign")) and _coordinate([row.get("lon"), row.get("lat")])})
    callsign = callsigns[0] if len(callsigns) == 1 else None
    return {
        "title": f"沙箱示例 · {callsign or sample_id}", "mode": "flight",
        "travel_date": first_instant.date().isoformat(), "service_number": None,
        "origin": None, "destination": None, "source": "Flightradar24 Sandbox",
        "source_crs": "WGS84", "geometry_crs": "WGS84", "geometry_kind": "observed",
        "quality_flags": ["sandbox_sample", *flags], "points": points,
        "notes": "官方沙箱固定位置样例，仅用于功能测试，不代表你的行程或完整航班；出行日期取首点 UTC 日期。",
        "metadata": {"sandbox": True, "fr24_id": sample_id, "callsign": callsign,
                     "date_basis": "first_point_utc", "date_timezone": "UTC", "track": track_metadata,
                     "fetched_at": _iso(datetime.now(UTC)), "altitude_kind": "barometric_pressure",
                     "source_url": "https://fr24api.flightradar24.com/docs/sandbox-environment"},
    }


def _gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    """Iterative approximate inverse, retaining the original GCJ-02 values."""
    if not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return lon, lat

    def forward(x: float, y: float) -> tuple[float, float]:
        a, b = x - 105, y - 35
        dlat = -100 + 2*a + 3*b + .2*b*b + .1*a*b + .2*math.sqrt(abs(a))
        dlat += (20*math.sin(6*a*math.pi) + 20*math.sin(2*a*math.pi))*2/3
        dlat += (20*math.sin(b*math.pi) + 40*math.sin(b/3*math.pi))*2/3
        dlat += (160*math.sin(b/12*math.pi) + 320*math.sin(b*math.pi/30))*2/3
        dlon = 300 + a + 2*b + .1*a*a + .1*a*b + .1*math.sqrt(abs(a))
        dlon += (20*math.sin(6*a*math.pi) + 20*math.sin(2*a*math.pi))*2/3
        dlon += (20*math.sin(a*math.pi) + 40*math.sin(a/3*math.pi))*2/3
        dlon += (150*math.sin(a/12*math.pi) + 300*math.sin(a/30*math.pi))*2/3
        rad = math.radians(y)
        magic = 1 - .00669342162296594323*math.sin(rad)**2
        dlat = dlat*180/((6378245*(1-.00669342162296594323))/(magic*math.sqrt(magic))*math.pi)
        dlon = dlon*180/(6378245/math.sqrt(magic)*math.cos(rad)*math.pi)
        return x + dlon, y + dlat

    guess_lon, guess_lat = lon, lat
    for _ in range(8):
        test_lon, test_lat = forward(guess_lon, guess_lat)
        dx, dy = test_lon - lon, test_lat - lat
        guess_lon, guess_lat = guess_lon - dx, guess_lat - dy
        if max(abs(dx), abs(dy)) < 1e-8:
            break
    return guess_lon, guess_lat


def _station_name(value: Any) -> str:
    return str(value or "").strip().removesuffix("站")


def _station_index(timetable: list[dict], name: str, label: str = "车站") -> int:
    matches = [i for i, stop in enumerate(timetable) if _station_name(stop.get("station")) == _station_name(name)
               or _normal(stop.get("stationTelecode")) == _normal(name)]
    if len(matches) != 1:
        raise ProviderError(422, f"{label}「{name}」不存在于该车次停站表中。")
    return matches[0]


def _stop_day(stop: dict, departing: bool) -> int:
    try:
        result = int(stop.get("day", 0))
    except (TypeError, ValueError):
        raise ProviderError(502, "RailGo 停站日序格式不正确。") from None
    arrival, departure = str(stop.get("arrive", "")), str(stop.get("depart", ""))
    if departing and re.fullmatch(r"\d{2}:\d{2}", arrival) and re.fullmatch(r"\d{2}:\d{2}", departure) and departure < arrival:
        result += 1
    return result


def _stop_instant(stop: dict, start_day: date, departing: bool) -> str | None:
    raw = stop.get("depart" if departing else "arrive")
    try:
        clock = time.fromisoformat(str(raw))
        instant = datetime.combine(start_day + timedelta(days=_stop_day(stop, departing)), clock, CHINA)
        return instant.isoformat()
    except (ValueError, TypeError):
        return None


def _schedule_signature(stops: list[dict]) -> list[tuple]:
    return [(stop.get("station"), stop.get("day"), stop.get("arrive"), stop.get("depart")) for stop in stops]


def _rail_data(payload: Any, start_day: date) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("timetable"), list) or len(data["timetable"]) < 2:
        raise ProviderError(404, "RailGo 未提供该日期完整的车次停站表。")
    rundays = data.get("rundays")
    if not isinstance(rundays, list) or start_day.strftime("%Y%m%d") not in rundays:
        raise ProviderError(404, "RailGo 未确认该始发日期开行；本次没有保存轨迹。")
    if any(not isinstance(stop, dict) or not stop.get("station") for stop in data["timetable"]):
        raise ProviderError(502, "RailGo 停站表格式不正确。")
    return data


def _rail_line(data: dict, timetable: list[dict]) -> tuple[list[tuple[float, float, str]], dict]:
    station_coords = {}
    rawstations = data.get("stations")
    if not isinstance(rawstations, list):
        raise ProviderError(502, "RailGo 线路站点响应格式不正确。")
    for item in rawstations:
        if isinstance(item, dict):
            for name, coord in item.items():
                parsed = _coordinate(coord)
                if parsed:
                    station_coords[_station_name(name)] = parsed
    # mapLine's station list can be incomplete or unordered. The dated
    # timetable and ordered projections onto the curve determine stop order.
    sections = data.get("train")
    if not isinstance(sections, dict):
        raise ProviderError(502, "RailGo 线路响应格式不正确。")
    try:
        indices = [section["index"] for section in sections.values()]
        if any(isinstance(index, bool) or not re.fullmatch(r"\d+", str(index)) for index in indices):
            raise ValueError("invalid section index")
        if len({int(index) for index in indices}) != len(indices):
            raise ValueError("duplicate section index")
        ordered = sorted(sections.items(), key=lambda item: int(item[1]["index"]))
    except (TypeError, KeyError, ValueError):
        raise ProviderError(502, "RailGo 线路区段缺少有效顺序。") from None
    points = []
    anchor = station_coords.get(_station_name(timetable[0]["station"]))
    if anchor is None and ordered:
        raise ProviderError(404, "线路曲线缺少始发站坐标，无法确认运行方向。")
    for name, section in ordered:
        rawcoords = section.get("line")
        if not isinstance(rawcoords, list):
            raise ProviderError(502, "RailGo 线路区段含无效坐标；本次没有保存轨迹。")
        coords = [_coordinate(coord) for coord in rawcoords]
        if len(coords) < 2 or any(coord is None for coord in coords):
            raise ProviderError(502, "RailGo 线路区段含无效坐标；本次没有保存轨迹。")
        if _haversine(anchor, coords[-1]) < _haversine(anchor, coords[0]):
            coords.reverse()
        points.extend((coord[0], coord[1], str(section["index"])) for coord in coords)
        anchor = coords[-1]
    # No straight-line fallback: when RailGo returns station coordinates but no
    # route sections, points stays empty and fetch_rail rebuilds the ride from the
    # single catalog line joining the two stations, or rejects the import.
    return points, station_coords


def _projections(points: list[tuple], target: tuple) -> list[tuple[float, float, tuple]]:
    """Project a station onto source edges, without joining source sections."""
    scale = math.cos(math.radians(target[1]))
    candidates = []
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        if a[2] != b[2]:
            continue
        dx, dy = (b[0] - a[0])*scale, b[1] - a[1]
        denom = dx*dx + dy*dy
        t = max(0, min(1, (((target[0] - a[0])*scale)*dx + (target[1] - a[1])*dy)/denom)) if denom else 0
        coord = (a[0] + t*(b[0] - a[0]), a[1] + t*(b[1] - a[1]), a[2])
        distance = _haversine(target, coord[:2])
        if distance <= 3:
            candidates.append((i + t, distance, coord))
    # Keeping all nearby matches matters on loops and overlapping railways:
    # truncating to the closest few edges can discard the only occurrence that
    # follows the preceding station in the train's actual stop order.
    return sorted(candidates, key=lambda item: item[0])


def _ordered_snaps(points: list[tuple], stations: list[tuple]) -> list[tuple]:
    states = []
    for target in stations:
        candidates = _projections(points, target)
        next_states = []
        for candidate in candidates:
            if not states:
                next_states.append((candidate[1]**2, [candidate]))
            else:
                parents = [state for state in states if state[1][-1][0] < candidate[0]]
                if parents:
                    cost, path = min(parents, key=lambda item: item[0])
                    next_states.append((cost + candidate[1]**2, path + [candidate]))
        if not next_states:
            raise ProviderError(404, "无法按列车停站顺序可靠匹配线路曲线；本次没有保存轨迹。")
        states = next_states
    return min(states, key=lambda item: item[0])[1]


async def fetch_rail(train_number: str, travel_date: str | date, origin: str, destination: str) -> TripDraft:
    train, day, today = _service(train_number, "rail"), _date(travel_date), _today()
    requested_numbers = _rail_numbers(train_number)
    is_historical = day < today
    query_date = today if is_historical else day
    if day > today + timedelta(days=14):
        raise ProviderError(404, "该日期超出当前线路可核验的近期范围（14天）；请临近出发日期再查询。")
    if not str(origin or "").strip() or not str(destination or "").strip():
        raise ProviderError(422, "铁路导入需要填写上车站与下车站。")
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
        payload = await _get(client, f"{RAILGO_BASE}/getTrainMain", {"trainNum": train, "date": query_date.strftime("%Y%m%d")}, "RailGo")
        data = _rail_data(payload, query_date)
        timetable = data["timetable"]
        if not set(requested_numbers).issubset({_normal(number) for number in data.get("numberFull", [])} | {_normal(stop.get("trainCode")) for stop in timetable}):
            raise ProviderError(404, "RailGo 返回的车次与所查询车次不一致。")
        first, last = _station_index(timetable, origin, "上车站"), _station_index(timetable, destination, "下车站")
        if first >= last:
            raise ProviderError(422, "下车站必须位于上车站之后，请检查该车次的运行方向。")
        start_day = query_date - timedelta(days=_stop_day(timetable[first], True))
        if start_day != query_date:
            payload = await _get(client, f"{RAILGO_BASE}/getTrainMain", {"trainNum": train, "date": start_day.strftime("%Y%m%d")}, "RailGo")
            selected = _rail_data(payload, start_day)
            if _schedule_signature(selected["timetable"]) != _schedule_signature(timetable):
                raise ProviderError(404, "跨夜车次的始发日停站表与上车日不一致，无法可靠确认本次乘车。")
            data, timetable = selected, selected["timetable"]
        if query_date != today:
            payload = await _get(client, f"{RAILGO_BASE}/getTrainMain", {"trainNum": train, "date": today.strftime("%Y%m%d")}, "RailGo")
            current = _rail_data(payload, today)
            if _schedule_signature(current["timetable"]) != _schedule_signature(timetable):
                raise ProviderError(404, "指定日期与当前运行图不同，无法可靠使用当前线路曲线。")
        if query_date != (start_day + timedelta(days=_stop_day(timetable[first], True))):
            raise ProviderError(404, "无法确认上车日期与列车始发日的对应关系。")
        payload = await _get(client, f"{RAILGO_BASE}/mapLine", {"train": train}, "RailGo")
    mapdata = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(mapdata, dict):
        raise ProviderError(502, "RailGo 线路响应格式不正确。")
    rawpoints, station_coords = _rail_line(mapdata, timetable)
    selected_stops = [{**stop, "arrival_local": _stop_instant(stop, start_day, False),
                       "departure_local": _stop_instant(stop, start_day, True)} for stop in timetable[first:last + 1]]
    if len(rawpoints) >= 2:
        # RailGo supplied the current route curve: clip it to the boarding span.
        # Compare geometry in WGS84 so missing map stations can use catalog
        # coordinates without mixing the two coordinate systems.
        normalized = [(*_gcj02_to_wgs84(lon, lat), segment) for lon, lat, segment in rawpoints]
        targets = []
        for stop in selected_stops:
            name = _station_name(stop["station"])
            coord = station_coords.get(name)
            target = _gcj02_to_wgs84(*coord) if coord else catalog_station_coordinate(name)
            if target is None:
                raise ProviderError(404, f"缺少停站「{name}」的可靠坐标，无法核验线路曲线。")
            targets.append(target)
        normalized_snaps = _ordered_snaps(normalized, targets)
        # Interpolate the same source edge in the original coordinate system.
        snaps = []
        for position, distance, coord in normalized_snaps:
            index = min(math.floor(position), len(rawpoints) - 2)
            fraction = position - index
            a, b = rawpoints[index:index + 2]
            snaps.append((position, distance, (a[0] + fraction * (b[0] - a[0]),
                                               a[1] + fraction * (b[1] - a[1]), coord[2])))
        start, end = snaps[0], snaps[-1]
        clipped = [start[2]]
        clipped.extend(rawpoints[i] for i in range(math.floor(start[0]) + 1, math.ceil(end[0])) if start[0] < i < end[0])
        clipped.append(end[2])
        # At an exact section boundary, an endpoint can belong to either adjacent
        # section. Keep the boarding endpoint in the following section and the
        # alighting endpoint in the preceding one, avoiding singleton segments.
        while len(clipped) > 2 and clipped[0][:2] == clipped[1][:2]:
            clipped.pop(0)
        while len(clipped) > 2 and clipped[-1][:2] == clipped[-2][:2]:
            clipped.pop()
        points = []
        for lon, lat, segment in clipped:
            normalized_lon, normalized_lat = _gcj02_to_wgs84(lon, lat)
            if points and (lon, lat, segment) == (points[-1]["original_longitude"], points[-1]["original_latitude"], points[-1]["segment_id"]):
                continue
            points.append({"sequence": len(points), "segment_id": segment, "longitude": normalized_lon,
                           "latitude": normalized_lat, "original_longitude": lon, "original_latitude": lat,
                           "timestamp": None, "altitude_m": None, "speed_kmh": None,
                           "source": "RailGo", "source_crs": "GCJ02"})
        if len(points) < 2:
            raise ProviderError(404, "到发站区间缺少有效轨迹点。")
        source_crs, catalog_line = "GCJ02", None
        source_point_count, source_section_count = len(rawpoints), len(mapdata["train"])
        snap_distances = {stop["station"]: round(snap[1], 4) for stop, snap in zip(selected_stops, snaps)}
        coordinate_conversion = "GCJ-02 to WGS84 iterative approximate inverse; original coordinates retained"
        geometry_flags = ["current_route_reconstruction", "approximate_crs_conversion"]
        geometry_notes = ["按停站顺序核验并裁剪的当前铁路经由重建路径，并非列车实测 GPS。"]
    else:
        # A through train may change infrastructure lines at intermediate
        # stops. Resolve every dated stop pair, rather than requiring one line
        # to span the entire ride or trusting mapLine's incomplete stop list.
        targets = []
        for stop in selected_stops:
            name = _station_name(stop["station"])
            coord = station_coords.get(name)
            target = _gcj02_to_wgs84(*coord) if coord else catalog_station_coordinate(name)
            if target is None:
                raise ProviderError(404, f"无经由信息不支持导入：缺少停站「{name}」的可靠坐标。")
            targets.append(target)
        points = []
        line_names = []
        for i, (start, end) in enumerate(zip(targets, targets[1:])):
            route = unique_line_route(start, end)
            if route is None:
                raise ProviderError(404, f"无经由信息不支持导入：{selected_stops[i]['station']} → {selected_stops[i + 1]['station']} 无唯一可用线路。")
            coords, line_name = route
            line_names.append(line_name)
            for lon, lat in coords:
                if points and points[-1]["segment_id"] == str(i) and (lon, lat) == (points[-1]["longitude"], points[-1]["latitude"]):
                    continue
                points.append({"sequence": len(points), "segment_id": str(i), "longitude": lon,
                               "latitude": lat, "original_longitude": lon, "original_latitude": lat,
                               "timestamp": None, "altitude_m": None, "speed_kmh": None,
                               "source": "OSM", "source_crs": "WGS84"})
        if len(points) < 2:
            raise ProviderError(404, "无经由信息不支持导入")
        source_crs, catalog_line = "WGS84", " → ".join(dict.fromkeys(line_names))
        source_point_count, source_section_count = len(points), 0
        snap_distances = {}
        coordinate_conversion = "OSM catalog geometry is native WGS84; no conversion applied"
        geometry_flags = ["catalog_line_reconstruction"]
        geometry_notes = ["RailGo 未提供线路曲线；已按时刻表停站顺序逐段匹配本地 OSM 铁路目录中唯一连接相邻停站的线路，并非列车实测 GPS。",
                          "保留本地 OSM 原始线路节点，未简化或插值补点；缺失线路几何不会补画。",
                          f"还原所用线路：{catalog_line}"]
    gaps = []
    for before, after in zip(points, points[1:]):
        if before["segment_id"] != after["segment_id"]:
            distance = _haversine((before["original_longitude"], before["original_latitude"]),
                                  (after["original_longitude"], after["original_latitude"]))
            if distance > 0.01:
                gaps.append({"after_sequence": before["sequence"], "distance_km": round(distance, 4)})
    flags = geometry_flags + ["no_observed_speed"]
    if is_historical:
        flags.insert(0, "historical_date_reconstruction")
    if gaps:
        flags.append("source_gaps")
    return {
        "title": f"{train} · {timetable[first]['station']} → {timetable[last]['station']}",
        "mode": "rail", "travel_date": day.isoformat(), "service_number": train,
        "origin": timetable[first]["station"], "destination": timetable[last]["station"],
        "source": "RailGo", "source_crs": source_crs, "geometry_crs": "WGS84",
        "geometry_kind": "reconstructed", "points": points,
        "quality_flags": flags,
        "metadata": {"source_url": "https://api.railgo.dev/479769351e0", "fetched_at": _iso(datetime.now(UTC)),
                     "route_snapshot_date": today.isoformat(), "train_start_date": start_day.isoformat(),
                     "source_point_count": source_point_count, "source_section_count": source_section_count,
                     "source_section_gaps": gaps, "catalog_line": catalog_line,
                     "date_timezone": "Asia/Shanghai", "timetable": selected_stops,
                     "train": {key: data.get(key) for key in ("car", "runner", "bureauShortName", "numberFull")},
                     "station_snap_distances_km": snap_distances,
                     "coordinate_conversion": coordinate_conversion,
                     "notes": (["历史日期：将根据今日该车次经由导入轨迹，并非当日实测路径。"] if is_historical else []) + geometry_notes +
                  ["没有逐点运行时间、实测速度或可靠的线路最高运行速度；缺失字段保留为空。",
                   "RailGo 数据仅供非商业个人使用，请保留来源标注。"]},
    }
