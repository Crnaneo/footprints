#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.11 or later is required.'
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo 'Node.js 22.12 or later is required.'
  exit 1
fi
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements.txt
npm ci
npm run build
echo 'Setup complete. Open start.command to launch Footprint.'
