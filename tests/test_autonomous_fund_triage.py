"""Intraday event triage: a fast model screens news and price moves before the manager is called.

Server evidence (2026-11-09): 1,031 news triggers in five days woke the manager at every review floor;
19 manager calls that day changed the targets 0 times. Synthetic triggers and answers only; no network,
broker or model inference.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.risk import RiskError, trading_day
from scripts.autonomous_fund.triage import (TRIAGED, TriageSettings, enforcing_state, evaluate, soft_only,
                                             upper_bound)
from tests.test_autonomous_fund_quant import NOW, account, decision, model_harness

NEWS = {"at": str(NOW), "reason": "news", "material": "news_query", "title": "Analysts discuss chip demand",
        "source": "google_news", "symbols": ["ALFA"], "published_at": str(NOW - timedelta(minutes=3))}


def verdict(escalate, reason="routine coverage of a known story"):
    return {"escalate": escalate, "importance": "high" if escalate else "low", "affected_symbols": ["ALFA"],
            "reason": reason}


def fund(tmp_path, mode, answers, decided_minutes_ago=10, start="enforce"):
    strategy = model_harness(tmp_path)
    strategy.triage, strategy.triage_model = TriageSettings(mode=mode, start=start), "deepseek/deepseek-flash"
    strategy.last_decision = decision([("ALFA", .05)])
    strategy.journal.set_meta("last_model_decision", {"at": (NOW - timedelta(minutes=decided_minutes_ago)).isoformat(),
                                                      "day": trading_day(NOW)})
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    strategy.agents = {"triage": SimpleNamespace(run=run)}  # The real _agent_call: counters and journal.
    strategy.agent_models = {"researcher": "deepseek/deepseek-flash", "manager": "deepseek/deepseek-v4-pro"}
    strategy.scripted = True
    return strategy, calls


def events(strategy, kind):
    return [row["body"] for row in strategy.journal.all_events() if row["kind"] == kind]


def manager_answers(strategy, changed):
    """The manager's call after a screening: same targets, or a changed portfolio."""
    before = strategy.last_decision
    held = before["targets"][0]["symbol"]
    strategy.last_decision = decision([(held if not changed else ("BETA" if held == "ALFA" else "ALFA"), .05)])
    return strategy._score_triage(before)


def test_settings_bound_and_soft_trigger_rules():
    assert TriageSettings().mode == "auto" and TriageSettings().start == "enforce" and TRIAGED == {"news", "price_move"}
    for bad in ({"mode": "always"}, {"window": 5}, {"max_miss_rate": 0.9}, {"audit_every": 1}, {"model": "flash"},
                {"start": "later"}):
        with pytest.raises(RiskError):
            TriageSettings(**bad)
    with pytest.raises(RiskError, match="Unknown triage"):
        TriageSettings.from_config({"bogus": 1})
    assert round(upper_bound(0, 40), 3) == 0.072 and round(upper_bound(2, 40), 3) == 0.149
    assert upper_bound(0, 0) == 1.0 and upper_bound(5, 5) == 1.0
    assert soft_only([NEWS, {"reason": "price_move"}]) and not soft_only([NEWS, {"reason": "market_stress"}])
    assert not soft_only([])


def test_in_the_shadow_the_manager_still_runs_blind_and_the_dismissal_is_scored(tmp_path):
    strategy, calls = fund(tmp_path, "shadow", [verdict(False)])
    strategy._triggers = [NEWS, {**NEWS, "title": "Another preview"}]
    assert strategy._triage_gate(account(), NOW) is False  # The manager runs.
    assert len(calls) == 1 and calls[0]["context"]["triggers"][0]["title"] == NEWS["title"]
    assert calls[0]["context"]["plan"]["targets"][0]["symbol"] == "ALFA"
    assert strategy._triage_note is None  # A shadow verdict is never shown to the manager.
    assert strategy.journal.counter("triage_runs", NOW) == 1 and strategy.journal.counter("agent_runs", NOW) == 0
    [screen] = events(strategy, "event_triage")
    assert screen["action"] == "shadow" and screen["reasons"] == ["news"] and screen["triggers"] == 2
    scored = manager_answers(strategy, changed=False)
    assert scored["miss"] is False and scored["stats"]["dismissals_scored"] == 1
    assert strategy.journal.get_meta("triage_pairs") == [False] and not strategy._triage_enforcing()


