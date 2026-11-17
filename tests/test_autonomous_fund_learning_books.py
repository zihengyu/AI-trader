"""Native selection uses current, costed, frozen-risk books; no provider calls."""
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.comparison import declaration, evaluate
from scripts.autonomous_fund.learning import (ACCOUNT_MODE, COST_SCOPE, evidence, independent_context,
    pack_estimate, pair_costs, snapshot, unpack_estimate)
from scripts.autonomous_fund.quant import QuantPolicy, RiskEstimate
from scripts.autonomous_fund.risk import Limits, instant, number
from scripts.autonomous_fund.scorecard import _exchange_sessions, _idea_session_close


def setup_trial(tmp_path, count=24, *, winner="baseline", missing_fee=False, missing_risk=False, turns=False):
    from .test_autonomous_fund_quant import harness
    strategy = harness(tmp_path)
    days = _exchange_sessions("2026-08-03", "2026-10-01")[:count]
    universe = {"ALFA": "stock", "BETA": "stock", "MARKET": "unlevered_etf"}
    policy = QuantPolicy.from_config({"stress_scenarios": {}, "stress_benchmark": "MARKET", "min_trade_notional": 0},
                                     universe, strategy.limits)
    p = declaration(asdict(strategy.limits), asdict(policy), universe,
                    [{"id": "fixed", "name": "fixed", "weights": {"ALFA": ".1"}}], fee_rate="0", slippage="0")
    strategy.comparison_protocol, strategy.prompt_version = p, "native-current"
    from scripts.autonomous_fund.validation import ValidationSettings
    strategy.validation = ValidationSettings(shadow_enabled=True, min_pairs=20)
    strategy.memory_mode = "rolling"
    strategy.universe = frozenset(universe)
    start = _idea_session_close(days[0]) - timedelta(hours=7)
    strategy.journal.event(start, "startup", {"mode": "local", "comparison_protocol": p})
    generation = {"protocol_id": p["id"], "prompt_version": strategy.prompt_version, "generation": "trial"}
    strategy.journal.event(start, "comparison_cohort_started", generation)
    strategy.journal.set_meta("comparison_generation", generation)
    history_days = _exchange_sessions("2026-05-01", days[0])
    estimate = RiskEstimate({s: [(day, 100.) for day in history_days] for s in universe}, policy)
    frozen = pack_estimate(estimate)
    for i, day in enumerate(days):
        before = _idea_session_close(day) - timedelta(hours=5)
        if not missing_risk or i != 3:
            strategy.journal.event(before, "comparison_risk", {"protocol_id": p["id"],
                "estimate": frozen, "earnings_caps": {}, "stressed": False, "complete": True})
        ids = {a: f"call-{i}-{a}" for a in ("memo", "baseline")}
        for arm, key in ids.items():
            strategy.journal.event(before, "agent", {"call_id": key, "name": "manager", "model": "synthetic",
                "purpose": "shadow", "cost_usd": None if missing_fee and i == 3 and arm == "memo" else .01})
        plans = {a: {"targets": [{"symbol": "ALFA" if a == "memo" else "BETA", "weight": ".1"}]} for a in ids}
        strategy.journal.event(before, "shadow_decision", {"valid": True, "arm": "baseline", "trading_arm": "memo",
            "decision": plans["baseline"], "trading_decision": plans["memo"], "comparison_equity": "10000",
            "comparison_protocol_id": p["id"], "comparison_prompt_version": strategy.prompt_version,
            "comparison_account_mode": ACCOUNT_MODE, "comparison_generation": "trial", "comparison_cost_scope": COST_SCOPE,
            "comparison_calls": {a: {"own": [key], "shared": []} for a, key in ids.items()}})
        close = _idea_session_close(day)
        prices = {"ALFA": 100 * (1 + (.002 if winner == "memo" else -.001)) ** i,
                  "BETA": 100 * (1 + (.002 if winner == "baseline" else -.001)) ** i, "MARKET": 100}
        if turns:
            prices.update(ALFA=100 * 1.003 ** min(i, 18) * .997 ** max(0, i - 18),
                          BETA=100 * .999 ** min(i, 18) * 1.001 ** max(0, i - 18))
        strategy.journal.event(close, "account", {"cash": 10000, "equity": 10000, "positions": {}, "as_of": str(close),
            "valuation": {"session_close": str(close), "book_existed_at_close": True},
            "benchmark_observations": {s: {"price": str(v), "as_of": str(close)} for s, v in prices.items()}})
    now = _idea_session_close(days[-1]) + timedelta(seconds=1)
    strategy.get_datetime = lambda: now
    return strategy, p, days, frozen


