"""Separate-process watchdog and durable, best-effort Telegram delivery."""
import hashlib
import json
import os
import time
import threading
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import requests

from .execution import EXECUTION_REVISION, approval_code, operator_connection, resolve_approval
from .journal import encode
from .risk import RiskError, instant


DECISION_GRACE_MINUTES = 90  # After the open, the first valid decision is expected within this.
DECISION_DROUGHT_HOURS = 3  # Later, a decision is expected within this after the manager's own next_review_at.
LEGITIMATE_PAUSES = ("model_budget_exhausted", "model_cost_budget_exhausted")
EXECUTION_MONITOR_MAX_AGE_SECONDS = 45
POLL_SECONDS = 10
NOTIFICATION_BUDGET_SECONDS = 20  # One in-flight ten-second request may finish after this budget.


def require_monitor(root, now):
    """Server execution gate, independent of AI and the supervising PID."""
    try:
        body = json.loads((Path(root) / "monitor.json").read_text(encoding="utf-8"))
        age = (instant(now) - instant(body["at"])).total_seconds()
        if (not 0 <= age <= EXECUTION_MONITOR_MAX_AGE_SECONDS or body["health"] not in {"healthy", "stopped"} or
                not body.get("telegram_configured")):
            raise ValueError("Unhealthy independent monitor")
    except Exception:
        raise RiskError("Independent monitor unavailable or unhealthy; server execution blocked") from None


APPROVE_WORDS = {"同意", "批准", "APPROVE"}
REJECT_WORDS = {"拒绝", "否决", "REJECT"}
STATUS_WORDS = {"状态", "STATUS"}


def approvers(chat_id, configured=None):
    """Who may approve: explicit user IDs, else the owner of a private chat."""
    raw = configured if configured is not None else os.environ.get("FUND_TELEGRAM_APPROVER_IDS", "")
    ids = {item.strip() for item in str(raw).split(",") if item.strip()}
    if not ids and not str(chat_id).startswith("-"):
        ids = {str(chat_id)}  # A private chat's ID is its user's ID; groups need explicit approvers.
    return ids


