import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import type { Trip } from '../types';
import { availableDays, buildDailySeries, formatTime, type TimeZoneMode, type TimedPoint } from '../lib/timeSeries';
import './TelemetryCharts.css';

type TimeRange = [number, number];
type RangeMode = 'active' | 'day';
const CHART_HEIGHT = 236;

function responsiveChartHeight(): number {
  if (typeof window === 'undefined' || window.innerWidth <= 650) return CHART_HEIGHT;
  return Math.max(140, Math.min(CHART_HEIGHT, Math.floor((window.innerHeight - 420) / 2)));
}

function closestIndex(points: readonly TimedPoint[], time: number): number {
  let low = 0;
  let high = points.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (points[middle].time < time) low = middle + 1;
    else high = middle;
  }
  if (low === 0) return 0;
  if (low === points.length) return points.length - 1;
  return time - points[low - 1].time <= points[low].time - time ? low - 1 : low;
}

/** Keep each bucket's extrema in time order; never combine separate source segments. */
function reduceSegment(points: readonly TimedPoint[], budget: number): readonly TimedPoint[] {
  if (points.length <= budget || points.length <= 4) return points;
  const bucketSize = Math.ceil((points.length - 2) / Math.max(1, Math.floor((budget - 2) / 4)));
  const result = [points[0]];
  for (let start = 1; start < points.length - 1; start += bucketSize) {
    const end = Math.min(start + bucketSize, points.length - 1);
    let minimum = start;
    let maximum = start;
    for (let index = start + 1; index < end; index += 1) {
      if (points[index].value < points[minimum].value) minimum = index;
      if (points[index].value > points[maximum].value) maximum = index;
    }
    const indices = [...new Set([start, minimum, maximum, end - 1])].sort((a, b) => a - b);
    for (const index of indices) result.push(points[index]);
  }
  result.push(points[points.length - 1]);
  return result;
}

