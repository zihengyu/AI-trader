"""Open strategy research acceptance controls. Synthetic data, no paid inference or broker access."""
import copy
import json
import math
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.research_budget import ResearchBudget, ResearchBudgetSettings
from scripts.autonomous_fund.risk import RiskError, instant
from scripts.autonomous_fund.strategy_definition import StrategyLabSettings
from scripts.autonomous_fund.strategy_lab import StrategyLab, run_worker
from scripts.autonomous_fund.strategy_program import Evaluator, Warmup, parse
from scripts.autonomous_fund.strategy_validation import check_dataset, replay, validate

ZONE = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 16, 18, tzinfo=ZONE)


def policy():
    return {"limits": {"max_position": "1", "max_exposure": "1", "drawdown": ".5", "daily_orders": 20},
            "universe": {"ALFA": "stock", "BASE": "unlevered_etf"}, "groups": [], "cash_symbol": None,
            "max_leveraged_position": "0", "max_leveraged_total": "0", "min_trade_notional": "0",
            "fee_rate": ".0001", "slippage": ".0001"}


def proposal(key="synthetic-research", version="v1"):
    return {"strategy_id": key, "version": version, "name": "Synthetic stateful strategy",
            "hypothesis": "Synthetic test only: observe a stateful holding-period policy",
            "mechanism": "Open-ended event continuation; not a factor expression",
            "instruments": ["ALFA"], "market_conditions": "Synthetic observations only",
            "entry_logic": "Enter when an observed event flag is positive", "exit_logic": "Exit after three sessions",
            "sizing_logic": "Half the independent virtual capital", "holding_period": "Three sessions",
            "failure_conditions": "Predeclared drawdown or failed independent validation",
            "data_requirements": ["Raw daily prices, publication receipts and event flags"],
            "validation": {"benchmark": "BASE", "holding_sessions": 3, "train_sessions": 60,
                           "validation_sessions": 40, "final_sessions": 40, "forward_sessions": 40,
                           "min_independent_observations": 10, "max_drawdown": .2,
                           "rationale": "Synthetic predeclared test protocol"},
            "costs": {"fee_rate": .0001, "slippage": .0001, "daily_model_usd": 0., "daily_data_usd": 0.,
                      "basis": "Explicit synthetic cost assumptions"},
            "implementation": {"adapter": "daily_rules_v1", "rules": [
                {"symbol": "ALFA", "entry": 'field("ALFA", "event") > 0',
                 "exit": "holding_sessions >= 3", "weight": "0.5"}]}}


def dataset(count=170, *, start="2025-01-02", fail_after=None):
    import pandas_market_calendars as mcal
    schedule = mcal.get_calendar("NYSE").schedule(start_date=start, end_date="2026-11-16").iloc[:count]
    rng, values = random.Random(23), {"ALFA": 100., "BASE": 100.}
    bars = {s: [] for s in values}
    for i, (stamp, session) in enumerate(schedule.iterrows()):
        day, opened, closed = str(stamp.date()), session.market_open, session.market_close
        for symbol in values:
            previous = values[symbol]
            drift = (.006 if fail_after is None or i < fail_after else -.012) if symbol == "ALFA" else .0002
            values[symbol] *= 1 + drift + rng.uniform(-.002, .002)
            bars[symbol].append({"day": day, "at": closed.isoformat(), "open_at": opened.isoformat(),
                                 "available_at": closed.isoformat(), "open": previous, "close": values[symbol],
                                 "high": max(previous, values[symbol]) * 1.001,
                                 "low": min(previous, values[symbol]) * .999, "volume": 1000000,
                                 "event": 1.})
    return {"bars": bars, "actions": [], "source": "synthetic acceptance fixture",
            "coverage": dict.fromkeys(["raw_prices", "corporate_actions_complete", "point_in_time_membership",
                                        "publication_times_complete"], True)}


def lab(tmp_path, **settings):
    journal = Journal(tmp_path, "synthetic-lab-owner")
    return StrategyLab(journal, policy(), StrategyLabSettings(**settings))


