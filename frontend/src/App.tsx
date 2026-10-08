import { lazy, Suspense, useDeferredValue, useEffect, useId, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { ArrowUpRight, CalendarDays, ChartLine, Check, ChevronDown, ChevronLeft, ChevronRight, Download, FileUp, Footprints, Info, Layers3, List, LoaderCircle, MapPin, Plane, PlaneTakeoff, Plus, Route, Search, Settings2, SlidersHorizontal, TrainFront, Trash2, X } from 'lucide-react';
import { api, ApiError, postJson } from './lib/api';
import { cumulativeDistance } from './lib/tripTotals';
import { formatTripDistance, tripDistance } from './lib/tripDistance';
import { formatAirportTime } from './lib/flightTime';
import { postFlightLookup } from './lib/flightLookup';
import { compareTripDates } from './lib/tripOrder';
import type { FlightCandidate, FlightInfo, GpsOverlay, Mode, ReconstructCandidate, LocalFlightCandidate, Trip, TripDraft, TrackPoint, ViaSegment } from './types';

const TrackMap = lazy(() => import('./components/TrackMap'));
const TelemetryCharts = lazy(() => import('./components/TelemetryCharts'));
const RailCoverage = lazy(() => import('./components/RailCoverage'));
const RailViaEditor = lazy(() => import('./components/RailViaEditor'));
const PreviewMap = lazy(() => import('./components/PreviewMap'));

const modeName: Record<Mode, string> = { flight: '飞机', rail: '铁路', other: '其他' };
const geometryName = { observed: '位置记录', reconstructed: '沿铁路重建', imported: 'CSV 轨迹', scheduled: '无轨迹' };
const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
const number = (n: number, digits = 0) => n.toLocaleString('zh-CN', { maximumFractionDigits: digits });
const isSandboxTrip = (trip: Trip) => trip.metadata?.sandbox === true || trip.quality_flags?.includes('sandbox_sample') === true || trip.source === 'Flightradar24 Sandbox';
// Reconstruction chains record their explanations under metadata.notes (list);
// the top-level notes only carries the CSV-imported remark.
const tripNotes = (trip: Trip) => [trip.notes, ...((trip.metadata?.notes as string[] | undefined) ?? [])].filter(Boolean).join(' ');
type ImportTab = 'flight' | 'rail' | 'csv';
const ModeIcon = ({ mode, size = 19 }: { mode: Mode; size?: number }) => mode === 'flight' ? <Plane size={size} /> : mode === 'rail' ? <TrainFront size={size} /> : <Route size={size} />;

function Modal({ title, subtitle, onClose, children, wide = false, busy = false }: { title: string; subtitle?: string; onClose: () => void; children: ReactNode; wide?: boolean; busy?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const subtitleId = useId();
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    const original = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = original; if (dialog.open) dialog.close(); };
  }, []);
  return <dialog ref={ref} aria-labelledby={titleId} aria-describedby={subtitle ? subtitleId : undefined} className={`modal ${wide ? 'modal-wide' : ''}`} onCancel={e => { e.preventDefault(); if (!busy) onClose(); }} onClick={e => { if (e.target === ref.current && !busy) onClose(); }}>
    <div className="modal-head"><div><h2 id={titleId}>{title}</h2>{subtitle && <p id={subtitleId}>{subtitle}</p>}</div><button className="icon-button" aria-label="关闭" onClick={onClose} disabled={busy}><X size={21} /></button></div>
    {children}
  </dialog>;
}

function ErrorNotice({ error }: { error: ApiError | Error | null }) {
  if (!error) return null;
  const details = error instanceof ApiError ? error.details : null;
  const errors = details && typeof details === 'object' && 'errors' in details ? (details as { errors: unknown }).errors : null;
  const describeRow = (row: unknown) => {
    if (typeof row === 'string') return row;
    if (row && typeof row === 'object' && 'row' in row && 'message' in row) return `第 ${String(row.row)} 行：${String(row.message)}`;
    return JSON.stringify(row);
  };
  return <div className="error-notice" role="alert"><Info size={18} /><div>{error.message}{Array.isArray(errors) && <ul>{errors.slice(0, 5).map((row, i) => <li key={i}>{describeRow(row)}</li>)}</ul>}</div></div>;
}

function AircraftPhoto({ info }: { info: FlightInfo }) {
  const [source, setSource] = useState(info.photo_url);
  if (!source) return <p className="field-help">照片暂不可用</p>;
  return <div><img className="flight-photo" src={source} alt="飞机照片" loading="lazy"
    onError={() => setSource(source !== info.photo_thumbnail && info.photo_thumbnail ? info.photo_thumbnail : null)} />
    {(info.photo_photographer || info.photo_link) && <small className="photo-credit">© {info.photo_photographer || 'planespotters'}{info.photo_link && <> · <a href={info.photo_link} target="_blank" rel="noreferrer">planespotters.net</a></>}</small>}
  </div>;
}

function AircraftLookupResult({ info }: { info?: FlightInfo }) {
  if (!info) return null;
  return <section className="flight-info" aria-label="航班及飞机资料">
    <strong>航班及飞机资料 · 尚未保存</strong>
    <div className="flight-info-grid">
      {flightRow('航班号', info.flight_number)}
      {flightRow('出发日期', info.travel_date)}
      {flightRow('呼号', info.callsign)}
      {flightRow('航空公司', info.airline_name || info.operating_as)}
      {flightRow('出发机场', info.departure_airport)}
      {flightRow('到达机场', info.arrival_airport)}
      {flightRow('起降机场依据', info.identity_evidence?.airport_evidence?.source === 'local-gps' ? '本地 GPS · 已匹配当日 ADS-B 观测' : null)}
      {flightRow(info.times_inferred ? '观测起点（出发地当地）' : '起飞时间（当地）', formatAirportTime(info.takeoff_time, info.departure_timezone))}
      {flightRow(info.times_inferred ? '观测终点（到达地当地）' : '落地时间（当地）', formatAirportTime(info.landing_time, info.arrival_timezone))}
      {flightRow('注册号', info.registration)}
      {flightRow('机型', info.aircraft_type)}
      {flightRow('型别', info.icao_type)}
      {flightRow('hex / ICAO24', info.icao24)}
      {flightRow('运营人', info.registered_owner)}
      {flightRow('当日飞机资料来源', info.identity_source === 'mrairspace' ? 'MrAirspace · ADS-B 历史观测' : null)}
    </div>
    {info.identity_evidence && <p className="field-help">{info.identity_evidence.airport_evidence?.source === 'local-gps' ? '飞机身份来自当日 ADS-B 记录，起降机场由匹配的本地 GPS 确认。' : '机场由实际观测端点推断，缺失时留空。'}观测起终点时间不等于实际起降时间。<a href={info.identity_evidence.source_url} target="_blank" rel="noreferrer">MrAirspace · ODbL-1.0</a></p>}
    {info.identity_lookup_note && <p className="field-help">{info.identity_lookup_note}</p>}
    {info.photo_url && <AircraftPhoto key={info.photo_url} info={info} />}
  </section>;
}

