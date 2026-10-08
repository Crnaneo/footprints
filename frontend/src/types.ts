export type Mode = 'flight' | 'rail' | 'other';
export interface TrackPoint {
  source?: string | null;
  longitude: number; latitude: number; sequence: number; segment_id: string;
  timestamp: string | null; altitude_m: number | null; speed_kmh: number | null;
  original_longitude?: number | null; original_latitude?: number | null;
}
export interface GpsOverlay {
  point_count: number;
  sources: { trip_id: string; ranges: [number, number][]; fingerprint: string; point_count: number }[];
  bounds: [[number, number], [number, number]];
  covered_distance_km?: Record<string, number>;
}
export interface Trip {
  id: string; title: string; mode: Mode; travel_date: string;
  service_number: string | null; origin: string | null; destination: string | null;
  source: string; source_crs: string; geometry_crs?: string;
  geometry_kind: 'observed' | 'reconstructed' | 'imported' | 'scheduled';
  notes: string | null; quality_flags?: string[]; metadata?: Record<string, unknown>;
  distance_km: number; point_count: number; observed_max_speed_kmh: number | null;
  design_speed_kmh?: number | null; line_speed_limit_kmh?: number | null;
  preview?: boolean; created_at: string; points: TrackPoint[];
  bounds?: [[number, number], [number, number]] | null;
  map_revision?: number;
}
export interface FlightCandidate {
  fr24_id: string; flight?: string; origin?: string; destination?: string;
  departure?: string | null; arrival?: string | null;
}

/** A not-yet-saved trip returned by the /api/preview/* endpoints. */
export interface TripDraft {
  title: string; mode: Mode; travel_date: string; points: TrackPoint[];
  [key: string]: unknown;
}

export interface ViaStation { id: string; name: string; kind: string; longitude: number; latitude: number }
export interface ViaLine { line_id: string; name: string }
export interface ViaCatalogStation { id: string; name: string; kind: string; order: number }
export interface ViaTimeField { time: string; day_offset: number; iso: string }
export interface ViaSegment {
  index: number; line_name: string; origin_name: string; via_name: string;
  distance_km: number; max_speed_kmh: number; speed_source: 'osm' | 'estimate';
  departure: ViaTimeField; arrival: ViaTimeField;
}
export interface ViaPreviewResult { trip: TripDraft; segments: ViaSegment[]; total_distance_km: number }

/** Uniform flight identity attached by the reconstruction/enrich chain. */
export interface FlightInfo {
  travel_date?: string | null;
  flight_number?: string | null; callsign?: string | null; icao24?: string | null;
  registration?: string | null; aircraft_type?: string | null;
  departure_airport?: string | null; arrival_airport?: string | null;
  departure_airport_name?: string | null; arrival_airport_name?: string | null;
  takeoff_time?: string | null; landing_time?: string | null;
  departure_timezone?: string | null; arrival_timezone?: string | null;
  takeoff_runway?: string | null; landing_runway?: string | null;
  /** True when the runway ident came from FR24's official summary. */
  runway_takeoff_official?: boolean; runway_landed_official?: boolean;
  /** Navaids flown over during cruise, inferred from the track (OurAirports). */
  route_waypoints?: { ident: string; name: string; type: string }[] | null;
  /** Cruise-level statistics computed from the observed points. */
  track_stats?: {
    max_altitude_m: number; cruise_altitude_m: number;
    cruise_speed_kmh?: number | null; max_speed_kmh?: number | null;
    flown_distance_km: number;
  } | null;
  /** flight-summary/full extras (FR24 chain only). */
  operating_as?: string | null; painted_as?: string | null;
  aircraft_category?: string | null;
  departure_iata?: string | null; arrival_iata?: string | null;
  arrival_airport_actual?: string | null; arrival_iata_actual?: string | null;
  flight_time_s?: number | null;
  actual_distance_km?: number | null; circle_distance_km?: number | null;
  /** adsbdb registry enrichment (present only when it answered). */
  icao_type?: string | null; registered_owner?: string | null;
  owner_country?: string | null;
  photo_url?: string | null; photo_thumbnail?: string | null;
  photo_photographer?: string | null; photo_link?: string | null;
  airline_name?: string | null; callsign_iata?: string | null;
  /** Set when the geometry was fitted from the same service on a nearby day. */
  borrowed_from_date?: string | null;
  track_source?: string | null;
  times_inferred?: boolean;
  identity_source?: string; identity_lookup_note?: string;
  identity_evidence?: { source_url: string; license: string; date_confirmed: boolean; origin_airports: string[]; destination_airports: string[]; airport_evidence?: { source: string; source_trip_ids: string[]; matched_adsb_endpoints: number } };
  /** Present only when a source provides procedure/airway data; hidden otherwise. */
  sid?: string | null; star?: string | null; approach?: string | null; airways?: string[] | null;
}

/** A candidate flight returned by /api/preview/flight-reconstruct on 409. */
export interface ReconstructCandidate {
  schedule_candidate_id?: string; registration?: string | null; date_confirmed?: boolean;
  observation_date?: string; observation_timezone?: string;
  icao24: string; callsign: string | null;
  first_seen: number | null; last_seen: number | null;
  est_departure_airport: string | null; est_arrival_airport: string | null;
  departure_local: string | null;
}

export interface LocalFlightCandidate {
  local_candidate_id: string; departure_local: string; origin: string; destination: string; point_count: number;
}