def test_nonfactor_proposal_and_no_ideas_are_accepted_without_trading(tmp_path):
    from scripts.autonomous_fund.strategy import Research
    research = Research(summary="No new opportunity", evidence=[{
        "source": "Synthetic source", "available_at": None, "observed_at": NOW.isoformat(),
        "estimated": False, "estimate_reason": ""}])
    assert research.strategy_proposals == research.research_tasks == []
    engine = lab(tmp_path)
    candidate = proposal()
    engine.submit(candidate, NOW)
    assert engine.rows()[0]["state"] == "candidate"
    assert engine.context(NOW)["candidates"][0]["proposal"]["mechanism"] == candidate["mechanism"]
    assert engine.journal.pending() == []


def test_unsupported_mechanisms_remain_feasibility_tasks(tmp_path):
    engine, candidate = lab(tmp_path), proposal()
    candidate["implementation"] = {"adapter": "novel_path_dependent_model", "implementation_notes": "inert code text"}
    engine.submit(candidate, NOW)
    assert engine.rows()[0]["state"] == "needs_adapter"
    assert engine.schedule(dataset(), NOW) == []


def test_uncompiled_implementation_retains_the_complete_research_hypothesis(tmp_path):
    engine, candidate = lab(tmp_path), proposal()
    candidate["implementation"]["rules"][0]["entry"] = "unknown_mechanism("
    engine.submit(candidate, NOW)
    saved = engine.rows()[0]
    assert saved["state"] == "needs_formalization"
    assert json.loads(saved["proposal"])["hypothesis"] == candidate["hypothesis"]
    assert engine.schedule(dataset(), NOW) == []


def test_a_novel_validation_method_is_not_forced_into_daily_significance_tests():
    candidate = proposal()
    candidate["validation"]["method"] = "rare_event_prospective_protocol"
    result = validate(candidate, dataset(), policy())
    assert result["status"] == "needs_adapter" and "tests" not in result


def test_versions_are_immutable_and_failures_survive_revisions(tmp_path):
    engine, candidate = lab(tmp_path), proposal()
    engine.submit(candidate, NOW)
    engine.submit(candidate, NOW + timedelta(days=1))
    changed = copy.deepcopy(candidate)
    changed["entry_logic"] = "Changed rule"
    with pytest.raises(RiskError, match="cannot be rewritten"):
        engine.submit(changed, NOW)
    changed.update(version="v2", parent_version="v1", change_type="revision")
    engine.submit(changed, NOW + timedelta(days=1))
    assert len(engine.rows()) == 2
    with engine.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_strategy_events WHERE kind='registered'").fetchone()[0] == 2


def test_separate_candidates_and_cross_day_task_progress(tmp_path):
    engine = lab(tmp_path)
    engine.submit(proposal("first"), NOW)
    engine.submit(proposal("second"), NOW)
    engine.update_task({"strategy_id": "first", "version": "v1", "action": "request_data",
                        "findings": "Need dated features", "next_step": "Acquire publication receipts"}, NOW)
    reopened = lab(tmp_path)
    candidates = {r["id"]: r for r in reopened.context(NOW + timedelta(days=1))["candidates"]}
    assert candidates["first"]["last_task"]["next_step"] == "Acquire publication receipts"
    assert candidates["second"]["last_task"] is None
    assert all(r["state"] == "candidate" for r in candidates.values())


@pytest.mark.parametrize("expression", ['__import__("os")', 'field.__globals__', 'open("secret")',
                                         '[x for x in range(100)]', '2 ** 100000000', '(lambda: 1)()'])
def test_generated_programs_cannot_reach_files_network_or_brokers(expression):
    with pytest.raises(RiskError):
        parse(expression)


