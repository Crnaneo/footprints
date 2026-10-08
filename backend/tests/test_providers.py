import asyncio
import copy
from datetime import date

import httpx
import pytest

from backend import providers as p

ASYNC_CLIENT = httpx.AsyncClient


AIRPORTS = {
    "ZBAA": {"icao": "ZBAA", "iata": "PEK", "tz": "Asia/Shanghai"},
    "ZSPD": {"icao": "ZSPD", "iata": "PVG", "tz": "Asia/Shanghai"},
    "KJFK": {"icao": "KJFK", "iata": "JFK", "tz": "America/New_York"},
    "KLAX": {"icao": "KLAX", "iata": "LAX", "tz": "America/Los_Angeles"},
}
AIRPORTS.update({item["iata"]: item for item in list(AIRPORTS.values())})


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    monkeypatch.setattr(p, "_today", lambda: date(2026, 10, 2))
    monkeypatch.setattr(p, "_airports", lambda: AIRPORTS)


def mocked(monkeypatch, handler):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(p.httpx, "AsyncClient", lambda **kwargs: ASYNC_CLIENT(transport=httpx.MockTransport(wrapped), **kwargs))
    return calls


def row(**values):
    return {"fr24_id": "abc123", "flight": "CA123", "orig_icao": "ZBAA", "dest_icao": "ZSPD",
            "datetime_takeoff": "2026-10-01T02:00:00Z", "datetime_landed": "2026-10-01T04:00:00Z",
            "first_seen": "2026-10-01T01:30:00Z", "flight_ended": True, **values}


def tracks(**values):
    return {"fr24_id": "abc123", "tracks": [
        {"timestamp": "2026-10-01T02:01:00Z", "lon": 116.6, "lat": 40.1, "alt": 1000, "gspeed": 100, "source": "ADSB"},
        {"timestamp": "2026-10-01T02:02:00Z", "lon": 116.61, "lat": 40.11, "alt": None, "gspeed": None, "source": "ESTIMATED"},
    ], **values}


def flight_handler(summary=None, track=None):
    return lambda request: httpx.Response(200, json={"data": [row()] if summary is None else summary}
                                          if "summary" in request.url.path else (track or tracks()))


def test_flight_missing_token(monkeypatch):
    monkeypatch.delenv("FR24_API_TOKEN")
    with pytest.raises(p.ProviderError, match="左上设置") as exc:
        asyncio.run(p.fetch_flight("CA123", "2026-10-01"))
    assert exc.value.status_code == 503


def test_flight_units_missing_values_source_and_secret(monkeypatch):
    calls = mocked(monkeypatch, flight_handler())
    draft = asyncio.run(p.fetch_flight("ca 123", "2026-10-01", "PEK", "PVG"))
    assert draft["points"][0]["altitude_m"] == pytest.approx(304.8)
    assert draft["points"][0]["speed_kmh"] == pytest.approx(185.2)
    assert draft["points"][1]["speed_kmh"] is None
    assert draft["points"][1]["source"] == "ESTIMATED"
    assert "estimated_positions" in draft["quality_flags"]
    assert "test-local-secret" not in str(draft)
    assert calls[0].headers["Authorization"] == "Bearer test-local-secret"
    assert calls[0].headers["Accept-Version"] == "v1"
    assert calls[0].url.params["flight_datetime_from"] == "2026-09-30T10:00:00Z"
    assert calls[1].url.params["flight_id"] == "abc123"


def test_flight_uses_origin_local_date_in_new_york(monkeypatch):
    # 01:00 UTC on October 2 is still October 1 in New York.
    departure = row(orig_icao="KJFK", dest_icao="KLAX", datetime_takeoff="2026-10-02T01:00:00Z")
    mocked(monkeypatch, flight_handler([departure]))
    draft = asyncio.run(p.fetch_flight("CA123", "2026-10-01", "JFK", "LAX"))
    assert draft["metadata"]["date_timezone"] == "America/New_York"
    assert draft["metadata"]["flight"]["departure_local"].startswith("2026-10-01T21:00:00")


