"""Open strategy research acceptance controls. Synthetic data, no paid inference or broker access."""
import copy
import hashlib
import json
import math
import random
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.research_budget import ResearchBudget, ResearchBudgetSettings
from scripts.autonomous_fund.risk import RiskError, instant
from scripts.autonomous_fund.strategy_definition import StrategyLabSettings, StrategyProposal
from scripts.autonomous_fund.strategy_lab import StrategyLab, run_worker
from scripts.autonomous_fund.strategy_program import Evaluator, Warmup, parse
from scripts.autonomous_fund.strategy_validation import COVERAGE_FIELDS, check_dataset, digest, replay, validate

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
    return attest({"bars": bars, "actions": [], "source": "synthetic acceptance fixture",
                   "coverage": dict.fromkeys(COVERAGE_FIELDS, True)})


def attest(snapshot):
    content = {key: value for key, value in snapshot.items() if key != "provenance"}
    checksum = digest(content)
    return {**content, "provenance": {"snapshot_sha256": checksum, "claims": {
        key: {"source": "synthetic fixture", "reference": key, "artifact_sha256": checksum} for key in COVERAGE_FIELDS}}}


def forward_one_session_at_a_time(engine, data, start, monkeypatch):
    from scripts.autonomous_fund import strategy_lab, strategy_worker

    def evaluate(payload, _timeout):
        # Use the worker's actual evaluator and its serializable receipt shape.
        # Separate tests cover the isolated subprocess and the real first job.
        return json.loads(json.dumps(strategy_worker.evaluate(payload), default=str))

    observed, result = None, None
    count = len(data["bars"]["BASE"])
    with monkeypatch.context() as worker:
        worker.setattr(strategy_lab, "run_worker", evaluate)
        for i in range(start, count):
            observed = instant(data["bars"]["BASE"][i]["at"]) + timedelta(hours=1)
            daily = attest({**data, "bars": {symbol: rows[i:i + 1] for symbol, rows in data["bars"].items()}})
            assert len(engine.schedule(daily, observed)) == 1
            result = engine.run_one(observed)
            assert result["prospective_sessions"] == i - start + 1
            assert result["replayed_sessions"] == 0
    return observed, result


def lab(tmp_path, **settings):
    journal = Journal(tmp_path, "synthetic-lab-owner")
    return StrategyLab(journal, policy(), StrategyLabSettings(**settings))


def test_offline_intake_preserves_the_runner_policy_and_checks_one_candidate(tmp_path):
    from scripts.autonomous_fund.lab import check_data, open_lab
    engine = lab(tmp_path, admission="paper_library", allocation_mode="validated_only", max_jobs_per_day=3)
    engine.journal.event(NOW, "startup", {"strategy_lab_policy": engine.policy,
                                         "research_budget": {"project_usd": "7"}})
    engine.submit(proposal("selected"), NOW)
    saved = engine.journal.get_meta("strategy_lab_policy")
    offline = open_lab(tmp_path, {})  # The CLI's defaults differ from the running account.
    assert offline.settings == engine.settings
    assert offline.budget.settings.project_usd == "7"
    assert engine.journal.get_meta("strategy_lab_policy") == saved
    data = dataset()
    valid = check_data(tmp_path, data, "selected", "v1", NOW)
    assert valid["ready_for_validation_input"] and not valid["receipt_gaps"]
    stale = {**data, "bars": {"BASE": data["bars"]["BASE"]}}
    result = check_data(tmp_path, stale, "selected", "v1", NOW)
    assert result["symbols_missing_from_this_snapshot"] == ["ALFA"]
    assert result["receipt_gaps"] == ["snapshot_provenance_unbound"]
    with engine.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_strategy_jobs").fetchone()[0] == 0


def test_strict_sources_gate_holdout_and_forward_with_exact_offline_receipts(tmp_path, monkeypatch):
    from scripts.autonomous_fund import lab as operator_lab
    from scripts.autonomous_fund.lab import check_data

    engine = lab(tmp_path, require_source_artifacts=True)
    data = dataset()
    formed = instant(data["bars"]["BASE"][-1]["at"]) + timedelta(hours=1)
    engine.journal.event(formed, "startup", {"strategy_lab_policy": engine.policy})
    engine.submit(proposal(), formed)
    artifacts = tmp_path / "sources"
    artifacts.mkdir()
    raw = b"synthetic original provider bytes"
    checksum = hashlib.sha256(raw).hexdigest()
    for receipt in data["provenance"]["claims"].values():
        receipt["artifact_sha256"] = checksum
        receipt["reference"] = "https://example.org/description-only"
    key = proposal()["strategy_id"]
    assert check_data(tmp_path, data, key, "v1", formed)["receipt_gaps"] == ["source_artifacts_unchecked"]
    exploratory, = engine.schedule(data, formed)
    with engine.journal.connection() as db:
        assert db.execute("SELECT kind FROM fund_strategy_jobs WHERE id=?", (exploratory,)).fetchone()[0] == "exploratory"
        assert db.execute("SELECT count(*) FROM fund_strategy_holdouts").fetchone()[0] == 0
    result = engine.run_one(formed)
    assert result["status"] == "needs_data" and "source_artifacts_unchecked" in result["limitations"]
    assert "final" not in result["tests"]

    original = artifacts / checksum
    original.write_bytes(b"changed provider bytes")
    with pytest.raises(RiskError, match="source_artifact_mismatch:raw_prices"):
        engine.schedule(data, formed, artifacts_dir=artifacts)
    original.unlink()
    original.mkdir()
    with pytest.raises(RiskError, match="source_artifact_unreadable:raw_prices"):
        engine.schedule(data, formed, artifacts_dir=artifacts)
    original.rmdir()
    original.write_bytes(raw)
    checked = check_data(tmp_path, data, key, "v1", formed, artifacts_dir=artifacts)
    assert checked["ready_for_validation_input"] and checked["source_artifacts_matched_this_check"]
    assert not checked["source_artifacts_recorded"]
    assert len(engine.schedule(data, formed, artifacts_dir=artifacts)) == 1
    assert check_data(tmp_path, data, key, "v1", formed)["source_artifacts_recorded"]
    assert engine.run_one(formed)["passed"]

    later = dataset(171)
    from scripts.autonomous_fund.lab import forward_audit

    opened = instant(later["bars"]["BASE"][-1]["open_at"])
    audit = forward_audit(tmp_path, key, "v1", opened - timedelta(minutes=1))
    upcoming, = audit["sessions"]
    assert audit["source_artifacts_required"] and upcoming["status"] == "ready_for_open"
    assert upcoming["signal_source_status"] == "artifact_bytes_matched"
    daily = attest({**later, "bars": {symbol: values[-1:] for symbol, values in later["bars"].items()}})
    for receipt in daily["provenance"]["claims"].values():
        receipt["artifact_sha256"] = checksum  # New normalized input still needs its own check.
    observed = instant(daily["bars"]["BASE"][-1]["at"]) + timedelta(hours=1)
    assert engine.schedule(daily, observed) == []
    assert engine.run_one(observed) is None
    snapshot = tmp_path / "daily.json"
    snapshot.write_text(json.dumps(daily))
    monkeypatch.setattr(operator_lab, "datetime", SimpleNamespace(now=lambda tz: observed.astimezone(tz)))
    operator_lab.main(["import-data", "--run-dir", str(tmp_path), "--dataset", str(snapshot),
                       "--artifacts-dir", str(artifacts)])
    with engine.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_strategy_source_artifacts").fetchone()[0] == 2
        assert db.execute("SELECT kind FROM fund_strategy_jobs WHERE state='queued'").fetchone()[0] == "forward"
    assert check_data(tmp_path, daily, key, "v1", observed)["source_artifacts_recorded"]
    assert not check_data(tmp_path, data, key, "v1", formed - timedelta(seconds=1))["source_artifacts_recorded"]
    assert not engine.journal.pending()


