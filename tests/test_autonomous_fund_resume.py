"""STOP reasons, automatic resume after a service restart, and the cross-platform supervisor.

Synthetic journals, heartbeats and child processes only; nothing is sent or started.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund import stops
from scripts.autonomous_fund.monitor import Monitor
from scripts.autonomous_fund.risk import Intent, Limits, RiskError, number
from .test_autonomous_fund_monitor import NOW, FakeTelegram, heartbeat, setup

POLICY = stops.ResumePolicy(auto_resume=True, auto_resume_after_seconds=180, max_auto_resumes_per_day=3)


def run_healthy(tmp_path, monitor, start, seconds):
    status = None
    for offset in range(0, seconds + 1, 30):
        at = start + timedelta(seconds=offset)
        heartbeat(tmp_path, at)
        status = monitor.poll(at)
    return status


def notifications(journal):
    return [r["body"]["kind"] for r in journal.all_events() if r["kind"] == "notification"]


def test_stop_reasons_never_downgrade_and_unknown_content_is_an_operator_stop(tmp_path):
    stops.write(tmp_path, "service", "service_start", NOW)
    assert stops.read(tmp_path)["class"] == "service"
    stops.write(tmp_path, "watchdog", "heartbeat_stale", NOW)
    stops.write(tmp_path, "service", "service_exit", NOW)
    assert stops.read(tmp_path)["reason"] == "heartbeat_stale"  # A restart never hides the fault.
    stops.write(tmp_path, "operator", "operator_stop", NOW)
    stops.write(tmp_path, "watchdog", "heartbeat_stale", NOW)
    stops.write(tmp_path, "service", "service_start", NOW)
    assert stops.read(tmp_path)["class"] == "operator"
    assert not stops.resumable(stops.read(tmp_path))
    (tmp_path / "STOP").write_text("")  # A STOP written by an earlier version.
    assert stops.read(tmp_path)["class"] == "operator" and not stops.resumable(stops.read(tmp_path))
    assert stops.clear(tmp_path, {"class": "service", "reason": "x", "at": None}) is False
    assert (tmp_path / "STOP").exists()


def test_a_service_restart_resumes_by_itself_once_the_runner_is_healthy(tmp_path):
    journal = setup(tmp_path)
    stops.write(tmp_path, "service", "service_start", NOW)
    transport = FakeTelegram()
    monitor = Monitor(tmp_path, transport, resume_policy=POLICY)
    status = run_healthy(tmp_path, monitor, NOW, 150)
    assert status["stop_active"] and status["health"] == "stopped"  # Not before 180 s of continuous health.
    status = run_healthy(tmp_path, monitor, NOW + timedelta(seconds=180), 0)
    assert not (tmp_path / "STOP").exists() and status["health"] == "healthy" and not status["stop_active"]
    assert "auto_resumed" in notifications(journal)
    assert any("auto_resumed" in message for message in transport.messages)


def test_a_liveness_fault_resumes_only_after_the_runner_recovers(tmp_path):
    journal = setup(tmp_path)
    monitor = Monitor(tmp_path, FakeTelegram(), resume_policy=POLICY)
    stale = NOW + timedelta(minutes=10)
    monitor.poll(stale)  # The heartbeat is 10 minutes old: the watchdog stops trading.
    assert stops.read(tmp_path)["reason"] == "heartbeat_stale"
    monitor.poll(stale + timedelta(minutes=10))
    assert (tmp_path / "STOP").exists()  # Still unhealthy: never resumed.
    run_healthy(tmp_path, monitor, stale + timedelta(minutes=20), 180)
    assert not (tmp_path / "STOP").exists()
    assert notifications(journal).count("watchdog_stop") == 1


def test_the_health_period_restarts_after_every_fault(tmp_path):
    setup(tmp_path)
    monitor = Monitor(tmp_path, FakeTelegram(), resume_policy=POLICY)
    run_healthy(tmp_path, monitor, NOW, 600)  # Healthy for ten minutes first.
    fault = NOW + timedelta(minutes=20)
    monitor.poll(fault)  # The heartbeat stopped at minute ten: the watchdog stops trading.
    assert stops.read(tmp_path)["reason"] == "heartbeat_stale"
    recovered = fault + timedelta(minutes=5)
    run_healthy(tmp_path, monitor, recovered, 150)
    assert (tmp_path / "STOP").exists()  # Counted from the recovery, not from the earlier healthy period.
    run_healthy(tmp_path, monitor, recovered + timedelta(seconds=180), 0)
    assert not (tmp_path / "STOP").exists()


@pytest.mark.parametrize("case", ["operator", "legacy", "low_disk", "disabled", "unresolved_intent"])
def test_operator_stops_and_unsafe_states_are_never_resumed(tmp_path, case):
    journal = setup(tmp_path)
    policy = POLICY
    if case == "operator":
        stops.write(tmp_path, "operator", "operator_stop", NOW)
    elif case == "legacy":
        (tmp_path / "STOP").touch()
    elif case == "low_disk":
        stops.write(tmp_path, "watchdog", "low_disk_space", NOW)
    elif case == "disabled":
        stops.write(tmp_path, "service", "service_start", NOW)
        policy = stops.ResumePolicy(auto_resume=False)
    else:
        stops.write(tmp_path, "service", "service_start", NOW)
        journal.reserve(Intent("ALFA", "buy", 1, number(100)), NOW, Limits())  # Never sent, outcome unknown.
    monitor = Monitor(tmp_path, FakeTelegram(), resume_policy=policy)
    run_healthy(tmp_path, monitor, NOW, 900)
    assert (tmp_path / "STOP").exists()
    if case == "unresolved_intent":
        assert notifications(journal).count("auto_resume_blocked") == 1  # Said once, not every poll.


def test_automatic_resumes_are_rate_limited(tmp_path):
    journal = setup(tmp_path)
    monitor = Monitor(tmp_path, FakeTelegram(), resume_policy=POLICY)
    start = NOW
    for attempt in range(4):
        stops.write(tmp_path, "service", "service_start", start)
        run_healthy(tmp_path, monitor, start, 210)
        start += timedelta(minutes=30)
    assert (tmp_path / "STOP").exists()  # The fourth restart within a day waits for the operator.
    kinds = notifications(journal)
    assert kinds.count("auto_resumed") == 3 and kinds.count("auto_resume_exhausted") == 1


def test_stop_command_overrides_a_service_stop_and_resume_clears_any(tmp_path):
    from scripts.autonomous_fund import run
    stops.write(tmp_path, "service", "service_start", NOW)
    run.main(["stop", "--run-dir", str(tmp_path)])
    assert stops.read(tmp_path)["class"] == "operator"  # An operator stop is never auto-resumed.
    run.main(["resume", "--run-dir", str(tmp_path)])
    assert not (tmp_path / "STOP").exists()


def test_resume_policy_is_validated():
    with pytest.raises(RiskError):
        stops.ResumePolicy.from_config({"auto_resume_after_seconds": 10})
    with pytest.raises(RiskError, match="Unknown service"):
        stops.ResumePolicy.from_config({"resume": True})


# ----- Cross-platform supervisor (Windows has no systemd) --------------------------------

class Child:
    def __init__(self, role):
        self.role, self.code = role, None

    def poll(self):
        return self.code


def test_supervisor_keeps_runner_and_monitor_alive_with_a_restart_delay(tmp_path, monkeypatch):
    import scripts.autonomous_fund.service as service
    env = tmp_path / ".env"
    env.write_text("FUND_TELEGRAM_BOT_TOKEN=synthetic\nFUND_TELEGRAM_CHAT_ID=synthetic\n")
    env.chmod(0o600)
    started, stopped, clock, at = [], [], [0.0], []

    def popen(command, **kwargs):
        child = Child(command[3])
        started.append(child)
        at.append(clock[0])
        return child
    ticks = iter(range(100))

    def wait(seconds):
        clock[0] += seconds
        step = next(ticks)
        if step == 1:
            started[0].code = 1  # The runner crashed.
        return step >= 12
    stop = SimpleNamespace(is_set=lambda: False, wait=wait, set=lambda: None)
    monkeypatch.setattr(service, "stop_child", lambda child: stopped.append(child.role))
    monkeypatch.setattr(service, "system_stopping", lambda: False)
    service.serve(env, tmp_path / "config.json", tmp_path, popen=popen, clock=lambda: clock[0], stop=stop,
                  install_signals=False)
    assert stops.read(tmp_path)["class"] == "operator"  # Ctrl+C on serve is a manual stop.
    roles = [child.role for child in started]
    assert roles[:2] == ["runner", "monitor"] and roles.count("runner") == 2  # Restarted once, not in a loop.
    assert sorted(stopped) == ["monitor", "runner"]  # Every live child is stopped on exit.
    restart_at = next(i for i, c in enumerate(started) if i > 1 and c.role == "runner")
    assert restart_at == 2 and at[restart_at] - at[0] >= service.RESTART_SECONDS  # No tight crash loop.


def test_supervisor_refuses_to_run_unattended_without_notifications(tmp_path, monkeypatch):
    import scripts.autonomous_fund.service as service
    for key in ("FUND_TELEGRAM_BOT_TOKEN", "FUND_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("FUND_MODE=local\n")
    env.chmod(0o600)
    with pytest.raises(RiskError, match="FUND_TELEGRAM"):
        service.serve(env, tmp_path / "config.json", popen=lambda *a, **k: pytest.fail("started a child"),
                      install_signals=False)


# ----- Manual versus abnormal shutdown ------------------------------------------------------

def run_service(tmp_path, monkeypatch, *, signalled, shutting_down=False):
    import threading
    import scripts.autonomous_fund.service as service
    from .test_autonomous_fund_server import service_harness
    env, _ = service_harness(tmp_path, monkeypatch)
    if not (tmp_path / "state.sqlite3").exists():
        from scripts.autonomous_fund.journal import Journal
        Journal(tmp_path, "synthetic-existing-account")  # An account that has run before.
    child = SimpleNamespace(poll=lambda: None if signalled else 0, returncode=0 if signalled else 3)
    monkeypatch.setattr(service.subprocess, "Popen", lambda command, **kwargs: child)
    event = threading.Event()
    if signalled:
        event.set()  # systemctl stop / Ctrl+C reached the supervisor.
    monkeypatch.setattr(service.threading, "Event", lambda: event)
    monkeypatch.setattr(service, "system_stopping", lambda: shutting_down)
    service.main(["runner", "--run-dir", str(tmp_path), "--env-file", str(env)])
    return stops.read(tmp_path)


def test_a_deliberate_service_stop_is_manual_and_never_resumed_automatically(tmp_path, monkeypatch):
    record = run_service(tmp_path, monkeypatch, signalled=True)
    assert record["class"] == "operator" and not stops.resumable(record)


def test_a_crash_or_an_operating_system_shutdown_resumes_automatically(tmp_path, monkeypatch):
    crashed = run_service(tmp_path, monkeypatch, signalled=False)  # The child exited by itself.
    assert crashed["class"] == "service" and stops.resumable(crashed)
    stops.clear(tmp_path)
    shutdown = run_service(tmp_path, monkeypatch, signalled=True, shutting_down=True)
    assert shutdown["reason"] == "system_shutdown" and stops.resumable(shutdown)


@pytest.mark.parametrize("output, expected", [("stopping\n", True), ("running\n", False), ("degraded\n", False),
                                              (None, False)])
def test_operating_system_shutdown_detection_falls_back_to_manual(monkeypatch, output, expected):
    import scripts.autonomous_fund.service as service
    def run(*args, **kwargs):
        if output is None:
            raise FileNotFoundError("systemctl")
        return SimpleNamespace(stdout=output)
    monkeypatch.setattr(service.subprocess, "run", run)
    monkeypatch.setattr(service.os, "name", "posix")
    assert service.system_stopping() is expected


def test_on_windows_a_stop_signal_is_always_manual(monkeypatch):
    import scripts.autonomous_fund.service as service
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="stopping\n"))
    monkeypatch.setattr(service.os, "name", "nt")
    assert service.system_stopping() is False  # Windows shutdown kills the process; Ctrl+C is a person.
