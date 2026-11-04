#!/usr/bin/env bash
# One-step start of the AI paper fund on Ubuntu (systemd):
# check .env -> install/update the services (runner, monitor, data desk) -> start
# them -> wait for the independent monitor -> offer to resume trading when it is
# stopped by hand.
# Usage: bash scripts/fund-start.sh [-y] [--restart]
#   -y         resume without asking
#   --restart  also restart running services so updated code takes effect
#              (used by fund-restart.sh; a plain start leaves running services as they are)
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"
py="${AI_FUND_PYTHON:-$repo/.venv/bin/python}"
env_file="${AI_FUND_ENV:-$repo/.env}"
unit_dir="${AI_FUND_UNIT_DIR:-/etc/systemd/system}"
build_dir="${AI_FUND_UNIT_BUILD:-$repo/artifacts/fund-systemd}"
user="${AI_FUND_USER:-$(id -un)}"
# Runner, independent monitor and the always-on data desk (数据员).
units=(lumibot-fund-runner.service lumibot-fund-monitor.service lumibot-fund-desk.service)
# Installed with the services; the timer takes a daily journal snapshot after the close.
backup_units=(lumibot-fund-backup.service lumibot-fund-backup.timer)
# Replaced by the data desk (2026-11-03): removed when an earlier version installed it.
legacy_units=(lumibot-fund-prefetch.timer lumibot-fund-prefetch.service)
yes=0
restart=0
for arg in "$@"; do
  case "$arg" in
    -y) yes=1 ;;
    --restart) restart=1 ;;
    *) echo "用法：bash scripts/fund-start.sh [-y] [--restart]" >&2; exit 2 ;;
  esac
done
run() { if [[ -n "${AI_FUND_DRY_RUN:-}" ]]; then echo "+ $*"; else "$@"; fi; }
fund() { "$py" -m scripts.autonomous_fund.run "$@" --env-file "$env_file"; }

if [[ "$user" == "root" ]]; then
  echo "请用普通用户（例如 ubuntu）运行，不要加 sudo；脚本需要时会自己调用 sudo。" >&2; exit 1
fi
if [[ ! -x "$py" ]]; then
  echo "没有找到 ${py}：先创建虚拟环境并安装依赖（见 docs/AUTONOMOUS_FUND_UBUNTU.md）。" >&2; exit 1
fi
if [[ ! -f "$env_file" ]]; then
  cp scripts/autonomous_fund/.env.example "$env_file"
  chmod 600 "$env_file"
  echo "已创建 ${env_file}。先填写 DEEPSEEK_API_KEY、FUND_TELEGRAM_BOT_TOKEN、FUND_TELEGRAM_CHAT_ID，再重新运行本脚本："
  echo "  nano $env_file"
  exit 1
fi
chmod 600 "$env_file"

echo "1/4 检查配置……"
fund show-config >/dev/null  # A mistyped FUND_* name or an invalid value stops here with the reason.

echo "2/4 检查服务文件……"
"$py" -m scripts.autonomous_fund.deploy units --user "$user" --env-file "$env_file" --output-dir "$build_dir" >/dev/null
changed=0
for unit in "${units[@]}" "${backup_units[@]}"; do
  if ! cmp -s "$build_dir/$unit" "$unit_dir/$unit"; then
    run sudo install -m 644 "$build_dir/$unit" "$unit_dir/$unit"
    changed=1
  fi
done
for unit in "${legacy_units[@]}"; do
  if [[ -f "$unit_dir/$unit" ]]; then
    run sudo systemctl disable --now "$unit" 2>/dev/null || true
    run sudo rm -f "$unit_dir/$unit"
    changed=1
  fi
done
if (( changed )); then
  run sudo systemctl daemon-reload
  echo "    服务文件已安装／更新。"
fi

echo "3/4 启动服务……"
run sudo systemctl enable --now "${units[@]}" lumibot-fund-backup.timer
fresh=()
if (( restart )); then
  # enable --now leaves a running service on its old code; a restart loads the update.
  # Stopping writes a manual STOP, so trading resumes only through the step below.
  [[ -z "${AI_FUND_DRY_RUN:-}" ]] && fresh=(--fresh-after "$(date +%s)")
  run sudo systemctl restart "${units[@]}"
  echo "    已重启交易、监控和数据员三个服务（加载新代码）。"
fi

echo "4/4 等待独立监控就绪（最多 3 分钟，第一次启动时监控可能先重启一次）……"
ready=0
for _ in $(seq 1 36); do
  # Bash 3.2 treats an empty array as unset under nounset; omit absent options.
  if fund status --check-monitor ${fresh[@]+"${fresh[@]}"} >/dev/null 2>&1; then ready=1; break; fi
  [[ -n "${AI_FUND_DRY_RUN:-}" ]] && break
  sleep 5
done
echo
fund status || true
echo
if (( ! ready )); then
  echo "监控还没有就绪。查看原因：bash scripts/fund-logs.sh errors" >&2
  exit 1
fi

stop_class="$(fund status --stop-class)"
if [[ "$stop_class" == none ]]; then
  echo "交易正在运行。日常查看：bash scripts/fund-logs.sh"
elif [[ "$(fund status --needs-operator)" == yes ]]; then
  # Operator stops, faults that need a person, and STOPs whose automatic resumes ran out.
  if [[ "$stop_class" == operator ]]; then
    question="交易处于人工停止状态（首次启动、stop 或手动关闭），现在恢复交易吗？[y/N] "
  else
    question="交易因监控／服务停止且不会再自动恢复（原因见上面的“交易”一行）。确认原因已处理，现在恢复交易吗？[y/N] "
  fi
  answer=n
  if (( yes )); then
    answer=y
  elif [[ -t 0 ]]; then
    read -r -p "$question" answer
  fi
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    fund resume >/dev/null
    echo "已恢复交易。日常查看：bash scripts/fund-logs.sh"
  else
    echo "交易保持停止。准备好后运行：bash scripts/fund-start.sh -y"
  fi
else
  echo "交易暂停中，原因见上面的“交易”一行；满足条件（进程健康 3 分钟、没有不明订单）后会自动恢复。"
fi