def test_source_artifact_rejects_a_symlink_to_matching_bytes(tmp_path):
    from scripts.autonomous_fund.strategy_validation import COVERAGE_FIELDS, source_artifact_gaps

    data = dataset()
    raw = b"synthetic original provider bytes"
    checksum = hashlib.sha256(raw).hexdigest()
    for receipt in data["provenance"]["claims"].values():
        receipt["artifact_sha256"] = checksum
    saved = tmp_path / "elsewhere"
    saved.write_bytes(raw)
    try:
        (tmp_path / checksum).symlink_to(saved)
    except OSError as exc:
        # Keep the file, digest and directory cases running on Windows without link privileges.
        if getattr(exc, "winerror", None) != 1314:
            raise
        pytest.skip("Windows runner cannot create file symlinks")
    assert source_artifact_gaps(data, tmp_path) == [f"source_artifact_unreadable:{key}" for key in COVERAGE_FIELDS]


@pytest.mark.parametrize("field", ["feature_available_at", "actions"])
def test_read_only_preflight_rejects_future_individual_publications(tmp_path, field):
    from scripts.autonomous_fund.lab import check_data

    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    data = dataset()
    future = (NOW + timedelta(seconds=1)).isoformat()
    if field == "actions":
        data["actions"] = [{"symbol": "ALFA", "ex_date": "2026-11-17", "split": 2, "available_at": future}]
    else:
        data["bars"]["ALFA"][-1]["feature_available_at"] = {"event": future}
    data = attest(data)
    expected = "publication"
    with pytest.raises(RiskError, match=expected):
        check_data(tmp_path, data, proposal()["strategy_id"], "v1", NOW)
    with pytest.raises(RiskError, match=expected):
        engine.schedule(data, NOW)


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
    from scripts.autonomous_fund.lab import report

    engine, candidate = lab(tmp_path), proposal()
    candidate["implementation"] = {"adapter": "novel_path_dependent_model", "implementation_notes": "inert code text"}
    engine.submit(candidate, NOW)
    assert engine.rows()[0]["state"] == "needs_adapter"
    assert engine.schedule(dataset(), NOW) == []
    assert any("需要支持 novel_path_dependent_model 的回测适配器" in line for line in report(tmp_path))


def test_uncompiled_implementation_retains_the_complete_research_hypothesis(tmp_path):
    engine, candidate = lab(tmp_path), proposal()
    candidate["implementation"]["rules"][0]["entry"] = "unknown_mechanism("
    engine.submit(candidate, NOW)
    saved = engine.rows()[0]
    assert saved["state"] == "needs_formalization"
    assert json.loads(saved["proposal"])["hypothesis"] == candidate["hypothesis"]
    assert engine.schedule(dataset(), NOW) == []


@pytest.mark.parametrize(("field", "expression", "reason"), [
    ("entry", "1", "Boolean"),
    ("exit", "True and 2", "Boolean operands"),
    ("weight", "False", "numeric"),
    ("weight", 'mean("ALFA", "close", p_window)', "integer lookback"),
    ("entry", 'field("ALFA") > 0', "argument count"),
])
def test_static_rule_errors_explain_the_field_and_do_not_queue(tmp_path, field, expression, reason):
    engine, candidate = lab(tmp_path), proposal()
    candidate["implementation"]["parameters"] = {"p_window": 0}
    candidate["implementation"]["rules"][0][field] = expression
    engine.submit(candidate, NOW)
    saved, = engine.context(NOW)["candidates"]
    assert saved["state"] == "needs_formalization"
    assert f"ALFA.{field}" in saved["feasibility_error"] and reason in saved["feasibility_error"]
    assert engine.schedule(dataset(), NOW) == []