function domainFor(points: readonly TimedPoint[]) {
  let minimum = Infinity;
  let maximum = -Infinity;
  for (const point of points) {
    minimum = Math.min(minimum, point.value);
    maximum = Math.max(maximum, point.value);
  }
  if (!points.length) return { minimum: 0, maximum: 1, ticks: [0, 1], step: 1 };
  const padding = maximum === minimum ? Math.max(1, Math.abs(maximum) * 0.05) : (maximum - minimum) * 0.08;
  const lower = minimum >= 0 ? Math.max(0, minimum - padding) : minimum - padding;
  const upper = maximum + padding;
  const roughStep = (upper - lower) / 4;
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const fraction = roughStep / magnitude;
  const step = (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * magnitude;
  const axisMinimum = Math.floor(lower / step) * step;
  const axisMaximum = Math.ceil(upper / step) * step;
  const ticks: number[] = [];
  for (let index = 0; index <= 8; index += 1) {
    const value = axisMinimum + step * index;
    if (value > axisMaximum + step / 100) break;
    ticks.push(value);
  }
  return { minimum: axisMinimum, maximum: axisMaximum, ticks, step };
}

function displayNumber(value: number, digits = 1): string {
  return value.toLocaleString('zh-CN', { maximumFractionDigits: digits });
}

interface MetricChartProps {
  title: string;
  metric: 'altitude_m' | 'speed_kmh';
  unit: string;
  color: 'cyan' | 'amber';
  segments: TimedPoint[][];
  range: TimeRange;
  dayEnd?: number;
  zone: TimeZoneMode;
  hoverPoint: TimedPoint | null;
  onHover: (point: TimedPoint | null) => void;
  emptyMessage: string;
}

function MetricChart({ title, metric, unit, color, segments, range, dayEnd, zone, hoverPoint, onHover, emptyMessage }: MetricChartProps) {
  const plotRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  const [chartHeight, setChartHeight] = useState(responsiveChartHeight);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const id = useId().replace(/:/g, '');
  const points = useMemo(() => segments.flat().sort((a, b) => a.time - b.time), [segments]);
  const domain = useMemo(() => domainFor(points), [points]);
  const left = width < 420 ? 49 : 62;
  const right = width < 420 ? 14 : 24;
  const top = 16;
  const bottom = 36;
  const plotWidth = Math.max(1, width - left - right);
  const plotHeight = chartHeight - top - bottom;
  const duration = Math.max(1, range[1] - range[0]);
  const showSeconds = duration < 600_000;
  const x = (time: number) => left + (time - range[0]) / duration * plotWidth;
  const y = (value: number) => top + (domain.maximum - value) / Math.max(Number.EPSILON, domain.maximum - domain.minimum) * plotHeight;
  const hoverTime = hoverPoint?.time ?? null;
  const activePoint = activeIndex === null ? null : points[activeIndex];
  const sharedValue = hoverPoint?.point[metric];
  const sharedValueAvailable = typeof sharedValue === 'number' && Number.isFinite(sharedValue)
    && (metric !== 'speed_kmh' || sharedValue >= 0);
  const nearest = hoverTime === null || !points.length ? null
    : sharedValueAvailable && hoverPoint ? { time: hoverTime, value: sharedValue, point: hoverPoint.point }
      : points[closestIndex(points, hoverTime)];
  const nearestFallback = nearest !== null && hoverPoint !== null && nearest.point !== hoverPoint.point;
  const cursorX = hoverTime === null ? null : Math.max(left, Math.min(width - right, x(hoverTime)));
  const paths = useMemo(() => {
    let line = '';
    let area = '';
    let dots = '';
    const budget = Math.max(600, Math.floor(plotWidth * 3));
    const baseline = chartHeight - bottom;
    const coordinate = (point: TimedPoint) => `${(left + (point.time - range[0]) / Math.max(1, range[1] - range[0]) * plotWidth).toFixed(2)},${(top + (domain.maximum - point.value) / Math.max(Number.EPSILON, domain.maximum - domain.minimum) * plotHeight).toFixed(2)}`;
    for (const segment of segments) {
      if (!segment.length) continue;
      if (segment.length === 1) {
        dots += `M${coordinate(segment[0])}l0.001,0 `;
        continue;
      }
      const sampled = reduceSegment(segment, Math.max(4, Math.floor(budget * segment.length / Math.max(1, points.length))));
      const part = `M${sampled.map(coordinate).join('L')}`;
      line += `${part} `;
      const startX = left + (sampled[0].time - range[0]) / duration * plotWidth;
      const endX = left + (sampled[sampled.length - 1].time - range[0]) / duration * plotWidth;
      area += `${part}L${endX.toFixed(2)},${baseline}L${startX.toFixed(2)},${baseline}Z `;
    }
    return { line, area, dots };
  }, [segments, points.length, range, plotWidth, plotHeight, left, domain, duration, chartHeight]);

  useEffect(() => {
    if (!plotRef.current) return;
    const resizeHeight = () => setChartHeight(responsiveChartHeight());
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(240, Math.round(entry.contentRect.width))));
    observer.observe(plotRef.current);
    window.addEventListener('resize', resizeHeight);
    return () => { observer.disconnect(); window.removeEventListener('resize', resizeHeight); };
  }, []);
  useEffect(() => { setActiveIndex(null); }, [points, zone, range]);

  const movePointer = (event: PointerEvent<SVGSVGElement>) => {
    if (!points.length) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const pointerX = (event.clientX - rect.left) * width / Math.max(1, rect.width);
    const fraction = Math.max(0, Math.min(1, (pointerX - left) / plotWidth));
    const index = closestIndex(points, range[0] + fraction * duration);
    setActiveIndex(index);
    onHover(points[index]);
  };
  const moveKeyboard = (event: KeyboardEvent<SVGSVGElement>) => {
    if (!points.length) return;
    let index = hoverTime === null ? 0 : activePoint?.point === hoverPoint?.point && activeIndex !== null
      ? activeIndex : closestIndex(points, hoverTime);
    if (hoverPoint && points[index].point !== hoverPoint.point && points[index].time === hoverTime) {
      for (let candidate = index; candidate < points.length && points[candidate].time === hoverTime; candidate += 1) {
        if (points[candidate].point === hoverPoint.point) { index = candidate; break; }
      }
    }
    if (event.key === 'ArrowLeft') index = Math.max(0, index - 1);
    else if (event.key === 'ArrowRight') index = Math.min(points.length - 1, index + 1);
    else if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = points.length - 1;
    else if (event.key === 'Escape') { if (hoverTime !== null) { event.preventDefault(); onHover(null); } return; }
    else return;
    event.preventDefault();
    setActiveIndex(index);
    onHover(points[index]);
  };
  const tickCount = width >= 560 ? 6 : width >= 390 ? 4 : 3;
  const timeTicks = Array.from({ length: tickCount }, (_, index) => range[0] + duration * index / (tickCount - 1));
  const fractionDigits = domain.step >= 1 ? 0 : Math.min(4, Math.ceil(-Math.log10(domain.step)));
  const description = points.length
    ? `${title}，${points.length} 个有时间的观测点，范围 ${displayNumber(domain.minimum)} 至 ${displayNumber(domain.maximum)} ${unit}。左右方向键选择记录，Home 与 End 跳到首尾，Esc 关闭提示。缺失数据不连线。`
    : `${title}：${emptyMessage}`;

  return (
    <section className={`telemetry-chart telemetry-chart--${color}`} aria-label={`${title}时间图`}>
      <div className="telemetry-chart-heading">
        <div><span className="telemetry-chart-swatch" /><h3>{title}</h3><span className="telemetry-unit">{unit}</span></div>
        <span className="telemetry-point-count">{points.length ? `${displayNumber(points.length, 0)} 个观测点` : '无观测数据'}</span>
      </div>
      <div ref={plotRef} className="telemetry-plot">
        {!points.length ? (
          <div className="telemetry-chart-empty"><span aria-hidden="true">—</span><p>{emptyMessage}</p><small>缺测值不会作为 0 绘制。</small></div>
        ) : (
          <>
            <svg className="telemetry-svg" style={{ height: chartHeight }} viewBox={`0 0 ${width} ${chartHeight}`} role="img" tabIndex={0}
              aria-labelledby={`${id}-title ${id}-description`} onPointerMove={movePointer} onPointerDown={movePointer}
              onPointerLeave={event => { if (event.pointerType !== 'touch') onHover(null); }}
              onKeyDown={moveKeyboard} onFocus={() => { if (hoverTime === null) { setActiveIndex(0); onHover(points[0]); } }} onBlur={() => onHover(null)}>
              <title id={`${id}-title`}>{title}随时间变化</title>
              <desc id={`${id}-description`}>{description}</desc>
              <defs><linearGradient id={`${id}-gradient`} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="currentColor" stopOpacity="0.18" /><stop offset="100%" stopColor="currentColor" stopOpacity="0.015" /></linearGradient><clipPath id={`${id}-clip`}><rect x={left - 4} y={top - 4} width={plotWidth + 8} height={plotHeight + 8} /></clipPath></defs>
              {domain.ticks.map(tick => <g key={tick}><line className="telemetry-grid" x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} /><text className="telemetry-axis-label" x={left - 10} y={y(tick)} textAnchor="end" dominantBaseline="middle">{displayNumber(tick, fractionDigits)}</text></g>)}
              {timeTicks.map((tick, index) => <g key={index}><line className="telemetry-grid telemetry-grid--vertical" x1={x(tick)} x2={x(tick)} y1={top} y2={chartHeight - bottom} /><text className="telemetry-axis-label telemetry-axis-time" x={x(tick)} y={chartHeight - 12} textAnchor={index === 0 ? 'start' : index === tickCount - 1 ? 'end' : 'middle'}>{index === tickCount - 1 && range[1] === dayEnd ? '24:00' : formatTime(tick, zone, showSeconds)}</text></g>)}
              <g clipPath={`url(#${id}-clip)`}>
                <path d={paths.area} fill={`url(#${id}-gradient)`} />
                <path className="telemetry-line" d={paths.line} />
                <path className="telemetry-single-points" d={paths.dots} />
                {cursorX !== null && <line className="telemetry-crosshair" x1={cursorX} x2={cursorX} y1={top} y2={chartHeight - bottom} />}
                {nearest && <circle className="telemetry-active-point" cx={x(nearest.time)} cy={y(nearest.value)} r={4.5} />}
              </g>
            </svg>
            {nearest && <div className={`telemetry-tooltip ${cursorX !== null && cursorX > width * 0.58 ? 'telemetry-tooltip--left' : ''}`} role="status"><span>最近观测 · {formatTime(nearest.time, zone, true)}</span><strong>{displayNumber(nearest.value)} <small>{unit}</small></strong>{nearestFallback && <small className="telemetry-tooltip-note">此点缺少{title}，显示最近记录</small>}</div>}
          </>
        )}
      </div>
    </section>
  );
}

