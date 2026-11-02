"""Quantitative risk construction, exception-based approval and execution controls.

Synthetic, seeded histories and synthetic account identities only; no network,
no model inference and no broker connection.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.autonomous_fund.execution import ApprovalPolicy, ExecutionPolicy, ExecutionState, resolve_approval
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.quant import QuantPolicy, RiskEstimate, construct, drawdown_scale, stress_state
from scripts.autonomous_fund.risk import Account, Intent, Limits, Price, RiskError, number, plan


NOW = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)  # A regular NYSE session.
LIMITS = Limits()
UNIVERSE = {"ALFA", "BETA", "GAMMA", "DELTA", "SPY"}
DAYS = pd.bdate_range(end=NOW.date() - timedelta(days=1), periods=300, tz="America/New_York")


MARKET = np.random.default_rng(99).normal(0, .012, len(DAYS))


def closes(daily_vol, seed, shocks=(), beta=0.0):
    returns = beta * MARKET + np.random.default_rng(seed).normal(0, daily_vol, len(DAYS))
    for index, value in shocks:
        returns[index] = value
    return 100 * np.cumprod(1 + returns)


def rows(series):
    return [(str(day.date()), float(close)) for day, close in zip(DAYS, series)]


def histories(**vols):
    return {symbol: rows(closes(vol, seed)) for seed, (symbol, vol) in enumerate(sorted(vols.items()))}


def quote(price=100, volume=50_000_000):
    return Price(number(price), number(price), NOW, number(volume))


def account(cash=10000, positions=None, equity=None, price=100):
    positions = positions or {}
    value = sum(quantity * price for quantity in positions.values())
    return Account(number(cash), number(equity if equity is not None else cash + value), positions, NOW)


def decision(targets):
    return {"as_of": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=2)).isoformat(),
            "next_review_at": (NOW + timedelta(hours=1)).isoformat(), "rationale": "Synthetic plan",
            "targets": [{"symbol": s, "weight": w} for s, w in targets]}


def harness(tmp_path, frames=None, last=None, approval=None, quant=None, mode="mock"):
    from scripts.autonomous_fund.strategy import AutonomousFund, ModelBudget
    strategy = object.__new__(AutonomousFund)
    strategy.mode, strategy._name = mode, "quant-test"
    strategy.journal = Journal(tmp_path, "synthetic-quant-account")
    strategy.halt_path = tmp_path / "STOP"
    strategy.limits, strategy.session_orders = LIMITS, {}
    strategy.universe = frozenset(UNIVERSE)
    strategy.get_datetime = lambda: NOW
    strategy.notices = []
    strategy.notify = lambda kind, details: strategy.notices.append((kind, details))
    strategy.quant = QuantPolicy.from_config(quant, UNIVERSE, LIMITS)
    strategy.execution_policy, strategy.model_budget = ExecutionPolicy(), ModelBudget()
    strategy.execution = ExecutionState(strategy.journal, ApprovalPolicy.from_config(approval))
    strategy._history, strategy._estimates, strategy._notices, strategy._stress_cache = {}, {}, {}, {}
    strategy._overlay_logged, strategy._chase_noted = {}, set()
    frames = frames or {}
    data = {symbol: pd.DataFrame({"close": [c for _, c in series]},
                                 index=pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York") for d, _ in series]))
            for symbol, series in frames.items()}
    strategy.get_historical_prices = lambda asset, length, timestep="day": SimpleNamespace(df=data.get(asset.symbol))
    # By default the benchmark trades at its last completed close: no intraday stress.
    last = last if last is not None else {s: series[-1][1] for s, series in frames.items()}
    strategy.get_last_price = lambda asset: last.get(asset.symbol)
    return strategy


def default_frames(**overrides):
    vols = {"ALFA": .008, "BETA": .008, "GAMMA": .008, "DELTA": .008, "SPY": .006, **overrides}
    return histories(**vols)


def accept(strategy, targets):
    strategy.execution.accept(decision(targets), NOW, strategy.universe, LIMITS)
    return strategy.execution.active(NOW, strategy.universe, LIMITS)


# ----- Pure quantitative construction ------------------------------------------------

def test_volatility_target_and_expected_shortfall_only_scale_weights_down():
    # A common market factor: diversification cannot hide the correlated risk.
    frames = {s: rows(closes(.01, seed, beta=1.6)) for seed, s in enumerate(["ALFA", "BETA", "GAMMA", "DELTA", "SPY"])}
    policy = QuantPolicy.from_config({"max_name_risk": "0.5"}, UNIVERSE, LIMITS)
    estimate = RiskEstimate(frames, policy)
    targets = {s: number(".1") for s in ("ALFA", "BETA", "GAMMA", "DELTA", "SPY")}
    weights, report = construct(targets, {}, estimate, policy, LIMITS, LIMITS.max_exposure)
    assert "volatility_target" in report["binding"]
    assert report["constructed_metrics"]["annual_vol"] <= float(policy.target_portfolio_vol) + 1e-9
    assert report["constructed_metrics"]["es_975_1d"] <= float(policy.max_portfolio_es) + 1e-9
    assert all(weights[s] <= targets[s] for s in weights)
    assert report["requested_metrics"]["annual_vol"] > report["constructed_metrics"]["annual_vol"]


def test_volatility_adjusted_name_cap_limits_high_volatility_names():
    frames = histories(ALFA=.04, SPY=.006)
    policy = QuantPolicy.from_config(None, UNIVERSE, LIMITS)
    estimate = RiskEstimate(frames, policy)
    weights, report = construct({"ALFA": number(".1"), "SPY": number(".1")}, {}, estimate, policy, LIMITS, LIMITS.max_exposure)
    assert float(weights["ALFA"]) == pytest.approx(float(policy.max_name_risk) / estimate.vol["ALFA"], abs=1e-6)
    assert weights["ALFA"] < number(".1") and "name_risk:ALFA" in report["binding"]


def test_overlapping_group_caps_hold_simultaneously():
    groups = {"tech": {"limit": "0.15", "members": ["ALFA", "BETA"]},
              "growth": {"limit": "0.12", "members": ["BETA", "GAMMA"]}}
    policy = QuantPolicy.from_config({"groups": groups}, UNIVERSE, LIMITS)
    estimate = RiskEstimate(default_frames(), policy)
    weights, report = construct({s: number(".1") for s in ("ALFA", "BETA", "GAMMA")}, {}, estimate, policy,
                                LIMITS, LIMITS.max_exposure)
    assert weights["ALFA"] + weights["BETA"] <= number("0.15")
    assert weights["BETA"] + weights["GAMMA"] <= number("0.12")
    assert {"group:tech", "group:growth"} <= set(report["binding"])


def test_missing_history_can_be_held_or_reduced_but_never_increased():
    frames = default_frames()
    frames["GAMMA"] = frames["GAMMA"][-5:]  # Too short to estimate; never padded.
    policy = QuantPolicy.from_config(None, UNIVERSE, LIMITS)
    estimate = RiskEstimate(frames, policy)
    assert "GAMMA" in estimate.missing
    weights, report = construct({"GAMMA": number(".1"), "ALFA": number(".05")}, {"GAMMA": number(".04")},
                                estimate, policy, LIMITS, LIMITS.max_exposure)
    assert weights["GAMMA"] <= number(".04")
    assert "missing_history:GAMMA" in report["binding"]
    fresh, _ = construct({"GAMMA": number(".1")}, {}, estimate, policy, LIMITS, LIMITS.max_exposure)
    assert "GAMMA" not in fresh


def test_flat_series_uses_volatility_floor_and_conservative_perfect_correlation():
    flat = [(str(day.date()), 100.0) for day in DAYS]
    policy = QuantPolicy.from_config(None, UNIVERSE, LIMITS)
    estimate = RiskEstimate({"ALFA": flat, "BETA": flat}, policy)
    assert estimate.vol == {"ALFA": float(policy.min_annual_vol), "BETA": float(policy.min_annual_vol)}
    metrics = estimate.metrics({"ALFA": .05, "BETA": .05})
    assert metrics["annual_vol"] == pytest.approx(.1 * float(policy.min_annual_vol))  # Linear, not diversified.


def test_fat_historical_tail_binds_expected_shortfall_beyond_normal_estimate():
    crash = [(i, -.09) for i in range(40, 290, 25)]  # Repeated synthetic gap days.
    frames = {"ALFA": rows(closes(.004, 3, crash)), "SPY": rows(closes(.004, 4))}
    policy = QuantPolicy.from_config({"max_name_risk": "1", "target_portfolio_vol": "1", "max_portfolio_es": "0.005"},
                                     UNIVERSE, LIMITS)
    estimate = RiskEstimate(frames, policy)
    metrics = estimate.metrics({"ALFA": .1})
    assert metrics["es_historical"] > metrics["es_parametric"]
    weights, report = construct({"ALFA": number(".1")}, {}, estimate, policy, LIMITS, LIMITS.max_exposure)
    assert "expected_shortfall" in report["binding"]
    assert report["constructed_metrics"]["es_975_1d"] <= float(policy.max_portfolio_es) + 1e-9


def test_drawdown_exposure_budget_is_tiered_between_soft_level_and_hard_latch():
    policy = QuantPolicy.from_config(None, UNIVERSE, LIMITS)
    assert drawdown_scale("0", policy, LIMITS.drawdown) == 1
    assert drawdown_scale("0.04", policy, LIMITS.drawdown) == 1
    assert drawdown_scale("0.06", policy, LIMITS.drawdown) == number("0.625")
    assert drawdown_scale("0.08", policy, LIMITS.drawdown) == policy.drawdown_floor_scale
    assert drawdown_scale("0.2", policy, LIMITS.drawdown) == policy.drawdown_floor_scale


def test_market_stress_detects_drops_and_volatility_and_never_guesses_without_history():
    policy = QuantPolicy.from_config(None, UNIVERSE, LIMITS)
    calm = rows(closes(.005, 9))
    assert stress_state(calm, calm[-1][1], policy)["stressed"] is False
    assert "benchmark_intraday_drop" in stress_state(calm, calm[-1][1] * .96, policy)["reasons"]
    dropped = rows(closes(.005, 9, [(len(DAYS) - 1, -.04)]))
    assert "benchmark_prior_session_drop" in stress_state(dropped, dropped[-1][1], policy)["reasons"]
    wild = rows(closes(.03, 9))
    assert "benchmark_volatility" in stress_state(wild, wild[-1][1], policy)["reasons"]
    assert stress_state(calm[:5], None, policy)["stressed"] is None


@pytest.mark.parametrize("settings", [{"drawdown_soft": "0.08"}, {"target_portfolio_vol": "0"},
                                      {"groups": {"x": {"limit": "0.2", "members": ["OUTSIDE"]}}},
                                      {"unknown_setting": 1}, {"min_history_days": 500}, {"rebalance_band": "0.5"}])
def test_invalid_quantitative_policy_fails_explicitly(settings):
    with pytest.raises(RiskError):
        QuantPolicy.from_config(settings, UNIVERSE, LIMITS)


def test_planner_controls_only_shrink_orders_and_full_exit_always_proceeds():
    prices = {"ALFA": quote(), "BETA": quote(volume=25_000_000)}
    base = plan({"ALFA": number(".09")}, account(), prices, UNIVERSE, LIMITS, NOW)[0]
    budgeted = plan({"ALFA": number(".09")}, account(), prices, UNIVERSE, LIMITS, NOW, buy_budget=number(450))[0]
    assert budgeted.quantity == 4 < base.quantity
    assert not plan({"ALFA": number(".09")}, account(), prices, UNIVERSE, LIMITS, NOW, blocked_buys=frozenset({"ALFA"}))
    free = plan({"BETA": number(".09")}, account(), prices, UNIVERSE, LIMITS, NOW, participation=number("0.001"))[0]
    assert free.quantity == 8  # 0.1% of $25M average dollar volume does not bind.
    thin = plan({"BETA": number(".09")}, account(), prices, UNIVERSE, LIMITS, NOW, participation=number("0.00002"))[0]
    assert thin.quantity == 4  # $500 participation cap at a $100.10 limit.
    tiny = account(9910, {"ALFA": 9})
    assert not plan({"ALFA": number(".08")}, tiny, prices, UNIVERSE, LIMITS, NOW, min_trade=number(100))
    exit_all = plan({}, account(9900, {"ALFA": 1}), prices, UNIVERSE, LIMITS, NOW, min_trade=number(100))
    assert exit_all == [Intent("ALFA", "sell", 1, number("99.90"))]


# ----- Strategy: one deterministic path from targets to the next order ------------------

def test_full_liquidation_is_automatic_risk_reduction(tmp_path):
    strategy = harness(tmp_path, default_frames())
    active = accept(strategy, [])
    held = account(5500, {"ALFA": 45})
    result = strategy._candidates(active, held, {"ALFA": quote()}, NOW)
    assert result["orders"][0].side == "sell" and result["orders"][0].quantity == 45
    assert result["requirement"] is None and result["request"] is None


def test_portfolio_build_runs_automatically_to_the_ramp_then_one_plan_approval_extends_it(tmp_path):
    strategy = harness(tmp_path, default_frames())
    strategy.journal.observe_session(NOW, account())  # Session starts in cash.
    targets = [("ALFA", .09), ("BETA", .09), ("GAMMA", .09), ("DELTA", .09)]
    active = accept(strategy, targets)
    prices = {s: quote() for s, _ in targets}
    first = strategy._candidates(active, account(), prices, NOW)
    assert first["orders"][0].side == "buy" and first["requirement"] is None and first["request"] is None
    built = account(7500, {"ALFA": 9, "BETA": 9, "GAMMA": 7})  # 25% exposure, $2,500 spent.
    strategy.journal.observe_session(NOW, built)
    waiting = strategy._candidates(active, built, prices, NOW)
    assert waiting["orders"] == [] and waiting["request"]["reasons"] == ["exposure_ramp"]
    assert set(waiting["request"]["buys"]) == {"DELTA", "GAMMA"}
    assert not waiting["complete"]
    request = strategy.execution.request(active, waiting["request"], NOW)
    strategy.journal.event(NOW, "startup", {"mode": "local"})  # Operator CLI requires a local/paper book.
    resolve_approval(tmp_path, request["id"], request["digest"], True, NOW)
    approved = strategy._candidates(active, built, prices, NOW)
    assert approved["orders"][0].side == "buy" and approved["requirement"]["reasons"] == ["exposure_ramp"]


def test_market_stress_requires_approval_for_purchases_but_never_for_sales(tmp_path):
    frames = default_frames()
    frames["SPY"] = rows(closes(.006, 11, [(len(DAYS) - 1, -.045)]))
    strategy = harness(tmp_path, frames, {"SPY": frames["SPY"][-1][1]})
    strategy.journal.observe_session(NOW, account())
    active = accept(strategy, [("ALFA", .05)])
    buy = strategy._candidates(active, account(), {"ALFA": quote()}, NOW)
    assert buy["orders"] == [] and buy["request"]["reasons"] == ["market_stress"]
    assert buy["request"]["stress"]["reasons"] == ["benchmark_prior_session_drop"]
    held = account(9000, {"ALFA": 5, "BETA": 5})
    sale = strategy._candidates(accept(strategy, [("ALFA", .05)]), held, {"ALFA": quote(), "BETA": quote()}, NOW)
    assert sale["orders"][0] == Intent("BETA", "sell", 5, number("99.90")) and sale["request"] is None


def test_unknown_benchmark_state_is_treated_as_stress(tmp_path):
    frames = default_frames()
    del frames["SPY"]
    strategy = harness(tmp_path, frames)
    active = accept(strategy, [("ALFA", .05)])
    result = strategy._candidates(active, account(), {"ALFA": quote()}, NOW)
    assert result["orders"] == [] and "market_stress" in result["request"]["reasons"]


def test_daily_loss_limit_pauses_purchases_until_the_next_session(tmp_path):
    strategy = harness(tmp_path, default_frames())
    strategy.journal.observe_session(NOW - timedelta(days=3), account())  # Prior session close mark.
    session = strategy.journal.observe_session(NOW, account(9750), strategy.quant.daily_loss_limit)
    assert session["loss_paused"] and session["day_loss"] == number("0.025")
    active = accept(strategy, [("ALFA", .05)])
    result = strategy._candidates(active, account(9750), {"ALFA": quote()}, NOW)
    assert result["orders"] == [] and result["request"] is None and not result["complete"]
    with strategy.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='daily_loss_pause'").fetchone()[0] == 1
    tomorrow = NOW + timedelta(days=1)
    assert not strategy.journal.observe_session(tomorrow, account(9750), strategy.quant.daily_loss_limit)["loss_paused"]


def test_drawdown_throttle_reduces_exposure_and_sells_without_approval(tmp_path):
    strategy = harness(tmp_path, default_frames())
    strategy.journal.mark_equity(NOW, number(10000), LIMITS)
    held = account(5700, {"ALFA": 9, "BETA": 9, "GAMMA": 9, "DELTA": 9})  # Equity 9,300: 7% drawdown.
    strategy.journal.observe_session(NOW, held)
    active = accept(strategy, [(s, .1) for s in ("ALFA", "BETA", "GAMMA", "DELTA")])
    prices = {s: quote() for s in ("ALFA", "BETA", "GAMMA", "DELTA")}
    result = strategy._candidates(active, held, prices, NOW)
    assert "exposure_budget" in result["overlay"]["binding"]
    assert sum(result["weights"].values()) <= number("0.30625") + number("0.000001")
    assert result["orders"][0].side == "sell" and result["request"] is None


def test_chase_limit_blocks_a_purchase_that_moved_beyond_its_arrival_price(tmp_path):
    strategy = harness(tmp_path, default_frames())
    active = accept(strategy, [("ALFA", .05)])
    strategy.execution.record_submission("earlier-intent", active["id"], Intent("ALFA", "buy", 5, number("100.10")), NOW)
    result = strategy._candidates(active, account(), {"ALFA": quote(101)}, NOW)
    assert result["orders"] == [] and result["request"] is None and not result["complete"]
    within = strategy._candidates(active, account(), {"ALFA": quote("100.5")}, NOW)
    assert within["orders"][0].symbol == "ALFA"
    with strategy.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='buy_chase_limit'").fetchone()[0] == 1


def test_volatility_scaling_flows_into_order_size(tmp_path):
    strategy = harness(tmp_path, default_frames(ALFA=.03))
    active = accept(strategy, [("ALFA", .1)])
    result = strategy._candidates(active, account(), {"ALFA": quote()}, NOW)
    assert result["weights"]["ALFA"] < number(".1")
    assert result["orders"][0].quantity * result["orders"][0].limit <= result["weights"]["ALFA"] * 10000


def test_risk_history_excludes_current_and_future_sessions(tmp_path):
    frames = default_frames()
    frames["ALFA"] = frames["ALFA"] + [(str(NOW.date()), 1_000_000.0), (str((NOW + timedelta(days=1)).date()), 1e9)]
    strategy = harness(tmp_path, frames)
    assert all(day < str(NOW.date()) for day, _ in strategy._daily_closes("ALFA", NOW))


def test_stale_limit_order_is_canceled_for_repricing_but_fresh_one_is_left(tmp_path):
    from lumibot.entities import Order
    strategy = harness(tmp_path, default_frames())
    active = accept(strategy, [("ALFA", .05)])
    key = strategy.journal.reserve(Intent("ALFA", "buy", 5, number("100.10")), NOW, LIMITS)
    strategy.session_orders[key] = Order("quant-test", "ALFA", 5, "buy", limit_price=100.10, status="new")
    canceled = []
    strategy.cancel_order = canceled.append
    strategy.execution.record_submission(key, active["id"], Intent("ALFA", "buy", 5, number("100.10")),
                                         NOW - timedelta(seconds=100))
    strategy._manage_open_orders(NOW)
    assert canceled == []
    strategy.execution.record_submission(key, active["id"], Intent("ALFA", "buy", 5, number("100.10")),
                                         NOW - timedelta(seconds=200))
    strategy._manage_open_orders(NOW)
    assert len(canceled) == 1
    with strategy.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='order_reprice_cancel'").fetchone()[0] == 1


def model_harness(tmp_path):
    strategy = harness(tmp_path, default_frames())
    strategy.last_decision = None
    strategy.next_review = None
    strategy._strategy_memory = lambda: None
    strategy._strategy_evaluations = lambda: []
    strategy._risk_context = lambda account, now: {"synthetic": True}
    strategy._record_agent = lambda name, result, phase=None: result
    strategy._continue_allocation = lambda active: None
    return strategy


def test_failed_model_cycles_back_off_exponentially_and_success_resets(tmp_path):
    strategy = model_harness(tmp_path)
    calls = []
    def failing(name, **kwargs):
        calls.append(name)
        raise RiskError("Invalid structured agent answer")
    strategy._agent_call = failing
    delays = []
    for _ in range(4):
        with pytest.raises(RiskError):
            strategy._decide(account(), NOW, False)
        delays.append((strategy.next_review - NOW).total_seconds() / 60)
    assert delays == [5, 15, 60, 60] and calls == ["researcher"] * 4
    evidence = [{"source": "Synthetic", "available_at": (NOW - timedelta(days=1)).isoformat(),
                 "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]
    answers = {"researcher": {"summary": "s", "evidence": evidence},
               "manager": decision([("ALFA", .05)]) | {"next_review_at": (NOW + timedelta(minutes=2)).isoformat()}}
    strategy._agent_call = lambda name, **kwargs: answers[name]
    strategy._decide(account(), NOW, False)
    assert strategy.journal.get_meta("decision_failures") == 0
    assert strategy.next_review == NOW + timedelta(minutes=strategy.model_budget.min_review_minutes)


def test_daily_model_budget_stops_new_inference_but_keeps_reviews_reserved(tmp_path):
    strategy = model_harness(tmp_path)
    strategy._agent_call = lambda *args, **kwargs: pytest.fail("Budget must prevent inference")
    strategy.journal.counter("agent_runs", NOW, strategy.model_budget.max_agent_runs_per_day -
                             strategy.model_budget.reserved_review_runs - 1)
    strategy._decide(account(), NOW, False)
    assert strategy.notices[-1][0] == "model_budget_exhausted"
    assert strategy.journal.counter("agent_runs", NOW + timedelta(days=1)) == 0


def test_identical_notices_are_journaled_once_per_window(tmp_path):
    from scripts.autonomous_fund.strategy import AutonomousFund
    strategy = harness(tmp_path)
    strategy.log_message = lambda *args, **kwargs: None
    clock = [NOW]
    strategy.get_datetime = lambda: clock[0]
    for minute in range(40):
        clock[0] = NOW + timedelta(minutes=minute)
        AutonomousFund.notify(strategy, "blocked", "Stale account snapshot")
    AutonomousFund.notify(strategy, "blocked", "Different reason")
    with strategy.journal.connection() as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT body FROM events WHERE kind='notification'")]
    assert [r["details"] for r in rows] == ["Stale account snapshot", "Stale account snapshot", "Different reason"]


def test_session_uses_prior_close_equity_and_sums_observed_purchase_cash(tmp_path):
    journal = Journal(tmp_path, "synthetic-session")
    journal.observe_session(NOW - timedelta(days=3), account(10000))
    journal.observe_session(NOW - timedelta(days=3, hours=-6), account(9000, {"ALFA": 11}, price=100))  # Prior close.
    first = journal.observe_session(NOW, account(9000, {"ALFA": 11}))
    assert first["start_equity"] == number(10100) and first["cash_spent"] == 0
    journal.observe_session(NOW, account(8500, {"ALFA": 16}))
    journal.observe_session(NOW, account(9200, {"ALFA": 9}))  # A sale raises cash: not purchase turnover.
    assert journal.session(NOW)["cash_spent"] == number(500)
    assert journal.session(NOW + timedelta(days=1)) is None


def test_event_export_appends_incrementally_and_rebuilds_after_tampering(tmp_path):
    journal = Journal(tmp_path, "synthetic-export")
    journal.event(NOW, "startup", {"mode": "local"})
    assert len(journal.export()) == 1
    journal.event(NOW, "account", {"equity": "1"})
    assert [row["kind"] for row in journal.export()] == ["account"]
    exported = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in exported] == ["startup", "account"]
    (tmp_path / "events.jsonl").write_text("tampered\n", encoding="utf-8")
    assert len(journal.export()) == 2
    assert (tmp_path / "events.jsonl").read_text(encoding="utf-8").count("\n") == 2
    with journal.connection() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT name FROM sqlite_master WHERE name='events_kind_sequence'").fetchone()


def test_paper_open_order_check_queries_only_open_orders_and_fails_closed_on_truncation(tmp_path):
    from alpaca.trading.enums import QueryOrderStatus
    strategy = harness(tmp_path, mode="paper")
    seen = []
    def get_orders(filter):
        seen.append(filter)
        return [SimpleNamespace(id=str(i)) for i in range(count[0])]
    count = [3]
    strategy.broker = SimpleNamespace(api=SimpleNamespace(get_orders=get_orders),
                                      _parse_broker_orders=lambda raw, name, strategy_object=None: list(raw))
    assert len(strategy._orders()) == 3
    assert seen[0].status == QueryOrderStatus.OPEN
    count[0] = 500
    with pytest.raises(RiskError, match="truncated"):
        strategy._orders()


def test_configured_policy_sections_validate_and_ship_with_documented_defaults():
    from pathlib import Path
    from scripts.autonomous_fund.strategy import configuration, risk_settings
    path = Path(__file__).resolve().parents[1] / "scripts/autonomous_fund/config.json"
    config = configuration(path)
    quant, approval, execution, budget = risk_settings(config, set(config["universe"]), Limits(**config["limits"]))
    assert quant.target_portfolio_vol == number("0.12") and quant.max_portfolio_es == number("0.02")
    assert (quant.correlation_lookback_days, quant.correlation_lambda, quant.correlation_shrinkage) == (
        252, number("0.97"), number("0.25"))
    assert quant.max_scenario_loss == number("0.35") and quant.stress_scenarios == QuantPolicy().stress_scenarios
    assert approval.auto_daily_exposure_increase == "0.25" and execution.reprice_after_seconds == 180
    assert budget.min_review_minutes == 15 and Limits(**config["limits"]).daily_orders == 20
    groups = {name: set(members) for name, _, members in quant.groups}
    assert {"AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "QQQ", "XLK"} == groups["us_growth_megacap"]
    bad = json.loads(path.read_text(encoding="utf-8"))
    bad["approval"]["price_collar"] = "0.5"
    with pytest.raises(RiskError):
        risk_settings(bad, set(bad["universe"]), Limits(**bad["limits"]))