def test_flight_unknown_timezone_does_not_guess(monkeypatch):
    mocked(monkeypatch, flight_handler([row(orig_icao="ZZZZ")]))
    with pytest.raises(p.ProviderError, match="时区") as exc:
        asyncio.run(p.fetch_flight("CA123", "2026-10-01"))
    assert exc.value.status_code == 422


def test_flight_multiple_candidates_and_selected_instance(monkeypatch):
    summaries = [row(), row(fr24_id="def456", datetime_takeoff="2026-10-01T07:00:00Z")]
    calls = mocked(monkeypatch, flight_handler(summaries))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(p.fetch_flight("CA123", "2026-10-01"))
    assert exc.value.status_code == 409
    assert len(exc.value.details["candidates"]) == 2
    assert len(calls) == 1  # No paid track fetch before disambiguation.
    mocked(monkeypatch, flight_handler(summaries, tracks(fr24_id="def456")))
    draft = asyncio.run(p.fetch_flight("CA123", "2026-10-01", fr24_id="def456"))
    assert draft["metadata"]["flight"]["fr24_id"] == "def456"


def test_selected_id_cannot_bypass_original_date(monkeypatch):
    calls = mocked(monkeypatch, flight_handler([row(datetime_takeoff="2026-09-30T02:00:00Z")]))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(p.fetch_flight("CA123", "2026-10-01", fr24_id="abc123"))
    assert exc.value.status_code == 404
    assert len(calls) == 1


def test_selected_id_cannot_bypass_airport_filter(monkeypatch):
    mocked(monkeypatch, flight_handler())
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(p.fetch_flight("CA123", "2026-10-01", origin="JFK", fr24_id="abc123"))
    assert exc.value.status_code == 404


def test_flight_tracks_response_must_match_instance(monkeypatch):
    mocked(monkeypatch, flight_handler(track=tracks(fr24_id="other")))
    with pytest.raises(p.ProviderError, match="不匹配"):
        asyncio.run(p.fetch_flight("CA123", "2026-10-01"))


@pytest.mark.parametrize("envelope", ["root_list", "data_list", "data_object", "single_object"])
def test_track_response_envelopes_match_instance_not_first_array_record(monkeypatch, envelope):
    selected = tracks()
    if envelope == "root_list":
        response = [tracks(fr24_id="unrelated"), selected]
    elif envelope == "data_list":
        response = {"data": [tracks(fr24_id="unrelated"), selected]}
    elif envelope == "data_object":
        response = {"data": selected}
    else:
        response = selected
    mocked(monkeypatch, flight_handler(track=response))
    draft = asyncio.run(p.fetch_flight("CA123", "2026-10-01", fr24_id="abc123"))
    assert len(draft["points"]) == 2
    assert draft["metadata"]["flight"]["fr24_id"] == "abc123"
    assert draft["points"][0]["altitude_m"] == pytest.approx(304.8)


@pytest.mark.parametrize("payload", [
    [tracks(fr24_id="unrelated")],
    {"data": [tracks(fr24_id="unrelated")]},
    [tracks(), tracks()],
    {"data": [tracks(), tracks()]},
    [{"tracks": tracks()["tracks"]}],
    tracks()["tracks"],
])
def test_array_track_response_rejects_wrong_missing_or_duplicate_instance_id(payload):
    with pytest.raises(p.ProviderError) as exc:
        p._normalize_flight_tracks(payload, "abc123")
    assert exc.value.status_code == 502


@pytest.mark.parametrize("payload", [[], {"data": []}, {"data": None}, tracks(tracks=None)])
def test_empty_track_envelopes_are_no_data_not_success(payload):
    with pytest.raises(p.ProviderError) as exc:
        normalized = p._normalize_flight_tracks(payload, "abc123")
        p._flight_points(normalized)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("payload", [{"data": "bad"}, tracks(tracks={"lat": 30}), {"data": [None]}])
