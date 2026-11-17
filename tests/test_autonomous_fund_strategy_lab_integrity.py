"""Regression controls for research evidence, allocation sizing and atomic job claims."""
import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.research_budget import ResearchBudgetSettings
from scripts.autonomous_fund.risk import RiskError, instant
from scripts.autonomous_fund.strategy_admission import verify_allocations
from scripts.autonomous_fund.strategy_definition import StrategyLabSettings, StrategyProposal
from scripts.autonomous_fund.strategy_lab import StrategyLab, run_worker
from scripts.autonomous_fund.strategy_validation import check_dataset, replay
from tests.test_autonomous_fund_strategy_lab import (NOW, attest, cash_dataset, dataset, forward_one_session_at_a_time,
                                                   lab, policy, proposal)


@pytest.mark.parametrize("cash_adapter", [False, True])
def test_allocation_uses_the_tested_sleeve_cap_instead_of_the_raw_program(cash_adapter, tmp_path, monkeypatch):
    from scripts.autonomous_fund import strategy_admission
    data, configured, candidate = cash_dataset(4) if cash_adapter else (dataset(4), policy(), proposal())
    configured["limits"]["max_position"] = ".1"
    candidate["implementation"]["rules"][0].update(entry="True", exit="False", weight="1")
    days = [r["day"] for r in data["bars"]["BASE"]]
    result = replay(candidate, data, configured, days[1:], initial_capital="10000")
    assert float(result["next_targets"]["ALFA"]) == pytest.approx(.1)
    engine = lab(tmp_path, admission="paper_library")
    allocation = {"strategy_id": candidate["strategy_id"], "version": "v1",
                  "digest": "a" * 64, "capital_weight": .5}
    # A half-capital sleeve tested at 10% may authorize only 5% of fund NAV.
    monkeypatch.setattr(strategy_admission, "library_entry", lambda *_: {
        "digest": allocation["digest"], "next_targets": result["next_targets"]})
    with engine.journal.connection() as db:
        verify_allocations(db, {"strategy_allocations": [allocation],
                               "targets": [{"symbol": "ALFA", "weight": .05}]}, NOW, purchase=True)
        with pytest.raises(RiskError, match="exceeds"):
            verify_allocations(db, {"strategy_allocations": [allocation],
                                   "targets": [{"symbol": "ALFA", "weight": .051}]}, NOW, purchase=True)


def forward_payload(data, configured=None):
    return {"kind": "forward", "proposal": StrategyProposal.model_validate(proposal()).model_dump(),
            "data": data, "policy": configured or policy(), "initial_capital": "10000", "trials": 1,
            "forward_after": (instant(data["bars"]["ALFA"][0]["at"]) + timedelta(hours=1)).isoformat(),
            "timeout_seconds": 30}


@pytest.mark.parametrize("missing_index", [1, 4])
def test_forward_cannot_skip_the_first_or_an_interior_exchange_session(missing_index):
    data = dataset(8)
    del data["bars"]["BASE"][missing_index]
    result = run_worker(forward_payload(data), 30)
    assert result["status"] == "needs_data" and not result["passed"]
    assert "missing exchange sessions" in result["reason"]
    assert "series" not in result


def test_complete_forward_history_starts_after_freeze_and_keeps_every_session():
    data = dataset(8)
    result = run_worker(forward_payload(data), 30)
    assert result["status"] == "forward" and result["observations"] == 7
    assert [r["day"] for r in result["series"]] == [r["day"] for r in data["bars"]["BASE"][1:]]


def test_worker_returns_the_combined_group_and_exposure_caps():
    data, configured, candidate = dataset(8), policy(), proposal()
    configured["limits"].update(max_position=".8", max_exposure=".6")
    configured["groups"] = [["shared-risk", ".3", ["ALFA", "BASE"]]]
    candidate["instruments"].append("BASE")
    candidate["implementation"]["rules"] = [
        {"symbol": symbol, "entry": "True", "exit": "False", "weight": ".5"} for symbol in ("ALFA", "BASE")]
    payload = forward_payload(data, configured)
    payload["proposal"] = StrategyProposal.model_validate(candidate).model_dump()
    result = run_worker(payload, 30)
    assert result["status"] == "forward"
    assert {s: float(w) for s, w in result["next_targets"].items()} == pytest.approx({"ALFA": .15, "BASE": .15})


