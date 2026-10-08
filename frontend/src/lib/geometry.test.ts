import { describe, expect, it } from 'vitest';
import { boundsForPoints, buildTrackSegments, type TrackPoint } from './geometry';

function point(longitude: number, latitude: number, sequence: number, segmentId = '0'): TrackPoint {
  return { longitude, latitude, sequence, segment_id: segmentId, timestamp: null, altitude_m: null, speed_kmh: null };
}

describe('trajectory geometry', () => {
  it('splits both directions at the dateline instead of joining across the map', () => {
    expect(buildTrackSegments([point(179, 10, 0), point(-179, 12, 1)])).toEqual([
      [[179, 10], [180, 11]], [[-180, 11], [-179, 12]],
    ]);
    expect(buildTrackSegments([point(-179, 12, 0), point(179, 10, 1)])).toEqual([
      [[-179, 12], [-180, 11]], [[180, 11], [179, 10]],
    ]);
    expect(buildTrackSegments([point(180, 10, 0), point(-179, 12, 1)])).toEqual([
      [[-180, 10], [-179, 12]],
    ]);
  });

  it('fits a Pacific crossing in two degrees and chooses the nearest world copy', () => {
    const points = [point(179, 10, 0), point(-179, 12, 1)];
    expect(boundsForPoints(points)).toEqual([[179, 10], [181, 12]]);
    expect(boundsForPoints(points, -170)).toEqual([[-181, 10], [-179, 12]]);
  });

  it('preserves explicit source segments and sorts points by sequence', () => {
    expect(buildTrackSegments([
      point(111, 41, 3, 'b'), point(101, 31, 1, 'a'), point(110, 40, 2, 'b'), point(100, 30, 0, 'a'),
    ])).toEqual([[[100, 30], [101, 31]], [[110, 40], [111, 41]]]);
  });

  it('does not connect valid coordinates around an invalid point', () => {
    expect(buildTrackSegments([point(100, 30, 0), point(NaN, 31, 1), point(110, 40, 2)])).toEqual([]);
    expect(boundsForPoints([point(200, 30, 0)])).toBeNull();
  });

  it('handles empty and single-point imports without invented line segments', () => {
    expect(boundsForPoints([])).toBeNull();
    expect(boundsForPoints([point(104, 31, 0)])).toEqual([[104, 31], [104, 31]]);
    expect(buildTrackSegments([point(104, 31, 0)])).toEqual([]);
  });
});
