#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! command -v node >/dev/null 2>&1; then
  echo 'Node.js 22.12 or later is required. Install it and add it to PATH.' >&2
  exit 1
fi
exec node scripts/start.mjs "$@"
