"""Offline validation: real-data risk replay, shadow A/B of the learning loop, acceptance.

The replay tests use recorded market data shipped in this repository
(data/*.csv: 2019 ETF daily closes; data/XYZ_1Min.csv: a US index future's
minute bars through the 2020 crash). No network, broker or model inference.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.quant import QuantPolicy
from scripts.autonomous_fund.risk import Intent, Limits, Price, RiskError, number
from scripts.autonomous_fund.validation import (
    ValidationSettings, acceptance, daily_from_minutes, load_closes, paired_test, risk_replay, shadow_pairs,
)

DATA = Path(__file__).resolve().parents[1] / "data"


def crash():
    minutes = pd.read_csv(DATA / "XYZ_1Min.csv")
    return {"XYZ": daily_from_minutes(zip(minutes["time"], minutes["close"]))}


def calm():
    return load_closes(DATA, ["SPY", "TLT", "GLD", "IEF", "DJP"])


# ----- Real-data risk replay ---------------------------------------------------------------

def test_2020_crash_volatility_target_and_drawdown_budget_halve_the_drawdown():
    limits = Limits(max_position="0.70")
    policy = QuantPolicy.from_config({"max_name_risk": "1", "max_etf_name_risk": "1"}, {"XYZ": "unlevered_etf"}, limits)
    result = risk_replay(crash(), {"XYZ": .70}, policy, limits)
    raw, managed = result["unconstrained"], result["risk_managed"]
    assert raw["max_drawdown"] > .24 and raw["annual_vol"] > .4  # The real crash: ~25% drawdown at 70% exposure.
    assert managed["max_drawdown"] <= .55 * raw["max_drawdown"]
    assert managed["annual_vol"] <= 1.25 * float(policy.target_portfolio_vol)  # EWMA lags the onset a little.
    assert managed["worst_day"] > .5 * raw["worst_day"]
    assert {"volatility_target", "exposure_budget"} <= set(result["binding_days"])


def test_2020_crash_full_default_policy_keeps_the_drawdown_inside_the_hard_latch():
    limits = Limits(max_position="0.70")
    policy = QuantPolicy.from_config(None, {"XYZ": "unlevered_etf"}, limits)
    managed = risk_replay(crash(), {"XYZ": .70}, policy, limits)["risk_managed"]
    assert managed["max_drawdown"] < float(limits.drawdown)


def test_2019_calm_market_risk_layer_costs_almost_nothing_with_shipped_limits():
    config = json.loads((DATA.parent / "scripts/autonomous_fund/config.json").read_text(encoding="utf-8"))
    limits = Limits(**config["limits"])
    section = {k: v for k, v in config["quant_risk"].items() if k != "groups"}
    targets = {s: .10 for s in ("SPY", "TLT", "GLD", "IEF", "DJP")}
    policy = QuantPolicy.from_config(section, {s: "unlevered_etf" for s in targets}, limits)
    result = risk_replay(calm(), targets, policy, limits)
    assert result["binding_days"] == {}
    assert result["return_cost"] < .003 and abs(result["vol_reduction"]) < .001


def test_etf_name_budget_fixes_the_calm_market_return_drag_found_on_real_data():
    limits = Limits(max_position="0.25")
    targets = {"SPY": .25, "TLT": .15, "GLD": .15, "IEF": .10, "DJP": .05}
    as_stocks = risk_replay(calm(), targets, QuantPolicy.from_config(None, {s: "stock" for s in targets}, limits), limits)
    as_etfs = risk_replay(calm(), targets, QuantPolicy.from_config(None, {s: "unlevered_etf" for s in targets}, limits), limits)
    assert as_stocks["return_cost"] > .015  # A single-stock budget cut broad ETFs for most of 2019.
    assert as_etfs["return_cost"] < .003
    assert as_etfs["risk_managed"]["max_drawdown"] <= as_stocks["risk_managed"]["max_drawdown"] + .001


def test_replay_is_look_ahead_free():
    limits = Limits(max_position="0.70")
    policy = QuantPolicy.from_config(None, {"XYZ": "unlevered_etf"}, limits)
    full = crash()
    cut = {"XYZ": full["XYZ"][:-10]}
    shocked = {"XYZ": full["XYZ"][:-10] + [(d, c * .5) for d, c in full["XYZ"][-10:]]}
    a = risk_replay(cut, {"XYZ": .7}, policy, limits)["daily"]
    b = risk_replay(shocked, {"XYZ": .7}, policy, limits)["daily"]
    assert [row["exposure"] for row in a] == [row["exposure"] for row in b[:len(a)]]  # Future crash changes nothing earlier.


def test_history_loader_accepts_yahoo_exports_and_minute_bars(tmp_path):
    assert calm()["SPY"][0][0] == "2019-01-02" and len(calm()["SPY"]) == 252
    days = crash()["XYZ"]
    assert days[0][0] == "2020-01-02" and all(len(d) == 10 for d, _ in days)
    (tmp_path / "BAD.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    with pytest.raises(RiskError, match="close column"):
        load_closes(tmp_path, ["BAD"])
    with pytest.raises(RiskError, match="Missing"):
        load_closes(tmp_path, ["NONE"])


def test_validate_risk_command_writes_results_and_refuses_targets_over_limits(tmp_path, capsys):
    from scripts.autonomous_fund import run
    run.main(["validate-risk", "--run-dir", str(tmp_path), "--data-dir", str(DATA),
              "--targets", "SPY=0.1,TLT=0.1"])
    assert '"risk_managed"' in capsys.readouterr().out
    saved = json.loads((tmp_path / "validation_risk.json").read_text(encoding="utf-8"))
    assert saved["window"]["from"].startswith("2019") and "daily" in saved
    with pytest.raises(RiskError, match="exceed"):
        run.main(["validate-risk", "--run-dir", str(tmp_path), "--data-dir", str(DATA), "--targets", "SPY=0.5"])


# ----- Shadow A/B of the learning loop -------------------------------------------------------

def test_paired_test_reports_effect_and_refuses_small_samples():
    stats = paired_test([.01, .02, .015, .012, .018], min_pairs=5)
    assert stats["sufficient"] and stats["t_stat"] > 5 and stats["win_rate"] == 1
    assert not paired_test([.01] * 4, min_pairs=5)["sufficient"]
    assert paired_test([], 5)["t_stat"] is None


def sessions(count):
    return pd.bdate_range("2025-01-02", periods=count, tz="America/New_York")


def closes_for(days, up, down):
    rows = {"UP": [], "DOWN": [], "SPY": []}
    for i, day in enumerate(days):
        rows["UP"].append((str(day.date()), 100 * (1 + up) ** i))
        rows["DOWN"].append((str(day.date()), 100 * (1 + down) ** i))
        rows["SPY"].append((str(day.date()), 100.0))
    return rows


def shadow_rows(days, memo_symbol, baseline_symbol, trading="memo"):
    rows = []
    for day in days:
        at = datetime(day.year, day.month, day.day, 15, tzinfo=timezone.utc).isoformat()
        memo = {"targets": [{"symbol": memo_symbol, "weight": .1}]}
        base = {"targets": [{"symbol": baseline_symbol, "weight": .1}]}
        shadow, traded = (base, memo) if trading == "memo" else (memo, base)
        rows.append({"arm": "baseline" if trading == "memo" else "memo", "trading_arm": trading, "decided_at": at,
                     "decision": shadow, "trading_decision": traded, "valid": True})
    return rows


def test_shadow_pairs_score_both_arms_identically_from_the_decision_close():
    days = sessions(30)
    data = closes_for(days, .001, -.001)
    differences = shadow_pairs(shadow_rows(days, "UP", "DOWN"), data)
    assert len(differences) == 29  # The last session has no completed next close yet.
    assert all(d > 0 for d in differences)
    reversed_roles = shadow_pairs(shadow_rows(days, "UP", "DOWN", trading="baseline"), data)
    assert reversed_roles == pytest.approx(differences)  # Always memo minus baseline.


def learning_harness(tmp_path, days, data):
    from .test_autonomous_fund_quant import harness
    frames = {s: rows for s, rows in data.items()}
    strategy = harness(tmp_path, frames)
    strategy.universe = frozenset({"UP", "DOWN", "SPY"})
    strategy.validation = ValidationSettings(shadow_enabled=True, min_pairs=20, switch_t_stat=2.0)
    strategy.memory_mode = "rolling"
    return strategy


def test_learning_champion_switches_to_baseline_only_on_significant_harm_then_back(tmp_path):
    days = sessions(40)
    data = closes_for(days, .001, -.001)
    strategy = learning_harness(tmp_path, days, data)
    now = datetime(2025, 3, 3, 21, tzinfo=timezone.utc)
    strategy.get_datetime = lambda: now
    for row in shadow_rows(days[:10], "DOWN", "UP"):
        strategy.journal.event(row["decided_at"], "shadow_decision", row)
    first = strategy._evaluate_learning(now)
    assert first["champion"] == "memo" and not first["memo_minus_baseline_1d_excess"]["sufficient"]
    for row in shadow_rows(days[10:30], "DOWN", "UP"):
        strategy.journal.event(row["decided_at"], "shadow_decision", row)
    second = strategy._evaluate_learning(now)
    assert second["champion"] == "baseline" and second["memo_minus_baseline_1d_excess"]["t_stat"] <= -2
    assert strategy.notices[-1][0] == "learning_champion_switched"
    assert strategy._champion() == "baseline"
    with strategy.journal.connection() as db:
        db.execute("DELETE FROM events WHERE kind='shadow_decision'")
    for row in shadow_rows(days[:35], "UP", "DOWN", trading="baseline"):
        strategy.journal.event(row["decided_at"], "shadow_decision", row)
    assert strategy._evaluate_learning(now)["champion"] == "memo"


def test_learning_starts_after_both_decisions_are_available(tmp_path):
    from datetime import date

    day = date(2026, 11, 27)
    dates = ("2026-11-27", "2026-11-30", "2026-12-01")
    data = {"SPY": list(zip(dates, (100, 100, 100))),
            "UP": list(zip(dates, (100, 110, 121))), "DOWN": list(zip(dates, (100, 95, 95)))}
    strategy = learning_harness(tmp_path, [day], data)
    strategy._daily_closes = lambda symbol, now: data[symbol]
    [row] = shadow_rows([day], "UP", "DOWN")
    strategy.journal.event("2026-11-27T19:00:00+00:00", "shadow_decision", row)
    monday = datetime(2026, 11, 30, 21, tzinfo=timezone.utc)
    tuesday = datetime(2026, 12, 1, 21, tzinfo=timezone.utc)
    assert strategy._evaluate_learning(monday)["memo_minus_baseline_1d_excess"]["pairs"] == 0
    assert strategy._evaluate_learning(tuesday)["memo_minus_baseline_1d_excess"]["pairs"] == 1


def test_shadow_runs_once_per_session_blind_and_within_budget(tmp_path):
    from .test_autonomous_fund_quant import NOW, account, model_harness
    strategy = model_harness(tmp_path)
    strategy.validation = ValidationSettings(shadow_enabled=True)
    strategy.memory_mode = "rolling"
    strategy._strategy_memory = lambda: {"version": 7, "review": {"strategy_memo": "learned"}}
    seen = []
    evidence = [{"source": "Synthetic", "available_at": (NOW - timedelta(days=1)).isoformat(),
                 "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]
    def call(name, **kwargs):
        seen.append((name, kwargs.get("context", {}).get("strategy_memory"), kwargs.get("context", {}).get("scorecards")))
        if name == "researcher":
            return {"summary": "s", "evidence": evidence}
        return {"as_of": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=2)).isoformat(),
                "next_review_at": (NOW + timedelta(hours=1)).isoformat(), "rationale": "r",
                "targets": [{"symbol": "ALFA", "weight": .05}]}
    strategy._agent_call = call
    strategy._decide(account(), NOW, False)
    assert [name for name, _, _ in seen] == ["researcher", "manager", "manager"]
    assert seen[1][1] == {"version": 7, "review": {"strategy_memo": "learned"}}  # Trading arm: memo.
    assert seen[2][1] is None and seen[2][2] is None  # Shadow arm: no memo, no scorecards.
    shadows = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "shadow_decision"]
    assert shadows[0]["arm"] == "baseline" and shadows[0]["valid"] and shadows[0]["memo_version"] == 7
    strategy.next_review = None
    strategy._decide(account(), NOW, False)
    assert [name for name, _, _ in seen].count("manager") == 3  # Still one shadow for this session.


def test_failed_shadow_never_affects_the_trading_decision(tmp_path):
    from .test_autonomous_fund_quant import NOW, account, model_harness
    strategy = model_harness(tmp_path)
    strategy.validation = ValidationSettings(shadow_enabled=True)
    strategy.memory_mode = "rolling"
    calls = []
    evidence = [{"source": "Synthetic", "available_at": (NOW - timedelta(days=1)).isoformat(),
                 "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]
    def call(name, **kwargs):
        calls.append(name)
        if name == "researcher":
            return {"summary": "s", "evidence": evidence}
        if calls.count("manager") == 2:
            raise TimeoutError("synthetic shadow timeout")
        return {"as_of": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=2)).isoformat(),
                "next_review_at": (NOW + timedelta(hours=1)).isoformat(), "rationale": "r",
                "targets": [{"symbol": "ALFA", "weight": .05}]}
    strategy._agent_call = call
    strategy._decide(account(), NOW, False)
    assert strategy.execution.active(NOW, strategy.universe, strategy.limits) is not None
    assert strategy.journal.get_meta("decision_failures", 0) == 0
    shadow = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "shadow_decision"][0]
    assert shadow["valid"] is False and shadow["error"] == "TimeoutError"


@pytest.mark.parametrize("settings", [{"shadow_enabled": "yes"}, {"min_pairs": 2}, {"switch_t_stat": 9},
                                      {"acceptance": {"unknown": 1}}, {"unknown": 1}])
def test_invalid_validation_settings_fail_explicitly(settings):
    with pytest.raises(RiskError):
        ValidationSettings.from_config(settings)


# ----- Acceptance report ------------------------------------------------------------------

def synthetic_run(tmp_path, sessions_count=3, unresolved=False):
    from scripts.autonomous_fund.execution import EXECUTION_REVISION, ExecutionState
    journal = Journal(tmp_path, "synthetic-acceptance")
    state = ExecutionState(journal)
    journal.event(datetime(2026, 11, 2, 13, tzinfo=timezone.utc), "startup",
                  {"mode": "local", "execution_revision": EXECUTION_REVISION, "execution": {"iteration_seconds": 60}})
    days = pd.bdate_range("2026-11-02", periods=sessions_count)
    with journal.connection() as db:
        for day in days:
            open_ = datetime(day.year, day.month, day.day, 14, 30, tzinfo=timezone.utc)
            for minute in range(0, 390):
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)",
                           ((open_ + timedelta(minutes=minute)).isoformat(), "iteration_finished", "{}"))
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", ((open_ + timedelta(minutes=5)).isoformat(),
                       "account", json.dumps({"cash": "9000", "equity": "10000", "positions": {}})))
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", ((open_ + timedelta(hours=7)).isoformat(),
                       "postmarket_review", json.dumps({"session_date": str(day.date())})))
    first = datetime(days[0].year, days[0].month, days[0].day, 15, tzinfo=timezone.utc)
    # A measured execution cost needs a market quote, not just a limit price.
    state.record_submission("intent-1", "decision-1", Intent("ALFA", "buy", 5, number("100.00")), first,
                            quote=Price(number(100), number(100), first, number(10000000)))
    journal.event(first, "order_submitted", {"intent_id": "intent-1", "broker_id": "b-1", "decision_id": "decision-1",
                                             "symbol": "ALFA", "side": "buy", "quantity": 5, "limit": "100.00"})
    journal.event(first, "fill", {"broker_id": "b-1", "symbol": "ALFA", "side": "buy", "quantity": "5", "price": "100.05"})
    journal.event(first, "agent", {"name": "manager", "parse_error": None})
    if unresolved:
        journal.reserve(Intent("ALFA", "buy", 1, number(100)), first, Limits())
    return journal


def test_acceptance_report_passes_a_clean_run_and_measures_slippage(tmp_path):
    synthetic_run(tmp_path)
    result = acceptance(tmp_path, {"min_sessions": 3}, now=datetime(2026, 11, 6, tzinfo=timezone.utc))
    assert result["verdict"] == "ready", result["criteria"]
    metrics = result["metrics"]
    assert metrics["sessions_observed"] == 3 and metrics["median_iteration_ratio"] == 1.0
    assert metrics["mean_slippage_bps_vs_arrival"] == pytest.approx(5.0)
    assert metrics["review_ratio"] == 1.0


def test_investment_acceptance_does_not_annualize_a_gap_as_one_day(tmp_path):
    from scripts.autonomous_fund.validation import investment_markdown

    journal = Journal(tmp_path, "synthetic-acceptance-gap")
    for at, equity in (("2026-11-24", 10_000), ("2026-11-27", 12_000), ("2026-11-30", 12_120)):
        journal.event(f"{at}T17:00:00+00:00", "account", {"equity": equity, "cash": equity, "positions": {}})
    result = acceptance(tmp_path, now=datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert result["metrics"]["sessions_observed"] == 3
    # Nov 25 has no account mark, Nov 26 is a holiday, and Nov 27 -> 30 is one session.
    assert result["performance_evidence"]["sessions"] == 1
    assert result["performance_evidence"]["missing_account_intervals"] == 2
    assert "跳过 2 个单日收益区间" in investment_markdown(result)


def test_acceptance_report_fails_on_short_runs_unresolved_orders_and_watchdog_stops(tmp_path):
    journal = synthetic_run(tmp_path, unresolved=True)
    for _ in range(4):
        journal.event(datetime(2026, 11, 3, 15, tzinfo=timezone.utc), "notification", {"kind": "watchdog_stop"})
    result = acceptance(tmp_path, now=datetime(2026, 11, 6, tzinfo=timezone.utc))
    failed = {row["criterion"] for row in result["criteria"] if not row["passed"]}
    assert result["verdict"] == "not_ready"
    assert {"min_sessions", "max_unresolved_intents", "max_watchdog_stops"} <= failed


def test_acceptance_counts_crashes_bookkeeping_errors_and_automatic_resumes(tmp_path):
    journal = synthetic_run(tmp_path)
    at = datetime(2026, 11, 3, 15, tzinfo=timezone.utc)
    journal.event(at, "notification", {"kind": "bookkeeping_error", "details": "export: OSError"})
    journal.event(at, "auto_resume", {"stop": {"class": "service", "reason": "service_start"}})
    clean = acceptance(tmp_path, {"min_sessions": 3}, now=datetime(2026, 11, 6, tzinfo=timezone.utc))
    assert clean["verdict"] == "ready"  # Recovered bookkeeping errors and restarts are reported, not fatal.
    assert clean["metrics"]["bookkeeping_errors"] == 1 and clean["metrics"]["auto_resumes"] == 1
    journal.event(at, "notification", {"kind": "bot_crash", "details": "RuntimeError"})
    crashed = acceptance(tmp_path, {"min_sessions": 3}, now=datetime(2026, 11, 6, tzinfo=timezone.utc))
    assert crashed["verdict"] == "not_ready" and crashed["metrics"]["bot_crashes"] == 1
    assert {row["criterion"] for row in crashed["criteria"] if not row["passed"]} == {"max_bot_crashes"}


def test_acceptance_command_writes_json_and_markdown(tmp_path, capsys):
    from scripts.autonomous_fund import run
    synthetic_run(tmp_path)
    run.main(["acceptance", "--run-dir", str(tmp_path)])
    assert json.loads((tmp_path / "acceptance.json").read_text(encoding="utf-8"))["verdict"] == "not_ready"
    assert "运行验收报告" in (tmp_path / "acceptance.md").read_text(encoding="utf-8")
    # V2 4.4: a separate investment report, and each says that passing one does not pass the other.
    investment = (tmp_path / "investment_acceptance.md").read_text(encoding="utf-8")
    assert investment.startswith("# 投资能力验收报告") and "样本不足，尚不能判断" in investment
    assert "运行验收通过不代表投资能力验收通过" in investment
    assert "运行验收通过不代表投资能力验收通过" in (tmp_path / "acceptance.md").read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out)["investment_verdict"] == "insufficient_evidence"
