"""Plan an offline railway-density upgrade, then apply that reviewed plan atomically."""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.rail_density import apply_plan, payload_hash, upgrade_record
from backend.store import TripStore, default_database_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=default_database_path())
    parser.add_argument('--output', type=Path)
    parser.add_argument('--apply-plan', type=Path)
    args = parser.parse_args()
    store = TripStore(args.database)
    if args.apply_plan:
        plan = json.loads(args.apply_plan.read_text())
        if str(store.path.resolve()) != plan['database']:
            raise ValueError('规划文件与目标数据库不一致')
        apply_plan(store, plan['changes'])
        result = {'applied': len(plan['changes']), 'database': plan['database'],
                  'completed_at': datetime.now(timezone.utc).isoformat()}
        args.apply_plan.with_name('applied.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
        return
    output = args.output or ROOT / 'outputs' / ('rail-density-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    output.mkdir(parents=True, exist_ok=False)
    with store.connect() as db:
        db.execute('BEGIN')
        originals = list(db.execute("SELECT id,payload FROM trips WHERE mode='rail' ORDER BY travel_date,id"))
        others = {row['id']: payload_hash(row['payload']) for row in db.execute("SELECT id,payload FROM trips WHERE mode!='rail'")}
    backup = {'database': str(store.path.resolve()), 'created_at': datetime.now(timezone.utc).isoformat(),
              'records': [json.loads(row['payload']) for row in originals], 'other_record_hashes': others}
    (output / 'before.json').write_text(json.dumps(backup, ensure_ascii=False, indent=2))
    plan = {'database': str(store.path.resolve()), 'backup': str((output / 'before.json').resolve()), 'changes': []}
    rows = []
    failures = 0
    for original in originals:
        record = json.loads(original['payload'])
        row = {'id': record['id'], 'title': record['title'], 'date': record['travel_date'],
               'before': len(record['points']), 'after': len(record['points']), 'status': '', 'reason': ''}
        try:
            replacement = upgrade_record(store, record)
            row['after'] = len(replacement['points'])
            if replacement == record:
                row['status'] = '已是原始节点密度'
            else:
                row['status'] = '待升级'
                plan['changes'].append({'before_sha256': payload_hash(original['payload']), 'record': replacement})
        except Exception as error:
            failures += 1
            row.update(status='未通过校验', reason=str(error))
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    with (output / 'report.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=['id', 'title', 'date', 'before', 'after', 'status', 'reason'])
        writer.writeheader(); writer.writerows(rows)
    (output / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    summary = {'records': len(rows), 'changes': len(plan['changes']), 'failures': failures,
               'before_points': sum(row['before'] for row in rows), 'after_points': sum(row['after'] for row in rows),
               'output': str(output.resolve())}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if failures:
        # An incomplete plan cannot accidentally be applied by the CLI.
        (output / 'plan.json').unlink()
        raise SystemExit(1)


if __name__ == '__main__':
    main()
