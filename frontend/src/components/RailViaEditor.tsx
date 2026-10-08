import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { ChevronDown, LoaderCircle, Map as MapIcon, Plus, Trash2 } from 'lucide-react';
import { api } from '../lib/api';
import type { ViaCatalogStation, ViaLine, ViaPreviewResult, ViaStation } from '../types';

interface Ref { id: string; name: string }
interface TimeSpec { mode: 'manual' | 'auto'; value: string; nextDay?: boolean }
interface Row { lineId: string | null; lineName: string; via: Ref | null; departure: TimeSpec; arrival: TimeSpec }

interface Choice { id: string; name: string; detail?: string }

function Combobox({ label, choices, selected, onSelect, onQuery, loading, remote, disabled, locked }: {
  label: string; choices: Choice[]; selected: Choice | null; onSelect: (choice: Choice | null) => void;
  onQuery?: (query: string) => void; loading?: boolean; remote?: boolean; disabled?: boolean; locked?: boolean;
}) {
  const id = useId(); const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState(selected?.name || ''); const [open, setOpen] = useState(false); const [active, setActive] = useState(-1);
  useEffect(() => { if (selected) setQuery(selected.name); setActive(-1); }, [selected?.id, selected?.name]);
  const options = remote ? choices : choices.filter(c => (c.name + ' ' + (c.detail || '')).includes(query.trim()));
  const choose = (c: Choice) => { onSelect(c); setQuery(c.name); setOpen(false); setActive(-1); };
  if (locked) return <div className="via-field via-field-locked"><span>{label}</span><strong>{selected?.name || '—'}</strong></div>;
  return <div className="via-field"><label htmlFor={id}>{label}</label>
    <div className="rail-match-input"><input ref={input} id={id} role="combobox" aria-autocomplete="list" aria-expanded={open}
      autoComplete="off" disabled={disabled} placeholder={disabled ? '请先选择上一项' : '输入后点击匹配项'} value={query}
      onFocus={() => { if (disabled) return; setOpen(true); if (selected) { setQuery(''); onQuery?.(''); } }}
      onBlur={() => { setOpen(false); if (selected) setQuery(selected.name); }}
      onChange={e => { const v = e.target.value; onSelect(null); setQuery(v); onQuery?.(v); setOpen(true); setActive(-1); }}
      onKeyDown={e => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); setOpen(true); setActive(i => Math.max(0, Math.min(options.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))); }
        if (e.key === 'Enter' && open) { e.preventDefault(); const pick = options[active >= 0 ? active : 0]; if (pick) choose(pick); }
        if (e.key === 'Escape' && open) { e.preventDefault(); e.stopPropagation(); setOpen(false); setQuery(selected?.name || query); }
      }} />
      <button type="button" aria-label={`打开${label}下拉框`} disabled={disabled} onMouseDown={e => e.preventDefault()}
        onClick={() => { if (disabled) return; if (open) { setOpen(false); if (selected) setQuery(selected.name); } else { input.current?.focus(); setOpen(true); setQuery(''); onQuery?.(''); } }}><ChevronDown size={15} /></button>
    </div>
    {open && <div className="rail-match-options" role="listbox" aria-label={`${label}匹配项`}>
      {loading ? <p role="status">正在读取目录…</p> : options.length ? options.map((c, i) =>
        <button type="button" role="option" aria-selected={selected?.id === c.id} className={active === i ? 'active' : ''} key={c.id}
          onMouseDown={e => e.preventDefault()} onClick={() => choose(c)}><span>{c.name}</span><small>{c.detail}</small></button>)
        : <p>没有匹配项，请调整输入。</p>}
    </div>}
  </div>;
}

