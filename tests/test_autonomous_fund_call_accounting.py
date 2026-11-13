"""Call adoption and unknown usage remain distinct from trading effectiveness.

Native application methods and synthetic journals/results; no inference or APIs.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund import call_value
from scripts.autonomous_fund.costs import cost_summary
from scripts.autonomous_fund.decision_activity import activity, lines
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.strategy import AutonomousFund
from tests.test_autonomous_fund_decision_activity import NOW as ACTIVITY_NOW, OPEN, plan
from tests.test_autonomous_fund_quant import NOW, account, harness
from tests.test_autonomous_fund_triage import NEWS, fund, verdict


@pytest.mark.parametrize("action", ["dismissed", "audit", "escalated", "shadow", "deadline"])
def test_triage_outcomes_link_to_the_actual_recorded_call(tmp_path, action):
    strategy, _ = fund(tmp_path, "shadow" if action == "shadow" else "enforce", [])
    strategy.scripted = False
    strategy.triage_model = "provider/screen"
    strategy.agent_models = {"manager": "provider/manage", "triage": strategy.triage_model}
    strategy.config = {"model_prices_per_million": {strategy.triage_model: {"input": 1, "output": 2}}}
    strategy._record_agent = AutonomousFund._record_agent.__get__(strategy)
    strategy._triggers = [NEWS]
    strategy.journal.set_meta("scheduled_review_at", (NOW + timedelta(minutes=40)).isoformat())
    if action == "audit":
        strategy.journal.set_meta("triage_dismissal_count", 4)

    def run(**kwargs):
        if action == "deadline":
            strategy.get_datetime = lambda: NOW + timedelta(hours=2)
        return SimpleNamespace(parsed=verdict(action == "escalated"), usage={"input_tokens": 100, "output_tokens": 20},
                               model=strategy.triage_model, cache_hit=False, summary="Synthetic screen", parse_error=None)

    strategy.agents["triage"] = SimpleNamespace(run=run)
    strategy._triage_gate(account(), NOW)
    events = strategy.journal.all_events()
    call, = [r["body"] for r in events if r["kind"] == "agent"]
    outcome, = [r["body"] for r in events if r["kind"] == "agent_outcome"]
    screen, = [r["body"] for r in events if r["kind"] == "event_triage"]
    assert outcome["call_id"] == screen["call_id"] == call["call_id"]
    assert outcome["triage_id"] == screen["id"] and outcome["action"] == screen["action"] == action
    assert outcome["valid"] is (action != "deadline")  # A mandatory review ignores the late verdict.
    value = call_value.from_events([(r["at"], r["kind"], r["body"]) for r in events])["by_purpose"]["triage"]
    assert value["used"] == int(action != "deadline") and value["outcome_unknown"] == 0


def test_a_saved_identity_probe_records_adoption_without_claiming_a_trade(tmp_path):
    from tests.test_autonomous_fund_model_identity import CLOSE, MODEL, answer, live
    strategy = live(tmp_path)
    strategy._agent_call = AutonomousFund._agent_call.__get__(strategy)
    strategy.get_datetime = lambda: CLOSE
    strategy.probe_agents = {"model_probe_1": MODEL}
    strategy.agents = {"model_probe_1": SimpleNamespace(run=lambda **kw: SimpleNamespace(
        parsed=answer("2025-04"), usage={"input_tokens": 10, "output_tokens": 10}, model=MODEL,
        cache_hit=False, summary="Synthetic probe", parse_error=None))}
    strategy._probe_models(CLOSE)
    events = strategy.journal.all_events()
    calls = [r["body"] for r in events if r["kind"] == "agent"]
    outcomes = [r["body"] for r in events if r["kind"] == "agent_outcome"]
    assert len(calls) == len(outcomes) == 1 and calls[0]["call_id"] == outcomes[0]["call_id"]
    assert outcomes[0]["purpose"] == "model_probe" and outcomes[0]["valid"]
    assert not outcomes[0].get("changed_targets")


@pytest.mark.parametrize("action", ["escalated", "audit", "shadow"])
@pytest.mark.parametrize("changed", [True, False])
def test_timeline_describes_manager_change_separately_from_miss_rate(tmp_path, action, changed):
    book = Journal(tmp_path, "synthetic-screen-account")
    plan(book, 0)
    book.event(OPEN + timedelta(minutes=5), "event_triage", {
        "id": "screen-1", "action": action, "verdict": {"escalate": action == "escalated"}})
    at, _ = plan(book, 1, targets=[{"symbol": "ALFA", "weight": .08 if changed else .05}])
    book.event(at, "triage_scored", {"id": "screen-1", "manager_changed": changed,
                                     "miss": changed and action != "escalated"})
    text = "\n".join(lines(activity(tmp_path, "2026-11-02", now=ACTIVITY_NOW), timeline=True))
    expected = "基金经理改了组合" if changed else "基金经理没改组合"
    assert expected in text
    assert ("算漏判" in text) is (changed and action != "escalated")
    if action == "escalated":
        assert "抽查结果" not in text


def test_a_legacy_escalation_without_manager_result_is_not_labeled_no_change(tmp_path):
    book = Journal(tmp_path, "synthetic-legacy-screen")
    plan(book, 0)
    book.event(OPEN + timedelta(minutes=5), "event_triage", {
        "id": "legacy-screen", "action": "escalated", "verdict": {"escalate": True}})
    at, _ = plan(book, 1, targets=[])
    book.event(at, "triage_scored", {"id": "legacy-screen", "miss": False})
    text = "\n".join(lines(activity(tmp_path, "2026-11-02", now=ACTIVITY_NOW), timeline=True))
    assert "基金经理没改组合" not in text


@pytest.mark.parametrize("usage,reason", [
    (None, "usage_missing"), ({}, "usage_missing"), ({"input_tokens": 12}, "usage_incomplete"),
    ("invalid-usage", "usage_invalid"), ({"input_tokens": 12, "output_tokens": 3}, "rate_missing"),
    ({"input_tokens": float("nan"), "output_tokens": 3}, "usage_invalid")])
def test_unpriced_call_records_why_without_guessing_tokens(tmp_path, usage, reason):
    strategy = harness(tmp_path)
    strategy.scripted, strategy.config, strategy.agent_models = False, {}, {"researcher": "provider/research"}
    result = SimpleNamespace(model="provider/research", usage=usage, cache_hit=False, parsed={"summary": "s"},
                             summary="Synthetic research", parse_error=None)
    strategy._record_agent("researcher", result)
    body, = [r["body"] for r in strategy.journal.all_events() if r["kind"] == "agent"]
    assert body["cost_usd"] is None and body["cost_unavailable_reason"] == reason
    summary = cost_summary([{"at": NOW, "kind": "agent", "body": body}], now=NOW)
    assert summary["unpriced_calls"] == 1 and summary["unpriced_by_reason"] == {reason: 1}
    assert not summary["complete"]


def test_legacy_missing_usage_is_unknown_in_every_weekly_cost_section(tmp_path):
    from scripts.autonomous_fund.notifications import weekly_document
    from tests.test_autonomous_fund_weekly_report import close_of, ledger
    journal, sequence = ledger(tmp_path)
    journal.event(close_of("2026-11-05", -50), "agent", {
        "call_id": "missing-usage", "purpose": "triage", "name": "triage", "model": "provider/screen",
        "usage": None, "cost_usd": None})
    journal.event(close_of("2026-10-30"), "agent", {"usage": None})  # Outside the report week.
    journal.event(close_of("2026-11-06", 40), "agent", {"usage": None})  # After the selected review cutoff.
    _, text = weekly_document(tmp_path, sequence)
    assert "模型调用 3 次，估算费用 0.2500 美元（另有 2 次未定价，不按 0 计）" in text
    section = next(line for line in text.splitlines() if "事件判断：" in line)
    assert "1 次未定价" in section and "采用结果未记录 1 次" in section
    assert "未被采用的调用花了" not in section


def test_old_successful_calls_without_outcomes_are_not_proof_of_waste():
    calls = [{"call_id": "old-screen", "purpose": "triage", "cost_usd": .02},
             {"call_id": "used-screen", "purpose": "triage", "cost_usd": .01},
             {"call_id": "failed-screen", "purpose": "triage", "cost_usd": .03, "parse_error": "TimeoutError"}]
    result = call_value.summarize(calls, [{"call_id": "used-screen", "valid": True}])
    group = result["by_purpose"]["triage"]
    assert group["used"] == group["outcome_unknown"] == 1 and group["failed"] == 1
    assert group["cost_per_used_call"] is None  # Incomplete adoption coverage cannot establish the ratio.
    text = "\n".join(call_value.lines(result))
    assert "采用结果未记录 1 次" in text and "未被采用的调用花了" not in text


def test_explicit_zero_and_unbilled_calls_do_not_become_unknown():
    calls = [{"call_id": str(i), "purpose": "triage", **body} for i, body in enumerate([
        {"cost_usd": 0.0}, {"cache_hit": True}, {"mock": True}, {"model": "scripted/fixture"}])]
    assert call_value.summarize(calls, [])["by_purpose"]["triage"]["unpriced"] == 0


def test_status_does_not_prescribe_rate_changes_when_provider_usage_is_missing():
    from scripts.autonomous_fund.status import cost_line
    data = cost_summary([{"at": NOW, "kind": "agent", "body": {"usage": None, "cost_usd": None}}], now=NOW)
    text = cost_line(data)
    assert "用量缺失 1" in text and "都没有单价" not in text


def test_one_selected_event_prints_the_untruncated_reason_without_mutating_the_book(tmp_path):
    from tests.test_autonomous_fund_scripts import account_book, script
    root, env = account_book(tmp_path)
    journal = Journal(root, "synthetic-script-account")
    plan(journal, 0)
    reason = "Account checks. " * 30 + "\nSynthetic new evidence changed the investment thesis."
    journal.event(OPEN + timedelta(minutes=10), "decision_validated", {
        "decision_id": "selected-plan", "targets": [], "rationale": reason})
    with journal.connection() as db:
        sequence = db.execute("SELECT max(sequence) FROM events").fetchone()[0]
    before = (root / "state.sqlite3").read_bytes()
    result = script("fund-logs.sh", tmp_path, env, "decisions", "2026-11-02", "--event", str(sequence))
    assert result.returncode == 0, result.stderr
    assert "完整理由（模型原文）" in result.stdout and "Synthetic new evidence" in result.stdout
    assert "理由：Synthetic assessment" not in result.stdout
    assert (root / "state.sqlite3").read_bytes() == before
    result = script("fund-logs.sh", tmp_path, env, "decisions", "2026-11-02", "--event", str(sequence + 999))
    assert result.returncode != 0 and "No validated decision" in result.stderr
    assert (root / "state.sqlite3").read_bytes() == before


@pytest.mark.parametrize("args", [("--event",), ("--event", "0"), ("--event", "bad"),
                                   ("2026-11-02", "--event", "1", "extra")])
def test_invalid_event_selection_never_falls_back_to_an_unfiltered_report(tmp_path, args):
    from tests.test_autonomous_fund_scripts import account_book, script
    root, env = account_book(tmp_path)
    before = (root / "state.sqlite3").read_bytes()
    result = script("fund-logs.sh", tmp_path, env, "decisions", *args)
    assert result.returncode == 2 and "用法" in result.stderr
    assert (root / "state.sqlite3").read_bytes() == before
