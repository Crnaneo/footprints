"""Canonical identities from public station/line metadata, never name exceptions."""
from collections import defaultdict,Counter


def canonical_control_station_ids(index):
    ids=list(index.station_ids)
    for a,b in sorted(index.station_tree.query_pairs(2000)):
        if index.station_kinds[a]!='线路所' or index.station_kinds[b]!='线路所':continue
        if index.station_names[a]!=index.station_names[b]:continue
        claims=index.station_claims.get(index.station_nodes[a],set())&index.station_claims.get(index.station_nodes[b],set())
        if not claims:continue
        ta,tb=index.station_tags[a],index.station_tags[b]
        # Distinct official identifiers or kilometer markers distinguish
        # stations even when their names happen to coincide.
        if any(k in ta and k in tb and ta[k]!=tb[k] for k in ta if k in {'ref','railway:ref'} or k.startswith('railway:position')):continue
        old,new=ids[b],ids[a]
        ids=[new if identity==old else identity for identity in ids]
    return ids


def collapse_line_aliases(index,lines):
    grouped=defaultdict(list)
    for row in lines:
        ref=index.lines[int(row['line_id'])].get('tags',{}).get('ref','')
        if ref.isdigit():grouped[ref].append(row)
    excluded=set()
    for rows in grouped.values():
        if len(rows)<2:continue
        ordered=sorted(rows,key=lambda r:(-len(index.reference_way_ids[int(r['line_id'])]),int(r['line_id'])))
        for i,primary in enumerate(ordered):
            if primary['line_id'] in excluded:continue
            ways=set(index.reference_way_ids[int(primary['line_id'])])
            stations={s['id'] for section in primary['intervals'] for s in section['station_details']}
            for alias in ordered[i+1:]:
                if alias['line_id'] in excluded:continue
                other=set(index.reference_way_ids[int(alias['line_id'])])
                if not other or len(ways&other)<min(len(ways),len(other))*.9:continue
                details={s['id'] for section in alias['intervals'] for s in section['station_details']}
                if not details<=stations:continue
                primary.setdefault('aliases',[]).append({'name':alias['name'],'line_id':alias['line_id'],'ref':index.lines[int(alias['line_id'])]['tags']['ref']})
                excluded.add(alias['line_id'])
    # Different public names/refs may describe a child line and its umbrella
    # relation. Collapse only when physical membership and the actual recorded
    # point multiset prove that the displayed reached section is duplicated.
    kept=[row for row in lines if row['line_id'] not in excluded]
    way_sets={r['line_id']:set(index.reference_way_ids[int(r['line_id'])]) for r in kept}
    station_sets={r['line_id']:{s['id'] for section in r['intervals'] for s in section['station_details']} for r in kept}
    sources={}
    def evidence(identity):
        if identity not in sources:
            sources[identity]=Counter((tid,tuple(point)) for line,tid,xy in getattr(index,'point_sources',[])
                if str(line)==identity for point in xy)
        return sources[identity]
    ordered=sorted(kept,key=lambda r:len(way_sets[r['line_id']]))
    for i,child in enumerate(ordered):
        cid=child['line_id'];ways=way_sets[cid]
        if cid in excluded or not ways:continue
        for parent in ordered[i+1:]:
            pid=parent['line_id']
            if pid in excluded or station_sets[cid]!=station_sets[pid]:continue
            shared=ways&way_sets[pid]
            if len(shared)<5 or len(shared)<len(ways)*.9:continue
            if not evidence(cid) or evidence(cid)!=evidence(pid):continue
            child.setdefault('aliases',[]).extend([{'name':parent['name'],'line_id':pid,
                'ref':index.lines[int(pid)].get('tags',{}).get('ref','')},*parent.get('aliases',[])])
            excluded.add(pid)
    return [row for row in kept if row['line_id'] not in excluded]
