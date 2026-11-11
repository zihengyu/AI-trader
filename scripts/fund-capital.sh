#!/usr/bin/env bash
# Subscriptions and redemptions of the AI fund: add money to it or take money out.
# Usage: bash scripts/fund-capital.sh [status|subscribe AMOUNT|redeem AMOUNT|cancel REQUEST_ID|deposit AMOUNT [--reserve-only]|withdraw AMOUNT]
#   status            (default) units, NAV per unit, net capital, waiting requests and recent subscriptions/redemptions
#   subscribe AMOUNT  add AMOUNT US dollars to the fund (local: virtual cash; paper/live: from reserved cash)
#   redeem AMOUNT     take AMOUNT US dollars out (cash first; otherwise a pro-rata slice of every holding)
#   cancel ID         cancel a waiting request
# subscribe/redeem/cancel work the same in every mode and only queue a request while the fund keeps running:
# the fund applies it itself at its next valuation in a trading session, at the NAV per unit.
# Live broker accounts only, when money really moves into or out of the broker (fund stopped first):
#   deposit AMOUNT    record money transferred IN and subscribe it (--reserve-only: keep it as reserved cash)
#   withdraw AMOUNT   record money transferred OUT of reserved cash (redeem it first)
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"
py="${AI_FUND_PYTHON:-$repo/.venv/bin/python}"
env_file="${AI_FUND_ENV:-$repo/.env}"
fund() { "$py" -m scripts.autonomous_fund.run "$@" --env-file "$env_file"; }
usage="用法：bash scripts/fund-capital.sh [status|subscribe 金额|redeem 金额|cancel 请求编号|deposit 金额 [--reserve-only]|withdraw 金额]"

need_value() {
  if [[ -z "${2:-}" ]]; then
    echo "$1 需要一个值。$usage" >&2
    exit 2
  fi
}

case "${1:-status}" in
  status)
    fund capital ;;
  subscribe|redeem)
    need_value "$1" "${2:-}"
    fund "$1" --amount "$2" ;;
  cancel)
    need_value "$1" "${2:-}"
    fund capital --cancel "$2" ;;
  deposit|withdraw)
    need_value "$1" "${2:-}"
    if systemctl is-active --quiet lumibot-fund-runner.service 2>/dev/null; then
      echo "登记券商转账前先停止基金：bash scripts/fund-stop.sh；转账到账后登记，再 bash scripts/fund-start.sh。" >&2
      echo "（基金运行时看到账户现金突然变化，会把转账误当成盈亏。）" >&2
      exit 2
    fi
    extra=()
    if [[ "$1" == deposit && "${3:-}" == --reserve-only ]]; then extra=(--reserve-only); fi
    fund "$1" --amount "$2" ${extra[@]+"${extra[@]}"} ;;
  *)
    echo "$usage" >&2; exit 2 ;;
esac
