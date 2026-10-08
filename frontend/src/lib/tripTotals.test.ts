import { describe, expect, it } from 'vitest';
import type { Trip } from '../types';
import { cumulativeDistance } from './tripTotals';

const source = (id: string, distance_km: number) => ({ id, distance_km } as Trip);
const recovered = { id: 'flight', distance_km: 100, metadata: {
  local_recovery: { source_trip_ids: ['day1', 'day2'], covered_distance_km: { day1: 40, day2: 50 } },
} } as unknown as Trip;

describe('recovered flight distance', () => {
  it('counts original observations once and counts newly joined edges', () => {
    expect(cumulativeDistance([source('day1', 80), source('day2', 90), recovered])).toBe(180);
  });
  it('counts a recovered copy when all original records were removed', () => {
    expect(cumulativeDistance([recovered])).toBe(100);
  });
  it('subtracts only the part still present when one source day was removed', () => {
    expect(cumulativeDistance([source('day1', 80), recovered])).toBe(140);
  });
});

describe('online flight with original GPS', () => {
  const online = {id:'online',distance_km:120,metadata:{gps_overlay:{covered_distance_km:{gps:90}}}} as unknown as Trip;
  it('adds only online distance not already covered by original GPS', () => {
    expect(cumulativeDistance([source('gps', 100), online])).toBe(130);
  });
  it('counts all online distance when the original GPS is deleted', () => {
    expect(cumulativeDistance([online])).toBe(120);
  });
});
