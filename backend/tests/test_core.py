from __future__ import annotations

import csv
import io
import json
import math
import os

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.csv_io import MAX_CSV_BYTES, CsvImportError, export_csv, parse_csv
from backend.models import TripCreate, canonical_trip
from backend.store import TripStore, shift_trip_date


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FOOTPRINT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FR24_API_TOKEN", raising=False)
    with TestClient(app) as http:
        yield http


def trip(**overrides):
    values = {
        "title": "北京到上海", "mode": "rail", "travel_date": "2026-10-01",
        "service_number": "G1", "origin": "北京南", "destination": "上海虹桥",
        "source": "csv", "geometry_kind": "imported", "source_crs": "WGS84",
        "points": [{"longitude": 0, "latitude": 0}, {"longitude": 1, "latitude": 0}],
    }
    values.update(overrides)
    return TripCreate(**values)


def test_persists_across_store_instances_and_preserves_source_metadata(tmp_path):
    database = tmp_path / "trips.sqlite3"
    saved = TripStore(database).create(trip(
        design_speed_kmh=350, line_speed_limit_kmh=300,
        metadata={"source_date": "2026-10-01", "historical_verified": False},
        quality_flags=["reconstructed_current_route"],
    ))
    reopened = TripStore(database)
    assert reopened.get(saved["id"]) == saved
    summary = reopened.list(mode="rail", q="北京南")[0]
    assert "points" not in summary
    assert summary["point_count"] == 2
    assert summary["observed_max_speed_kmh"] is None
    assert summary["design_speed_kmh"] == 350
    assert summary["metadata"]["historical_verified"] is False
    assert reopened.list(mode="flight") == []
    assert reopened.delete(saved["id"])
    assert reopened.get(saved["id"]) is None


def test_orders_each_segment_without_connecting_disjoint_segments(tmp_path):
    saved = TripStore(tmp_path / "db.sqlite3").create(trip(points=[
        {"longitude": 1, "latitude": 0, "sequence": 1, "segment_id": "a", "speed_kmh": 42},
        {"longitude": 0, "latitude": 0, "sequence": 0, "segment_id": "a"},
        {"longitude": 101, "latitude": 0, "sequence": 1, "segment_id": "b"},
        {"longitude": 100, "latitude": 0, "sequence": 0, "segment_id": "b"},
    ]))
    assert [p["longitude"] for p in saved["points"]] == [0, 1, 100, 101]
    assert math.isclose(saved["distance_km"], 222.39016, abs_tol=0.00001)
    assert saved["observed_max_speed_kmh"] == 42


def test_csv_validation_is_atomic_and_reports_bad_row(client):
    data = b"track_id,longitude,latitude\none,116.4,39.9\ntwo,121.4,31.2\ntwo,122,91\n"
    response = client.post("/api/import/csv", files={"file": ("track.csv", data, "text/csv")},
                           data={"mode": "rail", "travel_date": "2026-10-01"})
    assert response.status_code == 422
    assert response.json()["details"]["errors"][0]["row"] == 4
    assert client.get("/api/trips").json() == []


def test_csv_export_round_trip_preserves_multiple_trips_and_escapes_formulas(tmp_path):
    store = TripStore(tmp_path / "db.sqlite3")
    records = store.create_many([
        trip(title="=SUM(1,2)", service_number="+G1", source="@rail", notes=" -formula",
             source_crs="GCJ02", geometry_crs="GCJ02",
             metadata={"note": "旧经由未知"},
             points=[{"longitude": 116.4, "latitude": 39.9, "speed_kmh": None, "source": "GPS"},
                     {"longitude": 116.41, "latitude": 39.91, "speed_kmh": 120}]),
        trip(title="'=caption", mode="flight", points=[{"longitude": 103.99, "latitude": 1.36}]),
    ])
    exported = export_csv(records)
    assert exported.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"))))
    assert rows[0]["title"] == "'=SUM(1,2)"
    assert rows[0]["service_number"] == "'+G1"
    assert rows[0]["speed_kmh"] == ""
    assert rows[-1]["title"] == "''=caption"
    parsed = parse_csv(exported)
    round_tripped = TripStore(tmp_path / "second.sqlite3").create_many(parsed)
    assert len(round_tripped) == 2
    for original, restored in zip(records, round_tripped):
        assert restored["title"] == original["title"]
        assert restored["points"] == original["points"]
        assert restored["source_crs"] == original["source_crs"]
        assert restored["geometry_crs"] == "WGS84"
        assert restored["metadata"] == original["metadata"]
    assert records[0]["points"][0]["original_longitude"] == 116.4
    assert abs(records[0]["points"][0]["longitude"] - 116.4) > 0.001


