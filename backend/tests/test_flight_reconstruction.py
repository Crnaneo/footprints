import asyncio
import gzip
import json
import math
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from backend import flight_reconstruction as fr
from backend import models as m
from backend import providers as p
from pydantic import ValidationError

DAY = date(2026, 9, 20)
TAKEOFF = datetime(2026, 9, 20, 2, 21, tzinfo=timezone.utc)
LANDING = datetime(2026, 9, 20, 4, 17, tzinfo=timezone.utc)

AIRPORTS = {
    "ZSSS": {"icao": "ZSSS", "iata": "SHA", "tz": "Asia/Shanghai", "lat": 31.197876,
             "lon": 121.336313, "name": "Shanghai Hongqiao International Airport"},
    "ZBAA": {"icao": "ZBAA", "iata": "PEK", "tz": "Asia/Shanghai", "lat": 40.080111,
             "lon": 116.584556, "name": "Beijing Capital International Airport"},
}
AIRPORTS.update({item["iata"]: item for item in list(AIRPORTS.values())})

RUNWAY = {"le_ident": "17", "he_ident": "35", "latitude": 43.9, "longitude": 87.47,
          "heading": 175.0, "length_m": 3600.0}


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("FR24_HISTORY_DAYS", "1096")
    monkeypatch.setattr(p, "_airports", lambda: AIRPORTS)
    monkeypatch.setattr(p, "_today", lambda: date(2026, 10, 5))
    monkeypatch.delenv("FR24_API_TOKEN", raising=False)
    monkeypatch.delenv("OPENSKY_CLIENT_ID", raising=False)
    monkeypatch.delenv("OPENSKY_CLIENT_SECRET", raising=False)
    async def no_schedule(*args, **kwargs):return {}
    monkeypatch.setattr(fr, "lookup_dated_flight", no_schedule)
    fr._opensky_token.update(value=None, expires_at=None)
    fr._runways_cache = {}


ASYNC_CLIENT = httpx.AsyncClient


def mocked(monkeypatch, handler):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: ASYNC_CLIENT(transport=httpx.MockTransport(wrapped), **kwargs))
    return calls


def opensky_row(**values):
    return {"icao24": "781234", "callsign": "CES5105", "firstSeen": int(TAKEOFF.timestamp()),
            "lastSeen": int(LANDING.timestamp()), "estDepartureAirport": "ZSSS",
            "estArrivalAirport": "ZBAA", **values}


def track_path():
    return [
        [int(TAKEOFF.timestamp()), 31.20, 121.34, 120.0, 20.0, False],
        [int(TAKEOFF.timestamp()) + 600, 31.60, 121.60, 6000.0, 30.0, False],
        [int(LANDING.timestamp()), 40.07, 116.59, 150.0, 190.0, False],
    ]


def fr24_summary(**values):
    return {"fr24_id": "abc123", "flight": "MU5105", "orig_icao": "ZSSS", "dest_icao": "ZBAA",
            "datetime_takeoff": TAKEOFF.isoformat().replace("+00:00", "Z"),
            "datetime_landed": LANDING.isoformat().replace("+00:00", "Z"),
            "first_seen": int(TAKEOFF.timestamp()) - 300, "flight_ended": True,
            "callsign": "CES5105", "reg": "B-20AD", "type": "A320neo", **values}


def fr24_tracks():
    return {"fr24_id": "abc123", "tracks": [
        {"timestamp": TAKEOFF.isoformat().replace("+00:00", "Z"), "lon": 121.34, "lat": 31.20, "alt": 400, "gspeed": 160, "source": "ADSB"},
        {"timestamp": LANDING.isoformat().replace("+00:00", "Z"), "lon": 116.59, "lat": 40.07, "alt": 500, "gspeed": 150, "source": "ADSB"},
    ]}


def test_callsign_candidates():
    assert fr.callsign_candidates("MU5105") == ["CES5105", "MU5105"]
    assert fr.callsign_candidates("CA1501") == ["CCA1501", "CA1501"]
    assert fr.callsign_candidates("ZZ123") == ["ZZ123"]


def test_query_by_departure_airport_filters_callsign(monkeypatch):
    calls = mocked(monkeypatch, lambda request: httpx.Response(200, json=[
        opensky_row(), opensky_row(callsign="CES9999", icao24="aaaaaa")]))
    candidates = asyncio.run(fr.query_opensky_flights("MU5105", DAY, "SHA", "PEK"))
    assert [candidate["callsign"] for candidate in candidates] == ["CES5105"]
    assert candidates[0]["icao24"] == "781234"
    assert calls[0].url.params["airport"] == "ZSSS"
    # Local-day window: 2026-09-20 00:00 CST (= 09-19 16:00 UTC) ± 6 hours.
    assert calls[0].url.params["begin"] == str(int(datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc).timestamp()))
    assert calls[0].url.params["end"] == str(int(datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc).timestamp()))


def test_airport_miss_falls_back_to_global_search(monkeypatch):
    # Mainland-China airports often have no precomputed departure index;
    # the global time-window search must still find the flight by callsign.
    def handler(request):
        if request.url.path == "/api/flights/departure":
            return httpx.Response(404)
        if request.url.path == "/api/flights/all":
            return httpx.Response(200, json=[opensky_row()])
        return httpx.Response(404)

    mocked(monkeypatch, handler)
    candidates = asyncio.run(fr.query_opensky_flights("MU5105", DAY, "SHA", "PEK"))
    assert [candidate["callsign"] for candidate in candidates] == ["CES5105"]


def test_query_drops_wrong_local_date_and_airports(monkeypatch):
    late = int(datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc).timestamp())
    mocked(monkeypatch, lambda request: httpx.Response(200, json=[
        opensky_row(firstSeen=late),
        opensky_row(estDepartureAirport="ZGGG"),
        opensky_row(),
    ]))
    candidates = asyncio.run(fr.query_opensky_flights("MU5105", DAY, "SHA", "PEK"))
    assert len(candidates) == 1


def test_multiple_candidates_raise_409(monkeypatch):
    mocked(monkeypatch, lambda request: httpx.Response(200, json=[
        opensky_row(), opensky_row(icao24="79abcd", firstSeen=int(TAKEOFF.timestamp()) + 600)]))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.fetch_flight_reconstruction("MU5105", DAY, "SHA", "PEK"))
    assert exc.value.status_code == 409
    assert len(exc.value.details["candidates"]) == 2


