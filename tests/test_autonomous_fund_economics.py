"""Offline economic diagnostics use prior evidence, explicit costs and account-local receipts.

Synthetic histories and native application doubles only; no provider inference or network.
"""
import json
import math
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts.autonomous_fund.economics import (assess, decision_comparison, lines, opening_reference,
                                              saved_closes, weights)
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import instant
from scripts.autonomous_fund.scorecard import _exchange_sessions, beta_cutoff, score_decisions

ZONE = ZoneInfo("America/New_York")
DAYS = list(_exchange_sessions("2026-07-01", "2026-12-02"))


def history():
    prices, result = {"SPY": 100., "ALFA": 100.}, {"SPY": [], "ALFA": []}
    for i, day in enumerate(DAYS):
        move = .003 * math.sin(i) + .001
        for symbol, multiplier in (("SPY", 1), ("ALFA", 2)):
            prices[symbol] *= 1 + multiplier * move
            result[symbol].append((day, prices[symbol]))
    return result


def target(key, symbol="ALFA", weight=.5):
    return {"decision_id": key, "targets": [{"symbol": symbol, "weight": weight}] if weight else []}


def formed(index=40, time="10:00:00"):
    return f"{DAYS[index]}T{time}-04:00"


def test_intraday_beta_never_uses_the_later_scoring_start_close():
    closes = history()
    current = target("plan")
    before = score_decisions([(formed(), current)], closes, sessions=DAYS)["recent"][0]
    # A same-day close unknown at 10:00 must not change the ex-ante beta.
    closes["ALFA"] = [(d, p * (10 if d >= DAYS[40] else 1)) for d, p in closes["ALFA"]]
    after = score_decisions([(formed(), current)], closes, sessions=DAYS)["recent"][0]
    assert before["beta"] == pytest.approx(1)
    assert after["beta"] == before["beta"]
    assert before["beta_through"] < before["start_day"]


def test_beta_and_start_respect_early_close_and_exact_acceptance_time():
    assert beta_cutoff("2026-11-27T12:59:59-05:00") == "2026-11-26"
    assert beta_cutoff("2026-11-27T13:00:00-05:00") == "2026-11-27"
    result = decision_comparison([
        ("2026-11-27T12:59:59-05:00", target("a")),
        ("2026-11-27T13:00:00-05:00", target("b"))], history(), sessions=DAYS)
    assert [r["start_day"] for r in result["recent"]] == ["2026-11-27", "2026-11-30"]
    assert result["recent"][0]["beta_through"] == "2026-11-25"  # Thanksgiving is closed.
    assert result["recent"][1]["beta_through"] == "2026-11-27"


def test_change_value_and_review_cost_are_distinct_from_beta_return():
    closes = history()
    result = decision_comparison([(formed(), target("new"))], closes, sessions=DAYS,
        bases={"new": {"previous_decision": target("old", weight=0), "evaluation_call_ids": ["critic", "manager"],
                       "evaluation_cost_scope": "manager_corrections_fresh_critic_v1", "account_equity": "10000"}},
        calls={"critic": .2, "manager": .8})
    row = result["recent"][0]
    value = row["horizons"]["1"]
    expected = .5 * (closes["ALFA"][41][1] / closes["ALFA"][40][1] - 1)
    assert value["new_target_return"] == pytest.approx(expected)
    assert value["prior_target_return"] == 0
    assert row["estimated_trading_cost"] == pytest.approx(.0005)
    assert value["change_benefit_after_estimated_friction"] == pytest.approx(expected - .0005)
    assert value["benefit_after_linked_review_cost"] == pytest.approx(expected - .0006)
    assert value["beta_adjusted_return_after_estimated_friction"] == pytest.approx(-.0005)


def test_unchanged_targets_have_no_trading_benefit_but_still_cost_inference():
    result = decision_comparison([(formed(), target("new"))], history(), sessions=DAYS,
        bases={"new": {"previous_decision": target("old"), "evaluation_call_ids": ["manager"],
                       "account_equity": 10000}}, calls={"manager": 1.})
    row = result["recent"][0]
    assert row["turnover"] == 0
    assert row["horizons"]["5"]["change_benefit_after_estimated_friction"] == 0
    assert row["horizons"]["5"]["benefit_after_linked_review_cost"] == -.0001


@pytest.mark.parametrize("links,calls", [(None, {}), ([], {}), (["missing"], {}), (["a"], {"a": None}),
                                         (["a", "a"], {"a": 1.})])
def test_legacy_missing_or_duplicate_call_evidence_does_not_become_free(links, calls):
    basis = {"previous_decision": target("old"), "evaluation_call_ids": links, "account_equity": 10000}
    row = decision_comparison([(formed(), target("new"))], history(), sessions=DAYS,
                             bases={"new": basis}, calls=calls)["recent"][0]
    assert row["review_cost_usd"] is None
    assert row["horizons"]["1"]["benefit_after_linked_review_cost"] is None