def test_data_dependent_rule_error_is_not_classified_as_missing_data(tmp_path):
    engine, candidate = lab(tmp_path), proposal()
    candidate["implementation"]["rules"][0]["weight"] = '2 * field("ALFA", "event")'
    engine.submit(candidate, NOW)
    assert engine.rows()[0]["state"] == "candidate"
    assert engine.schedule(dataset(), NOW)
    result = engine.run_one(NOW)
    assert result["status"] == "needs_formalization" and "ALFA.weight" in result["reason"]
    assert engine.rows()[0]["state"] == "needs_formalization"


def test_forward_rule_error_stops_scheduling_that_version(tmp_path):
    engine, candidate, data = lab(tmp_path), proposal(), dataset()
    candidate["implementation"]["rules"][0]["weight"] = '0.25 + field("ALFA", "event") / 4'
    formed = instant(data["bars"]["BASE"][-1]["at"]) + timedelta(hours=1)
    engine.submit(candidate, formed)
    assert engine.schedule(data, formed)
    assert engine.run_one(formed)["passed"]
    assert engine.rows()[0]["state"] == "shadow"

    later = dataset(171)
    later["bars"]["ALFA"][-1]["event"] = 4.0
    observed = instant(later["bars"]["BASE"][-1]["at"]) + timedelta(hours=1)
    daily = attest({**later, "bars": {symbol: rows[-1:] for symbol, rows in later["bars"].items()}})
    assert engine.schedule(daily, observed)
    result = engine.run_one(observed)
    assert result["status"] == "needs_formalization" and "ALFA.weight" in result["reason"]
    assert engine.rows()[0]["state"] == "needs_formalization"
    assert engine.schedule(daily, observed + timedelta(minutes=1)) == []


def test_a_novel_validation_method_is_not_forced_into_daily_significance_tests():
    candidate = proposal()
    candidate["validation"]["method"] = "rare_event_prospective_protocol"
    result = validate(candidate, dataset(), policy())
    assert result["status"] == "needs_adapter" and "tests" not in result


def test_impossible_purged_windows_remain_researchable_without_spending_the_holdout(tmp_path):
    from scripts.autonomous_fund.lab import report

    engine = lab(tmp_path)
    candidate = proposal()
    candidate["validation"]["holding_sessions"] = 5  # 10 complete blocks plus a five-session purge needs 55 days.
    engine.submit(candidate, NOW, source_call_id="synthetic-research-call")
    saved = engine.context(NOW)["candidates"][0]
    assert saved["state"] == "needs_formalization"
    assert saved["source_call_id"] == "synthetic-research-call"
    assert "55" in saved["feasibility_error"]
    assert any(saved["feasibility_error"] in line for line in report(tmp_path))
    assert engine.schedule(dataset(), NOW) == []
    with engine.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_strategy_holdouts").fetchone()[0] == 0
    assert validate(candidate, dataset(), policy())["minimum_sessions"] == {
        "validation_sessions": 55, "final_sessions": 55}


@pytest.mark.parametrize("implementation", [None, {"adapter": "daily_rules_v1", "rules": []}])
def test_status_explains_when_a_saved_proposal_has_no_executable_rules(tmp_path, implementation):
    from scripts.autonomous_fund.lab import report

    engine, candidate = lab(tmp_path), proposal()
    candidate["implementation"] = implementation
    engine.submit(candidate, NOW)
    assert any("尚未提交可执行规则" in line for line in report(tmp_path))


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


def test_research_budget_and_tasks_are_reported_as_known_at_the_cutoff(tmp_path):
    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    reserved = engine.budget.reserve(NOW, "synthetic-research", "validation_compute", "0.05")
    later = NOW + timedelta(minutes=1)
    engine.budget.settle(reserved, later, "0.02", {})
    engine.update_task({"strategy_id": "synthetic-research", "version": "v1", "action": "pause",
                        "findings": "Synthetic new risk", "next_step": "Check inputs"}, later)
    early = engine.context(NOW)
    updated = engine.context(later)
    assert early["candidates"][0]["task_state"] == "open" and early["candidates"][0]["last_task"] is None
    assert updated["candidates"][0]["task_state"] == "paused"
    assert early["budget"]["daily_committed_usd"] == .05
    assert early["budget"]["known_lifetime_by_project_usd"] == {}
    assert updated["budget"]["known_lifetime_by_project_usd"] == {"synthetic-research": .02}


def test_daily_activity_omits_validation_receipts_after_the_report_cutoff(tmp_path):
    from scripts.autonomous_fund.notifications import _strategy_day_activity
    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    engine.submit(proposal("next-study"), NOW + timedelta(seconds=10))
    with engine.journal.connection() as db:
        lines = "\n".join(_strategy_day_activity(db, NOW.date().isoformat(), NOW))
    assert "登记 1 项" in lines and "synthetic-research / v1" in lines
    assert "next-study" not in lines


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
    candidate["name"] = "多市场研究 Ω 🧭"
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
    public = engine.context(NOW + timedelta(minutes=1))["candidates"][0]["validation"]
    assert "final" not in public["tests"]
    assert "final_passed" not in public
    assert public["tests"]["validation"]
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