class Telegram:
    def __init__(self, token=None, chat_id=None, post=None, approver_ids=None, replies_enabled=None):
        self.token = token or os.environ.get("FUND_TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("FUND_TELEGRAM_CHAT_ID")
        if not self.token or not self.chat_id:
            raise RiskError("Telegram requires FUND_TELEGRAM_BOT_TOKEN and FUND_TELEGRAM_CHAT_ID in the local environment")
        self.post = post or requests.post
        self.approver_ids = approvers(self.chat_id, approver_ids)
        if replies_enabled is None:
            replies_enabled = os.environ.get("FUND_TELEGRAM_APPROVALS", "on").strip().lower() not in {"off", "false", "0", "no"}
        self.replies_enabled = replies_enabled

    def send(self, text, reply_to=None):
        payload = {"chat_id": self.chat_id, "text": text[:4000], "link_preview_options": {"is_disabled": True}}
        if reply_to:
            payload["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        return self._request("sendMessage", json=payload)

    def updates(self, offset):
        """Fetch new messages sent to the bot (short poll; never blocks the watchdog)."""
        try:
            response = self.post(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                 json={"offset": int(offset), "timeout": 0, "allowed_updates": ["message"]},
                                 timeout=10, allow_redirects=False)
            body = response.json()
        except Exception:
            return None  # Exceptions may contain the token-bearing URL.
        if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            return None
        return [row for row in body.get("result") or [] if isinstance(row, dict)]

    def send_document(self, caption, filename, document):
        return self._request("sendDocument", data={"chat_id": self.chat_id, "caption": caption[:1000]},
                             files={"document": (filename, document.encode("utf-8"), "text/plain; charset=utf-8")})

    def _request(self, method, **payload):
        try:
            response = self.post(f"https://api.telegram.org/bot{self.token}/{method}",
                **payload, timeout=10, allow_redirects=False)
            body = response.json()
        except Exception:
            # Exceptions often include the token-bearing URL. Never propagate it.
            return {"ok": False, "error": "transport_or_response_error", "retry_after": 30}
        if not isinstance(body, dict):
            return {"ok": False, "error": "invalid_acknowledgement", "retry_after": 30}
        if response.status_code == 200 and body.get("ok") is True and isinstance(body.get("result"), dict) and body["result"].get("message_id"):
            return {"ok": True, "message_id": body["result"]["message_id"]}
        try:
            delay = min(3600, max(5, int((body.get("parameters") or {}).get("retry_after", 30))))
        except (TypeError, ValueError, AttributeError):
            delay = 30
        return {"ok": False, "error": f"telegram_http_{response.status_code}", "retry_after": delay}


class Monitor:
    def __init__(self, root, transport=None, *, heartbeat_seconds=180, agent_seconds=900, resume_policy=None):
        self.root, self.transport = Path(root), transport
        self.clock = time.monotonic
        self._observer = uuid4().hex
        self._commands_first = False
        self.resume_policy = resume_policy  # None: never resume automatically.
        if heartbeat_seconds < 30 or agent_seconds < 60:
            raise RiskError("Watchdog thresholds are too short")
        self.heartbeat_seconds, self.agent_seconds = heartbeat_seconds, agent_seconds
        with operator_connection(self.root) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fund_deliveries (
                    sequence INTEGER PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL,
                    next_at TEXT NOT NULL, last_error TEXT, message_id TEXT);
                CREATE TABLE IF NOT EXISTS fund_monitor_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            if transport is not None:
                db.execute("BEGIN IMMEDIATE")
                recipient = hashlib.sha256(str(transport.chat_id).encode()).hexdigest()
                old = db.execute("SELECT value FROM fund_monitor_meta WHERE key='recipient'").fetchone()
                if old and old[0] != recipient:
                    uncertain = db.execute("SELECT 1 FROM fund_deliveries WHERE state IN ('sent','sending') OR "
                        "(state='retry' AND last_error NOT IN ('telegram_http_400','telegram_http_401','telegram_http_403')) LIMIT 1").fetchone()
                    if uncertain:
                        raise RiskError("Monitor recipient changed; review the selected account and delivery ledger")
                # Allow correction of an initial typo only before any accepted
                # or uncertain delivery. Never reroute an established ledger.
                db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('recipient',?)", (recipient,))

    def _health(self, db, now):
        saved = db.execute("SELECT at,body,sequence FROM events WHERE kind='startup' ORDER BY sequence DESC LIMIT 1").fetchone()
        if not saved or json.loads(saved[1]).get("mode") not in {"local", "paper"}:
            raise RiskError("Monitor requires an existing local or Alpaca paper run")
        startup = json.loads(saved[1])
        state = "healthy"
        if startup.get("execution_revision") != EXECUTION_REVISION:
            state = "runner_upgrade_required"
        else:
            try:
                filename = "status.json" if startup["mode"] == "local" else "runner_health.json"
                status = json.loads((self.root / filename).read_text(encoding="utf-8"))
                if status.get("runner_state") != "running":
                    state = "runner_exited"
                elif (now - instant(status["heartbeat_at"])).total_seconds() > self.heartbeat_seconds:
                    state = "heartbeat_stale"
                elif status.get("matching_error"):
                    state = "matching_error"
            except Exception:
                state = "heartbeat_unavailable"
        # A killed provider call remains an audit fact, not a permanently hung
        # call in every subsequent process. Scope liveness to THIS startup.
        latest = db.execute("SELECT at,kind,body FROM events WHERE sequence>? AND kind IN "
            "('agent_call_started','agent_call_finished') ORDER BY sequence DESC LIMIT 1", (saved["sequence"],)).fetchone()
        if latest and latest[1] == "agent_call_started" and (now - instant(latest[0])).total_seconds() > self.agent_seconds:
            state = "agent_call_stalled"
        if state == "healthy" and not (latest and latest[1] == "agent_call_started"):
            market = self._session_status(now)
            iteration = db.execute("SELECT at,kind FROM events WHERE sequence>? AND kind IN "
                "('iteration_started','iteration_finished') ORDER BY sequence DESC LIMIT 1", (saved["sequence"],)).fetchone()
            observed_at = instant(iteration[0] if iteration else saved[0])
            if latest:
                # A model call that just finished is progress too: a long but moving
                # iteration (research, then manager) is not a stalled one.
                observed_at = max(observed_at, instant(latest[0]))
            timeout = self.heartbeat_seconds
            if iteration is None or iteration["kind"] == "iteration_finished":
                # A completed job may be waiting for its next scheduled slot.
                # Started jobs retain the original progress limit.
                execution = startup.get("execution")
                interval = execution.get("iteration_seconds") if isinstance(execution, dict) else None
                if type(interval) is int and 15 <= interval <= 300:
                    timeout = max(timeout, interval + POLL_SECONDS)
            if (market["open"] and (now - instant(market["next_open"])).total_seconds() > 300 and
                    (now - observed_at).total_seconds() > timeout):
                state = "iteration_stalled"
        if shutil.disk_usage(self.root).free < 200_000_000:
            state = "low_disk_space"
        underlying = state
        if state not in {"healthy", "stopped"}:
            # Reassert every poll even after someone resumes an unhealthy run;
            # deduplication applies to alerts, never to the protective STOP.
            from . import stops
            stops.write(self.root, "watchdog", state, now)
        if (self.root / "STOP").exists() and state == "healthy":
            state = "stopped"
        old = db.execute("SELECT value FROM fund_monitor_meta WHERE key='health'").fetchone()
        quiet = db.execute("SELECT value FROM fund_monitor_meta WHERE key='health_quiet'").fetchone()
        if not old or old[0] != state:
            from . import stops
            record = stops.read(self.root) or {}
            planned = state == "runner_exited" and record.get("class") == "operator"
            if state not in {"healthy", "stopped"} and planned:
                # A stop or restart by the operator ends the runner on purpose: recorded, not alerted
                # (the operator STOP already holds trading; a crash under no operator STOP still alerts).
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "watchdog_noted", encode({
                    "reason": state, "stop": record.get("reason")})))
                db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('health_quiet','1')")
            elif state not in {"healthy", "stopped"}:
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "notification", encode({
                    "kind": "watchdog_stop", "details": {"reason": state, "action": "STOP written; inspect broker orders and resume explicitly"}})))
                db.execute("DELETE FROM fund_monitor_meta WHERE key='health_quiet'")
            elif old and old[0] not in {"healthy", "stopped"} and quiet:
                db.execute("DELETE FROM fund_monitor_meta WHERE key='health_quiet'")  # The planned stop is over.
            elif old and old[0] not in {"healthy", "stopped"}:
                from . import stops
                policy = self.resume_policy
                automatic = (policy and policy.auto_resume and stops.resumable(stops.read(self.root)) and
                             self._resumes_last_day(db, now) < policy.max_auto_resumes_per_day)
                text = (f"Health recovered; trading resumes by itself after {policy.auto_resume_after_seconds} s of "
                        "continuous health if no order is unresolved" if automatic else
                        "Health recovered; STOP still requires explicit operator resume")
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "notification", encode({
                    "kind": "watchdog_recovered", "details": text})))
            db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('health',?)", (state,))
        if self._auto_resume(db, now, underlying, saved["sequence"]):
            state = "healthy"
            db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('health',?)", (state,))
        return state

    @staticmethod
    def _resumes_last_day(db, now):
        day_ago = now - timedelta(hours=24)
        return sum(instant(row[0]) >= day_ago for row in db.execute("SELECT at FROM events WHERE kind='auto_resume'"))

    def _session_status(self, now):
        """One NYSE calendar for the monitor's lifetime (the schedule is cached per day)."""
        from .market_data import SessionClock
        if getattr(self, "_session_clock", None) is None:
            self._session_clock = SessionClock(now=lambda: self._session_now)
        self._session_now = now
        return self._session_clock.status()

    def _auto_resume(self, db, now, underlying, startup_sequence):
        """Lift a STOP that a service restart or a liveness fault wrote, never an operator's.

        Requires continuous health for auto_resume_after_seconds after the stop,
        no unresolved order reservation, and at most max_auto_resumes_per_day.
        """
        from . import stops
        previous = db.execute("SELECT value FROM fund_monitor_meta WHERE key='health_observation'").fetchone()
        continuous = False
        if previous:
            try:
                observed = json.loads(previous[0])
                continuous = (observed["observer"] == self._observer and
                              observed["startup_sequence"] == startup_sequence and
                              0 <= (now - instant(observed["at"])).total_seconds() <= EXECUTION_MONITOR_MAX_AGE_SECONDS)
            except (KeyError, TypeError, ValueError):
                pass
        # Persist this with the health-period update: a rolled-back poll cannot
        # accidentally reuse the older period after an observation gap.
        db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('health_observation',?)", (encode({
            "observer": self._observer, "startup_sequence": startup_sequence, "at": now.isoformat()}),))
        if not continuous:
            db.execute("DELETE FROM fund_monitor_meta WHERE key='healthy_since'")
        if underlying != "healthy":
            db.execute("DELETE FROM fund_monitor_meta WHERE key='healthy_since'")
            return False
        db.execute("INSERT OR IGNORE INTO fund_monitor_meta VALUES('healthy_since',?)", (now.isoformat(),))
        policy = self.resume_policy
        if policy is None or not policy.auto_resume:
            return False
        record = stops.read(self.root)
        if not stops.resumable(record):
            return False
        since = instant(db.execute("SELECT value FROM fund_monitor_meta WHERE key='healthy_since'").fetchone()[0])
        begun = max(since, instant(record["at"])) if record.get("at") else since
        if (now - begun).total_seconds() < policy.auto_resume_after_seconds:
            return False
        marker = f"{record['at']}|{record['reason']}"

        def note_once(kind, details):
            seen = db.execute("SELECT value FROM fund_monitor_meta WHERE key='auto_resume_note'").fetchone()
            if not seen or seen[0] != f"{kind}|{marker}":
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "notification", encode({
                    "kind": kind, "details": details})))
                db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('auto_resume_note',?)", (f"{kind}|{marker}",))
        if db.execute("SELECT 1 FROM intents WHERE status!='terminal' LIMIT 1").fetchone():
            note_once("auto_resume_blocked", {"stop_reason": record["reason"], "action": (
                "An order reservation is unresolved: verify it at the broker, resolve it, then resume explicitly")})
            return False
        recent = self._resumes_last_day(db, now)
        if recent >= policy.max_auto_resumes_per_day:
            note_once("auto_resume_exhausted", {"stop_reason": record["reason"], "resumes_last_24h": recent, "action": (
                "Repeated restarts: inspect runtime.log and journalctl, then resume explicitly")})
            return False
        if not stops.clear(self.root, record):
            return False  # Someone changed STOP meanwhile; their record wins.
        detail = {"stop": record, "healthy_since": since.isoformat()}
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "auto_resume", encode(detail)))
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "notification", encode({
            "kind": "auto_resumed", "details": {"stop_reason": record["reason"], "stopped_at": record["at"],
                                                "healthy_since": since.isoformat()}})))
        return True

    def poll(self, now=None):
        now = instant(now or datetime.now(timezone.utc))
        deadline = self.clock() + NOTIFICATION_BUDGET_SECONDS
        with operator_connection(self.root) as db:
            state = self._health(db, now)
            # Recover even if the trader exited between persisting the review
            # and exporting it. One durable notification per completed review.
            # One indexed pass per poll; never a full-table scan per review.
            notified = {row[0] for row in db.execute(
                "SELECT json_extract(body,'$.details.review_sequence') FROM events WHERE kind='notification' "
                "AND json_extract(body,'$.kind')='postmarket_report'")}
            reviews = db.execute("SELECT sequence,at FROM events WHERE kind='postmarket_review'").fetchall()
            for review in reviews:
                if review["sequence"] in notified:
                    continue
                packet = json.loads(db.execute("SELECT body FROM events WHERE sequence=?", (review["sequence"],)).fetchone()[0])
                if instant(packet["completed_at"]) > now:
                    continue
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (review["at"], "notification", encode({
                    "kind": "postmarket_report", "details": {"review_sequence": review["sequence"],
                    "session_date": packet["session_date"], "summary": packet["review"]["summary"]}})))
            self._queue_weekly(db, reviews, now)
            self._queue_fill_updates(db, now)
            # Drop stale first-start alerts; retain recent requests and failures.
            # A durable cursor bounds each poll to new notifications. It never
            # advances past one stamped in the future, which is revisited later.
            cursor = db.execute("SELECT value FROM fund_monitor_meta WHERE key='notification_cursor'").fetchone()
            rows = db.execute("SELECT sequence,at,body FROM events WHERE kind='notification' AND sequence>? ORDER BY sequence",
                              (int(cursor[0]) if cursor else 0,)).fetchall()
            for row in rows:
                if instant(row["at"]) > now:
                    break
                db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('notification_cursor',?)", (str(row["sequence"]),))
                exists = db.execute("SELECT 1 FROM fund_deliveries WHERE sequence=?", (row["sequence"],)).fetchone()
                if exists:
                    continue
                body = json.loads(row["body"])
                duplicate = False
                prior_rows = [] if body.get("kind") in {"watchdog_stop", "watchdog_recovered", "approval_required"} else db.execute(
                    "SELECT e.at,e.body FROM events e JOIN fund_deliveries d ON e.sequence=d.sequence WHERE d.state!='suppressed' AND e.sequence<? ORDER BY e.sequence DESC LIMIT 30", (row["sequence"],))
                for previous in prior_rows:
                    if previous["body"] == row["body"] and abs((instant(row["at"]) - instant(previous["at"])).total_seconds()) < 1800:
                        duplicate = True
                        break
                stale = (body.get("kind") not in {"postmarket_report", "weekly_report"} and
                         now - instant(row["at"]) > timedelta(hours=1))
                db.execute("INSERT INTO fund_deliveries VALUES(?,?,?,?,NULL,NULL)",
                           (row["sequence"], "suppressed" if duplicate or stale else "pending", 0, now.isoformat()))
            self._note_desk(db, now)
            self._note_decision_drought(db, now)
        if self.transport is not None:
            replies = getattr(self.transport, "replies_enabled", False)
            # Both queues share the deadline. Alternate first access so slow
            # notifications cannot starve approvals, or vice versa.
            if replies and self._commands_first:
                self._commands(now, deadline=deadline)
            self._deliver(now, deadline=deadline)
            if replies and not self._commands_first:
                self._commands(now, deadline=deadline)
            self._commands_first = not self._commands_first
        with operator_connection(self.root) as db:
            counts = dict(db.execute("SELECT state,count(*) FROM fund_deliveries GROUP BY state"))
        status = {"at": now.isoformat(), "health": state, "telegram_configured": self.transport is not None,
                  "delivery_counts": counts, "stop_active": (self.root / "STOP").exists(),
                  "disk_free_bytes": shutil.disk_usage(self.root).free}
        temporary = self.root / "monitor.tmp"
        temporary.write_text(encode(status), encoding="utf-8")
        temporary.replace(self.root / "monitor.json")
        try:
            write_metrics(self.root, status)
        except Exception:  # Metrics are a convenience; they never affect supervision.
            pass
        return status

    def _week_ends(self, day):
        """True when `day` is the last NYSE session of its ISO week (cached; holidays and early weeks included)."""
        cache = self.__dict__.setdefault("_week_end_cache", {})
        if day not in cache:
            import pandas_market_calendars as mcal
            from datetime import date
            session = date.fromisoformat(day)
            schedule = mcal.get_calendar("NYSE").schedule(start_date=session + timedelta(days=1),
                                                          end_date=session + timedelta(days=10))
            following = schedule.index[0].date() if len(schedule) else None
            cache[day] = following is None or following.isocalendar()[:2] != session.isocalendar()[:2]
        return cache[day]

    def _queue_fill_updates(self, db, now):
        """Recover confirmed-fill notices once a coherent post-fill account exists."""
        startup = db.execute("SELECT body FROM events WHERE kind='startup' ORDER BY sequence DESC LIMIT 1").fetchone()
        if not startup or json.loads(startup[0]).get("mode") not in {"local", "paper"}:
            return
        saved = db.execute("SELECT value FROM fund_monitor_meta WHERE key='fill_notification_cursor'").fetchone()
        cursor = int(saved[0]) if saved else 0
        rows = db.execute("SELECT sequence,at,kind,body FROM events WHERE sequence>? "
                          "AND kind IN ('fill','fill_reconciled') ORDER BY sequence LIMIT 200", (cursor,)).fetchall()
        visible = []
        for row in rows:
            if instant(row["at"]) > now:
                break
            if now - instant(row["at"]) > timedelta(hours=1):
                cursor = row["sequence"]
            else:
                visible.append(row)
        if visible:
            account, account_sequence = None, 0
            for row in db.execute("SELECT sequence,at,body FROM events WHERE kind='account' ORDER BY sequence DESC LIMIT 200"):
                body = json.loads(row["body"])
                observed = instant(body.get("as_of") or row["at"])
                if instant(row["at"]) <= now and observed <= now:
                    body["as_of"] = str(observed)
                    if account is None or observed > instant(account["as_of"]):
                        account = body
                        account_sequence = row["sequence"]
            cutoff = instant(account["as_of"]) if account is not None else None
            completed = []
            for row in visible:
                if cutoff is None or instant(row["at"]) > cutoff or row["sequence"] >= account_sequence:
                    break
                completed.append(row)
            waiting = False
            if not completed:
                # Confirmed trades remain useful while a data outage prevents
                # marking the remaining holdings. Never attach pre-fill lots.
                prior = db.execute("SELECT value FROM fund_monitor_meta WHERE key='fill_snapshot_pending'").fetchone()
                if now - instant(visible[0]["at"]) < timedelta(seconds=60) or (prior and int(prior[0]) >= visible[-1]["sequence"]):
                    return
                waiting, account, cutoff, completed = True, None, now, visible
            from .holdings import recent_trades
            trades = recent_trades([(at, kind, json.loads(body)) for at, kind, body in db.execute(
                "SELECT at,kind,body FROM events WHERE kind IN ('fill','fill_reconciled') ORDER BY sequence")], cutoff)
            details = {"at": str(now), "account": account, "recent_trades": trades,
                       "fill_sequences": [row["sequence"] for row in completed], "account_pending": waiting}
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "notification", encode({
                "kind": "execution_update", "details": details})))
            if waiting:
                db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('fill_snapshot_pending',?)",
                           (str(completed[-1]["sequence"]),))
            else:
                cursor = completed[-1]["sequence"]
        if cursor:
            db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('fill_notification_cursor',?)", (str(cursor),))

    def _queue_weekly(self, db, reviews, now):
        """One weekly report per ISO week, after the review of the week's last session (recent reviews only)."""
        from .notifications import week_of
        sent = {row[0] for row in db.execute(
            "SELECT json_extract(body,'$.details.week') FROM events WHERE kind='notification' "
            "AND json_extract(body,'$.kind')='weekly_report'")}
        for review in reviews[-7:]:
            packet = json.loads(db.execute("SELECT body FROM events WHERE sequence=?", (review["sequence"],)).fetchone()[0])
            completed = instant(packet["completed_at"])
            if completed > now or now - completed > timedelta(days=7):
                continue
            week = week_of(packet["session_date"])[2]
            if week in sent:
                continue
            try:
                last = self._week_ends(packet["session_date"])
            except Exception:
                continue  # Without the calendar the week's end is unknown: no guessed report.
            if last:
                sent.add(week)
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (review["at"], "notification", encode({
                    "kind": "weekly_report", "details": {"review_sequence": review["sequence"], "week": week,
                                                         "session_date": packet["session_date"]}})))

    def _note_decision_drought(self, db, now):
        """Alert once a day when the session runs without a valid decision and nothing explains it.

        A healthy process can still fail to decide (a model failing on every
        retry, an order stuck open, a logic fault); the heartbeat cannot show it.
        Expected: a validated decision within DECISION_GRACE_MINUTES of the open,
        and later within DECISION_DROUGHT_HOURS of the latest decision's own
        next_review_at (covering quiet-review skips). Not a drought: a STOP, an
        exhausted run or cost budget, or an observe-only run.
        """
        from zoneinfo import ZoneInfo
        try:
            session = self._session_status(now)
        except Exception:
            return
        if not session.get("open"):
            return
        if (self.root / "STOP").exists():
            # Trading is held on purpose; the clock restarts when the STOP lifts.
            db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('decision_drought_paused_at',?)", (now.isoformat(),))
            return
        zone = ZoneInfo("America/New_York")
        day = now.astimezone(zone).date().isoformat()
        noted = db.execute("SELECT value FROM fund_monitor_meta WHERE key='decision_drought_noted'").fetchone()
        if noted and noted[0] == day:
            return
        opened = instant(session["next_open"])  # While open, next_open is the current session's open.
        paused = db.execute("SELECT value FROM fund_monitor_meta WHERE key='decision_drought_paused_at'").fetchone()
        clock = max(opened, instant(paused[0])) if paused else opened
        last = db.execute("SELECT at,body FROM events WHERE kind='decision_validated' "
                          "ORDER BY sequence DESC LIMIT 1").fetchone()
        last_at = instant(last["at"]) if last else None
        if last_at is not None and last_at >= opened:
            try:
                due = max(last_at, instant(json.loads(last["body"]).get("next_review_at") or last["at"]))
            except (TypeError, ValueError, RiskError):
                due = last_at
            expected = max(due + timedelta(hours=DECISION_DROUGHT_HOURS), clock + timedelta(minutes=DECISION_GRACE_MINUTES))
        else:
            expected = clock + timedelta(minutes=DECISION_GRACE_MINUTES)
        if now < expected:
            return
        startup = db.execute("SELECT body FROM events WHERE kind='startup' ORDER BY sequence DESC LIMIT 1").fetchone()
        if startup and json.loads(startup[0]).get("observe_only"):
            return
        since = opened.isoformat()
        for row in db.execute("SELECT at,body FROM events WHERE kind='notification' ORDER BY sequence DESC LIMIT 200"):
            if instant(row["at"]) < opened:
                break
            if json.loads(row["body"]).get("kind") in LEGITIMATE_PAUSES:
                return  # The day's model budget is spent: deciding nothing is the configured behavior.
        failures = sum(instant(r[0]) >= opened for r in db.execute(
            "SELECT at FROM events WHERE kind='decision_backoff' ORDER BY sequence DESC LIMIT 100"))
        db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('decision_drought_noted',?)", (day,))
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (now.isoformat(), "notification", encode({
            "kind": "decision_drought", "details": {
                "session_open": since, "last_decision_at": last_at.isoformat() if last_at else None,
                "minutes_without_decision": round((now - (last_at if last_at and last_at >= opened else opened))
                                                  .total_seconds() / 60),
                "decision_failures_today": failures}})))

    def _note_desk(self, db, now):
        """Tell the operator once per six hours when the data desk stopped updating (trading is unaffected)."""
        from .datadesk import DeskStatus
        state = DeskStatus(self.root).read()
        if state is None or state.get("disabled"):
            return  # Never ran (an older installation) or switched off: nothing to report.
        try:
            age = (now - instant(state["heartbeat_at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            return
        if age < 600:
            return
        noted = db.execute("SELECT value FROM fund_monitor_meta WHERE key='desk_stale_noted_at'").fetchone()
        if noted and (now - instant(noted[0])).total_seconds() < 6 * 3600:
            return
        db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('desk_stale_noted_at',?)", (now.isoformat(),))
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (now.isoformat(), "notification", encode({
            "kind": "data_desk_stale", "details": {"heartbeat_minutes_ago": round(age / 60)}})))

    def _deliver(self, now, only=None, *, deadline=None):
        if deadline is not None and self.clock() >= deadline:
            return
        with operator_connection(self.root) as db:
            recipient = db.execute("SELECT value FROM fund_monitor_meta WHERE key='recipient'").fetchone()
            if not recipient or recipient[0] != hashlib.sha256(str(self.transport.chat_id).encode()).hexdigest():
                raise RiskError("Monitor recipient changed; stop and restart the selected monitor")
            gate = db.execute("SELECT value FROM fund_monitor_meta WHERE key='next_delivery_at'").fetchone()
            if gate and instant(gate[0]) > now:
                return
            rows = db.execute("SELECT d.*,e.body FROM fund_deliveries d JOIN events e ON e.sequence=d.sequence WHERE d.state IN ('pending','retry','sending') ORDER BY d.sequence").fetchall()
        # Bound delivery work so the independent health gate stays fresh while
        # sending slow uploads; remaining durable messages wait for the next poll.
        for row in [r for r in rows if instant(r["next_at"]) <= now and (only is None or r["sequence"] == only)][:2]:
            if deadline is not None and self.clock() >= deadline:
                break
            body = json.loads(row["body"])
            text = f"LumiBot 模拟账户 · 事件 #{row['sequence']}\n{body.get('kind')}\n{encode(body.get('details'))}"
            with operator_connection(self.root) as db:
                db.execute("BEGIN IMMEDIATE")
                selected = db.execute("SELECT value FROM fund_monitor_meta WHERE key='recipient'").fetchone()
                if not selected or selected[0] != hashlib.sha256(str(self.transport.chat_id).encode()).hexdigest():
                    raise RiskError("Monitor recipient changed; stop and restart the selected monitor")
                claimed = db.execute("UPDATE fund_deliveries SET state='sending',attempts=attempts+1,next_at=? "
                    "WHERE sequence=? AND state IN ('pending','retry','sending') AND next_at=?",
                    ((now + timedelta(seconds=60)).isoformat(), row["sequence"], row["next_at"]))
                if claimed.rowcount != 1:
                    continue  # Another monitor/test sender already claimed it.
            try:
                from .notifications import approval_message, daily_document
                if body.get("kind") == "postmarket_report":
                    filename, document = daily_document(self.root, body["details"]["review_sequence"])
                    result = self.transport.send_document(
                        f"LumiBot 模拟账户 · {body['details']['session_date']} 盘后日报\n" + body["details"]["summary"],
                        filename, document)
                elif body.get("kind") == "weekly_report":
                    from .notifications import weekly_document
                    filename, document = weekly_document(self.root, body["details"]["review_sequence"])
                    result = self.transport.send_document(
                        f"LumiBot 模拟账户 · {body['details']['week']} 周报（{body['details']['session_date']} 收盘后）",
                        filename, document)
                elif body.get("kind") == "approval_required" and (
                        "side" in body.get("details", {}) or "buys" in body.get("details", {})):
                    message = approval_message(body["details"])
                    result = (self.transport.send(message) if len(message) <= 4000 else
                              self.transport.send_document(message[:900] + "\n完整理由见附件。",
                                  f"approval-{body['details']['id']}.txt", message))
                else:
                    from .notifications import LONG_MESSAGE, readable
                    message = readable(body.get("kind"), body.get("details"), row["sequence"], now=now) or text
                    if len(message) <= LONG_MESSAGE:
                        result = self.transport.send(message)
                    else:  # A long research text arrives whole, as a document.
                        # Keep holdings visible; the attachment retains the complete rationale.
                        caption = (message.split("\n理由：", 1)[0] if body.get("kind") == "decision_changed" else
                                   message.split("\n", 1)[0])
                        result = self.transport.send_document(caption[:900] + "\n内容较长，完整内容见附件。",
                                                              f"{body.get('kind')}-{row['sequence']}.txt", message)
            except Exception:
                # Never persist third-party exceptions (may contain credentials).
                result = {"ok": False, "error": "report_or_delivery_error", "retry_after": 30}
            delay = max(result.get("retry_after", 0), min(1800, 5 * 2 ** min(row["attempts"], 8)))
            with operator_connection(self.root) as db:
                db.execute("UPDATE fund_deliveries SET state=?,next_at=?,last_error=?,message_id=? WHERE sequence=?", (
                    "sent" if result["ok"] else "retry", (now + timedelta(seconds=delay)).isoformat(),
                    None if result["ok"] else result["error"], str(result.get("message_id", "")), row["sequence"]))
                if not result["ok"]:
                    db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('next_delivery_at',?)",
                               ((now + timedelta(seconds=delay)).isoformat(),))
            if not result["ok"]:
                break  # Respect rate limits and outages across the whole batch.


    def _commands(self, now, *, deadline=None):
        """Telegram replies as an approval channel: authenticated, request-bound, auditable.

        Accepted only from the configured chat and an authorized approver, for a
        request created before the message, identified by replying to its alert,
        by its short code, or (when exactly one request is pending) by plain text.
        Approval is the same digest-bound operator action as the CLI: it never
        relaxes a hard limit, cannot approve while STOP is active and expires.
        """
        if deadline is not None and self.clock() >= deadline:
            return
        with operator_connection(self.root) as db:
            row = db.execute("SELECT value FROM fund_monitor_meta WHERE key='telegram_offset'").fetchone()
        offset = int(row[0]) if row else 0
        updates = self.transport.updates(offset)
        if not updates:
            return
        try:
            for update in updates:
                if deadline is not None and self.clock() >= deadline:
                    break  # Leave the offset before this message; Telegram returns it next poll.
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    self._command(update, now)
                except Exception as exc:  # One odd update must never stop the watchdog or be re-read forever.
                    with operator_connection(self.root) as db:
                        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "telegram_command", encode({
                            "update_id": update.get("update_id"), "result": "error", "error": type(exc).__name__})))
        finally:
            with operator_connection(self.root) as db:
                db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('telegram_offset',?)", (str(offset),))

    def _command(self, update, now):
        """One Telegram update; the caller isolates failures and persists the offset."""
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        words = text.replace("，", " ").replace(",", " ").split()
        if not words:
            return
        verb = words[0].upper()
        if verb not in APPROVE_WORDS | REJECT_WORDS | STATUS_WORDS:
            return
        chat = str((message.get("chat") or {}).get("id"))
        sender = str((message.get("from") or {}).get("id"))
        reply_to = message.get("message_id")
        if chat != str(self.transport.chat_id):
            return  # Never answer or act on another conversation.
        if sender not in self.transport.approver_ids:
            self._answer(now, "未授权的审批人：本操作已忽略。", reply_to, {"sender": sender, "result": "unauthorized"})
            return
        if verb in STATUS_WORDS:
            self._answer(now, self._status_text(now), reply_to, {"sender": sender, "result": "status"})
            return
        sent_at = datetime.fromtimestamp(int(message.get("date", 0)), timezone.utc)
        target, problem = self._target(words[1:], (message.get("reply_to_message") or {}).get("message_id"), sent_at, now)
        if target is None:
            self._answer(now, problem, reply_to, {"sender": sender, "result": "unresolved", "text": text[:100]})
            return
        approve = verb in APPROVE_WORDS
        try:
            state = resolve_approval(self.root, target["id"], target["digest"], approve, now,
                                     authority=f"telegram:{sender}")
        except RiskError as exc:
            self._answer(now, f"未执行：{exc}", reply_to, {"sender": sender, "result": "refused",
                                                         "approval_id": target["id"]})
            return
        word = "已批准" if state == "approved" else "已拒绝"
        follow = ("交易进程将在下一次迭代重新通过全部风控后执行；硬性上限不受影响。" if state == "approved"
                  else "该目标已停止，等待 AI 下一次复核；已提交的订单须撤单处理。")
        self._answer(now, f"{word} {approval_code(target['id'])}。{follow}", reply_to,
                     {"sender": sender, "result": state, "approval_id": target["id"]})

    def _target(self, args, replied_message, sent_at, now):
        with operator_connection(self.root) as db:
            rows = [dict(r) for r in db.execute("SELECT id,digest,body,state FROM fund_approvals WHERE state='pending'")]
            delivered = {}
            if replied_message:
                for sequence, body in db.execute(
                        "SELECT e.sequence,e.body FROM events e JOIN fund_deliveries d ON d.sequence=e.sequence "
                        "WHERE e.kind='notification' AND d.message_id=?", (str(replied_message),)):
                    details = json.loads(body).get("details")
                    # Many notices carry plain text, not a request: never assume a dict.
                    delivered = details if isinstance(details, dict) else {}
        live = [r for r in rows if instant(json.loads(r["body"])["expires_at"]) > now]
        candidates = live
        if args:
            code = args[0].strip().upper()
            candidates = [r for r in live if approval_code(r["id"]) == code]
        elif replied_message:
            candidates = [r for r in live if r["id"] == delivered.get("id")]
        if len(candidates) != 1:
            if not live:
                return None, "没有待审批的请求，或请求已过期／被替换。"
            codes = "、".join(approval_code(r["id"]) for r in live)
            return None, f"有多个待审批请求，请回复对应提醒消息，或发送「同意 代码」／「拒绝 代码」：{codes}"
        target = candidates[0]
        if sent_at < instant(json.loads(target["body"])["created_at"]):
            return None, "这条回复早于审批请求，已忽略；请对新请求重新回复。"
        return target, None

    def _status_text(self, now):
        with operator_connection(self.root) as db:
            pending = db.execute("SELECT count(*) FROM fund_approvals WHERE state='pending'").fetchone()[0]
            account = db.execute("SELECT at,body FROM events WHERE kind='account' ORDER BY sequence DESC LIMIT 1").fetchone()
            health = db.execute("SELECT value FROM fund_monitor_meta WHERE key='health'").fetchone()
        lines = [f"监控状态：{health[0] if health else 'unknown'}；STOP：{(self.root / 'STOP').exists()}",
                 f"待审批请求：{pending}"]
        if account:
            body = json.loads(account[1])
            lines.append(f"最新分仓净值 ${body.get('equity')}，现金 ${body.get('cash')}，持仓 {encode(body.get('positions'))}"
                         f"（{account[0]}）")
        try:
            status = json.loads((self.root / "status.json").read_text(encoding="utf-8"))
            if status.get("valuation_error"):
                lines.append(f"账户估值暂未更新：{status['valuation_error']}；保留上次成功快照及其时间。")
        except (OSError, ValueError):
            pass
        return "\n".join(lines)

    def _answer(self, now, text, reply_to, audit):
        with operator_connection(self.root) as db:
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "telegram_command", encode(audit)))
        self.transport.send(text, reply_to=reply_to)


