import { memo, useEffect, useMemo, useRef, useState } from 'react';
import {
  AttributionControl,
  Map as MapLibreMap,
  NavigationControl,
  Popup,
  ScaleControl,
  setWorkerUrl,
  type VectorTileSource,
  type MapLayerMouseEvent,
  type StyleSpecification,
} from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { GpsOverlay, Trip, TrackPoint } from '../types';
import { api } from '../lib/api';
import { loadBasemapStyle } from '../lib/basemap';
import { boundsForPoints, type TrackBounds } from '../lib/geometry';

setWorkerUrl(workerUrl);

interface TrackMapProps {
  trips: Trip[];
  ready: boolean;
  selectedId: string | null;
  selectedTrip: Trip | null;
  onSelect: (id: string) => void;
  fitNonce: number;
}

type MapStatus = 'loading' | 'ready' | 'offline' | 'unavailable';
interface MapView {
  id: string; point_count: number; bounds: TrackBounds | null;
  union_only: boolean; gps_point_count: number; online_point_count: number;
  related_trip_ids: Record<string, string[]>;
}
const POINT_SOURCE = 'footprint-points';
const POINT_LAYER = 'footprint-track-point';
const FALLBACK_STYLE: StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#121b27' } }],
};

function popupRow(container: HTMLElement, label: string, value: string) {
  const row = document.createElement('div');
  row.className = 'track-popup-row';
  const key = document.createElement('span');
  key.textContent = label;
  const text = document.createElement('strong');
  text.textContent = value;
  row.append(key, text);
  container.append(row);
}

function numericValue(value: number | null | undefined, unit: string, precision = 0): string {
  return value !== null && value !== undefined && Number.isFinite(value)
    ? `${value.toFixed(precision)} ${unit}` : '未知';
}

