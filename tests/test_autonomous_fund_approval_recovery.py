"""Approval recovery boundaries with durable journals and synthetic transports/runtimes."""
import json
from datetime import datetime, timedelta

import pytest

from scripts.autonomous_fund import monitor as monitor_module, risk_review
from scripts.autonomous_fund.execution import ApprovalPolicy, ExecutionState, resolve_approval
from scripts.autonomous_fund.monitor import Monitor
from .test_autonomous_fund_ai_approval import native, advance, request_row
from .test_autonomous_fund_execution import NOW, LIMITS, UNIVERSE, envelope, setup
from .test_autonomous_fund_monitor import FakeTelegram, heartbeat


def pending(root, approver="operator", minutes=60):
    journal, state, _, active = setup(root)
    if minutes != 60:
        state = ExecutionState(journal, ApprovalPolicy(valid_minutes=minutes))
        state.accept(active["decision"], NOW, UNIVERSE, LIMITS)
        active = state.active(NOW, UNIVERSE, LIMITS)
    request = state.request(active, envelope(), NOW, approver=approver)
    heartbeat(root, NOW)
    journal.event(NOW, "iteration_finished", {})
    return journal, state, active, request


def delivery(journal, request):
    with journal.connection() as db:
        return dict(db.execute("SELECT d.* FROM fund_deliveries d JOIN events e ON e.sequence=d.sequence "
            "WHERE json_extract(e.body,'$.details.id')=? AND json_extract(e.body,'$.kind') "
            "IN ('approval_required','ai_approval_pending')", (request["id"],)).fetchone())


@pytest.mark.parametrize("approver", ["operator", "ai"])
def test_current_pending_request_is_still_delivered_once(tmp_path, approver):
    journal, _, _, request = pending(tmp_path, approver)
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    monitor.poll(NOW)
    monitor.poll(NOW + timedelta(seconds=1))
    assert len(transport.messages) == 1
    assert delivery(journal, request)["state"] == "sent"


def test_retry_does_not_resend_a_request_approved_after_transport_failure(tmp_path):
    journal, _, _, request = pending(tmp_path)
    transport = FakeTelegram([{"ok": False, "error": "synthetic_outage", "retry_after": 30}])
    monitor = Monitor(tmp_path, transport)
    monitor.poll(NOW)
    assert delivery(journal, request)["state"] == "retry" and len(transport.messages) == 1
    resolve_approval(tmp_path, request["id"], request["digest"], True, NOW + timedelta(seconds=1))
    heartbeat(tmp_path, NOW + timedelta(seconds=31))
    monitor.poll(NOW + timedelta(seconds=31))
    assert len(transport.messages) == 1  # No second transport attempt after approval.
    assert delivery(journal, request)["state"] == "suppressed"


@pytest.mark.parametrize("malformed", ["id", "scope"])
def test_unusable_approval_scope_does_not_block_a_later_notice(tmp_path, malformed):
    from scripts.autonomous_fund.execution import digest
    journal, _, _, request = pending(tmp_path)
    with journal.connection() as db:
        row = db.execute("SELECT sequence,body FROM events WHERE kind='notification'").fetchone()
        notice = json.loads(row["body"])
        if malformed == "id":
            notice["details"]["id"] = ["invalid-synthetic-id"]
        else:
            # A coherent legacy import with the wrong JSON root remains invalid
            # scope evidence, rather than aborting the monitor's whole poll.
            db.execute("UPDATE fund_approvals SET body=?,digest=? WHERE id=?", ("[]", digest([]), request["id"]))
            notice["details"]["digest"] = digest([])
        db.execute("UPDATE events SET body=? WHERE sequence=?", (json.dumps(notice), row["sequence"]))
    journal.event(NOW, "notification", {"kind": "blocked", "details": "Synthetic follow-up notice"})
    transport = FakeTelegram()
    Monitor(tmp_path, transport).poll(NOW)
    assert len(transport.messages) == 1 and "Synthetic follow-up notice" in transport.messages[0]
    with journal.connection() as db:
        first = db.execute("SELECT state,last_error FROM fund_deliveries WHERE sequence=?", (row["sequence"],)).fetchone()
    assert tuple(first) == ("suppressed", "approval_invalid_scope")


