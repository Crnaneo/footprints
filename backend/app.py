"""Loopback-only API for a persistent local footprint application."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from functools import lru_cache
from uuid import uuid4
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from .csv_io import MAX_CSV_BYTES, CsvImportError, export_csv, parse_csv
from .models import TripCreate, canonical_trip
from .store import TripStore, default_database_path, shift_trip_date
from .flight_timezones import with_flight_timezones

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_ORIGINS = {
    "http://localhost:5173", "http://127.0.0.1:5173",
    "http://localhost:8765", "http://127.0.0.1:8765",
    "http://[::1]:5173", "http://[::1]:8765",
}
LOCAL_PORT = os.environ.get("FOOTPRINT_PORT", "8765")
LOCAL_ORIGINS.update({f"http://localhost:{LOCAL_PORT}", f"http://127.0.0.1:{LOCAL_PORT}", f"http://[::1]:{LOCAL_PORT}"})
app = FastAPI(title="Footprint", version="0.1.0")
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(CORSMiddleware, allow_origins=sorted(LOCAL_ORIGINS),
                   allow_methods=["GET", "POST", "DELETE", "PUT"],
                   allow_headers=["Content-Type"], allow_credentials=False)


def load_token() -> None:
    # An explicit UI setting (including an empty value) overrides .env. Before
    # the first settings save, the deployment environment remains authoritative.
    settings = default_database_path().parent / "settings.json"
    try:
        saved = json.loads(settings.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            return
        token = saved.get("fr24_api_token")
        if isinstance(token, str):
            if token.strip():
                os.environ["FR24_API_TOKEN"] = token.strip()
            else:
                os.environ.pop("FR24_API_TOKEN", None)
    except (OSError, ValueError, TypeError):
        pass


try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass
load_token()


_RAIL_WARMUP_STARTED = False


def _warm_rail_catalog() -> None:
    """Pre-build the rail index and via station tables so the first combobox
    query does not pay the one-time national-catalog construction cost."""
    try:
        store = TripStore()
        if not (store.path.parent / "rail" / "catalog.sqlite3").exists():
            return
        from .rail_via import _tables, editor_index
        editor_index(store)
        _tables(store)
    except Exception:
        pass


@app.on_event("startup")
async def _startup_warm_rail() -> None:
    global _RAIL_WARMUP_STARTED
    if _RAIL_WARMUP_STARTED or os.environ.get("FOOTPRINT_SKIP_WARMUP"):
        return
    _RAIL_WARMUP_STARTED = True
    threading.Thread(target=_warm_startup, name="rail-warmup", daemon=True).start()


def _warm_startup() -> None:
    # Point colours first: the map is the first thing a returning user sees,
    # and they only need the catalog way geometry, not the full rail index.
    try:
        from .point_modes import backfill
        backfill(TripStore())
    except Exception:
        pass
    # The national railway topology is only needed by rail tools. Loading it
    # here retains a large index even for users who only browse their points.


@app.middleware("http")
async def require_loopback(request: Request, call_next: Any) -> Response:
    try:
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "无效 Host 请求头"})
    if host not in {"localhost", "127.0.0.1", "::1", "testserver"}:
        return JSONResponse(status_code=403, content={"detail": "本机应用仅接受 localhost 访问"})
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin and origin not in LOCAL_ORIGINS:
            return JSONResponse(status_code=403, content={"detail": "拒绝来自其他网站的写入请求"})
    load_token()
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Do not echo submitted values: settings payloads may contain credentials.
    errors = [{"loc": list(item["loc"]), "message": item["msg"], "type": item["type"]} for item in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": "请求字段校验失败", "details": {"errors": errors}})


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "application": "footprint", "version": "0.1.0"}


@app.get("/api/trips")
def list_trips(mode: Literal["flight", "rail", "other"] | None = None, q: str | None = None) -> list[dict[str, Any]]:
    return [with_flight_timezones(record) for record in TripStore().list(mode=mode, q=q)]


@app.get("/api/overview")
def map_overview(include_points: bool = True) -> list[dict[str, Any]]:
    return [with_flight_timezones(record) for record in current_map_store().overview(include_points=include_points)]


class MapViewRequest(BaseModel):
    trip_ids: list[str] = Field(max_length=100000)


@lru_cache(maxsize=8)
def map_store(path: str) -> TripStore:
    # Initialize/migrate once per database, rather than once per tile request.
    return TripStore(path)


def current_map_store() -> TripStore:
    return map_store(str(default_database_path().resolve()))


@app.post('/api/map/views')
def register_map_view(body: MapViewRequest) -> dict:
    from .map_points import register_view
    try:return register_view(current_map_store(),body.trip_ids)
    except ValueError as e:raise HTTPException(400,detail=str(e)) from e


@app.get('/api/map/tiles/{view_id}/{z}/{x}/{y}.pbf')
def map_point_tile(view_id: str,z: int,x: int,y: int,request: Request,packed: bool=False) -> Response:
    from .map_points import tile_entry, view_revision
    store = current_map_store()
    # Revalidation must not decode/encode an evicted million-point tile just
    # to return 304. The database revision is enough to validate its ETag.
    if request.headers.get('if-none-match'):
        try: revision = view_revision(store, view_id, z, x, y)
        except KeyError as e: raise HTTPException(404, detail=str(e)) from e
        except ValueError as e: raise HTTPException(400, detail=str(e)) from e
        etag = f'"{view_id}-{revision}-{z}-{x}-{y}{"-packed-v1" if packed else ""}"'
        if request.headers.get('if-none-match') == etag:
            return Response(status_code=304, headers={'ETag': etag, 'Cache-Control': 'private, no-cache', 'Vary': 'Accept-Encoding'})
    try:data,count,compressed,revision=tile_entry(store,view_id,z,x,y,packed=packed)
    except KeyError as e:raise HTTPException(404,detail=str(e)) from e
    except ValueError as e:raise HTTPException(400,detail=str(e)) from e
    etag=f'"{view_id}-{revision}-{z}-{x}-{y}{"-packed-v1" if packed else ""}"'
    headers={'Cache-Control':'private, no-cache','ETag':etag,'X-Point-Count':str(count),'Vary':'Accept-Encoding'}
    if request.headers.get('if-none-match')==etag:return Response(status_code=304,headers=headers)
    if 'gzip' in request.headers.get('accept-encoding',''):
        headers['Content-Encoding']='gzip';data=compressed
    return Response(data,media_type='application/vnd.mapbox-vector-tile',headers=headers)


@app.get('/api/map/point/{point_id}')
def map_point_detail(point_id: int) -> dict:
    from .map_points import point_detail
    result=point_detail(current_map_store(),point_id)
    if result is None:raise HTTPException(404,detail='轨迹点不存在')
    return result


@app.get("/api/trips/{trip_id}")
def get_trip(trip_id: str) -> dict[str, Any]:
    trip = TripStore().get(trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="旅程不存在")
    return with_flight_timezones(trip)


@app.post("/api/trips", status_code=201)
def create_trip(trip: TripCreate) -> dict[str, Any]:
    from .flight_overlay import attach_gps_overlay
    store = TripStore()
    draft = attach_gps_overlay(store, trip.model_dump(mode="json"))
    return with_flight_timezones(store.create(TripCreate.model_validate(draft)))


@app.delete("/api/trips/{trip_id}", status_code=204)
def delete_trip(trip_id: str) -> Response:
    store = TripStore()
    trip = store.get(trip_id)
    if trip is None or not store.delete(trip_id):
        raise HTTPException(status_code=404, detail="旅程不存在")
    archived = trip.get("metadata", {}).get("original_file")
    if isinstance(archived, str) and archived.endswith(".csv") and Path(archived).name == archived:
        if not any(summary.get("metadata", {}).get("original_file") == archived for summary in store.list()):
            archive = store.path.parent / "imports" / archived
            archive.unlink(missing_ok=True)
    return Response(status_code=204)


class TripDateUpdate(BaseModel):
    travel_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@app.put("/api/trips/{trip_id}/date")
def update_trip_date(trip_id: str, payload: TripDateUpdate) -> dict[str, Any]:
    """Re-date a via-reconstructed rail trip; all stored times shift together."""
    store = TripStore()
    trip = store.get(trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="旅程不存在")
    if trip.get("mode") != "rail" or trip.get("geometry_kind") != "reconstructed":
        raise HTTPException(status_code=422, detail="仅经由导入的铁路轨迹支持更改日期")
    try:
        return store.update_record(trip_id, lambda record: shift_trip_date(record, payload.travel_date))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.get("/api/export.csv")
def download_csv(trip_id: str | None = None, format: Literal["steplife", "extended"] = "steplife",
                 modes: str | None = None) -> Response:
    store = TripStore()
    if trip_id:
        trip = store.get(trip_id)
        if trip is None:
            raise HTTPException(status_code=404, detail="旅程不存在")
    # Optional traffic-mode filter (e.g. modes=rail,flight) so the export can
    # cover just the rail/flight records instead of the whole archive.
    mode_filter: tuple[str, ...] | None = None
    if modes:
        wanted = tuple(dict.fromkeys(m.strip() for m in modes.split(",") if m.strip()))
        unknown = [m for m in wanted if m not in {"rail", "flight", "other"}]
        if unknown:
            raise HTTPException(status_code=422, detail="mode 仅支持 rail、flight、other。")
        mode_filter = wanted or None
    suffix = "-" + "-".join(mode_filter) if mode_filter else "-all"
    if format == "steplife":
        from .steplife import iter_backup_csv
        return StreamingResponse(iter_backup_csv(store, trip_id, mode_filter), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="backUpData{suffix}.csv"', "X-Content-Type-Options": "nosniff"})
    def extended_rows():
        yield export_csv([])
        with store.connect() as db:
            conditions = []
            args: list = []
            if trip_id:
                conditions.append("id=?")
                args.append(trip_id)
            if mode_filter:
                conditions.append("json_extract(payload,'$.mode') IN ({})".format(",".join("?" * len(mode_filter))))
                args.extend(mode_filter)
            query = "SELECT payload FROM trips"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY travel_date DESC,created_at DESC,id"
            for row in db.execute(query, args):
                yield export_csv([json.loads(row[0])]).split(b'\r\n',1)[1]
    return StreamingResponse(extended_rows(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="footprint{suffix}.csv"',
                             "X-Content-Type-Options": "nosniff"})


def schedule_rail_match(background_tasks: BackgroundTasks) -> None:
    from .rail_coverage import STATUS, paths, rebuild
    store = TripStore()
    if paths(store)[0].exists() and STATUS['state'] not in {'running', 'queued'}:
        STATUS.update(state='queued', processed=0, total=len(store.list()), error=None)
        background_tasks.add_task(rebuild, store)


@app.get("/api/rail-coverage")
def rail_coverage() -> Any:
    from .rail_coverage import read_coverage
    return read_coverage()


class RailIntervalEdit(BaseModel):
    line_id: str = Field(pattern=r"^[1-9]\d*$", max_length=20)
    origin_id: str = Field(min_length=1, max_length=100)
    destination_id: str = Field(min_length=1, max_length=100)
    interval_id: str | None = Field(default=None, max_length=100)
    expected_revision: str = Field(default="auto", max_length=100)


class RailRecordDelete(BaseModel):
    line_id: str = Field(pattern=r"^[1-9]\d*$", max_length=20)
    interval_id: str | None = Field(default=None, max_length=100)
    expected_revision: str = Field(default="auto", max_length=100)


def rail_edit_call(callback):
    from .rail_manual import EditConflict
    try:
        return callback()
    except EditConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@app.get("/api/rail-catalog/lines")
def rail_catalog_lines(q: str = "") -> Any:
    from .rail_manual import search_lines
    return rail_edit_call(lambda: search_lines(TripStore(), q))


@app.get("/api/rail-catalog/lines/{line_id}/stations")
def rail_catalog_stations(line_id: str) -> Any:
    from .rail_manual import line_catalog, ManualStore
    store = TripStore()
    def read():
        result = line_catalog(store, line_id).public()
        result['manual_revision'] = ManualStore(store).rows().get(line_id, ({}, 'auto'))[1]
        return result
    return rail_edit_call(read)


@app.post("/api/rail-manual/preview")
def rail_interval_preview(body: RailIntervalEdit) -> Any:
    from .rail_manual import preview_interval
    return rail_edit_call(lambda: preview_interval(TripStore(), body.line_id, body.origin_id, body.destination_id))


@app.put("/api/rail-manual/interval")
def rail_interval_save(body: RailIntervalEdit) -> Any:
    from .rail_manual import save_interval
    return rail_edit_call(lambda: {'undo_token': save_interval(TripStore(), **body.model_dump()), 'line_id': body.line_id})


@app.post("/api/rail-manual/delete")
def rail_record_delete(body: RailRecordDelete) -> Any:
    from .rail_manual import delete_record
    return rail_edit_call(lambda: {'undo_token': delete_record(TripStore(), **body.model_dump())})


@app.post("/api/rail-manual/restore")
def rail_record_restore(body: RailRecordDelete) -> Any:
    from .rail_manual import ManualStore
    store = ManualStore(TripStore())
    def restore():
        previous = store.rows().get(body.line_id)
        if not previous:
            raise ValueError('该线路没有可恢复的手动修改。')
        payload = previous[0]
        payload = payload['saved_line'] if payload.get('hidden') else {'automatic':True}
        return {'undo_token': store.change(body.line_id, payload, body.expected_revision)}
    return rail_edit_call(restore)


class RailUndo(BaseModel):
    token: str = Field(min_length=1, max_length=100)


@app.post("/api/rail-manual/undo")
def rail_record_undo(body: RailUndo) -> Any:
    from .rail_manual import ManualStore
    def undo():
        ManualStore(TripStore()).undo(body.token)
        return {'ok':True}
    return rail_edit_call(undo)


class RailViaTime(BaseModel):
    mode: Literal["manual", "auto"] = "auto"
    value: str | None = Field(default=None, max_length=8)


class RailViaRow(BaseModel):
    origin_id: str = Field(min_length=1, max_length=100)
    origin_name: str = Field(default="", max_length=120)
    line_id: str = Field(pattern=r"^[1-9]\d*$", max_length=20)
    line_name: str = Field(default="", max_length=200)
    via_id: str = Field(min_length=1, max_length=100)
    via_name: str = Field(default="", max_length=120)
    departure: RailViaTime = Field(default_factory=RailViaTime)
    arrival: RailViaTime = Field(default_factory=RailViaTime)


class RailViaPreview(BaseModel):
    travel_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    origin_id: str = Field(min_length=1, max_length=100)
    destination_id: str = Field(min_length=1, max_length=100)
    rows: list[RailViaRow] = Field(min_length=1, max_length=200)


@app.get("/api/rail-via/stations")
def rail_via_stations(q: str = "") -> Any:
    from .rail_via import ViaError, search_stations
    try:
        return {"stations": search_stations(TripStore(), q)}
    except FileNotFoundError as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    except ViaError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@app.get("/api/rail-via/station-lines")
def rail_via_station_lines(station_id: str) -> Any:
    from .rail_via import ViaError, station_lines
    try:
        return {"lines": station_lines(TripStore(), station_id)}
    except FileNotFoundError as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    except ViaError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@app.post("/api/rail-via/preview")
def rail_via_preview(body: RailViaPreview) -> Any:
    from .rail_via import ViaError, build_route
    try:
        result = build_route(TripStore(), body.travel_date, [row.model_dump() for row in body.rows],
                             origin_id=body.origin_id, destination_id=body.destination_id)
        TripCreate.model_validate(result["trip"])
    except FileNotFoundError as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    except ViaError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except ValidationError:
        raise HTTPException(400, detail="经由重建的轨迹无效，请检查所选线路与车站。") from None
    return result


@app.post("/api/rail-coverage/rebuild", status_code=202)
def rebuild_rail_coverage(background_tasks: BackgroundTasks) -> Any:
    from .rail_coverage import STATUS, paths, rebuild
    store = TripStore()
    if not paths(store)[0].exists():
        raise HTTPException(status_code=503, detail="本地铁路目录尚未准备，请参阅 README 下载线路资料")
    if STATUS['state'] not in {'running', 'queued'}:
        STATUS.update(state='queued', processed=0, total=len(store.list()), error=None)
        background_tasks.add_task(rebuild, store)
    return {"progress": STATUS.copy()}


@app.get("/api/template.csv")
def csv_template() -> Response:
    template = (
        "dataTime,locType,longitude,latitude,heading,accuracy,speed,distance,isBackForeground,stepType,altitude\r\n"
        "1790899200,1,121.473700,31.230400,-1,10,8.333333,0,1,0,20\r\n"
        "1790899260,1,121.480000,31.235000,-1,10,10,700,1,0,22\r\n"
    )
    return Response(template.encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="footprint-template.csv"',
                             "X-Content-Type-Options": "nosniff"})


@app.post("/api/import/csv", status_code=201)
async def import_csv(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form("CSV 导入"),
    mode: str = Form("other"),
    travel_date: str | None = Form(None),
    source_crs: str = Form("WGS84"),
    column_mapping: str | None = Form(None),
) -> Any:
    original_filename = (file.filename or "import.csv").replace("\\", "/").rsplit("/", 1)[-1][:300]
    from .steplife import MAX_BACKUP_BYTES, is_steplife, import_backup
    import csv
    directory = default_database_path().parent / "imports"
    directory.mkdir(parents=True, exist_ok=True)
    spool = directory / f"{uuid4()}.csv"
    size = 0
    try:
        with spool.open("xb") as output:
            os.chmod(spool, 0o600)
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BACKUP_BYTES:
                    raise HTTPException(status_code=413, detail="CSV 文件不能超过 512 MB")
                output.write(chunk)
        with spool.open(encoding="utf-8-sig", newline="") as source:
            headers = next(csv.reader(source), [])
        if is_steplife(headers):
            try:
                result = await run_in_threadpool(import_backup, spool, TripStore(), source_crs=source_crs, filename=original_filename, archive_filename=spool.name, fallback_date=travel_date)
                if result.get("already_imported"):
                    spool.unlink(missing_ok=True)
                if not result.get("already_imported"):
                    schedule_rail_match(background_tasks)
                return result
            except (OSError, sqlite3.Error):
                spool.unlink(missing_ok=True)
                return JSONResponse(status_code=503, content={"detail": "本地存储写入失败，未保存轨迹", "details": None})
            except (CsvImportError, ValueError) as exc:
                spool.unlink(missing_ok=True)
                return JSONResponse(status_code=422, content={"detail": str(exc), "details": {"errors": getattr(exc, "errors", [])}})
        if size > MAX_CSV_BYTES:
            raise HTTPException(status_code=413, detail="通用 CSV 不能超过 100 MB；一生足迹备份支持 512 MB")
        data = spool.read_bytes()
    except (UnicodeError, csv.Error):
        spool.unlink(missing_ok=True)
        return JSONResponse(status_code=422, content={"detail": "CSV 需为有效 UTF-8 文本", "details": None})
    except BaseException:
        spool.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    spool.unlink(missing_ok=True)
    try:
        mapping = json.loads(column_mapping) if column_mapping else None
        if mapping is not None and (not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items())):
            raise ValueError("column_mapping 需为字段名到列名的 JSON 对象")
        drafts = parse_csv(data, title=title, mode=mode, travel_date=travel_date,
                           source_crs=source_crs, column_mapping=mapping)
    except (CsvImportError, ValueError) as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc),
                "details": {"errors": getattr(exc, "errors", [])}})
    directory = default_database_path().parent / "imports"
    directory.mkdir(parents=True, exist_ok=True)
    archive_name = f"{uuid4()}.csv"
    archive_path = directory / archive_name
    for draft in drafts:
        draft.metadata["original_file"] = archive_name
        draft.metadata["original_filename"] = original_filename
    # Validate the complete file first, then save its bytes and the associated
    # records together. A storage error cannot leave an imported trip behind.
    try:
        with archive_path.open("xb") as archive:
            os.chmod(archive_path, 0o600)
            archive.write(data)
            archive.flush()
            os.fsync(archive.fileno())
        records = TripStore().create_many(drafts)
    except (OSError, ValueError, sqlite3.Error):
        archive_path.unlink(missing_ok=True)
        return JSONResponse(status_code=503, content={"detail": "本地存储写入失败，未保存轨迹", "details": None})
    summaries = [{key: value for key, value in record.items() if key != "points"} for record in records]
    schedule_rail_match(background_tasks)
    return {"trips": summaries, "count": len(summaries), "point_count": sum(record["point_count"] for record in records)}


class FlightImport(BaseModel):
    """Flight import tab: FR24 first, then the free reconstruction chain."""

    flight_number: str = Field(min_length=1, max_length=100)
    travel_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    origin: str | None = Field(default=None, max_length=300)
    destination: str | None = Field(default=None, max_length=300)
    fr24_id: str | None = Field(default=None, max_length=100)
    schedule_candidate_id: str | None = Field(default=None, max_length=24)
    icao24: str | None = Field(default=None, max_length=20)
    first_seen: int | None = Field(default=None, ge=0)
    last_seen: int | None = Field(default=None, ge=0)
    registration: str | None = Field(default=None, max_length=20)
    track_source: Literal["auto", "local"] = "auto"
    local_candidate_id: str | None = Field(default=None, max_length=100)
    existing_trip_id: str | None = Field(default=None, max_length=100)
    departure_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


class RailImport(BaseModel):
    train_number: str = Field(min_length=1, max_length=100)
    travel_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    origin: str = Field(min_length=1, max_length=300)
    destination: str = Field(min_length=1, max_length=300)


class SandboxFlightImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sandbox_token: SecretStr = Field(min_length=8, max_length=5000)

    @field_validator("sandbox_token")
    @classmethod
    def validate_sandbox_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if "\r" in token or "\n" in token:
            raise ValueError("沙箱密钥不能包含换行符")
        token = token.strip()
        if len(token) < 8:
            raise ValueError("请输入有效的沙箱密钥")
        return SecretStr(token)


def contains_secret(value: Any, token: str) -> bool:
    if isinstance(value, str):
        return token in value
    if isinstance(value, dict):
        return any(contains_secret(key, token) or contains_secret(item, token) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_secret(item, token) for item in value)
    return False


def provider_response(exc: Exception) -> JSONResponse:
    if getattr(exc, "status_code", None) == 202:
        return JSONResponse(status_code=202, content={"lookup_pending": exc.details})
    return JSONResponse(status_code=getattr(exc, "status_code", 502),
                        content={"detail": getattr(exc, "message", "数据源请求失败"),
                                 "details": getattr(exc, "details", None)})


@app.post("/api/import/flight-sandbox", status_code=201)
async def import_flight_sandbox(payload: SandboxFlightImport) -> Any:
    from .providers import ProviderError, fetch_flight_sandbox
    token = payload.sandbox_token.get_secret_value()
    safe_failure = {"detail": "沙箱数据源请求失败，未保存轨迹", "details": None}
    try:
        draft = await fetch_flight_sandbox(token)
        trip = TripCreate.model_validate(draft)
        # A provider response cannot accidentally persist or export the request
        # credential, even if an upstream diagnostic includes it in a string.
        if contains_secret(trip.model_dump(mode="json"), token):
            return JSONResponse(status_code=502, content=safe_failure)
    except ProviderError as exc:
        if contains_secret({"message": exc.message, "details": exc.details}, token):
            return JSONResponse(status_code=502, content=safe_failure)
        return provider_response(exc)
    except ValidationError:
        return JSONResponse(status_code=502, content={"detail": "沙箱数据源返回无效轨迹，未保存", "details": None})
    except Exception:
        # Unexpected provider exceptions are never echoed: they may contain the
        # transient credential. This endpoint does not update production settings.
        return JSONResponse(status_code=502, content=safe_failure)
    return TripStore().create(trip)


@app.post("/api/import/rail", status_code=201)
async def import_rail(payload: RailImport) -> Any:
    from .providers import ProviderError, fetch_rail
    try:
        draft = await fetch_rail(**payload.model_dump())
        trip = TripCreate.model_validate(draft)
    except ProviderError as exc:
        return provider_response(exc)
    except ValidationError:
        return JSONResponse(status_code=502, content={"detail": "铁路数据源返回无效轨迹，未保存", "details": None})
    return TripStore().create(trip)


@app.get("/api/flight/schedules/status")
async def flight_schedule_status(name: str) -> Any:
    from .flight_schedules import cache_dir, _read, ensure_dataset
    from .providers import ProviderError
    catalog = _read(cache_dir() / "releases.json") or {}
    asset = next((row for row in catalog.get("assets", []) if row.get("name") == name), None)
    if asset is None:
        raise HTTPException(status_code=404, detail="历史索引不存在，请重新查询。")
    try:
        await ensure_dataset(asset, resume=False)
    except ProviderError as exc:
        return provider_response(exc)
    # A failed download also ends polling: the import can fall through to the
    # remaining providers without repeating paid FR24 calls every two seconds.
    return {"ready": True}


class FlightScheduleCancel(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@app.post("/api/flight/schedules/cancel")
def cancel_flight_schedule(payload: FlightScheduleCancel) -> Any:
    from .flight_schedules import cache_dir, _read, cancel_download
    catalog = _read(cache_dir() / "releases.json") or {}
    asset = next((row for row in catalog.get("assets", []) if row.get("name") == payload.name), None)
    if asset is None:raise HTTPException(status_code=404, detail="历史索引不存在。")
    cancel_download(asset)
    return {"cancelled": True}


@app.post("/api/preview/flight")
async def preview_flight(payload: FlightImport) -> Any:
    """Prefer observed tracks; otherwise preview available flight information."""
    from .flight_reconstruction import (ProviderError, fetch_flight_unified,
                                        lookup_aircraft_info, merge_aircraft_info)
    flight_info = {}
    try:
        from .flight_local import recover_local_flight, departure_hint
        from .providers import _service, _date
        store = TripStore()
        base = store.get(payload.existing_trip_id) if payload.existing_trip_id else None
        if payload.existing_trip_id and (base is None or base["mode"] != "flight"):
            raise ProviderError(404, "待恢复的航班不存在。")
        flight = _service(payload.flight_number, "flight")
        day = _date(payload.travel_date)
        # A chosen local candidate and the explicit recovery action stay local:
        # do not re-run online queries when confirming a previous selection.
        local_only = payload.track_source == "local" or payload.existing_trip_id or payload.local_candidate_id
        online, draft = None, None
        if base:
            flight_info.update(base.get("metadata", {}).get("flight", {}))
        flight_info.update(flight_number=flight, travel_date=day.isoformat())
        for field, value in (("departure_airport", payload.origin), ("arrival_airport", payload.destination)):
            if value:
                flight_info[field] = value.upper()
        if local_only and not base and (payload.registration or payload.icao24):
            merge_aircraft_info(flight_info, await lookup_aircraft_info(payload.registration, payload.icao24))
        if not local_only:
            body = payload.model_dump(exclude={"track_source", "local_candidate_id", "existing_trip_id", "departure_time"})
            hint = departure_hint(day, payload.origin, payload.departure_time)
            if hint is not None and body.get("first_seen") is None:
                body["first_seen"] = int(hint.timestamp())
            from .flight_overlay import matching_update_records
            from .providers import _datetime
            known = matching_update_records(store, flight, day, payload.origin, payload.destination)
            if len(known) == 1:
                saved_info = known[0].get("metadata", {}).get("flight", {})
                if not body.get("registration") and not body.get("icao24"):
                    body["registration"] = saved_info.get("registration")
                    if not body["registration"]:body["icao24"] = saved_info.get("icao24")
                for key in ("origin", "destination"):
                    body[key] = body.get(key) or known[0].get(key)
                for field, source in (("first_seen", "takeoff_time"), ("last_seen", "landing_time")):
                    stamp = _datetime(saved_info.get(source))
                    if body.get(field) is None and stamp:body[field] = int(stamp.timestamp())
            try:
                online = await fetch_flight_unified(**body)
            except ProviderError as exc:
                # Ambiguous identities and invalid inputs need correction;
                # unavailable / inaccessible observations may use local GPS.
                if exc.status_code in (202, 409, 422):
                    raise
                if isinstance(exc.details, dict):
                    flight_info.update(exc.details.get("flight_info") or exc.details.get("aircraft_info") or {})
            if online is not None:
                flight_info.update(online.get("metadata", {}).get("flight", {}))
            if online is not None and len(online.get("points") or []) >= 2:
                draft = online
        if draft is None:
            draft = recover_local_flight(
                store, flight, day, payload.origin or flight_info.get("departure_airport"),
                payload.destination or flight_info.get("arrival_airport"),
                payload.local_candidate_id, payload.departure_time,
                base or online or {"metadata": {"flight": flight_info}})
            if draft is None:
                if local_only:
                    raise ProviderError(404, "无数据：未找到同时匹配日期、两端机场及飞行速度特征的本地 GPS。本次没有保存。")
                # User-entered number/date/airports alone are not a lookup result.
                if not any(flight_info.get(key) for key in (
                        "registration", "icao24", "aircraft_type", "callsign", "photo_url",
                        "takeoff_time", "landing_time", "airline_name", "actual_distance_km")):
                    raise ProviderError(404, "轨迹无数据：未获取到当日轨迹或可用的航班、飞机资料。"
                                        + (flight_info.get("identity_lookup_note") or ""),
                                        {"flight_info": flight_info})
                from .flight_overlay import record_fingerprint
                info = dict(flight_info)
                for key in ("track_stats", "route_waypoints", "borrowed_from_date", "runway_evidence"):
                    info.pop(key, None)
                metadata = {"flight": info, "information_only": True}
                if len(known) == 1:
                    current = store.get(known[0]["id"])
                    metadata["information_update"] = {"trip_id": current["id"], "fingerprint": record_fingerprint(current)}
                origin = payload.origin or info.get("departure_airport")
                destination = payload.destination or info.get("arrival_airport")
                draft = {"title": f"{flight} · {origin} → {destination}" if origin and destination else flight,
                         "mode": "flight", "travel_date": day.isoformat(), "service_number": flight,
                         "origin": origin, "destination": destination,
                         "source": "Flightradar24" if info.get("track_source") == "flightradar24" else "航班及飞机资料",
                         "geometry_kind": "scheduled", "points": [], "metadata": metadata,
                         "quality_flags": ["no_track_data"]}
        if not draft.get("metadata", {}).get("information_only"):
            merge_aircraft_info(draft.setdefault("metadata", {}).setdefault("flight", {}), flight_info)
        if len(draft.get("points", [])) >= 2 and draft.get("source") in ("Flightradar24", "OpenSky", "adsb.lol"):
            from .flight_overlay import attach_gps_overlay, update_target
            attach_gps_overlay(store, draft)
            update_target(store, draft)
        TripCreate.model_validate(draft)
    except ProviderError as exc:
        if flight_info:
            details = dict(exc.details) if isinstance(exc.details, dict) else {}
            details.setdefault("flight_info", flight_info)
            details.setdefault("aircraft_info", flight_info)
            exc = ProviderError(exc.status_code, exc.message, details)
        return provider_response(exc)
    except ValidationError:
        return JSONResponse(status_code=502, content={"detail": "航班数据源返回无效轨迹", "details": None})
    return {"trip": with_flight_timezones(draft)}


@app.post("/api/flight/{trip_id}/information")
def update_flight_information(trip_id: str, trip: TripCreate) -> Any:
    """Save reviewed information without replacing any existing observations."""
    from .flight_overlay import record_fingerprint
    from .providers import ProviderError
    draft = trip.model_dump(mode="json")
    metadata = draft.get("metadata", {})
    review = metadata.get("information_update", {})
    try:
        if draft["mode"] != "flight" or draft["points"] or not metadata.get("information_only") or not metadata.get("flight"):
            raise ProviderError(422, "请先预览航班资料。")
        def transform(record):
            if (review.get("trip_id") != trip_id or review.get("fingerprint") != record_fingerprint(record)
                    or record.get("mode") != "flight" or record.get("service_number") != draft.get("service_number")
                    or record.get("travel_date") != draft.get("travel_date")):
                raise ProviderError(409, "航班记录已变化，请重新预览资料。")
            info = record.setdefault("metadata", {}).setdefault("flight", {})
            incoming = dict(metadata["flight"])
            from .flight_reconstruction import normalize_registration
            changed_airframe = any(info.get(key) and incoming.get(key) and normal(info[key]) != normal(incoming[key])
                                   for key, normal in (("icao24", str.upper), ("registration", normalize_registration)))
            if changed_airframe:
                for key in ("registration", "icao24", "aircraft_type", "icao_type", "aircraft_manufacturer",
                            "registered_owner", "owner_country", "photo_url", "photo_thumbnail", "photo_photographer", "photo_link"):
                    info.pop(key, None)
            elif incoming.get("photo_url") and incoming["photo_url"] != info.get("photo_url"):
                info.pop("photo_photographer", None)
                info.pop("photo_link", None)
            if record.get("points"):
                incoming.pop("track_source", None)
            info.update({key: value for key, value in incoming.items() if value is not None})
            if not record.get("points"):
                record["metadata"]["information_only"] = True
                record["source"] = draft["source"]
                record["geometry_kind"] = "scheduled"
            return record
        result = TripStore().update_record(trip_id, transform)
        if result is None:
            raise ProviderError(404, "航班不存在。")
        return with_flight_timezones(result)
    except ProviderError as exc:
        return provider_response(exc)


@app.post("/api/flight/{trip_id}/recover")
async def save_recovered_flight(trip_id: str, payload: FlightImport) -> Any:
    """Recompute a reviewed local match and update a geometry-free flight."""
    from .flight_local import recover_local_flight
    from .providers import ProviderError, _service
    store = TripStore()
    try:
        if not payload.local_candidate_id:
            raise ProviderError(422, "请先预览并选择本地轨迹。")
        # Re-check inside the write transaction to prevent stale previews from
        # overwriting a flight that acquired a track meanwhile.
        def transform(record):
            if record["mode"] != "flight" or record.get("points"):
                raise ProviderError(409, "仅可恢复尚无轨迹的航班；已有轨迹不会被替换。")
            if (record.get("service_number") != _service(payload.flight_number, "flight")
                    or record["travel_date"] != payload.travel_date):
                raise ProviderError(409, "航班号或日期已变化，请重新预览。")
            draft = recover_local_flight(store, payload.flight_number, payload.travel_date, payload.origin,
                                         payload.destination, payload.local_candidate_id, payload.departure_time, record)
            if draft is None:
                raise ProviderError(409, "本地轨迹已变化，请重新预览。")
            draft["notes"] = record.get("notes")
            updated = canonical_trip(TripCreate.model_validate(draft))
            return {**updated, "id": record["id"], "created_at": record["created_at"]}
        result = store.update_record(trip_id, transform)
        if result is None:
            raise ProviderError(404, "航班不存在。")
        return with_flight_timezones(result)
    except ProviderError as exc:
        return provider_response(exc)


@app.post("/api/flight/{trip_id}/track")
def update_online_flight_track(trip_id: str, trip: TripCreate) -> Any:
    """Save reviewed online fixes, preserving the original GPS records."""
    from .flight_overlay import (ONLINE_SOURCES, attach_gps_overlay, record_fingerprint)
    from .flight_reconstruction import merge_aircraft_info
    from .providers import ProviderError
    store = TripStore()
    draft = trip.model_dump(mode="json")
    review = draft.get("metadata", {}).pop("online_update", {})
    try:
        if draft.get("source") not in ONLINE_SOURCES or draft.get("mode") != "flight" or len(draft["points"]) < 2:
            raise ProviderError(422, "请先预览有效的在线航班轨迹。")
        attach_gps_overlay(store, draft)
        def transform(record):
            if (review.get("trip_id") != trip_id or review.get("fingerprint") != record_fingerprint(record)
                    or record.get("mode") != "flight" or record.get("service_number") != draft.get("service_number")
                    or record.get("travel_date") != draft.get("travel_date")):
                raise ProviderError(409, "航班记录已变化，请重新预览在线轨迹。")
            if (record.get("points") and record.get("source") not in ONLINE_SOURCES
                    and not record.get("metadata", {}).get("local_recovery")):
                raise ProviderError(409, "原始 GPS 记录不会被在线轨迹替换。")
            merge_aircraft_info(draft.setdefault("metadata", {}).setdefault("flight", {}), record.get("metadata", {}).get("flight", {}))
            draft["notes"] = record.get("notes")
            updated = canonical_trip(TripCreate.model_validate(draft))
            return {**updated, "id": record["id"], "created_at": record["created_at"]}
        result = store.update_record(trip_id, transform)
        if result is None:
            raise ProviderError(404, "航班不存在。")
        return with_flight_timezones(result)
    except ProviderError as exc:
        return provider_response(exc)


@app.post("/api/preview/rail")
async def preview_rail(payload: RailImport) -> Any:
    """Build a train-number track for on-map preview without persisting it."""
    from .providers import ProviderError, fetch_rail
    try:
        draft = await fetch_rail(**payload.model_dump())
        TripCreate.model_validate(draft)
    except ProviderError as exc:
        return provider_response(exc)
    except ValidationError:
        return JSONResponse(status_code=502, content={"detail": "铁路数据源返回无效轨迹", "details": None})
    return {"trip": draft}


class FlightEnrichPayload(BaseModel):
    """Manually attach flight identity (number + date, optional airports) to
    an existing trip; every other field is filled from the data sources."""

    trip_id: str = Field(min_length=1, max_length=100)
    flight_number: str = Field(min_length=1, max_length=100)
    travel_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    origin: str | None = Field(default=None, max_length=300)
    destination: str | None = Field(default=None, max_length=300)
    registration: str | None = Field(default=None, max_length=20)
    icao24: str | None = Field(default=None, max_length=6)
    schedule_candidate_id: str | None = Field(default=None, max_length=24)
    first_seen: int | None = Field(default=None, ge=0)
    last_seen: int | None = Field(default=None, ge=0)


@app.post("/api/flight/enrich")
async def enrich_flight(payload: FlightEnrichPayload) -> Any:
    from .flight_reconstruction import enrich_trip
    from .providers import ProviderError
    store = TripStore()
    if store.get(payload.trip_id) is None:
        raise HTTPException(status_code=404, detail="旅程不存在")
    try:
        record = await enrich_trip(store, payload.trip_id, payload.flight_number,
                                   payload.travel_date, payload.origin, payload.destination,
                                   payload.registration, payload.icao24, payload.schedule_candidate_id,
                                   payload.first_seen, payload.last_seen)
    except ProviderError as exc:
        return provider_response(exc)
    if record is None:
        raise HTTPException(status_code=404, detail="旅程不存在")
    return with_flight_timezones(record)


class SettingsUpdate(BaseModel):
    fr24_api_token: str = Field(max_length=5000)


@app.get("/api/settings")
def get_settings() -> dict[str, bool]:
    load_token()
    return {"fr24_configured": bool(os.environ.get("FR24_API_TOKEN", "").strip())}


@app.put("/api/settings")
def update_settings(payload: SettingsUpdate) -> dict[str, bool]:
    token = payload.fr24_api_token.strip()
    if "\r" in token or "\n" in token:
        raise HTTPException(status_code=422, detail="API token 不能包含换行符")
    directory = default_database_path().parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".settings-", dir=directory)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"fr24_api_token": token}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, directory / "settings.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)
    if token:
        os.environ["FR24_API_TOKEN"] = token
    else:
        os.environ.pop("FR24_API_TOKEN", None)
    return {"fr24_configured": bool(token)}


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str) -> Response:
    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API 路径不存在")
    dist = (PROJECT_ROOT / "dist").resolve()
    candidate = (dist / path).resolve()
    if candidate != dist and dist not in candidate.parents:
        raise HTTPException(status_code=404, detail="文件不存在")
    if candidate.is_file():
        return FileResponse(candidate)
    if path and Path(path).suffix:
        raise HTTPException(status_code=404, detail="文件不存在")
    index = dist / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=503, detail="前端尚未构建，请运行 npm run build 或使用开发服务器")
    return FileResponse(index)
