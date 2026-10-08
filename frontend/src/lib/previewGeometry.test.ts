import { expect, it } from 'vitest';
import { previewGeometry } from './previewGeometry';
import type { TrackPoint } from '../types';

const point = (longitude: number, latitude: number, sequence: number, segment_id = '0'): TrackPoint => ({
  longitude, latitude, sequence, segment_id, timestamp: null, altitude_m: null, speed_kmh: null,
});

it('rail preview retains source points and isolated pieces without lines or extra points', () => {
  const points = [point(110, 35, 0), point(110.01, 35.00004, 1), point(111, 36, 2, 'gap')];
  const preview = previewGeometry(points, 'rail');
  expect(preview.features.map(f => f.geometry)).toEqual([{
    type: 'MultiPoint', coordinates: points.map(p => [p.longitude, p.latitude]),
  }]);
  expect(previewGeometry([point(179, 10, 0), point(-179, 11, 1)], 'rail').features[0].geometry).toEqual({
    type: 'MultiPoint', coordinates: [[179, 10], [-179, 11]],
  });
  expect(previewGeometry([], 'rail').features).toEqual([]);
});

it('flight preview keeps source groups and explicit gaps', () => {
  const online = [point(110, 35, 0), point(111, 35.1, 1), point(112, 36, 2, 'gap')];
  const gps = [point(113, 37, 0), point(114, 37.1, 1)];
  const preview = previewGeometry(online, 'flight', gps);
  expect(preview.features.map(f => [f.properties?.source, f.geometry.type])).toEqual([
    ['online', 'LineString'], ['gps', 'LineString'],
  ]);
  expect(preview.features[0].geometry).toEqual({type: 'LineString', coordinates: [[110, 35], [111, 35.1]]});
});
