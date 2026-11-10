"""Idle cash parking is system cash management: it outlives the AI decision it followed.

Incident (2026-11-05..09, local book): after the AI moved to cash, every parking order was canceled when
the short-lived hold-cash decision expired, one to three minutes after submission and before the virtual
broker could match a fresh minute bar. About fifty orders never filled, and on one day they used the whole
daily purchase budget. Synthetic accounts, neutral symbols and deterministic bars only.
"""
from datetime import timedelta

from scripts.autonomous_fund.risk import number
from .test_autonomous_fund_autonomy import decision_body
from .test_autonomous_fund_execution_policy_boundaries import native_fund, submitted  # noqa: F401
from .test_autonomous_fund_local import advance


def hold_cash(strategy, clock, minutes=2):
    """A hold-cash decision whose expiry is a few minutes away, as the AI often writes it."""
    body = decision_body([], when=clock())
    body["expires_at"] = (clock() + timedelta(minutes=minutes)).isoformat()
    strategy.execution.accept(body, clock(), strategy.universe, strategy.limits)
    return strategy.execution.active(clock(), strategy.universe, strategy.limits)


def test_a_parking_order_is_not_canceled_when_the_hold_cash_decision_expires(native_fund, monkeypatch):
    strategy, clock, source, journal, broker = native_fund()
    strategy._continue_allocation(hold_cash(strategy, clock), strategy._snapshot())
    order = submitted(strategy, journal)
    canceled = []
    monkeypatch.setattr(strategy, "cancel_order", lambda observed: canceled.append(observed.identifier))
    clock.value += timedelta(minutes=2, seconds=30)  # Past the decision's expiry, before a reprice is due.
    strategy._manage_open_orders(clock())
    assert canceled == []
    advance(clock, source, minutes=0)
    broker.do_polling()
    assert broker._pull_order(order.identifier, strategy.name).status == "fill"


def kinds(journal, kind):
    return [row["body"] for row in journal.all_events() if row["kind"] == kind]


def test_a_newer_ai_allocation_in_force_cancels_the_parking_order(native_fund, monkeypatch):
    strategy, clock, source, journal, broker = native_fund()
    strategy._continue_allocation(hold_cash(strategy, clock), strategy._snapshot())
    order = submitted(strategy, journal)
    canceled = []
    monkeypatch.setattr(strategy, "cancel_order", lambda observed: canceled.append(observed.identifier))
    body = decision_body([{"symbol": "ALFA", "weight": .05}], when=clock())
    newer = strategy.execution.accept(body, clock(), strategy.universe, strategy.limits)
    strategy._manage_open_orders(clock())
    assert canceled == [order.identifier]  # The cash is wanted for the new target.
    assert kinds(journal, "cash_parking_superseded")[-1]["decision_id"] == newer


def terminal_intent(strategy, journal, clock, symbol, side="buy"):
    from scripts.autonomous_fund.risk import Intent
    key = journal.reserve(Intent(symbol, side, 1, number("100")), clock(), strategy.limits)
    journal.update(key, None, True, clock())


def test_idle_iterations_park_without_a_new_decision_after_an_ai_free_session(native_fund):
    strategy, clock, source, journal, broker = native_fund()
    hold_cash(strategy, clock)
    latest = strategy.execution.latest(clock())["id"]
    park = strategy._park_idle_cash(strategy._snapshot(), clock(), latest)
    assert park is not None and submitted(strategy, journal).asset.symbol == "BILLS"


def test_cash_waits_for_the_next_session_after_an_ai_trade(native_fund):
    strategy, clock, source, journal, broker = native_fund()
    hold_cash(strategy, clock)
    latest = strategy.execution.latest(clock())["id"]
    terminal_intent(strategy, journal, clock, "ALFA", "sell")  # The AI sold today.
    assert strategy._park_idle_cash(strategy._snapshot(), clock(), latest) is None and journal.pending() == []
    assert strategy._park_idle_cash(strategy._snapshot(), clock(), latest) is None
    [note] = kinds(journal, "cash_sweep_unavailable")  # Said once, not every idle minute.
    assert "next session" in note["error"]
    strategy.cash_sweep = type(strategy.cash_sweep)("BILLS", min_idle_sessions=0)
    assert strategy._park_idle_cash(strategy._snapshot(), clock(), latest) is not None  # 0: park at once.


def test_parking_never_uses_more_than_three_purchase_submissions_a_day(native_fund):
    from scripts.autonomous_fund.cash import SWEEP_BUYS_PER_DAY
    strategy, clock, source, journal, broker = native_fund()
    hold_cash(strategy, clock)
    latest = strategy.execution.latest(clock())["id"]
    for _ in range(SWEEP_BUYS_PER_DAY):
        terminal_intent(strategy, journal, clock, "BILLS")
    assert strategy._park_idle_cash(strategy._snapshot(), clock(), latest) is None and journal.pending() == []
    assert "daily parking limit" in kinds(journal, "cash_sweep_unavailable")[-1]["error"]
    assert SWEEP_BUYS_PER_DAY < strategy.limits.daily_orders


def test_settings_and_the_policy_the_manager_sees():
    import pytest
    from scripts.autonomous_fund.cash import CashSweep, policy_context
    from scripts.autonomous_fund.risk import RiskError
    universe = {"ALFA": "stock", "BILLS": "cash_equivalent"}
    assert CashSweep.from_config(None, universe).min_idle_sessions == 1
    assert CashSweep.from_config({"min_idle_sessions": 0}, universe).min_idle_sessions == 0
    with pytest.raises(RiskError, match="min_idle_sessions"):
        CashSweep.from_config({"min_idle_sessions": 2}, universe)
    policy = policy_context(CashSweep("BILLS"))
    assert policy["min_idle_sessions"] == 1 and policy["max_parking_orders_per_day"] == 3
    assert policy["ai_target_allowed"] is False and policy["counts_as_cash"] is True


def test_the_manager_decision_context_carries_the_cash_policy(tmp_path):
    from datetime import timedelta as delta
    from scripts.autonomous_fund.cash import CashSweep
    from .test_autonomous_fund_quant import NOW, account, decision, model_harness
    strategy = model_harness(tmp_path)
    strategy.cash_sweep = CashSweep("RESERVE")
    seen = {}
    evidence = [{"source": "Synthetic", "available_at": (NOW - delta(days=1)).isoformat(),
                 "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]

    def call(name, **kwargs):
        seen[name] = kwargs.get("context")
        return {"summary": "s", "evidence": evidence} if name == "researcher" else decision([])
    strategy._agent_call = call
    strategy._decide(account(), NOW, False)
    assert seen["manager"]["cash_management"]["symbol"] == "RESERVE"
