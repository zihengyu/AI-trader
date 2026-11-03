#!/usr/bin/env bash
# Pre-open dress rehearsal of the AI paper fund (local mode), best run while the
# market is closed: one complete live decision on a COPY of the account, with
# real market data and the real models, stopped before any order. It runs under
# the trading service's own systemd sandbox (same filesystem permissions), so
# problems that only appear in the service, such as a read-only directory,
# show up here instead of at the open. The real account, its STOP, approvals
# and Telegram are not touched. Each stage reports its time and result.
# It runs as a background one-off service, so a dropped SSH session does not stop it.
# Usage: bash scripts/fund-rehearse.sh [--no-models | --show]
#   --no-models  data stages only, no model cost
#   --show       print the newest saved result (e.g. after reconnecting)
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"
py="${AI_FUND_PYTHON:-$repo/.venv/bin/python}"
env_file="${AI_FUND_ENV:-$repo/.env}"
user="${AI_FUND_USER:-$(id -un)}"
unit=lumibot-fund-rehearsal
extra=()
show=0
for arg in "$@"; do
  case "$arg" in
    --no-models) extra+=(--no-models) ;;
    --show) show=1 ;;
    *) echo "用法：bash scripts/fund-rehearse.sh [--no-models | --show]" >&2; exit 2 ;;
  esac
done
latest() { "$py" -m scripts.autonomous_fund.run rehearse --latest --env-file "$env_file"; }
if (( show )); then
  if systemctl is-active --quiet "$unit" 2>/dev/null; then
    echo "彩排还在运行中。实时进度：journalctl -u $unit -f -o cat"
  fi
  latest
  exit $?
fi
run() { if [[ -n "${AI_FUND_DRY_RUN:-}" ]]; then echo "+ $*"; else "$@"; fi; }

if [[ "$user" == "root" ]]; then
  echo "请用普通用户（例如 ubuntu）运行，不要加 sudo；脚本需要时会自己调用 sudo。" >&2; exit 1
fi
command=("$py" -m scripts.autonomous_fund.run rehearse --env-file "$env_file" "${extra[@]}")
if command -v systemd-run >/dev/null 2>&1 && [[ -z "${AI_FUND_NO_SANDBOX:-}" ]]; then
  properties=()
  while IFS= read -r line; do
    [[ -n "$line" ]] && properties+=(-p "$line")
  done < <("$py" -m scripts.autonomous_fund.deploy sandbox --user "$user" --env-file "$env_file")
  if systemctl is-active --quiet "$unit" 2>/dev/null; then
    echo "已有一次彩排在运行。实时进度：journalctl -u $unit -f -o cat；结束后：bash scripts/fund-rehearse.sh --show"
    exit 0
  fi
  echo "在交易服务相同的文件沙箱中彩排（以 $user 身份在后台运行，通常 5–15 分钟；需要 sudo 启动一次临时任务）……"
  started="$(date +%s)"
  run sudo systemd-run --quiet --collect --unit="$unit" --uid="$user" --gid="$(id -gn "$user")" \
    "${properties[@]}" "${command[@]}"
  echo "彩排已在后台运行，断开 SSH 也不会中断。重新连上后查看结果：bash scripts/fund-rehearse.sh --show"
  [[ -n "${AI_FUND_DRY_RUN:-}" ]] && exit 0
  # While connected: follow the log, then print the result.
  journalctl -u "$unit" -f -o cat --since "@$started" &
  follower=$!
  trap 'kill "$follower" 2>/dev/null || true' EXIT
  sleep 2
  while systemctl is-active --quiet "$unit" 2>/dev/null; do sleep 5; done
  kill "$follower" 2>/dev/null || true
  echo
  latest
else
  echo "没有 systemd（或设置了 AI_FUND_NO_SANDBOX）：直接运行彩排，文件权限类问题不会暴露。"
  run "${command[@]}"
fi