def test_reconstruction_end_to_end(monkeypatch):
    def handler(request):
        if request.url.host == "api.adsbdb.com":
            return httpx.Response(200, json={"aircraft": {"registration": "B-20AD", "type": "A320neo", "manufacturer": "Airbus",
                "icao_type": "A20N", "registered_owner": "China Eastern", "registered_owner_country_name": "China",
                "url_photo": "https://img.example/B-20AD.jpg"}})
        if request.url.path == "/api/flights/departure":
            return httpx.Response(200, json=[opensky_row()])
        if request.url.path == "/api/tracks":
            return httpx.Response(200, json={"icao24": "781234", "path": track_path()})
        return httpx.Response(404)

    calls = mocked(monkeypatch, handler)
    draft = asyncio.run(fr.fetch_flight_reconstruction("MU5105", DAY, "SHA", "PEK"))
    assert draft["mode"] == "flight"
    assert draft["title"] == "MU5105 · ZSSS → ZBAA"
    assert draft["service_number"] == "MU5105"
    assert draft["origin"] == "ZSSS" and draft["destination"] == "ZBAA"
    assert len(draft["points"]) == 3
    assert draft["points"][0]["altitude_m"] == 120.0
    assert draft["points"][0]["speed_kmh"] is None
    info = draft["metadata"]["flight"]
    assert info["icao24"] == "781234"
    assert info["callsign"] == "CES5105"
    assert info["registration"] == "B-20AD"
    assert info["aircraft_type"] == "A320neo"
    assert info["departure_airport"] == "ZSSS"
    assert info["arrival_airport"] == "ZBAA"
    assert info["registered_owner"] == "China Eastern"
    assert info["icao_type"] == "A20N"
    assert info["photo_url"] == "https://img.example/B-20AD.jpg"
    stats = info["track_stats"]
    assert stats is not None
    assert stats["max_altitude_m"] == 6000
    assert stats["cruise_altitude_m"] == 6000
    assert stats["flown_distance_km"] > 0
    assert info["departure_airport_name"] == "Shanghai Hongqiao International Airport"
    assert info["takeoff_time"] == TAKEOFF.isoformat().replace("+00:00", "Z")
    assert info["takeoff_runway"] is None
    assert "sid" not in info and "star" not in info and "airways" not in info
    assert draft["metadata"]["flight"]["track_source"] == "opensky-tracks"
    assert any(request.url.host == "api.adsbdb.com" for request in calls)


def adsblol_trace(callsign="CES5105", **extra):
    """gzip-compressed readsb trace_full payload, as adsb.lol serves it."""
    payload = {
        "icao": "781234", "r": "B-1234", "t": "A332", "dbFlags": 0, "desc": "",
        "version": 2, "timestamp": int(TAKEOFF.timestamp()) - 60,
        "trace": [
            # [relative_s, lat, lon, alt_ft, gs_kt, track_deg, flags, vertical_rate, details, ...]
            [60.0, 31.20, 121.34, 400, 160, 20.0, 0, 0, {"flight": callsign}, "adsb_icao", 8075, 3296, 252, 0.0],
            [660.0, 31.60, 121.60, 6000, 300, 30.0, 0, 1000, None, "adsb_icao", 8075, 3296, 252, 0.0],
            [7050.0, 40.07, 116.59, 500, 150, 190.0, 0, -500, None, "adsb_icao", 8075, 3296, 252, 0.0],
        ],
        **extra,
    }
    return gzip.compress(json.dumps(payload).encode())


def test_missing_track_raises_when_no_source_has_track(monkeypatch):
    # Neither OpenSky nor the adsb.lol archive observed this flight; the
    # import is rejected with a clear message instead of an indicative line.
    def handler(request):
        if request.url.path == "/api/flights/departure":
            return httpx.Response(200, json=[opensky_row()])
        return httpx.Response(404)

    mocked(monkeypatch, handler)
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.fetch_flight_reconstruction("MU5105", DAY, "SHA", "PEK"))
    assert exc.value.status_code == 404
    assert "在线实际轨迹" in exc.value.message


def test_readsb_path_converts_rows_and_windows():
    payload = {
        "icao": "781234", "r": "B-20AD", "t": "A320neo", "timestamp": int(TAKEOFF.timestamp()) - 300,
        "trace": [
            [300.0, 31.20, 121.34, 400, 160, 20.0, 0, "1000", None],
            [900.0, 31.60, 121.60, "ground", 0, 30.0, 0, 1000, {"flight": "CES5105"}],
            [999999.0, 40.07, 116.59, 500, 150, 190.0, -500, "1000", None],
        ],
    }
    rows = fr._readsb_path(payload, int(TAKEOFF.timestamp()), int(LANDING.timestamp()))
    # The far-off fix falls outside the flight window and is dropped.
    assert len(rows) == 2
    assert rows[0][0] == int(TAKEOFF.timestamp())  # relative seconds → absolute epoch
    assert rows[0][3] == pytest.approx(400 * 0.3048)  # feet → metres
    assert rows[0][4] == 20.0
    assert rows[0][5] is False
    assert rows[0][6] == pytest.approx(160 * 1.852)
    assert rows[1][5] is True
    assert rows[1][3] == 0
    assert rows[1][6] == 0


def test_readsb_path_cuts_at_gap_without_landing_time():
    payload = {
        "icao": "781234", "timestamp": int(TAKEOFF.timestamp()) - 300,
        "trace": [
            [300.0, 31.20, 121.34, 400, 160, 20.0, 0, "1000", None],
            [900.0, 31.60, 121.60, 6000, 300, 30.0, 1000, "1000", None],
            # The same aircraft's next flight eight hours later on that day.
            [28800.0, 39.50, 116.60, 20000, 400, 350.0, 0, "1000", None],
        ],
    }
    rows = fr._readsb_path(payload, int(TAKEOFF.timestamp()), None)
    # Without a landing time, the continuous run seeded at takeoff is kept;
    # the later leg beyond the 30-minute reception gap never bleeds in.
    assert len(rows) == 2
    assert rows[-1][0] == int(TAKEOFF.timestamp()) + 600


def test_fr24_failure_falls_back_to_adsblol(monkeypatch):
    # FR24 keeps tracks for ~30 days; beyond that the single-aircraft gzip
    # trace from adsb.lol stands in, still without any bulk download.
    def handler(request):
        if request.url.host == "fr24api.flightradar24.com":
            return httpx.Response(404)
        if request.url.host == "adsb.lol":
            return httpx.Response(200, content=adsblol_trace())
        if request.url.path == "/api/flights/departure":
            return httpx.Response(200, json=[opensky_row()])
        if request.url.host == "api.planespotters.net":
            # The reconstruction path also fills the photo from planespotters
            # when adsbdb has none for the airframe.
            return httpx.Response(200, json={"photos": [{
                "thumbnail": {"src": "https://t.planespotters.net/photo/p/100/x_100.jpg"},
                "thumbnail_large": {"src": "https://t.planespotters.net/photo/p/280/x_280.jpg"},
                "link": "https://www.planespotters.net/photo/x/", "photographer": "Zhang Shuo Hao"}]})
        return httpx.Response(404)

    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    mocked(monkeypatch, handler)
    draft = asyncio.run(fr.fetch_flight_reconstruction("MU5105", DAY, "SHA", "PEK"))
    assert draft["source"] == "adsb.lol"
    assert draft["geometry_kind"] == "observed"
    assert len(draft["points"]) == 3
    assert draft["points"][0]["altitude_m"] == pytest.approx(400 * 0.3048)
    assert draft["points"][0]["speed_kmh"] == pytest.approx(160 * 1.852)
    assert draft["points"][0]["source"] == "adsb.lol"
    info = draft["metadata"]["flight"]
    assert info["track_source"] == "adsblol-globe-history"
    assert info["icao24"] == "781234"
    assert info["registration"] == "B-1234"  # from the trace file itself
    assert info["aircraft_type"] == "A332"
    assert info["photo_url"].endswith("_280.jpg")
    assert info["photo_photographer"] == "Zhang Shuo Hao"
    assert "adsblol_reconstruction" in draft["quality_flags"]
    assert "low_resolution_track" not in draft["quality_flags"]