def test_no_future_rows_or_unpublished_feature_is_visible():
    data = dataset(3)
    rows = data["bars"]["ALFA"]
    now = rows[0]["at"]
    evaluator = Evaluator(data["bars"], now, {})
    assert evaluator.value(parse('field("ALFA", "close")')) == rows[0]["close"]
    rows[0]["feature_available_at"] = {"event": rows[1]["at"]}
    with pytest.raises(Warmup, match="not-yet-published"):
        evaluator.value(parse('field("ALFA", "event")'))
    with pytest.raises(RiskError, match="bounded"):
        evaluator.value(parse('field("ALFA", "close", -1)'))


def test_completed_signals_fill_only_on_the_next_session_open():
    data, candidate = dataset(10), proposal()
    days = [r["day"] for r in data["bars"]["BASE"]]
    result = replay(candidate, data, policy(), days[1:], initial_capital="10000")
    first = result["series"][0]
    trade = first["trades"][0]
    assert float(trade["price"]) == pytest.approx(data["bars"]["ALFA"][1]["open"] * 1.0001)
    assert first["positions"]["ALFA"] > 0
    assert any(r["trades"] and r["trades"][0]["side"] == "sell" for r in result["series"][1:])


def test_high_in_sample_return_does_not_override_failed_oos():
    data, candidate = dataset(fail_after=95), proposal()
    result = validate(candidate, data, policy())
    assert result["tests"]["train"]["strategy"]["net_return"] > 0
    assert result["tests"]["final"]["strategy"]["net_return"] < 0
    assert result["status"] == "rejected" and not result["passed"]


def test_missing_historical_coverage_never_qualifies():
    data = dataset()
    data["coverage"]["point_in_time_membership"] = False
    result = validate(proposal(), data, policy())
    assert result["status"] == "needs_data" and not result["passed"]
    assert "point_in_time_membership" in result["limitations"]


