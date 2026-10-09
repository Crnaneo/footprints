"""Dated ADS-B flight identities from MrAirspace (ODbL-1.0).

Quarterly files are cached once, never confused with a full observed track.
Standing routes may resolve a callsign, but never supply historical airports.
"""
from __future__ import annotations

import asyncio
import csv
import errno
import os
import hashlib
import io
import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import httpx

from .providers import ProviderError, UTC, _airport, _airport_codes, _airport_tz, _datetime, _iso, _normal, _number
from .store import default_database_path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

REPOSITORY = "https://github.com/MrAirspace/aircraft-flight-schedules"
RELEASES = "https://api.github.com/repos/MrAirspace/aircraft-flight-schedules/releases"
AIRLINES = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"
_tasks: dict[str, asyncio.Task] = {}
_guard = threading.Lock()


def cache_dir() -> Path:
    root = default_database_path().parent / "flight_schedules"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, value) -> None:
    # Atomic, also when the two local service ports prepare the same quarter.
    import uuid
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def split_flight(flight: str) -> tuple[str, str]:
    value = _normal(flight)
    # Three letters explicitly supplied means an ICAO callsign. Otherwise the
    # first two characters form IATA, including 3U, U2, 6E and 9C.
    match = re.fullmatch(r"([A-Z]{3})(\d{1,5}[A-Z]?)", value)
    if not match:
        match = re.fullmatch(r"([A-Z0-9]{2})(\d{1,5}[A-Z]?)", value)
    if not match:
        raise ProviderError(422, "请输入有效的航班号。")
    return match.group(1), match.group(2)


async def resolve_callsigns(flight: str, fallback: dict[str, str]) -> list[str]:
    prefix, suffix = split_flight(flight)
    if len(prefix) == 3:
        return [prefix + suffix]
    path = cache_dir() / "airlines.json"
    saved = _read(path)
    if not saved or time.time() - saved.get("updated", 0) > 30 * 86400:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                response = await client.get(AIRLINES)
                response.raise_for_status()
            mapping: dict[str, list[str]] = {}
            rows = list(csv.reader(io.StringIO(response.text)))
            for row in sorted(rows, key=lambda row: len(row) < 8 or row[7] != "Y"):
                if len(row) >= 5 and re.fullmatch(r"[A-Z0-9]{2}", row[3]) and re.fullmatch(r"[A-Z]{3}", row[4]):
                    values = mapping.setdefault(row[3], [])
                    if row[4] not in values:
                        values.append(row[4])
            if mapping:
                saved = {"updated": time.time(), "mapping": mapping, "source": AIRLINES}
                _write(path, saved)
        except (httpx.HTTPError, OSError, ValueError):
            pass
    prefixes = list((saved or {}).get("mapping", {}).get(prefix, []))
    if not prefixes:
        online_path = cache_dir() / f"airline-{prefix}.json"
        online = _read(online_path)
        if not online or time.time() - online.get("updated", 0) > 30 * 86400:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(f"https://api.adsbdb.com/v0/airline/{prefix}")
                    response.raise_for_status()
                    rows = response.json().get("response")
                if isinstance(rows, dict):rows = [rows]
                values = [row.get("icao", "") for row in (rows or []) if isinstance(row, dict)
                          and row.get("iata") == prefix and re.fullmatch(r"[A-Z]{3}", row.get("icao", ""))]
                online = {"updated": time.time(), "prefixes": values}
                _write(online_path, online)
            except (httpx.HTTPError, OSError, ValueError, TypeError):
                pass
        prefixes.extend((online or {}).get("prefixes", []))
    if prefix in fallback:prefixes.append(fallback[prefix])
    return list(dict.fromkeys([p + suffix for p in prefixes] + [flight]))


async def alternate_callsign(flight: str) -> str | None:
    """Use only the alias, never the standing route's origin/destination."""
    path = cache_dir() / f"callsign-{flight}.json"
    saved = _read(path)
    if saved and time.time() - saved.get("updated", 0) < 7 * 86400:
        return saved.get("callsign")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"https://api.adsbdb.com/v0/callsign/{flight}")
            response.raise_for_status()
            row = response.json().get("response", {}).get("flightroute", {})
        result = _normal(row.get("callsign_icao"))
        if not re.fullmatch(r"[A-Z]{3}[A-Z0-9]{1,5}", result):result = None
        _write(path, {"updated": time.time(), "callsign": result})
        return result
    except (httpx.HTTPError, ValueError, OSError, TypeError, AttributeError):
        return None


