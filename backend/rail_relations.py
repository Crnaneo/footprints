"""Expand public railway relation parts, retaining unique physical members."""


def railway_members(root, relations):
    ways, stations, missing = set(), set(), set()
    seen = set()
    pending = [root]
    while pending:
        rid = pending.pop()
        if rid in seen:
            continue
        seen.add(rid)
        relation = relations.get(rid)
        if relation is None:
            missing.add(rid)
            continue
        for member in relation.get('members', []):
            kind, ref = member['type'], member['ref']
            if kind == 'way':
                ways.add(ref)
            elif kind == 'node':
                stations.add(ref)
            elif kind == 'relation':
                pending.append(ref)
    return sorted(ways), sorted(stations), sorted(missing)