function ImportModal({ initialTab, onClose, onImported, configured, existingTrip }: { existingTrip?: Trip; initialTab: ImportTab; onClose: () => void; onImported: (id?: string, hint?: string) => Promise<void>; configured: boolean }) {
  const [tab, setTab] = useState(initialTab);
  const [lookupProgress, setLookupProgress] = useState('');
  const lookupController = useRef<AbortController | null>(null);
  useEffect(() => () => lookupController.current?.abort(), []);
  const close = () => { lookupController.current?.abort(); onClose(); };
  const [railMode, setRailMode] = useState<'train' | 'via'>('train');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [headers, setHeaders] = useState<string[]>([]);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [candidates, setCandidates] = useState<FlightCandidate[]>([]);
  const [reconstructCandidates, setReconstructCandidates] = useState<ReconstructCandidate[]>([]);
  const [localCandidates, setLocalCandidates] = useState<LocalFlightCandidate[]>([]);
  const [pending, setPending] = useState<Record<string, unknown> | null>(null);
  const [preview, setPreview] = useState<{ trip: TripDraft; segments?: ViaSegment[]; gpsPoints?: TrackPoint[] } | null>(null);

  async function chooseFile(selected: File | null) {
    setFile(selected); setHeaders([]); setMapping({}); setError(null);
    if (!selected) return;
    if (selected.size > 512 * 1024 * 1024) { setError(new Error('文件过大，一生足迹备份最大支持 512 MB。')); setFile(null); return; }
    const firstLine = (await selected.slice(0, 16000).text()).replace(/^\uFEFF/, '').split(/\r?\n/)[0];
    const separator = firstLine.includes('\t') ? '\t' : firstLine.includes(';') && !firstLine.includes(',') ? ';' : ',';
    const values: string[] = []; let cell = ''; let quoted = false;
    for (let i = 0; i < firstLine.length; i++) { const ch = firstLine[i]; if (ch === '"') { if (quoted && firstLine[i + 1] === '"') { cell += '"'; i++; } else quoted = !quoted; } else if (ch === separator && !quoted) { values.push(cell.trim()); cell = ''; } else cell += ch; }
    values.push(cell.trim()); setHeaders(values.filter(Boolean));
  }

  // Flight import: FR24 first, then the free reconstruction chain. A 409 means
  // the day has multiple candidates: FR24 multi-segment days carry fr24_id,
  // OpenSky ambiguities carry icao24 + first_seen.
  async function previewFlight(body: Record<string, unknown>) {
    setBusy(true); setError(null); setLookupProgress(''); setCandidates([]); setReconstructCandidates([]); setLocalCandidates([]); setPending(body);
    const controller = new AbortController(); lookupController.current = controller;
    try {
      const result = await postFlightLookup<{ trip: TripDraft }>('/api/preview/flight', body, setLookupProgress, controller.signal);
      const overlay = (result.trip.metadata as { gps_overlay?: GpsOverlay } | undefined)?.gps_overlay;
      const gpsPoints = overlay ? (await Promise.all(overlay.sources.map(async source => {
        const record = await api<Trip>(`/api/trips/${encodeURIComponent(source.trip_id)}`);
        return source.ranges.flatMap(([start, end], range) => record.points.slice(start, end + 1).map((p, index) => ({
          ...p, sequence: start + index, segment_id: `gps:${source.trip_id}:${range}:${p.segment_id}`,
        })));
      }))).flat().map((p, sequence) => ({ ...p, sequence })) : [];
      setPreview({ trip: result.trip, gpsPoints });
    } catch (err) {
      if (controller.signal.aborted) return;
      const e = err as ApiError;
      const details = e.details as { candidates?: unknown[] } | null;
      if (e.status === 409 && details?.candidates?.length) {
        const first = details.candidates[0] as Record<string, unknown>;
        if ('local_candidate_id' in first) { setLocalCandidates(details.candidates as LocalFlightCandidate[]); }
        else if ('fr24_id' in first) { setCandidates(details.candidates as FlightCandidate[]); setPending(body); }
        else { setReconstructCandidates(details.candidates as ReconstructCandidate[]); setPending(body); }
      } else setError(e);
    } finally { setBusy(false); setLookupProgress(''); }
  }

  async function previewRail(body: Record<string, unknown>) {
    setBusy(true); setError(null);
    try {
      const result = await postJson<{ trip: TripDraft }>('/api/preview/rail', body);
      setPreview({ trip: result.trip });
    } catch (err) { setError(err as Error); } finally { setBusy(false); }
  }

  async function commit() {
    if (!preview) return;
    setBusy(true); setError(null);
    try {
      const recovery = (preview.trip.metadata as { local_recovery?: { candidate_id?: string } } | undefined)?.local_recovery;
      const update = (preview.trip.metadata as { online_update?: { trip_id: string } } | undefined)?.online_update;
      const information = (preview.trip.metadata as { information_only?: boolean; information_update?: { trip_id: string } } | undefined);
      const result = information?.information_update
        ? await postJson<Trip>(`/api/flight/${encodeURIComponent(information.information_update.trip_id)}/information`, preview.trip)
        : update
        ? await postJson<Trip>(`/api/flight/${encodeURIComponent(update.trip_id)}/track`, preview.trip)
        : existingTrip
        ? await postJson<Trip>(`/api/flight/${encodeURIComponent(existingTrip.id)}/recover`, { ...pending, local_candidate_id: recovery?.candidate_id })
        : await postJson<Trip>('/api/trips', preview.trip);
      const flags = preview.trip.quality_flags as string[] | undefined;
      const hint = information?.information_only ? '航班及飞机资料已保存' : flags?.includes('historical_date_reconstruction') ? '已根据今日该车次经由导入轨迹' : undefined;
      await onImported(result.id, hint);
      onClose();
    } catch (err) { setError(err as Error); setBusy(false); }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(null);
    const values = new FormData(event.currentTarget);
    if (tab === 'flight') {
      await previewFlight({ flight_number: String(values.get('flight_number')).trim().toUpperCase(), travel_date: values.get('travel_date'), origin: String(values.get('origin') || '').trim().toUpperCase() || null, destination: String(values.get('destination') || '').trim().toUpperCase() || null, registration: String(values.get('registration') || '').trim().toUpperCase() || null, icao24: String(values.get('icao24') || '').trim().toUpperCase() || null, departure_time: values.get('departure_time') || null, track_source: values.get('track_source') || 'auto', existing_trip_id: existingTrip?.id || null }); return;
    }
    if (tab === 'rail' && railMode === 'train') {
      await previewRail({ train_number: String(values.get('train_number')).trim().toUpperCase(), travel_date: values.get('travel_date'), origin: String(values.get('origin')).trim(), destination: String(values.get('destination')).trim() }); return;
    }
    setBusy(true);
    try {
      if (!file) throw new Error('请先选择轨迹 CSV 文件。');
      if (!String(values.get('title') || '').trim()) values.set('title', file.name.replace(/\.[^.]+$/, '') || 'CSV 导入');
      values.set('file', file); values.set('column_mapping', JSON.stringify(mapping));
      const result = await api<{ trips: Trip[]; count: number; point_count: number }>('/api/import/csv', { method: 'POST', body: values });
      await onImported(result.trips[0]?.id);
      onClose();
    } catch (err) { setError(err as Error); } finally { setBusy(false); }
  }

  const viaMode = tab === 'rail' && railMode === 'via';

  const trip = preview?.trip;
  const overlay = (trip?.metadata as { gps_overlay?: GpsOverlay; online_update?: unknown; information_only?: boolean; information_update?: unknown; flight?: FlightInfo } | undefined);
  // Keep the form and via editor mounted while previewing, including their
  // uncontrolled inputs, file selection and local station/time state.
  return <Modal wide={!!preview} title={preview ? (overlay?.information_only ? '保存前预览航班资料' : '保存前预览轨迹') : existingTrip ? '恢复航班轨迹' : '添加一段足迹'} subtitle={preview ? String(trip?.title || '') : existingTrip ? '匹配已有个人 GPS，预览后补全这条航班。' : '从航班、列车或已有轨迹开始。'} onClose={close} busy={busy && !lookupProgress}>
    {preview && trip && <>
      {overlay?.information_only ? <><p className="field-help">轨迹无数据，仅保留航班、飞机资料和照片。</p><AircraftLookupResult info={overlay.flight} /></> : <Suspense fallback={<p className="field-help">正在准备预览地图…</p>}><PreviewMap points={trip.points} gpsPoints={preview.gpsPoints} mode={String(trip.mode || 'rail')} /></Suspense>}
      {!!overlay?.information_update && <p className="field-help">确认后更新已有航班的资料，保留原记录和已有轨迹，不新增重复航班。</p>}
      {overlay?.gps_overlay && <p className="field-help">{String(trip.source)} {number(trip.points.length)} 点 + 本地 GPS {number(overlay.gps_overlay.point_count)} 点。保存在线点，保留原始 GPS，地图显示两份数据的并集。</p>}
      {!!overlay?.online_update && <p className="field-help">确认后更新已有航班的在线轨迹，保留航班 ID 和原始 GPS，不新增重复航班。</p>}
      {preview.segments && <div className="via-summary"><p className="via-summary-total">全程约 {number(preview.segments.reduce((s, x) => s + x.distance_km, 0), 1)} km · {preview.segments.length} 段经由</p><ol>{preview.segments.map(s => <li key={s.index}><strong>{s.origin_name} → {s.via_name}</strong><span>{s.line_name} · {number(s.distance_km, 1)} km · 最高 {number(s.max_speed_kmh)} km/h{s.speed_source === 'estimate' ? '（估算）' : ''}</span><span className="via-summary-time">{s.departure.time}{s.departure.day_offset ? `(+${s.departure.day_offset}天)` : ''} → {s.arrival.time}{s.arrival.day_offset ? `(+${s.arrival.day_offset}天)` : ''}</span></li>)}</ol></div>}
      <p className="field-help">{((trip.metadata as { notes?: string[] } | undefined)?.notes || []).join(' ')}</p>
      <p className="field-help">{overlay?.information_only ? '确认资料无误后保存到“我的旅程”；返回可继续修改。' : '地图仅显示这段轨迹的范围。确认无误后保存到本机；返回可继续修改。'}</p>
      <ErrorNotice error={error} />
      <div className="modal-actions"><button type="button" className="button-secondary" onClick={() => { setPreview(null); setError(null); }} disabled={busy}>返回修改</button><button type="button" className="button-primary" onClick={() => void commit()} disabled={busy}>{busy ? <LoaderCircle className="spin" size={18} /> : <Check size={18} />}{busy ? '正在保存…' : overlay?.information_only ? '确认保存资料' : existingTrip ? '补全这条航班' : '确认并保存'}</button></div>
    </>}
    <div hidden={!!preview}>
    {!existingTrip && <div className="import-tabs" role="tablist" aria-label="导入方式">{([['flight', '航班', Plane], ['rail', '铁路', TrainFront], ['csv', '轨迹 CSV', FileUp]] as const).map(([key, label, Icon]) => <button role="tab" aria-selected={tab === key} className={tab === key ? 'active' : ''} key={key} disabled={busy} onClick={() => { setTab(key); setError(null); setCandidates([]); setReconstructCandidates([]); setLocalCandidates([]); }}><Icon size={18} />{label}</button>)}</div>}
    <form onSubmit={submit} className="import-form">
      {tab === 'flight' && <>
        <div className="inline-note"><Info size={17} /><span>{existingTrip ? '从原始 GPS 匹配这趟航班，保留飞机资料。没有合适的本地轨迹时会明确提示。' : '可查询范围内优先 FR24，超出范围直接查询 ADS-B，最后匹配本地 GPS。只需航班号和出发日期，自动查询当日机场、注册号和 hex；轨迹无数据时仍可保存飞机资料和照片。'}</span></div>
        <label>轨迹来源<select name="track_source" defaultValue={existingTrip ? 'local' : 'auto'} disabled={!!existingTrip}><option value="auto">自动：FR24 → ADS-B → 本地 GPS</option><option value="local">仅从已有 GPS 恢复</option></select></label>
        <div className="field-row"><label>航班号<input name="flight_number" defaultValue={existingTrip?.service_number || ''} placeholder="例如 CA1501" required maxLength={15} readOnly={!!existingTrip} autoComplete="off" /></label><label>出发日期<input type="date" name="travel_date" defaultValue={existingTrip?.travel_date || today()} max={today()} readOnly={!!existingTrip} required /></label></div>
        <div className="field-row"><label>出发机场 <span>选填 · 自动查询</span><input name="origin" defaultValue={existingTrip?.origin || ''} placeholder="IATA / ICAO，例如 PEK" maxLength={4} required={!!existingTrip} /></label><label>到达机场 <span>选填 · 自动查询</span><input name="destination" defaultValue={existingTrip?.destination || ''} placeholder="例如 SHA" maxLength={4} required={!!existingTrip} /></label></div>
        <div className="field-row"><label>飞机注册号 <span>选填</span><input name="registration" readOnly={!!existingTrip} defaultValue={(existingTrip?.metadata?.flight as FlightInfo | undefined)?.registration || ''} placeholder="例如 B-6130、JA808Z" maxLength={20} autoComplete="off" /></label><label>hex / ICAO24 <span>选填</span><input name="icao24" disabled={!!existingTrip} placeholder="六位十六进制" pattern="[0-9A-Fa-f]{6}" maxLength={6} /></label></div>
        <label>起飞时间 <span>选填 · 出发机场当地时间</span><input name="departure_time" type="time" /></label>
        <p className="field-help">自动查询无需填写机场、注册号、hex 或起飞时间。已填两端机场及注册号或 hex 时无需下载历史索引。缺少这些资料时按航班号与日期查询；历史索引从 2024 年起按季度发布，首次使用需下载缓存，可随时终止并退出。尚未发布或接收不全的航班可能无数据。仅从本地 GPS 恢复时需要两端机场。GPS 轨迹需覆盖两端机场和飞行时段；多个匹配会列出候选。无轨迹但有航班或飞机资料时，可预览并仅保存资料，轨迹留空。</p>
      </>}
      {tab === 'rail' && <div className="rail-mode-toggle" role="group" aria-label="铁路添加方式"><button type="button" className={railMode === 'train' ? 'active' : ''} disabled={busy} onClick={() => setRailMode('train')}><TrainFront size={16} />根据车次</button><button type="button" className={railMode === 'via' ? 'active' : ''} disabled={busy} onClick={() => setRailMode('via')}><Route size={16} />经由添加</button></div>}
      {tab === 'rail' && railMode === 'train' && <><div className="inline-note"><Info size={17} /><span>沿铁路重建乘车路径。历史日期使用当前经由并标注；缺少曲线时按停站顺序匹配本地铁路目录。</span></div><div className="field-row"><label>车次<input name="train_number" placeholder="例如 G2082/3、D2281、Z35" required maxLength={20} autoComplete="off" /></label><label>上车日期<input type="date" name="travel_date" defaultValue={today()} max={today()} required /></label></div><div className="field-row"><label>上车站<input name="origin" placeholder="例如 北京南" required maxLength={80} /></label><label>下车站<input name="destination" placeholder="例如 上海虹桥" required maxLength={80} /></label></div><p className="field-help">轨迹按你的乘车区间截取。线路曲线没有逐点实测速度。</p></>}
      {viaMode && <Suspense fallback={<p className="field-help">正在准备经由编辑器…</p>}><RailViaEditor onError={() => {}} onPreview={result => { setError(null); setPreview({ trip: result.trip, segments: result.segments }); }} /></Suspense>}
      {tab === 'csv' && <><label className={`file-drop ${file ? 'has-file' : ''}`}><FileUp size={30} /><strong>{file ? file.name : '选择轨迹 CSV'}</strong><span>{file ? `${number(file.size / 1024, 1)} KB · ${headers.length} 列` : '支持一生足迹完整备份 · 最多 512 MB'}</span><input type="file" accept=".csv,.tsv,.txt,text/csv" aria-label="选择轨迹 CSV 文件" onChange={e => void chooseFile(e.target.files?.[0] ?? null)} /></label><div className="field-row"><label>轨迹名称 <span>选填</span><input name="title" placeholder={file?.name.replace(/\.[^.]+$/, '') || '给这段旅程取个名字'} maxLength={160} /></label><label>出行日期<input type="date" name="travel_date" defaultValue={today()} required /></label></div><div className="field-row"><label>类型<select name="mode" defaultValue="other"><option value="other">其他轨迹</option><option value="rail">铁路</option><option value="flight">飞机</option></select></label><label>文件坐标系<select name="source_crs" defaultValue="WGS84"><option value="WGS84">WGS84 · GPS / FR24</option><option value="GCJ02">GCJ-02 · 高德 / RailGo</option></select></label></div>{headers.includes('dataTime') && <div className="inline-note"><Info size={17} /><span>已识别一生足迹 11 列格式。按北京时间自动分日；speed 使用米/秒，海拔使用米。完整保留原始字段。</span></div>}{headers.length > 0 && !headers.includes('dataTime') && <details className="column-mapping"><summary>自定义列匹配 <span>常用列名会自动识别</span></summary><div className="mapping-grid">{[['longitude', '经度'], ['latitude', '纬度'], ['timestamp', '时间'], ['speed_kmh', '速度 km/h'], ['altitude_m', '高度 m'], ['sequence', '点序号'], ['trip_id', '轨迹分组']].map(([key, label]) => <label key={key}>{label}<select value={mapping[key] || ''} onChange={e => setMapping(old => { const next = { ...old }; if (e.target.value) next[key] = e.target.value; else delete next[key]; return next; })}><option value="">自动识别</option>{headers.map(header => <option key={header} value={header}>{header}</option>)}</select></label>)}</div></details>}<p className="field-help">默认使用一生足迹 11 列格式：dataTime 为 Unix 秒，speed 为米/秒，altitude 为米。通用点列仍可导入（最多 100 MB）；无时间时使用所选日期。<a href="/api/template.csv" download>下载 CSV 模板</a></p></>}
      <ErrorNotice error={error} />
      {busy && lookupProgress && <div><p className="field-help" role="status">{lookupProgress}</p><button type="button" className="button-secondary" onClick={close}>{lookupProgress.startsWith('首次准备') ? '终止下载并退出' : '取消查询并退出'}</button></div>}
      {tab === 'flight' && error instanceof ApiError && <AircraftLookupResult info={(error.details as { flight_info?: FlightInfo; aircraft_info?: FlightInfo } | null)?.flight_info || (error.details as { aircraft_info?: FlightInfo } | null)?.aircraft_info} />}
      {candidates.length > 0 && <div className="candidate-list"><p>找到多个航段，请选择本次乘坐的航班：</p>{candidates.map(candidate => <button type="button" key={candidate.fr24_id} onClick={() => void previewFlight({ ...pending, fr24_id: candidate.fr24_id })} disabled={busy}><Plane size={18} /><span><strong>{candidate.flight || ''} {candidate.origin} → {candidate.destination}</strong><small>{candidate.departure?.replace('T', ' ').replace('Z', '') || '时间未提供'} UTC</small></span><ChevronRight size={18} /></button>)}</div>}
      {reconstructCandidates.length > 0 && <div className="candidate-list"><p>{reconstructCandidates.some(candidate => candidate.date_confirmed === false) ? '以下包含未确认出发日期的观测片段，请核对观测日期；它不等于起飞日期：' : '该日期找到多个航班，请选择本次乘坐的航班：'}</p>{reconstructCandidates.map(candidate => <button type="button" key={`${candidate.icao24}-${candidate.first_seen}`} onClick={() => void previewFlight({ ...pending, ...(candidate.schedule_candidate_id ? { schedule_candidate_id: candidate.schedule_candidate_id } : { icao24: candidate.icao24, first_seen: candidate.first_seen }) })} disabled={busy}><Plane size={18} /><span><strong>{candidate.callsign || candidate.icao24.toUpperCase()} {candidate.est_departure_airport || '—'} → {candidate.est_arrival_airport || '—'}</strong><small>{candidate.schedule_candidate_id ? '观测起点：' : ''}{candidate.schedule_candidate_id && candidate.first_seen != null ? formatAirportTime(new Date(candidate.first_seen * 1000).toISOString(), candidate.observation_timezone) : candidate.departure_local || '时间未提供'}{candidate.registration ? ` · ${candidate.registration}` : ''}{candidate.date_confirmed === false ? ' · 观测不完整，请核对日期' : ''}</small></span><ChevronRight size={18} /></button>)}</div>}
      {localCandidates.length > 0 && <div className="candidate-list"><p>找到多个本地航段，请选择：</p>{localCandidates.map(candidate => <button type="button" key={candidate.local_candidate_id} disabled={busy} onClick={() => void previewFlight({ ...pending, local_candidate_id: candidate.local_candidate_id })}><Plane size={18} /><span><strong>{candidate.origin} → {candidate.destination}</strong><small>{candidate.departure_local.replace('T', ' ')} · {candidate.point_count} 个点</small></span><ChevronRight size={16} /></button>)}</div>}
      {!viaMode && <div className="modal-actions"><button type="button" className="button-secondary" onClick={onClose} disabled={busy}>取消</button><button className="button-primary" type="submit" disabled={busy || (tab === 'csv' && !file)}>{busy ? <LoaderCircle className="spin" size={18} /> : tab === 'csv' ? <FileUp size={18} /> : <Search size={18} />}{busy ? '正在获取轨迹…' : tab === 'csv' ? '导入并显示' : '查找并预览'}</button></div>}
    </form>
    </div>
  </Modal>;
}