def test_flight_track_stats_separates_cruise_and_peak():
    points = [
        {"longitude": 121.34, "latitude": 31.20, "altitude_m": 500, "speed_kmh": 400},
        {"longitude": 121.60, "latitude": 31.60, "altitude_m": 10000, "speed_kmh": 850},
        {"longitude": 118.00, "latitude": 34.00, "altitude_m": 9800, "speed_kmh": 870},
        {"longitude": 117.00, "latitude": 37.00, "altitude_m": 10500, "speed_kmh": 880},
        {"longitude": 116.59, "latitude": 40.07, "altitude_m": 300, "speed_kmh": 350},
    ]
    stats = fr._flight_track_stats(points)
    assert stats["max_altitude_m"] == 10500
    assert stats["cruise_altitude_m"] == 10000  # median of points within 15% of the ceiling
    assert stats["max_speed_kmh"] == 880
    assert stats["cruise_speed_kmh"] == 870
    assert stats["flown_distance_km"] > 900


def test_fr24_official_runways_outrank_inference(monkeypatch):
    # flight-summary/full carries FR24's own runway idents; they beat the
    # geometric inference and suppress the runway_inferred flag.
    def handler(request):
        if request.url.host == "fr24api.flightradar24.com":
            if "flight-summary" in request.url.path:
                return httpx.Response(200, json={"data": [fr24_summary(runway_landed="36R", flight_time=4188,
                                                                      actual_distance=628.77, circle_distance=574.8,
                                                                      category="Passenger", orig_iata="SHA", dest_iata="PEK")]})
            if "flight-tracks" in request.url.path:
                return httpx.Response(200, json=fr24_tracks())
        return httpx.Response(404)

    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    mocked(monkeypatch, handler)
    draft = asyncio.run(fr.fetch_flight_reconstruction("MU5105", DAY, "SHA", "PEK"))
    info = draft["metadata"]["flight"]
    assert info["landing_runway"] == "36R"
    assert info["runway_landed_official"] is True
    assert "runway_inferred" not in draft["quality_flags"]
    assert info["flight_time_s"] == 4188
    assert info["aircraft_category"] == "Passenger"
    assert info["departure_iata"] == "SHA" and info["arrival_iata"] == "PEK"
    assert "icao24" in info


def test_import_summary_draft_finishes_to_unified_shape(monkeypatch):
    # /api/preview/flight and /api/import/flight hand providers.fetch_flight's
    # raw summary draft to finish_fr24_summary_draft; the result must carry
    # the same unified metadata.flight shape as the reconstruction chain.
    def handler(request):
        if request.url.host == "fr24api.flightradar24.com":
            if "flight-summary" in request.url.path:
                return httpx.Response(200, json={"data": [fr24_summary(hex="7809ab", runway_landed="36R",
                                                                     flight_time=4188, actual_distance=628.77,
                                                                     circle_distance=574.8, category="Passenger",
                                                                     orig_iata="SHA", dest_iata="PEK")]})
            if "flight-tracks" in request.url.path:
                return httpx.Response(200, json=fr24_tracks())
        if request.url.host == "api.adsbdb.com":
            return httpx.Response(200, json={"response": {"aircraft": {"registered_owner": "China Eastern",
                "url_photo": "https://img.example/B-20AD.jpg"}}})
        return httpx.Response(404)

    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    mocked(monkeypatch, handler)
    raw = asyncio.run(fr.fetch_flight("MU5105", DAY, "SHA", "PEK"))
    draft = asyncio.run(fr.finish_fr24_summary_draft(raw))
    info = draft["metadata"]["flight"]
    assert info["landing_runway"] == "36R"
    assert info["runway_landed_official"] is True
    assert info["flight_time_s"] == 4188
    assert info["aircraft_category"] == "Passenger"
    assert info["departure_iata"] == "SHA" and info["arrival_iata"] == "PEK"
    assert info["registered_owner"] == "China Eastern"
    assert info["photo_url"] == "https://img.example/B-20AD.jpg"
    assert info["track_stats"] is not None
    assert info["track_source"] == "flightradar24"


def test_enrich_prefers_fr24_summary_even_with_opensky_identity(monkeypatch):
    # Identity-only enrichment (require_track=False) must reach the FR24 full
    # summary even when OpenSky resolves the identity first: the OpenSky-only
    # shape has no official runways/times/distances.
    def handler(request):
        if request.url.host == "opensky-network.org":
            if request.url.path == "/api/flights/departure":
                return httpx.Response(200, json=[opensky_row()])
            if request.url.path == "/api/tracks":
                return httpx.Response(200, json={"path": track_path()})
        if request.url.host == "fr24api.flightradar24.com":
            if "flight-summary" in request.url.path:
                return httpx.Response(200, json={"data": [fr24_summary(hex="7809ab", runway_landed="36R",
                                                                     flight_time=4188, actual_distance=628.77,
                                                                     circle_distance=574.8, category="Passenger")]})
            if "flight-tracks" in request.url.path:
                return httpx.Response(200, json=fr24_tracks())
        return httpx.Response(404)

    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    mocked(monkeypatch, handler)
    draft = asyncio.run(fr.fetch_flight_reconstruction("MU5105", DAY, "SHA", "PEK",
                                                       require_track=False))
    info = draft["metadata"]["flight"]
    assert info["landing_runway"] == "36R"
    assert info["flight_time_s"] == 4188
    assert info["actual_distance_km"] == 628.77
    assert info["track_source"] == "flightradar24"


