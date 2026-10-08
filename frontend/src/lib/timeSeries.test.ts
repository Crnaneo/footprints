import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { TrackPoint } from '../types';
import { availableDays, buildDailySeries, formatTime } from './timeSeries';

const originalTimezone = process.env.TZ;

beforeEach(() => { process.env.TZ = 'Asia/Singapore'; });
afterEach(() => {
  if (originalTimezone === undefined) delete process.env.TZ;
  else process.env.TZ = originalTimezone;
});

function point(timestamp: string | null, sequence: number, overrides: Partial<TrackPoint> = {}): TrackPoint {
  return { longitude: 116.4, latitude: 39.9, sequence, segment_id: '0', timestamp,
    altitude_m: 100, speed_kmh: 200, ...overrides };
}

const heights = (series: ReturnType<typeof buildDailySeries>): number[][] => series.altitude.map(segment => segment.map(sample => sample.value));
const speeds = (series: ReturnType<typeof buildDailySeries>): number[][] => series.speed.map(segment => segment.map(sample => sample.value));

describe('available telemetry days', () => {
  it('returns unique valid dates descending, including dates with no metrics', () => {
    const points = [
      point('2026-10-01T00:00:00Z', 0), point('2026-10-03T00:00:00Z', 1),
      point('2026-10-02T00:00:00Z', 2, { altitude_m: null, speed_kmh: null }),
      point('2026-10-03T12:00:00Z', 3), point(null, 4), point('bad time', 5),
      point('2026-02-30T00:00:00Z', 6), point('1', 7),
    ];
    expect(availableDays(points, 'utc')).toEqual(['2026-10-03', '2026-10-02', '2026-10-01']);
    expect(availableDays([], 'local')).toEqual([]);
  });

  it('uses the selected zone for the calendar day rather than a timestamp prefix', () => {
    const points = [point('2026-10-01T17:00:00Z', 0)];
    expect(availableDays(points, 'utc')).toEqual(['2026-10-01']);
    expect(availableDays(points, 'local')).toEqual(['2026-10-02']);
    const offsetPoint = point('2026-10-02T00:30:00+08:00', 0);
    expect(availableDays([offsetPoint], 'utc')).toEqual(['2026-10-01']);
    expect(availableDays([offsetPoint], 'local')).toEqual(['2026-10-02']);
  });

  it('interprets timezone-free ISO times consistently with backend UTC imports', () => {
    expect(availableDays([point('2026-10-01T17:00:00', 0)], 'local')).toEqual(['2026-10-02']);
  });
});

