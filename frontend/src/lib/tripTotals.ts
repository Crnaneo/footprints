import type { Trip } from '../types';

/** Count only recovered edges not already represented by original records. */
export function cumulativeDistance(trips: Trip[]): number {
  const ids = new Set(trips.map(trip => trip.id));
  return trips.reduce((sum, trip) => {
    const recovery = (trip.metadata?.gps_overlay || trip.metadata?.local_recovery) as {
      source_trip_ids?: string[]; covered_distance_km?: Record<string, number>;
    } | undefined;
    const covered = recovery?.covered_distance_km;
    if (covered) {
      const duplicated = Object.entries(covered).reduce((distance, [id, value]) => distance + (ids.has(id) ? value : 0), 0);
      return sum + Math.max(0, trip.distance_km - duplicated);
    }
    // Backward compatibility for recovered records without edge contributions.
    return sum + (recovery?.source_trip_ids?.length && recovery.source_trip_ids.every(id => ids.has(id)) ? 0 : trip.distance_km);
  }, 0);
}
