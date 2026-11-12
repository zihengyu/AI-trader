"""Frozen regular-session closing marks and independent local-book reconciliation."""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from scripts.autonomous_fund.ledger_audit import audit
from scripts.autonomous_fund.local_broker import LocalPaperBroker
from scripts.autonomous_fund.market_data import SessionClock
from scripts.autonomous_fund.report import report
from scripts.autonomous_fund.risk import Limits, RiskError, number
from scripts.autonomous_fund.status import account_lines
from .test_autonomous_fund_local import setup_book, advance, submit
from .test_autonomous_fund_live_observations import configure


CLOSE = datetime(2026, 11, 2, 21, tzinfo=timezone.utc)


def closing_feed(clock, source, price=103):
    original = source.fetch
    def fetch(symbol, interval, period):
        if interval != "1m" or source.session.status()["open"]:
            return original(symbol, interval, period)
        end = source.session.last_close() - timedelta(minutes=1)
        index = pd.date_range(end=end, periods=8, freq="min")
        return pd.DataFrame({"Open": price, "High": price, "Low": price,
                             "Close": price, "Volume": 10000}, index=index)
    source.fetch = fetch
    return fetch


@pytest.mark.parametrize("holding", [False, True])
def test_cash_and_holdings_freeze_once_at_the_real_close_across_polls_and_restart(tmp_path, holding):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    if holding:
        submit(broker, journal, clock.value)
        advance(clock, source)
    broker.do_polling()
    previous = report(tmp_path, write_files=False)["latest_account"]
    closing_feed(clock, source)
    clock.value = CLOSE + timedelta(seconds=5)
    broker.do_polling()  # Existing intraday cache must not hide the final minute.
    closed = report(tmp_path, write_files=False)["latest_account"]
    assert closed["equity"] == pytest.approx(previous["cash"] + (515 if holding else 0))
    assert closed["as_of"] == str(CLOSE)
    assert closed["valuation"]["session_close"] == str(CLOSE)
    count = sum(row["kind"] == "account" for row in journal.all_events())
    clock.value += timedelta(hours=2)
    clock.price, clock.high = 900, 901  # Off-hours prices must not reprice this close.
    for _ in range(3):
        broker.do_polling()
    assert report(tmp_path, write_files=False)["latest_account"] == closed
    restored = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    configure(restored)
    restored.do_polling()
    assert sum(row["kind"] == "account" for row in journal.all_events()) == count
    assert "（休市）" in "\n".join(account_lines(closed, now=clock.value))
    clock.value = datetime(2026, 11, 3, 15, tzinfo=timezone.utc)
    source._cache.clear()
    clock.price, clock.high = 104, 104.1
    restored.do_polling()
    opened = report(tmp_path, write_files=False)["latest_account"]
    assert opened["as_of"] != closed["as_of"]
    assert opened["equity"] == pytest.approx(previous["cash"] + (520 if holding else 0))
    assert "（0秒前）" in "\n".join(account_lines(opened, now=clock.value))


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 11, 1, 17, tzinfo=timezone.utc), datetime(2026, 10, 30, 20, tzinfo=timezone.utc)),
    (datetime(2026, 11, 27, 19, tzinfo=timezone.utc), datetime(2026, 11, 27, 18, tzinfo=timezone.utc)),
])
def test_closing_boundary_follows_holidays_early_closes_and_daylight_saving(now, expected):
    assert SessionClock(now=lambda: now).last_close() == expected