def quarters(day: date) -> set[tuple[int, int]]:
    # UTC+14 through UTC-12, including a departure with a late first reception.
    start = datetime.combine(day, datetime.min.time(), UTC) - timedelta(hours=14)
    end = start + timedelta(hours=50)
    return {(value.year, (value.month - 1) // 3 + 1) for value in (start, end)}


async def release_assets(day: date, *, trust_env: bool = True) -> list[dict]:
    path = cache_dir() / "releases.json"
    saved = _read(path)
    if not saved or time.time() - saved.get("updated", 0) > 6 * 3600:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, trust_env=trust_env) as client:
                releases = []
                for page in range(1, 5):
                    response = await client.get(RELEASES, params={"per_page": 100, "page": page})
                    response.raise_for_status()
                    batch = response.json()
                    if not isinstance(batch, list):break
                    releases.extend(batch)
                    if len(batch) < 100:break
            assets = []
            for release in releases:
                if release.get("draft") or release.get("prerelease"):continue
                for asset in release.get("assets", []):
                    match = re.fullmatch(r"(20\d{2})_Q([1-4])_(?:detailed_)?github\.parquet", asset.get("name", ""))
                    if match:
                        assets.append({"year": int(match[1]), "quarter": int(match[2]),
                                       "name": asset["name"], "size": asset["size"],
                                       "url": asset["browser_download_url"], "digest": asset.get("digest"),
                                       "updated_at": asset.get("updated_at"), "release_url": release.get("html_url")})
            saved = {"updated": time.time(), "assets": assets}
            _write(path, saved)
        except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError):
            if not saved:raise ProviderError(503, "MrAirspace 发布目录暂不可用，继续查询其它来源。") from None
    found = {}
    for asset in saved.get("assets", []):
        key = asset["year"], asset["quarter"]
        if key in quarters(day) and (key not in found or "detailed" in asset["name"]):
            found[key] = asset
    return [found[key] for key in sorted(found)]


def _valid(path: Path, asset: dict) -> bool:
    try:
        return path.stat().st_size == asset["size"] and _read(path.with_suffix(".source.json")) == asset
    except OSError:
        return False


def cancel_download(asset: dict) -> None:
    path = cache_dir() / asset["name"]
    if _valid(path, asset):return
    path.with_suffix(".cancel").touch()
    _write(path.with_suffix(".status.json"), {"state": "cancelled", "asset": asset, "updated": time.time()})


def _lock_download(lock) -> None:
    if os.name == "nt":
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError from exc
            raise
    else:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _download(asset: dict, *, trust_env: bool = True) -> None:
    path = cache_dir() / asset["name"]
    status = path.with_suffix(".status.json")
    # Closing the descriptor releases the lock on both platforms.
    with path.with_suffix(".lock").open("a+b") as lock:
        try:
            _lock_download(lock)
        except BlockingIOError:
            return  # The other local service owns the same download.
        if _valid(path, asset):return
        partial = path.with_suffix(".part")
        cancelled = path.with_suffix(".cancel")
        try:
            if cancelled.exists():return
            # Restart on a changed asset. Never append two revisions together.
            state = _read(status) or {}
            if state.get("asset") != asset and partial.exists():partial.unlink()
            offset = partial.stat().st_size if partial.exists() else 0
            _write(status, {"state": "downloading", "asset": asset, "updated": time.time()})
            if offset > asset["size"]:
                partial.unlink()
                offset = 0
            if offset != asset["size"]:
                with httpx.Client(timeout=httpx.Timeout(45, connect=15), follow_redirects=True, trust_env=trust_env) as client:
                    with client.stream("GET", asset["url"], headers={"Range": f"bytes={offset}-"} if offset else {}) as response:
                        response.raise_for_status()
                        if response.status_code != 206:offset = 0
                        elif not response.headers.get("content-range", "").startswith(f"bytes {offset}-"):
                            raise ValueError("Invalid download range")
                        with partial.open("ab" if offset else "wb") as output:
                            for chunk in response.iter_bytes(64 * 1024):
                                if cancelled.exists():return
                                output.write(chunk)
            if cancelled.exists():return
            if partial.stat().st_size != asset["size"]:raise ValueError("Incomplete dataset")
            digest = asset.get("digest")
            if digest and digest.startswith("sha256:"):
                with partial.open("rb") as source:
                    actual = hashlib.file_digest(source, "sha256").hexdigest()
                if actual != digest[7:]:
                    partial.unlink()
                    raise ValueError("Dataset checksum mismatch")
            with duckdb.connect() as con:
                con.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(partial)])
            if cancelled.exists():return
            partial.replace(path)
            _write(path.with_suffix(".source.json"), asset)
            _write(status, {"state": "ready", "asset": asset, "updated": time.time()})
        except (httpx.HTTPError, OSError, ValueError, duckdb.Error):
            if not cancelled.exists():
                _write(status, {"state": "failed", "asset": asset, "updated": time.time()})
        finally:
            if cancelled.exists():
                _write(status, {"state": "cancelled", "asset": asset, "updated": time.time()})