@pytest.mark.parametrize("gap", ["feature", "non_numeric", "benchmark_session", "declared_series"])
def test_preflight_does_not_spend_the_holdout_or_a_slot_on_incomplete_data(tmp_path, gap):
    from scripts.autonomous_fund.lab import check_data

    engine = lab(tmp_path)
    history = dataset(170)
    candidate = proposal()
    if gap == "feature":
        history["bars"]["ALFA"][155].pop("event")
    elif gap == "non_numeric":
        history["bars"]["ALFA"][155]["event"] = None
    elif gap == "benchmark_session":
        del history["bars"]["BASE"][155]
    else:
        candidate["data_symbols"] = ["RELEASE"]
        candidate["implementation"]["rules"][0]["entry"] = 'field("RELEASE", "value") > 0'
    first = attest(history)
    engine.submit(candidate, NOW)
    checked = check_data(tmp_path, first, candidate["strategy_id"], "v1", NOW)
    assert not checked["ready_for_validation_input"] and checked["input_gap"]["status"] == "needs_data"
    assert engine.schedule(first, NOW) == []
    assert engine.run_one(NOW) is None
    with engine.journal.connection() as db:
        for table in ("fund_strategy_holdouts", "fund_strategy_cohorts", "fund_research_reservations"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert engine.context(NOW)["candidates"][0]["validation"]["reason"] == checked["input_gap"]["reason"]

    # Another version can use that unconsumed window when its own inputs are complete.
    later = NOW + timedelta(minutes=1)
    engine.submit(proposal("ready-independently"), later)
    engine.schedule(dataset(), later, candidate=("ready-independently", "v1"))
    with engine.journal.connection() as db:
        assert db.execute("SELECT id FROM fund_strategy_holdouts").fetchone()[0] == "ready-independently"


def test_binding_a_new_snapshot_cannot_move_or_replace_a_reserved_holdout(tmp_path, monkeypatch):
    from scripts.autonomous_fund import strategy_lab

    engine = lab(tmp_path)
    first, corrected = dataset(170), dataset(171)
    engine.submit(proposal(), NOW)
    first_job, = engine.schedule(first, NOW)
    assert engine.schedule(corrected, NOW) == []
    # A failure that is only detected at execution time leaves the original
    # holdout reserved. A subsequent snapshot must still use those sessions.
    with monkeypatch.context() as patch:
        patch.setattr(strategy_lab, "run_worker", lambda *_: {"status": "needs_data", "passed": False})
        report = engine.run_one(NOW)
    assert report["status"] == "needs_data" and "final" not in report.get("tests", {})
    with engine.journal.connection() as db:
        receipt = db.execute("SELECT dataset,sessions FROM fund_strategy_holdouts").fetchone()
        sessions = json.loads(receipt["sessions"])
        assert receipt["dataset"] == digest(first)
        assert db.execute("SELECT state FROM fund_strategy_jobs WHERE id=?", (first_job,)).fetchone()[0] == "finished"
    again = NOW + timedelta(days=1)
    assert len(engine.schedule(corrected, again)) == 1
    final = engine.run_one(again)["tests"]["final"]
    assert final["start"] == sessions[-37] and final["end"] == sessions[-1]
    assert final["end"] != corrected["bars"]["BASE"][-1]["day"]


def test_snapshot_provenance_at_the_automatic_ingest(tmp_path, monkeypatch):
    from scripts.autonomous_fund.strategy_lab_runtime import tick

    engine = lab(tmp_path)
    inbox = tmp_path / "strategy_lab_snapshot.json"
    state = SimpleNamespace(strategy_lab=engine, mode="local", observe_only=False, halt_path=tmp_path / "STOP",
                            get_datetime=lambda: NOW)
    # Run preparation synchronously to inspect its exact cutoff in this intake test.
    monkeypatch.setattr(engine, "kick", lambda now, *, prepare: prepare(now))
    candidate = dataset()
    candidate["bars"]["ALFA"][-1]["event"] = 0
    inbox.write_text(json.dumps(candidate), encoding="utf-8")
    tick(state)
    assert engine.run_one(NOW) is None
    later = NOW + timedelta(minutes=1)
    engine.submit(proposal(), later)
    state.get_datetime = lambda: later
    tick(state)  # The unchanged file can serve a new candidate.
    result = engine.run_one(later)
    assert "snapshot_provenance_unbound" in result["limitations"]
    assert "final" not in result["tests"]
    with engine.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_strategy_holdouts").fetchone()[0] == 0
    # The normal off-hours entry point accepts a replacement attested to the
    # new contents. The previous exploratory run does not use the holdout.
    inbox.write_text(json.dumps(attest(candidate)), encoding="utf-8")
    state.get_datetime = lambda: later + timedelta(minutes=1)
    tick(state)
    assert "final" in engine.run_one(state.get_datetime())["tests"]
    late = dataset()
    late["bars"]["ALFA"][-1]["feature_available_at"] = {"event": (NOW + timedelta(days=1)).isoformat()}
    with pytest.raises(RiskError, match="invalid publication time"):
        engine.record_dataset(attest(late), NOW)


def test_automatic_intake_rechecks_originals_delivered_after_the_snapshot(tmp_path):
    from scripts.autonomous_fund.strategy_lab_runtime import prepare_inputs

    engine = lab(tmp_path, require_source_artifacts=True)
    engine.submit(proposal(), NOW)
    data = dataset()
    (tmp_path / "strategy_lab_snapshot.json").write_text(json.dumps(data), encoding="utf-8")
    state = SimpleNamespace(strategy_lab=engine, halt_path=tmp_path / "STOP")
    prepare_inputs(state, NOW)
    assert engine.journal.get_meta("strategy_lab_last_intake")["coverage_gaps"] == ["source_artifacts_unchecked"]
    with engine.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_strategy_holdouts").fetchone()[0] == 0

    originals = tmp_path / "strategy_lab_source_artifacts"
    originals.mkdir()
    content = {key: value for key, value in data.items() if key != "provenance"}
    source = originals / data["provenance"]["snapshot_sha256"]
    source.write_bytes(b"incorrect artifact")
    later = NOW + timedelta(minutes=1)
    prepare_inputs(state, later)
    assert "source_artifact_mismatch" in engine.journal.get_meta("strategy_lab_last_intake")["coverage_gaps"][0]

    # An adapter atomically publishes corrected bytes without touching the snapshot.
    body = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    staging = originals / "staging"
    staging.write_bytes(body)
    staging.replace(source)
    prepare_inputs(state, later + timedelta(minutes=1))
    assert engine.journal.get_meta("strategy_lab_last_intake")["coverage_gaps"] == []
    with engine.journal.connection() as db:
        assert db.execute("SELECT dataset FROM fund_strategy_source_artifacts").fetchone()[0] == digest(data)
        assert db.execute("SELECT kind FROM fund_strategy_jobs WHERE state='queued'").fetchone()[0] == "historical"
    assert "final" in engine.run_one(later + timedelta(minutes=1))["tests"]


@pytest.mark.parametrize("mode", ["local", "paper"])
def test_data_desk_automatically_fills_candidate_history_without_writing_trading_prices(tmp_path, mode):
    import pandas as pd
    import pandas_market_calendars as mcal

    from scripts.autonomous_fund.datadesk import DataDesk
    from scripts.autonomous_fund.strategy_lab_runtime import prepare_inputs
    from .test_autonomous_fund_datadesk import config

    engine = lab(tmp_path)
    research = proposal()
    research["implementation"]["rules"][0]["entry"] = 'field("ALFA", "close") > 0'
    engine.submit(research, NOW)
    session_dates = mcal.get_calendar("NYSE").schedule(start_date="2025-01-02", end_date=NOW.date()).index[-140:]
    closes = [100 + i / 10 for i in range(len(session_dates))]
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes,
                          "volume": 10000}, index=session_dates)
    calls = []

    def prices(asset, length, timestep):
        calls.append((asset.symbol, length, timestep))
        return SimpleNamespace(df=frame.tail(length))

    settings = config()
    settings["universe"].update(policy()["universe"])
    desk = DataDesk(tmp_path, settings, mode, source=SimpleNamespace(get_historical_prices=prices),
                    clock=lambda: NOW, sleep=lambda _: None, environ={})
    day = NOW.date().isoformat()
    baseline = [(stamp.date(), close, 10000, close, close, close) for stamp, close in zip(session_dates, closes)]
    desk.store.save_bars(day, "BASE", 140, baseline, cutoff=NOW.date() + timedelta(days=1))
    desk.store.save_bars(day, "ALFA", 40, baseline[-40:], cutoff=NOW.date() + timedelta(days=1))
    original = desk.store.get(day, "bars/ALFA")
    assert desk.enabled("strategy_history", NOW)
    result = desk.run_job("strategy_history")
    assert calls == [("ALFA", 140, "day")]
    assert result["fetched"] == 1 and result["pending"] == 0 and result["missing_source"] == []
    assert desk.store.get(day, "bars/ALFA") == original
    assert desk._job_strategy_history(NOW)["fetched"] == 0  # A repeat consumes no provider call.
    assert len(calls) == 1

    state = SimpleNamespace(strategy_lab=engine, daily_store=desk.store, universe={"ALFA", "BASE"}, halt_path=tmp_path / "STOP")
    prepare_inputs(state, NOW)
    with engine.journal.connection() as db:
        assert db.execute("SELECT kind FROM fund_strategy_jobs").fetchone()[0] == "exploratory"
        assert db.execute("SELECT count(*) FROM fund_strategy_holdouts").fetchone()[0] == 0
    result = engine.run_one(NOW)
    assert result["status"] == "needs_data" and "final" not in result.get("tests", {})
    assert "publication_times_complete" in result["limitations"]