function PointsModal({ trip, onClose }: { trip: Trip; onClose: () => void }) {
  const [page, setPage] = useState(0);
  const pages = Math.ceil(trip.points.length / 100);
  return <Modal wide title="轨迹点" subtitle={`${trip.title} · ${number(trip.point_count)} 个点 · WGS84`} onClose={onClose}><div className="table-scroll"><table><thead><tr><th>序号</th><th>经度</th><th>纬度</th><th>时间</th><th>速度 km/h</th><th>高度 m</th></tr></thead><tbody>{trip.points.slice(page * 100, page * 100 + 100).map((p, i) => <tr key={`${p.sequence}-${i}`}><td>{p.sequence}</td><td>{p.longitude.toFixed(6)}</td><td>{p.latitude.toFixed(6)}</td><td>{p.timestamp ? new Date(p.timestamp).toLocaleString('zh-CN', { timeZoneName: 'short' }) : '—'}</td><td>{p.speed_kmh == null ? '—' : number(p.speed_kmh, 1)}</td><td>{p.altitude_m == null ? '—' : number(p.altitude_m, 1)}</td></tr>)}</tbody></table></div><div className="pagination"><span>第 {page + 1} / {Math.max(1, pages)} 页</span><button className="icon-button" aria-label="上一页" disabled={page === 0} onClick={() => setPage(page - 1)}><ChevronLeft size={19} /></button><button className="icon-button" aria-label="下一页" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}><ChevronRight size={19} /></button></div></Modal>;
}

