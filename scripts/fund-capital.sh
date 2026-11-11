#!/usr/bin/env bash
# Subscriptions and redemptions of the AI fund: add money to it or take money out.
# Usage: bash scripts/fund-capital.sh [status|subscribe AMOUNT|redeem AMOUNT|cancel REQUEST_ID|deposit AMOUNT|withdraw AMOUNT]
#   status            (default) units, NAV per unit, waiting requests and recent subscriptions/redemptions
#   subscribe AMOUNT  add AMOUNT US dollars to the fund (local: virtual cash; paper/live: from reserved cash)
#   redeem AMOUNT     take AMOUNT US dollars out (cash first; otherwise a pro-rata slice of every holding)
#   cancel ID         cancel a waiting request
#   deposit AMOUNT    paper/live only, fund stopped: record money transferred INTO the broker account (reserved cash)
#   withdraw AMOUNT   paper/live only, fund stopped: record money transferred OUT of the broker account
# subscribe/redeem/cancel only queue a request while the fund keeps running: the fund applies it itself at
# its next valuation in a trading session, at the NAV per unit, so investing is not interrupted.
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo"
py="${AI_FUND_PYTHON:-$repo/.venv/bin/python}"
env_file="${AI_FUND_ENV:-$repo/.env}"
fund() { "$py" -m scripts.autonomous_fund.run "$@" --env-file "$env_file"; }
usage="用法：bash scripts/fund-capital.sh [status|subscribe 金额|redeem 金额|cancel 请求编号|deposit 金额|withdraw 金额]"

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
    fund "$1" --amount "$2" ;;
  *)
    echo "$usage" >&2; exit 2 ;;
esac