async def ensure_dataset(asset: dict, *, resume: bool = True) -> Path | None:
    path = cache_dir() / asset["name"]
    if _valid(path, asset):return path
    state = _read(path.with_suffix(".status.json")) or {}
    if path.with_suffix(".cancel").exists() or state.get("state") == "cancelled":
        if not resume:raise ProviderError(409, "历史索引下载已终止；重新查询可继续下载。")
        path.with_suffix(".cancel").unlink(missing_ok=True)
        _write(path.with_suffix(".status.json"), {"state": "queued", "asset": asset, "updated": time.time()})
    if state.get("state") == "failed" and state.get("asset") == asset and time.time() - state.get("updated", 0) < 300:
        return None
    key = str(path)
    with _guard:
        if key not in _tasks or _tasks[key].done():
            _tasks[key] = asyncio.create_task(asyncio.to_thread(_download, asset))
    partial = path.with_suffix(".part")
    received = partial.stat().st_size if partial.exists() else 0
    raise ProviderError(202, "正在准备历史航班索引", {"quarter": f"{asset['year']} Q{asset['quarter']}",
                        "received_bytes": received, "total_bytes": asset["size"], "retry_after": 2,
                        "status_path": f"/api/flight/schedules/status?name={asset['name']}"})


def query_rows(path: Path, callsigns: list[str], day: date) -> list[dict]:
    # Callsign-only first scan avoids decoding every quarter's timestamps.
    # A compact per-callsign cache makes repeat dates on a route inexpensive.
    key = hashlib.sha256(json.dumps([path.name, path.stat().st_mtime_ns, sorted(callsigns)]).encode()).hexdigest()
    cache = cache_dir() / f"query-{key}.json"
    rows = _read(cache)
    if rows is None:
        with duckdb.connect() as con:
            con.execute("SET threads=2")
            con.execute("SET memory_limit='512MB'")
            columns = {row[0] for row in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()}
            wanted = ["ICAO_Hex", "Reg", "AC_Type", "AC_Type_Description", "AC_Type_Detailed", "Airline", "Callsign",
                      *[f"Track_{side}_{field}" for side in ("Origin", "Destination")
                        for field in ("Lat", "Lon", "FL_Ft", "DateTime_UTC", "ApplicableAirports")]]
            projection = ','.join('"' + field + '"' for field in wanted if field in columns)
            marks = ','.join('?' for _ in callsigns)
            result = con.execute(f'SELECT {projection} FROM read_parquet(?) WHERE upper(trim(Callsign)) IN ({marks})',
                                 [str(path), *callsigns])
            names = [item[0] for item in result.description]
            rows = [dict(zip(names, row)) for row in result.fetchall()]
        _write(cache, rows)
    start = datetime.combine(day, datetime.min.time(), UTC) - timedelta(hours=14)
    end = start + timedelta(hours=50)
    return [row for row in rows if (stamp := _datetime(row.get("Track_Origin_DateTime_UTC"))) and start <= stamp < end]


