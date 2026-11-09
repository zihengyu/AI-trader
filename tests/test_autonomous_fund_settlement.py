"""Local book: acquired or delisted holdings are detected and settled to cash by the operator.

Synthetic clock, bars and books only; no network, broker or model inference.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from lumibot.entities import Asset
from scripts.autonomous_fund import ledger_audit
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import RiskError, number
from scripts.autonomous_fund.settlement import STALE_SESSIONS, sessions_after, settle_position, stale_holding
from tests.test_autonomous_fund_local import advance, setup_book, submit


def held_book(tmp_path):
    """A real local book holding 5 ALFA bought through the local broker."""
    clock, source, journal, broker = setup_book(tmp_path)
    # The opening book the strict replay starts from (the local runner records it at first start).
    journal.event(clock.value, "local_ledger_origin", {"cash": "10000", "receivables": "0", "positions": {}})
    key, order = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    journal.update(key, order.identifier, True, clock.value)  # The strategy's reconciliation ends the intent.
    with journal.connection() as db:
        assert db.execute("SELECT quantity FROM local_positions WHERE symbol='ALFA'").fetchone()[0] == 5
        cash = number(db.execute("SELECT cash FROM local_account").fetchone()[0])
    return clock, source, journal, broker, cash


def test_an_acquired_holding_settles_to_cash_and_the_strict_replay_reproduces_the_book(tmp_path):
    clock, _, journal, _, cash = held_book(tmp_path)
    record = settle_position(journal, clock.value, " alfa ", "54.20", "acquired for cash at 54.20")
    assert record["proceeds"] == "271.00" and record["quantity"] == "5" and record["date"] == "2026-11-02"
    with journal.connection() as db:
        assert number(db.execute("SELECT cash FROM local_account").fetchone()[0]) == cash + number("271.00")
        assert db.execute("SELECT quantity FROM local_positions WHERE symbol='ALFA'").fetchone()[0] == 0
        assert db.execute("SELECT amount FROM local_actions WHERE id='ALFA:2026-11-02:cash_settlement'").fetchone()[0] \
            == "54.20"
        kinds = [row[0] for row in db.execute("SELECT kind FROM events WHERE kind IN "
                                              "('local_corporate_action','operator_position_settled')")]
    assert kinds == ["local_corporate_action", "operator_position_settled"]
    result = ledger_audit.audit(tmp_path)
    assert result["status"] != "failed" and result["replayed"]["positions"] == {}
    assert number(result["replayed"]["cash"]) == cash + number("271.00")
    with pytest.raises(RiskError, match="holds no ALFA"):  # Settled once; nothing left to settle.
        settle_position(journal, clock.value, "ALFA", "54.20", "again")


def test_a_worthless_delisting_settles_at_zero(tmp_path):
    clock, _, journal, _, cash = held_book(tmp_path)
    settle_position(journal, clock.value, "ALFA", "0", "bankruptcy; shares cancelled")
    result = ledger_audit.audit(tmp_path)
    assert result["status"] != "failed" and number(result["replayed"]["cash"]) == cash


@pytest.mark.parametrize("amount, reason, message", [("-1", "x", ">= 0"), ("nan", "x", "finite"),
                                                     ("abc", "x", "finite"), ("10", " ", "--reason")])
def test_invalid_amounts_or_a_missing_reason_are_refused(tmp_path, amount, reason, message):
    clock, _, journal, _, _ = held_book(tmp_path)
    with pytest.raises(RiskError, match=message):
        settle_position(journal, clock.value, "ALFA", amount, reason)


def test_an_open_order_or_unresolved_intent_for_the_symbol_blocks_the_settlement(tmp_path):
    from scripts.autonomous_fund.risk import Intent, Limits
    clock, _, journal, broker, _ = held_book(tmp_path)
    submit(broker, journal, clock.value, side="sell", limit=99)  # Accepted, not yet filled.
    with pytest.raises(RiskError, match="open order for ALFA"):
        settle_position(journal, clock.value, "ALFA", "50", "acquired")
    clock, _, journal, _, _ = held_book(tmp_path / "second")
    journal.reserve(Intent("ALFA", "sell", 5, number(99)), clock.value, Limits())  # Reserved, never resolved.
    with pytest.raises(RiskError, match="unresolved order intent for ALFA"):
        settle_position(journal, clock.value, "ALFA", "50", "acquired")


def test_an_inconsistent_book_refuses_the_settlement_and_writes_nothing(tmp_path):
    clock, _, journal, _, _ = held_book(tmp_path)
    with journal.connection() as db:
        db.execute("UPDATE local_account SET cash=cash+1")  # Tampered: the replay no longer agrees.
    with pytest.raises(RiskError):
        settle_position(journal, clock.value, "ALFA", "50", "acquired")
    with journal.connection() as db:
        assert db.execute("SELECT quantity FROM local_positions WHERE symbol='ALFA'").fetchone()[0] == 5
        assert not db.execute("SELECT 1 FROM local_actions WHERE id LIKE '%cash_settlement'").fetchone()


def test_a_tampered_settlement_amount_fails_the_strict_replay(tmp_path):
    clock, _, journal, _, _ = held_book(tmp_path)
    settle_position(journal, clock.value, "ALFA", "54.20", "acquired for cash")
    with journal.connection() as db:
        db.execute("UPDATE local_actions SET amount='60' WHERE id LIKE '%cash_settlement'")
    codes = {row["code"] for row in ledger_audit.audit(tmp_path)["errors"]}
    assert "corporate_action_amount_mismatch" in codes


def test_paper_accounts_have_no_local_book_to_settle(tmp_path):
    journal = Journal(tmp_path, "synthetic-paper-account")
    with pytest.raises(RiskError, match="no local book"):
        settle_position(journal, datetime(2026, 11, 9, tzinfo=timezone.utc), "ALFA", "50", "acquired")


def test_the_operator_command_settles_from_the_account_directory(tmp_path, capsys):
    from scripts.autonomous_fund.run import settle_position as command
    from scripts.autonomous_fund.settings import ACCOUNT_COMMANDS
    held_book(tmp_path)
    command(SimpleNamespace(run_dir=tmp_path, symbol="ALFA", cash_per_share="54.20", reason="acquired"))
    assert "Settled 5 ALFA at 54.20 per share: 271.00 USD" in capsys.readouterr().out
    assert "settle-position" in ACCOUNT_COMMANDS
    with pytest.raises(RiskError, match="No existing account journal"):
        command(SimpleNamespace(run_dir=tmp_path / "missing", symbol="ALFA", cash_per_share="1", reason="x"))


NOW = datetime(2026, 11, 9, 21, 30, tzinfo=timezone.utc)  # After Monday's close.


def test_a_holding_is_stale_only_on_evidence_of_missing_bars():
    assert sessions_after("2026-11-06", NOW) == 1 and sessions_after("2026-11-09", NOW) == 0
    assert sessions_after("2026-10-27", NOW) == 9
    fresh = [("2026-11-04", 10.0, 1e6), ("2026-11-05", 10.0, 1e6)]
    assert stale_holding("ALFA", fresh, None, NOW) is None  # Two sessions without a bar: not yet.
    assert STALE_SESSIONS == 3 and stale_holding("ALFA", fresh[:1], None, NOW) is None  # Three: still within.
    old = [("2026-10-27", 10.0, 1e6)]
    assert stale_holding("ALFA", old, None, NOW)["sessions_without_bar"] == 9
    assert stale_holding("ALFA", [], None, NOW) == {"symbol": "ALFA", "last_bar": None, "sessions_without_bar": None}
    assert stale_holding("ALFA", [], "ConnectionError", NOW) is None  # A failed read is not evidence.


def stale_strategy(tmp_path, mode, end):
    from tests.test_autonomous_fund_quant import harness
    strategy = harness(tmp_path, mode=mode)
    strategy.broker = SimpleNamespace(_pull_positions=lambda _: [SimpleNamespace(asset=Asset("ALFA"), quantity=5),
                                                                 SimpleNamespace(asset=Asset("BETA"), quantity=0)])
    index = pd.bdate_range(end=end, periods=30, tz="America/New_York") + pd.Timedelta(hours=16)
    closes = np.linspace(50, 54, len(index))
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1e6}, index=index)
    strategy.get_historical_prices = lambda asset, length, timestep="day": SimpleNamespace(df=frame)
    return strategy


def test_the_review_reports_a_held_stock_without_bars_once_a_day_with_the_command(tmp_path):
    from scripts.autonomous_fund.notifications import readable
    strategy = stale_strategy(tmp_path, "local", "2026-10-27")
    assert strategy._check_stale_holdings(NOW)[0]["sessions_without_bar"] == 9
    strategy._bars = None
    strategy._check_stale_holdings(NOW + timedelta(hours=1))  # Same New York day: told once.
    notices = [details for kind, details in strategy.notices if kind == "holding_without_market_data"]
    assert len(notices) == 1 and notices[0]["symbol"] == "ALFA" and notices[0]["mode"] == "local"
    text = readable("holding_without_market_data", notices[0], 7)
    assert "持仓 ALFA 已连续 9 个交易日没有新的日线（最后一根是 2026-10-27）" in text
    assert "settle-position --run-dir <账户目录> --symbol ALFA --cash-per-share" in text
    strategy._bars = None
    strategy._check_stale_holdings(NOW + timedelta(days=1))  # The next day it is repeated while unresolved.
    assert len([k for k, _ in strategy.notices if k == "holding_without_market_data"]) == 2


def test_paper_holdings_are_reported_without_the_local_command_and_fresh_ones_not_at_all(tmp_path):
    from scripts.autonomous_fund.notifications import readable
    strategy = stale_strategy(tmp_path / "paper", "paper", "2026-10-27")
    [found] = strategy._check_stale_holdings(NOW)
    text = readable("holding_without_market_data", found, 8)
    assert "模拟券商会自行处理" in text and "settle-position" not in text
    fresh = stale_strategy(tmp_path / "fresh", "local", "2026-11-09")
    assert fresh._check_stale_holdings(NOW) == [] and fresh.notices == []
    assert stale_strategy(tmp_path / "mock", "mock", "2026-10-27")._check_stale_holdings(NOW) == []