def test_repeated_cache_revision_schedules_once_on_the_existing_worker(tmp_path, monkeypatch):
    from scripts.autonomous_fund.datastore import DailyStore
    from scripts.autonomous_fund.risk import trading_day
    from scripts.autonomous_fund.strategy_lab_runtime import tick

    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    store = DailyStore(tmp_path / "daily")
    row = ["2026-11-13", 101., 10000., 100., 102., 99.]
    for symbol in ("ALFA", "BASE"):
        store.put(trading_day(NOW), "bars/" + symbol, {"rows": [row]})
    strategy = SimpleNamespace(strategy_lab=engine, daily_store=store, universe={"ALFA", "BASE"},
                               mode="local", observe_only=False, halt_path=tmp_path / "STOP", get_datetime=lambda: NOW)
    original, threads = engine.schedule, []

    def schedule(data, now, **kwargs):
        threads.append(get_ident())
        return original(data, now, **kwargs)

    monkeypatch.setattr(engine, "schedule", schedule)
    monkeypatch.setattr(engine, "run_one", lambda _now: None)
    try:
        tick(strategy)
        engine._future.result(timeout=10)
        store.put(trading_day(NOW), "bars/ALFA", {"rows": [row]})
        strategy.get_datetime = lambda: NOW + timedelta(seconds=30)
        tick(strategy)
        engine._future.result(timeout=10)
        strategy.get_datetime = lambda: NOW + timedelta(seconds=60)
        tick(strategy)
        engine._future.result(timeout=10)
        assert len(threads) == 1 and threads[0] != get_ident()
        strategy = SimpleNamespace(strategy_lab=engine, daily_store=store, universe={"ALFA", "BASE"},
                                   mode="local", observe_only=False, halt_path=tmp_path / "STOP",
                                   get_datetime=lambda: NOW + timedelta(seconds=80))
        tick(strategy)
        engine._future.result(timeout=10)
        assert len(threads) == 1
        store.put(trading_day(NOW), "bars/ALFA", {"rows": [[*row[:-1], 98.]]})
        strategy.get_datetime = lambda: NOW + timedelta(seconds=110)
        tick(strategy)
        engine._future.result(timeout=10)
        assert len(threads) == 2
    finally:
        if engine._executor is not None:
            engine._executor.shutdown()


