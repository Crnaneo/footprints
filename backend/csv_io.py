"""Strict CSV import and spreadsheet-safe, reversible CSV export."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from datetime import date, datetime, timezone
from typing import Any

from pydantic import ValidationError

from .models import MAX_POINTS, TrackPoint, TripCreate

MAX_CSV_BYTES = 100 * 1024 * 1024
MAX_CSV_TRIPS = 1000
TEXT_ENCODING = "apostrophe-v1"
CSV_FIELDS = [
    "trip_id", "segment_id", "sequence", "mode", "travel_date", "service_number",
    "origin", "destination", "timestamp", "longitude", "latitude", "altitude_m",
    "speed_kmh", "source", "source_crs", "geometry_kind", "title", "geometry_crs",
    "original_longitude", "original_latitude", "design_speed_kmh", "line_speed_limit_kmh",
    "notes", "quality_flags", "metadata", "point_source", "point_source_crs", "csv_text_encoding",
]
POINT_FIELDS = ("segment_id", "sequence", "timestamp", "longitude", "latitude", "altitude_m",
                "speed_kmh", "original_longitude", "original_latitude")
TRIP_FIELDS = tuple(field for field in CSV_FIELDS if field not in {
    *POINT_FIELDS, "trip_id", "point_source", "point_source_crs", "csv_text_encoding",
})


class CsvImportError(ValueError):
    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.errors = errors or []


def normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-()/（）]+", "", value.strip().casefold())


ALIASES: dict[str, tuple[str, ...]] = {
    "trip_id": ("trip_id", "track_id", "轨迹id", "旅程id", "轨迹编号", "旅程编号"),
    "segment_id": ("segment_id", "segment", "段id", "分段", "区段", "区段编号"),
    "sequence": ("sequence", "seq", "index", "序号", "顺序"),
    "longitude": ("longitude", "lon", "lng", "long", "经度"),
    "latitude": ("latitude", "lat", "纬度"),
    "position": ("position", "坐标"),
    "timestamp": ("timestamp", "utc", "time", "datetime", "时间", "时刻", "记录时间", "日期时间"),
    "altitude_m": ("altitude_m", "altitude", "height", "height_m", "海拔", "高度", "海拔米", "高度米"),
    "altitude_ft": ("altitude_ft", "height_ft", "altitudefeet", "高度英尺"),
    "speed_kmh": ("speed_kmh", "speed", "速度", "速度kmh", "速度公里小时"),
    "speed_knots": ("speed_knots", "speed_kts", "knots", "速度节"),
    "mode": ("mode", "type", "交通方式", "类型"),
    "travel_date": ("travel_date", "date", "日期", "出行日期"),
    "service_number": ("service_number", "flight_number", "train_number", "callsign", "航班号", "车次", "班次"),
    "origin": ("origin", "from", "departure", "到发起点", "出发站", "出发地", "起点", "始发站"),
    "destination": ("destination", "to", "arrival", "到达站", "目的地", "终点", "终到站"),
    "title": ("title", "name", "名称", "标题", "旅程名称"),
    "source": ("source", "来源", "数据来源"),
    "source_crs": ("source_crs", "crs", "坐标系", "原始坐标系"),
    "geometry_crs": ("geometry_crs", "轨迹坐标系"),
    "geometry_kind": ("geometry_kind", "轨迹类型"),
    "original_longitude": ("original_longitude", "原始经度"),
    "original_latitude": ("original_latitude", "原始纬度"),
    "design_speed_kmh": ("design_speed_kmh", "设计速度"),
    "line_speed_limit_kmh": ("line_speed_limit_kmh", "线路限速"),
    "notes": ("notes", "备注"),
    "quality_flags": ("quality_flags",),
    "metadata": ("metadata",),
    "point_source": ("point_source", "逐点来源"),
    "point_source_crs": ("point_source_crs", "逐点原始坐标系"),
    "csv_text_encoding": ("csv_text_encoding",),
}
HEADER_TARGETS = {normalize_header(alias): key for key, aliases in ALIASES.items() for alias in aliases}
FIELD_LABELS = {
    "longitude": "经度", "latitude": "纬度", "original_longitude": "原始经度", "original_latitude": "原始纬度",
    "timestamp": "时间", "sequence": "序号", "segment_id": "区段编号", "altitude_m": "高度", "speed_kmh": "速度",
    "title": "标题", "mode": "交通方式", "travel_date": "出行日期", "service_number": "航班号或车次",
    "origin": "出发地", "destination": "目的地", "source": "数据来源", "source_crs": "来源坐标系",
    "geometry_crs": "轨迹坐标系", "geometry_kind": "轨迹类型", "notes": "备注", "quality_flags": "来源提示",
    "metadata": "来源信息", "points": "轨迹点", "design_speed_kmh": "设计速度", "line_speed_limit_kmh": "线路限速",
}


def validation_message(error: ValidationError) -> str:
    """Expose useful field errors without Python internals or documentation URLs."""
    messages = []
    for issue in error.errors(include_url=False, include_input=False):
        field = next((part for part in reversed(issue["loc"]) if isinstance(part, str)), "")
        label = FIELD_LABELS.get(field, "字段")
        kind, context = issue["type"], issue.get("ctx", {})
        if field in {"longitude", "original_longitude"}:
            message = f"{label}必须在 -180 至 180 之间"
        elif field in {"latitude", "original_latitude"}:
            message = f"{label}必须在 -90 至 90 之间"
        elif kind in {"float_parsing", "float_type", "int_parsing", "int_type", "finite_number"}:
            message = f"{label}必须是有效数字"
        elif kind == "greater_than_equal":
            message = f"{label}不能小于 {context.get('ge', 0)}"
        elif kind == "string_too_long":
            message = f"{label}不能超过 {context.get('max_length')} 个字符"
        elif kind == "string_too_short":
            message = f"{label}不能为空"
        elif field == "mode":
            message = "交通方式仅支持 flight、rail 或 other"
        elif field == "geometry_kind":
            message = "轨迹类型仅支持 observed、reconstructed 或 imported"
        elif field in {"source_crs", "geometry_crs"}:
            message = f"{label}仅支持 WGS84 或 GCJ02"
        elif field == "travel_date":
            message = "出行日期无效，请填写 YYYY-MM-DD 格式的真实日期"
        elif field == "metadata":
            message = "来源信息格式无效，需为不超过 100000 字符的有效 JSON 对象"
        elif field == "quality_flags":
            message = "来源提示需为 JSON 字符串数组"
        elif "duplicate sequence" in issue["msg"]:
            message = "同一区段内的序号不能重复"
        elif "sequence must be supplied" in issue["msg"]:
            message = "同一区段需全部填写序号，或全部留空"
        else:
            message = f"{label}格式无效，请检查字段内容"
        if message not in messages:
            messages.append(message)
    return "；".join(messages[:5])


def normalize_crs(value: str) -> str:
    normalized = normalize_header(value).upper()
    if normalized not in {"WGS84", "GCJ02"}:
        raise ValueError("坐标系仅支持 WGS84 或 GCJ02")
    return normalized


def numeric(value: str, field: str, optional: bool = False) -> float | None:
    if not value.strip():
        if optional:
            return None
        raise ValueError(f"{field}不能为空")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"{field}必须是数字") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field}必须是有限数字")
    return result


def parse_timestamp(value: str) -> str | None:
    if not value.strip():
        return None
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value.strip()):
        try:
            # Timestamp is UNIX seconds in Flightradar24 CSV. Milliseconds are
            # accepted for GPS exports but a date is never guessed from latitude.
            epoch = float(value)
            if abs(epoch) > 100_000_000_000:
                epoch /= 1000
            return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("时间戳超出支持范围") from exc
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("时间需为 ISO 8601 日期时间或 Unix 时间戳") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: str) -> str:
    # The version marker makes this escaping reversible only for our exports.
    if value.startswith("'") or (value and (value[0] in "\t\r\n" or value.lstrip().startswith(("=", "+", "-", "@")))):
        return "'" + value
    return value


def _original_text(value: str, marked: bool) -> str:
    return value[1:] if marked and value.startswith("'") else value


def export_csv(trips: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    for trip in trips:
        for index, point in enumerate(trip["points"]):
            # Trip metadata is a property of the whole trip. Repeating notes and
            # source payloads at every GPS fix can turn a small route into GBs.
            row = {field: trip.get(field) if index == 0 else "" for field in TRIP_FIELDS}
            row.update({field: point.get(field) for field in POINT_FIELDS})
            row["trip_id"] = trip["id"]
            row["point_source"] = point.get("source")
            row["point_source_crs"] = point.get("source_crs")
            row["csv_text_encoding"] = TEXT_ENCODING if index == 0 else ""
            if index == 0:
                row["geometry_crs"] = "WGS84"
                row["quality_flags"] = json.dumps(trip.get("quality_flags", []), ensure_ascii=False, separators=(",", ":"))
                row["metadata"] = json.dumps(trip.get("metadata", {}), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            for field, value in row.items():
                if isinstance(value, str):
                    row[field] = _safe_text(value)
                elif value is None:
                    row[field] = ""
            writer.writerow(row)
    return stream.getvalue().encode("utf-8-sig")


def parse_csv(
    data: bytes,
    *,
    title: str = "CSV 导入",
    mode: str = "other",
    travel_date: str | None = None,
    source_crs: str = "WGS84",
    column_mapping: dict[str, str] | None = None,
) -> list[TripCreate]:
    if len(data) > MAX_CSV_BYTES:
        raise CsvImportError("CSV 文件不能超过 100 MB")
    if not title.strip():
        title = "CSV 导入"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvImportError("CSV 需使用 UTF-8 编码，可包含 BOM") from exc
    if "\x00" in text:
        raise CsvImportError("CSV 包含无效 NUL 字符")
    if not text.strip():
        raise CsvImportError("CSV 文件为空")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
    try:
        headers = reader.fieldnames
    except csv.Error as exc:
        raise CsvImportError("CSV 表头格式无效") from exc
    if not headers:
        raise CsvImportError("CSV 缺少表头")
    normalized_headers = [normalize_header(h) for h in headers]
    if len(set(normalized_headers)) != len(normalized_headers):
        raise CsvImportError("CSV 含重复列名")
    columns: dict[str, str] = {}
    ambiguous: set[str] = set()
    for header in headers:
        target = HEADER_TARGETS.get(normalize_header(header))
        if target:
            if target in columns and target != "timestamp":
                ambiguous.add(target)
            # UTC includes timezone-friendly text and wins over UNIX Timestamp.
            if target != "timestamp" or target not in columns or normalize_header(header) == "utc":
                columns[target] = header
    for left, right in (column_mapping or {}).items():
        if left in ALIASES and right in headers:
            columns[left] = right
            ambiguous.discard(left)
        elif right in ALIASES and left in headers:
            columns[right] = left
            ambiguous.discard(right)
        else:
            raise CsvImportError(f"无效列映射 {left}: {right}；使用 标准字段名: 文件列名")
    if ambiguous:
        raise CsvImportError(f"多个列对应同一字段 {', '.join(sorted(ambiguous))}，请使用 column_mapping 指定")
    if not ({"longitude", "latitude"} <= columns.keys()) and "position" not in columns:
        raise CsvImportError("CSV 缺少经度或纬度列；可使用 longitude/latitude、lon/lat、经度/纬度，或指定 column_mapping")
    fr24_profile = {"timestamp", "utc"} <= set(normalized_headers) and bool({"position", "callsign"} & set(normalized_headers))
    if fr24_profile:
        if "altitude_m" in columns and normalize_header(columns["altitude_m"]) == "altitude":
            columns["altitude_ft"] = columns.pop("altitude_m")
        if "speed_kmh" in columns and normalize_header(columns["speed_kmh"]) == "speed":
            columns["speed_knots"] = columns.pop("speed_kmh")
    try:
        default_crs = normalize_crs(source_crs)
    except ValueError as exc:
        raise CsvImportError(str(exc)) from exc

    groups: dict[str, dict[str, Any]] = {}
    text_encodings: dict[str, bool] = {}
    errors: list[dict[str, Any]] = []
    point_count = 0
    try:
        for row in reader:
            line = reader.line_num
            if not any(value for key, value in row.items() if key is not None):
                continue
            if point_count >= MAX_POINTS:
                raise CsvImportError(f"一次导入最多 {MAX_POINTS:,} 个轨迹点")
            point_count += 1
            if None in row or any(row.get(h) is None for h in headers):
                if len(errors) < 100:
                    errors.append({"row": line, "message": "列数与表头不一致"})
                continue
            raw_key = row.get(columns.get("trip_id", ""), "") or "__single_trip__"
            encoding = row.get(columns.get("csv_text_encoding", ""), "")
            marked = encoding == TEXT_ENCODING if encoding else text_encodings.get(raw_key, False)

            def value(field: str, default: str = "") -> str:
                column = columns.get(field)
                if column is None:
                    return default
                return _original_text(row[column], marked)

            try:
                timestamp = parse_timestamp(value("timestamp"))
                key = value("trip_id") or "__single_trip__"
                previous = groups[key]["attributes"] if key in groups else {}

                def attribute(field: str, fallback: Any = None, parse: Any = None) -> Any:
                    raw = value(field)
                    if raw == "":
                        return previous.get(field, fallback)
                    return parse(raw) if parse else raw

                row_date = attribute("travel_date", travel_date or (timestamp[:10] if timestamp else ""))
                if not row_date:
                    raise ValueError("缺少出行日期；请填写 travel_date，或在 CSV 提供日期/时间列")
                try:
                    parsed_date = date.fromisoformat(row_date.strip()).isoformat()
                except ValueError:
                    raise ValueError("出行日期无效，请填写 YYYY-MM-DD 格式的真实日期") from None
                row_crs = attribute("source_crs", default_crs, normalize_crs)
                geometry_crs = attribute("geometry_crs", row_crs, normalize_crs)
                row_mode = attribute("mode", mode).strip().lower()
                row_mode = {"飞机": "flight", "航班": "flight", "高铁": "rail", "火车": "rail", "铁路": "rail", "其他": "other"}.get(row_mode, row_mode)
                attributes: dict[str, Any] = {
                    "title": attribute("title", title),
                    "mode": row_mode,
                    "travel_date": parsed_date,
                    "service_number": attribute("service_number"),
                    "origin": attribute("origin"),
                    "destination": attribute("destination"),
                    "source": attribute("source", "csv"),
                    "source_crs": row_crs,
                    "geometry_crs": geometry_crs,
                    "geometry_kind": attribute("geometry_kind", "imported").strip().lower(),
                    "notes": attribute("notes"),
                    "design_speed_kmh": attribute("design_speed_kmh", parse=lambda raw: numeric(raw, "设计速度")),
                    "line_speed_limit_kmh": attribute("line_speed_limit_kmh", parse=lambda raw: numeric(raw, "线路限速")),
                    "quality_flags": attribute("quality_flags", [], json.loads),
                    "metadata": attribute("metadata", {}, json.loads),
                }
                if {"longitude", "latitude"} <= columns.keys():
                    lon = numeric(value("longitude"), "经度")
                    lat = numeric(value("latitude"), "纬度")
                else:
                    position = value("position").split(",")
                    if len(position) != 2:
                        raise ValueError("Position 需为明确的 纬度,经度（Flightradar24 格式）")
                    lat = numeric(position[0], "纬度")
                    lon = numeric(position[1], "经度")
                altitude = numeric(value("altitude_m"), "高度", optional=True)
                if altitude is None and value("altitude_ft"):
                    altitude = numeric(value("altitude_ft"), "高度英尺") * 0.3048
                speed = numeric(value("speed_kmh"), "速度", optional=True)
                if speed is None and value("speed_knots"):
                    speed = numeric(value("speed_knots"), "速度节") * 1.852
                sequence_text = value("sequence")
                sequence = None
                if sequence_text:
                    if not re.fullmatch(r"\d+", sequence_text.strip()):
                        raise ValueError("序号需为非负整数")
                    sequence = int(sequence_text)
                point = TrackPoint(
                    longitude=lon, latitude=lat, timestamp=timestamp,
                    altitude_m=altitude, speed_kmh=speed,
                    sequence=sequence, segment_id=value("segment_id") or "0",
                    original_longitude=numeric(value("original_longitude"), "原始经度", optional=True),
                    original_latitude=numeric(value("original_latitude"), "原始纬度", optional=True),
                    source=value("point_source") or None,
                    source_crs=normalize_crs(value("point_source_crs")) if value("point_source_crs") else None,
                )
                if key not in groups:
                    if len(groups) >= MAX_CSV_TRIPS:
                        raise ValueError(f"一次导入最多 {MAX_CSV_TRIPS} 个旅程")
                    groups[key] = {"attributes": attributes, "points": [], "first_row": line}
                elif groups[key]["attributes"] != attributes:
                    raise ValueError("同一 trip_id/track_id 的旅程信息不一致；请为不同旅程使用不同轨迹编号")
                groups[key]["points"].append(point)
                text_encodings.setdefault(raw_key, marked)
            except ValidationError as exc:
                if len(errors) < 100:
                    errors.append({"row": line, "message": validation_message(exc)})
            except json.JSONDecodeError:
                if len(errors) < 100:
                    errors.append({"row": line, "message": "来源信息或来源提示的 JSON 格式无效"})
            except TypeError:
                if len(errors) < 100:
                    errors.append({"row": line, "message": "字段格式无效，请检查数字和来源信息"})
            except ValueError as exc:
                if len(errors) < 100:
                    errors.append({"row": line, "message": str(exc)})
    except csv.Error as exc:
        raise CsvImportError("CSV 格式无效", [{"row": reader.line_num, "message": "CSV 引号或分隔符格式无效"}]) from exc
    if errors:
        raise CsvImportError("CSV 校验失败，未保存任何轨迹；请修正以下行", errors)
    if not point_count:
        raise CsvImportError("CSV 没有轨迹点")
    trips = []
    for group in groups.values():
        try:
            trips.append(TripCreate(**group["attributes"], points=group["points"]))
        except ValidationError as exc:
            errors.append({"row": group["first_row"], "message": validation_message(exc)})
    if errors:
        raise CsvImportError("CSV 旅程校验失败，未保存任何轨迹", errors)
    return trips
