import type { Trip } from '../types';

export type TrackPoint = Trip['points'][number];
export type Coordinate = [number, number];
export type TrackBounds = [Coordinate, Coordinate];

export function isValidPoint(point: Pick<TrackPoint, 'longitude' | 'latitude'>): boolean {
  return Number.isFinite(point.longitude) && Number.isFinite(point.latitude)
    && Math.abs(point.longitude) <= 180 && Math.abs(point.latitude) <= 90;
}

export function normalizeLongitude(longitude: number): number {
  const normalized = ((longitude + 180) % 360 + 360) % 360 - 180;
  return normalized === -180 && longitude > 0 ? 180 : normalized;
}

/** Preserve explicit gaps and split dateline crossings instead of drawing across the world. */
export function buildTrackSegments(points: readonly TrackPoint[]): Coordinate[][] {
  const ordered = [...points].sort((a, b) => a.sequence - b.sequence);
  const segments: Coordinate[][] = [];
  let current: Coordinate[] = [];
  let previousSegment: string | undefined;

  const finish = () => {
    if (current.length > 1) segments.push(current);
    current = [];
  };

  for (const point of ordered) {
    if (!isValidPoint(point)) {
      finish();
      previousSegment = undefined;
      continue;
    }
    const next: Coordinate = [normalizeLongitude(point.longitude), point.latitude];
    if (previousSegment !== undefined && point.segment_id !== previousSegment) finish();
    const previous = current.at(-1);
    if (previous && Math.abs(next[0] - previous[0]) > 180) {
      const crossingEast = previous[0] > next[0];
      const boundary = crossingEast ? 180 : -180;
      const unwrappedNext = next[0] + (crossingEast ? 360 : -360);
      const fraction = (boundary - previous[0]) / (unwrappedNext - previous[0]);
      const latitude = previous[1] + (next[1] - previous[1]) * fraction;
      if (previous[0] !== boundary || previous[1] !== latitude) current.push([boundary, latitude]);
      finish();
      current = [[-boundary, latitude]];
    }
    current.push(next);
    previousSegment = point.segment_id;
  }
  finish();
  return segments;
}

/** Find the shortest circular longitude interval, including routes across the Pacific. */
export function boundsForPoints(
  points: readonly Pick<TrackPoint, 'longitude' | 'latitude'>[],
  centerLongitude = 105,
): TrackBounds | null {
  const valid = points.filter(isValidPoint);
  if (!valid.length) return null;
  const longitudes = valid.map((point) => ((point.longitude % 360) + 360) % 360).sort((a, b) => a - b);
  let largestGap = -1;
  let gapIndex = 0;
  for (let index = 0; index < longitudes.length; index += 1) {
    const next = index + 1 < longitudes.length ? longitudes[index + 1] : longitudes[0] + 360;
    const gap = next - longitudes[index];
    if (gap > largestGap) {
      largestGap = gap;
      gapIndex = index;
    }
  }
  let west = longitudes[(gapIndex + 1) % longitudes.length];
  let east = west + 360 - largestGap;
  const shift = Math.round((centerLongitude - (west + east) / 2) / 360) * 360;
  west += shift;
  east += shift;
  let south = Infinity;
  let north = -Infinity;
  for (const point of valid) {
    const latitude = Math.max(-85.051129, Math.min(85.051129, point.latitude));
    south = Math.min(south, latitude);
    north = Math.max(north, latitude);
  }
  return [[west, south], [east, north]];
}

export function tripColor(mode: string): string {
  if (mode === 'flight') return '#61dce5';
  if (mode === 'rail') return '#f3bf69';
  return '#b4a1ff';
}