def test_enrich_photos_fall_back_to_planespotters(monkeypatch):
    # adsbdb's photo index misses most Chinese marks; when it has no photo the
    # planespotters public API (queried by hex) fills url + photographer credit.
    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    def handler(request):
        if request.url.host == "fr24api.flightradar24.com":
            if "flight-summary" in request.url.path:
                return httpx.Response(200, json={"data": [fr24_summary(hex="7809ab")]})
            if "flight-tracks" in request.url.path:
                return httpx.Response(200, json=fr24_tracks())
        if request.url.host == "api.planespotters.net":
            return httpx.Response(200, json={"photos": [{"id": 1986449,
                "thumbnail": {"src": "https://t.plnspttrs.net/28476/1986449_fc356883df_100.jpg", "size": [100, 66]},
                "thumbnail_large": {"src": "https://t.plnspttrs.net/28476/1986449_fc356883df_280.jpg", "size": [280, 187]},
                "link": "https://www.planespotters.net/photo/1986449/b-6923-china-eastern-airbus-a320",
                "photographer": "Zhang Shuo Hao", "registration": "B-6923"}]})
        return httpx.Response(404)

    mocked(monkeypatch, handler)
    draft = asyncio.run(fr.finish_fr24_summary_draft(
        asyncio.run(fr.fetch_flight("MU5105", DAY, "SHA", "PEK"))))
    info = draft["metadata"]["flight"]
    assert info["photo_url"].endswith("_280.jpg")
    assert info["photo_photographer"] == "Zhang Shuo Hao"
    assert info["photo_link"].startswith("https://www.planespotters.net/photo/")


def test_enrich_keeps_adsbdb_photo_without_planespotters_call(monkeypatch):
    # When adsbdb already provides a photo the planespotters fallback must
    # not be queried at all.
    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    def handler(request):
        if request.url.host == "fr24api.flightradar24.com":
            if "flight-summary" in request.url.path:
                return httpx.Response(200, json={"data": [fr24_summary(hex="7809ab")]})
            if "flight-tracks" in request.url.path:
                return httpx.Response(200, json=fr24_tracks())
        if request.url.host == "api.adsbdb.com":
            return httpx.Response(200, json={"response": {"aircraft": {"registered_owner": "Air China",
                "url_photo": "https://image.airport-data.com/aircraft/640721.jpg"}}})
        if request.url.host == "api.planespotters.net":
            raise AssertionError("planespotters must not be called when adsbdb has a photo")
        return httpx.Response(404)

    calls = mocked(monkeypatch, handler)
    draft = asyncio.run(fr.finish_fr24_summary_draft(
        asyncio.run(fr.fetch_flight("MU5105", DAY, "SHA", "PEK"))))
    info = draft["metadata"]["flight"]
    assert info["photo_url"] == "https://image.airport-data.com/aircraft/640721.jpg"
    assert all(request.url.host != "api.planespotters.net" for request in calls)


def test_missing_tracks_never_queries_standing_route(monkeypatch):
    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    def handler(request):
        if "/callsign/" in request.url.path:
            raise AssertionError("standing routes must not be queried for historical flights")
        return httpx.Response(404)

    mocked(monkeypatch, handler)
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.fetch_flight_reconstruction("MU583", DAY, "ZSPD", "KLAX"))
    assert exc.value.status_code == 404


def test_missing_tracks_returns_no_data(monkeypatch):
    mocked(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.fetch_flight_reconstruction("MU999", DAY, "ZSPD", "KLAX"))
    assert exc.value.status_code == 404


def test_unified_entry_skips_second_fr24_call_on_fallback(monkeypatch):
    fr24_hits = 0
    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    def handler(request):
        nonlocal fr24_hits
        if request.url.host == "fr24api.flightradar24.com":
            fr24_hits += 1
        if "/callsign/" in request.url.path:
            raise AssertionError("no standing route fallback")
        return httpx.Response(404)

    mocked(monkeypatch, handler)
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.fetch_flight_unified("MU583", DAY, "ZSPD", "KLAX"))
    assert exc.value.status_code == 404
    assert fr24_hits == 1


def test_unified_entry_with_registration_enriches_aircraft(monkeypatch):
    # When a user supplies a registration, the unified entry fetches aircraft
    # details from adsbdb and merges them (type, owner, hex, photo) into the
    # draft without overwriting existing fields.
    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    def handler(request):
        if request.url.host == "fr24api.flightradar24.com":
            return httpx.Response(404)
        if request.url.host == "adsb.lol":
            return httpx.Response(200, content=adsblol_trace(callsign="GK234", icao="86d4f1", r="JA808Z", t="Airbus A320-214"))
        if request.url.host == "api.adsbdb.com" and "/aircraft/JA808Z" in request.url.path:
            return httpx.Response(200, json={"response": {"aircraft": {
                "registration": "JA808Z", "type": "Airbus A320-214", "manufacturer": "Airbus",
                "registered_owner": "Jetstar Japan", "icao_type": "A320",
                "registered_owner_country_name": "Japan", "mode_s": "86D4F1",
                "url_photo": "https://example.com/photo.jpg"}}})
        return httpx.Response(404)

    mocked(monkeypatch, handler)
    draft = asyncio.run(fr.fetch_flight_unified("GK234", DAY, "KIX", "NRT", registration="JA808Z",
                                              first_seen=int(TAKEOFF.timestamp())))
    info = draft["metadata"]["flight"]
    assert info["registration"] == "JA808Z"
    assert info["aircraft_type"] == "Airbus A320-214"
    assert info["registered_owner"] == "Jetstar Japan"
    assert info["owner_country"] == "Japan"
    assert info["icao24"].upper() == "86D4F1"
    assert info["photo_url"] == "https://example.com/photo.jpg"


def test_flight_trip_without_points_is_valid_but_rail_is_not():
    trip = m.TripCreate(title="MU583 · ZSPD → KLAX", mode="flight", travel_date=DAY,
                        service_number="MU583", origin="ZSPD", destination="KLAX",
                        geometry_kind="scheduled", points=[])
    canonical = m.canonical_trip(trip)
    assert canonical["bounds"] is None
    assert canonical["stats"]["point_count"] == 0
    with pytest.raises(ValidationError):
        m.TripCreate(title="empty rail", mode="rail", travel_date=DAY, points=[])


def test_fr24_bad_request_stays_fatal(monkeypatch):
    # A parameter mistake (bad airport code) surfaces at the OpenSky stage,
    # before any fallback chain gets a chance to run.
    mocked(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.fetch_flight_reconstruction("MU5105", DAY, "bad-airport", None))
    assert exc.value.status_code == 422
    assert "机场" in exc.value.message


def test_opensky_rate_limit_falls_back_to_fr24(monkeypatch):
    # OpenSky credit exhaustion must not block a configured FR24 chain.
    def handler(request):
        if request.url.host == "fr24api.flightradar24.com":
            if "flight-summary" in request.url.path:
                return httpx.Response(200, json={"data": [fr24_summary()]})
            if "flight-tracks" in request.url.path:
                return httpx.Response(200, json=fr24_tracks())
        return httpx.Response(429)

    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    mocked(monkeypatch, handler)
    draft = asyncio.run(fr.fetch_flight_reconstruction("MU5105", DAY, "SHA", "PEK"))
    assert draft["source"] == "Flightradar24"
    assert draft["metadata"]["flight"]["track_source"] == "flightradar24"
    assert draft["metadata"]["flight"]["registration"] == "B-20AD"


