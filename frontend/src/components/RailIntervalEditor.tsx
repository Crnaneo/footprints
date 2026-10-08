import { useEffect, useId, useRef, useState } from 'react';
import { ChevronDown, LoaderCircle, Save, X } from 'lucide-react';
import { api } from '../lib/api';
import type { Catalog, CatalogStation, EditResult, Line, LineChoice, Section } from './railTypes';

interface Choice { id:string; name:string; detail?:string }
function MatchSelect({label, choices, selected, onSelect, onQuery, loading=false, remote=false}: {
 label:string; choices:Choice[]; selected:Choice|null; onSelect:(choice:Choice|null)=>void; onQuery?:(query:string)=>void; loading?:boolean; remote?:boolean
}) {
 const id=useId();const input=useRef<HTMLInputElement>(null);const [query,setQuery]=useState(selected?.name||'');const [open,setOpen]=useState(false);const [active,setActive]=useState(-1);
 useEffect(()=>{if(selected)setQuery(selected.name);setActive(-1)},[selected?.id,selected?.name]);
 const options=remote?choices:choices.filter(choice=>(choice.name+'站 '+(choice.detail||'')).includes(query.trim()));
 function choose(choice:Choice){onSelect(choice);setQuery(choice.name);setOpen(false);setActive(-1)}
 return <div className="rail-match-select"><label htmlFor={id}>{label}</label><div className="rail-match-input"><input ref={input} id={id} role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls={id+'-options'} aria-activedescendant={open&&active>=0?id+'-'+active:undefined} autoComplete="off" placeholder="输入名称后点击匹配项" value={query} onFocus={()=>{setOpen(true);if(selected){setQuery('');onQuery?.('')}}} onBlur={()=>{setOpen(false);if(selected)setQuery(selected.name)}} onChange={e=>{const value=e.target.value;onSelect(null);setQuery(value);onQuery?.(value);setOpen(true);setActive(-1)}} onKeyDown={e=>{
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();setOpen(true);setActive(index=>Math.max(0,Math.min(options.length-1,index+(e.key==='ArrowDown'?1:-1))))}
  if(e.key==='Enter'&&open){e.preventDefault();if(options[active>=0?active:0])choose(options[active>=0?active:0])}
  if(e.key==='Escape'&&open){e.preventDefault();e.stopPropagation();setOpen(false);setQuery(selected?.name||query)}
 }}/><button type="button" aria-label={`打开${label}下拉框`} onMouseDown={e=>e.preventDefault()} onClick={()=>{if(open){setOpen(false);if(selected)setQuery(selected.name)}else{input.current?.focus();setOpen(true);setQuery('');onQuery?.('')}}}><ChevronDown size={16}/></button></div>
 {open&&<div className="rail-match-options" id={id+'-options'} role="listbox" aria-label={`${label}匹配项`}>{loading?<p role="status">正在读取目录…</p>:options.length?options.map((choice,index)=><button type="button" role="option" aria-selected={selected?.id===choice.id} className={active===index?'active':''} id={id+'-'+index} key={choice.id} onMouseDown={e=>e.preventDefault()} onClick={()=>choose(choice)}><span>{choice.name}</span><small>{choice.detail}</small></button>):<p>没有匹配项，请调整输入。</p>}</div>}
 </div>
}

