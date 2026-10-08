"""Download optional public datasets; personal tracks must be imported separately."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', choices=['rail', 'global-rail', 'flights', 'all'])
    parser.add_argument('--from', dest='start', default='2024Q1', help='航班索引开始季度')
    parser.add_argument('--to', dest='end', default='2026Q2', help='航班索引结束季度')
    parser.add_argument('--list', action='store_true', help='仅列出将运行的命令，不下载')
    args = parser.parse_args()
    commands = []
    if args.dataset in ('rail', 'all') or (args.dataset == 'global-rail' and
                                          not (ROOT / 'data/rail/catalog.sqlite3').exists()):
        commands.append(['scripts/download_rail_catalog.py'])
    if args.dataset in ('global-rail', 'all'):
        commands.extend([['scripts/build_global_rail.py'], ['scripts/fetch_overture_rail.py']])
    if args.dataset in ('flights', 'all'):
        commands.append(['scripts/download_flight_indexes.py', '--from', args.start, '--to', args.end])
    for command in commands:
        print('运行：' + ' '.join([sys.executable, *command]), flush=True)
        if not args.list:
            result = subprocess.run([sys.executable, *command], cwd=ROOT)
            if result.returncode:
                return result.returncode
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\n已停止。航班索引的部分下载保留，可重新运行续传。')
        raise SystemExit(130)
