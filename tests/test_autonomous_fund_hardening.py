"""Regressions from a code review of the restart, stop and reporting paths.

Each test reproduces one confirmed finding. Synthetic journals, heartbeats and
child processes only; no network, broker or model.
"""
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund import stops
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import RiskError
from .test_autonomous_fund_monitor import NOW, FakeTelegram, heartbeat, setup


def fresh(root, filename="runner_health.json", age=0):
    root.mkdir(parents=True, exist_ok=True)
    at = datetime.now(timezone.utc) - timedelta(seconds=age)
    (root / filename).write_text(json.dumps({"heartbeat_at": at.isoformat(), "runner_state": "running"}))


def run_cli(tmp_path, monkeypatch, argv, env_text, mode=0o600):
    from scripts.autonomous_fund import run
    from scripts.autonomous_fund.settings import SETTINGS
    for name in list(SETTINGS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    env = tmp_path / ".env"
    env.write_text(env_text, encoding="utf-8")
    env.chmod(mode)
    before = set(os.environ)
    try:
        run.main([*argv, "--env-file", str(env)])
    finally:
        for name in set(os.environ) - before:
            os.environ.pop(name, None)


# 1. A brand-new account's first start is verified by a person.
def test_a_brand_new_account_waits_for_the_operator_on_its_first_start(tmp_path, monkeypatch):
    from scripts.autonomous_fund.service import main
    from .test_autonomous_fund_server import service_harness
    env, _ = service_harness(tmp_path, monkeypatch)
    new, existing = tmp_path / "artifacts-new", tmp_path / "artifacts-existing"
    Journal(existing, "synthetic-existing-account")
    main(["runner", "--run-dir", str(new), "--env-file", str(env)])
    main(["runner", "--run-dir", str(existing), "--env-file", str(env)])
    assert stops.read(new)["class"] == "operator" and stops.read(new)["reason"] == "first_start"
    assert stops.resumable(stops.read(existing))  # An existing book that crashed still resumes.


# 2. A liveness reason never hides a fault that needs the operator.
def test_a_liveness_reason_never_replaces_a_fault_that_needs_the_operator(tmp_path):
    stops.write(tmp_path, "watchdog", "matching_error", NOW)
    stops.write(tmp_path, "watchdog", "heartbeat_stale", NOW)
    stops.write(tmp_path, "watchdog", "independent_monitor_stale", NOW)
    assert stops.read(tmp_path)["reason"] == "matching_error" and not stops.resumable(stops.read(tmp_path))
    stops.clear(tmp_path)
    stops.write(tmp_path, "watchdog", "heartbeat_stale", NOW)
    stops.write(tmp_path, "watchdog", "low_disk_space", NOW)  # A fault needing a person may replace it.
    assert stops.read(tmp_path)["reason"] == "low_disk_space"


# 3. and 5. The emergency stop finds the account that is actually running.
def test_stop_reads_the_account_from_an_env_file_it_cannot_load(tmp_path, monkeypatch, capsys):
    run_cli(tmp_path, monkeypatch, ["stop"], "FUND_MODE=paper\n", mode=0o644)
    assert stops.read(tmp_path / "artifacts/fund-paper")["class"] == "operator"
    assert not (tmp_path / "artifacts/fund-local/STOP").exists()
    assert "chmod 600" in capsys.readouterr().out  # The problem is still reported.


def test_stop_without_a_directory_also_stops_every_running_account(tmp_path, monkeypatch, capsys):
    fresh(tmp_path / "artifacts/fund-paper")  # Started explicitly in paper while .env says local.
    fresh(tmp_path / "artifacts/autonomous-fund", "status.json", age=3600)  # Idle for an hour.
    run_cli(tmp_path, monkeypatch, ["stop"], "FUND_MODE=local\n")
    assert stops.read(tmp_path / "artifacts/fund-local")["class"] == "operator"
    assert stops.read(tmp_path / "artifacts/fund-paper")["class"] == "operator"
    assert not (tmp_path / "artifacts/autonomous-fund/STOP").exists()
    assert "artifacts/fund-paper" in capsys.readouterr().out


# 4. An unreadable earlier book fails closed instead of starting a new one.
def test_an_unreadable_earlier_book_fails_closed(tmp_path, monkeypatch):
    from scripts.autonomous_fund.settings import startup
    monkeypatch.chdir(tmp_path)
    earlier = tmp_path / "artifacts/autonomous-fund"
    earlier.mkdir(parents=True)
    (earlier / "state.sqlite3").write_bytes(b"not a database at all" * 10)
    with pytest.raises(RiskError, match="--run-dir"):
        startup("start", None, {"FUND_MODE": "local"})


# 6. The recovery message only promises an automatic resume that will happen.
def test_recovery_message_does_not_promise_a_resume_that_cannot_happen(tmp_path):
    from scripts.autonomous_fund.monitor import Monitor
    journal = setup(tmp_path)
    stops.write(tmp_path, "operator", "operator_stop", NOW)
    monitor = Monitor(tmp_path, FakeTelegram(), resume_policy=stops.ResumePolicy())
    monitor.poll(NOW + timedelta(minutes=10))  # Stale heartbeat: unhealthy.
    heartbeat(tmp_path, NOW + timedelta(minutes=11))
    monitor.poll(NOW + timedelta(minutes=11))
    recovered = [r["body"]["details"] for r in journal.all_events()
                 if r["kind"] == "notification" and r["body"]["kind"] == "watchdog_recovered"]
    assert recovered and "explicit" in recovered[0]


# 7. Each STOP gets its own notice, labelled with who stopped trading.
def test_every_new_stop_is_announced_with_its_reason(tmp_path):
    from .test_autonomous_fund_stability import live
    strategy = live(tmp_path)
    del strategy.notify  # The real, deduplicating notify.
    strategy._notices = {}
    stops.clear(tmp_path)  # live() leaves an older empty STOP, which counts as an operator stop.
    stops.write(tmp_path, "service", "service_start", NOW)
    strategy.on_trading_iteration()
    stops.clear(tmp_path)
    stops.write(tmp_path, "operator", "operator_stop", NOW + timedelta(hours=2))
    strategy.on_trading_iteration()
    stopped = [r["body"]["details"] for r in strategy.journal.all_events()
               if r["kind"] == "notification" and r["body"]["kind"] == "stop_active"]
    assert [d["reason"] for d in stopped] == ["service_start", "operator_stop"]
    assert stopped[1]["class"] == "operator"


# 8. On Windows, stopping serve ends the whole process tree.
def test_windows_stop_ends_the_child_process_tree(monkeypatch):
    import scripts.autonomous_fund.service as service
    calls = []

    class Child:
        pid, waited = 4242, 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.waited += 1
            if self.waited == 1:
                raise subprocess.TimeoutExpired("child", timeout)
            return 0
    monkeypatch.setattr(service.os, "name", "nt")
    monkeypatch.setattr(service.subprocess, "run", lambda command, **kwargs: calls.append(command))
    service.stop_child(Child(), timeout=1)
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


# 9. A report never presents an earlier session's scorecard as today's.
def test_daily_report_never_shows_an_earlier_sessions_scorecard(tmp_path):
    from scripts.autonomous_fund.notifications import daily_document
    from .test_autonomous_fund_notifications import completed
    from .test_autonomous_fund_review import TRADE
    strategy, _ = completed(tmp_path)
    strategy.journal.event(TRADE - timedelta(days=1), "scorecard", {
        "session_date": "2026-10-30", "predictions": {"total": 777, "scored": 0, "correct": 0, "hit_rate": None,
                                                      "pending": 777}, "decisions": {"by_horizon": {}}})
    with strategy.journal.connection() as db:
        sequence = db.execute("SELECT sequence FROM events WHERE kind='postmarket_review'").fetchone()[0]
    document = daily_document(tmp_path, sequence)[1]
    assert "777" not in document and "本交易日没有记分卡" in document


# 10. One daily-history request per symbol and day serves both risk and the factor lab.
def test_risk_and_the_factor_lab_share_one_history_request_per_symbol(tmp_path):
    from scripts.autonomous_fund.factors import FactorLab, FactorSettings
    from .test_autonomous_fund_quant import NOW as QUANT_NOW, default_frames, harness
    strategy = harness(tmp_path, default_frames())
    strategy.factor_lab, strategy._factor_cache = FactorLab(strategy.journal, FactorSettings()), None
    original, requests = strategy.get_historical_prices, []

    def counted(asset, length, timestep="day"):
        requests.append((asset.symbol, length))
        return original(asset, length, timestep)
    strategy.get_historical_prices = counted
    closes = strategy._daily_closes("ALFA", QUANT_NOW)
    strategy._factor_panel(QUANT_NOW)
    strategy._daily_closes("ALFA", QUANT_NOW)
    assert sorted(s for s, _ in requests) == sorted(strategy.universe)  # Each symbol fetched once.
    assert all(length == FactorSettings().history_days for _, length in requests)
    assert len(closes) <= strategy.quant.es_lookback_days + 5 and all(d < str(QUANT_NOW.date()) for d, _ in closes)


# ----- Second review -------------------------------------------------------------------------

def test_one_failing_account_does_not_stop_the_emergency_stop_of_the_others(tmp_path, monkeypatch, capsys):
    fresh(tmp_path / "artifacts/fund-paper")
    real = stops.write

    def flaky(root, kind, reason, now=None, detail=None):
        if Path(root).name == "fund-local":
            raise PermissionError("synthetic: owned by the service user")
        return real(root, kind, reason, now, detail)
    monkeypatch.setattr(stops, "write", flaky)
    with pytest.raises(RiskError, match="fund-local"):
        run_cli(tmp_path, monkeypatch, ["stop"], "FUND_MODE=local\n")
    assert stops.read(tmp_path / "artifacts/fund-paper")["class"] == "operator"  # Still stopped.


def test_stop_also_reaches_an_account_that_just_exited_and_will_be_restarted(tmp_path, monkeypatch):
    exited = tmp_path / "artifacts/fund-paper"
    fresh(exited, age=60)
    body = json.loads((exited / "runner_health.json").read_text())
    (exited / "runner_health.json").write_text(json.dumps({**body, "runner_state": "exited"}))
    fresh(tmp_path / "artifacts/autonomous-fund", "status.json", age=2 * 3600)  # Idle for two hours.
    run_cli(tmp_path, monkeypatch, ["stop"], "FUND_MODE=local\n")
    assert stops.read(exited)["class"] == "operator"  # The supervisor's restart cannot lift it.
    assert not (tmp_path / "artifacts/autonomous-fund/STOP").exists()


def test_stop_refuses_a_mistyped_run_directory_instead_of_reporting_success(tmp_path, monkeypatch):
    fresh(tmp_path / "artifacts/fund-paper")
    with pytest.raises(RiskError, match="fund-paper"):  # The error names the account that is running.
        run_cli(tmp_path, monkeypatch, ["stop", "--run-dir", "artifacts/fund-papr"], "FUND_MODE=local\n")
    assert not (tmp_path / "artifacts/fund-papr").exists()


class Hanging:
    def __init__(self, pid, exits_after=None):
        self.pid, self.timeouts, self.exits_after = pid, [], exits_after

    def poll(self):
        return 0 if self.exits_after is not None and len(self.timeouts) >= self.exits_after else None

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        if self.poll() is None:
            raise subprocess.TimeoutExpired("child", timeout)
        return 0


def test_windows_children_share_one_short_grace_period_and_a_stuck_one_never_blocks_the_rest(monkeypatch):
    import scripts.autonomous_fund.service as service
    calls = []
    monkeypatch.setattr(service.os, "name", "nt")
    monkeypatch.setattr(service.subprocess, "run", lambda command, **kwargs: calls.append(command[2]))
    stuck, other = Hanging(1), Hanging(2)
    clock = [0.0]

    def tick():
        clock[0] += 1
        return clock[0]
    service.stop_children([stuck, other], timeout=30, clock=tick)  # Must not raise.
    assert calls == ["1", "2"]  # Both trees ended, even though the first never exited.
    assert max(stuck.timeouts[0], other.timeouts[0]) <= service.WINDOWS_GRACE_SECONDS


def test_repeated_stops_with_the_same_reason_are_announced_once(tmp_path):
    from .test_autonomous_fund_stability import live
    strategy = live(tmp_path)
    del strategy.notify
    strategy._notices = {}
    stops.clear(tmp_path)
    for minutes in (0, 1, 2):  # Restart loop: a fresh service STOP record each time.
        stops.clear(tmp_path)
        stops.write(tmp_path, "service", "service_start", NOW + timedelta(minutes=minutes))
        strategy.on_trading_iteration()
    stopped = [r for r in strategy.journal.all_events()
               if r["kind"] == "notification" and r["body"]["kind"] == "stop_active"]
    assert len(stopped) == 1


def test_recovery_message_respects_the_daily_resume_limit(tmp_path):
    from scripts.autonomous_fund.monitor import Monitor
    journal = setup(tmp_path)
    for minutes in range(3):
        journal.event(NOW + timedelta(minutes=minutes), "auto_resume", {"stop": {}})
    monitor = Monitor(tmp_path, FakeTelegram(), resume_policy=stops.ResumePolicy())
    monitor.poll(NOW + timedelta(minutes=10))  # Stale heartbeat: a resumable liveness STOP.
    heartbeat(tmp_path, NOW + timedelta(minutes=11))
    monitor.poll(NOW + timedelta(minutes=11))
    recovered = [r["body"]["details"] for r in journal.all_events()
                 if r["kind"] == "notification" and r["body"]["kind"] == "watchdog_recovered"]
    assert recovered and "explicit" in recovered[0]


def test_a_bad_volume_value_never_costs_a_symbols_risk_history(tmp_path):
    import pandas as pd
    from .test_autonomous_fund_quant import NOW as QUANT_NOW, harness
    days = pd.bdate_range(end=QUANT_NOW.date() - timedelta(days=1), periods=5, tz="America/New_York")
    frame = pd.DataFrame({"close": [100.0, 101.0, 102.0, 103.0, 104.0],
                          "volume": [1e6, None, "n/a", 1e6, 1e6]}, index=days)
    strategy = harness(tmp_path)
    strategy.get_historical_prices = lambda asset, length, timestep="day": type("Bars", (), {"df": frame})()
    assert [c for _, c in strategy._daily_closes("ALFA", QUANT_NOW)] == [100.0, 101.0, 102.0, 103.0, 104.0]
    assert not any(r["kind"] == "risk_history_unavailable" for r in strategy.journal.all_events())