def _endpoint(row: dict, side: str, hint: str | None) -> tuple[str | None, list[str]]:
    field = f"Track_{side}_"
    airports = list(dict.fromkeys(code for code in re.findall(r"\b[A-Z0-9]{4}\b", str(row.get(field + "ApplicableAirports") or "").upper())
                                 if _airport(code)))
    level = str(row.get(field + "FL_Ft", "")).lower().strip()
    altitude = _number(level)
    close_to_ground = level == "ground" or altitude is not None and altitude <= 3000
    if hint:
        airport = _airport(hint)
        if not airport:raise ProviderError(422, "无法识别机场代码，请填写 IATA 或 ICAO。")
        # Airports under an aircraft at cruise are flyover possibilities, not
        # evidence contradicting a known departure airport.
        if close_to_ground and airports and airport["icao"] not in airports:return None, airports
        return airport["icao"], airports
    # Multiple nearby airports are not an invitation to guess the nearest one.
    return (airports[0] if len(airports) == 1 and close_to_ground else None), airports


def candidates_from_rows(rows: list[dict], flight: str, day: date, origin=None, destination=None,
                         registration=None, icao24=None, first_seen=None) -> list[dict]:
    candidates = {}
    for row in rows:
        hex_code = re.sub(r"^icaohex[-_:]", "", _normal(row.get("ICAO_Hex")).lower())
        if not re.fullmatch(r"[0-9a-f]{6}", hex_code):continue
        mark = _normal(row.get("Reg"))
        if mark in ("-", "N/A", "NONE"):mark = ""
        if icao24 and hex_code != _normal(icao24).lower():continue
        if registration and mark and mark.replace('-', '') != _normal(registration).replace('-', ''):continue
        start, end = _datetime(row.get("Track_Origin_DateTime_UTC")), _datetime(row.get("Track_Destination_DateTime_UTC"))
        if not start or not end or end <= start:continue
        dep, deps = _endpoint(row, "Origin", origin)
        arr, arrs = _endpoint(row, "Destination", destination)
        if origin and deps and not dep or destination and arrs and not arr:continue
        zone = _airport_tz(dep) if dep else None
        # Nearby airports may agree on the observation's timezone even when
        # cruise altitude means none can be labelled the departure airport.
        nearby_zones = {z.key for code in deps if (z := _airport_tz(code))}
        observation_zone = zone
        if observation_zone is None and len(nearby_zones) == 1:
            observation_zone = _airport_tz(deps[0])
        observed_date = start.astimezone(observation_zone).date() if observation_zone else start.date()
        # First observed time is only a reliable departure-day test when the
        # airport was observed near ground. A user hint cannot turn cruise
        # reception into a takeoff observation.
        level = str(row.get("Track_Origin_FL_Ft", "")).lower().strip()
        near_ground = level == "ground" or (_number(level) is not None and _number(level) <= 3000)
        certain_date = bool(zone and near_ground and dep in deps)
        if certain_date and start.astimezone(zone).date() != day:continue
        if first_seen is not None and not (start.timestamp() - 3 * 3600 <= first_seen <= end.timestamp()):continue
        candidate_id = hashlib.sha256(f"{hex_code}|{row.get('Callsign')}|{_iso(start)}|{_iso(end)}".encode()).hexdigest()[:24]
        info = {"flight_number": flight, "travel_date": day.isoformat(), "icao24": hex_code,
                "registration": mark or None, "callsign": _normal(row.get("Callsign")),
                "aircraft_type": row.get("AC_Type_Detailed") or row.get("AC_Type_Description") or row.get("AC_Type"),
                "icao_type": row.get("AC_Type"), "operating_as": row.get("Airline"),
                "departure_airport": dep, "arrival_airport": arr,
                "takeoff_time": _iso(start), "landing_time": _iso(end), "times_inferred": True,
                "identity_source": "mrairspace", "identity_evidence": {
                    "source_url": REPOSITORY, "license": "ODbL-1.0", "matched_callsign": row.get("Callsign"),
                    "method": "dated_adsb_callsign", "date_confirmed": certain_date,
                    "origin_airports": deps, "destination_airports": arrs,
                    "track_start_utc": _iso(start), "track_end_utc": _iso(end),
                    "airports_inferred": True,
                    "track_endpoints": [{"timestamp": _iso(stamp),
                        "longitude": _number(row.get(f"Track_{side}_Lon")),
                        "latitude": _number(row.get(f"Track_{side}_Lat"))}
                        for side, stamp in (("Origin", start), ("Destination", end))]}}
        candidates[candidate_id] = {"schedule_candidate_id": candidate_id, "icao24": hex_code,
                   "registration": mark or None, "callsign": info["callsign"], "first_seen": int(start.timestamp()),
                   "last_seen": int(end.timestamp()), "est_departure_airport": dep, "est_arrival_airport": arr,
                   "departure_local": start.astimezone(observation_zone).isoformat() if observation_zone else _iso(start),
                   "observation_date": observed_date.isoformat(),
                   "observation_timezone": observation_zone.key if observation_zone else "UTC",
                   "date_confirmed": certain_date, "flight_info": info}
    return sorted(candidates.values(), key=lambda row: row["first_seen"])