def test_independent_cache_jobs_keep_their_depth_when_the_formal_file_is_broken(tmp_path):
    from scripts.autonomous_fund.datastore import DailyStore
    from scripts.autonomous_fund.risk import trading_day
    from scripts.autonomous_fund.strategy_lab_runtime import prepare_inputs

    engine = lab(tmp_path, max_history_rows=520)
    store = DailyStore(tmp_path / "daily")
    data = dataset()
    for name, symbols in (("first", ("ALFA", "BASE")), ("second", ("BETA", "INDEX"))):
        candidate = proposal(name)
        candidate["instruments"] = [symbols[0]]
        candidate["validation"]["benchmark"] = symbols[1]
        candidate["implementation"]["rules"][0].update(symbol=symbols[0], entry=f'field("{symbols[0]}", "close") > 0')
        engine.submit(candidate, NOW)
        for symbol, fixture in zip(symbols, ("ALFA", "BASE")):
            store.put(trading_day(NOW), "bars/" + symbol,
                      {"rows": [[row[k] for k in ("day", "close", "volume", "open", "high", "low")]
                                for row in data["bars"][fixture]]})

    inbox = tmp_path / "strategy_lab_snapshot.json"
    inbox.write_text("{broken JSON", encoding="utf-8")
    state = SimpleNamespace(strategy_lab=engine, daily_store=store, halt_path=tmp_path / "STOP")
    prepare_inputs(state, NOW)
    assert engine.journal.get_meta("strategy_lab_last_intake")["coverage_gaps"] == ["JSONDecodeError"]
    with engine.journal.connection() as db:
        jobs = db.execute("SELECT j.strategy_id,d.body FROM fund_strategy_jobs j JOIN fund_strategy_datasets d "
                          "ON d.digest=j.dataset WHERE j.state='queued' ORDER BY j.strategy_id").fetchall()
    assert {key: {symbol: len(rows) for symbol, rows in json.loads(body)["bars"].items()} for key, body in jobs} == {
        "first": {"ALFA": 170, "BASE": 170}, "second": {"BETA": 170, "INDEX": 170}}
    inbox.write_text(json.dumps(data), encoding="utf-8")
    prepare_inputs(state, NOW + timedelta(seconds=30))
    assert engine.journal.get_meta("strategy_lab_last_intake")["coverage_gaps"] == []


def test_live_lab_poll_does_not_depend_on_a_trading_iteration():
    from scripts.autonomous_fund.strategy_lab_runtime import poll_live_inputs

    called = Event()
    moments = []

    def tick(*, now):
        moments.append(now)
        called.set()

    strategy = SimpleNamespace(_strategy_lab_tick=tick)
    with poll_live_inputs(strategy, enabled=False, interval=.01):
        assert not moments
    with poll_live_inputs(strategy, enabled=True, interval=.01):
        assert called.wait(2)
    assert moments and all(now.tzinfo is not None for now in moments)


def test_old_policy_queue_does_not_keep_waking_worker(tmp_path):
    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    # Create a real queued job before changing its policy; short history now
    # produces a preflight gap without entering the queue.
    assert engine.schedule(dataset(), NOW)
    with engine.journal.connection() as db:
        db.execute("UPDATE fund_strategy_versions SET policy_hash='previous-policy'")
    assert not engine.kick(NOW)


def test_opening_deadline_surfaces_source_gaps_and_deduplicates_across_runner_restarts(tmp_path):
    from scripts.autonomous_fund.execution import EXECUTION_REVISION
    from scripts.autonomous_fund.lab import forward_readiness
    from scripts.autonomous_fund.monitor import Monitor
    from scripts.autonomous_fund.strategy_validation import following_session
    from tests.test_autonomous_fund_monitor import FakeTelegram, heartbeat

    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    key = proposal()["strategy_id"]
    _, opened = following_session("2026-11-16")
    pending = opened - timedelta(minutes=3)
    engine.journal.event(NOW, "startup", {"mode": "local", "execution_revision": EXECUTION_REVISION})
    engine.journal.set_meta("strategy_lab_last_intake", {"at": NOW.isoformat(), "coverage_gaps": ["source_artifacts_unchecked"]})
    with engine.journal.connection() as db:
        db.execute("UPDATE fund_strategy_versions SET state='shadow'")
        engine._event(db, key, "v1", NOW, "historical_pass", {})
        readiness = forward_readiness(db, pending, engine.admission_policy, opened=opened, budget=engine.budget.settings)
    assert readiness["waiting_reasons"] == {f"{key} / v1": "source_evidence_incomplete"}
    transport = FakeTelegram()
    heartbeat(tmp_path, pending)
    Monitor(tmp_path, transport).poll(pending)
    Monitor(tmp_path, transport).poll(pending + timedelta(seconds=30))
    heartbeat(tmp_path, opened)
    Monitor(tmp_path, transport).poll(opened)
    notices = [row["body"] for row in engine.journal.events(["notification"])]
    assert [notice["kind"] for notice in notices] == ["strategy_lab_open_pending", "strategy_lab_open_missed"]
    assert notices[-1]["details"]["versions"] == {f"{key} / v1": "missed"}
    assert any("开盘前仍待留证" in message for message in transport.messages)
    assert any("已错过开盘留证" in message for message in transport.messages)


def test_research_task_cannot_promote_a_strategy(tmp_path):
    from pydantic import ValidationError
    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    with pytest.raises(ValidationError):
        engine.update_task({"strategy_id": "synthetic-research", "version": "v1", "action": "paper_active",
                            "findings": "I approve myself", "next_step": "Trade"}, NOW)
    assert engine.rows()[0]["state"] == "candidate"