def test_missing_track_falls_back_to_fr24(monkeypatch):
    def handler(request):
        if request.url.host == "fr24api.flightradar24.com":
            if "flight-summary" in request.url.path:
                return httpx.Response(200, json={"data": [fr24_summary()]})
            if "flight-tracks" in request.url.path:
                return httpx.Response(200, json=fr24_tracks())
        if request.url.path == "/api/flights/departure":
            return httpx.Response(200, json=[opensky_row()])
        return httpx.Response(404)

    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    mocked(monkeypatch, handler)
    draft = asyncio.run(fr.fetch_flight_reconstruction("MU5105", DAY, "SHA", "PEK"))
    assert draft["source"] == "Flightradar24"
    assert draft["metadata"]["flight"]["track_source"] == "flightradar24"
    assert draft["metadata"]["flight"]["registration"] == "B-20AD"
    assert draft["metadata"]["flight"]["icao24"] == "781234"
    assert draft["metadata"]["flight"]["departure_airport"] == "ZSSS"


def test_waypoints_from_points_derives_heading():
    points = [
        {"timestamp": "2026-09-28T00:00:00Z", "latitude": 39.0, "longitude": 116.0, "altitude_m": 0.0},
        {"timestamp": "2026-09-28T00:01:00Z", "latitude": 39.0, "longitude": 116.01, "altitude_m": 50.0},
        {"timestamp": "2026-09-28T00:02:00Z", "latitude": 39.01, "longitude": 116.01, "altitude_m": 120.0},
    ]
    rows = fr._waypoints_from_points(points)
    # Eastward leg, then a northward leg; the last fix repeats its inbound course.
    assert [row[4] for row in rows] == [90.0, 0.0, 0.0]


def test_waypoints_from_points_skips_stationary_fixes():
    points = [
        {"timestamp": "2026-09-28T00:00:00Z", "latitude": 39.0, "longitude": 116.0, "altitude_m": 0.0},
        {"timestamp": "2026-09-28T00:01:00Z", "latitude": 39.0, "longitude": 116.0, "altitude_m": 0.0},
        {"timestamp": "2026-09-28T00:02:00Z", "latitude": 39.0, "longitude": 116.01, "altitude_m": 0.0},
    ]
    rows = fr._waypoints_from_points(points)
    # Coincident fixes have no direction of their own; only real movement does.
    assert [row[4] for row in rows] == [None, 90.0, 90.0]


def test_fr24_fallback_infers_runway(monkeypatch):
    rows = takeoff_path(175.0)
    tracks = {"fr24_id": "abc123", "tracks": [
        {"timestamp": datetime.fromtimestamp(row[0], timezone.utc).isoformat().replace("+00:00", "Z"),
         "lat": row[1], "lon": row[2], "alt": round(row[3] / 0.3048), "gspeed": 180, "source": "ADSB"}
        for row in rows
    ]}

    def handler(request):
        if request.url.host == "fr24api.flightradar24.com":
            if "flight-summary" in request.url.path:
                return httpx.Response(200, json={"data": [fr24_summary()]})
            if "flight-tracks" in request.url.path:
                return httpx.Response(200, json=tracks)
        if request.url.path == "/api/flights/departure":
            return httpx.Response(200, json=[opensky_row()])
        return httpx.Response(404)

    monkeypatch.setenv("FR24_API_TOKEN", "test-local-secret")
    monkeypatch.setattr(fr, "load_runways", lambda: {"ZSSS": [RUNWAY]})
    mocked(monkeypatch, handler)
    draft = asyncio.run(fr.fetch_flight_reconstruction("MU5105", DAY, "SHA", "PEK"))
    info = draft["metadata"]["flight"]
    assert info["icao24"] == "781234"
    assert info["takeoff_runway"] == "17"
    assert "runway_inferred" in draft["quality_flags"]


def test_enrich_requires_identity(monkeypatch):
    mocked(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.fetch_flight_reconstruction("MU5105", DAY, "SHA"))
    assert exc.value.status_code == 404


def _advance(lat, lon, heading, distance):
    return (lat + distance * math.cos(math.radians(heading)) / 111320.0,
            lon + distance * math.sin(math.radians(heading)) / (111320.0 * math.cos(math.radians(lat))))


def takeoff_path(heading=175.0, speed_mps=70.0, step=5, climb_mps=9.0):
    rows, lat, lon = [], RUNWAY["latitude"], RUNWAY["longitude"]
    for index in range(-3, 20):
        flown = max(0, index) * step * speed_mps
        lat, lon = _advance(RUNWAY["latitude"], RUNWAY["longitude"], heading, flown)
        rows.append([int(TAKEOFF.timestamp()) + index * step,
                     lat, lon, max(0.0, (index - 1) * step * climb_mps) if index > 0 else 0.0,
                     heading % 360, index < 1])
    return rows


def landing_path(heading=355.0, speed_mps=72.0, step=5):
    rows = []
    # Final approach from ~10.8 km out along the landing heading, then touchdown
    # and a short ground roll; altitudes follow a 3-degree glide path.
    for index in range(-30, 5):
        remaining = max(0, -index) * step * speed_mps
        lat, lon = _advance(RUNWAY["latitude"], RUNWAY["longitude"], heading, remaining)
        altitude = remaining * math.tan(math.radians(3.0))
        rows.append([int(LANDING.timestamp()) + index * step,
                     lat, lon, altitude, heading % 360, index >= 0])
    return rows


def test_parse_runways_ourairports_schema():
    header = ('id,airport_ref,airport_ident,length_ft,width_ft,surface,lighted,closed,'
              'le_ident,le_latitude_deg,le_longitude_deg,le_elevation_ft,le_heading_degT,le_displaced_threshold_ft,'
              'he_ident,he_latitude_deg,he_longitude_deg,he_elevation_ft,he_heading_degT,he_displaced_threshold_ft\n')
    row = '269343,27188,ZBAA,12467,197,CON,1,0,01,40.058914,116.617599,94,353,,"19",40.092834,116.612297,84,173,\n'
    runways = fr._parse_runways(header + row).get("ZBAA")
    assert len(runways) == 1
    runway = runways[0]
    assert (runway["le_ident"], runway["he_ident"]) == ("01", "19")
    assert runway["heading"] == 353
    assert runway["latitude"] == pytest.approx((40.058914 + 40.092834) / 2)
    assert runway["length_m"] == pytest.approx(12467 * 0.3048)


def test_parse_navaids_buckets_by_grid():
    header = "id,ident,name,latitude_deg,longitude_deg,elevation_ft,type\n"
    row = "1,SADAN,SADAN VOR-DME,36.5,117.2,300,VOR-DME\n"
    index = fr._parse_navaids(header + row)
    stored = next(iter(index.values()))[0]
    assert stored["ident"] == "SADAN" and stored["type"] == "VOR-DME"