function TimeField({ label, spec, onChange }: { label: string; spec: TimeSpec; onChange: (next: TimeSpec) => void }) {
  return <div className="via-time">
    <span className="via-time-label">{label}</span>
    <div className="via-time-modes" role="group" aria-label={`${label}设置方式`}>
      {(['manual', 'auto'] as const).map(m => <button type="button" key={m} className={spec.mode === m ? 'active' : ''}
        onClick={() => onChange({ ...spec, mode: m, value: m === 'manual' ? (spec.value || '08:00') : '' })}>{m === 'manual' ? '手动' : '自动'}</button>)}
    </div>
    {spec.mode === 'manual'
      ? <div className="via-time-input">
          <input type="time" value={spec.value} onChange={e => onChange({ ...spec, value: e.target.value })} aria-label={`${label}（手动）`} />
          <button type="button" className={spec.nextDay ? 'active' : ''} aria-pressed={!!spec.nextDay} title="跨零点列车：此时间是第二日"
            aria-label={`${label}设为次日`} onClick={() => onChange({ ...spec, nextDay: !spec.nextDay })}>+1日</button>
        </div>
      : <span className="via-time-auto">按已填时间推算</span>}
  </div>;
}

const autoSpec = (): TimeSpec => ({ mode: 'auto', value: '' });

