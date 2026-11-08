#!/usr/bin/env bash
# Maintenance and logs for the AI paper fund on Ubuntu.
# Usage: bash scripts/fund-logs.sh [status|logs|errors|report|audit|approvals|decisions [YYYY-MM-DD]|prune [--apply]]
#   status     (default) services, trading/STOP state, monitor, account, latest notices
#   logs       follow the runner, monitor and data desk logs live (Ctrl+C to quit)
#   errors     last 80 warning/error records in 24 hours, including complete tracebacks
#   report     account report: return, drawdown, fills, model usage
#   approvals  purchase plans waiting for approval
#   decisions [YYYY-MM-DD]  read-only decision/execution/Telegram timeline for a New York date
#   prune [--apply]  old headlines, model-call traces and export archives: preview, or delete with --apply
#                    (also runs daily after the backup; the ledger, decisions and reviews are never deleted)
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
      echo "+ journalctl -u ${units[0]} -u ${units[1]} -u ${units[2]} --since '24 hours ago' --output=json"
    else
      journalctl -u "${units[0]}" -u "${units[1]}" -u "${units[2]}" --since "24 hours ago" --no-pager --output=json |
        "$py" -m scripts.autonomous_fund.log_errors
    fi ;;
  report)
    fund report ;;
  audit)
    fund audit-ledger ;;
  approvals)
    fund approvals ;;
  decisions)
    args=(decisions)
    if [[ $# -gt 2 ]]; then
      echo "用法：bash scripts/fund-logs.sh decisions [YYYY-MM-DD]" >&2; exit 2
    fi
    if [[ $# -eq 2 ]]; then args+=(--session-date "$2"); fi
    fund "${args[@]}" ;;
  prune)
    if [[ $# -gt 2 || ( $# -eq 2 && "$2" != "--apply" ) ]]; then
      echo "用法：bash scripts/fund-logs.sh prune [--apply]" >&2; exit 2
    fi
    if [[ $# -eq 2 ]]; then fund prune --apply; else fund prune; fi ;;
  *)
    echo "用法：bash scripts/fund-logs.sh [status|logs|errors|report|audit|approvals|decisions [YYYY-MM-DD]|prune [--apply]]" >&2; exit 2 ;;
esac
