#!/usr/bin/env bash
# Run explicitly from this checkout's venv and secret file, never shell-source it.
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"
if [[ ! -x "$repo/.venv/bin/python" ]]; then
  echo 'Create .venv and install the checkout plus application requirements first.' >&2
  exit 1
fi
for arg in "$@"; do
  if [[ "$arg" == --env-file || "$arg" == --env-file=* ]]; then
    exec "$repo/.venv/bin/python" -m scripts.autonomous_fund.run "$@"
  fi
done
exec "$repo/.venv/bin/python" -m scripts.autonomous_fund.run "$@" --env-file "$repo/.env"
