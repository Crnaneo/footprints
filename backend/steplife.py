"""Streaming, lossless support for the eleven-column StepLife point backup."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .csv_io import CsvImportError
from .models import MAX_POINTS, TripCreate, canonical_trip
from .store import TripStore

FIELDS = ['dataTime', 'locType', 'longitude', 'latitude', 'heading', 'accuracy', 'speed', 'distance', 'isBackForeground', 'stepType', 'altitude']
MAX_BACKUP_BYTES = 512 * 1024 * 1024
MAX_BACKUP_POINTS = 5_000_000
BACKUP_ZONE = timezone(timedelta(hours=8))


def is_steplife(headers: list[str] | None) -> bool:
    return headers is not None and set(FIELDS) == set(headers) and len(headers) == len(FIELDS)


def point_row(values: list[str], row_number: int, fallback_date: str | None = None) -> tuple[float, str, dict]:
    try:
        nums = [float(v) if v.strip() else None for v in values]
        if any(v is not None and not math.isfinite(v) for v in nums):
            raise ValueError('字段需为有限数字')
        epoch, _, lon, lat, _, _, speed, _, _, _, altitude = nums
        if lon is None or lat is None:
            raise ValueError('longitude、latitude 不能为空')
        if epoch is None and not fallback_date:
            raise ValueError('dataTime 为空时需指定出行日期')
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError('经纬度超出有效范围')
        instant = datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else None
        day = instant.astimezone(BACKUP_ZONE).date().isoformat() if instant else date.fromisoformat(fallback_date).isoformat()
        return epoch, day, {
            'longitude': lon, 'latitude': lat,
            'timestamp': instant.isoformat().replace('+00:00', 'Z') if instant else None,
            'speed_kmh': speed * 3.6 if speed is not None and speed >= 0 else None,
            'altitude_m': altitude,
        }
    except (ValueError, OverflowError, OSError) as exc:
        raise CsvImportError('一生足迹 CSV 校验失败，未保存任何轨迹', [{'row': row_number, 'message': str(exc)}]) from exc


def raw_line(values: list[str]) -> str:
    stream = io.StringIO(newline='')
    csv.writer(stream, lineterminator='').writerow(values)
    return stream.getvalue()


def import_backup(path: Path, store: TripStore, *, source_crs: str = 'WGS84', filename: str | None = None, archive_filename: str | None = None, fallback_date: str | None = None) -> dict:
    """Validate/sort on disk, then commit daily trips and original rows atomically.

    Never holds the complete CSV or entire multi-year point collection in RAM.
    Negative CoreLocation speed values are unavailable, not negative motion.
    """
    if path.stat().st_size > MAX_BACKUP_BYTES:
        raise CsvImportError('一生足迹 CSV 文件不能超过 512 MB')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    fingerprint = hashlib.sha256((digest.hexdigest() + '|' + source_crs).encode()).hexdigest()
    existing_dates = set()
    with store.connect() as db:
        existing = db.execute('SELECT id, point_count FROM backup_batches WHERE sha256=?', (fingerprint,)).fetchone()
        if existing:
            summaries = [json.loads(row[0]) for row in db.execute("SELECT summary FROM trips WHERE id IN (SELECT DISTINCT trip_id FROM point_csv WHERE batch_id=?) ORDER BY travel_date", (existing['id'],))]
            # A partially deleted previous import can be restored intentionally.
            if sum(t['point_count'] for t in summaries) == existing['point_count']:
                return {'trips': summaries, 'count': len(summaries), 'point_count': existing['point_count'], 'already_imported': True}
            existing_dates = {t['travel_date'] for t in summaries}
    batch = existing['id'] if existing else str(uuid4())
    count = 0
    fd, staging_name = tempfile.mkstemp(prefix='footprint-backup-', suffix='.sqlite3')
    os.close(fd)
    staging = sqlite3.connect(staging_name)
    try:
        staging.execute('PRAGMA journal_mode=OFF')
        staging.execute('CREATE TABLE rows (ordinal INTEGER PRIMARY KEY, epoch REAL, day TEXT, raw TEXT)')
        pending = []
        with path.open(encoding='utf-8-sig', newline='') as source:
            reader = csv.DictReader(source, strict=True)
            if not is_steplife(reader.fieldnames):
                raise CsvImportError('一生足迹备份需包含指定的 11 列，且列名不能重复')
            for row in reader:
                if not any(v for v in row.values()):
                    continue
                if None in row or any(row.get(k) is None for k in FIELDS):
                    raise CsvImportError('CSV 列数与表头不一致', [{'row': reader.line_num, 'message': '应为 11 列'}])
                values = [row[k] for k in FIELDS]
                epoch, day, _ = point_row(values, reader.line_num, fallback_date)
                pending.append((count, epoch, day, raw_line(values)))
                count += 1
                if count > MAX_BACKUP_POINTS:
                    raise CsvImportError('一次备份最多导入 5,000,000 个点')
                if len(pending) >= 10000:
                    staging.executemany('INSERT INTO rows VALUES (?,?,?,?)', pending)
                    pending.clear()
            staging.executemany('INSERT INTO rows VALUES (?,?,?,?)', pending)
        if not count:
            raise CsvImportError('CSV 没有轨迹点')
        staging.execute('CREATE INDEX rows_day_time ON rows(day,epoch,ordinal)')
        staging.commit()
        days = [row[0] for row in staging.execute('SELECT DISTINCT day FROM rows ORDER BY day')]
        summaries = []
        with store.connect() as db:
            if not existing:
                db.execute('INSERT INTO backup_batches VALUES (?,?,?,?,?,?,?)',
                       (batch, fingerprint, filename or path.name, count, source_crs, 'Asia/Shanghai', datetime.now(timezone.utc).isoformat()))
            for day in days:
                if day in existing_dates:
                    continue
                if staging.execute('SELECT count(*) FROM rows WHERE day=?', (day,)).fetchone()[0] > MAX_POINTS:
                    raise CsvImportError(f'{day} 超过单日 200,000 点限制，请按连续区段拆分备份后导入')
                points, originals = [], []
                previous = None
                segment = 0
                for ordinal, epoch, raw in staging.execute('SELECT ordinal,epoch,raw FROM rows WHERE day=? ORDER BY epoch,ordinal', (day,)):
                    values = next(csv.reader([raw]))
                    _, _, point = point_row(values, ordinal + 2, fallback_date)
                    if previous is not None and (epoch is None) != (previous[0] is None):
                        segment += 1
                    if previous is not None and epoch is not None and previous[0] is not None:
                        dt = epoch - previous[0]
                        # A missing interval or implausible GPS jump must not
                        # become a long straight line across the map.
                        distance = _distance(previous[1], previous[2], point['longitude'], point['latitude'])
                        if dt > 900 or (distance > 5000 and (dt <= 0 or distance / dt > 400)):
                            segment += 1
                    point.update(sequence=len(points), segment_id=str(segment))
                    points.append(point)
                    originals.append((len(points) - 1, ordinal, raw))
                    previous = (epoch, point['longitude'], point['latitude'])
                trip = TripCreate(title=f'{day} · 一生足迹', mode='other', travel_date=day,
                                  source='一生足迹', source_crs=source_crs, geometry_crs=source_crs,
                                  geometry_kind='observed', points=points,
                                  notes='GPS 记录按北京时间分日；保留全部原始点。断开超过 15 分钟的记录间隔和明显定位跳变。',
                                  metadata={'csv_profile': 'steplife-v1', 'backup_batch': batch, 'date_timezone': 'Asia/Shanghai',
                                            'speed_unit': 'm/s', 'raw_columns': FIELDS, 'original_filename': filename or path.name, 'original_file': archive_filename})
                record = canonical_trip(trip)
                record.update(id=str(uuid4()), created_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
                store.write_record(db, record)
                db.executemany('INSERT INTO point_csv VALUES (?,?,?,?,?)',
                               ((record['id'], seq, ordinal, batch, raw) for seq, ordinal, raw in originals))
                summaries.append({k: v for k, v in record.items() if k != 'points'})
        if existing:
            with store.connect() as db:
                summaries = [json.loads(row[0]) for row in db.execute("SELECT summary FROM trips WHERE id IN (SELECT DISTINCT trip_id FROM point_csv WHERE batch_id=?) ORDER BY travel_date", (batch,))]
        return {'trips': summaries, 'count': len(summaries), 'point_count': count, 'already_imported': False}
    except (UnicodeError, csv.Error) as exc:
        raise CsvImportError('CSV 需为有效 UTF-8 文本，并使用正确的引号与分隔符') from exc
    finally:
        staging.close()
        Path(staging_name).unlink(missing_ok=True)


def _distance(x1: float, y1: float, x2: float, y2: float) -> float:
    a, b = math.radians(y1), math.radians(y2)
    h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(x2-x1)/2)**2
    return 12742000 * math.asin(min(1, math.sqrt(h)))


def iter_backup_csv(store: TripStore, trip_id: str | None = None,
                    modes: tuple[str, ...] | None = None) -> Iterator[bytes]:
    yield ('\ufeff' + ','.join(FIELDS) + '\r\n').encode('utf-8')
    # Original lexical values and their file order survive import/export,
    # including -1, zero, precision, heading, accuracy and app flags.
    with store.connect() as db:
        query = 'SELECT p.raw,COALESCE(b.source_crs,"WGS84") FROM point_csv p JOIN trips t ON p.trip_id=t.id LEFT JOIN backup_batches b ON b.id=p.batch_id'
        conditions = []
        args = []
        if trip_id:
            conditions.append('p.trip_id=?')
            args.append(trip_id)
        if modes:
            conditions.append("json_extract(t.payload,'$.mode') IN ({})".format(','.join('?' * len(modes))))
            args.extend(modes)
        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)
        query += ' ORDER BY p.batch_id,p.row_order'
        buf = []
        for row in db.execute(query, args):
            line = row[0]
            if row[1] == 'GCJ02':
                from .models import gcj02_to_wgs84
                values = next(csv.reader([line])); lon,lat=gcj02_to_wgs84(float(values[2]),float(values[3])); values[2:4]=[str(lon),str(lat)];line=raw_line(values)
            buf.append(line + '\r\n')
            if len(buf) >= 1000:
                yield ''.join(buf).encode('utf-8'); buf.clear()
        if buf:
            yield ''.join(buf).encode('utf-8')
        # Non-StepLife sources share the same interchange schema. Unknown
        # timestamps and measurements remain empty / -1, never fabricated.
        sql = "SELECT payload FROM trips t WHERE NOT EXISTS (SELECT 1 FROM point_csv p WHERE p.trip_id=t.id)"
        extra = []
        payload_args = []
        if trip_id:
            extra.append('t.id=?')
            payload_args.append(trip_id)
        if modes:
            extra.append("json_extract(t.payload,'$.mode') IN ({})".format(','.join('?' * len(modes))))
            payload_args.extend(modes)
        if extra:
            sql += ' AND ' + ' AND '.join(extra)
        for row in db.execute(sql, payload_args):
            trip = json.loads(row[0])
            for point in trip['points']:
                stamp = point.get('timestamp')
                epoch = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp() if stamp else ''
                if isinstance(epoch, float) and epoch.is_integer(): epoch = int(epoch)
                coords = (point.get('original_longitude'), point.get('original_latitude'))
                coords = (point['longitude'], point['latitude'])
                values = [str(epoch), '', str(coords[0]), str(coords[1]), '-1', '-1',
                          str(point['speed_kmh'] / 3.6) if point.get('speed_kmh') is not None else '-1',
                          '', '', '', str(point['altitude_m']) if point.get('altitude_m') is not None else '']
                yield (raw_line(values) + '\r\n').encode('utf-8')