def test_csv_aliases_missing_speed_and_rejects_coordinate_swap():
    parsed = parse_csv("经度,纬度,序号\n121.4,31.2,0\n121.5,31.3,1\n".encode(),
                       title="上海", mode="other", travel_date="2026-10-01")
    assert parsed[0].points[0].speed_kmh is None
    assert parsed[0].points[0].timestamp is None
    with pytest.raises(CsvImportError) as exc:
        parse_csv(b"longitude,latitude\n45,121\n", travel_date="2026-10-01")
    assert exc.value.errors[0]["row"] == 2


def test_fr24_csv_units_and_utc_alias():
    data = b'Timestamp,UTC,Callsign,Position,Altitude,Speed\n1790812800,2026-10-01T00:00:00Z,CA123,"39.9,116.4",10000,100\n'
    imported = parse_csv(data, mode="flight")
    point = imported[0].points[0]
    assert point.longitude == 116.4 and point.latitude == 39.9
    assert point.altitude_m == 3048
    assert point.speed_kmh == pytest.approx(185.2)
    assert point.timestamp == "2026-10-01T00:00:00Z"
    assert imported[0].travel_date.isoformat() == "2026-10-01"


def test_csv_multitrack_import_list_export_and_delete(client, tmp_path):
    data = b"track_id,title,longitude,latitude\na,Flight,103.9,1.3\na,Flight,104,1.4\nb,Rail,116.4,39.9\n"
    response = client.post("/api/import/csv", files={"file": ("record.csv", data)},
                           data={"mode": "other", "travel_date": "2026-10-01"})
    assert response.status_code == 201
    body = response.json()
    assert body["count"] == 2 and body["point_count"] == 3
    records = client.get("/api/trips").json()
    assert len(records) == 2 and all("points" not in record for record in records)
    archive = tmp_path / "imports" / records[0]["metadata"]["original_file"]
    assert archive.read_bytes() == data
    assert records[0]["metadata"]["original_filename"] == "record.csv"
    if os.name != "nt":
        assert (archive.stat().st_mode & 0o777) == 0o600
    selected = records[0]["id"]
    exported = client.get("/api/export.csv", params={"trip_id": selected, "format": "extended"})
    assert exported.status_code == 200
    assert len(parse_csv(exported.content)) == 1
    assert client.delete(f"/api/trips/{selected}").status_code == 204
    assert client.get(f"/api/trips/{selected}").status_code == 404
    assert archive.exists()  # The other trip still refers to the same original.
    other = next(record["id"] for record in records if record["id"] != selected)
    assert client.delete(f"/api/trips/{other}").status_code == 204
    assert not archive.exists()


def test_local_mutations_and_api_fallback_are_protected(client):
    assert client.post("/api/trips", headers={"Origin": "https://example.com"}, json=trip().model_dump(mode="json")).status_code == 403
    assert client.get("/api/health", headers={"Host": "remote.example"}).status_code == 403
    assert client.get("/api/unknown").status_code == 404
    assert client.get("/api/trips").json() == []


def test_settings_persist_privately_without_returning_token(client, tmp_path, monkeypatch):
    response = client.put("/api/settings", json={"fr24_api_token": "test-secret"})
    assert response.json() == {"fr24_configured": True}
    assert "test-secret" not in client.get("/api/settings").text
    settings = tmp_path / "settings.json"
    if os.name != "nt":
        assert (settings.stat().st_mode & 0o777) == 0o600
    assert json.loads(settings.read_text())["fr24_api_token"] == "test-secret"
    monkeypatch.delenv("FR24_API_TOKEN")
    assert client.get("/api/settings").json() == {"fr24_configured": True}
    assert os.environ["FR24_API_TOKEN"] == "test-secret"
    assert client.put("/api/settings", json={"fr24_api_token": ""}).json() == {"fr24_configured": False}
    monkeypatch.setenv("FR24_API_TOKEN", "stale-env-token")
    assert client.get("/api/settings").json() == {"fr24_configured": False}
    assert "FR24_API_TOKEN" not in os.environ


def test_csv_template_is_importable_and_not_saved_to_library(client):
    response = client.get("/api/template.csv")
    assert response.status_code == 200
    assert client.get("/api/trips").json() == []
    imported = client.post("/api/import/csv", files={"file": ("template.csv", response.content)})
    assert imported.status_code == 201
    assert imported.json()["point_count"] == 2


