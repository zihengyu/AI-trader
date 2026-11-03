#!/usr/bin/env bash
# Pre-open dress rehearsal of the AI paper fund (local mode), best run while the
# market is closed: one complete live decision on a COPY of the account, with
# real market data and the real models, stopped before any order. It runs under
# the trading service's own systemd sandbox (same filesystem permissions), so
# problems that only appear in the service, such as a read-only directory,
# show up here instead of at the open. The real account, its STOP, approvals
# and Telegram are not touched. Each stage reports its time and result.
# Usage: bash scripts/fund-rehearse.sh [--no-models]   (--no-models: data stages only, no model cost)
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"
py="${AI_FUND_PYTHON:-$repo/.venv/bin/python}"
env_file="${AI_FUND_ENV:-$repo/.env}"
user="${AI_FUND_USER:-$(id -un)}"
extra=()
for arg in "$@"; do
  case "$arg" in
    --no-models) extra+=(--no-models) ;;
    *) echo "用法：bash scripts/fund-rehearse.sh [--no-models]" >&2; exit 2 ;;
  esac
done
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
  echo "在交易服务相同的文件沙箱中彩排（以 $user 身份运行，可能需要几分钟；需要 sudo 启动一次临时任务）……"
  run sudo systemd-run --quiet --wait --pipe --collect --uid="$user" --gid="$(id -gn "$user")" \
    "${properties[@]}" "${command[@]}"
else
  echo "没有 systemd（或设置了 AI_FUND_NO_SANDBOX）：直接运行彩排，文件权限类问题不会暴露。"
  run "${command[@]}"
fi
