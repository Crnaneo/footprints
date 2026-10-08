import type { Feature, FeatureCollection } from 'geojson';
import type { TrackPoint } from '../types';
import { buildTrackSegments, isValidPoint } from './geometry';

/** Rail previews show source positions only, including isolated segment ends. */
export function previewGeometry(points: TrackPoint[], mode: string, gpsPoints: TrackPoint[] = []): FeatureCollection {
  const groups = [{ points, source: 'online' }, { points: gpsPoints, source: 'gps' }];
  return {
    type: 'FeatureCollection',
    features: groups.flatMap<Feature>(group => {
      if (mode === 'rail') {
        const coordinates = group.points.filter(isValidPoint).map(p => [p.longitude, p.latitude]);
        return coordinates.length ? [{
          type: 'Feature' as const, properties: { source: group.source },
          geometry: { type: 'MultiPoint' as const, coordinates },
        }] : [];
      }
      return buildTrackSegments(group.points).map(coordinates => ({
        type: 'Feature' as const, properties: { source: group.source },
        geometry: { type: 'LineString' as const, coordinates },
      }));
    }),
  };
}