@pytest.mark.parametrize("winner,initial", [("baseline", "memo"), ("memo", "baseline")])
def test_native_champion_switches_both_directions_only_on_qualified_net_books(tmp_path, winner, initial):
    strategy, p, days, frozen = setup_trial(tmp_path, winner=winner)
    strategy.journal.set_meta("learning_champion", initial)
    result = strategy._evaluate_learning(strategy.get_datetime())
    assert result["champion"] == winner
    assert result["memo_minus_baseline_1d_excess"]["pairs"] == len(days) - 1
    assert result["qualification"] == "current_profile_risk_managed_linked_costs"
    assert strategy.notices[-1][0] == "learning_champion_switched"


def test_current_qualified_recent_cohort_can_roll_back_without_full_record_signal(tmp_path):
    strategy, p, days, frozen = setup_trial(tmp_path, count=40, turns=True)
    result = strategy._evaluate_learning(strategy.get_datetime())
    assert result["memo_minus_baseline_1d_excess"]["t_stat"] > -2
    assert result["recent"]["pairs"] == 20 and result["recent"]["t_stat"] < -2
    assert result["champion"] == "baseline" and result["reason"].startswith("degradation:")


@pytest.mark.parametrize("fault", ["fee", "risk", "version", "reentered_policy"])
def test_missing_cost_or_risk_and_other_profiles_cannot_vote(tmp_path, fault):
    strategy, p, days, _ = setup_trial(tmp_path, missing_fee=fault == "fee", missing_risk=fault == "risk")
    if fault == "version":
        strategy.prompt_version = "different-model"
    if fault == "reentered_policy":
        now = strategy.get_datetime()
        strategy.journal.event(now, "startup", {"comparison_protocol": {**p, "id": "other"}})
        strategy.journal.event(now, "startup", {"comparison_protocol": p})
    result = strategy._evaluate_learning(strategy.get_datetime())
    assert result["champion"] == "memo" and not result["memo_minus_baseline_1d_excess"]["sufficient"]
    assert result["memo_minus_baseline_1d_excess"]["pairs"] == 0


def test_current_new_model_does_not_borrow_an_older_model_sample(tmp_path):
    strategy, p, days, _ = setup_trial(tmp_path)
    strategy.prompt_version = "new-model"
    now = strategy.get_datetime()
    strategy.journal.event(now, "shadow_decision", {"valid": True, "arm": "baseline", "trading_arm": "memo",
        "decision": {"targets": []}, "trading_decision": {"targets": []}, "comparison_equity": "10000",
        "comparison_protocol_id": p["id"], "comparison_prompt_version": strategy.prompt_version})
    result = strategy._evaluate_learning(now)
    assert result["memo_minus_baseline_1d_excess"]["pairs"] == 0 and result["champion"] == "memo"


