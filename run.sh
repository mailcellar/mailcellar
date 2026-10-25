#!/usr/bin/env bash
# Usage: ./run.sh <account> [backup|count|meta] [options]
# Kept for old habits; it runs: uv run mailcellar <backup|count|meta> <account> [options]
# Prefer mailcellar directly: it takes several accounts (or all of them by default)
# and has the dedup, offload, report and config commands too.
set -euo pipefail
cd "$(dirname "$0")"

if [[ $# -lt 1 || $1 == -* ]]; then
  echo "usage: $0 <account> [backup|count|meta] [options]" >&2
  exit 2
fi
account=$1
shift
action=backup
if [[ $# -gt 0 && $1 != -* ]]; then  # ./run.sh gmail --full is a backup too
  action=$1
  shift
fi

exec uv run mailcellar "$action" "$account" "$@"
