import { describe, expect, it } from 'vitest';
import type { Trip } from '../types';
import { formatTripDistance, tripDistance } from './tripDistance';

const flight = (info = {}): Pick<Trip, 'mode' | 'distance_km' | 'metadata'> => ({ mode: 'flight', distance_km: 99, metadata: { flight: info } });

describe('flight mileage from FR24', () => {
  it('uses the official actual distance instead of point-derived distance', () => {
    expect(tripDistance(flight({ actual_distance_km: 663.12 }))).toBe(663.12);
    expect(formatTripDistance(flight({ actual_distance_km: 663.12 }))).toBe('663.1');
    expect(tripDistance(flight({ summary: { actual_distance: 783.567 } }))).toBe(783.567);
  });
  it('shows unknown without substituting track statistics or great-circle distance', () => {
    expect(formatTripDistance(flight({ circle_distance_km: 80, track_stats: { flown_distance_km: 99 } }))).toBe('-.-');
    for (const value of [null, -1, NaN, Infinity, '123']) {
      expect(formatTripDistance(flight({ actual_distance_km: value }))).toBe('-.-');
    }
  });
  it('preserves a valid zero and non-flight mileage', () => {
    expect(formatTripDistance(flight({ actual_distance_km: 0 }))).toBe('0');
    expect(tripDistance({ mode: 'rail', distance_km: 99 })).toBe(99);
    expect(tripDistance({ mode: 'other', distance_km: 42 })).toBe(42);
  });
});
