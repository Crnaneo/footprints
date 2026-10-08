import type { TrackPoint } from '../types';

export type TimeZoneMode = 'local' | 'utc';
export type TimeMetric = 'altitude_m' | 'speed_kmh';

export interface TimedPoint {
  /** Unix time in milliseconds, matching JavaScript Date and chart axes. */
  time: number;
  value: number;
  point: TrackPoint;
}

export interface DailySeries {
  day: string;
  start: number;
  end: number;
  altitude: TimedPoint[][];
  speed: TimedPoint[][];
  timedPointCount: number;
  altitudePointCount: number;
  speedPointCount: number;
}

const MAX_CONNECTED_GAP = 15 * 60 * 1000;
const pad = (value: number, width = 2): string => String(value).padStart(width, '0');

function calendarDate(year: number, month: number, day: number): Date | null {
  // setUTCFullYear preserves years 0000–0099, unlike Date.UTC's 1900 offset.
  const result = new Date(0);
  result.setUTCFullYear(year, month - 1, day);
  result.setUTCHours(0, 0, 0, 0);
  return result.getUTCFullYear() === year && result.getUTCMonth() === month - 1 && result.getUTCDate() === day
    ? result : null;
}

function pointTime(point: TrackPoint): number | null {
  if (typeof point.timestamp !== 'string') return null;
  const value = point.timestamp.trim();
  // Date.parse accepts non-ISO strings and rolls February 30 into March.
  // Source records use ISO dates; explicitly validate their calendar and clock.
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(Z|[+-]\d{2}:\d{2})?)?$/i.exec(value);
  if (!match || !calendarDate(Number(match[1]), Number(match[2]), Number(match[3]))) return null;
  if (match[4] && (Number(match[4]) > 23 || Number(match[5]) > 59 || Number(match[6] ?? 0) > 59)) return null;
  // The backend normalizes timezone-free ISO imports to UTC. Keep this rule
  // consistent instead of letting browser location reinterpret the instant.
  const normalized = match[4] && !match[7] ? `${value}Z` : value;
  const instant = Date.parse(normalized);
  return Number.isFinite(instant) ? instant : null;
}

function dayKey(time: number, zone: TimeZoneMode): string {
  const instant = new Date(time);
  const year = zone === 'utc' ? instant.getUTCFullYear() : instant.getFullYear();
  const month = zone === 'utc' ? instant.getUTCMonth() : instant.getMonth();
  const day = zone === 'utc' ? instant.getUTCDate() : instant.getDate();
  return `${pad(year, 4)}-${pad(month + 1)}-${pad(day)}`;
}

function dayBounds(day: string, zone: TimeZoneMode): [number, number] {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  if (!match) throw new RangeError('日期必须为有效的 YYYY-MM-DD');
  const year = Number(match[1]), month = Number(match[2]), date = Number(match[3]);
  const calendar = calendarDate(year, month, date);
  if (!calendar) throw new RangeError('日期必须为有效的 YYYY-MM-DD');
  if (zone === 'utc') return [calendar.getTime(), calendar.getTime() + 24 * 60 * 60 * 1000];
  const start = new Date(0);
  start.setFullYear(year, month - 1, date);
  start.setHours(0, 0, 0, 0);
  const end = new Date(0);
  end.setFullYear(year, month - 1, date + 1);
  end.setHours(0, 0, 0, 0);
  // Construct each local midnight independently: a DST day can be 23 or 25h.
  return [start.getTime(), end.getTime()];
}

function metricValue(point: TrackPoint, metric: TimeMetric): number | null {
  const value = point[metric];
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  if (metric === 'speed_kmh' && value < 0) return null;
  return value;
}

export function availableDays(points: TrackPoint[], zone: TimeZoneMode): string[] {
  const days = new Set<string>();
  for (const point of points) {
    const time = pointTime(point);
    if (time !== null) days.add(dayKey(time, zone));
  }
  return [...days].sort().reverse();
}

export function buildDailySeries(points: TrackPoint[], day: string, zone: TimeZoneMode): DailySeries {
  const [start, end] = dayBounds(day, zone);
  const result: DailySeries = {
    day, start, end, altitude: [], speed: [],
    timedPointCount: 0, altitudePointCount: 0, speedPointCount: 0,
  };
  let previousTime: number | null = null;
  let previousSegment: string | null = null;
  let altitude: TimedPoint[] | null = null;
  let speed: TimedPoint[] | null = null;

  // Preserve the supplied source/sequence order. Sorting by timestamp would
  // remove time reversals and connect observations around missing records.
  for (const point of points) {
    const time = pointTime(point);
    if (time === null || time < start || time >= end) {
      previousTime = null;
      previousSegment = null;
      altitude = null;
      speed = null;
      continue;
    }
    result.timedPointCount += 1;
    if (previousTime !== null && (point.segment_id !== previousSegment || time < previousTime || time - previousTime > MAX_CONNECTED_GAP)) {
      altitude = null;
      speed = null;
    }

    const height = metricValue(point, 'altitude_m');
    if (height === null) {
      altitude = null;
    } else {
      if (altitude === null) {
        altitude = [];
        result.altitude.push(altitude);
      }
      altitude.push({ time, value: height, point });
      result.altitudePointCount += 1;
    }

    const velocity = metricValue(point, 'speed_kmh');
    if (velocity === null) {
      speed = null;
    } else {
      if (speed === null) {
        speed = [];
        result.speed.push(speed);
      }
      speed.push({ time, value: velocity, point });
      result.speedPointCount += 1;
    }
    previousTime = time;
    previousSegment = point.segment_id;
  }
  return result;
}

export function formatTime(time: number, zone: TimeZoneMode, withSeconds = false): string {
  if (!Number.isFinite(time)) return '—';
  const instant = new Date(time);
  if (!Number.isFinite(instant.getTime())) return '—';
  const hours = zone === 'utc' ? instant.getUTCHours() : instant.getHours();
  const minutes = zone === 'utc' ? instant.getUTCMinutes() : instant.getMinutes();
  const seconds = zone === 'utc' ? instant.getUTCSeconds() : instant.getSeconds();
  return `${pad(hours)}:${pad(minutes)}${withSeconds ? `:${pad(seconds)}` : ''}`;
}
