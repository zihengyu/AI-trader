#!/usr/bin/env bash
# Manual shutdown of the AI paper fund: stop trading first (an operator STOP that
# is never lifted automatically; the runner cancels this application's open
# orders), give it time to do so, then stop both services.
# Usage: bash scripts/fund-stop.sh [--now] [--disable]
#   --now      do not wait for the runner to cancel orders (default: wait 60 s)
#   --disable  also stop the services from starting at boot
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"
py="${AI_FUND_PYTHON:-$repo/.venv/bin/python}"
env_file="${AI_FUND_ENV:-$repo/.env}"
units=(lumibot-fund-runner.service lumibot-fund-monitor.service)
wait_seconds=60
disable=0
for arg in "$@"; do
  case "$arg" in
    --now) wait_seconds=0 ;;
    --disable) disable=1 ;;
    *) echo "未知参数：$arg（可用：--now、--disable）" >&2; exit 2 ;;
  esac
done
run() { if [[ -n "${AI_FUND_DRY_RUN:-}" ]]; then echo "+ $*"; else "$@"; fi; }

echo "1/3 停止交易（人工停止，不会自动恢复）……"
"$py" -m scripts.autonomous_fund.run stop --env-file "$env_file"

if (( wait_seconds > 0 )) && [[ -z "${AI_FUND_DRY_RUN:-}" ]] && systemctl is-active --quiet "${units[0]}" 2>/dev/null; then
  echo "2/3 等待 ${wait_seconds} 秒，让交易进程撤销本应用的挂单……"
  sleep "$wait_seconds"
else
  echo "2/3 跳过等待。"
fi

echo "3/3 停止服务……"
run sudo systemctl stop "${units[@]}"
if (( disable )); then
  run sudo systemctl disable --now "${units[@]}" lumibot-fund-backup.timer
  echo "    已取消开机自启（含每日备份定时器）。"
fi
echo
echo "已关停：交易保持停止，不会自动恢复。重新启动：bash scripts/fund-start.sh"
echo "如果是 paper 模式，请到 Alpaca 网页确认挂单都已撤销。"
