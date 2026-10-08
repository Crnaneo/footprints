#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python || ! -f dist/index.html ]]; then
  ./scripts/setup.sh
fi
exec .venv/bin/python scripts/run.py --open