describe('daily telemetry series', () => {
  it('uses start-inclusive, end-exclusive UTC bounds and splits across other days', () => {
    const points = [
      point('2026-09-30T23:59:59Z', 0), point('2026-10-01T00:00:00Z', 1),
      point('2026-10-02T00:00:00Z', 2), point('2026-10-01T00:01:00Z', 3),
    ];
    const series = buildDailySeries(points, '2026-10-01', 'utc');
    expect(series.start).toBe(Date.parse('2026-10-01T00:00:00Z'));
    expect(series.end).toBe(Date.parse('2026-10-02T00:00:00Z'));
    expect(series.timedPointCount).toBe(2);
    expect(series.altitude.map(segment => segment.length)).toEqual([1, 1]);
    expect(series.speed.map(segment => segment.length)).toEqual([1, 1]);
  });

  it('constructs actual local midnight boundaries', () => {
    const series = buildDailySeries([point('2026-10-01T17:00:00Z', 0)], '2026-10-02', 'local');
    expect(series.start).toBe(Date.parse('2026-10-01T16:00:00Z'));
    expect(series.end).toBe(Date.parse('2026-10-02T16:00:00Z'));
    expect(series.timedPointCount).toBe(1);
  });

  it('keeps zero speed and altitude, negative altitude, and independent metric counts', () => {
    const series = buildDailySeries([
      point('2026-10-01T00:00:00Z', 0, { altitude_m: 0, speed_kmh: 0 }),
      point('2026-10-01T00:01:00Z', 1, { altitude_m: -20, speed_kmh: null }),
      point('2026-10-01T00:02:00Z', 2, { altitude_m: null, speed_kmh: 20 }),
      point('2026-10-01T00:03:00Z', 3, { altitude_m: null, speed_kmh: null }),
    ], '2026-10-01', 'utc');
    expect(heights(series)).toEqual([[0, -20]]);
    expect(speeds(series)).toEqual([[0], [20]]);
    expect(series.timedPointCount).toBe(4);
    expect(series.altitudePointCount).toBe(2);
    expect(series.speedPointCount).toBe(2);
  });

  it('splits only the metric whose value is missing or invalid', () => {
    const series = buildDailySeries([
      point('2026-10-01T00:00:00Z', 0, { altitude_m: 1, speed_kmh: 1 }),
      point('2026-10-01T00:01:00Z', 1, { altitude_m: NaN, speed_kmh: 2 }),
      point('2026-10-01T00:02:00Z', 2, { altitude_m: 3, speed_kmh: Infinity }),
      point('2026-10-01T00:03:00Z', 3, { altitude_m: 4, speed_kmh: 4 }),
      point('2026-10-01T00:04:00Z', 4, { altitude_m: 5, speed_kmh: -1 }),
    ], '2026-10-01', 'utc');
    expect(heights(series)).toEqual([[1], [3, 4, 5]]);
    expect(speeds(series)).toEqual([[1, 2], [4]]);
    expect(series.timedPointCount).toBe(5);
    expect(series.altitudePointCount).toBe(4);
    expect(series.speedPointCount).toBe(3);
  });

  it('does not coerce string, boolean, or null metric values to numbers', () => {
    const series = buildDailySeries([
      point('2026-10-01T00:00:00Z', 0, { altitude_m: '12' as unknown as number, speed_kmh: false as unknown as number }),
      point('2026-10-01T00:01:00Z', 1, { altitude_m: null, speed_kmh: null }),
    ], '2026-10-01', 'utc');
    expect(series.timedPointCount).toBe(2);
    expect(series.altitude).toEqual([]);
    expect(series.speed).toEqual([]);
  });

  it('breaks both metrics at missing or malformed times without filling gaps', () => {
    const series = buildDailySeries([
      point('2026-10-01T00:00:00Z', 0), point(null, 1),
      point('2026-10-01T00:02:00Z', 2), point('2026-10-01T25:00:00Z', 3),
      point('2026-10-01T00:04:00Z', 4), point('2026-02-30T00:00:00Z', 5),
      point('2026-10-01T00:06:00Z', 6),
    ], '2026-10-01', 'utc');
    expect(series.timedPointCount).toBe(4);
    expect(series.altitude.map(segment => segment.length)).toEqual([1, 1, 1, 1]);
    expect(series.speed.map(segment => segment.length)).toEqual([1, 1, 1, 1]);
  });

  it('starts a new segment on every source segment change, including a reused ID', () => {
    const series = buildDailySeries([
      point('2026-10-01T00:00:00Z', 0, { segment_id: 'a', altitude_m: 1 }),
      point('2026-10-01T00:01:00Z', 1, { segment_id: 'b', altitude_m: 2 }),
      point('2026-10-01T00:02:00Z', 2, { segment_id: 'a', altitude_m: 3 }),
    ], '2026-10-01', 'utc');
    expect(heights(series)).toEqual([[1], [2], [3]]);
    expect(series.speed.map(segment => segment.length)).toEqual([1, 1, 1]);
  });

  it('preserves source order and breaks on backwards time instead of sorting it away', () => {
    const points = [
      point('2026-10-01T00:00:00Z', 0, { altitude_m: 0 }),
      point('2026-10-01T00:02:00Z', 1, { altitude_m: 2 }),
      point('2026-10-01T00:01:00Z', 2, { altitude_m: 1 }),
      point('2026-10-01T00:03:00Z', 3, { altitude_m: 3 }),
    ];
    const series = buildDailySeries(points, '2026-10-01', 'utc');
    expect(heights(series)).toEqual([[0, 2], [1, 3]]);
    expect(series.altitude[1][0].point).toBe(points[2]);
    expect(points.map(point => point.sequence)).toEqual([0, 1, 2, 3]);
  });

  it('splits gaps longer than 15 minutes and retains exactly-15-minute connections', () => {
    const series = buildDailySeries([
      point('2026-10-01T00:00:00Z', 0), point('2026-10-01T00:15:00Z', 1),
      point('2026-10-01T00:30:01Z', 2), point('2026-10-01T00:30:01Z', 3),
    ], '2026-10-01', 'utc');
    expect(series.altitude.map(segment => segment.length)).toEqual([2, 2]);
    expect(series.speed.map(segment => segment.length)).toEqual([2, 2]);
  });

  it('has no invented series for untimed RailGo reconstruction points', () => {
    const points = [point(null, 0, { altitude_m: null, speed_kmh: null }), point(null, 1, { altitude_m: null, speed_kmh: null })];
    expect(availableDays(points, 'utc')).toEqual([]);
    const series = buildDailySeries(points, '2026-10-01', 'utc');
    expect(series.timedPointCount).toBe(0);
    expect(series.altitude).toEqual([]);
    expect(series.speed).toEqual([]);
  });

  it('rejects invalid selected calendar dates explicitly', () => {
    for (const day of ['bad', '2026-02-30', '2026-13-01', '2026-1-01']) {
      expect(() => buildDailySeries([], day, 'utc')).toThrow(RangeError);
    }
  });
});

