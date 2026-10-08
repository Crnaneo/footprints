"""Reproduce local-only railway rule/fragment audit. No network requests."""
from pathlib import Path
import sys,json
from collections import defaultdict
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.store import TripStore
from backend.rail_coverage import RailIndex,paths,COVERAGE_VERSION
from backend.rail_evidence import load_rail_trip


def make_audit(index,state,lines,visits,dates):
    from backend.rail_exclusions import apply_exclusions
    filtered=apply_exclusions({'lines':lines,'shared_only_lines':getattr(index,'shared_only_lines',[]),
        'unresolved_lines':index.unresolved_lines},index.path.parent)
    lines=filtered['lines']
    counts=defaultdict(int)
    for line,xy in index.point_observations:counts[line]+=len(xy)
    rows=[];issues=[]
    for line in lines:
        lid=int(line['line_id']);sections=[];used=set()
        for s in line['intervals']:
            ids=[st['id'] for st in s['station_details']]
            checks={'positive_distance':s['distance_km']>0,'unique_stations':len(ids)==len(set(ids)),
                'endpoint_consistency':ids[0]==s['origin_id'] and ids[-1]==s['destination_id'],
                'downward_order':all(index.station_orders[lid,a]<=index.station_orders[lid,b] for a,b in zip(ids,ids[1:])),
                'no_unmerged_shared_boundary':not bool(used&set(ids))}
            if COVERAGE_VERSION>=10:
                proof=s.get('terminal_evidence',{})
                checks['recorded_terminal_dates']=bool(s['origin_station'].get('first_arrival_date') and s['destination_station'].get('first_arrival_date'))
                checks['terminal_point_threshold']=all(proof.get(end,{}).get('reached',False) for end in ('origin','destination'))
            used.update(ids)
            if not all(checks.values()):issues.append({'line':line['name'],'section':s['origin']+'—'+s['destination'],'checks':checks})
            sections.append({'origin':s['origin'],'destination':s['destination'],'distance_km':s['distance_km'],
                'station_count':s['station_count'],'passenger_count':sum(st['kind']=='客运站' for st in s['station_details']),
                'stations':s['station_details'],'terminal_evidence':s.get('terminal_evidence',{}),'reference_gaps':s.get('reference_gaps',[]),'checks':checks})
        empty_blocks={}
        for key,stops in state['stations'].items():
            if key[0]!=lid:continue
            passengers=[s for s in stops if s['kind']=='客运站']
            positions=state['points'].get(key,[])
            for a,b in zip(passengers,passengers[1:]):
                if any(a['position']<=p<=b['position'] for p in positions):continue
                pair=tuple(sorted((a['id'],b['id'])))
                empty_blocks.setdefault(pair,{'origin':a['name'],'destination':b['name'],
                    'distance_km':round((b['position']-a['position'])/1000,1)})
        rows.append({'line_id':line['line_id'],'name':line['name'],'aliases':line.get('aliases',[]),'interval_count':len(sections),
            'point_count':counts[lid],'source_dates':sorted({dates[t] for t in visits[line['line_id']]}),
            'intervals':sections,'empty_passenger_blocks':list(empty_blocks.values()),
            'flags':(['区间较多'] if len(sections)>2 else [])+(['含短区间'] if any(s['distance_km']<5 for s in sections) else [])+(['点证据较少'] if counts[lid]<=3 else [])})
    return {'coverage_version':COVERAGE_VERSION,'line_count':len(rows),'interval_count':sum(r['interval_count'] for r in rows),
        'boundary_rejections':getattr(index,'boundary_rejections',[]),'hub_inferences':getattr(index,'hub_inferences',[]),
        'structural_issues':issues,'lines':rows,'shared_only_lines':filtered['shared_only_lines'],
        'unresolved_lines':filtered['unresolved_lines'],'manual_exclusions':filtered['manual_exclusions']}


def write_audit(report,root):
    directory=root/'data'/'rail';directory.mkdir(parents=True,exist_ok=True)
    version=report['coverage_version']
    (directory/f'coverage-v{version}-all-lines-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    text=[f'# 全线路区间复核（v{version}）','',f"已重算 {report['line_count']} 条线路、{report['interval_count']} 个区间。结构问题 {len(report['structural_issues'])} 个。",'',
      '密度汇总所有日期的已归属点，不要求同一记录或连续；长度只用于相邻铁路站点且不跨断档拼接。线路归属先考虑几何、定位精度、局部方向与明确邻线证据。原始地图和CSV不删除、不移动任何点。', '',
      '以下逐条列出最终边界，供核对断裂、短区间和枢纽。区间公里数是参考里程。点证据较少的条目仅说明符合几何规则，不能据此证明实际乘坐。','',
      '| 线路 | 区间数 | 显示区间（km） | 已归属点数 | 复核标记 |',
      '|---|---:|---|---:|---|']
    for r in sorted(report['lines'],key=lambda r:r['name']):
        sections='；'.join(f"{s['origin']}—{s['destination']}（{s['distance_km']}）" for s in r['intervals'])
        name=r['name']+('（含 '+ '、'.join(a['name'] for a in r['aliases'])+'）' if r.get('aliases') else '')
        text.append(f"| {name} | {r['interval_count']} | {sections} | {r['point_count']} | {'、'.join(r['flags']) or '边界/排序/合并检查通过'} |")
    text+=['','## 共用路段候选归并','', '| 候选 | 归入有长区间记录的线路 | 点数 |','|---|---|---:|']
    for r in report['shared_only_lines']:text.append(f"| {r['name']} | {r['attributed_to']} | {r['point_count']} |")
    notes=root/'docs'/f'rail-interval-review-v{version}-notes.md'
    if notes.exists():text+=['',notes.read_text()]
    (root/'docs'/f'rail-interval-review-v{version}.md').write_text('\n'.join(text)+'\n')


def main():
    store=TripStore();catalog,_=paths(store);index=RailIndex(catalog)
    index.raw_store=store;index.sandbox_trip_ids={t['id'] for t in store.list() if t.get('metadata',{}).get('sandbox')}
    coverage=defaultdict(list);visits=defaultdict(set);state={};dates={}
    def capture(*args):state.update(zip(['axes','stations','points','ranges','line_axes','directions','edges','boundary_points'],args))
    index.audit_callback=capture
    for t in store.list():
        dates[t['id']]=t['travel_date']
        if t.get('metadata',{}).get('sandbox'):continue
        covered,matched=index.match_trip(load_rail_trip(store,t['id']))
        for w,rs in covered.items():coverage[w].extend(rs)
        for line,tid in matched.items():visits[line].add(tid)
    lines=index.reference_sections(coverage,visits)
    report=make_audit(index,state,lines,visits,dates)
    write_audit(report,Path(__file__).resolve().parents[1])
    print(json.dumps({k:v for k,v in report.items() if k in ('line_count','interval_count','structural_issues')},ensure_ascii=False))

if __name__=='__main__':main()
