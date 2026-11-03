#!/usr/bin/env bash
# Maintenance and logs for the AI paper fund on Ubuntu.
# Usage: bash scripts/fund-logs.sh [status|logs|errors|report|approvals]
#   status     (default) services, trading/STOP state, monitor, account, latest notices
#   logs       follow the runner, monitor and data desk logs live (Ctrl+C to quit)
#   errors     warnings and errors from the last 24 hours
#   report     account report: return, drawdown, fills, model usage
#   approvals  purchase plans waiting for approval
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"
py="${AI_FUND_PYTHON:-$repo/.venv/bin/python}"
env_file="${AI_FUND_ENV:-$repo/.env}"
units=(lumibot-fund-runner.service lumibot-fund-monitor.service lumibot-fund-desk.service)
run() { if [[ -n "${AI_FUND_DRY_RUN:-}" ]]; then echo "+ $*"; else "$@"; fi; }
fund() { "$py" -m scripts.autonomous_fund.run "$@" --env-file "$env_file"; }

case "${1:-status}" in
  status)
    for unit in "${units[@]}"; do
      printf '%s：%s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
    done
    if systemctl is-active --quiet lumibot-fund-rehearsal 2>/dev/null; then
      printf '注意：彩排仍在运行（开始于 %s）；正常不超过 15 分钟，卡住时停止：sudo systemctl stop lumibot-fund-rehearsal\n' \
        "$(systemctl show -p ActiveEnterTimestamp --value lumibot-fund-rehearsal 2>/dev/null || true)"
    fi
    fund status ;;
  logs)
    run journalctl -u "${units[0]}" -u "${units[1]}" -u "${units[2]}" -f -n 50 ;;
  errors)
    if [[ -n "${AI_FUND_DRY_RUN:-}" ]]; then
      echo "+ journalctl -u ${units[0]} -u ${units[1]} -u ${units[2]} --since '24 hours ago'"
    else
      journalctl -u "${units[0]}" -u "${units[1]}" -u "${units[2]}" --since "24 hours ago" --no-pager |
        grep -E "WARNING|ERROR|CRITICAL|Traceback|Error:|RiskError|exited|failed|FUND " | tail -n 80 || true
    fi ;;
  report)
    fund report ;;
  approvals)
    fund approvals ;;
  *)
    echo "用法：bash scripts/fund-logs.sh [status|logs|errors|report|approvals]" >&2; exit 2 ;;
esac