def test_first_plan_is_not_assumed_to_replace_cash_and_explicit_basis_wins():
    events = [(formed(40), target("first")), (formed(41), target("second"))]
    rows = decision_comparison(events, history(), sessions=DAYS,
                               bases={"second": {"previous_decision": None}})["recent"]
    assert all(r["horizons"]["1"]["status"] == "unavailable_previous_plan" for r in rows)
    legacy = decision_comparison(events, history(), sessions=DAYS)["recent"]
    assert legacy[1]["horizons"]["1"]["status"] == "observed"


def test_missing_endpoint_is_not_replaced_and_overlapping_decisions_are_counted():
    closes = history()
    events = [(formed(40), target("first")), (formed(40, "11:00:00"), target("second")),
              (formed(41), target("third"))]
    bases = {b["decision_id"]: {"previous_decision": target("old")} for _, b in events}
    report = decision_comparison(events, closes, sessions=DAYS, bases=bases)
    assert report["by_horizon"]["1"]["observed"] == 3
    assert report["by_horizon"]["1"]["non_overlapping"] == 2
    closes["ALFA"] = [(d, p) for d, p in closes["ALFA"] if d != DAYS[41]]
    missing = decision_comparison(events[:1], closes, sessions=DAYS, bases=bases)["recent"][0]
    assert missing["horizons"]["1"]["status"] == "unavailable"
    pending = decision_comparison(events[:1], history(), sessions=DAYS[:41], bases=bases)["recent"][0]
    assert pending["horizons"]["1"]["status"] == "pending"


@pytest.mark.parametrize("targets", [[("A", True)], [("A", float("nan"))], [("A", -.1)],
                                     [("A", 0), ("A", .1)], [("A", .6), ("B", .5)]])
def test_invalid_target_evidence_cannot_produce_a_return(targets):
    with pytest.raises(ValueError):
        weights({"targets": [{"symbol": s, "weight": w} for s, w in targets]})


def interval():
    return [{"start_day": DAYS[40], "end_day": DAYS[41], "return": .03}]


def test_opening_reference_is_frozen_and_includes_declared_cash_proxy():
    a, b = DAYS[40:42]
    book = {a: {"equity": 10000, "positions": {"SPY": 40}, "position_prices": {"SPY": 100}}}
    marks = {"SPY": {a: 100, b: 110}, "PARK": {a: 100, b: 101}}
    result = opening_reference(interval(), book, history(), marks, {}, cash_symbol="PARK")
    assert result["reference_return"] == pytest.approx(.046)
    assert result["gross_difference"] == pytest.approx(-.016)
    assert result["recent"][0]["opening_beta"] == .4
    changed = history()
    changed["ALFA"] = [(d, p * (100 if d > a else 1)) for d, p in changed["ALFA"]]
    leveraged_book = {a: {"equity": 10000, "positions": {"ALFA": 50}, "position_prices": {"ALFA": 100}}}
    frozen = opening_reference(interval(), leveraged_book, changed, marks, {})
    assert frozen["recent"][0]["opening_beta"] == pytest.approx(1)


@pytest.mark.parametrize("reason", ["beta", "marks", "cash_policy", "zero_equity", "leverage"])
def test_missing_opening_evidence_does_not_invent_a_matched_return(reason):
    a = DAYS[40]
    book = {a: {"equity": 10000, "positions": {"ALFA": 50}, "position_prices": {"ALFA": 100}}}
    closes, kwargs = history(), {}
    if reason == "beta":
        closes["ALFA"] = closes["ALFA"][-5:]
    elif reason == "marks":
        book[a]["position_prices"] = {}
    elif reason == "cash_policy":
        book[a]["positions"] = {}
        kwargs["cash_policies"] = {a: (False, None)}
    elif reason == "zero_equity":
        book[a]["equity"] = 0
    else:
        book[a]["positions"]["ALFA"] = 100
    report = opening_reference(interval(), book, closes, {}, {}, **kwargs)
    assert report["status"] == "incomplete" and report["reference_return"] is None


def test_a_gap_in_every_history_is_not_mistaken_for_one_daily_beta_observation():
    closes = {s: [(d, p) for d, p in rows[:22] if d != DAYS[10]] for s, rows in history().items()}
    a, b = DAYS[21:23]
    books = {a: {"equity": 10000, "positions": {"ALFA": 50}, "position_prices": {"ALFA": 100}}}
    report = opening_reference([{"start_day": a, "end_day": b, "return": 0}], books, closes, {}, {})
    # There are 19 valid daily return pairs, not 20: the missing session invalidates both adjacent pairs.
    assert report["recent"][0]["reason"] == "missing_opening_marks_or_beta"


