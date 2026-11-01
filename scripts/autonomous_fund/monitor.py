"""Separate-process watchdog and durable, best-effort Telegram delivery."""
import hashlib
import json
import os
import time
import threading
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .execution import EXECUTION_REVISION, operator_connection
from .journal import encode
from .risk import RiskError, instant


def require_monitor(root, now):
    """Server execution gate, independent of AI and the supervising PID."""
    try:
        body = json.loads((Path(root) / "monitor.json").read_text(encoding="utf-8"))
        age = (instant(now) - instant(body["at"])).total_seconds()
        if (not 0 <= age <= 45 or body["health"] not in {"healthy", "stopped"} or
                not body.get("telegram_configured")):
            raise ValueError("Unhealthy independent monitor")
    except Exception:
        raise RiskError("Independent monitor unavailable or unhealthy; server execution blocked") from None


class Telegram:
    def __init__(self, token=None, chat_id=None, post=None):
        self.token = token or os.environ.get("FUND_TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("FUND_TELEGRAM_CHAT_ID")
        if not self.token or not self.chat_id:
            raise RiskError("Telegram requires FUND_TELEGRAM_BOT_TOKEN and FUND_TELEGRAM_CHAT_ID in the local environment")
        self.post = post or requests.post

    def send(self, text):
        return self._request("sendMessage", json={"chat_id": self.chat_id, "text": text[:4000],
            "link_preview_options": {"is_disabled": True}})

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
    def __init__(self, root, transport=None, *, heartbeat_seconds=180, agent_seconds=900):
        self.root, self.transport = Path(root), transport
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
            from .market_data import SessionClock
            market = SessionClock(now=lambda: now).status()
            iteration = db.execute("SELECT at FROM events WHERE sequence>? AND kind IN "
                "('iteration_started','iteration_finished') ORDER BY sequence DESC LIMIT 1", (saved["sequence"],)).fetchone()
            observed_at = instant(iteration[0] if iteration else saved[0])
            if (market["open"] and (now - instant(market["next_open"])).total_seconds() > 300 and
                    (now - observed_at).total_seconds() > self.heartbeat_seconds):
                state = "iteration_stalled"
        if shutil.disk_usage(self.root).free < 200_000_000:
            state = "low_disk_space"
        if state not in {"healthy", "stopped"}:
            # Reassert every poll even after someone resumes an unhealthy run;
            # deduplication applies to alerts, never to the protective STOP.
            (self.root / "STOP").touch()
        # A deliberate manual stop is already a safe state. Never auto-resume.
        if (self.root / "STOP").exists() and state == "healthy":
            state = "stopped"
        old = db.execute("SELECT value FROM fund_monitor_meta WHERE key='health'").fetchone()
        if not old or old[0] != state:
            if state not in {"healthy", "stopped"}:
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "notification", encode({
                    "kind": "watchdog_stop", "details": {"reason": state, "action": "STOP written; inspect broker orders and resume explicitly"}})))
            elif old and old[0] not in {"healthy", "stopped"}:
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(now), "notification", encode({
                    "kind": "watchdog_recovered", "details": "Health recovered; STOP still requires explicit operator resume"})))
            db.execute("INSERT OR REPLACE INTO fund_monitor_meta VALUES('health',?)", (state,))
        return state

    def poll(self, now=None):
        now = instant(now or datetime.now(timezone.utc))
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
                stale = body.get("kind") != "postmarket_report" and now - instant(row["at"]) > timedelta(hours=1)
                db.execute("INSERT INTO fund_deliveries VALUES(?,?,?,?,NULL,NULL)",
                           (row["sequence"], "suppressed" if duplicate or stale else "pending", 0, now.isoformat()))
        if self.transport is not None:
            self._deliver(now)
        with operator_connection(self.root) as db:
            counts = dict(db.execute("SELECT state,count(*) FROM fund_deliveries GROUP BY state"))
        status = {"at": now.isoformat(), "health": state, "telegram_configured": self.transport is not None,
                  "delivery_counts": counts, "stop_active": (self.root / "STOP").exists(),
                  "disk_free_bytes": shutil.disk_usage(self.root).free}
        temporary = self.root / "monitor.tmp"
        temporary.write_text(encode(status), encoding="utf-8")
        temporary.replace(self.root / "monitor.json")
        return status

    def _deliver(self, now, only=None):
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
                elif body.get("kind") == "approval_required" and (
                        "side" in body.get("details", {}) or "buys" in body.get("details", {})):
                    message = approval_message(body["details"])
                    result = (self.transport.send(message) if len(message) <= 4000 else
                              self.transport.send_document(message[:900] + "\n完整理由见附件。",
                                  f"approval-{body['details']['id']}.txt", message))
                else:
                    result = self.transport.send(text)
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


class RunnerHeartbeat:
    """Paper-run liveness, independent of a slow model call in that process."""
    def __init__(self, root):
        self.root, self.stopped = Path(root), threading.Event()

    def _write(self, state):
        target = self.root / "runner_health.json"
        temporary = self.root / "runner_health.tmp"
        temporary.write_text(encode({"heartbeat_at": datetime.now(timezone.utc).isoformat(), "runner_state": state}), encoding="utf-8")
        temporary.replace(target)

    def _loop(self):
        while not self.stopped.wait(5):
            self._write("running")

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
    transport = None if args.console_only else Telegram()
    with FileLock(str(args.run_dir / "monitor.lock"), timeout=0):
        monitor = Monitor(args.run_dir, transport, heartbeat_seconds=args.heartbeat_seconds, agent_seconds=args.agent_seconds)
        while True:
            status = monitor.poll()
            if args.once:
                print(encode(status))
                return
            time.sleep(10)