def test_frozen_forward_books_admission_and_degradation(tmp_path, monkeypatch):
    from scripts.autonomous_fund.lab import forward_audit, forward_readiness
    from scripts.autonomous_fund.strategy_admission import library, verify_allocations
    from scripts.autonomous_fund.strategy_evidence import load_frozen_dataset
    data = dataset(170)
    formed = instant(data["bars"]["BASE"][-1]["at"]) + timedelta(hours=1)
    engine = lab(tmp_path, admission="paper_library", min_forward_sessions=40)
    engine.submit(proposal(), formed)
    engine.schedule(data, formed)
    assert engine.run_one(formed)["passed"]
    assert engine.rows()[0]["state"] == "shadow"
    assert library(engine.journal, formed) == []
    # Every admitted observation must have a signal on record before its open.
    observed, result = forward_one_session_at_a_time(engine, dataset(210), 170, monkeypatch)
    assert result["observations"] == 40
    assert result["passed"] and engine.rows()[0]["state"] == "paper_active"
    assert result["qualification"]["status"] == "passed"
    assert result["qualification"]["end"] == result["series"][-1]["day"]
    audit = forward_audit(tmp_path, proposal()["strategy_id"], "v1", observed + timedelta(seconds=10))
    assert audit["window_evidence_complete"] and audit["checkpoint"]["status"] == "passed"
    assert audit["counts"] == {"prospective": 40} and audit["economic_comparison"]["sessions"] == 40
    assert sum(item["sessions"] for item in result["regimes"].values()) == 40
    assert 0 < result["volume_participation"]["max_fraction_of_daily_volume"] < 1
    with engine.journal.connection() as db:
        links = db.execute("SELECT body FROM fund_strategy_datasets WHERE json_type(body,'$.forward_base')='text'").fetchall()
        assert len(links) == 40 and sum(len(row[0]) for row in links) < 8000
        latest = db.execute("SELECT body FROM fund_strategy_forward ORDER BY day DESC LIMIT 1").fetchone()[0]
        frozen = json.loads(latest)["frozen_dataset_hash"]
        rebuilt = load_frozen_dataset(db, frozen)
        assert digest(rebuilt) == frozen and rebuilt['bars'] == dataset(210)['bars']
        readiness = forward_readiness(db, observed + timedelta(seconds=10), engine.admission_policy)
        assert readiness["ready"] == ["synthetic-research / v1"] and not readiness["waiting"]
        previous = forward_readiness(db, observed + timedelta(seconds=10),
                                     {**engine.admission_policy, "policy_hash": "new-policy"})
        assert previous["previous_policy"] == ["synthetic-research / v1"] and not previous["ready"]
    comparison, = engine.context(observed + timedelta(seconds=10))["competition"]["strategies"]
    assert comparison["return_basis"] == "prospective"
    assert comparison["risk_matched"]["prospective_prefix_sessions"] == 40
    assert comparison["risk_matched"]["evaluation_sessions"] == 19
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


def test_forward_audit_preserves_cutoffs_and_stops_economics_at_first_replayed_session(tmp_path, monkeypatch, capsys):
    from scripts.autonomous_fund import strategy_lab, strategy_worker
    from scripts.autonomous_fund.lab import forward_audit, main

    data = dataset(174)
    bars = data["bars"]["BASE"]
    freeze = instant(bars[169]["at"]) + timedelta(hours=1)
    engine = lab(tmp_path)
    engine.journal.event(freeze - timedelta(minutes=1), "agent", {"name": "researcher", "call_id": "pilot-call",
                                                                  "cost_usd": 0.24})
    key, version = engine.submit(proposal(), freeze, source_call_id="pilot-call")
    assert forward_audit(tmp_path, key, version, freeze)["status"] == "awaiting_historical_pass"
    monkeypatch.setattr(strategy_lab, "run_worker", lambda payload, _timeout: json.loads(
        json.dumps(strategy_worker.evaluate(payload), default=str)))
    for day_index in (169, 170, 172, 173):
        now = instant(bars[day_index]["at"]) + timedelta(hours=1)
        part = attest({**data, "bars": {symbol: values[:day_index + 1] for symbol, values in data["bars"].items()}})
        assert engine.schedule(part, now)
        assert engine.run_one(now)["status"] == ("historical_pass" if day_index == 169 else "forward")

    before_replay = instant(bars[172]["open_at"]) + timedelta(minutes=1)
    main(["forward-audit", "--run-dir", str(tmp_path), "--strategy-id", key, "--version", version,
          "--as-of", before_replay.isoformat()])
    audit = json.loads(capsys.readouterr().out)
    assert audit["first_evidence_gap"]["session"] == bars[172]["day"]
    assert audit["first_evidence_gap"]["status"] == "missed_signal"
    assert audit["counts"] == {"prospective": 1, "awaiting_observation": 1, "missed_signal": 1, "awaiting_signal": 1}
    assert audit["economic_comparison"]["sessions"] == 1

    after_replay = forward_audit(tmp_path, key, version, now + timedelta(seconds=10))
    assert after_replay["counts"] == {"prospective": 3, "replayed": 1, "ready_for_open": 1}
    assert after_replay["first_evidence_gap"]["status"] == "replayed"
    comparison = after_replay["economic_comparison"]
    assert comparison["sessions"] == 2 and comparison["through"] == bars[171]["day"]
    assert comparison["passive_reference"]["net_return"] is not None
    assert after_replay["originating_research_call"]["reported_call_cost_usd"] == 0.24
    assert after_replay["passive_reference"]["kind"] == "static_no_ai"
    assert not after_replay["window_evidence_complete"]


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
    # The snapshot is qualified data, but bulk replay cannot recreate timely
    # signal receipts. Its statistical result remains available for research.
    assert result["statistical_passed"] and result["observations"] == 65
    assert result["prospective_sessions"] == 1 and result["replayed_sessions"] == 64
    assert result["qualification"]["status"] == "incomplete"
    assert engine.rows()[0]["state"] == "forward_incomplete"
    assert not result["passed"] and library(engine.journal, resumed + timedelta(minutes=1)) == []