def recorded_account(root, binding="synthetic-economic-account"):
    from scripts.autonomous_fund.performance import PerformanceSettings
    journal = Journal(root, binding)
    a, b = "2026-11-09", "2026-11-10"
    journal.event(instant(a + "T15:00:00Z"), "startup", {"cash_management_symbol": None,
                  "performance": {"data_costs_usd_per_month": 0, "benchmarks": PerformanceSettings().definitions()}})
    for day, equity in ((a, 10000), (b, 15000)):
        if day == b:
            journal.event(instant(b + "T15:00:00Z"), "capital_flow", {
                "kind": "subscription", "amount": "5000", "units_before": "10000", "units_after": "15000"})
        close = day + "T16:00:00-05:00"
        journal.event(instant(close), "account", {"equity": equity, "cash": equity, "positions": {},
            "valuation": {"session_close": close}, "as_of": close})
        journal.event(instant(close), "benchmark", {"SPY": 100, "PARK": 100})
    return journal


def test_readonly_assessment_preserves_flow_adjustment_and_account_isolation(tmp_path, monkeypatch, capsys):
    root, other = tmp_path / "selected", tmp_path / "unrelated"
    journal = recorded_account(root)
    recorded_account(other)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = assess(root, now="2026-11-10T22:00:00Z")
    assert result["fixed_benchmarks_and_fund"]["account_return"] == 0
    assert result["opening_reference"]["reference_return"] == 0
    assert result["opening_reference"]["net_difference"] == 0
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert {p: after[p] for p in before} == before
    # SQLite mode=ro may create empty WAL/shared-memory coordination files; business records stay untouched.
    assert set(after) - set(before) <= {root / "state.sqlite3-wal", root / "state.sqlite3-shm"}
    # Native CLI selects this directory and must return before loading models or creating a new journal.
    from scripts.autonomous_fund import run
    monkeypatch.setattr(run, "model_ready", lambda *a: pytest.fail("offline diagnostics must not load a model"))
    run.main(["economics", "--run-dir", str(root)])
    assert "只读经济评估" in capsys.readouterr().out
    assert not (root / "economics.json").exists()


def test_receipts_and_calls_are_strictly_cut_off_and_unpriced_cost_stays_unknown(tmp_path):
    journal = recorded_account(tmp_path)
    journal.event(instant("2026-11-10T20:00:00Z"), "agent", {"name": "manager", "model": "provider/model"})
    # A later stale/failed receipt and a later startup cannot rewrite the earlier report.
    journal.event(instant("2026-11-11T22:00:00Z"), "account", {
        "equity": 1, "positions": {}, "valuation": {"session_close": "2026-11-10T21:00:00Z"},
        "ledger_audit": {"status": "failed"}})
    result = assess(tmp_path, now="2026-11-10T22:00:00Z")
    assert result["opening_reference"]["reference_return"] == 0
    assert result["opening_reference"]["net_difference"] is None
    assert result["fixed_benchmarks_and_fund"]["unpriced_calls"] == 1


def test_cost_links_are_loaded_exactly_with_microsecond_cutoff_and_mock_zero(tmp_path):
    journal = recorded_account(tmp_path)
    at = instant("2026-11-09T15:00:00Z")
    cutoff = instant("2026-11-10T22:00:00Z")
    journal.event(at, "decision_validated", target("new"))
    journal.event(at, "decision_basis", {"decision_id": "new", "previous_decision": target("old", weight=0),
        "account_equity": 10000, "evaluation_call_ids": ["manager", "critic"]})
    journal.event(at, "agent", {"call_id": "manager", "model": "provider/model", "cost_usd": .4})
    journal.event(at, "agent", {"call_id": "critic", "mock": True})
    journal.event(cutoff + timedelta(microseconds=1), "agent", {"call_id": "manager", "cost_usd": 999})
    row, = assess(tmp_path, now=cutoff)["decisions"]["recent"]
    assert row["review_cost_usd"] == .4  # No proximity guess, missing-usage charge or microsecond future row.


def test_offline_command_uses_configured_directory_and_never_imports_runtime(tmp_path):
    from tests.test_autonomous_fund_weekly_delivery import account_book, script
    book, env = account_book(tmp_path)
    recorded_account(book, binding="synthetic-script-account")
    result = script("fund-logs.sh", tmp_path, env, "economics")
    assert result.returncode == 0 and result.stdout.startswith("只读经济评估")
    assert "LumiBot v" not in result.stdout
    assert not (tmp_path / "artifacts/autonomous-fund").exists()