def test_missing_final_minute_preserves_old_nav_then_recovers_without_a_fake_close(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    previous = report(tmp_path, write_files=False)["latest_account"]
    original = source.fetch
    clock.value = CLOSE + timedelta(seconds=5)
    def missing_closes(symbol, interval, period):
        frame = original(symbol, interval, period)
        # Keep both closing sources unavailable; a completed daily close is
        # now sufficient evidence even when the final minute is missing.
        return (frame[frame.index < CLOSE - timedelta(minutes=1)] if interval == "1m" else
                frame[frame.index.date < CLOSE.date()])
    source.fetch = missing_closes
    broker.do_polling()
    assert report(tmp_path, write_files=False)["latest_account"] == previous
    assert broker._valuation_error
    closing_feed(clock, source)
    clock.value += timedelta(seconds=31)
    broker.do_polling()
    assert report(tmp_path, write_files=False)["latest_account"]["as_of"] == str(CLOSE)


def test_recovered_closing_evidence_is_not_visible_before_it_was_observed(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    clock.value = CLOSE + timedelta(days=1, hours=-10)
    configure(broker)
    broker.do_polling()
    recovered = report(tmp_path, write_files=False)["latest_account"]
    # A newly created account cannot claim it existed at yesterday's close.
    assert recovered["valuation"]["session_close"] == str(CLOSE)
    assert recovered["valuation"]["book_existed_at_close"] is False
    assert report(tmp_path, write_files=False, as_of=CLOSE + timedelta(minutes=1))["latest_account"] is None


def traded_book(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)  # Establish a recorded opening balance before any execution.
    key, buy = submit(broker, journal, clock.value)
    from scripts.autonomous_fund.execution import ExecutionState
    from scripts.autonomous_fund.risk import Intent, Price
    ExecutionState(journal).record_submission(key, "neutral-plan", Intent("ALFA", "buy", 5, number(101)), clock.value,
        quote=Price(number(100), number(100), clock.value, number(10000000)))
    advance(clock, source)
    broker.do_polling()
    journal.update(key, buy.identifier, True, clock.value)
    assert audit(tmp_path)["status"] == "verified"
    return clock, source, journal, broker, buy


def test_round_trip_replays_cash_positions_fees_and_execution_price_inputs(tmp_path):
    clock, source, journal, broker, buy = traded_book(tmp_path)
    key, sell = submit(broker, journal, clock.value, side="sell", limit=99)
    from scripts.autonomous_fund.execution import ExecutionState
    from scripts.autonomous_fund.risk import Intent, Price
    ExecutionState(journal).record_submission(key, "neutral-plan", Intent("ALFA", "sell", 5, number(99)), clock.value,
        quote=Price(number(100), number(100), clock.value, number(10000000)))
    advance(clock, source)
    broker.do_polling()
    result = audit(tmp_path)
    assert result["status"] == "verified" and result["fills_checked"] == 2
    assert result["execution_inputs_verified"] == 2 and result["replayed"]["positions"] == {}
    cash = number(result["replayed"]["cash"])
    assert cash == number(10000) + (number(sell.avg_fill_price) - number(buy.avg_fill_price)) * 5 - number(buy.trade_cost) - number(sell.trade_cost)


@pytest.mark.parametrize("sql,code", [
    ("UPDATE local_account SET cash='9999'", "cash_mismatch"),
    ("UPDATE local_positions SET quantity=6", "position_quantity_or_average_mismatch"),
    ("UPDATE local_positions SET average='1'", "position_quantity_or_average_mismatch"),
    ("UPDATE local_fills SET fee='0'", "fill_event_mismatch"),
    ("UPDATE local_fills SET price='1'", "execution_price_mismatch"),
    ("UPDATE local_orders SET notional='1'", "order_fill_totals_mismatch"),
    ("DELETE FROM local_fills", "orphan_fill_events"),
])
def test_corrupt_ledger_is_reported_and_not_served_as_valid_account_information(tmp_path, sql, code):
    _, _, journal, broker, _ = traded_book(tmp_path)
    with journal.connection() as db:
        db.execute(sql)
    result = audit(tmp_path)
    assert result["status"] == "failed" and code in {r["code"] for r in result["errors"]}
    with pytest.raises(RiskError, match="reconciliation failed"):
        broker._get_balances_at_broker(None, "LocalTest")
    with pytest.raises(RiskError, match="reconciliation failed"):
        broker._pull_positions("LocalTest")
    summary = report(tmp_path, write_files=False)
    assert summary["ledger_audit"]["status"] == "failed"
    assert summary["latest_account"] is None and summary["recent_trades"] == []


def test_legacy_execution_evidence_is_explicitly_partial_instead_of_invented(tmp_path):
    _, _, journal, _, _ = traded_book(tmp_path)
    with journal.connection() as db:
        row = db.execute("SELECT sequence,body FROM events WHERE kind='fill'").fetchone()
        body = json.loads(row[1])
        body.pop("bar")
        body.pop("execution_assumptions")
        db.execute("UPDATE events SET body=? WHERE sequence=?", (json.dumps(body), row[0]))
    result = audit(tmp_path)
    assert result["status"] == "partial" and result["errors"] == []
    assert "historical_execution_inputs" in result["missing_evidence"]


def test_nav_mark_arithmetic_is_checked_independently_of_local_cash_tables(tmp_path):
    _, _, journal, _, _ = traded_book(tmp_path)
    with journal.connection() as db:
        row = db.execute("SELECT sequence,body FROM events WHERE kind='account' ORDER BY sequence DESC LIMIT 1").fetchone()
        body = json.loads(row[1])
        body["equity"] = "100000"
        db.execute("UPDATE events SET body=? WHERE sequence=?", (json.dumps(body), row[0]))
    # A derived snapshot is not the authoritative cash/fill ledger. Exclude
    # its bad value while allowing fresh, correctly priced trades to proceed.
    checked = audit(tmp_path)
    assert checked["status"] == "partial" and checked["errors"] == []
    assert "account_nav_arithmetic_mismatch" in {r["code"] for r in checked["warnings"]}
    assert report(tmp_path, write_files=False)["latest_account"] is None


def test_partial_fills_splits_and_dividend_entitlement_reconcile_after_restart(tmp_path):
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    clock.volume = 200
    key, _ = submit(broker, journal, clock.value)
    from scripts.autonomous_fund.execution import ExecutionState
    from scripts.autonomous_fund.risk import Intent, Price
    ExecutionState(journal).record_submission(key, "neutral-plan", Intent("ALFA", "buy", 5, number(101)), clock.value,
        quote=Price(number(100), number(100), clock.value, number(10000000)))
    # The first completed bar started before submission and cannot fill.
    for _ in range(4):
        advance(clock, source, 1)
        broker.do_polling()
    assert audit(tmp_path)["fills_checked"] == 3
    clock.value += timedelta(days=1)
    clock.actions = [(clock.value.date().isoformat(), 2, .4)]
    clock.price, clock.high = 50, 50.01
    source._cache.clear()
    broker.do_polling()
    result = audit(tmp_path)
    assert result["status"] == "verified", result
    assert number(result["replayed"]["positions"]["ALFA"]["quantity"]) == 10
    assert number(result["replayed"]["receivables"]) == 4
    restored = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    restored.do_polling()
    assert audit(tmp_path)["replayed"] == result["replayed"]


@pytest.mark.parametrize("holding", [False, True])
def test_closed_strategy_snapshot_does_not_republish_the_same_closing_nav(tmp_path, holding):
    from scripts.autonomous_fund.strategy import AutonomousFund, Mandate
    from lumibot.entities import Asset
    clock, source, journal, broker = setup_book(tmp_path)
    configure(broker)
    if holding:
        submit(broker, journal, clock.value)
        advance(clock, source)
        broker.do_polling()
    closing_feed(clock, source)
    clock.value = CLOSE + timedelta(minutes=1)
    broker.do_polling()
    original = report(tmp_path, write_files=False)["latest_account"]
    count = sum(row["kind"] == "account" for row in journal.all_events())
    strategy = object.__new__(AutonomousFund)
    strategy.mode, strategy.broker, strategy.journal = "local", broker, journal
    strategy._name = "ClosedSnapshot"
    strategy.limits, strategy.mandate = Limits(), Mandate(capital_usd=None)
    strategy.universe = strategy._configured_universe = frozenset({"ALFA"})
    strategy.quote_asset, strategy.cash_sweep = Asset("USD", asset_type="forex"), None
    strategy._orders = lambda: []
    strategy._set_cash_position = lambda value: None
    strategy.get_datetime = source.get_datetime
    for _ in range(2):
        clock.value += timedelta(minutes=10)
        strategy._snapshot()
    assert report(tmp_path, write_files=False)["latest_account"] == original
    assert sum(row["kind"] == "account" for row in journal.all_events()) == count


def test_audit_cli_is_read_only_and_uses_the_configured_account(tmp_path):
    import hashlib
    from .test_autonomous_fund_scripts import script
    traded_book(tmp_path)
    env = tmp_path / "audit.env"
    env.write_text(f"FUND_MODE=local\nFUND_RUN_DIR={tmp_path}\n")
    env.chmod(0o600)
    book = tmp_path / "state.sqlite3"
    digest = hashlib.sha256(book.read_bytes()).hexdigest()
    result = script("fund-logs.sh", tmp_path, env, "audit")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "verified"
    assert hashlib.sha256(book.read_bytes()).hexdigest() == digest
    env.write_text(f"FUND_MODE=local\nFUND_RUN_DIR={tmp_path / 'missing'}\n")
    result = script("fund-logs.sh", tmp_path, env, "audit")
    assert result.returncode == 2 and json.loads(result.stdout)["status"] == "unavailable"
    assert not (tmp_path / "missing").exists()


def test_native_account_tools_cannot_fall_back_to_cache_after_a_ledger_mismatch(tmp_path):
    from lumibot.components.agents.builtins import _bind_positions, _bind_portfolio
    from scripts.autonomous_fund.strategy import AutonomousFund
    _, _, journal, broker, _ = traded_book(tmp_path)
    with journal.connection() as db:
        db.execute("UPDATE local_account SET cash='9999'")
    strategy = object.__new__(AutonomousFund)
    strategy.mode, strategy.broker = "local", broker
    strategy._portfolio_value = 10000  # A plausible old cache must not be served.
    for tool in (_bind_positions(strategy, None), _bind_portfolio(strategy, None)):
        with pytest.raises(RiskError, match="reconciliation failed"):
            tool.function()