def test_invalid_track_envelopes_are_rejected(payload):
    with pytest.raises(p.ProviderError) as exc:
        p._normalize_flight_tracks(payload, "abc123")
    assert exc.value.status_code == 502


def test_flight_gap_segmentation_and_live_status(monkeypatch):
    data = tracks()
    data["tracks"][1]["timestamp"] = "2026-10-01T02:31:00Z"
    mocked(monkeypatch, flight_handler([row(flight_ended=False)], data))
    draft = asyncio.run(p.fetch_flight("CA123", "2026-10-01"))
    assert draft["points"][0]["segment_id"] != draft["points"][1]["segment_id"]
    assert {"source_gaps", "partial_live_track"}.issubset(draft["quality_flags"])


def test_flight_live_flag_from_official_string_boolean(monkeypatch):
    mocked(monkeypatch, flight_handler([row(flight_ended="false")]))
    draft = asyncio.run(p.fetch_flight("CA123", "2026-10-01"))
    assert draft["metadata"]["flight"]["flight_ended"] is False
    assert "partial_live_track" in draft["quality_flags"]


def sandbox_tracks(fr24_id="35dc6f7d"):
    return {"fr24_id": fr24_id, "tracks": [
        {"timestamp": "2024-06-18T10:32:02Z", "lat": 55.54876, "lon": 37.13967,
         "alt": 3750, "gspeed": 212, "callsign": "THY5LK", "source": "MLAT"},
        {"timestamp": "2024-06-18T10:32:08Z", "lat": 55.54722, "lon": 37.12818,
         "alt": 4150, "gspeed": 215, "callsign": "THY5LK", "source": "MLAT"},
        {"timestamp": "2024-06-18T10:32:22Z", "lat": 55.54474, "lon": 37.10452,
         "alt": 4675, "gspeed": 215, "callsign": "THY5LK", "source": "MLAT"},
    ]}


def test_sandbox_fetch_uses_explicit_token_and_never_combines_unrelated_summary(monkeypatch):
    calls = mocked(monkeypatch, lambda request: httpx.Response(200, json=[sandbox_tracks()]))
    draft = asyncio.run(p.fetch_flight_sandbox("explicit-sandbox-key"))
    assert len(calls) == 1 and calls[0].url.path.endswith("/flight-tracks")
    assert calls[0].headers["Authorization"] == "Bearer explicit-sandbox-key"
    assert draft["title"] == "沙箱示例 · THY5LK"
    assert draft["travel_date"] == "2024-06-18"
    assert draft["service_number"] is None and draft["origin"] is None and draft["destination"] is None
    assert draft["metadata"]["sandbox"] is True
    assert draft["metadata"]["fr24_id"] == "35dc6f7d"
    assert draft["metadata"]["date_basis"] == "first_point_utc"
    assert "sandbox_sample" in draft["quality_flags"]
    assert draft["source"] == "Flightradar24 Sandbox"
    assert draft["points"][0]["altitude_m"] == pytest.approx(1143)
    assert draft["points"][0]["speed_kmh"] == pytest.approx(392.624)
    assert draft["points"][0]["source"] == "MLAT"
    assert "explicit-sandbox-key" not in str(draft) and "test-local-secret" not in str(draft)


def test_sandbox_record_id_is_from_response_not_ignored_placeholder_query(monkeypatch):
    mocked(monkeypatch, lambda request: httpx.Response(200, json={"data": [sandbox_tracks("87654321")]}))
    draft = asyncio.run(p.fetch_flight_sandbox("sandbox-key"))
    assert draft["metadata"]["fr24_id"] == "87654321"


def test_sandbox_multiple_valid_records_are_not_mixed(monkeypatch):
    mocked(monkeypatch, lambda request: httpx.Response(200, json=[sandbox_tracks(), sandbox_tracks("87654321")]))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(p.fetch_flight_sandbox("sandbox-key"))
    assert exc.value.status_code == 409


