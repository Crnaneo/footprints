"""Validated, source-aware trip records in a common WGS84 geometry."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_POINTS = 200_000
CRS = Literal["WGS84", "GCJ02"]


class TrackPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    sequence: int | None = Field(default=None, ge=0)
    segment_id: str = Field(default="0", max_length=200)
    timestamp: str | None = None
    altitude_m: float | None = Field(default=None, allow_inf_nan=False)
    speed_kmh: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    original_longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    original_latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    source: str | None = Field(default=None, max_length=100)
    source_crs: CRS | None = None

    @field_validator("longitude", "latitude", "altitude_m", "speed_kmh", mode="before")
    @classmethod
    def reject_boolean(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("coordinates and measurements must be numbers, not booleans")
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be an ISO 8601 datetime") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class TripCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=300)
    mode: Literal["flight", "rail", "other"]
    travel_date: date
    service_number: str | None = Field(default=None, max_length=100)
    origin: str | None = Field(default=None, max_length=300)
    destination: str | None = Field(default=None, max_length=300)
    source: str = Field(default="manual", max_length=300)
    geometry_kind: Literal["observed", "reconstructed", "imported", "scheduled"] = "imported"
    source_crs: CRS = "WGS84"
    geometry_crs: CRS = "WGS84"
    notes: str | None = Field(default=None, max_length=20_000)
    design_speed_kmh: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    line_speed_limit_kmh: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    quality_flags: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    points: list[TrackPoint] = Field(max_length=MAX_POINTS)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @field_validator("metadata")
    @classmethod
    def finite_json_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata must contain finite JSON values") from exc
        if len(encoded) > 100_000:
            raise ValueError("metadata must not exceed 100,000 characters")
        return value

    @model_validator(mode="after")
    def order_points(self) -> "TripCreate":
        # A segment denotes a continuous line. Preserve first-seen segment order
        # and use explicit sequence values to reconstruct shuffled CSV rows.
        # Flights may retain route metadata when no observed track is available.
        if not self.points and self.mode != "flight":
            raise ValueError("points must not be empty")
        groups: dict[str, list[TrackPoint]] = {}
        for point in self.points:
            groups.setdefault(point.segment_id, []).append(point)
        ordered: list[TrackPoint] = []
        for group in groups.values():
            explicit = [p.sequence for p in group if p.sequence is not None]
            if explicit and len(explicit) != len(group):
                raise ValueError("sequence must be supplied for every point in a segment, or omitted for all")
            if len(set(explicit)) != len(explicit):
                raise ValueError("duplicate sequence within one segment")
            if explicit:
                group.sort(key=lambda p: p.sequence)
            for point in group:
                if point.sequence is None:
                    point.sequence = len(ordered)
                ordered.append(point)
        self.points = ordered
        return self


def gcj02_to_wgs84(longitude: float, latitude: float) -> tuple[float, float]:
    """Invert the standard mainland GCJ-02 offset using fixed-point iteration."""
    if not (72.004 <= longitude <= 137.8347 and 0.8293 <= latitude <= 55.8271):
        return longitude, latitude

    def forward(lon: float, lat: float) -> tuple[float, float]:
        x, y = lon - 105.0, lat - 35.0
        lat_delta = -100 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*math.sqrt(abs(x))
        lat_delta += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
        lat_delta += (20*math.sin(y*math.pi) + 40*math.sin(y*math.pi/3))*2/3
        lat_delta += (160*math.sin(y*math.pi/12) + 320*math.sin(y*math.pi/30))*2/3
        lon_delta = 300 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*math.sqrt(abs(x))
        lon_delta += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
        lon_delta += (20*math.sin(x*math.pi) + 40*math.sin(x*math.pi/3))*2/3
        lon_delta += (150*math.sin(x*math.pi/12) + 300*math.sin(x*math.pi/30))*2/3
        rad = lat*math.pi/180
        magic = 1 - 0.00669342162296594323*math.sin(rad)**2
        root = math.sqrt(magic)
        lat_delta = lat_delta*180 / ((6378245*(1-0.00669342162296594323))/(magic*root)*math.pi)
        lon_delta = lon_delta*180 / (6378245/root*math.cos(rad)*math.pi)
        return lon+lon_delta, lat+lat_delta

    lon, lat = longitude, latitude
    for _ in range(5):
        shifted_lon, shifted_lat = forward(lon, lat)
        lon -= shifted_lon - longitude
        lat -= shifted_lat - latitude
    return lon, lat


def canonical_trip(trip: TripCreate) -> dict[str, Any]:
    record = trip.model_dump(mode="json")
    if record["geometry_crs"] == "GCJ02":
        for point in record["points"]:
            lon, lat = point["longitude"], point["latitude"]
            point["original_longitude"] = lon
            point["original_latitude"] = lat
            point["longitude"], point["latitude"] = gcj02_to_wgs84(lon, lat)
        record["geometry_crs"] = "WGS84"

    distance = 0.0
    previous = None
    for point in record["points"]:
        if previous and previous["segment_id"] == point["segment_id"]:
            lat1, lat2 = math.radians(previous["latitude"]), math.radians(point["latitude"])
            dlat = lat2-lat1
            dlon = math.radians(point["longitude"]-previous["longitude"])
            a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
            distance += 6371.0088*2*math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1-a)))
        previous = point
    speeds = [p["speed_kmh"] for p in record["points"] if p["speed_kmh"] is not None]
    stats = {"distance_km": round(distance, 6), "point_count": len(record["points"]),
             "observed_max_speed_kmh": max(speeds) if speeds else None}
    record["stats"] = stats
    record.update(stats)
    longitudes = [p["longitude"] for p in record["points"]]
    latitudes = [p["latitude"] for p in record["points"]]
    record["bounds"] = ([min(longitudes), min(latitudes), max(longitudes), max(latitudes)]
                        if longitudes else None)
    return record