def test_shadow_reads_own_cash_positions_and_previous_plan(tmp_path):
    strategy, p, days, _ = setup_trial(tmp_path, count=3)
    context = {"account": {"cash": "1", "equity": "20000", "positions": {"TRADING_ONLY": 99}},
        "holding_math": {"risk_positions_usd": 19999, "positions": {"TRADING_ONLY": {"quantity": "99"}}},
        "previous_decision": {"targets": [{"symbol": "TRADING_ONLY", "weight": .1}]},
        "recent_trades": [{"symbol": "TRADING_ONLY"}], "risk": {"drawdown": ".5", "drawdown_exposure_budget": "999",
            "exposures": {"current": 99, "name_scores": {"BETA": {"beta": 1}}}},
        "fund_capital": {"pending_redemption": True}}
    result = independent_context(strategy, context, "baseline", strategy.get_datetime())
    # After the rise, daily whole-share rebalancing trims 10 shares to 9.
    assert result["account"]["positions"] == {"BETA": 9}
    assert result["account"]["cash"] != "1" and result["account"]["equity"] != "20000"
    assert result["previous_decision"]["targets"][0]["symbol"] == "BETA"
    assert result["fund_capital"] is None and result["recent_trades"] == []
    assert number(result["risk"]["drawdown"]) < number(".5")
    assert number(result["risk"]["drawdown_exposure_budget"]) <= number(".7")
    assert result["risk"]["exposures"]["current"]["beta"] < 1
    assert result["risk"]["exposures"]["name_scores"] == {"BETA": {"beta": 1}}
    assert result["account"]["position_average_prices"]["BETA"] == "100.0"
    # Derived account arithmetic must follow the shadow book too; copied
    # trading-account helpers would contradict the independent account above.
    assert set(result["holding_math"]["positions"]) == {"BETA"}
    assert result["holding_math"]["equity_usd"] == float(result["account"]["equity"])
    assert result["holding_math"]["estimated_exit_cost_usd"] is None
    assert context["account"]["positions"] == {"TRADING_ONLY": 99}  # Actual account is untouched.


def test_missing_evidence_starts_new_forward_cohort_without_erasing_old_gap(tmp_path):
    strategy, p, days, _ = setup_trial(tmp_path, count=6, missing_risk=True)
    before = snapshot(strategy.journal, strategy.get_datetime())
    result = independent_context(strategy, {"account": {"equity": "10000"}}, "baseline", strategy.get_datetime())
    assert result["account"]["positions"] == {} and result["comparison_account"]["generation"] != "trial"
    after = snapshot(strategy.journal, strategy.get_datetime())
    assert after["shadow"][0]["risk_managed"]["baseline"]["unavailable"]
    assert evidence(after, p["id"], strategy.prompt_version, 20)[0]["pairs"] == 0


def test_exact_shared_costs_are_halved_and_never_charged_twice():
    body = {"comparison_cost_scope": COST_SCOPE, "comparison_calls": {
        "memo": {"own": ["manager"], "shared": ["critic"]},
        "baseline": {"own": ["shadow"], "shared": ["critic"]}}}
    charged = set()
    assert pair_costs(body, {"manager": 1, "shadow": 2, "critic": 4}, charged) == {
        "memo": number(3), "baseline": number(4)}
    assert pair_costs(body, {"manager": 1, "shadow": 2, "critic": 4}, charged) == {"memo": 0, "baseline": 0}
    assert pair_costs(body, {"manager": 1, "shadow": 2}, set()) == {"memo": None, "baseline": None}


def test_risk_snapshot_changes_after_report_cutoff_cannot_rewrite_earlier_result(tmp_path):
    strategy, p, days, frozen = setup_trial(tmp_path, count=4)
    cutoff = _idea_session_close(days[2]) + timedelta(seconds=1)
    before = snapshot(strategy.journal, cutoff)
    strategy.journal.event(strategy.get_datetime(), "comparison_risk", {"protocol_id": p["id"],
        "estimate": {**frozen, "cov": [[1e6] * 3] * 3}, "complete": True})
    assert snapshot(strategy.journal, cutoff) == before


def test_frozen_native_quantitative_guard_changes_virtual_weights(tmp_path):
    strategy, p, days, frozen = setup_trial(tmp_path, count=3)
    before = snapshot(strategy.journal, strategy.get_datetime())
    strategy.journal.event(_idea_session_close(days[-1]) - timedelta(hours=1), "comparison_risk", {
        "protocol_id": p["id"], "estimate": frozen, "earnings_caps": {"ALFA": ".001"}, "complete": True})
    after = snapshot(strategy.journal, strategy.get_datetime())
    assert before["shadow"][0]["risk_managed"]["memo"]["last"]["positions"].get("ALFA", 0) > 0
    assert after["shadow"][0]["risk_managed"]["memo"]["last"]["positions"].get("ALFA", 0) == 0