def prefer_requested_date(candidates: list[dict], day: date) -> list[dict]:
    # Confirmed departures and same-day observations take precedence. Nearby
    # dates remain a clearly labelled fallback only when the day has no hits;
    # late first reception can legitimately cross midnight (e.g. long-haul).
    primary = [row for row in candidates if row.get("date_confirmed")
               or row.get("observation_date") == day.isoformat()]
    return primary or candidates


def supplement_gps_airports(candidates: list[dict], day: date) -> list[dict]:
    from .flight_identity_gps import airport_hints_from_gps
    from .store import TripStore
    if not any(not row["date_confirmed"] or not row["est_arrival_airport"] for row in candidates):
        return candidates
    store = TripStore()
    for candidate in candidates:
        if candidate["date_confirmed"] and candidate["est_arrival_airport"]:continue
        hints = airport_hints_from_gps(store, candidate, day)
        if not hints:continue
        info = candidate["flight_info"]
        for field in ("departure_airport", "arrival_airport"):
            info[field] = hints[field]
        info["identity_evidence"].update(date_confirmed=True, airport_evidence=hints["airport_evidence"])
        info["departure_timezone"] = hints["departure_timezone"]
        candidate.update(est_departure_airport=hints["departure_airport"], est_arrival_airport=hints["arrival_airport"],
                         date_confirmed=True, departure_local=hints["departure_local"])
    return candidates


async def lookup_dated_flight(flight: str, day: date, fallback: dict[str, str], origin=None, destination=None,
                             registration=None, icao24=None, first_seen=None, schedule_candidate_id=None) -> dict:
    try:
        assets = await release_assets(day)
    except ProviderError as exc:
        return {"identity_lookup_note": exc.message}
    if not assets:
        return {"identity_lookup_note": f"MrAirspace 尚无 {day.year} Q{(day.month - 1) // 3 + 1} 可用数据，继续查询其它来源。"}
    paths = []
    for asset in assets:
        if path := await ensure_dataset(asset):paths.append(path)
    if not paths:
        return {"identity_lookup_note": "MrAirspace 历史索引下载暂不可用，继续查询其它来源。"}
    callsigns = await resolve_callsigns(flight, fallback)
    rows = []
    for path in paths:
        try:rows.extend(await asyncio.to_thread(query_rows, path, callsigns, day))
        except (OSError, ValueError, duckdb.Error):continue
    candidates = candidates_from_rows(rows, flight, day, origin, destination, registration, icao24, first_seen)
    if not candidates:
        alias = await alternate_callsign(flight)
        if alias and alias not in callsigns:
            for path in paths:
                try:rows.extend(await asyncio.to_thread(query_rows, path, [alias], day))
                except (OSError, ValueError, duckdb.Error):continue
    candidates = candidates_from_rows(rows, flight, day, origin, destination, registration, icao24, first_seen)
    candidates = await asyncio.to_thread(supplement_gps_airports, candidates, day)
    candidates = prefer_requested_date(candidates, day)
    if schedule_candidate_id:
        candidates = [row for row in candidates if row["schedule_candidate_id"] == schedule_candidate_id]
        if not candidates:raise ProviderError(409, "所选历史航班已变化，请重新查询。")
    if candidates and (len(candidates) > 1 or not candidates[0]["date_confirmed"] and not schedule_candidate_id):
        raise ProviderError(409,
            "该日期有多个匹配航段，请选择本次乘坐的航班。" if all(row["date_confirmed"] for row in candidates)
            else "以下记录含未确认出发日期的空中观测片段，请核对观测日期和飞机后选择。",
            {"candidates": candidates})
    if not candidates:
        return {"identity_lookup_note": "MrAirspace 未找到可确认的当日航班；继续查询其它来源。"}
    info = candidates[0]["flight_info"]
    info["identity_evidence"]["datasets"] = [{"name": asset["name"], "source_url": asset["release_url"]} for asset in assets]
    return info
