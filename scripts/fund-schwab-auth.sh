#!/usr/bin/env bash
# Renew the fund's Schwab market-data OAuth token from an interactive SSH shell.
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"
py="${AI_FUND_PYTHON:-$repo/.venv/bin/python}"
env_file="${AI_FUND_ENV:-$repo/.env}"
desk_user="$(systemctl show -p User --value lumibot-fund-desk.service 2>/dev/null || true)"
command=("$py" -m scripts.autonomous_fund.schwab_auth --env-file "$env_file" "$@")
if [[ -n "$desk_user" && "$desk_user" != "$(id -un)" ]]; then
  exec sudo -u "$desk_user" -- "${command[@]}"
fi
exec "${command[@]}"
