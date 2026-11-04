#!/usr/bin/env bash
# Restart the AI paper fund so updated code takes effect (fund-start.sh alone
# leaves already-running services on their old code).
# --update first pulls the current branch (fast-forward only) and reinstalls the
# pinned dependencies. Then: reinstall changed service files, restart the runner,
# monitor and data desk, wait for a monitor heartbeat newer than the restart, and offer to
# resume trading (a restart stops trading by hand, exactly like fund-stop.sh).
# Usage: bash scripts/fund-restart.sh [--update] [-y]   (-y: resume without asking)
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"
py="${AI_FUND_PYTHON:-$repo/.venv/bin/python}"
update=0
pass=()
for arg in "$@"; do
  case "$arg" in
    --update) update=1 ;;
    -y) pass+=(-y) ;;
    *) echo "用法：bash scripts/fund-restart.sh [--update] [-y]" >&2; exit 2 ;;
  esac
done
run() { if [[ -n "${AI_FUND_DRY_RUN:-}" ]]; then echo "+ $*"; else "$@"; fi; }

if [[ "$(id -un)" == "root" && -z "${AI_FUND_USER:-}" ]]; then
  echo "请用普通用户（例如 ubuntu）运行，不要加 sudo；脚本需要时会自己调用 sudo。" >&2; exit 1
fi
if (( update )); then
  echo "更新代码（只快进，不覆盖本地修改）……"
  run git -C "$repo" pull --ff-only
  echo "按锁定版本安装依赖……"
  run "$py" -m pip install -q -c scripts/autonomous_fund/constraints-py312-linux.txt \
    -e "$repo" -r scripts/autonomous_fund/requirements.txt
  echo "当前代码：$(git -C "$repo" log --oneline -1)"
fi
# Empty forwarded options must also work with Bash 3.2 and nounset.
exec bash "$repo/scripts/fund-start.sh" --restart ${pass[@]+"${pass[@]}"}