@pytest.mark.parametrize("approver", ["operator", "ai"])
@pytest.mark.parametrize("change", ["approved", "rejected", "replaced", "policy", "expired", "scope"])
def test_obsolete_pending_approval_does_not_send_an_actionable_notice(tmp_path, approver, change):
    journal, state, active, request = pending(tmp_path, approver, minutes=1)
    later = NOW + timedelta(seconds=2)
    if change in {"approved", "rejected"}:
        resolve_approval(tmp_path, request["id"], request["digest"], change == "approved", NOW)
    elif change == "replaced":
        state.accept(active["decision"], NOW, UNIVERSE, LIMITS)
    elif change == "policy":
        ExecutionState(journal, risk_digest="new-synthetic-policy")
    elif change == "expired":
        later = NOW + timedelta(seconds=61)
    else:
        with journal.connection() as db:
            body = json.loads(request["body"])
            body["max_buy_turnover_usd"] = "20000"
            db.execute("UPDATE fund_approvals SET body=? WHERE id=?", (json.dumps(body), request["id"]))
    heartbeat(tmp_path, later)
    transport = FakeTelegram()
    Monitor(tmp_path, transport).poll(later)
    assert not transport.messages
    row = delivery(journal, request)
    assert row["state"] == "suppressed" and row["last_error"].startswith("approval_")


def test_replaced_request_in_backlog_leaves_only_the_current_actionable_request(tmp_path):
    journal, state, active, old = pending(tmp_path)
    current = state.request(active, envelope(reasons=("exposure_ramp", "market_stress")), NOW)
    transport = FakeTelegram()
    Monitor(tmp_path, transport).poll(NOW)
    assert len(transport.messages) == 1 and current["id"] in transport.messages[0]
    assert delivery(journal, old)["state"] == "suppressed"
    assert delivery(journal, current)["state"] == "sent"


@pytest.mark.parametrize("change", ["rejected", "expired"])
def test_pending_approval_rechecks_after_message_construction(tmp_path, monkeypatch, change):
    journal, _, _, request = pending(tmp_path, minutes=1)
    wall = [NOW]

    class ClockedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall[0].astimezone(tz) if tz is not None else wall[0].replace(tzinfo=None)

    monkeypatch.setattr(monitor_module, "datetime", ClockedDatetime)
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport)
    outgoing = monitor._outgoing

    def build(*args):
        message = outgoing(*args)
        if change == "expired":
            wall[0] += timedelta(seconds=61)
        else:
            resolve_approval(tmp_path, request["id"], request["digest"], False, wall[0])
        return message

    monitor._outgoing = build
    monitor.poll()
    assert not transport.messages
    assert delivery(journal, request)["state"] == "suppressed"


def test_resolved_ai_notice_is_retained_when_its_pending_notice_is_obsolete(tmp_path):
    journal, _, _, request = pending(tmp_path, "ai")
    resolve_approval(tmp_path, request["id"], request["digest"], True, NOW)
    journal.event(NOW, "notification", {"kind": "ai_approval_resolved", "details": {
        "id": request["id"], "state": "approved", "model": "deepseek/synthetic-reviewer", "reason": "Synthetic assessment"}})
    transport = FakeTelegram()
    Monitor(tmp_path, transport).poll(NOW)
    assert len(transport.messages) == 1 and "批准不是成交" in transport.messages[0]
    assert delivery(journal, request)["state"] == "suppressed"


@pytest.mark.parametrize("finish", ["running", "failed", "abandoned"])
def test_last_review_attempt_keeps_its_lease_before_manager_reassessment(native, finish):
    def timeout(*args):
        raise TimeoutError("Synthetic provider failure")
    strategy, clock, source, journal, calls = native(hook=timeout)
    strategy.on_trading_iteration()
    request = request_row(journal)
    advance(clock, source, 301)
    attempt = risk_review.claim(journal, request, strategy.execution.digest, strategy.automation, clock, 120)
    assert attempt == 2
    risk_review.fail(journal, request, attempt, clock(), strategy.automation, "SyntheticFailure")
    advance(clock, source, 301)
    attempt = risk_review.claim(journal, request, strategy.execution.digest, strategy.automation, clock, 120)
    assert attempt == 3
    strategy._review_purchase_request(request, strategy._snapshot(), clock())
    assert journal.get_meta("risk_review_exhausted") != request["id"]
    if finish == "running":
        assert sum(name == "risk_reviewer" for name, _ in calls) == 1
        return
    if finish == "failed":
        risk_review.fail(journal, request, attempt, clock(), strategy.automation, "SyntheticFailure")
    else:
        advance(clock, source, 121)
    for _ in range(2):
        strategy._review_purchase_request(request, strategy._snapshot(), clock())
    assert journal.get_meta("risk_review_exhausted") == request["id"]
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='review_triggered' "
                          "AND json_extract(body,'$.reason')='risk_review_failed'").fetchone()[0] == 1