@pytest.mark.parametrize("sample", [sandbox_tracks("invalid-ID"), {"fr24_id": "35dc6f7d", "tracks": []}])
def test_sandbox_requires_valid_record_id_and_points(monkeypatch, sample):
    mocked(monkeypatch, lambda request: httpx.Response(200, json=[sample]))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(p.fetch_flight_sandbox("sandbox-key"))
    assert exc.value.status_code == 404


def test_sandbox_skips_invalid_record_without_mixing_it_with_valid_specimen(monkeypatch):
    mocked(monkeypatch, lambda request: httpx.Response(200, json=[None, {"tracks": []}, sandbox_tracks()]))
    draft = asyncio.run(p.fetch_flight_sandbox("sandbox-key"))
    assert len(draft["points"]) == 3


def test_sandbox_does_not_read_configured_production_token(monkeypatch):
    monkeypatch.delenv("FR24_API_TOKEN")
    mocked(monkeypatch, lambda request: httpx.Response(200, json=[sandbox_tracks()]))
    assert asyncio.run(p.fetch_flight_sandbox("sandbox-key"))["metadata"]["sandbox"] is True


@pytest.mark.parametrize("status", [401, 403])
def test_sandbox_auth_error_directs_to_sandbox_key_without_upstream_payload(monkeypatch, status):
    mocked(monkeypatch, lambda request: httpx.Response(status, json={"message": "secret-upstream-detail"}))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(p.fetch_flight_sandbox("sandbox-key"))
    assert exc.value.status_code == 403
    assert "Key management" in exc.value.message and "Sandbox API key" in exc.value.message
    assert "套餐" not in exc.value.message and "secret-upstream-detail" not in exc.value.message


@pytest.mark.parametrize("token", ["", "with\nnewline", "中文"])
def test_sandbox_rejects_missing_or_invalid_key_without_network(monkeypatch, token):
    calls = mocked(monkeypatch, lambda request: httpx.Response(200, json=[sandbox_tracks()]))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(p.fetch_flight_sandbox(token))
    assert exc.value.status_code == 422
    assert not calls


def test_flight_duplicate_time_with_distant_positions_is_a_gap(monkeypatch):
    data = tracks()
    data["tracks"][1].update(timestamp=data["tracks"][0]["timestamp"], lon=125, lat=35)
    mocked(monkeypatch, flight_handler(track=data))
    draft = asyncio.run(p.fetch_flight("CA123", "2026-10-01"))
    assert draft["points"][0]["segment_id"] != draft["points"][1]["segment_id"]
    assert "source_gaps" in draft["quality_flags"]


def test_flight_unknown_time_is_retained_without_a_fabricated_connection(monkeypatch):
    data = tracks()
    data["tracks"][1]["timestamp"] = "not-a-time"
    mocked(monkeypatch, flight_handler(track=data))
    draft = asyncio.run(p.fetch_flight("CA123", "2026-10-01"))
    assert draft["points"][1]["timestamp"] is None
    assert draft["points"][0]["segment_id"] != draft["points"][1]["segment_id"]
    assert "missing_timestamps" in draft["quality_flags"]


@pytest.mark.parametrize("status,expected", [(401, 403), (429, 429), (402, 402), (500, 502)])
def test_provider_errors_are_actionable(monkeypatch, status, expected):
    mocked(monkeypatch, lambda request: httpx.Response(status, json={"message": "upstream message"}))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(p.fetch_flight("CA123", "2026-10-01"))
    assert exc.value.status_code == expected
    assert "upstream message" not in exc.value.message


def test_provider_timeout(monkeypatch):
    def timed_out(request):
        raise httpx.ReadTimeout("network", request=request)

    mocked(monkeypatch, timed_out)
    with pytest.raises(p.ProviderError, match="超时") as exc:
        asyncio.run(p.fetch_flight("CA123", "2026-10-01"))
    assert exc.value.status_code == 504


def test_older_flight_does_not_batch_snapshot_requests(monkeypatch):
    calls = mocked(monkeypatch, flight_handler())
    with pytest.raises(p.ProviderError, match="三年"):
        asyncio.run(p.fetch_flight("CA123", "2019-01-01"))
    assert not calls