export default function TelemetryCharts({ trip }: { trip: Trip }) {
  const [zone, setZone] = useState<TimeZoneMode>('local');
  const [chosenDay, setChosenDay] = useState(trip.travel_date);
  const [rangeMode, setRangeMode] = useState<RangeMode>('active');
  const [hoverPoint, setHoverPoint] = useState<TimedPoint | null>(null);
  const days = useMemo(() => [...availableDays(trip.points, zone)].sort(), [trip.points, zone]);
  const preferredDay = days.includes(trip.travel_date) ? trip.travel_date : days.at(-1) || '';
  const day = days.includes(chosenDay) ? chosenDay : preferredDay;
  const series = useMemo(() => day ? buildDailySeries(trip.points, day, zone) : null, [trip.points, day, zone]);
  const range = useMemo<TimeRange>(() => {
    if (!series) return [0, 1];
    if (rangeMode === 'day') return [series.start, series.end];
    let first = Infinity;
    let last = -Infinity;
    for (const segments of [series.altitude, series.speed]) {
      for (const segment of segments) {
        for (const point of segment) {
          first = Math.min(first, point.time);
          last = Math.max(last, point.time);
        }
      }
    }
    if (!Number.isFinite(first)) return [series.start, series.end];
    const margin = last === first ? 30_000 : Math.max(1_000, (last - first) * 0.04);
    return [Math.max(series.start, first - margin), Math.min(series.end, last + margin)];
  }, [series, rangeMode]);
  useEffect(() => { setHoverPoint(null); }, [trip.id, day, zone, rangeMode]);
  const localZone = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone || '本机时区', []);
  const timezoneLabel = zone === 'utc' ? 'UTC' : `本机时间 · ${localZone}`;
  const barometric = trip.metadata?.altitude_kind === 'barometric_pressure';
  const emptyMessage = (metric: string) => {
    if (!days.length) return `没有带有效时间的${metric}记录，无法生成时间曲线。`;
    return `所选日期没有同时包含时间和${metric}的记录。`;
  };

  return (
    <div className="telemetry-charts">
      <div className="telemetry-controls">
        <label>观测日期<select value={day} disabled={!days.length} onChange={event => setChosenDay(event.target.value)} aria-label="图表观测日期">{days.length ? days.map(value => <option key={value} value={value}>{value}</option>) : <option value="">无时间数据</option>}</select></label>
        <label>时区<select value={zone} onChange={event => setZone(event.target.value as TimeZoneMode)} aria-label="图表时间显示时区"><option value="local">本机时间</option><option value="utc">UTC</option></select></label>
        <div className="telemetry-range-control"><span>横轴范围</span><div role="group" aria-label="图表时间轴范围"><button type="button" className={rangeMode === 'active' ? 'active' : ''} aria-pressed={rangeMode === 'active'} disabled={!series} onClick={() => setRangeMode('active')}>有效时段</button><button type="button" className={rangeMode === 'day' ? 'active' : ''} aria-pressed={rangeMode === 'day'} disabled={!series} onClick={() => setRangeMode('day')}>全天</button></div></div>
      </div>
      <div className="telemetry-date-line"><strong>{day || '无可用观测日期'}</strong><span>{timezoneLabel}</span>{series && <small>{rangeMode === 'day' ? '全天时间轴' : '有效观测时段'} · {formatTime(range[0], zone, rangeMode === 'active')}–{range[1] === series.end ? '次日' : ''}{formatTime(range[1], zone, rangeMode === 'active')}</small>}</div>
      {!days.length && <p className="telemetry-data-note">这条轨迹没有有效时间戳。铁路重建路线通常不包含逐点时间、高度或实测速度。</p>}
      {series && series.altitudePointCount === 0 && series.speedPointCount === 0 && <p className="telemetry-data-note">当天有 {displayNumber(series.timedPointCount, 0)} 个时间点，但未提供对应的高度和速度。</p>}
      {barometric && <p className="telemetry-data-note telemetry-data-note--altitude">高度来源：气压高度，已换算为米。</p>}
      <MetricChart title={barometric ? '气压高度' : '高度'} metric="altitude_m" unit="m" color="cyan" segments={series?.altitude || []} range={range} dayEnd={series?.end} zone={zone} hoverPoint={hoverPoint} onHover={setHoverPoint} emptyMessage={emptyMessage('高度')} />
      <MetricChart title="速度" metric="speed_kmh" unit="km/h" color="amber" segments={series?.speed || []} range={range} dayEnd={series?.end} zone={zone} hoverPoint={hoverPoint} onHover={setHoverPoint} emptyMessage={emptyMessage('速度')} />
      <p className="telemetry-footnote">两图共用时间轴。悬停或轻触查看最近观测；聚焦图表后可用左右方向键查值。不同区段、缺失值或超过 15 分钟的记录间隔会断线，提示只显示实际观测点。</p>
    </div>
  );
}