def test_weekly_comparison_is_cut_off_at_its_review_not_at_query_time(tmp_path):
    from scripts.autonomous_fund.notifications import weekly_document
    from tests.test_autonomous_fund_weekly_report import review
    journal = recorded_account(tmp_path)
    key = review(journal, "2026-11-10", "Synthetic review")
    _, before = weekly_document(tmp_path, key)
    assert "经济对照（截至报告截止时间的全部可用记录）" in before
    assert "期初贝塔对照：覆盖 1/1" in before and "经济评估暂不可用" not in before
    journal.event(instant("2026-11-11T15:00:00Z"), "agent", {
        "model": "provider/model", "cost_usd": 500, "call_id": "later-call"})
    assert weekly_document(tmp_path, key)[1] == before


def test_postclose_policy_change_cannot_rewrite_a_backfilled_starting_close(tmp_path):
    journal = Journal(tmp_path, "synthetic-backfilled-close")
    for at, proxy in (("2026-11-09T15:00:00Z", None), ("2026-11-09T21:01:00Z", "PARK")):
        journal.event(instant(at), "startup", {"cash_management_symbol": proxy,
                      "performance": {"data_costs_usd_per_month": 0}})
    for day, receipt, park in (("2026-11-09", "21:02:00", 100), ("2026-11-10", "21:00:00", 110)):
        closed = day + "T21:00:00Z"
        journal.event(instant(day + "T" + receipt + "Z"), "account", {
            "equity": 10000, "positions": {}, "valuation": {"session_close": closed}})
        journal.event(instant(day + "T" + receipt + "Z"), "benchmark", {"SPY": 100, "PARK": park})
    report = assess(tmp_path, now="2026-11-10T22:00:00Z")
    assert report["opening_reference"]["reference_return"] == 0
    assert report["opening_reference"]["recent"][0]["cash_proxy"] == "USD_zero_return"


def test_missing_directory_or_closing_receipt_does_not_invent_performance(tmp_path):
    from scripts.autonomous_fund.risk import RiskError
    with pytest.raises(RiskError, match="No journal"):
        assess(tmp_path / "absent")
    assert not (tmp_path / "absent").exists()
    journal = Journal(tmp_path, "synthetic-intraday-only")
    journal.event(instant("2026-11-10T20:59:59Z"), "account", {"equity": 10000, "positions": {}})
    result = assess(tmp_path, now="2026-11-10T22:00:00Z")
    assert result["opening_reference"]["intervals"] == 0
    assert result["opening_reference"]["reference_return"] is None


def test_saved_histories_ignore_future_sessions_and_never_join_rebased_series(tmp_path):
    old, new = tmp_path / "cache/daily/2026-11-09/bars", tmp_path / "cache/daily/2026-11-10/bars"
    for folder in (old, new):
        folder.mkdir(parents=True)
    (old / "ALFA.json").write_text(json.dumps({"rows": [["2026-11-06", 200], ["2026-11-09", 202]]}))
    (new / "ALFA.json").write_text(json.dumps({"rows": [["2026-11-09", 101], ["2026-11-10", 103]]}))
    with sqlite3.connect(":memory:") as db:
        rows = saved_closes(tmp_path, db, "2026-11-10T15:00:00-05:00")
    assert rows == {"ALFA": [("2026-11-09", 101.)]}


def test_real_application_records_exact_call_chain_including_correction(tmp_path):
    from tests.test_autonomous_fund_decision_continuity import model_cycle
    from tests.test_autonomous_fund_quant import NOW, account
    from scripts.autonomous_fund.strategy import AutonomousFund
    strategy, _, _ = model_cycle(tmp_path)
    agent = strategy.agents["manager"].run
    calls = []

    def answer(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return SimpleNamespace(parsed=None, summary="Not JSON", parse_error="missing object",
                model=strategy.agent_models["manager"], usage={"input_tokens": 20, "output_tokens": 10}, cache_hit=False)
        return agent(**kwargs)

    strategy.agents["manager"].run = answer
    strategy._decide(account(), NOW, False)
    events = strategy.journal.all_events()
    basis, = [r["body"] for r in events if r["kind"] == "decision_basis"]
    agent_rows = [r["body"] for r in events if r["kind"] == "agent"]
    assert [r["name"] for r in agent_rows] == ["critic", "manager", "manager"]
    assert basis["evaluation_call_ids"] == [r["call_id"] for r in agent_rows]
    assert basis["account_equity"] == str(account().equity)
    assert basis["evaluation_cost_scope"] == "manager_corrections_fresh_critic_v1"
    assert all("evaluation_calls" not in kwargs for kwargs in calls)  # Never leaks a journal detail to the SDK.
    captured = []
    context = {"account": {"positions": {}}, "previous_decision": None, "recent_trades": []}
    research = {"summary": "Synthetic", "evidence": []}
    AutonomousFund._critique(strategy, context, research, strategy.model_budget, NOW, evaluation_calls=captured)
    AutonomousFund._critique(strategy, context, research, strategy.model_budget, NOW, evaluation_calls=captured)
    assert len(captured) == 1  # Reusing a prior critique does not charge it a second time.