def test_frozen_volatility_and_es_guards_are_native_and_do_not_see_future_packets(tmp_path):
    strategy, p, days, frozen = setup_trial(tmp_path, count=3)
    changed = dict(frozen)
    changed["vol"] = {s: 2.0 for s in frozen["symbols"]}
    changed["cov"] = [[4.0] * len(frozen["symbols"]) for _ in frozen["symbols"]]
    strategy.journal.event(_idea_session_close(days[-1]) - timedelta(hours=1), "comparison_risk", {
        "protocol_id": p["id"], "estimate": changed, "complete": True})
    row = snapshot(strategy.journal, strategy.get_datetime())["shadow"][0]["risk_managed"]["memo"]
    assert row["last"]["positions"].get("ALFA", 0) <= 1
    assert any("name_risk:ALFA" in r["binding"] for r in row["risk_overlays"])


def test_missing_risk_context_capture_never_blocks_actual_risk_context(tmp_path):
    strategy, p, days, frozen = setup_trial(tmp_path, count=2)
    original = strategy.journal.event
    def fail_capture(at, kind, body):
        if kind == "comparison_risk":
            raise ValueError("synthetic diagnostic write failure")
        return original(at, kind, body)
    strategy.journal.event = fail_capture
    policy = QuantPolicy(**p["quant_risk"])
    estimate = unpack_estimate(frozen, policy)
    strategy._capture_comparison_risk(estimate, {}, strategy.get_datetime())
    assert any(k == "comparison_risk_unavailable" for k, _ in strategy.notices)


def test_failed_paid_unpaired_attempt_cannot_be_hidden_by_successful_only_fees(tmp_path):
    strategy, p, days, frozen = setup_trial(tmp_path)
    strategy.journal.event(strategy.get_datetime(), "shadow_decision", {"valid": False,
        "comparison_protocol_id": p["id"], "comparison_prompt_version": strategy.prompt_version,
        "comparison_generation": "trial", "error": "synthetic timeout"})
    report = snapshot(strategy.journal, strategy.get_datetime())
    assert report["shadow"][0]["failed_pair_attempts"] == 1
    assert not report["shadow"][0]["risk_managed"]["memo"]["costs_complete"]
    assert strategy._evaluate_learning(strategy.get_datetime())["champion"] == "memo"


def test_earnings_gaps_remain_per_symbol_without_discarding_other_valid_risk_evidence(tmp_path):
    strategy, p, days, frozen = setup_trial(tmp_path, count=3)
    strategy.journal.event(_idea_session_close(days[-1]) - timedelta(hours=1), "comparison_risk", {
        "protocol_id": p["id"], "estimate": frozen, "earnings_unknown": ["BETA"], "complete": True})
    row = snapshot(strategy.journal, strategy.get_datetime())["shadow"][0]["risk_managed"]
    assert row["memo"]["risk_overlays"][-1]["complete"] is True
    assert row["baseline"]["risk_overlays"][-1]["complete"] is False


def test_unpriced_call_cohort_can_restart_forward_without_erasing_the_unknown_bill(tmp_path):
    strategy, p, days, frozen = setup_trial(tmp_path, count=6, missing_fee=True)
    result = independent_context(strategy, {"account": {"equity": "10000"}}, "baseline", strategy.get_datetime())
    assert result["comparison_account"]["generation"] != "trial"
    report = snapshot(strategy.journal, strategy.get_datetime())
    assert not report["shadow"][0]["risk_managed"]["memo"]["costs_complete"]
    assert evidence(report, p["id"], strategy.prompt_version, 20)[0]["pairs"] == 0