def rail_payload(overnight=False):
    return {"success": True, "data": {"numberFull": ["G1"], "rundays": ["20261001", "20261002", "20261003"], "car": "CR400",
        "timetable": [
            {"station": "甲", "stationTelecode": "AAA", "day": 0, "arrive": "22:00", "depart": "22:00", "trainCode": "G1"},
            {"station": "乙", "stationTelecode": "BBB", "day": 1 if overnight else 0, "arrive": "01:00" if overnight else "22:20", "depart": "01:02" if overnight else "22:22", "trainCode": "G1"},
            {"station": "丙", "stationTelecode": "CCC", "day": 1 if overnight else 0, "arrive": "02:00" if overnight else "23:00", "depart": "02:00" if overnight else "23:00", "trainCode": "G1"},
    ]}}


def map_payload():
    return {"success": True, "data": {"stations": [{"甲": [110, 30]}, {"乙": [110.1, 30]}, {"丙": [110.2, 30]}],
        "train": {"甲-乙": {"index": 0, "line": [[110, 30], [110.05, 30], [110.1, 30]]},
                  "乙-丙": {"index": 1, "line": [[110.1, 30], [110.15, 30], [110.2, 30]]}}}}


def rail_handler(main=None, line=None):
    return lambda request: httpx.Response(200, json=(line or map_payload()) if "mapLine" in request.url.path else (main or rail_payload()))


def test_rail_clips_to_boarding_segment_and_preserves_original_coordinates(monkeypatch):
    mocked(monkeypatch, rail_handler())
    draft = asyncio.run(p.fetch_rail("G1", "2026-10-02", "乙站", "CCC"))
    assert [point["original_longitude"] for point in draft["points"]] == pytest.approx([110.1, 110.15, 110.2])
    assert draft["points"][0]["longitude"] != 110.1
    assert draft["source_crs"] == "GCJ02" and draft["geometry_crs"] == "WGS84"
    assert draft["geometry_kind"] == "reconstructed"
    assert all(point["speed_kmh"] is None and point["timestamp"] is None for point in draft["points"])
    assert draft["origin"] == "乙"


def test_rail_historical_date_uses_current_route_with_flag(monkeypatch):
    calls = mocked(monkeypatch, rail_handler())
    draft = asyncio.run(p.fetch_rail("G1", "2019-01-01", "甲", "丙"))
    assert "historical_date_reconstruction" in draft["quality_flags"]
    assert draft["travel_date"] == "2019-01-01"
    assert draft["metadata"]["route_snapshot_date"] == "2026-10-02"
    assert "历史日期" in draft["metadata"]["notes"][0]
    assert any("20261002" in call.url.params.get("date", "") for call in calls)


def test_rail_overnight_boarding_date_queries_start_date(monkeypatch):
    calls = mocked(monkeypatch, rail_handler(rail_payload(overnight=True)))
    draft = asyncio.run(p.fetch_rail("G1", "2026-10-02", "乙", "丙"))
    assert [call.url.params.get("date") for call in calls[:2]] == ["20261002", "20261001"]
    assert draft["metadata"]["train_start_date"] == "2026-10-01"
    assert draft["metadata"]["timetable"][0]["departure_local"] == "2026-10-02T01:02:00+08:00"


def test_rail_midnight_dwell_advances_departure_day():
    assert p._stop_day({"day": 0, "arrive": "23:58", "depart": "00:03"}, True) == 1


def test_rail_wrong_direction_rejected_before_geometry(monkeypatch):
    calls = mocked(monkeypatch, rail_handler())
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(p.fetch_rail("G1", "2026-10-02", "丙", "甲"))
    assert exc.value.status_code == 422
    assert len(calls) == 1


def test_rail_map_station_list_order_does_not_override_timetable(monkeypatch):
    line = map_payload()
    line["data"]["stations"].reverse()
    mocked(monkeypatch, rail_handler(line=line))
    draft = asyncio.run(p.fetch_rail("G1", "2026-10-02", "甲", "丙"))
    assert draft["points"][0]["original_longitude"] == pytest.approx(110)
    assert draft["points"][-1]["original_longitude"] == pytest.approx(110.2)
    assert [stop["station"] for stop in draft["metadata"]["timetable"]] == ["甲", "乙", "丙"]