def test_hard_triggers_the_first_decision_and_the_two_hour_floor_bypass_screening(tmp_path):
    strategy, calls = fund(tmp_path, "enforce", [verdict(False)])
    strategy._triggers = [NEWS, {"reason": "market_stress"}]
    assert strategy._triage_gate(account(), NOW) is False
    strategy._triggers = [NEWS]
    strategy.journal.set_meta("last_model_decision", {"at": (NOW - timedelta(minutes=121)).isoformat(),
                                                      "day": trading_day(NOW)})
    assert strategy._triage_gate(account(), NOW) is False  # The manager is due at least every 2 hours.
    strategy.journal.set_meta("last_model_decision", None)
    assert strategy._triage_gate(account(), NOW) is False  # No decision yet today: the manager first.
    strategy.triage = TriageSettings(mode="off")
    strategy.journal.set_meta("last_model_decision", {"at": NOW.isoformat(), "day": trading_day(NOW)})
    assert strategy._triage_gate(account(), NOW) is False
    assert calls == [] and events(strategy, "event_triage") == []


def test_an_enforced_dismissal_skips_the_manager_returns_to_its_schedule_and_batches_later_news(tmp_path):
    strategy, calls = fund(tmp_path, "enforce", [verdict(False), verdict(False)])
    scheduled = NOW + timedelta(minutes=50)
    strategy.journal.set_meta("scheduled_review_at", scheduled.isoformat())
    strategy._triggers = [NEWS]
    assert strategy._triage_gate(account(), NOW) is True
    assert strategy._triggers == [] and strategy.next_review == scheduled
    assert events(strategy, "event_triage")[-1]["action"] == "dismissed"
    later = NOW + timedelta(minutes=5)
    strategy._triggers = [{**NEWS, "at": str(later)}]
    assert strategy._triage_gate(account(), later) is True and len(calls) == 1  # Within the floor: batched.
    assert strategy.next_review == NOW + timedelta(minutes=strategy.model_budget.min_review_minutes)
    after = NOW + timedelta(minutes=16)
    assert strategy._triage_gate(account(), after) is True and len(calls) == 2


def test_an_enforced_escalation_tells_the_manager_why_and_is_scored_but_not_counted_as_a_dismissal(tmp_path):
    strategy, _ = fund(tmp_path, "enforce", [verdict(True, "guidance cut on a held stock")])
    strategy._triggers = [NEWS]
    assert strategy._triage_gate(account(), NOW) is False
    assert strategy._triage_note["reason"] == "guidance cut on a held stock" and "meaning" in strategy._triage_note
    scored = manager_answers(strategy, changed=True)
    assert scored["action"] == "escalated" and scored["miss"] is False and "stats" not in scored
    assert strategy._triage_note is None and strategy.journal.get_meta("triage_pairs") in (None, [])


def test_every_fifth_enforced_dismissal_is_audited_by_running_the_manager(tmp_path):
    strategy, _ = fund(tmp_path, "enforce", [verdict(False)] * 5)
    strategy.journal.set_meta("scheduled_review_at", (NOW + timedelta(hours=1)).isoformat())
    actions = []
    for i in range(5):
        at = NOW + timedelta(minutes=16 * i)
        strategy.journal.set_meta("last_model_decision", {"at": at.isoformat(), "day": trading_day(NOW)})
        strategy._triggers = [NEWS]
        skipped = strategy._triage_gate(account(), at)
        actions.append((events(strategy, "event_triage")[-1]["action"], skipped))
    assert actions == [("dismissed", True)] * 4 + [("audit", False)]
    assert strategy._pending_triage["action"] == "audit"


def test_auto_from_the_shadow_enforces_after_a_clean_window_and_rolls_back_when_misses_rise(tmp_path):
    strategy, _ = fund(tmp_path, "auto", [], start="shadow")
    notices = strategy.notices

    def score(miss):
        strategy._pending_triage = {"id": "x", "action": "shadow", "verdict": verdict(False)}
        return manager_answers(strategy, changed=miss)
    for _ in range(39):
        score(False)
    assert not strategy._triage_enforcing()
    score(False)  # The 40th clean dismissal: upper bound 7.2% <= 15%.
    assert strategy.journal.get_meta("triage_enforcing") is True
    assert [k for k, _ in notices] == ["triage_enforced"]
    for _ in range(6):
        score(True)
    assert strategy.journal.get_meta("triage_enforcing") is True  # 6 of 40 = 15%: not above the limit.
    score(True)
    assert strategy.journal.get_meta("triage_enforcing") is False
    assert [k for k, _ in notices] == ["triage_enforced", "triage_rolled_back"]
    enforced, stats = evaluate([False] * 40, TriageSettings(mode="shadow"), False)
    assert not enforced and stats["misses"] == 0  # Shadow mode only scores.