def test_dense_route_with_large_source_metadata_can_be_exported_and_reimported():
    metadata = {"source_payload": "x" * 700, "airport_zones": {"origin": "Asia/Shanghai", "destination": "Asia/Singapore"}}
    record = canonical_trip(trip(
        metadata=metadata, notes="来源备注 " * 150,
        quality_flags=["reconstructed_current_route", "no_observed_speed"],
        points=[{"longitude": 121 + index / 1_000_000, "latitude": 31 + index / 1_000_000,
                 "source": "=POINT", "source_crs": "WGS84"} for index in range(25_000)],
    ))
    record["id"] = "56164412-2b55-4cbe-a1f2-a8748a6e9b69"
    exported = export_csv([record])
    assert len(exported) < MAX_CSV_BYTES
    # Large trip payloads appear once; point-level sources remain available at
    # every fix, including formula-safe values whose encoding marker is inherited.
    assert exported.count(b"x" * 700) == 1
    imported = parse_csv(exported)[0]
    assert len(imported.points) == 25_000
    assert imported.metadata == metadata
    assert imported.notes == record["notes"]
    assert imported.quality_flags == record["quality_flags"]
    assert imported.source == record["source"]
    assert imported.mode == record["mode"]
    assert imported.points[0].model_dump() == record["points"][0]
    assert imported.points[-1].model_dump() == record["points"][-1]


def test_blank_trip_attributes_inherit_but_explicit_conflicts_are_rejected():
    header = "trip_id,title,mode,travel_date,source,source_crs,geometry_crs,notes,metadata,longitude,latitude\n"
    data = header + 'a,原始旅程,rail,2026-10-01,provider,GCJ02,WGS84,备注,"{""flag"":true}",116.4,39.9\n'
    data += "a,,,,,,,,,116.41,39.91\n"
    imported = parse_csv(data.encode())[0]
    assert imported.title == "原始旅程" and imported.mode == "rail"
    assert imported.notes == "备注" and imported.metadata == {"flag": True}
    assert imported.source_crs == "GCJ02" and imported.geometry_crs == "WGS84"
    conflicting = data.replace("a,,,,,,,,,116.41,39.91", "a,另一旅程,,,,,,,,116.41,39.91")
    with pytest.raises(CsvImportError) as error:
        parse_csv(conflicting.encode())
    assert error.value.errors[0]["row"] == 3
    assert "信息不一致" in error.value.errors[0]["message"]


def test_csv_optional_blank_title_and_matching_size_limit(monkeypatch):
    imported = parse_csv(b"longitude,latitude\n121,31\n", title=" \t ", travel_date="2026-10-01")
    assert imported[0].title == "CSV 导入"
    assert MAX_CSV_BYTES == 100 * 1024 * 1024
    # Exercise the over-limit path without allocating a 100 MB test fixture.
    monkeypatch.setattr("backend.csv_io.MAX_CSV_BYTES", 8)
    with pytest.raises(CsvImportError, match="100 MB"):
        parse_csv(b"longitude,latitude\n121,31\n", travel_date="2026-10-01")


def test_csv_field_errors_are_readable_chinese_and_keep_import_atomic(client):
    cases = [
        ("longitude,latitude\n121,31\n121,999\n", "纬度必须在 -90 至 90 之间"),
        ("longitude,latitude\n121,31\n不是数字,31\n", "经度必须是数字"),
        ("longitude,latitude,travel_date\n121,31,2026-10-01\n121,31,2026-02-30\n", "出行日期无效"),
    ]
    for contents, expected in cases:
        response = client.post("/api/import/csv", files={"file": ("bad.csv", contents.encode())},
                               data={"travel_date": "2026-10-01"})
        assert response.status_code == 422
        error = response.json()["details"]["errors"][0]
        assert error["row"] == 3 and expected in error["message"]
        assert "errors.pydantic.dev" not in response.text
        assert "validation error" not in response.text
        assert "input_value" not in response.text
    assert client.get("/api/trips").json() == []