def test_infer_route_waypoints_cruise_sequence(monkeypatch):
    navaids = fr._parse_navaids(
        "id,ident,name,latitude_deg,longitude_deg,elevation_ft,type\n"
        "1,AAA,AAA VOR,36.500,117.000,0,VOR\n"
        "2,BBB,BBB WPT,36.600,117.200,0,WP\n"
        "3,CCC,CCC NDB,36.700,117.400,0,NDB\n")
    monkeypatch.setattr(fr, "load_navaids", lambda: navaids)

    def fix(lon: float, lat: float, alt: float) -> dict:
        return {"longitude": lon, "latitude": lat, "altitude_m": alt}

    points = [
        fix(117.000, 36.500, 800),     # climbing: below cruise, ignored
        fix(117.0001, 36.5001, 9000),  # right over AAA
        fix(117.0002, 36.5002, 9000),  # still near AAA: collapses to one
        fix(117.200, 36.600, 9000),    # right over BBB
        fix(117.400, 36.700, 900),     # on approach: ignored
    ]
    sequence = fr.infer_route_waypoints(points)
    assert [w["ident"] for w in sequence] == ["AAA", "BBB"]


def test_infer_route_waypoints_none_without_database(monkeypatch):
    monkeypatch.setattr(fr, "load_navaids", lambda: {})
    points = [{"longitude": 117, "latitude": 36, "altitude_m": 9000}]
    assert fr.infer_route_waypoints(points) is None


def test_parse_runways_falls_back_to_opposite_heading():
    header = ('id,airport_ref,airport_ident,length_ft,width_ft,surface,lighted,closed,'
              'le_ident,le_latitude_deg,le_longitude_deg,le_elevation_ft,le_heading_degT,le_displaced_threshold_ft,'
              'he_ident,he_latitude_deg,he_longitude_deg,he_elevation_ft,he_heading_degT,he_displaced_threshold_ft\n')
    row = '1,2,ZTST,9000,100,ASP,1,0,09,30.0,120.0,50,,,"27",30.01,120.01,40,91,\n'
    runways = fr._parse_runways(header + row).get("ZTST")
    assert runways[0]["heading"] == 271  # (91 + 180) % 360 from the far end


def test_infer_runway_takeoff_direction(monkeypatch):
    monkeypatch.setattr(fr, "load_runways", lambda: {"ZWWW": [RUNWAY]})
    result = fr.infer_runway(takeoff_path(175.0), "ZWWW", "takeoff")
    assert result is not None
    assert result["runway"] == "17"
    assert result["mean_offset_m"] < 50


def test_infer_runway_landing_direction(monkeypatch):
    monkeypatch.setattr(fr, "load_runways", lambda: {"ZWWW": [RUNWAY]})
    result = fr.infer_runway(landing_path(355.0), "ZWWW", "landing")
    assert result is not None
    assert result["runway"] == "35"


def test_infer_runway_rejects_wrong_alignment(monkeypatch):
    monkeypatch.setattr(fr, "load_runways", lambda: {"ZWWW": [RUNWAY]})
    # A path flying perpendicular to the runway never matches.
    result = fr.infer_runway(takeoff_path(265.0), "ZWWW", "takeoff")
    assert result is None


def test_infer_runway_landing_from_truncated_approach(monkeypatch):
    # FR24 tracks often stop short of the ground; fixes at ~850 m on the
    # extended centreline must still match the runway.
    rows = []
    for index in range(-6, 0):
        remaining = -index * 5 * 72.0
        lat, lon = _advance(RUNWAY["latitude"], RUNWAY["longitude"], 355.0, remaining)
        rows.append([int(LANDING.timestamp()) + index * 5, lat, lon, 850.0 + index * 4, 355.0, False])
    monkeypatch.setattr(fr, "load_runways", lambda: {"ZWWW": [RUNWAY]})
    result = fr.infer_runway(rows, "ZWWW", "landing")
    assert result is not None
    assert result["runway"] == "35"


def test_enrich_preserves_points_and_fills_identity(monkeypatch, tmp_path):
    from backend.models import TripCreate
    from backend.store import TripStore
    store = TripStore(tmp_path / "trips.sqlite3")
    trip = store.create(TripCreate(title="GPS 记录", mode="flight", travel_date=DAY, points=[
        {"longitude": 121.0, "latitude": 31.0, "timestamp": "2026-09-20T02:00:00Z"},
        {"longitude": 121.5, "latitude": 31.5, "timestamp": "2026-09-20T02:10:00Z"},
    ]))

    async def fake_reconstruct(flight_number, travel_date, origin=None, destination=None,
                               icao24=None, first_seen=None, require_track=True, **kwargs):
        return {"metadata": {"flight": {"flight_number": "MU5105", "callsign": "CES5105",
                                        "icao24": "781234", "departure_airport": "ZSSS",
                                        "arrival_airport": "ZBAA", "takeoff_runway": "36R"}}}

    monkeypatch.setattr(fr, "fetch_flight_reconstruction", fake_reconstruct)
    record = asyncio.run(fr.enrich_trip(store, trip["id"], "MU5105", DAY))
    assert len(record["points"]) == 2
    assert record["service_number"] == "MU5105"
    assert record["metadata"]["flight"]["callsign"] == "CES5105"
    assert record["metadata"]["flight"]["takeoff_runway"] == "36R"
    assert record["origin"] == "ZSSS"
    stored = store.get(trip["id"])
    assert stored["metadata"]["flight"]["icao24"] == "781234"
    assert stored["point_count"] == 2


def test_update_record_keeps_previews_in_sync(tmp_path):
    from backend.models import TripCreate
    from backend.store import TripStore
    store = TripStore(tmp_path / "trips.sqlite3")
    trip = store.create(TripCreate(title="旧标题", mode="other", travel_date=DAY, points=[
        {"longitude": 10.0, "latitude": 10.0}, {"longitude": 11.0, "latitude": 11.0}]))

    def transform(record):
        record["title"] = "新标题"
        return record

    updated = store.update_record(trip["id"], transform)
    assert updated["title"] == "新标题"
    overview = {item["id"]: item for item in store.overview()}
    assert overview[trip["id"]]["title"] == "新标题"
    assert store.update_record("missing", transform) is None


def test_registration_hex_is_resolved_before_archive_query(monkeypatch):
    def handler(request):
        if request.url.path == '/v0/aircraft/TEST123':
            return httpx.Response(200, json={'response': {'aircraft': {'registration': 'TEST123', 'mode_s': '781234'}}})
        if request.url.host == 'adsb.lol':
            return httpx.Response(200, content=adsblol_trace())
        if '/flights/' in request.url.path:
            raise AssertionError('explicit airframe and time must bypass the flights index')
        return httpx.Response(404)
    calls = mocked(monkeypatch, handler)
    result = asyncio.run(fr.fetch_flight_unified('MU5105', DAY, 'SHA', 'PEK',
                        registration='TEST123', first_seen=int(TAKEOFF.timestamp())))
    assert result['source'] == 'adsb.lol'
    assert calls[0].url.path == '/v0/aircraft/TEST123'
    assert any('/trace_full_781234.json' in r.url.path for r in calls)
    assert result['metadata']['flight']['track_stats']['max_speed_kmh'] == round(300 * 1.852)


