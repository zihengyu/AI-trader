"""Capital writes recheck authority after database waits and price representable units.

Uses real journal transactions and native local snapshots with synthetic data;
Paper-mode tests replace only the broker boundary. No provider calls or inference.
"""
from contextlib import contextmanager
from datetime import timedelta

import pytest

from scripts.autonomous_fund import capital
from scripts.autonomous_fund.risk import RiskError, number
from .test_autonomous_fund_capital_flows import mandate_journal, running, sleeve
from .test_autonomous_fund_closed_ledger import CLOSE
from .test_autonomous_fund_quant import NOW


def after_statement(monkeypatch, journal, callback, statement="BEGIN IMMEDIATE"):
    """Advance the environment after the selected native database operation."""
    original, armed = journal.connection, [True]

    class Connection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, key):
            return getattr(self.db, key)

        def execute(self, query, *args):
            result = self.db.execute(query, *args)
            if armed[0] and query == statement:
                armed[0] = False
                callback()
            return result

    @contextmanager
    def connection():
        with original() as db:
            yield Connection(db)

    monkeypatch.setattr(journal, "connection", connection)
    return lambda: not armed[0]


@pytest.mark.parametrize("mode", ["local", "paper"])
@pytest.mark.parametrize("kind", ["subscription", "redemption"])
@pytest.mark.parametrize("change", ["stop", "closed", "stale", "future", "next_session"])
def test_a_flow_cannot_use_authority_from_before_the_database_wait(tmp_path, monkeypatch, mode, kind, change):
    broker = None
    if mode == "local":
        from .test_autonomous_fund_observation_timing import native_snapshot
        clock, _, journal, broker, strategy = native_snapshot(tmp_path, monkeypatch, capital="10000")
        journal = strategy.journal  # Same database, separate native Strategy/Broker Journal instances.
        if change == "closed":
            clock.value = CLOSE - timedelta(seconds=.5)
        account = strategy._snapshot()
        current = clock
    else:
        strategy = running(tmp_path)
        journal = strategy.journal
        moment = [CLOSE - timedelta(seconds=.5) if change == "closed" else NOW]
        current = lambda: moment[0]
        strategy.get_datetime = current
        strategy.broker.is_market_open = lambda: current() < CLOSE
        account = sleeve(10000)
        from dataclasses import replace
        account = replace(account, as_of=current())

    queued = capital.request(journal, current(), kind, "100")
    before_reserve = journal.reserve_cash()
    with journal.connection() as db:
        before_units = capital.fund_units(db)
        before_cash = db.execute("SELECT cash FROM local_account").fetchone()[0] if mode == "local" else None

    def changed():
        if change == "stop":
            strategy.halt_path.write_text("Synthetic operator stop")
        else:
            delay = {"closed": timedelta(seconds=1), "stale": timedelta(seconds=61),
                     "future": timedelta(seconds=-1), "next_session": timedelta(days=1)}[change]
            if mode == "local":
                clock.value += delay
            else:
                moment[0] += delay

    triggered = after_statement(monkeypatch, journal, changed)
    try:
        strategy._apply_capital_flow(queued, current(), account, account.cash)
        assert triggered(), "The real strategy's write transaction must reach the delayed-lock boundary"
        assert capital.pending(journal) == [queued]
        assert journal.reserve_cash() == before_reserve
        with journal.connection() as db:
            assert capital.fund_units(db) == before_units
            if mode == "local":
                assert db.execute("SELECT cash FROM local_account").fetchone()[0] == before_cash
        assert not [row for row in journal.all_events() if row["kind"] in (capital.FLOW, capital.CASH_FLOW)]
    finally:
        if broker is not None:
            broker._close_connection()


def test_a_still_authorized_flow_records_the_clock_after_the_write_wait(tmp_path, monkeypatch):
    strategy = running(tmp_path)
    moment = [NOW]
    strategy.get_datetime = lambda: moment[0]
    request = capital.request(strategy.journal, NOW, "subscription", "100")
    after_statement(monkeypatch, strategy.journal, lambda: moment.__setitem__(0, NOW + timedelta(seconds=2)))
    strategy._apply_capital_flow(request, NOW, sleeve(10000), number(10000))
    [row] = [row for row in strategy.journal.all_events() if row["kind"] == capital.FLOW]
    from scripts.autonomous_fund.risk import instant
    assert instant(row["at"]) == moment[0]
    assert not capital.pending(strategy.journal)


@pytest.mark.parametrize("change", ["stop", "stale"])
def test_authority_is_rechecked_after_unit_reads_and_before_any_mutation(tmp_path, monkeypatch, change):
    strategy = running(tmp_path)
    moment, broker_reads = [NOW], []
    strategy.get_datetime = lambda: moment[0]
    strategy.broker.is_market_open = lambda: broker_reads.append(moment[0]) or True
    request = capital.request(strategy.journal, NOW, "subscription", "100")

    def changed():
        if change == "stop":
            strategy.halt_path.write_text("Synthetic stop during unit lookup")
        else:
            moment[0] += timedelta(seconds=61)

    query = "SELECT body FROM events WHERE kind=? ORDER BY sequence DESC LIMIT 1"
    triggered = after_statement(monkeypatch, strategy.journal, changed, statement=query)
    strategy._apply_capital_flow(request, NOW, sleeve(10000), number(10000))
    assert triggered() and capital.pending(strategy.journal) == [request]
    assert strategy.journal.reserve_cash() == 40000
    assert broker_reads == [NOW]  # No broker/network status check while holding the write lock.


@pytest.mark.parametrize("kind", ["subscription", "redemption"])
@pytest.mark.parametrize("amount", ["0.01", "0.06"])
def test_cash_is_not_moved_when_units_round_away_or_misprice_the_amount(tmp_path, kind, amount):
    journal = mandate_journal(tmp_path, capital_usd="0.01")
    request = capital.request(journal, NOW, kind, amount)
    reserve = journal.reserve_cash()
    # NAV is $10m/unit, so one representable unit quantum is $0.10.
    # A cent rounds to no units; six cents rounds to ten cents of ownership.
    with pytest.raises(RiskError, match="precision"):
        capital.apply(journal, NOW, request["id"], equity=100000, cash=100000, mode="paper")
    assert capital.pending(journal) == [request] and journal.reserve_cash() == reserve
    assert not any(row["kind"] == capital.FLOW for row in journal.all_events())


@pytest.mark.parametrize("kind", ["subscription", "redemption"])
def test_a_representable_small_amount_preserves_the_unit_nav(tmp_path, kind):
    journal = mandate_journal(tmp_path, capital_usd="0.01")
    request = capital.request(journal, NOW, kind, "0.10")
    flow = capital.apply(journal, NOW, request["id"], equity=100000, cash=100000, mode="paper")
    assert number(flow["units_after"]) != number(flow["units_before"])
    assert number(flow["equity_after"]) / number(flow["units_after"]) == number("10000000")


def test_normal_cent_transfers_are_still_supported(tmp_path):
    journal = mandate_journal(tmp_path)
    equity = number(10000)
    for kind in ("subscription", "redemption"):
        request = capital.request(journal, NOW, kind, "0.01")
        flow = capital.apply(journal, NOW, request["id"], equity=equity, cash=equity, mode="paper")
        equity = number(flow["equity_after"])
        assert equity / number(flow["units_after"]) == 1
    assert equity == 10000 and journal.reserve_cash() == 40000