def test_strong_later_returns_cannot_reopen_the_first_forward_checkpoint():
    from scripts.autonomous_fund.strategy_worker import evaluate

    data = dataset(260)
    start = 170
    value = data["bars"]["ALFA"][start - 1]["close"]
    for i, bar in enumerate(data["bars"]["ALFA"][start:], start):
        opened = value
        value *= 1 + (-.0003 if i < 210 else .035 + .001 * (i % 3))
        bar.update(open=opened, close=value, high=max(opened, value), low=min(opened, value))
    payload = {"kind": "forward", "proposal": StrategyProposal.model_validate(proposal()).model_dump(),
        "data": data, "policy": policy(), "initial_capital": "10000", "trials": 1,
        "forward_after": (instant(data["bars"]["BASE"][start - 1]["at"]) + timedelta(hours=1)).isoformat(),
        "forward_plan": {"required_sessions": 40, "trials": 1}}
    result = evaluate(payload)
    assert result["tests"]["forward"]["passed"]
    assert result["qualification"]["end"] == data["bars"]["BASE"][209]["day"]
    assert result["qualification"]["status"] == "failed" and not result["passed"]


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


def test_regime_outcomes_use_only_the_benchmark_visible_before_the_open():
    from scripts.autonomous_fund.strategy_validation import regime_outcomes
    bars = dataset(211)["bars"]["BASE"]
    day = bars[-1]["day"]
    own, reference = {"daily_returns": [.022]}, {"daily_returns": [.01]}
    original = regime_outcomes([day], own, reference, bars)
    assert set(original) - {"unclassified"}
    later = copy.deepcopy(bars)
    later[-1]["close"] *= .3
    assert regime_outcomes([day], own, reference, later) == original
    later[-2]["available_at"] = (instant(later[-1]["open_at"]) + timedelta(seconds=1)).isoformat()
    assert regime_outcomes([day], own, reference, later)["unclassified"]["sessions"] == 1


def test_reference_costs_are_frozen_independently_of_proposer_assumptions():
    data = dataset(8)
    configured = {**policy(), "shared_data_cost_usd_per_session": "0.50"}
    version = proposal()
    days = [row["day"] for row in data["bars"]["BASE"]][1:]
    baseline = replay(version, data, configured, days, initial_capital="10000", static_weight=1)
    original = replay(version, data, configured, days, initial_capital="10000")
    version["costs"].update(slippage=.012, daily_data_usd=4, daily_model_usd=2)
    candidate = replay(version, data, configured, days, initial_capital="10000")
    assert replay(version, data, configured, days, initial_capital="10000", static_weight=1) == baseline
    assert float(baseline["cost_usd"]) == 3.5
    assert float(candidate["cost_usd"]) == 42
    assert float(candidate["summary"]["friction"]) > float(original["summary"]["friction"])
    missing = validate(version, data, {**configured, "shared_data_cost_usd_per_session": None})
    assert missing["status"] == "needs_data" and "shared data cost" in missing["reason"]


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


def test_history_before_warmup_does_not_block_scheduling_or_move_the_holdout(tmp_path):
    from scripts.autonomous_fund.strategy_validation import historical_input_gap

    engine, candidate, data = lab(tmp_path), proposal(), dataset()
    candidate["implementation"]["parameters"] = {"p_window": 5}
    candidate["implementation"]["rules"][0]["entry"] = 'mean("ALFA", "event", p_window) > 0'
    expected = [row["day"] for row in data["bars"]["BASE"][-140:]]
    data["bars"]["BASE"].pop(1)
    data["bars"]["ALFA"][1].pop("event")
    assert historical_input_gap(candidate, data, policy(), locked_sessions=expected) is None
    engine.submit(candidate, NOW)
    assert engine.schedule(attest(data), NOW)
    with engine.journal.connection() as db:
        locked, = db.execute("SELECT sessions FROM fund_strategy_holdouts").fetchone()
    assert json.loads(locked) == expected
    assert engine.run_one(NOW)["status"] == "historical_pass"


@pytest.mark.parametrize(("symbol", "index", "expression"), [
    ("ALFA", 27, 'mean("ALFA", "event", 5) > 0'),
    ("ALFA", 29, "True"),
    ("BASE", 29, 'field("ALFA", "event") > 0'),
    ("BASE", 1, "session_index >= 4"),
])
def test_required_warmup_and_absolute_index_history_still_reject_gaps(symbol, index, expression):
    from scripts.autonomous_fund.strategy_validation import historical_input_gap

    candidate, data = proposal(), dataset()
    candidate["implementation"]["rules"][0]["entry"] = expression
    data["bars"][symbol].pop(index)
    result = historical_input_gap(candidate, data, policy())
    assert result["status"] == "needs_data" and result["missing_symbols"] == [symbol]


def test_delayed_bar_keeps_earlier_observations_in_the_required_warmup():
    from scripts.autonomous_fund.strategy_validation import historical_input_gap

    candidate, data = proposal(), dataset()
    candidate["implementation"]["rules"][0]["entry"] = 'mean("ALFA", "event", 5) > 0'
    rows = data["bars"]["ALFA"]
    rows[29]["available_at"] = rows[30]["at"]
    rows[24].pop("event")
    result = historical_input_gap(candidate, data, policy())
    assert result["status"] == "needs_data" and result["missing_feature"] == {"symbol": "ALFA", "column": "event"}


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
        now = strategy.get_datetime()
        budget = strategy.strategy_lab.budget
        charge = budget.reserve(now, "synthetic-notice", "research_model", "0.50")
        budget.settle(charge, now, "0.20", {})
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
    [notice] = [r["body"]["details"] for r in strategy.journal.all_events()
                if r["kind"] == "notification" and r["body"]["kind"] == "premarket_research"]
    text = "\n".join(notice["strategy_research"])
    assert "本轮确认登记 1 条策略提案" in text and "$0.20 / $30" in text
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
    [notice] = [r["body"]["details"] for r in strategy.journal.all_events()
                if r["kind"] == "notification" and r["body"]["kind"] == "premarket_research"]
    text = "\n".join(notice["strategy_research"])
    assert "本轮确认登记 0 条策略提案" in text and "提案登记失败" in text
    assert "private path" not in text and "新策略提案" not in text
    assert not strategy.journal.pending()
