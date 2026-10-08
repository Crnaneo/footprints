import json
from types import SimpleNamespace
from backend.rail_exclusions import apply_exclusions
from backend.rail_coverage import _cache_result, read_coverage, fingerprint
from backend.store import TripStore


def test_manual_exclusion_survives_cache_republication_without_changing_evidence(tmp_path):
    store=TripStore(tmp_path/'footprint.sqlite3')
    directory=tmp_path/'rail';directory.mkdir()
    (directory/'excluded-lines.json').write_text(json.dumps([{'line_id':'1','name':'废弃线','reason':'用户指定'}]))
    rows=[{'line_id':'1','name':'废弃线','intervals':[]},{'line_id':'2','name':'保留线','intervals':[]}]
    index=SimpleNamespace(unresolved_lines=[],shared_only_lines=[])
    _cache_result(store,index,rows,[],fingerprint(store),{})
    assert [r['line_id'] for r in read_coverage(store)['lines']]==['2']
    assert len(rows)==2  # The matching evidence stays available for auditing.
    data={'lines':rows,'unresolved_lines':[{'line_id':'1'}],'shared_only_lines':[{'line_id':'1'}]}
    filtered=apply_exclusions(data,directory)
    assert not filtered['unresolved_lines'] and not filtered['shared_only_lines']
    assert filtered['manual_exclusions'][0]['line_id']=='1'