def test_cash_signal_uses_the_same_buffer_and_friction_as_replay():
    data, configured, candidate = cash_dataset(4)
    candidate["implementation"]["rules"][0].update(entry="True", exit="False")
    days = [r["day"] for r in data["bars"]["BASE"]]
    result = replay(candidate, data, configured, days[1:], initial_capital="10000")
    expected = .5 * .99 * (1 - 2 * (.0001 + .0001 + .0001 * .0001))
    assert float(result["next_targets"]["ALFA"]) == pytest.approx(expected)


def test_verified_sleeve_cap_reaches_manager_acceptance_and_order_authority(tmp_path, monkeypatch):
    from scripts.autonomous_fund.execution import ExecutionState, OrderAuthorizationChanged, require_submission_authority
    from scripts.autonomous_fund.risk import Intent, Limits, number
    from scripts.autonomous_fund.strategy_admission import library
    def history(count):
        data = dataset(count)
        # A flat synthetic reference makes this a deterministic authorization
        # control, rather than a test of a random small sleeve beating equities.
        for row in data["bars"]["BASE"]:
            row.update(open=100., high=100., low=100., close=100.)
        return attest(data)

    configured, candidate, initial = policy(), proposal(), history(170)
    configured["limits"]["max_position"] = ".1"
    candidate["implementation"]["rules"][0].update(entry="True", exit="False", weight="1")
    journal = Journal(tmp_path, "synthetic-verified-sleeve")
    engine = StrategyLab(journal, configured, StrategyLabSettings(admission="paper_library", min_forward_sessions=40))
    formed = instant(initial["bars"]["BASE"][-1]["at"]) + timedelta(hours=1)
    engine.submit(candidate, formed)
    engine.schedule(initial, formed)
    assert engine.run_one(formed)["passed"]
    # The same sleeve tested prospectively is the only one authorized here.
    observed, result = forward_one_session_at_a_time(engine, history(210), 170, monkeypatch)
    assert result["passed"]
    now = observed + timedelta(minutes=10)
    entry, = library(journal, now)
    assert float(entry["next_targets"]["ALFA"]) == pytest.approx(.1)
    allocation = {k: entry[k] for k in ("strategy_id", "version", "digest")}
    allocation["capital_weight"] = .5
    decision = {"as_of": now.isoformat(), "expires_at": (now + timedelta(hours=2)).isoformat(),
                "next_review_at": (now + timedelta(hours=1)).isoformat(), "rationale": "Synthetic verified allocation",
                "strategy_allocations": [allocation], "targets": [{"symbol": "ALFA", "weight": .05}]}
    state = ExecutionState(journal)
    state.accept(decision, now, {"ALFA", "BASE"}, Limits())
    active = state.active(now, {"ALFA", "BASE"}, Limits())
    with journal.connection() as db:
        state.record_authority(db, "synthetic-intent", active["id"], Intent("ALFA", "buy", 1, number(100)), now,
                               kind="allocation", requirement=None)
    with pytest.raises(RiskError, match="exceeds admitted"):
        state.accept({**decision, "targets": [{"symbol": "ALFA", "weight": .1}]}, now, {"ALFA", "BASE"}, Limits())
    with journal.connection() as db:
        assert require_submission_authority(db, "synthetic-intent", now)["source"] == "automatic"
        db.execute("UPDATE meta SET value='unreadable' WHERE key='strategy_lab_policy'")
        with pytest.raises(OrderAuthorizationChanged, match="strategy_admission_changed"):
            require_submission_authority(db, "synthetic-intent", now)
        proof = state.record_authority(db, "synthetic-reduction", active["id"], Intent("ALFA", "sell", 1, number(100)), now,
                                       kind="allocation", requirement=None)
        assert proof["source"] == "risk_reducing"