describe('local daylight saving and clock formatting', () => {
  it('uses a 23-hour local spring-forward day and a 24-hour UTC day', () => {
    process.env.TZ = 'America/New_York';
    const local = buildDailySeries([], '2026-03-08', 'local');
    expect(local.start).toBe(Date.parse('2026-03-08T05:00:00Z'));
    expect(local.end).toBe(Date.parse('2026-03-09T04:00:00Z'));
    expect(local.end - local.start).toBe(23 * 60 * 60 * 1000);
    const utc = buildDailySeries([], '2026-03-08', 'utc');
    expect(utc.end - utc.start).toBe(24 * 60 * 60 * 1000);
  });

  it('uses a 25-hour fall-back day and preserves both repeated local clock instants', () => {
    process.env.TZ = 'America/New_York';
    const points = [point('2026-11-01T05:30:00Z', 0), point('2026-11-01T06:30:00Z', 1)];
    const series = buildDailySeries(points, '2026-11-01', 'local');
    expect(series.start).toBe(Date.parse('2026-11-01T04:00:00Z'));
    expect(series.end).toBe(Date.parse('2026-11-02T05:00:00Z'));
    expect(series.end - series.start).toBe(25 * 60 * 60 * 1000);
    const samples = series.altitude.flat();
    expect(samples[1].time - samples[0].time).toBe(60 * 60 * 1000);
    expect(samples.map(sample => formatTime(sample.time, 'local'))).toEqual(['01:30', '01:30']);
  });

  it('formats UTC or local HH:mm with optional seconds, keeping invalid instants empty', () => {
    const time = Date.parse('2026-10-01T00:05:06Z');
    expect(formatTime(time, 'utc')).toBe('00:05');
    expect(formatTime(time, 'utc', true)).toBe('00:05:06');
    expect(formatTime(time, 'local')).toBe('08:05');
    expect(formatTime(time, 'local', true)).toBe('08:05:06');
    expect(formatTime(NaN, 'utc')).toBe('—');
    expect(formatTime(Infinity, 'local')).toBe('—');
  });
});