export default function RailViaEditor({ onPreview, onError }: { onPreview: (result: ViaPreviewResult) => void; onError: (message: string) => void }) {
  const [travelDate, setTravelDate] = useState(() => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; });
  const [origin, setOrigin] = useState<Ref | null>(null);
  const [destination, setDestination] = useState<Ref | null>(null);
  const [rows, setRows] = useState<Row[]>([{ lineId: null, lineName: '', via: null, departure: { mode: 'manual', value: '08:00' }, arrival: autoSpec() }]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const [stationQuery, setStationQuery] = useState(''); const [stationResults, setStationResults] = useState<ViaStation[]>([]); const [loadingStations, setLoadingStations] = useState(false);
  const [destQuery, setDestQuery] = useState(''); const [destResults, setDestResults] = useState<ViaStation[]>([]); const [loadingDest, setLoadingDest] = useState(false);
  const [lineCache, setLineCache] = useState<Record<string, ViaLine[]>>({});
  const [stationCache, setStationCache] = useState<Record<string, ViaCatalogStation[]>>({});

  // Remote station search for the two title endpoints.
  useEffect(() => { const c = new AbortController(); setLoadingStations(true); const t = setTimeout(() => {
    void api<{ stations: ViaStation[] }>('/api/rail-via/stations?q=' + encodeURIComponent(stationQuery), { signal: c.signal })
      .then(r => setStationResults(r.stations)).catch(() => {}).finally(() => { if (!c.signal.aborted) setLoadingStations(false); });
  }, 200); return () => { clearTimeout(t); c.abort(); }; }, [stationQuery]);
  useEffect(() => { const c = new AbortController(); setLoadingDest(true); const t = setTimeout(() => {
    void api<{ stations: ViaStation[] }>('/api/rail-via/stations?q=' + encodeURIComponent(destQuery), { signal: c.signal })
      .then(r => setDestResults(r.stations)).catch(() => {}).finally(() => { if (!c.signal.aborted) setLoadingDest(false); });
  }, 200); return () => { clearTimeout(t); c.abort(); }; }, [destQuery]);

  const lastIndex = rows.length - 1;
  const originOf = (i: number): Ref | null => (i === 0 ? origin : rows[i - 1]?.via ?? null);
  const viaOf = (i: number): Ref | null => rows[i].via;
  const lastVia = rows.length ? rows[rows.length - 1].via : null;
  const chainDone = !!destination && !!lastVia && lastVia.id === destination.id;

  // Load the lines serving each row's origin station, and stations of each chosen line.
  useEffect(() => {
    const ids = new Set<string>();
    for (let i = 0; i < rows.length; i += 1) { const o = originOf(i); if (o) ids.add(o.id); }
    for (const id of ids) if (!(id in lineCache)) {
      void api<{ lines: ViaLine[] }>('/api/rail-via/station-lines?station_id=' + encodeURIComponent(id))
        .then(r => setLineCache(c => ({ ...c, [id]: r.lines }))).catch(e => setError(e.message));
    }
  }, [origin, destination, rows]);
  useEffect(() => {
    for (const row of rows) if (row.lineId && !(row.lineId in stationCache)) {
      void api<{ stations: ViaCatalogStation[] }>(`/api/rail-catalog/lines/${row.lineId}/stations`)
        .then(r => setStationCache(c => ({ ...c, [row.lineId as string]: r.stations }))).catch(e => setError(e.message));
    }
  }, [rows]);

  const setRow = (i: number, patch: Partial<Row>) => setRows(rs => rs.map((r, k) => (k === i ? { ...r, ...patch } : r)));
  const pickOrigin = (ref: Ref | null) => { setOrigin(ref); };
  const pickDestination = (ref: Ref | null) => { setDestination(ref); };

  // When the chosen line also serves the title destination, auto-complete this
  // row's via station as the destination so the chain ends here naturally.
  const pickLine = async (i: number, choice: Choice | null) => {
    setRow(i, { lineId: choice?.id || null, lineName: choice?.name || '', via: null });
    if (!choice || !destination) return;
    const lineId = choice.id;
    let stations = stationCache[lineId];
    if (!stations) {
      try {
        stations = (await api<{ stations: ViaCatalogStation[] }>(`/api/rail-catalog/lines/${lineId}/stations`)).stations;
      } catch { return; }
      setStationCache(cache => (cache[lineId] ? cache : { ...cache, [lineId]: stations as ViaCatalogStation[] }));
    }
    if (stations.some(s => s.id === destination.id)) setRow(i, { via: { id: destination.id, name: destination.name } });
  };

  const addRow = () => setRows(rs => [...rs, { lineId: null, lineName: '', via: null, departure: autoSpec(), arrival: autoSpec() }]);
  const removeRow = () => setRows(rs => (rs.length <= 1 ? rs : rs.slice(0, -1)));

  const stationChoices = useMemo(() => stationResults.map(s => ({ id: s.id, name: s.name, detail: s.kind })), [stationResults]);
  const destChoices = useMemo(() => destResults.map(s => ({ id: s.id, name: s.name, detail: s.kind })), [destResults]);

  async function preview() {
    setError('');
    if (!origin) { setError('请先选择始发站。'); return; }
    if (!destination) { setError('请先选择终到站。'); return; }
    for (let i = 0; i < rows.length; i += 1) {
      const o = originOf(i), v = viaOf(i), line = rows[i].lineId;
      if (!o) { setError(`第 ${i + 1} 行缺少始发站，请先填写上一行的经由站。`); return; }
      if (!line) { setError(`第 ${i + 1} 行未选择经由线路。`); return; }
      if (!v) { setError(`第 ${i + 1} 行未选择经由站。`); return; }
      if (rows[i].departure.mode === 'manual' && !rows[i].departure.value) { setError(`第 ${i + 1} 行始发时间设为手动，请填写时间。`); return; }
      if (rows[i].arrival.mode === 'manual' && !rows[i].arrival.value) { setError(`第 ${i + 1} 行经由时间设为手动，请填写时间。`); return; }
      if (destination && v.id === destination.id && i !== lastIndex) { setError(`第 ${i + 1} 段不能以终到站作为经由站，请把它放到最后一段。`); return; }
    }
    if (destination && viaOf(lastIndex)?.id !== destination.id) { setError('最后一段的经由站必须是标题中的终到站，请继续填写后续路段。'); return; }
    const manualCount = rows.reduce((n, r) => n + (r.departure.mode === 'manual' ? 1 : 0) + (r.arrival.mode === 'manual' ? 1 : 0), 0);
    if (manualCount === 0) { setError('至少将一个时间设为手动填写，其余时间才能自动计算。'); return; }
    setBusy(true);
    try {
      const body = {
        travel_date: travelDate,
        origin_id: origin.id,
        destination_id: destination.id,
        rows: rows.map((r, i) => ({
          origin_id: originOf(i)!.id, origin_name: originOf(i)!.name,
          line_id: r.lineId, line_name: r.lineName,
          via_id: viaOf(i)!.id, via_name: viaOf(i)!.name,
          departure: r.departure.mode === 'manual' ? { mode: 'manual', value: r.departure.value + (r.departure.nextDay ? '+1' : '') } : { mode: 'auto', value: null },
          arrival: r.arrival.mode === 'manual' ? { mode: 'manual', value: r.arrival.value + (r.arrival.nextDay ? '+1' : '') } : { mode: 'auto', value: null },
        })),
      };
      const result = await api<ViaPreviewResult>('/api/rail-via/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      onPreview(result);
    } catch (e) { const message = (e as Error).message; setError(message); onError(message); } finally { setBusy(false); }
  }

  return <div className="via-editor">
    <div className="inline-note"><MapIcon size={17} /><span>沿本地铁路目录逐段拼接经由。每一行的经由站就是下一行的始发站，首行始发站锁定为标题始发站；当某段选择的经由线路包含终到站时，该段经由站会自动补为终到站。线路有缺口时会保存为独立轨迹段，时间连续。</span></div>
    <div className="field-row">
      <label>乘车日期<input type="date" value={travelDate} max={travelDate} onChange={e => setTravelDate(e.target.value)} /></label>
    </div>
    <div className="via-endpoints">
      <Combobox label="始发站" choices={stationChoices} selected={origin} remote loading={loadingStations} onQuery={setStationQuery}
        onSelect={c => pickOrigin(c ? { id: c.id, name: c.name } : null)} />
      <Combobox label="终到站" choices={destChoices} selected={destination} remote loading={loadingDest} onQuery={setDestQuery}
        onSelect={c => pickDestination(c ? { id: c.id, name: c.name } : null)} />
    </div>

    <div className="via-rows">
      {rows.map((row, i) => {
        const o = originOf(i), v = viaOf(i), isLast = i === lastIndex;
        const lineChoices = (o ? lineCache[o.id] || [] : []).map(l => ({ id: l.line_id, name: l.name }));
        const viaChoices = (row.lineId ? stationCache[row.lineId] || [] : []).map(s => ({ id: s.id, name: s.name, detail: s.kind }));
        return <div className="via-row" key={i}>
          <div className="via-row-head"><span className="via-row-index">第 {i + 1} 段</span>
            {rows.length > 1 && isLast && <button type="button" className="text-button" onClick={removeRow} disabled={busy}><Trash2 size={14} />删除末段</button>}
          </div>
          <div className="via-row-grid">
            <Combobox label="始发站" choices={[]} selected={o} locked onSelect={() => {}} />
            <div className="via-slot"><TimeField label="始发时间" spec={row.departure} onChange={next => setRow(i, { departure: next })} /></div>
            <Combobox label="经由线路" choices={lineChoices} selected={row.lineId ? { id: row.lineId, name: row.lineName } : null}
              disabled={!o} onSelect={c => void pickLine(i, c)} />
            <Combobox label="经由站" choices={viaChoices} selected={v} disabled={!row.lineId}
              onSelect={c => setRow(i, { via: c ? { id: c.id, name: c.name } : null })} />
            <div className="via-slot"><TimeField label="经由时间" spec={row.arrival} onChange={next => setRow(i, { arrival: next })} /></div>
          </div>
        </div>;
      })}
    </div>
    <button type="button" className="button-secondary via-add" onClick={addRow} disabled={busy || !destination || !lastVia || chainDone}><Plus size={16} />{chainDone ? '经由已到达终到站' : '添加下一段经由'}</button>

    {error && <div className="error-notice" role="alert">{error}</div>}
    <div className="modal-actions">
      <button type="button" className="button-primary" onClick={() => void preview()} disabled={busy}>
        {busy ? <LoaderCircle className="spin" size={18} /> : <MapIcon size={18} />}{busy ? '正在重建经由轨迹…' : '预览经由轨迹'}
      </button>
    </div>
  </div>;
}