def test_registration_hex_scopes_the_opensky_identity_query(monkeypatch):
    def handler(request):
        if request.url.path == '/v0/aircraft/TEST123':
            return httpx.Response(200, json={'aircraft': {'mode_s': '781234'}})
        if request.url.path == '/api/flights/aircraft':
            assert request.url.params['icao24'] == '781234'
            return httpx.Response(200, json=[opensky_row()])
        if request.url.host == 'adsb.lol':
            return httpx.Response(200, content=adsblol_trace())
        return httpx.Response(404)
    mocked(monkeypatch, handler)
    result = asyncio.run(fr.fetch_flight_unified('MU5105', DAY, 'SHA', 'PEK', registration='TEST123'))
    assert result['source'] == 'adsb.lol'


@pytest.mark.parametrize(('entered', 'canonical'), [
    ('b5678', 'B-5678'), (' B 32A6 ', 'B-32A6'), ('B－5555', 'B-5555'),
    ('B-1234', 'B-1234'), ('JA808Z', 'JA808Z'), ('HL7626', 'HL7626'), ('N123AB', 'N123AB'),
    ('vna123', 'VN-A123'), (' VN B 456 ', 'VN-B456'), ('VN-A789', 'VN-A789'),
])
def test_registration_format_is_normalized(entered, canonical):
    assert fr.normalize_registration(entered) == canonical


def test_aircraft_info_survives_failed_track_lookup(monkeypatch):
    def handler(request):
        if request.url.path == '/v0/aircraft/B-5678':
            return httpx.Response(200, json={'response': {'aircraft': {
                'registration': 'B-5678', 'mode_s': 'abcdef', 'type': '737 800',
                'registered_owner': 'Example Air', 'url_photo': 'https://example.com/aircraft.jpg'}}})
        if request.url.host == 'adsb.lol' or request.url.path == '/api/tracks':
            return httpx.Response(404)
        raise AssertionError(f'unexpected request {request.url.path}')
    mocked(monkeypatch, handler)
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.fetch_flight_unified('AB123', DAY, 'SHA', 'PEK',
                    registration='b5678', first_seen=int(TAKEOFF.timestamp())))
    assert exc.value.status_code == 404
    info = exc.value.details['aircraft_info']
    assert info['registration'] == 'B-5678'
    assert info['icao24'] == 'abcdef'
    assert info['aircraft_type'] == '737 800'
    assert info['photo_url'] == 'https://example.com/aircraft.jpg'


def test_registration_photo_does_not_require_hex_or_track(monkeypatch):
    def handler(request):
        if request.url.host == 'api.adsbdb.com':
            return httpx.Response(404)
        if request.url.path == '/pub/photos/reg/B-5678':
            return httpx.Response(200, json={'photos': [{'thumbnail_large': {'src': 'https://example.com/plane.jpg'},
                'photographer': 'Example Photographer', 'link': 'https://www.planespotters.net/photo/example'}]})
        raise AssertionError('aircraft photo must not require any flight endpoint')
    mocked(monkeypatch, handler)
    info = asyncio.run(fr.lookup_aircraft_info('B5678'))
    assert info['photo_url'] == 'https://example.com/plane.jpg'
    assert info['photo_photographer'] == 'Example Photographer'
    assert 'icao24' not in info


def test_conflicting_registration_and_hex_is_rejected(monkeypatch):
    mocked(monkeypatch, lambda request: httpx.Response(200, json={
        'response': {'aircraft': {'registration': 'B-5678', 'mode_s': 'abcdef'}}}))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.lookup_aircraft_info('B5678', '123456'))
    assert exc.value.status_code == 422


def test_readsb_flags_distinguish_ground_geometric_altitude_and_new_leg():
    payload = {'timestamp': 0, 'trace': [
        [1000, 30, 110, 3000, 250, 90, 4, 0, None],
        [1100, 30, 111, 4000, None, 90, 8, 0, None],
        [1200, 30, 112, 'ground', 0, 90, 0, 0, None],
        [1300, 30, 113, 1000, 150, 90, 2, 0, None],
        [1400, 30, 114, 2000, 200, 90, 0, 0, None],
    ]}
    rows = fr._readsb_path(payload, 1000, None)
    assert len(rows) == 3
    assert rows[0][3] == pytest.approx(3000 * .3048)  # flag 4 is vertical-rate kind, not ground
    assert rows[0][5] is False
    assert rows[1][3] is None and rows[1][6] is None  # no mislabelled geometric altitude or invented speed
    assert rows[2][3] == 0 and rows[2][5] is True


def test_archive_merges_days_across_utc_midnight(monkeypatch):
    start = datetime(2026, 9, 20, 23, 50, tzinfo=timezone.utc)
    def handler(request):
        if '/2026/09/20/' in request.url.path:
            rows = [[0, 30, 110, 30000, 400, 90, 0, 0, None], [300, 30, 111, 30000, 410, 90, 0, 0, None]]
            base = int(start.timestamp())
        else:
            rows = [[0, 30, 112, 30000, 420, 90, 0, 0, None], [300, 30, 113, 30000, 430, 90, 0, 0, None]]
            base = int((start + timedelta(minutes=10)).timestamp())
        return httpx.Response(200, json={'timestamp': base, 'trace': rows})
    calls = mocked(monkeypatch, handler)
    result = asyncio.run(fr._fetch_adsblol_path({'icao24': 'aabbcc', 'first_seen': int(start.timestamp()),
                                              'last_seen': int((start + timedelta(minutes=15)).timestamp())}))
    assert len(calls) == 2 and len(result[0]) == 4
    assert result[0][-1][6] == pytest.approx(430 * 1.852)


def test_registry_must_not_merge_another_aircraft_into_observed_identity():
    info = {'icao24': 'aabbcc'}
    fr.merge_registration(info, {'mode_s': 'ddeeff', 'registration': 'WRONG', 'url_photo': 'wrong-photo'})
    assert info == {'icao24': 'aabbcc'}


def test_archive_does_not_use_an_unrelated_later_flight_for_missing_seed():
    payload = {'timestamp': 0, 'trace': [
        [10000, 30, 110, 30000, 400, 90, 0, 0, None],
        [10100, 30, 111, 30000, 400, 90, 0, 0, None],
    ]}
    assert fr._readsb_path(payload, 1000, None) == []