def test_flight_sandbox_saves_marked_sample_without_persisting_secret(client, tmp_path, monkeypatch):
    token = "sandbox-test-secret-value"
    production_token = "production-test-token"
    monkeypatch.setenv("FR24_API_TOKEN", production_token)
    called = []

    async def fetch_sample(provided_token):
        called.append(provided_token)
        return trip(
            title="沙箱示例·THY5LK", mode="flight", travel_date="2024-06-18",
            service_number=None, origin=None, destination=None, source="Flightradar24 Sandbox",
            geometry_kind="observed", quality_flags=["sandbox_sample"],
            metadata={"sandbox": True, "sample_callsign": "THY5LK"},
            points=[{"longitude": 28.7, "latitude": 40.9, "timestamp": "2024-06-18T10:00:00Z"},
                    {"longitude": 28.8, "latitude": 41.0, "timestamp": "2024-06-18T10:01:00Z"},
                    {"longitude": 28.9, "latitude": 41.1, "timestamp": "2024-06-18T10:02:00Z"}],
        ).model_dump(mode="json")

    monkeypatch.setattr("backend.providers.fetch_flight_sandbox", fetch_sample, raising=False)
    response = client.post("/api/import/flight-sandbox", json={"sandbox_token": token})
    assert response.status_code == 201
    record = response.json()
    assert called == [token]
    assert record["source"] == "Flightradar24 Sandbox"
    assert "sandbox_sample" in record["quality_flags"]
    assert record["metadata"]["sandbox"] is True
    assert record["service_number"] is None and record["origin"] is None and record["destination"] is None
    assert token not in response.text
    assert token not in client.get("/api/export.csv").text
    assert client.get(f"/api/trips/{record['id']}").json()["metadata"]["sandbox"] is True
    assert os.environ["FR24_API_TOKEN"] == production_token
    assert not (tmp_path / "settings.json").exists()
    assert not (tmp_path / "imports").exists()
    assert all(token.encode() not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


def test_flight_sandbox_invalid_credentials_never_echo_or_save(client, monkeypatch):
    called = []

    async def unexpected_call(token):
        called.append(token)
        raise AssertionError("provider must not be called for an invalid payload")

    monkeypatch.setattr("backend.providers.fetch_flight_sandbox", unexpected_call, raising=False)
    for payload in ({}, {"sandbox_token": "sandbox\nsecret-value"}, {"sandbox_token": " " * 8}, {"sandbox_token": "tinykey"}):
        response = client.post("/api/import/flight-sandbox", json=payload)
        assert response.status_code == 422
        submitted = payload.get("sandbox_token")
        if submitted and submitted.strip():
            assert submitted not in response.text
        assert "input_value" not in response.text
    assert not called
    assert client.get("/api/trips").json() == []


def test_flight_sandbox_provider_and_invalid_response_failures_do_not_save(client, monkeypatch):
    from backend.providers import ProviderError
    token = "sandbox-test-secret-value"

    async def provider_failure(_token):
        raise ProviderError(403, "沙箱密钥无效")

    monkeypatch.setattr("backend.providers.fetch_flight_sandbox", provider_failure, raising=False)
    assert client.post("/api/import/flight-sandbox", json={"sandbox_token": token}).status_code == 403

    async def invalid_draft(_token):
        return {"title": "沙箱示例", "mode": "flight", "travel_date": "2024-06-18", "points": [{"longitude": 28, "latitude": 999}]}

    monkeypatch.setattr("backend.providers.fetch_flight_sandbox", invalid_draft)
    response = client.post("/api/import/flight-sandbox", json={"sandbox_token": token})
    assert response.status_code == 502 and token not in response.text

    async def diagnostic_with_secret(_token):
        raise ProviderError(502, f"upstream rejected {token}", {"credential": token})

    monkeypatch.setattr("backend.providers.fetch_flight_sandbox", diagnostic_with_secret)
    response = client.post("/api/import/flight-sandbox", json={"sandbox_token": token})
    assert response.status_code == 502 and token not in response.text

    async def sample_with_secret(_token):
        return trip(mode="flight", metadata={"debug": token}).model_dump(mode="json")

    monkeypatch.setattr("backend.providers.fetch_flight_sandbox", sample_with_secret)
    response = client.post("/api/import/flight-sandbox", json={"sandbox_token": token})
    assert response.status_code == 502 and token not in response.text
    assert client.get("/api/trips").json() == []


def via_trip(**overrides):
    values = {
        "title": "大兴机场 → 雄安 · 2026-10-06", "mode": "rail", "travel_date": "2026-10-06",
        "origin": "大兴机场", "destination": "雄安",
        "source": "本地目录经由", "geometry_kind": "reconstructed",
        "quality_flags": ["via_reconstruction", "no_observed_speed"],
        "points": [
            {"longitude": 116.4, "latitude": 39.5, "timestamp": "2026-10-06T08:00:00Z"},
            {"longitude": 116.5, "latitude": 39.6, "timestamp": "2026-10-06T09:30:00Z"},
        ],
        "metadata": {"via_segments": [{"line_name": "京雄城际",
                     "departure": {"time": "08:00", "day_offset": 0, "iso": "2026-10-06T08:00:00Z"},
                     "arrival": {"time": "09:30", "day_offset": 0, "iso": "2026-10-06T09:30:00Z"}}]},
    }
    values.update(overrides)
    return TripCreate(**values)


def test_update_via_trip_date_shifts_every_stored_time(client):
    saved = client.post("/api/trips", json=via_trip().model_dump(mode="json")).json()
    response = client.put(f"/api/trips/{saved['id']}/date", json={"travel_date": "2026-11-01"})
    assert response.status_code == 200
    record = response.json()
    assert record["travel_date"] == "2026-11-01"
    # Clock times stay, the day offset moves; the title's date suffix follows.
    assert [p["timestamp"] for p in record["points"]] == ["2026-11-01T08:00:00Z", "2026-11-01T09:30:00Z"]
    segment = record["metadata"]["via_segments"][0]
    assert segment["departure"]["iso"] == "2026-11-01T08:00:00Z"
    assert segment["departure"]["time"] == "08:00" and segment["departure"]["day_offset"] == 0
    assert segment["arrival"]["iso"] == "2026-11-01T09:30:00Z"
    assert record["title"] == "大兴机场 → 雄安 · 2026-11-01"
    # The saved record, its preview summary and the overview stay in sync.
    assert client.get(f"/api/trips/{saved['id']}").json()["travel_date"] == "2026-11-01"
    overview = client.get("/api/overview").json()
    assert next(t for t in overview if t["id"] == saved["id"])["travel_date"] == "2026-11-01"


def test_update_via_trip_date_keeps_a_trip_without_dates_intact(client):
    saved = client.post("/api/trips", json=via_trip(
        title="无名经由", travel_date="2026-10-06",
        points=[{"longitude": 116.4, "latitude": 39.5, "timestamp": None},
                {"longitude": 116.5, "latitude": 39.6, "timestamp": None}],
        metadata={"via_segments": [{"line_name": "京雄城际"}]},
    ).model_dump(mode="json")).json()
    record = client.put(f"/api/trips/{saved['id']}/date", json={"travel_date": "2026-10-09"}).json()
    assert record["travel_date"] == "2026-10-09"
    assert all(p["timestamp"] is None for p in record["points"])
    assert record["title"] == "无名经由"


def test_update_date_rejects_trips_without_reconstructed_geometry(client):
    saved = client.post("/api/trips", json=trip().model_dump(mode="json")).json()
    response = client.put(f"/api/trips/{saved['id']}/date", json={"travel_date": "2026-10-09"})
    assert response.status_code == 422 and "经由" in response.json()["detail"]


def test_update_date_validates_payload_and_trip(client):
    assert client.put("/api/trips/missing/date", json={"travel_date": "2026-10-09"}).status_code == 404
    saved = client.post("/api/trips", json=via_trip().model_dump(mode="json")).json()
    assert client.put(f"/api/trips/{saved['id']}/date", json={"travel_date": "09-10-2026"}).status_code == 422
    assert client.get(f"/api/trips/{saved['id']}").json()["travel_date"] == "2026-10-06"


def test_shift_trip_date_rejects_invalid_dates():
    record = {"travel_date": "2026-10-06", "points": []}
    with pytest.raises(ValueError):
        shift_trip_date(record, "not-a-date")


def test_export_modes_filter_rail_and_flight(client):
    for mode, track in (("rail", b"track_id,title,longitude,latitude\nr,Rail,116.4,39.9\nr,Rail,116.5,39.95\n"),
                        ("flight", b"track_id,title,longitude,latitude\nf,Flight,103.9,1.3\nf,Flight,104,1.4\n"),
                        ("other", b"track_id,title,longitude,latitude\no,Other,116.4,40.0\no,Other,116.45,40.05\n")):
        assert client.post("/api/import/csv", files={"file": (f"{mode}.csv", track)},
                           data={"mode": mode, "travel_date": "2026-10-01"}).status_code == 201
    everything = client.get("/api/export.csv", params={"format": "extended"})
    assert len(parse_csv(everything.content)) == 3
    filtered = client.get("/api/export.csv", params={"format": "extended", "modes": "rail,flight"})
    rows = parse_csv(filtered.content)
    assert len(rows) == 2
    assert {row.mode for row in rows} == {"rail", "flight"}
    # The steplife format honours the same filter: only the 2 rail points remain.
    backup = client.get("/api/export.csv", params={"modes": "rail"})
    assert backup.headers["content-disposition"].endswith('backUpData-rail.csv"')
    data_rows = [row for row in list(csv.reader(io.StringIO(backup.content.decode("utf-8-sig"))))[1:] if any(row)]
    assert len(data_rows) == 2
    assert client.get("/api/export.csv", params={"modes": "rail,bus"}).status_code == 422
    # No filter means the whole archive, as before.
    assert len(client.get("/api/export.csv").content) > len(backup.content)
