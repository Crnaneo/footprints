import { describe, expect, it } from 'vitest';
import { formatAirportTime } from './flightTime';

describe('airport-local flight times', () => {
  it('uses separate endpoint zones and preserves the local date across midnight', () => {
    expect(formatAirportTime('2026-02-08T19:24:19Z', 'Asia/Ho_Chi_Minh')).toBe('2026-02-09 02:24:19 UTC+07:00');
    expect(formatAirportTime('2026-02-09T02:47:45Z', 'Australia/Sydney')).toBe('2026-02-09 13:47:45 UTC+11:00');
    expect(formatAirportTime('2026-02-09T02:47:45Z', 'America/Los_Angeles')).toBe('2026-02-08 18:47:45 UTC-08:00');
  });
  it('applies the offset at the flight date, including summer time and half hours', () => {
    expect(formatAirportTime('2026-07-09T02:47:45Z', 'Australia/Sydney')).toBe('2026-07-09 12:47:45 UTC+10:00');
    expect(formatAirportTime('2026-02-09T02:47:45Z', 'Asia/Kolkata')).toBe('2026-02-09 08:17:45 UTC+05:30');
  });
  it('labels unknown zones explicitly and does not use the browser timezone', () => {
    expect(formatAirportTime('2026-02-09T02:47:45')).toBe('2026-02-09 02:47:45 UTC+00:00（机场时区未知）');
    expect(formatAirportTime('2026-02-09T02:47:45Z', 'Invalid/Zone')).toBe('2026-02-09 02:47:45 UTC+00:00（机场时区未知）');
    expect(formatAirportTime(null, 'Asia/Tokyo')).toBeNull();
    expect(formatAirportTime('invalid', 'Asia/Tokyo')).toBeNull();
  });
});