def test_rail_wrong_geometry_stop_order_still_rejected(monkeypatch):
    main = rail_payload()
    main["data"]["timetable"][1], main["data"]["timetable"][2] = main["data"]["timetable"][2], main["data"]["timetable"][1]
    mocked(monkeypatch, rail_handler(main=main))
    with pytest.raises(p.ProviderError, match="停站顺序"):
        asyncio.run(p.fetch_rail("G1", "2026-10-02", "甲", "乙"))


def test_rail_missing_map_stop_uses_catalog_coordinate(monkeypatch):
    line = map_payload()
    line["data"]["stations"].pop(1)
    mocked(monkeypatch, rail_handler(line=line))
    monkeypatch.setattr(p, "catalog_station_coordinate", lambda name: p._gcj02_to_wgs84(110.1, 30) if name == "乙" else None)
    draft = asyncio.run(p.fetch_rail("G1", "2026-10-02", "乙", "丙"))
    assert draft["points"][0]["original_longitude"] == pytest.approx(110.1, abs=1e-6)


def test_rail_unrelated_stops_outside_boarding_span_do_not_block(monkeypatch):
    line = map_payload()
    line["data"]["stations"][0] = {"甲": [109, 31]}
    mocked(monkeypatch, rail_handler(line=line))
    draft = asyncio.run(p.fetch_rail("G1", "2026-10-02", "乙", "丙"))
    assert draft["origin"] == "乙"


def test_future_train_schedule_must_match_current_route(monkeypatch):
    def handler(request):
        main = rail_payload()
        if request.url.params.get("date") == "20261003":
            main["data"]["timetable"][1]["depart"] = "23:00"
        return httpx.Response(200, json=main)

    calls = mocked(monkeypatch, handler)
    with pytest.raises(p.ProviderError, match="运行图不同"):
        asyncio.run(p.fetch_rail("G1", "2026-10-03", "甲", "丙"))
    assert len(calls) == 2


def test_rail_reversed_source_sections_are_oriented(monkeypatch):
    line = copy.deepcopy(map_payload())
    for section in line["data"]["train"].values():
        section["line"].reverse()
    mocked(monkeypatch, rail_handler(line=line))
    draft = asyncio.run(p.fetch_rail("G1", "2026-10-02", "甲", "丙"))
    assert draft["points"][0]["original_longitude"] == pytest.approx(110)
    assert draft["points"][-1]["original_longitude"] == pytest.approx(110.2)


def test_duplicate_rail_section_index_cannot_join_unrelated_geometry(monkeypatch):
    line = map_payload()
    line["data"]["train"]["乙-丙"]["index"] = 0
    mocked(monkeypatch, rail_handler(line=line))
    with pytest.raises(p.ProviderError, match="顺序") as exc:
        asyncio.run(p.fetch_rail("G1", "2026-10-02", "甲", "丙"))
    assert exc.value.status_code == 502


def test_rail_source_section_gap_is_preserved_and_flagged(monkeypatch):
    line = map_payload()
    line["data"]["train"]["乙-丙"]["line"][0][0] = 110.11
    mocked(monkeypatch, rail_handler(line=line))
    draft = asyncio.run(p.fetch_rail("G1", "2026-10-02", "甲", "丙"))
    assert "source_gaps" in draft["quality_flags"]
    assert draft["metadata"]["source_section_gaps"][0]["distance_km"] > 0.9