def metrics_text(root, status):
    """Prometheus text exposition of the fund's state, for a node_exporter textfile collector."""
    import sqlite3
    from contextlib import closing
    root = Path(root)
    lines = ["# HELP fund_monitor_timestamp_seconds When the independent monitor last checked.",
             "# TYPE fund_monitor_timestamp_seconds gauge",
             f"fund_monitor_timestamp_seconds {instant(status['at']).timestamp():.0f}",
             "# HELP fund_healthy 1 when the monitor sees a healthy runner, else 0 (see fund_health_state).",
             "# TYPE fund_healthy gauge", f"fund_healthy {int(status.get('health') == 'healthy')}",
             "# TYPE fund_health_state gauge", f'fund_health_state{{state="{status.get("health")}"}} 1',
             "# TYPE fund_stop_active gauge", f"fund_stop_active {int(bool(status.get('stop_active')))}",
             "# TYPE fund_disk_free_bytes gauge", f"fund_disk_free_bytes {status.get('disk_free_bytes', 0)}",
             "# TYPE fund_notifications gauge"]
    lines += [f'fund_notifications{{state="{state}"}} {count}'
              for state, count in sorted((status.get("delivery_counts") or {}).items())]
    from .datadesk import DeskStatus
    desk = DeskStatus(root).read()
    if desk and desk.get("heartbeat_at"):
        lines += ["# HELP fund_desk_heartbeat_timestamp_seconds When the data desk last wrote its heartbeat.",
                  "# TYPE fund_desk_heartbeat_timestamp_seconds gauge",
                  f"fund_desk_heartbeat_timestamp_seconds {instant(desk['heartbeat_at']).timestamp():.0f}",
                  "# TYPE fund_desk_dataset_timestamp_seconds gauge"]
        lines += [f'fund_desk_dataset_timestamp_seconds{{dataset="{name}"}} {instant(job["ok_at"]).timestamp():.0f}'
                  for name, job in sorted((desk.get("jobs") or {}).items()) if job.get("ok_at")]
    book = root / "state.sqlite3"
    if book.is_file():
        with closing(sqlite3.connect(book.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT body FROM events WHERE kind='account' ORDER BY sequence DESC LIMIT 1").fetchone()
            peak = db.execute("SELECT value FROM meta WHERE key='peak'").fetchone()
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            pending = (db.execute("SELECT count(*) FROM fund_approvals WHERE state='pending'").fetchone()[0]
                       if "fund_approvals" in tables else 0)
        if row:
            account = json.loads(row[0])
            equity, cash = float(account["equity"]), float(account["cash"])
            lines += ["# TYPE fund_equity_usd gauge", f"fund_equity_usd {equity:.2f}",
                      "# TYPE fund_cash_usd gauge", f"fund_cash_usd {cash:.2f}",
                      "# TYPE fund_cash_equivalent_usd gauge",
                      f"fund_cash_equivalent_usd {float(account.get('cash_equivalent_value') or 0):.2f}",
                      "# TYPE fund_gross_exposure gauge",
                      f"fund_gross_exposure {(equity - cash) / equity if equity > 0 else 0:.6f}"]
            try:  # Written raw by the equity marks; JSON-encoded by generic meta writes.
                high = float(json.loads(peak[0])) if peak else 0.0
            except (TypeError, ValueError):
                high = float(peak[0])
            if high > 0:
                lines += ["# TYPE fund_drawdown gauge", f"fund_drawdown {max(0.0, 1 - equity / high):.6f}"]
        lines += ["# TYPE fund_pending_approvals gauge", f"fund_pending_approvals {pending}"]
    return "\n".join(lines) + "\n"


def write_metrics(root, status):
    root = Path(root)
    temporary = root / "metrics.prom.tmp"
    temporary.write_text(metrics_text(root, status), encoding="utf-8")
    temporary.replace(root / "metrics.prom")


class RunnerHeartbeat:
    """Paper-run liveness, independent of a slow model call in that process."""
    def __init__(self, root, interval=5):
        self.root, self.stopped, self.interval = Path(root), threading.Event(), interval

    def _write(self, state):
        target = self.root / "runner_health.json"
        temporary = self.root / "runner_health.tmp"
        temporary.write_text(encode({"heartbeat_at": datetime.now(timezone.utc).isoformat(), "runner_state": state}), encoding="utf-8")
        temporary.replace(target)

    def _loop(self):
        while not self.stopped.wait(self.interval):
            try:
                self._write("running")
            except Exception:
                # A transient write failure (a reader holding the file on Windows,
                # a full disk) must not end the heartbeat while the runner trades.
                continue

    def __enter__(self):
        self._write("running")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopped.set()
        self.thread.join(timeout=6)
        self._write("exited")


def send_test(root):
    """Explicit operator test: sends one notification, never changes trading state."""
    transport = Telegram()
    monitor = Monitor(root, transport)
    now = datetime.now(timezone.utc)
    with operator_connection(root) as db:
        row = db.execute("SELECT body FROM events WHERE kind='startup' ORDER BY sequence DESC LIMIT 1").fetchone()
        if not row or json.loads(row[0]).get("mode") not in {"local", "paper"}:
            raise RiskError("Notification test requires an existing local or paper account")
        cursor = db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "notification", encode({
            "kind": "operator_notification_test", "details": "模拟账户通知测试；本操作没有提交任何交易订单。"})))
        key = cursor.lastrowid
        db.execute("INSERT INTO fund_deliveries VALUES(?,?,?,?,NULL,NULL)", (key, "pending", 0, now.isoformat()))
    monitor._deliver(now, only=key)
    with operator_connection(root) as db:
        row = dict(db.execute("SELECT * FROM fund_deliveries WHERE sequence=?", (key,)).fetchone())
    return {"event": key, "state": row["state"], "message_id": row["message_id"],
            "operator_should_confirm_on_device": True}


def run_monitor(args):
    from filelock import FileLock
    from .stops import ResumePolicy
    from .strategy import configuration
    policy = ResumePolicy.from_config(configuration(args.config).get("service"))
    transport = None if args.console_only else Telegram()
    with FileLock(str(args.run_dir / "monitor.lock"), timeout=0):
        monitor = Monitor(args.run_dir, transport, heartbeat_seconds=args.heartbeat_seconds, agent_seconds=args.agent_seconds,
                          resume_policy=policy)
        while True:
            status = monitor.poll()
            if args.once:
                print(encode(status))
                return
            time.sleep(POLL_SECONDS)