def test_a_failed_screening_leaves_the_decision_to_the_manager(tmp_path):
    strategy, _ = fund(tmp_path, "enforce", [RiskError("Invalid structured agent answer: triage")])
    strategy._triggers = [NEWS]
    assert strategy._triage_gate(account(), NOW) is False
    assert events(strategy, "triage_unavailable")[0]["error"].startswith("Invalid structured agent answer")
    assert strategy._pending_triage is None


def test_the_manager_context_in_a_shadow_round_has_the_triggers_but_never_the_verdict(tmp_path):
    strategy, _ = fund(tmp_path, "shadow", [verdict(False)])
    strategy._triggers = [NEWS]
    assert strategy._triage_gate(account(), NOW) is False
    seen = []
    evidence = [{"source": "Synthetic", "available_at": (NOW - timedelta(days=1)).isoformat(),
                 "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]

    def call(name, **kwargs):
        seen.append((name, kwargs.get("context") or {}))
        return {"summary": "s", "evidence": evidence} if name == "researcher" else decision([("ALFA", .05)])
    strategy._agent_call = call
    before = strategy.last_decision
    strategy._decide(account(), NOW, False)
    scored = strategy._score_triage(before)
    manager = next(context for name, context in seen if name == "manager")
    assert manager["watch_triggers"] and "event_triage" not in manager
    assert scored["manager_changed"] is False and scored["miss"] is False


def test_notices_and_status_line_say_what_happened(tmp_path):
    from scripts.autonomous_fund.notifications import readable
    from scripts.autonomous_fund.status import triage_line
    stats = {"dismissals_scored": 40, "misses": 1, "miss_rate": 0.025, "miss_rate_upper_95": 0.1132,
             "max_miss_rate": 0.15, "audit_every": 5}
    text = readable("triage_enforced", stats, 3)
    assert "盘中事件判断开始生效" in text and "最近 40 次" in text and "漏判率 95% 上限 11.32%" in text
    assert "每 5 次" in text and "至少每 2 小时" in text
    back = readable("triage_rolled_back", {**stats, "misses": 7, "miss_rate": 0.175}, 4)
    assert "退回影子观察" in back and "17.50%" in back
    strategy, _ = fund(tmp_path, "shadow", [verdict(False)])
    strategy._triggers = [NEWS]
    strategy._triage_gate(account(), NOW)
    manager_answers(strategy, changed=False)
    with strategy.journal.connection() as db:
        line = triage_line(db, NOW)
    assert line.startswith("盘中事件判断：影子观察") and "最近 1 次" in line and "今天判断 1 次" in line


def test_by_default_auto_takes_effect_at_once_and_its_audits_roll_it_back_on_misses(tmp_path):
    settings = TriageSettings()
    assert enforcing_state(settings, None) is True  # A new account: effective immediately.
    assert enforcing_state(settings, False) is False and enforcing_state(TriageSettings(start="shadow"), None) is False
    assert enforcing_state(TriageSettings(mode="enforce"), False) is True
    assert enforcing_state(TriageSettings(mode="shadow"), True) is False
    strategy, calls = fund(tmp_path, "auto", [verdict(False)])
    strategy.journal.set_meta("scheduled_review_at", (NOW + timedelta(hours=1)).isoformat())
    strategy._triggers = [NEWS]
    assert strategy._triage_gate(account(), NOW) is True and len(calls) == 1  # Dismissed: no manager call.

    def audit(miss):
        strategy._pending_triage = {"id": "a", "action": "audit", "verdict": verdict(False)}
        return manager_answers(strategy, changed=miss)
    for _ in range(9):
        audit(False)
    audit(True)
    assert strategy._triage_enforcing() is True  # 1 of 10 audits: 10% is within 15%.
    audit(True)
    assert strategy._triage_enforcing() is False  # 2 of 11: back to the shadow.
    assert [k for k, _ in strategy.notices] == ["triage_rolled_back"]