def test_resource_budget_is_atomic_monthly_and_unknown_usage_is_retained(tmp_path):
    journal = Journal(tmp_path, "synthetic-budget-owner")
    budget = ResearchBudget(journal, ResearchBudgetSettings(daily_usd="1", monthly_usd="1", project_usd="1"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        keys = list(pool.map(lambda _: budget.reserve(NOW, "project", "research_model", ".6"), range(4)))
    assert sum(k is not None for k in keys) == 1
    key = next(k for k in keys if k)
    budget.settle(key, NOW, None, {})
    assert budget.summary(NOW)["daily_committed_usd"] == .6
    assert budget.reserve(NOW + timedelta(days=1), "other", "validation_compute", ".6") is None
    budget.settle(key, NOW, ".2", {"receipt": "observed"})
    assert budget.reserve(NOW + timedelta(days=1), "other", "validation_compute", ".6")
    with pytest.raises(RiskError, match="cannot be rewritten"):
        budget.settle(key, NOW, ".1", {})
    assert journal.pending() == []  # Financial supervision is a separate owner.


def test_shared_daily_research_does_not_exhaust_a_lifetime_project_forever(tmp_path):
    from tests.test_autonomous_fund_premarket import harness
    strategy, calls = harness(tmp_path)
    strategy.scripted = False
    strategy.strategy_lab = StrategyLab(strategy.journal, policy(), budget_settings=ResearchBudgetSettings(project_usd="0.5"))
    for now in (NOW, NOW.replace(month=12, day=1)):
        strategy.get_datetime = lambda now=now: now
        strategy._agent_call("researcher", task_prompt="Synthetic monthly budget test", context={"as_of": now.isoformat()})
    assert len(calls) == 2
    starts = [r["body"] for r in strategy.journal.all_events() if r["kind"] == "agent_call_started"]
    assert {r["research_project"] for r in starts} == {"shared-research:2026-11", "shared-research:2026-12"}
    assert all(r["research_reservation"] for r in starts)


def test_unknown_execution_costs_and_unmodeled_cash_sweeps_block_admission():
    configured, data = policy(), dataset()
    configured["fee_rate"] = None
    result = validate(proposal(), data, configured)
    assert result["status"] == "needs_data" and "execution costs" in result["reason"]
    configured["fee_rate"] = ".0001"
    configured["cash_symbol"] = "BILL"
    result = validate(proposal(), data, configured)
    assert not result["passed"] and "cash_sweep_execution_adapter_required" in result["limitations"]


def test_worker_has_no_inherited_secrets_and_does_not_import_generated_code(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_BROKER_SECRET", "must-not-reach-worker")
    candidate = proposal()
    sentinel = tmp_path / "SHOULD_NOT_EXIST"
    candidate["implementation"]["implementation_notes"] = f"open({str(sentinel)!r}, 'w').write('bad')"
    from scripts.autonomous_fund import strategy_lab
    original = strategy_lab.subprocess.run

    def child(*args, **kwargs):
        assert "SYNTHETIC_BROKER_SECRET" not in kwargs["env"]
        assert kwargs["close_fds"] is True and "-I" in args[0]
        return original(*args, **kwargs)

    monkeypatch.setattr(strategy_lab.subprocess, "run", child)
    result = run_worker({"kind": "historical", "proposal": candidate, "data": dataset(), "policy": policy(),
                         "initial_capital": "10000", "trials": 1, "timeout_seconds": 30}, 30)
    assert result["status"] in {"historical_pass", "rejected"}
    assert not sentinel.exists()


def test_duplicate_distribution_receipts_cannot_create_free_income():
    data = dataset(8)
    action = {"symbol": "ALFA", "ex_date": data["bars"]["ALFA"][3]["day"],
              "pay_date": data["bars"]["ALFA"][5]["day"], "dividend": .2,
              "available_at": data["bars"]["ALFA"][1]["at"]}
    data["actions"] = [action, copy.deepcopy(action)]
    with pytest.raises(RiskError, match="Duplicate"):
        check_dataset(data)


def test_no_holdout_details_in_research_context_and_new_variants_wait(tmp_path):
    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    engine.schedule(dataset(), NOW)
    result = engine.run_one(NOW)
    assert result is not None
    public = engine.context(NOW)["candidates"][0]["validation"]
    assert "final" not in public["tests"]
    revision = proposal(version="v2")
    revision.update(parent_version="v1", change_type="revision")
    engine.submit(revision, NOW + timedelta(minutes=1))
    # Revisions can continue exploratory research; the consumed final window is
    # withheld rather than freezing all analysis until new market data exists.
    queued = engine.schedule(dataset(), NOW + timedelta(minutes=2))
    assert len(queued) == 1
    assert engine.rows()[-1]["state"] == "awaiting_new_holdout"
    exploratory = engine.run_one(NOW + timedelta(minutes=2))
    assert exploratory["status"] == "awaiting_new_holdout" and "final" not in exploratory["tests"]


def test_research_task_cannot_promote_a_strategy(tmp_path):
    from pydantic import ValidationError
    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    with pytest.raises(ValidationError):
        engine.update_task({"strategy_id": "synthetic-research", "version": "v1", "action": "paper_active",
                            "findings": "I approve myself", "next_step": "Trade"}, NOW)
    assert engine.rows()[0]["state"] == "candidate"


def test_frozen_forward_books_admission_and_degradation(tmp_path):
    from scripts.autonomous_fund.strategy_admission import library, verify_allocations
    data = dataset(170)
    formed = instant(data["bars"]["BASE"][-1]["at"]) + timedelta(hours=1)
    engine = lab(tmp_path, admission="paper_library", min_forward_sessions=40)
    engine.submit(proposal(), formed)
    engine.schedule(data, formed)
    assert engine.run_one(formed)["passed"]
    assert engine.rows()[0]["state"] == "shadow"
    assert library(engine.journal, formed) == []
    later = dataset(235)
    observed = instant(later["bars"]["BASE"][-1]["at"]) + timedelta(hours=1)
    engine.schedule(later, observed)
    result = engine.run_one(observed)
    assert result["observations"] == 65
    assert result["passed"] and engine.rows()[0]["state"] == "paper_active"
    approved = library(engine.journal, observed + timedelta(seconds=10))
    assert len(approved) == 1
    allocation = {k: approved[0][k] for k in ("strategy_id", "version", "digest")}
    allocation["capital_weight"] = .5
    decision = {"strategy_allocations": [allocation], "targets": [{"symbol": "ALFA", "weight": .25}]}
    with engine.journal.connection() as db:
        verify_allocations(db, decision, observed + timedelta(seconds=10), purchase=True)
        bad = {**decision, "targets": [{"symbol": "ALFA", "weight": .9}]}
        with pytest.raises(RiskError, match="exceeds"):
            verify_allocations(db, bad, observed + timedelta(seconds=10), purchase=True)
    with engine.journal.connection() as db:
        db.execute("UPDATE fund_strategy_versions SET state='paused'")
        with pytest.raises(RiskError, match="not admitted"):
            verify_allocations(db, decision, observed + timedelta(seconds=10), purchase=True)
        verify_allocations(db, {"targets": []}, observed, purchase=False)


def test_exploratory_snapshots_cannot_claim_or_supersede_forward_evidence(tmp_path):
    from scripts.autonomous_fund.strategy_admission import library
    engine = lab(tmp_path, admission="paper_library", min_forward_sessions=40)
    history = dataset(170)
    formed = instant(history["bars"]["BASE"][-1]["at"]) + timedelta(hours=1)
    engine.submit(proposal(), formed)
    engine.schedule(history, formed)
    assert engine.run_one(formed)["passed"]

    qualifying = dataset(235)
    observed = instant(qualifying["bars"]["BASE"][-1]["at"]) + timedelta(hours=1)
    exploratory = copy.deepcopy(qualifying)
    exploratory["coverage"]["publication_times_complete"] = False
    # A preliminary receipt would make the deficient snapshot the frozen seed
    # and prevent the later attested snapshot from proving complete coverage.
    assert engine.schedule(exploratory, observed) == []
    assert engine.run_one(observed) is None
    # A job queued before the scheduling fix still needs the write-side check.
    snapshot = engine.record_dataset(exploratory, observed)
    with engine.journal.connection() as db:
        db.execute("INSERT INTO fund_strategy_jobs VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)",
                   ("legacy-forward", proposal()["strategy_id"], "v1", observed.isoformat(), "forward", snapshot, "queued"))
    assert engine.run_one(observed)["status"] == "needs_data"
    with engine.journal.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fund_strategy_forward").fetchone()[0] == 0

    resumed = observed + timedelta(minutes=5)
    assert len(engine.schedule(qualifying, resumed)) == 1
    exploratory["coverage"]["raw_prices"] = False
    assert engine.schedule(exploratory, resumed) == []
    result = engine.run_one(resumed)
    assert result["passed"] and result["observations"] == 65
    assert len(library(engine.journal, resumed + timedelta(minutes=1))) == 1


def test_dividends_are_receivables_until_paid_and_never_double_counted():
    data, candidate = dataset(8), proposal()
    candidate["implementation"]["rules"][0]["exit"] = "False"
    days = [r["day"] for r in data["bars"]["BASE"]]
    data["actions"] = [{"symbol": "ALFA", "ex_date": days[3], "pay_date": days[5], "dividend": 1.,
                        "available_at": data["bars"]["ALFA"][1]["at"]}]
    result = replay(candidate, data, policy(), days[1:], initial_capital="10000")
    ex = next(r for r in result["series"] if r["day"] == days[3])
    paid = next(r for r in result["series"] if r["day"] == days[5])
    assert ex["receivables"] and not paid["receivables"]
    assert float(paid["cash"]) > float(ex["cash"])


def cash_dataset(count=12):
    data = dataset(count)
    data["bars"]["BILL"] = [{**row, "open": 100., "high": 100., "low": 100., "close": 100.}
                             for row in data["bars"]["BASE"]]
    configured = policy()
    configured.update(cash_symbol="BILL", cash_management={"enabled": True, "symbol": "BILL", "keep_cash": "0.01",
                                                          "band": "0.02", "min_idle_sessions": 1})
    configured["universe"]["BILL"] = "cash_equivalent"
    candidate = proposal()
    candidate["implementation"]["adapter"] = "daily_cash_rules_v1"
    return data, configured, candidate


def test_cash_adapter_funds_risk_trades_and_charges_both_legs():
    data, configured, candidate = cash_dataset()
    candidate["implementation"]["rules"][0].update(entry="session_index >= 4", exit="False")
    days = [r["day"] for r in data["bars"]["BASE"]]
    result = replay(candidate, data, configured, days[1:], initial_capital="10000")
    assert result["series"][0]["positions"].get("BILL", 0) > 0
    funded = [r for r in result["series"] if any(t["symbol"] == "ALFA" and t["side"] == "buy" for t in r["trades"])]
    assert funded and funded[0]["trades"][0]["symbol"] == "BILL" and funded[0]["trades"][0]["side"] == "sell"
    assert all(float(r["cash"]) >= 0 for r in result["series"])
    charges = sum(float(t["trade_cost"]) for r in result["series"] for t in r["trades"])
    assert float(result["summary"]["fees"]) == pytest.approx(charges)


def test_cash_adapter_counts_total_return_without_spending_dividends_early():
    data, configured, candidate = cash_dataset()
    candidate["implementation"]["rules"][0].update(entry="False", exit="False")
    days = [r["day"] for r in data["bars"]["BASE"]]
    for row in data["bars"]["BILL"][3:]:
        row.update(open=99.9, close=99.9, high=99.9, low=99.9)
    data["actions"] = [{"symbol": "BILL", "ex_date": days[3], "pay_date": days[5], "dividend": .1,
                        "available_at": data["bars"]["BILL"][1]["at"]}]
    result = replay(candidate, data, configured, days[1:], initial_capital="10000")
    before, ex, paid = [next(r for r in result["series"] if r["day"] == day) for day in (days[2], days[3], days[5])]
    assert ex["receivables"] and not paid["receivables"]
    assert float(before["equity"]) == pytest.approx(float(ex["equity"]))
    assert float(ex["equity"]) == pytest.approx(float(paid["equity"]))
    assert float(paid["cash"]) > float(ex["cash"])


def test_cash_adapter_validates_with_an_independent_passive_reference():
    data, configured, candidate = cash_dataset(170)
    result = validate(candidate, data, configured)
    assert result["status"] == "historical_pass" and result["passed"]
    assert set(result["tests"]) == {"train", "validation", "final"}
    assert any(trade["symbol"] == "BILL" for row in result["receipts"]["validation"]["strategy"]["series"]
               for trade in row["trades"])


def test_shadow_risk_pause_is_not_a_fresh_buy_signal():
    data, configured, candidate = dataset(170, fail_after=60), policy(), proposal()
    configured["limits"]["drawdown"] = ".01"
    candidate["implementation"]["rules"][0]["exit"] = "False"
    result = validate(candidate, data, configured)
    assert any(r["strategy"]["risk_paused"] for r in result["receipts"].values())
    assert not result["passed"]


def test_validated_only_blocks_new_buys_but_keeps_protective_reductions(tmp_path):
    from scripts.autonomous_fund.strategy_admission import verify_allocations
    engine = lab(tmp_path, admission="paper_library", allocation_mode="validated_only")
    with engine.journal.connection() as db:
        with pytest.raises(RiskError, match="require an admitted"):
            verify_allocations(db, {"targets": []}, NOW, purchase=True)
        verify_allocations(db, {"targets": []}, NOW, purchase=False)


def test_lab_status_is_read_only_and_budget_exhaustion_preserves_orders(tmp_path):
    from scripts.autonomous_fund.lab import report
    assert "尚无" in report(tmp_path)[0]
    assert not (tmp_path / "state.sqlite3").exists()
    engine = lab(tmp_path)
    before = engine.journal.all_events()
    assert "Strategy Lab" in report(tmp_path)[0]
    assert engine.journal.all_events() == before
    key = engine.budget.reserve(NOW, "shared-research", "research_model", "2")
    assert key
    engine.submit(proposal(), NOW)
    engine.schedule(dataset(), NOW)
    assert engine.run_one(NOW) is None
    assert engine.journal.pending() == []


def test_declared_parameters_are_interpreted_and_perturbed_without_optimization():
    candidate = proposal()
    candidate["implementation"]["parameters"] = {"p_hold": 3}
    candidate["implementation"]["rules"][0]["exit"] = "holding_sessions >= p_hold"
    result = validate(candidate, dataset(), policy())
    assert len(result["parameter_sensitivity"]) == 2
    assert {r["value"] for r in result["parameter_sensitivity"]} == {2, 4}
    assert result["walk_forward"]["folds"]
    assert candidate["implementation"]["parameters"]["p_hold"] == 3


def test_missing_features_and_benchmark_gaps_are_not_failed_investment_hypotheses():
    candidate, data = proposal(), dataset()
    candidate["implementation"]["rules"][0]["entry"] = 'field("EVENTS", "surprise") > 0'
    result = validate(candidate, data, policy())
    assert result["status"] == "needs_data" and result["missing_feature"]["symbol"] == "EVENTS"
    data["bars"]["BASE"].pop(70)
    result = validate(proposal(), data, policy())
    assert result["status"] == "needs_data" and "exchange sessions" in result["reason"]


def test_an_experiment_configuration_cannot_change_under_the_same_version(tmp_path):
    from scripts.autonomous_fund.experiments import Registry
    journal = Journal(tmp_path, "synthetic-immutable-experiment")
    registry = Registry(journal)
    entry = {"id": "trial", "version": "v1", "kind": "strategy", "hypothesis": "Synthetic hypothesis",
             "config": {"lookback": 5}, "state": "candidate"}
    registry.observe(NOW, [entry])
    with pytest.raises(ValueError, match="cannot change"):
        registry.observe(NOW, [{**entry, "config": {"lookback": 20}}])


def test_research_wire_schema_has_no_open_parameter_objects():
    from scripts.autonomous_fund.strategy import Research
    schema = Research.model_json_schema()

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)

    check(schema)


def test_no_evidence_is_invented_for_random_returns():
    from scripts.autonomous_fund.strategy_definition import StrategyProposal
    from scripts.autonomous_fund.strategy_validation import test_excess as evaluate_excess
    rng, positives = random.Random(97), 0
    plan = StrategyProposal.model_validate(proposal()).validation.model_dump()
    for _ in range(60):
        a = [rng.gauss(0, .01) for _ in range(240)]
        b = [rng.gauss(0, .01) for _ in range(240)]
        left = {"daily_returns": a, "net_return": math.prod(1 + x for x in a) - 1, "max_drawdown": .1}
        right = {"daily_returns": b, "net_return": math.prod(1 + x for x in b) - 1, "max_drawdown": .1}
        positives += evaluate_excess(left, right, plan, trials=60)["passed"]
    assert positives <= 2  # A search-adjusted negative control; no expected-profit fixture.


def test_lab_command_uses_the_selected_account_and_cache_inputs_stay_exploratory(tmp_path):
    from scripts.autonomous_fund.datastore import DailyStore
    from scripts.autonomous_fund.settings import startup
    from scripts.autonomous_fund.strategy_lab_runtime import cached_snapshot
    resolved = startup("strategy-lab", None, {"FUND_RUN_DIR": str(tmp_path), "FUND_MODE": "local"})
    assert resolved[1] == tmp_path
    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    store, data = DailyStore(tmp_path / "daily"), dataset()
    from scripts.autonomous_fund.risk import trading_day
    for symbol, rows in data["bars"].items():
        values = [(r["day"], r["close"], r["volume"], r["open"], r["high"], r["low"]) for r in rows]
        store.save_bars(trading_day(NOW), symbol, len(values), values, cutoff="2026-11-17")
    snapshot = cached_snapshot(SimpleNamespace(strategy_lab=engine, daily_store=store, universe={"ALFA", "BASE"}), NOW)
    assert snapshot and not any(snapshot["coverage"].values())
    assert snapshot["bars"]["ALFA"][0]["availability_estimated"]


def test_research_reporting_uses_saved_evidence_and_accepts_no_new_idea():
    from scripts.autonomous_fund.notifications import strategy_research_lines
    assert strategy_research_lines({"summary": "Legacy research"}) == []
    lines = strategy_research_lines({"research_direction": "Synthetic event research", "strategy_proposals": [proposal()]},
                                    {"candidates": [{"id": "older", "version": "v1", "state": "rejected"}]})
    text = "\n".join(lines)
    assert "新策略提案" in text and "验证未通过" in text and "不等于策略准入" in text


def test_researcher_handoff_registers_a_strategy_without_a_manager_or_order_call(tmp_path):
    from scripts.autonomous_fund.strategy import Research
    from tests.test_autonomous_fund_premarket import harness
    strategy, calls = harness(tmp_path)
    strategy.strategy_lab = StrategyLab(strategy.journal, policy())
    original = strategy.agents["researcher"].run

    def research(**kwargs):
        result = original(**kwargs)
        body = result.parsed.model_dump()
        body.update(research_direction="Synthetic event research", strategy_proposals=[proposal()])
        result.parsed = Research.model_validate(body)
        result.summary = result.parsed.model_dump_json()
        return result

    strategy.agents["researcher"].run = research
    strategy.before_market_opens()
    assert len(calls) == 1 and calls[0]["context"]["orders_enabled"] is False
    assert strategy.strategy_lab.rows()[0]["state"] == "candidate"
    assert strategy.premarket_research["research"]["strategy_proposals"][0]["strategy_id"] == proposal()["strategy_id"]
    assert not strategy.journal.pending()
    assert not any(r["kind"] == "decision" for r in strategy.journal.all_events())


def test_worker_failure_retries_the_same_frozen_job_without_rewriting_evidence(tmp_path, monkeypatch):
    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    engine.schedule(dataset(), NOW)
    from scripts.autonomous_fund import strategy_lab
    original = strategy_lab.run_worker
    monkeypatch.setattr(strategy_lab, "run_worker", lambda *_: (_ for _ in ()).throw(TimeoutError()))
    assert engine.run_one(NOW)["status"] == "worker_error"
    assert engine.rows()[0]["state"] == "candidate"
    monkeypatch.setattr(strategy_lab, "run_worker", original)
    assert engine.run_one(NOW)["status"] in {"historical_pass", "rejected"}
    with engine.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_strategy_events WHERE kind='worker_failure'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM fund_strategy_results").fetchone()[0] == 1


def test_optional_lab_storage_failure_does_not_discard_market_research(tmp_path, monkeypatch):
    from scripts.autonomous_fund.strategy import Research
    from tests.test_autonomous_fund_premarket import harness
    strategy, calls = harness(tmp_path)
    engine = StrategyLab(strategy.journal, policy())
    strategy.strategy_lab = engine
    original = strategy.agents["researcher"].run

    def research(**kwargs):
        result = original(**kwargs)
        result.parsed = Research.model_validate({**result.parsed.model_dump(), "strategy_proposals": [proposal()]})
        result.summary = result.parsed.model_dump_json()
        return result

    strategy.agents["researcher"].run = research
    monkeypatch.setattr(engine, "submit", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private path")))
    monkeypatch.setattr(engine, "context", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private path")))
    strategy.before_market_opens()
    assert strategy.premarket_research and len(calls) == 1
    assert strategy.premarket_research["research"]["summary"]
    assert not strategy.journal.pending()