function flightRow(label: string, value: ReactNode) {
  if (value == null || value === '') return null;
  return <div className="flight-info-row" key={label}><span>{label}</span><strong>{value}</strong></div>;
}

function flightHeading(label: string) {
  return <div className="flight-info-heading" key={label}>{label}</div>;
}

const FLIGHT_CATEGORY: Record<string, string> = { Passenger: '客运', Cargo: '货运', Government: '公务', Business: '公务', Other: '其它' };

function FlightInfoPanel({ trip }: { trip: Trip }) {
  const overlay = trip.metadata?.gps_overlay as GpsOverlay | undefined;
  // Records imported before the full-summary upgrade keep the old shape:
  // identity fields live in .summary and the airports/times use other keys.
  const raw = trip.metadata?.flight as (FlightInfo & { summary?: Record<string, any>; origin?: string; destination?: string; departure?: string; arrival?: string }) | undefined;
  const info = raw ? {
    ...raw,
    callsign: raw.callsign ?? raw.summary?.callsign,
    registration: raw.registration ?? raw.summary?.reg,
    aircraft_type: raw.aircraft_type ?? raw.summary?.type,
    icao24: raw.icao24 ?? raw.summary?.hex,
    departure_airport: raw.departure_airport ?? raw.origin,
    arrival_airport: raw.arrival_airport ?? raw.destination,
    takeoff_time: raw.takeoff_time ?? raw.departure,
    landing_time: raw.landing_time ?? raw.arrival,
  } as FlightInfo : undefined;
  const hasInfo = !!info && typeof info === 'object';
  const airport = (code?: string | null, name?: string | null, iata?: string | null) =>
    code ? [code, iata && iata !== code ? iata : null].filter(Boolean).join(' / ') + (name ? ` · ${name}` : '') : null;
  const runway = (value?: string | null, official?: boolean) =>
    value ? <>{value}{!official && <small className="runway-note">（推断）</small>}</> : null;
  const duration = (seconds?: number | null) => seconds == null ? null :
    `${Math.floor(seconds / 3600)}小时${Math.round(seconds % 3600 / 60)}分`;
  const altitude = (metres?: number) => metres == null ? null :
    `${number(metres, 0)} m · ${number(Math.round(metres / 0.3048), 0)} ft`;
  const diverted = info!.arrival_airport_actual && info!.arrival_airport && info!.arrival_airport_actual !== info!.arrival_airport;
  const stats = info!.track_stats;
  // Group headings only render when their group actually has a row.
  const hasOverview = !!(info!.callsign || info!.airline_name || info!.aircraft_type || info!.registration || info!.icao_type || info!.registered_owner || info!.aircraft_category || info!.icao24);
  const hasAirports = !!(info!.departure_airport || info!.arrival_airport || info!.takeoff_runway || info!.landing_runway || diverted);
  const hasTimes = !!(info!.takeoff_time || info!.landing_time || info!.flight_time_s != null || info!.actual_distance_km != null || info!.circle_distance_km != null);
  const hasRoute = !!(info!.borrowed_from_date || info!.route_waypoints?.length || info!.sid || info!.star || info!.approach || info!.airways?.length);
  return <div className="flight-info">
    {hasInfo && <>
      {hasOverview && <div className="flight-info-grid">
        {flightRow('呼号', info!.callsign)}
        {flightRow('当日飞机资料来源', info!.identity_source === 'mrairspace' ? 'MrAirspace · ADS-B 历史观测' : null)}
        {info!.airline_name && flightRow('航空公司', info!.airline_name)}
        {flightRow('机型 / 注册号', [info!.aircraft_type, info!.registration].filter(Boolean).join(' · ') || null)}
        {info!.icao_type && info!.icao_type !== info!.aircraft_type && flightRow('型别', info!.icao_type)}
        {flightRow('运营人', [info!.registered_owner, info!.owner_country].filter(Boolean).join(' · ') || null)}
        {info!.aircraft_category && flightRow('航班类别', FLIGHT_CATEGORY[info!.aircraft_category] || info!.aircraft_category)}
        {flightRow('ICAO24', info!.icao24 ? String(info!.icao24).toUpperCase() : null)}
      </div>}
      {hasAirports && flightHeading('机场与跑道')}
      <div className="flight-info-grid">
        {flightRow('起飞机场', airport(info!.departure_airport, info!.departure_airport_name, info!.departure_iata))}
        {flightRow('到达机场', airport(info!.arrival_airport, info!.arrival_airport_name, info!.arrival_iata))}
        {flightRow('起降机场依据', info!.identity_evidence?.airport_evidence?.source === 'local-gps' ? '本地 GPS · 已匹配当日 ADS-B 观测' : null)}
        {diverted && flightRow('实际到达', airport(info!.arrival_airport_actual, null, info!.arrival_iata_actual))}
        {flightRow('起飞跑道', runway(info!.takeoff_runway, info!.runway_takeoff_official))}
        {flightRow('落地跑道', runway(info!.landing_runway, info!.runway_landed_official))}
      </div>
      {hasTimes && flightHeading('时间与距离')}
      <div className="flight-info-grid">
        {flightRow(info!.times_inferred ? '飞行记录起点（当地·推断）' : '实际起飞（当地）', formatAirportTime(info!.takeoff_time, info!.departure_timezone))}
        {flightRow(info!.times_inferred ? '飞行记录终点（当地·推断）' : '实际落地（当地）', formatAirportTime(info!.landing_time, info!.arrival_timezone))}
        {flightRow('飞行时长', duration(info!.flight_time_s))}
        {info!.actual_distance_km != null && flightRow('实际飞行距离', `${number(info!.actual_distance_km, 0)} km`)}
        {info!.circle_distance_km != null && flightRow('大圆距离', `${number(info!.circle_distance_km, 0)} km`)}
      </div>
      {stats && flightHeading('飞行统计')}
      {stats && <div className="flight-info-grid">
        {flightRow('巡航高度', altitude(stats.cruise_altitude_m))}
        {flightRow('最高高度', altitude(stats.max_altitude_m))}
        {flightRow('巡航速度', stats.cruise_speed_kmh != null ? `${number(stats.cruise_speed_kmh, 0)} km/h` : null)}
        {flightRow('最高地速', stats.max_speed_kmh != null ? `${number(stats.max_speed_kmh, 0)} km/h` : null)}
      </div>}
      {hasRoute && flightHeading('航路与拟合')}
      <div className="flight-info-grid">
        {info!.borrowed_from_date && flightRow('轨迹拟合', `借自 ${info!.borrowed_from_date} 同航班号的实际飞行`)}
        {/* Navaids flown over during cruise, inferred from the observed track. */}
        {info!.route_waypoints?.length ? flightRow('航路导航台', info!.route_waypoints.map(w => w.ident).join(' · ') + (info!.route_waypoints.length >= 40 ? ' …' : '')) : null}
        {/* SID/STAR/approach/airway rows render only when a data source provided them. */}
        {flightRow('离场程序 SID', info!.sid)}
        {flightRow('进场程序 STAR', info!.star)}
        {flightRow('进近程序', info!.approach)}
        {info!.airways?.length ? flightRow('航路', info!.airways.join(' → ')) : null}
      </div>
      {info!.photo_url && <AircraftPhoto key={info!.photo_url} info={info!} />}
    </>}
    {info?.identity_evidence && <p className="field-help">{info.identity_evidence.airport_evidence?.source === 'local-gps' ? '飞机身份来自当日 ADS-B 记录，起降机场由匹配的本地 GPS 确认。' : '机场由实际观测端点推断；未获取的端点留空。'}<a href={info.identity_evidence.source_url} target="_blank" rel="noreferrer">MrAirspace · ODbL-1.0</a></p>}
    {overlay && <p className="field-help">地图并集：{trip.source} {number(trip.point_count)} 点 + 本地 GPS {number(overlay.point_count)} 点。在线数据和原始 GPS 分别保留；点位详情可查看来源。</p>}
    {!info?.registration && !info?.icao24 && <p className="field-help">尚未识别当日执飞飞机。可在下方补充当日注册号，查询机型与可用照片。</p>}
  </div>;
}

