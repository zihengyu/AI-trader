"""Fewer, better AI requests: a precomputed market snapshot, critique reuse and quiet-review skips.

Synthetic histories, a fake agent and neutral symbols only; no network or model.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.test_autonomous_fund_quant import NOW, UNIVERSE, accept, account, closes, default_frames, harness, rows


# ----- Market snapshot ------------------------------------------------------------------------

def test_the_snapshot_gives_returns_volatility_beta_trend_and_liquidity_from_completed_closes(tmp_path):
    frames = default_frames()
    frames["SPY"] = rows(closes(.01, 7))
    frames["ALFA"] = [(d, 2 * c - 100) for d, c in frames["SPY"]]  # Twice SPY's moves around 100.
    strategy = harness(tmp_path, frames)
    snapshot = strategy._market_snapshot(NOW)
    alfa, spy = snapshot["symbols"]["ALFA"], snapshot["symbols"]["SPY"]
    last, past = frames["ALFA"][-1][1], frames["ALFA"][-21][1]
    assert alfa["ret_20d"] == pytest.approx(last / past - 1, abs=1e-4)
    assert spy["beta_60d"] == pytest.approx(1.0, abs=1e-6) and alfa["beta_60d"] > 1.5
    assert alfa["vol_20d"] > spy["vol_20d"] > 0 and "pct_from_ma200" in alfa and alfa["adv_20d_usd"] is None
    assert snapshot["as_of_close"] == frames["SPY"][-1][0] and strategy._market_snapshot(NOW) is snapshot


def test_the_researcher_and_manager_are_told_to_use_the_snapshot():
    from scripts.autonomous_fund import strategy
    assert "market_snapshot" in strategy.RESEARCHER_PROMPT and "market_snapshot" in strategy.MANAGER_PROMPT


# ----- Critique reuse -------------------------------------------------------------------------

def critic_harness(tmp_path):
    from tests.test_autonomous_fund_construction import critic_strategy
    calls = []
    answer = SimpleNamespace(model_dump=lambda **kw: {"summary": "s", "concerns": [], "missing_evidence": []})

    def run(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(parsed=answer, parse_error=None, model="m/x", cache_hit=False, usage={}, summary="")
    fake, budget, now = critic_strategy(tmp_path, run=run)
    return fake, budget, now, calls


def test_a_critique_is_reused_while_the_research_and_holdings_are_unchanged(tmp_path):
    from scripts.autonomous_fund.strategy import AutonomousFund
    fake, budget, now, calls = critic_harness(tmp_path)
    research = {"summary": "pre-open research", "evidence": []}
    context = {"account": {"positions": {"ALFA": "5"}}}
    first = AutonomousFund._critique(fake, context, research, budget, now)
    again = AutonomousFund._critique(fake, context, research, budget, now + timedelta(minutes=30))
    assert again == first and len(calls) == 1
    assert any(r["kind"] == "critic_reused" for r in fake.journal.all_events())
    AutonomousFund._critique(fake, {"account": {"positions": {"ALFA": "9"}}}, research, budget, now)
    assert len(calls) == 2  # Holdings changed: a fresh critique.
    AutonomousFund._critique(fake, {"account": {"positions": {"ALFA": "9"}}}, research, budget, now + timedelta(days=1))
    assert len(calls) == 3  # A new session: a fresh critique.


# ----- Quiet reviews --------------------------------------------------------------------------

def quiet_strategy(tmp_path):
    strategy = harness(tmp_path, default_frames(), mode="local")
    strategy._triggers = []
    held = account(9100, {"ALFA": 9})
    strategy.journal.observe_session(NOW, held)
    active = accept(strategy, [("ALFA", .09)])
    strategy.execution.complete(active["id"], NOW)
    strategy.journal.set_meta("allocation_complete", {"decision_id": active["id"], "positions": {"ALFA": "9"}})
    strategy.journal.set_meta("last_model_decision", {"at": NOW.isoformat(), "day": "2026-11-02"})
    return strategy, held


def test_a_scheduled_review_with_nothing_new_skips_the_model_call(tmp_path):
    strategy, held = quiet_strategy(tmp_path)
    later = NOW + timedelta(minutes=30)
    assert strategy._quiet_review(held, later) is True
    assert strategy.next_review == later + timedelta(minutes=strategy.model_budget.min_review_minutes)
    assert any(r["kind"] == "review_skipped" for r in strategy.journal.all_events())


@pytest.mark.parametrize("change", ["trigger", "positions", "too_long", "new_day", "incomplete", "disabled"])
def test_any_change_still_calls_the_manager(tmp_path, change):
    from scripts.autonomous_fund.strategy import ModelBudget
    strategy, held = quiet_strategy(tmp_path)
    later = NOW + timedelta(minutes=30)
    if change == "trigger":
        strategy._triggers = [{"kind": "alert", "symbol": "ALFA"}]
    elif change == "positions":
        held = account(9900, {"ALFA": 1})  # A stop-loss or another fill changed the holdings.
    elif change == "too_long":
        later = NOW + timedelta(minutes=strategy.model_budget.max_quiet_minutes)
    elif change == "new_day":
        later = NOW + timedelta(days=1)
    elif change == "incomplete":
        accept(strategy, [("ALFA", .05)])  # A newer target is still being executed.
    elif change == "disabled":
        strategy.model_budget = ModelBudget(skip_quiet_reviews=False)
    assert strategy._quiet_review(held, later) is False


def test_quiet_review_settings_validate():
    from scripts.autonomous_fund.risk import RiskError
    from scripts.autonomous_fund.strategy import ModelBudget
    assert ModelBudget().max_quiet_minutes == 120 and ModelBudget().skip_quiet_reviews is True
    with pytest.raises(RiskError):
        ModelBudget(max_quiet_minutes=5)  # Below the review floor.


def test_a_symbol_without_history_is_listed_not_filled_and_the_snapshot_is_not_an_error(tmp_path):
    from scripts.autonomous_fund.rehearsal import _snapshot_judge
    frames = default_frames()
    del frames["DELTA"]
    strategy = harness(tmp_path, frames)
    snapshot = strategy._market_snapshot(NOW)
    assert snapshot["no_history"] == ["DELTA"] and "DELTA" not in snapshot["symbols"]
    assert _snapshot_judge(snapshot) == ("ok", "4 symbols, no history: DELTA")


def test_a_partial_snapshot_waits_for_the_next_iteration_instead_of_being_cached(tmp_path):
    strategy = harness(tmp_path, default_frames(), mode="local")
    strategy._may_prepare = lambda: False  # The iteration's preparation budget is spent.
    partial = strategy._market_snapshot(NOW)
    assert partial["deferred"] == sorted(UNIVERSE) and partial["symbols"] == {}
    strategy._may_prepare = lambda: True
    assert set(strategy._market_snapshot(NOW)["symbols"]) == UNIVERSE and strategy._market_snapshot_cache
