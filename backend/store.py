"""SQLite persistence with one transaction per complete import."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .models import TripCreate, canonical_trip
from .map_points import create_schema, migrate_index, index_record
import threading

MAP_MIGRATION_LOCK = threading.Lock()


def default_database_path() -> Path:
    directory = Path(os.environ.get("FOOTPRINT_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data")))
    return directory / "footprint.sqlite3"


def shift_iso_stamp(stamp: str, days: int) -> str:
    value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    moved = value + timedelta(days=days)
    text = moved.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{text}Z" if stamp.endswith("Z") else text


def shift_trip_date(record: dict[str, Any], new_date: str) -> dict[str, Any]:
    """Re-date a via-reconstructed trip by moving every stored time by the same
    day offset, keeping each clock time unchanged.

    Points, via-segment departures/arrivals and the title's date suffix move
    together so the record never mixes two dates. The relative fields ("time",
    "day_offset") stay as they are: they are defined against travel_date.
    """
    old_date = str(record.get("travel_date") or "")
    try:
        delta = date.fromisoformat(new_date) - date.fromisoformat(old_date)
    except ValueError:
        raise ValueError("日期格式应为 YYYY-MM-DD") from None
    if not delta.days:
        return record
    record["travel_date"] = new_date
    title = record.get("title")
    if isinstance(title, str) and old_date in title:
        record["title"] = title.replace(old_date, new_date)
    for point in record.get("points") or ():
        stamp = point.get("timestamp") if isinstance(point, dict) else None
        if isinstance(stamp, str) and stamp:
            point["timestamp"] = shift_iso_stamp(stamp, delta.days)
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for segment in metadata.get("via_segments") or ():
            if not isinstance(segment, dict):
                continue
            for key in ("departure", "arrival"):
                field = segment.get(key)
                if isinstance(field, dict) and isinstance(field.get("iso"), str) and field["iso"]:
                    field["iso"] = shift_iso_stamp(field["iso"], delta.days)
    return record


class TripStore:
    def __init__(self, database_path: Path | str | None = None):
        self.path = Path(database_path) if database_path is not None else default_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with MAP_MIGRATION_LOCK, self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS trips (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                mode TEXT NOT NULL,
                travel_date TEXT NOT NULL,
                search_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload TEXT NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS trips_mode_date ON trips(mode, travel_date)")
            connection.execute("CREATE TABLE IF NOT EXISTS backup_batches (id TEXT PRIMARY KEY, sha256 TEXT UNIQUE, filename TEXT, point_count INTEGER, source_crs TEXT, date_timezone TEXT, created_at TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS point_csv (trip_id TEXT REFERENCES trips(id) ON DELETE CASCADE, sequence INTEGER, row_order INTEGER, batch_id TEXT, raw TEXT, PRIMARY KEY(trip_id,sequence))")
            connection.execute("CREATE INDEX IF NOT EXISTS point_csv_batch_order ON point_csv(batch_id,row_order)")
            connection.execute("CREATE TABLE IF NOT EXISTS map_previews (trip_id TEXT PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE, payload TEXT NOT NULL)")
            create_schema(connection)
            migrate_index(connection)


    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=60)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def write_record(connection: sqlite3.Connection, record: dict[str, Any]) -> None:
        summary = {key: value for key, value in record.items() if key != "points"}
        search = " ".join(str(record.get(key) or "") for key in ("title", "service_number", "origin", "destination", "notes")).casefold()
        connection.execute("INSERT INTO trips VALUES (?,?,?,?,?,?,?,?)", (
            record["id"], record["title"], record["mode"], record["travel_date"], search, record["created_at"],
            json.dumps(summary, ensure_ascii=False, allow_nan=False), json.dumps(record, ensure_ascii=False, allow_nan=False)))
        # Bounded previews serve the library and initial selection bounds only.
        # The map itself always loads every original point from local tiles.
        groups: dict[str, list[dict]] = {}
        for point in record["points"]:
            groups.setdefault(point["segment_id"], []).append(point)
        selected = []
        for points in groups.values():
            stride = max(1, (len(points) + 159) // 160)
            selected.extend(points[::stride])
            if selected[-1] is not points[-1]: selected.append(points[-1])
        preview = {**summary, "points": [{k: p[k] for k in ("longitude", "latitude", "sequence", "segment_id")} for p in selected], "preview": True}
        connection.execute("INSERT OR REPLACE INTO map_previews VALUES (?,?)", (record["id"], json.dumps(preview, ensure_ascii=False, allow_nan=False)))
        index_record(connection, record)

    def overview(self, include_points: bool = True) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if not include_points:
                # The map reads original points from MVT. List/selection only
                # need summaries and exact indexed bounds, never preview arrays.
                connection.execute('BEGIN')
                revision = connection.execute('SELECT revision FROM map_revision WHERE id=1').fetchone()[0]
                records = []
                for row in connection.execute('SELECT t.summary,b.* FROM trips t JOIN map_bounds b ON b.trip_id=t.id ORDER BY t.travel_date DESC,t.created_at DESC,t.id'):
                    record = json.loads(row['summary'])
                    bounds = None
                    if row['point_count']:
                        west, east = row['west'], row['east']
                        if row['shifted_east'] - row['shifted_west'] < east - west:
                            west, east = row['shifted_west'], row['shifted_east']
                        bounds = [[west, row['south']], [east, row['north']]]
                    records.append({**record, 'points': [], 'preview': True,
                                    'bounds': bounds, 'map_revision': revision})
                return records
            # Migrate old, small records lazily without changing their IDs.
            missing = connection.execute("SELECT payload FROM trips WHERE id NOT IN (SELECT trip_id FROM map_previews)").fetchall()
            for row in missing:
                record = json.loads(row[0])
                preview = {**record, "preview": True}
                connection.execute("INSERT INTO map_previews VALUES (?,?)", (record["id"], json.dumps(preview, ensure_ascii=False)))
            return [json.loads(row[0]) for row in connection.execute("SELECT m.payload FROM map_previews m JOIN trips t ON t.id=m.trip_id ORDER BY t.travel_date DESC,t.created_at DESC,t.id")]

    def create_many(self, trips: list[TripCreate]) -> list[dict[str, Any]]:
        # All validation and serialization finish before SQLite receives any row.
        records = []
        for trip in trips:
            record = canonical_trip(trip)
            record["id"] = str(uuid4())
            record["created_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            records.append(record)
        with self.connect() as connection:
            for record in records:
                self.write_record(connection, record)
        return records

    def create(self, trip: TripCreate) -> dict[str, Any]:
        return self.create_many([trip])[0]

    def list(self, mode: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        clauses, args = [], []
        if mode:
            clauses.append("mode = ?")
            args.append(mode)
        if q:
            clauses.append("instr(search_text, ?) > 0")
            args.append(q.casefold())
        sql = "SELECT summary FROM trips" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        sql += " ORDER BY travel_date DESC, created_at DESC, id"
        with self.connect() as connection:
            return [json.loads(row["summary"]) for row in connection.execute(sql, args)]

    def get(self, trip_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM trips WHERE id = ?", (trip_id,)).fetchone()
            return json.loads(row["payload"]) if row else None

    def all(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [json.loads(row["payload"]) for row in connection.execute(
                "SELECT payload FROM trips ORDER BY travel_date DESC, created_at DESC, id")]

    def observations_between(self, start: date, end: date) -> list[dict[str, Any]]:
        """Bounded local recovery lookup; never load the entire GPS library."""
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM trips WHERE travel_date BETWEEN ? AND ? ORDER BY travel_date,id",
                (start.isoformat(), end.isoformat()))]

    def update_record(self, trip_id: str, transform) -> dict[str, Any] | None:
        """Apply a pure transform to one stored trip, keeping derived rows in sync.

        The transform receives the full record (with points) and returns the
        replacement; previews and the map index are rebuilt from the result so
        a metadata-only edit cannot desynchronize the library views.
        """
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM trips WHERE id = ?", (trip_id,)).fetchone()
            if row is None:
                return None
            record = transform(json.loads(row["payload"]))
            return self.replace_record(connection, record)

    @staticmethod
    def replace_record(connection: sqlite3.Connection, record: dict[str, Any]) -> dict[str, Any]:
        """Replace one existing record inside the caller's transaction."""
        trip_id = record["id"]
        summary = {key: value for key, value in record.items() if key != "points"}
        search = " ".join(str(record.get(key) or "") for key in ("title", "service_number", "origin", "destination", "notes")).casefold()
        connection.execute(
            "UPDATE trips SET title = ?, mode = ?, travel_date = ?, search_text = ?, summary = ?, payload = ? WHERE id = ?",
            (record["title"], record["mode"], record["travel_date"], search,
             json.dumps(summary, ensure_ascii=False, allow_nan=False),
             json.dumps(record, ensure_ascii=False, allow_nan=False), trip_id))
        preview_row = connection.execute("SELECT payload FROM map_previews WHERE trip_id = ?", (trip_id,)).fetchone()
        if preview_row:
            preview = json.loads(preview_row["payload"])
            preview.update(summary)
            # Geometry recovery can change an empty record into a track.
            # Rebuild the bounded map preview as well as the map index.
            groups = {}
            for point in record["points"]:
                groups.setdefault(point["segment_id"], []).append(point)
            sampled = []
            for points in groups.values():
                stride = max(1, (len(points) + 159) // 160)
                sampled.extend(points[::stride])
                if sampled[-1] is not points[-1]:
                    sampled.append(points[-1])
            preview["points"] = [{key: point[key] for key in ("longitude", "latitude", "sequence", "segment_id")} for point in sampled]
            connection.execute("UPDATE map_previews SET payload = ? WHERE trip_id = ?",
                               (json.dumps(preview, ensure_ascii=False, allow_nan=False), trip_id))
        index_record(connection, record)
        return record

    def delete(self, trip_id: str) -> bool:
        with self.connect() as connection:
            return connection.execute("DELETE FROM trips WHERE id = ?", (trip_id,)).rowcount > 0