def test_rail_no_sections_uses_single_catalog_line(monkeypatch):
    """Empty train sections: rebuild the ride from the single OSM catalog line."""
    line = map_payload()
    line["data"]["train"] = {}  # no route sections
    mocked(monkeypatch, rail_handler(line=line))
    seen = []

    def fake_route(origin, destination, **kwargs):
        seen.append((origin, destination))
        return [origin, destination], "测试一线"

    monkeypatch.setattr(p, "unique_line_route", fake_route)
    draft = asyncio.run(p.fetch_rail("G1", "2026-10-02", "甲", "丙"))
    assert "catalog_line_reconstruction" in draft["quality_flags"]
    assert "station_only_reconstruction" not in draft["quality_flags"]
    # Endpoints handed to the catalog are WGS84, converted from RailGo GCJ-02.
    assert seen == [(p._gcj02_to_wgs84(110, 30), p._gcj02_to_wgs84(110.1, 30)),
                    (p._gcj02_to_wgs84(110.1, 30), p._gcj02_to_wgs84(110.2, 30))]
    assert draft["source_crs"] == "WGS84" and draft["geometry_crs"] == "WGS84"
    assert [point["longitude"] for point in draft["points"]] == pytest.approx([seen[0][0][0], seen[0][1][0], seen[1][0][0], seen[1][1][0]])
    assert [point["segment_id"] for point in draft["points"]] == ["0", "0", "1", "1"]
    assert all(point["source"] == "OSM" and point["source_crs"] == "WGS84" for point in draft["points"])
    assert all(point["longitude"] == point["original_longitude"] for point in draft["points"])
    assert draft["metadata"]["catalog_line"] == "测试一线"
    assert draft["metadata"]["source_section_count"] == 0
    assert draft["metadata"]["station_snap_distances_km"] == {}
    assert "唯一连接" in draft["metadata"]["notes"][0]


@pytest.mark.parametrize("number", ["G2082/3", "G2082/G2083", "G2082/2083", "G2083"])
def test_g2082_3_incomplete_map_reconstructs_each_timetable_leg(monkeypatch, number):
    names = ["淄博北", "潍坊北", "平度", "荣成", "威海"]
    coords = {name: (118 + i * .1, 37) for i, name in enumerate(names)}
    main = rail_payload()
    main["data"]["numberFull"] = ["G2082", "G2083"]
    main["data"]["timetable"] = [{**main["data"]["timetable"][0], "station": name, "trainCode": "G2083"} for name in names]
    line = {"success": True, "data": {"stations": [{name: coords[name]} for name in names if name != "平度"], "train": {}}}
    calls = mocked(monkeypatch, rail_handler(main=main, line=line))
    monkeypatch.setattr(p, "catalog_station_coordinate", lambda name: p._gcj02_to_wgs84(*coords[name]))
    seen = []
    def route(start, end):
        seen.append((start, end))
        return [start, end], ["济青高速铁路", "潍荣高速线", "潍荣高速线", "青荣城际铁路"][len(seen) - 1]
    monkeypatch.setattr(p, "unique_line_route", route)
    draft = asyncio.run(p.fetch_rail(number, "2026-10-02", "淄博北", "威海"))
    assert len(seen) == 4
    assert [stop["station"] for stop in draft["metadata"]["timetable"]] == names
    assert draft["metadata"]["catalog_line"] == "济青高速铁路 → 潍荣高速线 → 青荣城际铁路"
    assert calls[0].url.params["trainNum"] == number.split("/")[0]
    assert draft["travel_date"] == "2026-10-02"


def test_rail_alias_must_belong_to_returned_train(monkeypatch):
    mocked(monkeypatch, rail_handler())
    with pytest.raises(p.ProviderError, match="车次.*不一致"):
        asyncio.run(p.fetch_rail("G1/2", "2026-10-02", "甲", "丙"))


def test_rail_no_sections_rejected_without_single_catalog_line(monkeypatch):
    """Empty train sections and no unique catalog line: refuse the import."""
    line = map_payload()
    line["data"]["train"] = {}
    mocked(monkeypatch, rail_handler(line=line))
    monkeypatch.setattr(p, "unique_line_route", lambda origin, destination, **kwargs: None)
    with pytest.raises(p.ProviderError, match="无经由信息不支持导入") as exc:
        asyncio.run(p.fetch_rail("G1", "2026-10-02", "甲", "丙"))
    assert exc.value.status_code == 404
