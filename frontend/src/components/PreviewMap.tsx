import { useEffect, useMemo, useRef, useState } from 'react';
import { AttributionControl, Map as MapLibreMap, NavigationControl, setWorkerUrl, type LayerSpecification, type StyleSpecification } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { TrackPoint } from '../types';
import { loadBasemapStyle } from '../lib/basemap';
import { boundsForPoints, tripColor } from '../lib/geometry';
import { previewGeometry } from '../lib/previewGeometry';

setWorkerUrl(workerUrl);

const TRACK_SOURCE = 'preview-track';
const TRACK_LAYER = 'preview-track-geometry';
const EMPTY_POINTS: TrackPoint[] = [];
const FALLBACK_STYLE: StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#121b27' } }],
};

/** A lightweight map that draws an unsaved track so the user can confirm before saving. */
export default function PreviewMap({ points, mode, gpsPoints = EMPTY_POINTS }: { points: TrackPoint[]; mode: string; gpsPoints?: TrackPoint[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'offline'>('loading');
  const color = tripColor(mode);
  const geojson = useMemo(() => previewGeometry(points, mode, gpsPoints), [points, mode, gpsPoints]);
  const bounds = useMemo(() => boundsForPoints([...points, ...gpsPoints], 105), [points, gpsPoints]);

  useEffect(() => {
    if (!containerRef.current) return;
    let disposed = false;
    let remoteStyleActive = false;
    let styleReady = false;
    const layer: LayerSpecification = mode === 'rail' ? {
      id: TRACK_LAYER, type: 'circle', source: TRACK_SOURCE, paint: {
        'circle-color': color, 'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 1.2, 12, 2.5, 18, 4],
        'circle-opacity': 0.95, 'circle-stroke-width': 0,
      },
    } : {
      id: TRACK_LAYER, type: 'line', source: TRACK_SOURCE, paint: {
        'line-color': ['case', ['==', ['get', 'source'], 'gps'], '#a7b6cd', color],
        'line-width': 2.4, 'line-opacity': 0.95,
      },
    };
    const withTrack = (style: StyleSpecification): StyleSpecification => ({
      ...style, sources: { ...style.sources, [TRACK_SOURCE]: { type: 'geojson', data: geojson } },
      layers: [...style.layers, layer],
    });
    const map = new MapLibreMap({
      // Include the track in the initial style: a fast cached basemap must not
      // call addSource against a fallback style that has not finished loading.
      container: containerRef.current, style: withTrack(FALLBACK_STYLE), center: [105, 32], zoom: 3,
      attributionControl: false, renderWorldCopies: false, maxTileCacheSize: 24, maxTileCacheZoomLevels: 2,
      pixelRatio: Math.min(window.devicePixelRatio || 1, 2),
      locale: { 'NavigationControl.ZoomIn': '放大', 'NavigationControl.ZoomOut': '缩小', 'AttributionControl.ToggleAttribution': '地图来源' },
    });
    map.addControl(new NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(new AttributionControl({ compact: true }), 'bottom-right');
    map.getCanvas().setAttribute('aria-label', mode === 'rail' ? '铁路预览地图，仅显示线路点位' : '轨迹预览地图');

    const fit = () => { if (bounds) map.fitBounds(bounds, { padding: 40, maxZoom: 12, duration: 0 }); };
    map.on('style.load', () => { styleReady = true; fit(); if (remoteStyleActive) setStatus('ready'); });
    map.on('load', fit);
    const resize = new ResizeObserver(() => { if (!disposed) { map.resize(); if (styleReady) fit(); } });
    resize.observe(containerRef.current);

    let controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    let styleTimeout: ReturnType<typeof setTimeout> | null = null;
    loadBasemapStyle(controller.signal)
      .then(style => {
        if (disposed) return;
        remoteStyleActive = true; styleReady = false;
        map.setStyle(withTrack(style), { diff: false });
        styleTimeout = setTimeout(() => {
          if (!disposed && !styleReady) {
            remoteStyleActive = false;
            map.setStyle(withTrack(FALLBACK_STYLE), { diff: false });
            setStatus('offline');
          }
        }, 15000);
      })
      .catch(() => { if (!disposed) setStatus('offline'); })
      .finally(() => clearTimeout(timeout));

    return () => { disposed = true; controller.abort(); clearTimeout(timeout); if (styleTimeout) clearTimeout(styleTimeout); resize.disconnect(); map.remove(); };
  }, [geojson, color, bounds, mode]);

  return <div className="preview-map">
    <div ref={containerRef} className="preview-map-canvas" />
    {status === 'loading' && <div className="map-status map-status--loading" role="status">正在加载预览底图…</div>}
    {status === 'offline' && <div className="map-status map-status--offline" role="status">底图暂时不可用，仍可确认轨迹范围后保存。</div>}
    <div className="map-help">保存前预览{mode === 'rail' ? ` · ${points.length.toLocaleString('zh-CN')} 个线路点 · 不自动连线` : gpsPoints.length ? ' · 青色：在线轨迹 · 灰色：本地 GPS' : ' · 仅显示本段轨迹范围'} · 拖动浏览 · 滚轮缩放</div>
  </div>;
}