function EnrichModal({ trip, onClose, onSaved }: { trip: Trip; onClose: () => void; onSaved: (trip: Trip) => void }) {
  const [busy, setBusy] = useState(false);
  const [lookupProgress, setLookupProgress] = useState('');
  const lookupController = useRef<AbortController | null>(null);
  useEffect(() => () => lookupController.current?.abort(), []);
  const close = () => { lookupController.current?.abort(); onClose(); };
  const [choices, setChoices] = useState<ReconstructCandidate[]>([]);
  const [pendingBody, setPendingBody] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<ApiError | Error | null>(null);
  const info = (trip.metadata?.flight as FlightInfo | undefined) || {};
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(null);
    const values = new FormData(event.currentTarget);
    const body = {
        trip_id: trip.id,
        flight_number: String(values.get('flight_number') || '').trim().toUpperCase(),
        travel_date: values.get('travel_date'),
        origin: String(values.get('origin') || '').trim().toUpperCase() || null,
        destination: String(values.get('destination') || '').trim().toUpperCase() || null,
        registration: String(values.get('registration') || '').trim().toUpperCase() || null,
        icao24: String(values.get('icao24') || '').trim().toUpperCase() || null,
      };
    await lookup(body);
  }
  async function lookup(body: Record<string, unknown>) {
    setBusy(true); setError(null); setChoices([]); setPendingBody(body); setLookupProgress('');
    const controller = new AbortController(); lookupController.current = controller;
    try {
      const record = await postFlightLookup<Trip>('/api/flight/enrich', body, setLookupProgress, controller.signal);
      onSaved(record); onClose();
    } catch (err) {
      if (controller.signal.aborted) return;
      const e = err as ApiError;
      const details = e.details as { candidates?: ReconstructCandidate[] } | null;
      if (e.status === 409 && details?.candidates?.length) setChoices(details.candidates);
      else setError(e);
    } finally { setBusy(false); setLookupProgress(''); }
  }
  return <Modal title="补充航班信息" subtitle={`${trip.title} · 只补充航班信息，不会改动已保存的轨迹点`} onClose={close} busy={busy && !lookupProgress}>
    <form onSubmit={submit} className="import-form">
      <div className="field-row"><label>航班号<input name="flight_number" defaultValue={info.flight_number || trip.service_number || ''} placeholder="例如 MU5105" required maxLength={15} autoComplete="off" /></label><label>起飞日期<input type="date" name="travel_date" defaultValue={trip.travel_date} max={today()} required /></label></div>
      <div className="field-row"><label>起飞机场 <span>选填</span><input name="origin" defaultValue={info.departure_airport || trip.origin || ''} placeholder="IATA / ICAO，例如 SHA" maxLength={4} /></label><label>到达机场 <span>选填</span><input name="destination" defaultValue={info.arrival_airport || trip.destination || ''} placeholder="例如 PEK" maxLength={4} /></label></div>
      <div className="field-row"><label>当日飞机注册号 <span>选填 · 自动查 hex</span><input name="registration" defaultValue={info.registration || ''} placeholder="例如 B7838 或 B-7838" maxLength={20} autoComplete="off" /></label><label>hex / ICAO24 <span>选填</span><input name="icao24" placeholder="已填注册号可留空" pattern="[0-9A-Fa-f]{6}" maxLength={6} /></label></div>
      <p className="field-help">填写当日注册号或 hex，可独立补充机型、运营人和可用照片，不需要历史轨迹或起飞时间。均留空时使用已有飞机标识；没有标识时再按航班号与日期查询。不会据此生成航路、速度或起降时刻。</p>
      {busy && lookupProgress && <div><p className="field-help" role="status">{lookupProgress}</p><button type="button" className="button-secondary" onClick={close}>{lookupProgress.startsWith('首次准备') ? '终止下载并退出' : '取消查询并退出'}</button></div>}
      <ErrorNotice error={error} />
      {choices.length > 0 && <div className="candidate-list"><p>请选择实际乘坐的航段：</p>{choices.map(candidate => <button type="button" key={`${candidate.icao24}-${candidate.first_seen}`} disabled={busy} onClick={() => void lookup({ ...pendingBody, ...(candidate.schedule_candidate_id ? { schedule_candidate_id: candidate.schedule_candidate_id } : { icao24: candidate.icao24, first_seen: candidate.first_seen }) })}><Plane size={18} /><span><strong>{candidate.callsign} {candidate.est_departure_airport || '—'} → {candidate.est_arrival_airport || '—'}</strong><small>{candidate.departure_local} · {candidate.registration || candidate.icao24}</small></span><ChevronRight size={18} /></button>)}</div>}
      <div className="modal-actions"><button type="button" className="button-secondary" onClick={onClose} disabled={busy}>取消</button><button className="button-primary" type="submit" disabled={busy}>{busy ? <LoaderCircle className="spin" size={18} /> : <Search size={18} />}{busy ? '正在获取…' : '获取并补充'}</button></div>
    </form>
  </Modal>;
}