def test_explicit_hex_still_tries_fr24_before_adsb(monkeypatch):
    monkeypatch.setenv('FR24_API_TOKEN', 'test-local-secret')
    def handler(request):
        if request.url.path.endswith('/flight-summary/full'):
            return httpx.Response(200, json={'data': [fr24_summary(hex='781234')]})
        if request.url.path.endswith('/flight-tracks'):
            return httpx.Response(200, json=fr24_tracks())
        if request.url.host in ('opensky-network.org', 'adsb.lol'):
            raise AssertionError('ADS-B must not run after a usable FR24 track')
        return httpx.Response(404)
    calls = mocked(monkeypatch, handler)
    draft = asyncio.run(fr.fetch_flight_unified('MU5105', DAY, 'SHA', 'PEK', icao24='781234'))
    assert draft['source'] == 'Flightradar24'
    assert [r.url.path for r in calls[:2]] == ['/api/flight-summary/full', '/api/flight-tracks']


@pytest.mark.parametrize('track_result', ['forbidden', 'missing', 'invalid', 'empty', 'single'])
def test_fr24_summary_survives_track_failure_and_seeds_adsb(monkeypatch, track_result):
    monkeypatch.setenv('FR24_API_TOKEN', 'test-local-secret')
    def handler(request):
        if request.url.path.endswith('/flight-summary/full'):
            return httpx.Response(200, json={'data': [fr24_summary(hex='781234', runway_takeoff='18L')]})
        if request.url.path.endswith('/flight-tracks'):
            if track_result in ('forbidden', 'missing'):
                return httpx.Response(403 if track_result == 'forbidden' else 404)
            if track_result == 'invalid':
                return httpx.Response(200, json={'data': 'bad'})
            result = fr24_tracks()
            result['tracks'] = result['tracks'][:1] if track_result == 'single' else []
            return httpx.Response(200, json=result)
        if '/flights/' in request.url.path:
            raise AssertionError('FR24 identity must seed ADS-B without a second flights index')
        if request.url.host == 'adsb.lol':
            return httpx.Response(200, content=adsblol_trace(r='B-20AD'))
        return httpx.Response(404)
    calls = mocked(monkeypatch, handler)
    draft = asyncio.run(fr.fetch_flight_unified('MU5105', DAY, 'SHA', 'PEK'))
    info = draft['metadata']['flight']
    assert draft['source'] == 'adsb.lol'
    assert info['takeoff_runway'] == '18L' and info['runway_takeoff_official'] is True
    assert info['landing_time'] == LANDING.isoformat().replace('+00:00', 'Z')
    assert len([r for r in calls if r.url.path.endswith('/flight-summary/full')]) == 1
    assert calls[0].url.path.endswith('/flight-summary/full')
    assert calls[1].url.path.endswith('/flight-tracks')


def test_no_tracks_preserves_fr24_flight_identity_and_aircraft_photo(monkeypatch):
    monkeypatch.setenv('FR24_API_TOKEN', 'test-local-secret')
    def handler(request):
        if request.url.path.endswith('/flight-summary/full'):
            return httpx.Response(200, json={'data': [fr24_summary(hex='781234', operating_as='CES')]})
        if request.url.host == 'api.adsbdb.com':
            return httpx.Response(200, json={'aircraft': {'registration': 'B-20AD', 'mode_s': '781234',
                'type': 'A320neo', 'url_photo': 'https://example.com/airframe.jpg'}})
        return httpx.Response(404)
    mocked(monkeypatch, handler)
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.fetch_flight_unified('MU5105', DAY, 'SHA', 'PEK'))
    assert exc.value.status_code == 404
    info = exc.value.details['flight_info']
    assert info['callsign'] == 'CES5105' and info['registration'] == 'B-20AD'
    assert info['takeoff_time'] == TAKEOFF.isoformat().replace('+00:00', 'Z')
    assert info['departure_airport'] == 'ZSSS' and info['arrival_airport'] == 'ZBAA'
    assert info['photo_url'] == 'https://example.com/airframe.jpg'
    assert info['operating_as'] == 'CES'


def test_opensky_identity_retains_aircraft_info_without_track_or_registration_hint(monkeypatch):
    def handler(request):
        if request.url.path == '/api/flights/departure':
            return httpx.Response(200, json=[opensky_row()])
        if request.url.host == 'api.adsbdb.com':
            return httpx.Response(200, json={'aircraft': {'registration': 'B-20AD', 'mode_s': '781234',
                'url_photo': 'https://example.com/airframe.jpg'}})
        return httpx.Response(404)
    mocked(monkeypatch, handler)
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.fetch_flight_unified('MU5105', DAY, 'SHA', 'PEK'))
    info = exc.value.details['flight_info']
    assert info['icao24'] == '781234' and info['registration'] == 'B-20AD'
    assert info['callsign'] == 'CES5105' and info['photo_url']


def test_missing_tracks_does_not_label_user_time_as_observed_takeoff(monkeypatch):
    mocked(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(p.ProviderError) as exc:
        asyncio.run(fr.fetch_flight_unified('AB123', DAY, 'SHA', 'PEK',
                    icao24='abcdef', first_seen=int(TAKEOFF.timestamp())))
    info = exc.value.details['flight_info']
    assert not info.get('takeoff_time') and not info.get('landing_time')


def test_partial_adsb_requires_matching_callsign_and_keeps_reception_holes():
    payload={'timestamp':0,'trace':[
        [10000,30,110,30000,400,90,0,0,{'flight':'TST123'}],
        [10100,30,111,30000,400,90,0,0,None],
        [20000,30,112,30000,400,90,0,0,{'flight':'TST123'}],
        [20100,30,113,30000,400,90,0,0,None],
        [21000,30,114,30000,400,90,2,0,{'flight':'OTHER456'}],
        [21100,30,115,30000,400,90,0,0,None]]}
    assert fr._readsb_path(payload,1000,None)==[]
    rows=fr._readsb_path(payload,1000,None,['TST123'])
    assert [r[0] for r in rows]==[10000,10100,20000,20100]
    points=fr._track_points(rows)
    assert points[1]['segment_id']!=points[2]['segment_id']
    assert fr._readsb_path(payload,1000,None,['WRONG'])==[]


def test_official_archive_cache_checks_hash_and_aircraft(monkeypatch,tmp_path):
    import hashlib
    monkeypatch.setenv('FOOTPRINT_DATA_DIR',str(tmp_path))
    directory=tmp_path/'flight_archive_queries'/DAY.isoformat();directory.mkdir(parents=True)
    path=directory/'trace_full_781234.json'
    raw=json.dumps({'icao':'781234','timestamp':int(TAKEOFF.timestamp()),'trace':[]}).encode()
    path.write_bytes(raw)
    provenance={'source':f'https://github.com/adsblol/globe_history_2026/releases/download/v{DAY:%Y.%m.%d}-planes-readsb-prod-0/archive.tar.aa',
                'sha256':hashlib.sha256(raw).hexdigest()}
    path.with_suffix('.source.json').write_text(json.dumps(provenance))
    assert fr._cached_adsblol_day(DAY,'781234')['icao']=='781234'
    path.write_bytes(raw+b' ')
    assert fr._cached_adsblol_day(DAY,'781234') is None
    assert fr._cached_adsblol_day(DAY,'../../bad') is None