function TrackMap(props: TrackMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const latestProps = useRef(props);
  latestProps.current = props;
  const syncRef = useRef<(() => void) | null>(null);
  const retryRef = useRef<(() => void) | null>(null);
  const [status, setStatus] = useState<MapStatus>('loading');
  const [viewId,setViewId] = useState<string|null>(null);
  const [pointCount,setPointCount] = useState(0);
  const [pointBounds,setPointBounds] = useState<TrackBounds|null>(null);
  const boundsRef=useRef<TrackBounds|null>(null);boundsRef.current=pointBounds;
  const [pointError,setPointError] = useState('');
  const [unionInfo,setUnionInfo] = useState<{union_only:boolean;gps_point_count:number;online_point_count:number;related_trip_ids:Record<string,string[]>} | null>(null);
  const unionRef=useRef(unionInfo);unionRef.current=unionInfo;
  const viewRef = useRef<string|null>(null);viewRef.current=viewId;
  const viewCache = useRef(new Map<string, MapView>());
  const signature=useMemo(()=>props.trips.map(t=>`${t.id}:${t.map_revision ?? `${t.point_count}:${t.travel_date}`}`).sort().join('|'),[props.trips]);
  useEffect(()=>{
    if (!props.ready) return;
    const controller=new AbortController();setPointError('');
    const apply = (d: MapView) => {
      if (controller.signal.aborted) return;
      setViewId(d.id);setPointCount(d.point_count);setPointBounds(d.bounds);setUnionInfo(d);
    };
    const cache = viewCache.current;
    const cached = cache.get(signature);
    if (cached) {
      cache.delete(signature);cache.set(signature,cached);apply(cached);
      return () => controller.abort();
    }
    // Initial load starts immediately. Coalesce rapid filter/search changes;
    // cached filters return synchronously without a new registration request.
    const timer = setTimeout(() => {
      void api<MapView>('/api/map/views',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trip_ids:latestProps.current.trips.map(t=>t.id)}),signal:controller.signal})
        .then(d=>{
          if (controller.signal.aborted) return;
          cache.set(signature,d);
          while (cache.size > 16) cache.delete(cache.keys().next().value!);
          apply(d);
        }).catch(e=>{if(!controller.signal.aborted)setPointError(e.message)});
    }, viewRef.current ? 150 : 0);
    return ()=>{clearTimeout(timer);controller.abort();};
  },[signature,props.ready]);

  useEffect(() => {
    if (!containerRef.current) return;
    let disposed = false;
    let map: MapLibreMap;
    try {
      map = new MapLibreMap({
        container: containerRef.current,
        style: FALLBACK_STYLE,
        center: [105, 32],
        zoom: 3.2,
        minZoom: 0,
        maxZoom: 19,
        attributionControl: false,
        renderWorldCopies: true,
        maxTileCacheSize: 32,
        maxTileCacheZoomLevels: 2,
        pixelRatio: Math.min(window.devicePixelRatio || 1, 2),
        locale: {
          'NavigationControl.ZoomIn': '放大',
          'NavigationControl.ZoomOut': '缩小',
          'NavigationControl.ResetBearing': '恢复正北方向',
          'AttributionControl.ToggleAttribution': '地图来源',
        },
      });
    } catch {
      setStatus('unavailable');
      return;
    }
    mapRef.current = map;
    map.addControl(new NavigationControl({ showCompass: true }), 'top-right');
    map.addControl(new ScaleControl({ unit: 'metric', maxWidth: 120 }), 'bottom-left');
    map.addControl(new AttributionControl({
      compact: true,
    }), 'bottom-right');
    map.getCanvas().setAttribute('aria-label', '足迹地图，可使用方向键平移、加减键缩放');
    const popup = new Popup({ className: 'track-map-popup', closeButton: true, closeOnClick: false, offset: 12, maxWidth: '300px' });
    let activeView: string | null = null;
    let detailAbort: AbortController | null = null;
    let paintedSelection: string | null | undefined;
    let abortController: AbortController | null = null;
    let styleTimeout: ReturnType<typeof setTimeout> | null = null;
    let styleReadyTimeout: ReturnType<typeof setTimeout> | null = null;
    let styleReady = false;
    let fitted = false;
    let fittedSelection: string | null = null;
    let fittedPointLength = 0;
    let fittedNonce = props.fitNonce;
    let remoteStyleActive = false;

    const tileUrl=(view:string)=>`${window.location.origin}/api/map/tiles/${view}/{z}/{x}/{y}.pbf`;
    const ensureLayers = () => {
      if (!viewRef.current) return;
      if (!map.getSource(POINT_SOURCE)) {
        map.addSource(POINT_SOURCE,{type:'vector',tiles:[tileUrl(viewRef.current)],minzoom:0,maxzoom:19});
        activeView=viewRef.current;paintedSelection=undefined;
        map.addLayer({id:POINT_LAYER,type:'circle',source:POINT_SOURCE,'source-layer':'points',paint:{
          'circle-color':['get','color'],
          'circle-radius':['interpolate',['linear'],['zoom'],2,0.8,8,1.3,13,2.5,18,4],
          'circle-opacity':0.8,
          'circle-stroke-width':0,
        }});
      } else if(activeView!==viewRef.current) {
        (map.getSource(POINT_SOURCE) as VectorTileSource).setTiles([tileUrl(viewRef.current)]);
        activeView=viewRef.current;paintedSelection=undefined;
      }
    };

    const sync = () => {
      if (disposed || !styleReady) return;
      if (!map.getSource(POINT_SOURCE) || activeView!==viewRef.current) fitted=false;
      const { selectedTrip, selectedId, fitNonce } = latestProps.current;
      const pointLength=selectedTrip?.points.length||0;
      // Do not flatten/re-scan every selected GPS point on unrelated UI renders.
      if (!fitted || selectedId!==fittedSelection || fitNonce!==fittedNonce || pointLength!==fittedPointLength) {
        let bounds=selectedId?(selectedTrip?.bounds ?? boundsForPoints(selectedTrip?.points||[],map.getCenter().lng)):boundsRef.current;
        const overlay=selectedTrip?.metadata?.gps_overlay as GpsOverlay|undefined;
        if (overlay && unionRef.current?.related_trip_ids[selectedId || '']?.length) bounds=overlay.bounds;
        if(bounds){const shift=Math.round((map.getCenter().lng-(bounds[0][0]+bounds[1][0])/2)/360)*360;bounds=[[bounds[0][0]+shift,bounds[0][1]],[bounds[1][0]+shift,bounds[1][1]]]}
        if (bounds) {
          const mapContainer = map.getContainer();
          const width = mapContainer.clientWidth;
          const height = mapContainer.clientHeight;
          const padding = {
            top: Math.min(100, height * 0.18),
            right: Math.min(90, width * 0.18),
            bottom: Math.min(100, height * 0.18),
            left: Math.min(90, width * 0.18),
          };
          const detail = selectedId ? mapContainer.closest('.map-workspace')?.querySelector<HTMLElement>('.trip-detail') : null;
          if (detail) {
            const mapRect = mapContainer.getBoundingClientRect();
            const detailRect = detail.getBoundingClientRect();
            const overlaps = detailRect.width > 0 && detailRect.height > 0
              && detailRect.right > mapRect.left && detailRect.left < mapRect.right
              && detailRect.bottom > mapRect.top && detailRect.top < mapRect.bottom;
            if (overlaps) {
              const clearLeft = Math.max(padding.left, detailRect.right - mapRect.left + 24);
              if (width - clearLeft - padding.right >= 300) {
                padding.left = clearLeft;
              } else {
                const clearBottom = Math.max(padding.bottom, height - (detailRect.top - mapRect.top) + 24);
                const minimumViewportHeight = Math.min(100, Math.max(24, height * 0.2));
                const spaceAboveDetail = Math.max(0, height - clearBottom);
                if (spaceAboveDetail - padding.top < minimumViewportHeight) {
                  padding.top = Math.min(padding.top, spaceAboveDetail * 0.25);
                }
                const viewportHeight = Math.min(minimumViewportHeight, Math.max(1, spaceAboveDetail - padding.top));
                padding.bottom = Math.min(clearBottom, Math.max(0, height - padding.top - viewportHeight));
              }
            }
          }
          map.fitBounds(bounds, {
            padding,
            maxZoom: 13,
            // Selection/filter changes should request the destination tiles,
            // without loading every intermediate viewport during a flight.
            duration: 0,
          });
          fitted = true;
          fittedSelection = selectedId;
          fittedNonce = fitNonce;fittedPointLength=pointLength;
        }
      }
      // Fit first, so the initial source requests only the final viewport tiles.
      ensureLayers();
      if (map.getLayer(POINT_LAYER) && paintedSelection!==selectedId) {
        const selectedIds=selectedId?[selectedId,...(unionRef.current?.related_trip_ids[selectedId] || [])]:[];
        map.setPaintProperty(POINT_LAYER,'circle-opacity',selectedId?['case',['in',['get','tripId'],['literal',selectedIds]],1,0.3]:0.8);
        paintedSelection=selectedId;
      }
    };
    syncRef.current = sync;
    // Hit-test only on click: querying dense tiles on every mouse move blocks
    // map panning, and background hover requests usually get aborted anyway.
    map.on('click',POINT_LAYER,async (event: MapLayerMouseEvent) => {
      const feature=event.features?.[0];
      if (!feature) return;
      const selectedFlight=latestProps.current.selectedId;
      if (!selectedFlight || !unionRef.current?.related_trip_ids[selectedFlight]?.includes(feature.properties.tripId))
        latestProps.current.onSelect(feature.properties.tripId);
      detailAbort?.abort();const controller=new AbortController();detailAbort=controller;
      try {
        const detail=await api<{title:string;source?:string;point:TrackPoint}>(`/api/map/point/${feature.id}`,{signal:controller.signal});
        if(controller.signal.aborted || disposed)return;
        const {point}=detail;const content=document.createElement('div');content.className='track-popup';
        const title=document.createElement('div');title.className='track-popup-title';title.textContent=detail.title;content.append(title);
        popupRow(content,'经纬度',`${point.longitude.toFixed(6)}, ${point.latitude.toFixed(6)}`);
        popupRow(content,'数据源',detail.source || point.source || '未知');
        popupRow(content,'时间',point.timestamp?point.timestamp.replace('T',' '):'未知');
        popupRow(content,'速度',numericValue(point.speed_kmh,'km/h',1));
        popupRow(content,'高度',numericValue(point.altitude_m,'m'));
        const longitude=point.longitude+Math.round((event.lngLat.lng-point.longitude)/360)*360;
        popup.setLngLat([longitude,point.latitude]).setDOMContent(content).addTo(map);
      }catch{/* Point details must not interrupt map interaction. */}
    });
    map.on('style.load', () => {
      styleReady = true;
      if (styleReadyTimeout) clearTimeout(styleReadyTimeout);
      sync();
      if (remoteStyleActive) setStatus('ready');
    });
    const fallback = () => {
      remoteStyleActive = false;
      styleReady = false;
      map.setStyle(FALLBACK_STYLE, { diff: false });
      setStatus('offline');
    };
    map.on('error', (event) => {
      if (!disposed && remoteStyleActive && !String(event.error?.message || '').includes('footprint-')) {
        setStatus('offline');
        if (!styleReady) queueMicrotask(() => { if (!disposed && remoteStyleActive && !styleReady) fallback(); });
      }
    });
    const loadBasemap = async () => {
      abortController?.abort();
      if (styleTimeout) clearTimeout(styleTimeout);
      if (styleReadyTimeout) clearTimeout(styleReadyTimeout);
      abortController = new AbortController();
      const currentController = abortController;
      setStatus('loading');
      styleTimeout = setTimeout(() => currentController.abort(), 15_000);
      try {
        const style = await loadBasemapStyle(currentController.signal);
        if (disposed || currentController !== abortController) return;
        remoteStyleActive = true;
        styleReady = false;
        // Preserve the already-loaded point source while adding the basemap.
        // A full style reset would parse and upload the million-point tiles twice.
        const current=map.getStyle();
        if(current.sources[POINT_SOURCE]) {
          style.sources[POINT_SOURCE]=current.sources[POINT_SOURCE];
          style.layers.push(...current.layers.filter(layer=>layer.id===POINT_LAYER));
        }
        map.setStyle(style);
        styleReadyTimeout = setTimeout(() => {
          if (!disposed && !styleReady) fallback();
        }, 15_000);
      } catch {
        if (!disposed && currentController === abortController) setStatus('offline');
      } finally {
        if (currentController === abortController && styleTimeout) clearTimeout(styleTimeout);
      }
    };
    retryRef.current = () => { void loadBasemap(); };
    void loadBasemap();
    let previousWidth = map.getContainer().clientWidth;
    let previousHeight = map.getContainer().clientHeight;
    let resizeFrame: number | null = null;
    const resizeObserver = new ResizeObserver(() => {
      if (resizeFrame !== null) return;
      resizeFrame = requestAnimationFrame(() => {
        resizeFrame = null;
        if (disposed) return;
        const width = map.getContainer().clientWidth;
        const height = map.getContainer().clientHeight;
        const changed = width !== previousWidth || height !== previousHeight;
        previousWidth = width;
        previousHeight = height;
        map.resize();
        if (changed && latestProps.current.selectedId && width > 0 && height > 0) {
          fitted = false;
          sync();
        }
      });
    });
    resizeObserver.observe(containerRef.current);
    return () => {
      disposed = true;
      abortController?.abort();
      if (styleTimeout) clearTimeout(styleTimeout);
      if (styleReadyTimeout) clearTimeout(styleReadyTimeout);
      if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
      resizeObserver.disconnect();
      detailAbort?.abort();
      popup.remove();
      syncRef.current = null;
      retryRef.current = null;
      mapRef.current = null;
      map.remove();
    };
  }, []);

  useEffect(() => {
    syncRef.current?.();
  }, [props.selectedTrip, props.selectedId, props.fitNonce, viewId, pointBounds]);

  return (
    <div className="track-map">
      <div ref={containerRef} className="track-map-canvas" />
      {status === 'loading' && <div className="map-status map-status--loading" role="status">正在加载地图底图…</div>}
      {status === 'offline' && (
        <div className="map-status map-status--offline" role="status">
          <span>底图暂时不可用，本地轨迹仍可查看</span>
          <button type="button" onClick={() => retryRef.current?.()}>重试底图</button>
        </div>
      )}
      {status === 'unavailable' && (
        <div className="map-status map-status--unavailable" role="alert">浏览器无法启动 WebGL 地图。轨迹仍已保存，可继续查看列表和导出 CSV。</div>
      )}
      {pointError&&<div className="map-status map-status--offline" role="alert">轨迹点加载失败：{pointError}</div>}
      <div className="map-help">{viewId?(unionInfo?.union_only?`轨迹并集 · 在线 ${unionInfo.online_point_count.toLocaleString('zh-CN')} 点 + 本地 GPS ${unionInfo.gps_point_count.toLocaleString('zh-CN')} 点 · 共 ${pointCount.toLocaleString('zh-CN')} 点`:`全部原始点 · ${pointCount.toLocaleString('zh-CN')} 个 · 不连线`):'正在准备原始轨迹点…'} · 拖动浏览 · 滚轮缩放 · 点击点位查看</div>
    </div>
  );
}

export default memo(TrackMap);
