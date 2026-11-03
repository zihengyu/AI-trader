#!/usr/bin/env bash
# Pre-open dress rehearsal of the AI paper fund (local mode), best run while the
# market is closed: one complete live decision on a COPY of the account, with
# real market data, stopped before any order. By default a scripted AI answers
# every model call (no inference, no model cost); --real-models uses the real models. It runs under
# the trading service's own systemd sandbox (same filesystem permissions), so
# problems that only appear in the service, such as a read-only directory,
# show up here instead of at the open. The real account, its STOP, approvals
# and Telegram are not touched. Each stage reports its time and result.
# It runs as a background one-off service, so a dropped SSH session does not stop it.
# Usage: bash scripts/fund-rehearse.sh [--real-models | --no-models | --show]
#   (default)      whole flow with a scripted AI: real data, no model cost
#   --real-models  whole flow with the real models (costs money; after model or prompt changes)
#   --no-models    data stages only
#   --show         print the newest saved result (e.g. after reconnecting)
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"
py="${AI_FUND_PYTHON:-$repo/.venv/bin/python}"
env_file="${AI_FUND_ENV:-$repo/.env}"
user="${AI_FUND_USER:-$(id -un)}"
unit=lumibot-fund-rehearsal
extra=()
show=0
how="模拟 AI，不调用模型、不花模型费用，通常 2–5 分钟"
for arg in "$@"; do
  case "$arg" in
    --no-models) extra+=(--no-models); how="只检查数据，不调用模型，通常 1–3 分钟" ;;
    --real-models) extra+=(--real-models); how="真实模型，会产生模型费用，通常 5–15 分钟" ;;
    --show) show=1 ;;
    *) echo "用法：bash scripts/fund-rehearse.sh [--real-models | --no-models | --show]" >&2; exit 2 ;;
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
    since="$(systemctl show -p ActiveEnterTimestamp --value "$unit" 2>/dev/null || true)"
    echo "已有一次彩排在运行（开始于 ${since:-未知}）。实时进度：journalctl -u $unit -f -o cat；结束后：bash scripts/fund-rehearse.sh --show"
    echo "正常彩排不超过 15 分钟；如果已经运行很久，可以停掉它（只影响彩排副本）：sudo systemctl stop $unit"
    exit 0
  fi
  echo "在交易服务相同的文件沙箱中彩排（$how；以 $user 身份在后台运行，需要 sudo 启动一次临时任务）……"
  started="$(date +%s)"
  # A hard 30-minute limit and low CPU priority: a stuck rehearsal can never run for hours or
  # take CPU from the trading process (2026-11-03: one used both cores for 12 hours).
  run sudo systemd-run --quiet --collect --unit="$unit" --uid="$user" --gid="$(id -gn "$user")" \
    -p RuntimeMaxSec=1800 -p Nice=10 -p CPUWeight=20 "${properties[@]}" "${command[@]}"
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
