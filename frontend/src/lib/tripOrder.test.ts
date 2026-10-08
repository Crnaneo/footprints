import { expect, it } from 'vitest';
import type { Trip } from '../types';
import { compareTripDates } from './tripOrder';
const flight = (id: string, time: string | null, date = '2026-02-04') => ({ id, mode: 'flight', travel_date: date, created_at: `2026-10-08T${id === 'early' ? '20' : '10'}:00:00Z`, points: [], metadata: { flight: { takeoff_time: time } } }) as unknown as Trip;

it('puts later departures above earlier flights independently of import order', () => {
  const flights = [flight('early', '2026-02-04T01:00:00Z'), flight('late', '2026-02-04T09:00:00Z')];
  expect(flights.sort(compareTripDates).map(t => t.id)).toEqual(['late', 'early']);
  expect([...flights].sort((a, b) => compareTripDates(a, b, true)).map(t => t.id)).toEqual(['late', 'early']);
});

it('sorts by departure date before UTC time and places unspecified times last', () => {
  const flights = [flight('unknown', null), flight('early', '2026-02-04T01:00:00Z'), flight('tomorrow', '2026-02-04T23:00:00Z', '2026-02-05')];
  expect(flights.sort(compareTripDates).map(t => t.id)).toEqual(['tomorrow', 'early', 'unknown']);
});

it('uses airport-local scheduled time when an information-only flight has no observation time', () => {
  const scheduled = flight('schedule', null);
  scheduled.metadata = { flight: { departure_timezone: 'Asia/Tokyo' }, batch_import: { scheduled_departure_local: '10:00' } };
  expect(compareTripDates(scheduled, flight('early', '2026-02-04T00:30:00Z'))).toBeLessThan(0);
  expect(compareTripDates(scheduled, flight('late', '2026-02-04T01:30:00Z'))).toBeGreaterThan(0);
});
