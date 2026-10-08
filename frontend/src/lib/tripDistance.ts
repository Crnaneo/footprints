import type { FlightInfo, Trip } from '../types';

/** Flight mileage comes only from FR24's full flight summary, in kilometres. */
export function tripDistance(trip: Pick<Trip, 'mode' | 'metadata' | 'distance_km'>): number | null {
  if (trip.mode !== 'flight') return trip.distance_km;
  const flight = trip.metadata?.flight as (FlightInfo & { summary?: { actual_distance?: unknown } }) | undefined;
  const value = flight?.actual_distance_km ?? flight?.summary?.actual_distance;
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

export function formatTripDistance(trip: Pick<Trip, 'mode' | 'metadata' | 'distance_km'>): string {
  const distance = tripDistance(trip);
  return distance == null ? '-.-' : distance.toLocaleString('zh-CN', { maximumFractionDigits: 1 });
}