function DateModal({ trip, onClose, onSaved }: { trip: Trip; onClose: () => void; onSaved: (trip: Trip) => void }) {
  const [value, setValue] = useState(trip.travel_date);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | Error | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(null);
    try {
      const record = await api<Trip>(`/api/trips/${encodeURIComponent(trip.id)}/date`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ travel_date: value }) });
      onSaved(record); onClose();
    } catch (err) { setError(err as Error); setBusy(false); }
  }
  return <Modal title="更改轨迹日期" subtitle={`${trip.title} · 各点时刻不变，整条轨迹平移到新日期`} onClose={onClose} busy={busy}>
    <form onSubmit={submit} className="import-form">
      <label>出行日期<input type="date" value={value} onChange={e => setValue(e.target.value)} required /></label>
      <ErrorNotice error={error} />
      <div className="modal-actions"><button type="button" className="button-secondary" onClick={onClose} disabled={busy}>取消</button><button className="button-primary" type="submit" disabled={busy || value === trip.travel_date}>{busy ? <LoaderCircle className="spin" size={18} /> : <Check size={18} />}保存新日期</button></div>
    </form>
  </Modal>;
}

export default function App() {
  const [trips, setTrips] = useState<Trip[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode | 'all'>('all');
  const [q, setQ] = useState('');
  const [dateFrom, setDateFrom] = useState(''); const [dateTo, setDateTo] = useState('');
  const [sort, setSort] = useState('newest'); const [showFilters, setShowFilters] = useState(false);
  const [fitNonce, setFitNonce] = useState(0);
  const [importTab, setImportTab] = useState<ImportTab | null>(null);
  const [settings, setSettings] = useState(false); const [configured, setConfigured] = useState(false);
  const [token, setToken] = useState(''); const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ApiError | Error | null>(null); const [settingsError, setSettingsError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true); const [pointModal, setPointModal] = useState(false); const [deleteModal, setDeleteModal] = useState(false);
  const [chartModal, setChartModal] = useState(false);
  const [dateModal, setDateModal] = useState(false);
  const [fullTrip, setFullTrip] = useState<Trip | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const detailAbort = useRef<AbortController | null>(null);
  const currentSelection = useRef<string | null>(selectedId);
  const [railModal, setRailModal] = useState(false);
  const [enrichModal, setEnrichModal] = useState(false);
  const [recoverTrip, setRecoverTrip] = useState<Trip | null>(null);
  const [listLimit, setListLimit] = useState(100);
  const [toast, setToast] = useState('');
  const [exportMenu, setExportMenu] = useState(false);

  useEffect(() => {
    if (!exportMenu) return;
    const close = (e: MouseEvent) => { if (!(e.target as HTMLElement).closest('.export-menu')) setExportMenu(false); };
    document.addEventListener('click', close);
    return () => document.removeEventListener('click', close);
  }, [exportMenu]);

  async function refresh(select?: string, signal?: AbortSignal) {
    const full = await api<Trip[]>('/api/overview?include_points=false', { signal });
    if (signal?.aborted) return;
    setTrips(full); setSelectedId(old => select || (full.some(t => t.id === old) ? old : null));
  }
  useEffect(() => {
    const controller = new AbortController();
    void refresh(undefined, controller.signal).catch(err => { if (!controller.signal.aborted) setError(err); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    void api<{ fr24_configured: boolean }>('/api/settings', { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setConfigured(result.fr24_configured); })
      .catch(err => { if (!controller.signal.aborted) setError(err); });
    return () => controller.abort();
  }, []);
  useEffect(() => { if (!toast) return; const id = setTimeout(() => setToast(''), 5000); return () => clearTimeout(id); }, [toast]);

  const deferredQuery = useDeferredValue(q);
  const query = deferredQuery.trim().toLowerCase();
  const visible = useMemo(() => trips.filter(t => (mode === 'all' || t.mode === mode) && (!dateFrom || t.travel_date >= dateFrom) && (!dateTo || t.travel_date <= dateTo) && [t.title, t.origin, t.destination, t.service_number].join(' ').toLowerCase().includes(query)).sort((a, b) => (sort === 'distance' ? (tripDistance(b) ?? -1) - (tripDistance(a) ?? -1) : 0) || compareTripDates(a, b, sort === 'oldest')), [trips, mode, query, dateFrom, dateTo, sort]);
  const preview = visible.find(t => t.id === selectedId) || null;
  currentSelection.current = preview?.id ?? null;
  const selected = preview && fullTrip?.id === preview.id ? fullTrip : preview;
  useEffect(() => {
    detailAbort.current?.abort();
    setFullTrip(null); setDetailLoading(false); setPointModal(false); setChartModal(false); setDateModal(false);
    return () => detailAbort.current?.abort();
  }, [preview?.id, trips]);
  useEffect(() => { if (!pointModal && !chartModal) setFullTrip(null); }, [pointModal, chartModal]);

  async function openPoints(kind: 'points' | 'chart') {
    if (!selected || detailLoading) return;
    const id = selected.id;
    const controller = new AbortController();
    detailAbort.current?.abort(); detailAbort.current = controller;
    setDetailLoading(true);
    try {
      const record = await api<Trip>(`/api/trips/${encodeURIComponent(id)}`, { signal: controller.signal });
      if (controller.signal.aborted || currentSelection.current !== id) return;
      setFullTrip(record);
      if (kind === 'points') setPointModal(true); else setChartModal(true);
    } catch (e) { if (!controller.signal.aborted) setError(e as Error); }
    finally { if (!controller.signal.aborted) setDetailLoading(false); }
  }
  useEffect(() => setListLimit(100), [q, mode, dateFrom, dateTo, sort]);
  const totals = useMemo(() => {
    const personalTrips = trips.filter(t => !isSandboxTrip(t));
    return { distance: cumulativeDistance(personalTrips), flight: personalTrips.filter(t => t.mode === 'flight').length, rail: personalTrips.filter(t => t.mode === 'rail').length };
  }, [trips]);
  const backupDays = useMemo(() => trips.filter(t => t.metadata?.csv_profile === 'steplife-v1').length, [trips]);
  const personalPointCount = useMemo(() => trips.reduce((n, t) => n + (isSandboxTrip(t) ? 0 : t.point_count), 0), [trips]);
  const visiblePointCount = useMemo(() => visible.reduce((n, t) => n + t.point_count, 0), [visible]);
  const filtered = mode !== 'all' || q !== '' || dateFrom !== '' || dateTo !== '';

  async function saveSettings(event: FormEvent) {
    event.preventDefault(); setSaving(true); setSettingsError(null);
    try { const result = await api<{ fr24_configured: boolean }>('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fr24_api_token: token }) }); setConfigured(result.fr24_configured); setToken(''); setSettings(false); setToast('航班数据源设置已保存'); } catch (err) { setSettingsError(err as Error); } finally { setSaving(false); }
  }
  async function removeSelected() {
    if (!selected) return; setSaving(true);
    try { await api(`/api/trips/${selected.id}`, { method: 'DELETE' }); await refresh(); setDeleteModal(false); setToast('轨迹已删除'); } catch (err) { setError(err as Error); setDeleteModal(false); } finally { setSaving(false); }
  }
  async function applyEnriched(record: Trip) {
    setFullTrip(record);
    await refresh(record.id);
    setToast('航班信息已补充到这条轨迹');
  }

  async function applyReDated(record: Trip) {
    setFullTrip(record);
    await refresh(record.id);
    setToast('轨迹日期已更改');
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-symbol"><Footprints size={24} /></span><div><strong>足迹</strong><span>FOOTPRINT</span></div><button className="icon-button settings-button" aria-label="设置" onClick={() => { setSettings(true); setSettingsError(null); }}><Settings2 size={20} /></button></div>
      <div className="library-heading"><h1>我的旅程</h1><span>{number(trips.length)} 段足迹</span></div>
      <button className="button-primary add-button" onClick={() => setImportTab('rail')}><Plus size={20} />添加足迹</button>
      <div className="search-box"><Search size={18} /><input placeholder="搜索车次、航班或地点" aria-label="搜索轨迹" value={q} onChange={e => setQ(e.target.value)} />{q && <button aria-label="清空搜索" onClick={() => setQ('')}><X size={16} /></button>}</div>
      <div className="mode-tabs" aria-label="轨迹类型">{(['all', 'rail', 'flight', 'other'] as const).map(m => <button key={m} className={mode === m ? 'active' : ''} onClick={() => setMode(m)}>{m === 'all' ? '全部' : modeName[m]}</button>)}</div>
      <div className="list-tools"><span>{filtered ? `${visible.length} 条匹配` : '全部记录'}</span><button className={showFilters ? 'active' : ''} aria-expanded={showFilters} onClick={() => setShowFilters(!showFilters)}><SlidersHorizontal size={15} />筛选与排序</button></div>
      {showFilters && <div className="filters"><div className="field-row"><label>从<input aria-label="起始日期" type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} /></label><label>至<input aria-label="结束日期" type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} /></label></div><label>排序<select value={sort} onChange={e => setSort(e.target.value)}><option value="newest">日期从新到旧</option><option value="oldest">日期从旧到新</option><option value="distance">里程从长到短</option></select></label>{filtered && <button className="text-button" onClick={() => { setDateFrom(''); setDateTo(''); setMode('all'); setQ(''); }}>清除筛选</button>}</div>}
      <div className="trip-list">{loading ? <div className="list-empty"><LoaderCircle className="spin" size={24} /><p>正在读取足迹…</p></div> : visible.length === 0 ? <div className="list-empty"><Route size={30} /><strong>{filtered ? '没有匹配的足迹' : '旅程从这里开始'}</strong><p>{filtered ? '试试其他日期或关键词。' : '添加一次乘车、飞行，或导入自己的轨迹。'}</p></div> : visible.slice(0, listLimit).map(trip => <button className={`trip-card ${selectedId === trip.id ? 'selected' : ''}`} key={trip.id} onClick={() => setSelectedId(trip.id)}><div className="trip-card-top"><span className={`mode-badge ${trip.mode}`}><ModeIcon mode={trip.mode} size={16} />{isSandboxTrip(trip) ? '沙箱示例' : trip.service_number || (trip.metadata?.csv_profile === 'steplife-v1' ? 'GPS 记录' : modeName[trip.mode])}</span><time>{trip.travel_date}</time></div><h3>{trip.origin && trip.destination ? <>{trip.origin}<span className="route-separator">—</span>{trip.destination}</> : trip.title}</h3><div className="trip-card-bottom"><span>{formatTripDistance(trip)} <small>km</small></span><span>{isSandboxTrip(trip) ? '固定测试样例' : trip.mode === 'flight' && !trip.point_count ? '航班资料' : geometryName[trip.geometry_kind]}</span><ChevronRight size={16} /></div></button>)}{visible.length > listLimit && <button className="button-secondary" onClick={() => setListLimit(n => n + 100)}>继续显示 · 还有 {number(visible.length - listLimit)} 条</button>}</div>
      <div className="sidebar-foot"><span><Layers3 size={16} />数据保存在本机</span><span className="version">v0.1</span></div>
    </aside>
    <main className="workspace">
      <header className="workspace-header"><div className="workspace-title"><MapPin size={19} /><span>我的足迹地图</span></div><div className="overview-stats"><div><strong>{number(totals.distance, 1)}</strong><span>累计 km</span></div><i /><div><strong>{backupDays || totals.rail}</strong><span>{backupDays ? '记录日' : '铁路'}</span></div><div><strong>{backupDays ? number(personalPointCount) : totals.flight}</strong><span>{backupDays ? '轨迹点' : '飞行'}</span></div></div><button className="button-secondary header-import" onClick={() => setImportTab('csv')}><FileUp size={17} />导入 CSV</button></header>
      <div className="map-workspace">
        <Suspense fallback={<div className="track-map"><div className="map-status" role="status">正在准备足迹地图…</div></div>}><TrackMap ready={!loading} trips={visible} selectedTrip={preview} selectedId={selected?.id || null} onSelect={setSelectedId} fitNonce={fitNonce} /></Suspense>
        <div className="map-topline"><div className="map-legend"><span><i className="legend-dot rail" />铁路</span><span><i className="legend-dot flight" />飞机</span><span><i className="legend-dot other" />其他</span></div><div className="map-tools"><button onClick={() => setRailModal(true)}><TrainFront size={17} />铁路足迹</button><span className="raw-points-badge"><Layers3 size={17} />全部原始点</span><button onClick={() => { setSelectedId(null); setFitNonce(n => n + 1); }}><Route size={17} />总览</button><div className="export-menu"><button aria-expanded={exportMenu} title="导出一生足迹 11 列格式" className={trips.length ? '' : 'disabled-link'} aria-disabled={!trips.length}
  onClick={() => { if (trips.length) setExportMenu(v => !v); }}><Download size={17} />导出<ChevronDown size={13} /></button>
  {exportMenu && <div className="export-options" role="menu">
    <a role="menuitem" href="/api/export.csv" onClick={() => setExportMenu(false)}><strong>导出全部</strong><small>所有交通方式的全部轨迹</small></a>
    <a role="menuitem" href="/api/export.csv?modes=rail,flight" onClick={() => setExportMenu(false)}><strong>仅铁路与飞机</strong><small>通过本应用导入的铁路 / 航班轨迹</small></a>
  </div>}</div></div></div>
        {error && <div className="workspace-error"><ErrorNotice error={error} /><button className="text-button" onClick={() => { setError(null); void refresh().catch(e => setError(e)); }}>重新加载</button></div>}
        {!loading && trips.length === 0 && !error && <div className="map-empty-card"><span className="empty-icon"><Route size={29} /></span><span className="eyebrow">YOUR PERSONAL ATLAS</span><h2>走过的路，都留在这里。</h2><p>记录铁路与飞行轨迹，在地图上回看每一段旅程。</p><div className="empty-actions"><button className="button-primary" onClick={() => setImportTab('rail')}><Plus size={18} />添加第一段足迹</button><button className="text-button" onClick={() => setImportTab('csv')}>导入已有 CSV <ArrowUpRight size={15} /></button></div></div>}
        {selected && <section className={`trip-detail ${selected.mode === 'flight' ? 'flight-detail' : ''}`} aria-label="轨迹详情">
          <div className="detail-heading">
            <div>
              <span className={`detail-mode ${selected.mode}`}><ModeIcon mode={selected.mode} size={16} />{isSandboxTrip(selected) ? '沙箱示例' : selected.service_number || modeName[selected.mode]}<span>·</span>{selected.mode === 'flight' && !selected.point_count ? '航班资料' : geometryName[selected.geometry_kind]}</span>
              <h2>{selected.title}</h2>
              <p><CalendarDays size={14} />{selected.travel_date}<span className="detail-source">{selected.source || 'CSV'}</span></p>
            </div>
            <button className="icon-button" aria-label="收起轨迹详情" onClick={() => setSelectedId(null)}><X size={19} /></button>
          </div>
          <div className="detail-body" role={selected.mode === 'flight' ? 'region' : undefined} aria-label={selected.mode === 'flight' ? '航班详细资料' : undefined} tabIndex={selected.mode === 'flight' ? 0 : undefined}>
            {(selected.mode !== 'flight' || selected.point_count > 0) && <div className="detail-metrics">
              <div><span>{selected.mode === 'flight' ? '实际飞行里程' : '轨迹里程'}</span><strong>{formatTripDistance(selected)}<small>km</small></strong></div>
              <div><span>轨迹点</span><strong>{number(selected.point_count)}<small>个</small></strong></div>
              <div><span>{isSandboxTrip(selected) ? '样例最高地速' : '观测最高速度'}</span><strong>{selected.observed_max_speed_kmh == null ? '—' : number(selected.observed_max_speed_kmh, 1)}<small>{selected.observed_max_speed_kmh == null ? '无速度数据' : 'km/h'}</small></strong></div>
            </div>}
            {(selected.design_speed_kmh != null || selected.line_speed_limit_kmh != null) && <p className="speed-metadata">{selected.design_speed_kmh != null && `设计速度 ${selected.design_speed_kmh} km/h`}{selected.line_speed_limit_kmh != null && ` · 线路限速 ${selected.line_speed_limit_kmh} km/h`}</p>}
            {tripNotes(selected) && <p className="detail-note"><Info size={15} /><span>{tripNotes(selected)}{isSandboxTrip(selected) ? ' 示例不计入累计里程及飞行次数。' : ''}</span></p>}
            {selected.mode === 'flight' && <FlightInfoPanel trip={selected} />}
          </div>
          <div className="detail-actions">
            {selected.point_count > 0 && <>
              <button disabled={detailLoading} onClick={() => void openPoints('points')}>{detailLoading ? <LoaderCircle className="spin" size={16} /> : <List size={16} />}查看轨迹点</button>
              <button disabled={detailLoading} onClick={() => void openPoints('chart')}><ChartLine size={16} />高度与速度图</button>
              <a href={`/api/export.csv?trip_id=${encodeURIComponent(selected.id)}`}><Download size={16} />导出 CSV</a>
            </>}
            {selected.mode === 'flight' && <>
              <button onClick={() => setEnrichModal(true)}><PlaneTakeoff size={16} />补充 / 更新航班信息</button>
              {selected.point_count === 0 && <button onClick={() => setRecoverTrip(selected)}><Route size={16} />从已有 GPS 恢复轨迹</button>}
            </>}
            {selected.mode === 'rail' && selected.geometry_kind === 'reconstructed' && <button onClick={() => setDateModal(true)}><CalendarDays size={16} />更改日期</button>}
            <button className="delete-button" aria-label="删除这条轨迹" onClick={() => setDeleteModal(true)}><Trash2 size={16} /></button>
          </div>
        </section>}
        <div className="map-caption"><span>FOOTPRINT / PERSONAL TRAVEL ARCHIVE</span><span>{visible.length ? `${visible.length} 段轨迹 · ${number(visiblePointCount)} 个点` : '准备记录下一段旅程'}</span></div>
      </div>
    </main>
    {importTab && <ImportModal initialTab={importTab} onClose={() => setImportTab(null)} configured={configured} onImported={async (id, hint) => { await refresh(id); setToast(hint || '轨迹已保存到本机'); }} />}
    {railModal && <Modal wide title="铁路足迹" subtitle="客运站、货运站、线路所均可连接区间，按下行排列。" onClose={() => setRailModal(false)}><Suspense fallback={<p className="field-help">正在读取铁路足迹…</p>}><RailCoverage /></Suspense></Modal>}
    {pointModal && selected && <PointsModal trip={selected} onClose={() => setPointModal(false)} />}
    {dateModal && selected && <DateModal trip={selected} onClose={() => setDateModal(false)} onSaved={applyReDated} />}
    {recoverTrip && <ImportModal initialTab="flight" existingTrip={recoverTrip} onClose={() => setRecoverTrip(null)} configured={configured} onImported={async id => { setFullTrip(null); await refresh(id); setToast('航班轨迹已从本地 GPS 恢复'); }} />}
    {enrichModal && selected && <EnrichModal trip={selected} onClose={() => setEnrichModal(false)} onSaved={record => void applyEnriched(record)} />}
    {chartModal && selected && <Modal wide title="一天的高度与速度" subtitle={`${selected.title} · ${selected.source || "CSV"}`} onClose={() => setChartModal(false)}><Suspense fallback={<p className="field-help">正在准备图表…</p>}><TelemetryCharts key={selected.id} trip={selected} /></Suspense></Modal>}
    {deleteModal && selected && <Modal title="删除这条足迹？" onClose={() => setDeleteModal(false)} busy={saving}><div className="delete-content"><p>{selected.title}</p><p>这会从本地轨迹库移除记录。你可以先导出 CSV 留存。</p></div><div className="modal-actions"><button className="button-secondary" onClick={() => setDeleteModal(false)} disabled={saving}>取消</button><button className="button-danger" onClick={() => void removeSelected()} disabled={saving}>{saving ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}删除轨迹</button></div></Modal>}
    {settings && <Modal title="数据源设置" subtitle="航班查询密钥与轨迹来源。" onClose={() => { setSettings(false); setToken(''); }} busy={saving}><form onSubmit={saveSettings} className="settings-form"><div className="settings-status"><Plane size={20} /><div><strong>Flightradar24 API</strong><p>{configured ? '已配置，可以查询套餐范围内的航班。' : '尚未配置，CSV 导入仍可正常使用。'}</p></div><span className={`config-state ${configured ? 'configured' : ''}`}>{configured ? '已配置' : '未配置'}</span></div><label>{configured ? '更新 API 密钥' : 'API 密钥'}<input type="password" value={token} onChange={e => setToken(e.target.value)} autoComplete="new-password" placeholder="粘贴你的官方 API token" /></label><p className="field-help">密钥仅保存在这台电脑，不会出现在导出的轨迹中。保存空值会清除已配置密钥。</p><a className="source-link" href="https://fr24api.flightradar24.com/" target="_blank" rel="noreferrer">查看官方 API 与套餐 <ArrowUpRight size={14} /></a><div className="source-info"><TrainFront size={19} /><div><strong>铁路线路 · RailGo</strong><p>按需获取线路曲线，标记为经由重建。历史日期将根据今日该车次经由导入轨迹。</p><a href="https://api.railgo.dev/" target="_blank" rel="noreferrer">RailGo 数据服务</a></div></div><div className="source-info"><Download size={19}/><div><strong>完整来源备份</strong><p>保留车次、区段和来源备注；日常轨迹导出使用一生足迹 11 列格式。</p><a href="/api/export.csv?format=extended" download>下载扩展 CSV 备份</a></div></div><ErrorNotice error={settingsError} /><div className="modal-actions"><button className="button-secondary" type="button" disabled={saving} onClick={() => { setSettings(false); setToken(''); }}>关闭</button><button className="button-primary" disabled={saving}>{saving ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}保存设置</button></div></form></Modal>}
    {toast && <div className="toast" role="status"><Check size={18} />{toast}</div>}
  </div>;
}