@pytest.mark.parametrize("field", ["at", "open_at"])
def test_daily_prices_cannot_claim_an_earlier_session_clock(field):
    data = dataset(3)
    row = data["bars"]["ALFA"][1]
    row[field] = (instant(row[field]) - timedelta(hours=1)).isoformat()
    with pytest.raises(RiskError, match="exchange session"):
        check_dataset(data)


def test_a_holiday_cannot_become_a_price_session():
    data = dataset(3, start="2025-11-26")
    row = copy.deepcopy(data["bars"]["ALFA"][0])
    row["day"] = "2025-11-27"  # Thanksgiving: no regular US equity session.
    for key in ("at", "open_at", "available_at"):
        row[key] = (instant(row[key]) + timedelta(days=1)).isoformat()
    data["bars"]["ALFA"].insert(1, row)
    with pytest.raises(RiskError, match="exchange session"):
        check_dataset(data)


def test_half_days_and_delayed_publication_are_valid_daily_evidence():
    data = dataset(3, start="2025-11-26")
    for rows in data["bars"].values():
        for row in rows:
            row["available_at"] = (instant(row["at"]) + timedelta(minutes=15)).isoformat()
    assert instant(data["bars"]["BASE"][1]["at"]).hour == 18  # 13:00 EST.
    assert check_dataset(data)


def test_validated_only_does_not_fail_open_if_library_tables_are_missing(tmp_path):
    journal = Journal(tmp_path, "synthetic-partial-restore")
    with journal.connection() as db:
        db.execute("INSERT INTO meta VALUES(?,?)", ("strategy_lab_policy", json.dumps({
            "enabled": True, "admission": "paper_library", "allocation_mode": "validated_only"})))
        with pytest.raises(RiskError, match="require an admitted"):
            verify_allocations(db, {"targets": [{"symbol": "ALFA", "weight": .1}]}, NOW, purchase=True)
        verify_allocations(db, {"targets": []}, NOW, purchase=False)
        db.execute("UPDATE meta SET value=? WHERE key='strategy_lab_policy'", (json.dumps({
            "enabled": True, "admission": "research_only", "allocation_mode": "legacy_mandate"}),))
        verify_allocations(db, {"targets": [{"symbol": "ALFA", "weight": .1}]}, NOW, purchase=True)
        with pytest.raises(RiskError, match="unavailable"):
            verify_allocations(db, {"targets": [], "strategy_allocations": [{}]}, NOW, purchase=True)


def test_daily_job_slot_and_money_are_reserved_in_the_claim_transaction(tmp_path):
    engine = lab(tmp_path, max_jobs_per_day=1)
    engine.submit(proposal("first-candidate"), NOW)
    engine.submit(proposal("second-candidate"), NOW)
    engine.schedule(dataset(), NOW)
    # Claims overlap before either worker begins. Dollars for both are available.
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: engine._claim(NOW), range(2)))
    assert sum(row is not None for row in claims) == 1
    claimed = next(row for row in claims if row)
    assert claimed["reservation"]
    with engine.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_research_reservations").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM fund_strategy_jobs WHERE state='queued'").fetchone()[0] == 1
    assert engine.budget.summary(NOW)["daily_committed_usd"] == .05


@pytest.mark.parametrize("saved", ["invalid json", "[]"])
def test_unreadable_library_policy_blocks_purchases_without_blocking_reductions(tmp_path, saved):
    journal = Journal(tmp_path, "synthetic-policy-restore")
    with journal.connection() as db:
        db.execute("INSERT INTO meta VALUES('strategy_lab_policy',?)", (saved,))
        with pytest.raises(RiskError, match="unavailable"):
            verify_allocations(db, {"targets": []}, NOW, purchase=True)
        verify_allocations(db, {"targets": []}, NOW, purchase=False)