export default function RailIntervalEditor({line,section,onSaved,onCancel}:{line?:Line;section?:Section;onSaved:(result:EditResult,message:string)=>Promise<void>;onCancel:()=>void}){
 const [selectedLine,setSelectedLine]=useState<LineChoice|null>(line?{line_id:line.line_id,name:line.name}:null);
 const [lineQuery,setLineQuery]=useState('');const [lineChoices,setLineChoices]=useState<LineChoice[]>([]);const [lineTotal,setLineTotal]=useState(0);const [loadingLines,setLoadingLines]=useState(false);
 const [catalog,setCatalog]=useState<Catalog|null>(null);const [loadingCatalog,setLoadingCatalog]=useState(false);
 const [origin,setOrigin]=useState<CatalogStation|null>(null);const [destination,setDestination]=useState<CatalogStation|null>(null);
 const [preview,setPreview]=useState<Section|null>(null);const [previewBusy,setPreviewBusy]=useState(false);const [saving,setSaving]=useState(false);const [error,setError]=useState('');
 useEffect(()=>{if(line)return;const controller=new AbortController();setLoadingLines(true);const timer=setTimeout(()=>{void api<{lines:LineChoice[];total:number}>('/api/rail-catalog/lines?q='+encodeURIComponent(lineQuery),{signal:controller.signal}).then(result=>{setLineChoices(result.lines);setLineTotal(result.total)}).catch(e=>{if(!controller.signal.aborted)setError(e.message)}).finally(()=>{if(!controller.signal.aborted)setLoadingLines(false)})},180);return()=>{clearTimeout(timer);controller.abort()}},[lineQuery,line]);
 useEffect(()=>{
  setCatalog(null);setOrigin(null);setDestination(null);setPreview(null);setLoadingCatalog(false);setError('');
  if(!selectedLine)return;
  const controller=new AbortController();setLoadingCatalog(true);
  void api<Catalog>(`/api/rail-catalog/lines/${selectedLine.line_id}/stations`,{signal:controller.signal}).then(result=>{
   setCatalog(result);setOrigin(result.stations.find(s=>s.id===(section?.origin_id||section?.origin_station?.id))||null);setDestination(result.stations.find(s=>s.id===(section?.destination_id||section?.destination_station?.id))||null);
  }).catch(e=>{if(!controller.signal.aborted)setError(e.message)}).finally(()=>{if(!controller.signal.aborted)setLoadingCatalog(false)});
  return()=>controller.abort();
 },[selectedLine?.line_id,section]);
 useEffect(()=>{
  setPreview(null);if(!selectedLine||!origin||!destination){setPreviewBusy(false);return}
  const controller=new AbortController();setPreviewBusy(true);setError('');
  void api<Section>('/api/rail-manual/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({line_id:selectedLine.line_id,origin_id:origin.id,destination_id:destination.id}),signal:controller.signal}).then(setPreview).catch(e=>{if(!controller.signal.aborted)setError(e.message)}).finally(()=>{if(!controller.signal.aborted)setPreviewBusy(false)});
  return()=>controller.abort();
 },[selectedLine?.line_id,origin?.id,destination?.id]);
 async function save(){if(!selectedLine||!origin||!destination||!catalog||!preview)return;setSaving(true);setError('');try{
  const result=await api<EditResult>('/api/rail-manual/interval',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({line_id:selectedLine.line_id,origin_id:origin.id,destination_id:destination.id,interval_id:section?.interval_id||null,expected_revision:line?.manual_revision||catalog.manual_revision})});
  await onSaved(result,`${selectedLine.name}区间已${section?'修改':'添加'}`);
 }catch(e){setError((e as Error).message);setSaving(false)}}
 const choices=(catalog?.stations||[]).map(s=>({id:s.id,name:s.name,detail:s.kind}));
 const choice=(s:CatalogStation|null)=>s?{id:s.id,name:s.name,detail:s.kind}:null;
 const passengers=(preview?.station_details||[]).filter(s=>s.kind==='客运站');
 return <section className="rail-editor" aria-label="铁路区间编辑"><div className="rail-editor-heading"><h3>{section?'编辑区间':line?'添加区间':'添加线路与区间'}</h3><button type="button" className="rail-icon-button" aria-label="关闭区间编辑" disabled={saving} onClick={onCancel}><X size={18}/></button></div>
 <p className="field-help">选择铁路和两端站点，自动列出区间内的客运站与首次到达日期。手动维护的线路在重新匹配后保留，可恢复自动统计。共享站点的连续、重叠区间合并，保留已记录的支线站点。</p>
 {line?<p className="rail-editor-line"><strong>{line.name}</strong><span>编辑该线路的统计记录</span></p>:<><MatchSelect label="铁路线" choices={lineChoices.map(l=>({id:l.line_id,name:l.name,detail:[l.ref,`目录 ${l.line_id}`].filter(Boolean).join(' · ')}))} selected={selectedLine?{id:selectedLine.line_id,name:selectedLine.name}:null} onSelect={c=>setSelectedLine(c?lineChoices.find(l=>l.line_id===c.id)||null:null)} onQuery={setLineQuery} loading={loadingLines} remote/>{lineTotal>100&&<p className="field-help">匹配 {lineTotal} 条，展示前 100 条；输入名称可缩小范围。</p>}</>}
 {loadingCatalog&&<p role="status" className="rail-editor-loading"><LoaderCircle size={17} className="spin"/>正在读取该线路所有站点和线路所…首次打开需加载本地铁路目录。</p>}
 {catalog&&<><p className="field-help">目录共 {catalog.stations.length} 个站点（含货运站与线路所）。{catalog.direction?.known?`下行：${catalog.direction.from} → ${catalog.direction.to}`:'下行方向待核对'}</p><div className="rail-editor-fields"><MatchSelect label="起始站点" choices={choices} selected={choice(origin)} onSelect={c=>setOrigin(c?catalog.stations.find(s=>s.id===c.id)||null:null)}/><MatchSelect label="终止站点" choices={choices} selected={choice(destination)} onSelect={c=>setDestination(c?catalog.stations.find(s=>s.id===c.id)||null:null)}/></div></>}
 {previewBusy&&<p role="status" className="rail-editor-loading"><LoaderCircle size={17} className="spin"/>正在匹配区间内车站与本地轨迹日期…</p>}
 {preview&&<div className="rail-editor-preview"><strong>{preview.origin} → {preview.destination}</strong><span>约 {preview.distance_km.toLocaleString('zh-CN')} km · {preview.station_count} 个站点 · {passengers.length} 个客运站</span><p className="field-help">已按下行排列。首次到达取站点半径 1 公里内最早的原始轨迹日期，表示接近车站，不证明停靠；无轨迹则显示“未记录”。手动区间由你确认，不受自动判定的点密度门槛限制。</p><p className="field-help">边界：{preview.origin} {preview.origin_station?.first_arrival_date||'未记录'} · {preview.destination} {preview.destination_station?.first_arrival_date||'未记录'}</p>{!!preview.reference_gaps?.length&&<p className="rail-editor-warning">目录存在 {preview.reference_gaps.length} 处几何缺口，缺口里程为直线估算，保存不会补造轨迹点。</p>}<div className="rail-passenger-heading"><span>区间内客运站</span><span>首次到达</span></div><ol>{passengers.map(s=><li key={s.id}><span>{s.name}</span>{s.first_arrival_date?<time dateTime={s.first_arrival_date}>{s.first_arrival_date}</time>:<small>未记录</small>}</li>)}</ol>{!passengers.length&&<p className="field-help">区间内没有客运站，仍可保存货运站或线路所区间。</p>}</div>}
 {error&&<div role="alert" className="error-notice">{error}</div>}
 <div className="rail-editor-actions"><button type="button" className="button-secondary" onClick={onCancel} disabled={saving}>取消</button><button type="button" className="button-primary" onClick={()=>void save()} disabled={!preview||previewBusy||saving}>{saving?<LoaderCircle className="spin" size={16}/>:<Save size={16}/>}保存区间</button></div>
 </section>
}