def test_missing_result_table_is_an_order_denial_instead_of_a_database_error(tmp_path):
    engine = lab(tmp_path, admission="paper_library")
    candidate = proposal()
    engine.submit(candidate, NOW)
    with engine.journal.connection() as db:
        row = db.execute("SELECT digest FROM fund_strategy_versions").fetchone()
        db.execute("UPDATE fund_strategy_versions SET state='paper_active'")
        db.execute("DROP TABLE fund_strategy_results")
        with pytest.raises(RiskError, match="unavailable"):
            verify_allocations(db, {"targets": [], "strategy_allocations": [{
                "strategy_id": candidate["strategy_id"], "version": "v1", "digest": row[0], "capital_weight": .5}]},
                NOW, purchase=True)


def test_failed_lease_write_rolls_back_its_budget_reservation(tmp_path):
    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    engine.schedule(dataset(), NOW)
    with engine.journal.connection() as db:
        db.execute("CREATE TRIGGER reject_lease BEFORE UPDATE ON fund_strategy_jobs "
                   "BEGIN SELECT RAISE(ABORT,'synthetic lease failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic lease failure"):
        engine._claim(NOW)
    assert engine.budget.summary(NOW)["daily_committed_usd"] == 0
    with engine.journal.connection() as db:
        assert db.execute("SELECT state,lease,reservation FROM fund_strategy_jobs").fetchone()[:] == ("queued", None, None)


def test_insufficient_money_does_not_lease_a_job_or_consume_a_daily_slot(tmp_path):
    engine = lab(tmp_path)
    engine.submit(proposal(), NOW)
    engine.schedule(dataset(), NOW)
    assert engine.budget.reserve(NOW, "shared-research", "research_model", "2")
    assert engine._claim(NOW) is None
    with engine.journal.connection() as db:
        assert db.execute("SELECT state,lease,reservation FROM fund_strategy_jobs").fetchone()[:] == ("queued", None, None)


def test_exhausted_project_does_not_starve_independent_queued_research(tmp_path):
    journal = Journal(tmp_path, "synthetic-budget-owner")
    engine = StrategyLab(journal, policy(), budget_settings=ResearchBudgetSettings(project_usd="1"))
    engine.submit(proposal("exhausted-project"), NOW)
    engine.submit(proposal("funded-project"), NOW + timedelta(seconds=1))
    scheduled_at = NOW + timedelta(seconds=2)
    assert len(engine.schedule(dataset(), scheduled_at)) == 2
    assert engine.budget.reserve(NOW, "exhausted-project", "research_model", "1")
    # The oldest job is queued first, but only the second project can afford
    # a validation. No lease or daily attempt should be spent on the first.
    claimed = engine._claim(scheduled_at)
    assert claimed is not None and claimed["strategy_id"] == "funded-project"
    assert engine._claim(scheduled_at) is None
    with journal.connection() as db:
        assert dict(db.execute("SELECT strategy_id,state FROM fund_strategy_jobs")) == {
            "exhausted-project": "queued", "funded-project": "running"}
        assert db.execute("SELECT count(*) FROM fund_research_reservations "
                          "WHERE category='validation_compute'").fetchone()[0] == 1


def test_a_faster_second_job_does_not_backdate_its_validation_receipt(tmp_path, monkeypatch):
    from scripts.autonomous_fund import strategy_lab
    engine = lab(tmp_path)
    engine.submit(proposal("first-project"), NOW)
    engine.submit(proposal("second-project"), NOW)
    engine.schedule(dataset(), NOW)
    elapsed = [0]

    def simulated_worker(payload, _timeout):
        elapsed[0] += 8 if payload["proposal"]["strategy_id"] == "first-project" else 1
        return {"status": "needs_data", "passed": False}

    monkeypatch.setattr(strategy_lab, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(strategy_lab, "run_worker", simulated_worker)
    engine._drain(NOW)
    with engine.journal.connection() as db:
        completed = db.execute("SELECT j.strategy_id,r.at FROM fund_strategy_results r "
                               "JOIN fund_strategy_jobs j ON j.id=r.job_id ORDER BY r.at").fetchall()
    assert [row["strategy_id"] for row in completed] == ["first-project", "second-project"]
