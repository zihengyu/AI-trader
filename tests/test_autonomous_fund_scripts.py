"""The one-step Ubuntu scripts and the read-only status command.

The shell scripts run for real against a temporary account; only sudo/systemctl
commands are printed instead of executed (AI_FUND_DRY_RUN). No service starts.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.autonomous_fund import stops
from scripts.autonomous_fund.journal import Journal

ROOT = Path(__file__).resolve().parents[1]


def account_book(tmp_path, monitor_age=0, stop="operator"):
    now = datetime.now(timezone.utc)  # At call time: the monitor freshness check uses the wall clock.
    book = tmp_path / "book"
    journal = Journal(book, "synthetic-script-account")
    journal.event(now, "startup", {"mode": "local"})
    journal.event(now, "account", {"as_of": now.isoformat(), "cash": "10000", "equity": "10000", "positions": {}})
    journal.event(now, "notification", {"kind": "stop_active", "details": {"class": "operator"}})
    at = now.timestamp() - monitor_age
    (book / "monitor.json").write_text(json.dumps({
        "at": datetime.fromtimestamp(at, timezone.utc).isoformat(), "health": "stopped", "telegram_configured": True}))
    if stop:
        stops.write(book, stop, "first_start" if stop == "operator" else "service_start")
    env = tmp_path / ".env"
    env.write_text(f"FUND_MODE=local\nFUND_RUN_DIR={book}\n")
    env.chmod(0o600)
    return book, env


def script(name, tmp_path, env, *args):
    variables = {k: v for k, v in os.environ.items() if not k.startswith("FUND_")}
    variables.update(AI_FUND_PYTHON=sys.executable, AI_FUND_ENV=str(env), AI_FUND_DRY_RUN="1",
                     AI_FUND_UNIT_DIR=str(tmp_path / "installed"), AI_FUND_UNIT_BUILD=str(tmp_path / "build"),
                     AI_FUND_USER="nobody")
    return subprocess.run(["bash", str(ROOT / "scripts" / name), *args], capture_output=True, text=True,
                          env=variables, timeout=300)


@pytest.mark.parametrize("name", ["fund-start.sh", "fund-stop.sh", "fund-logs.sh", "fund-restart.sh"])
def test_scripts_are_valid_bash(name):
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)]).returncode == 0


def test_start_installs_starts_waits_for_the_monitor_and_resumes_a_manual_stop(tmp_path):
    book, env = account_book(tmp_path)
    (tmp_path / "installed").mkdir()
    (tmp_path / "installed" / "lumibot-fund-prefetch.timer").write_text("[Timer]\n")  # From an earlier version.
    result = script("fund-start.sh", tmp_path, env, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "+ sudo install -m 644" in result.stdout and "+ sudo systemctl daemon-reload" in result.stdout
    assert ("+ sudo systemctl enable --now lumibot-fund-runner.service lumibot-fund-monitor.service "
            "lumibot-fund-desk.service lumibot-fund-backup.timer") in result.stdout
    # The data desk replaced the daily prefetch timer: an installed one is disabled and removed.
    assert "+ sudo systemctl disable --now lumibot-fund-prefetch.timer" in result.stdout
    assert f"+ sudo rm -f {tmp_path / 'installed' / 'lumibot-fund-prefetch.timer'}" in result.stdout
    assert "已恢复交易" in result.stdout and not (book / "STOP").exists()
    assert sorted(p.name for p in (tmp_path / "build").iterdir()) == [
        "lumibot-fund-backup.service", "lumibot-fund-backup.timer", "lumibot-fund-desk.service",
        "lumibot-fund-monitor.service", "lumibot-fund-runner.service"]
    assert "+ sudo install -m 644" in result.stdout and "lumibot-fund-backup.timer" in result.stdout
    assert not (ROOT / "artifacts" / "autonomous-fund").exists()  # Checking the config leaves no stray directory.


def test_start_without_a_confirmation_keeps_trading_stopped(tmp_path):
    book, env = account_book(tmp_path)
    result = script("fund-start.sh", tmp_path, env)  # Not a terminal and no -y: never resumes by itself.
    assert result.returncode == 0 and "交易保持停止" in result.stdout
    assert stops.read(book)["class"] == "operator"


def test_start_never_resumes_while_the_monitor_is_not_ready(tmp_path):
    book, env = account_book(tmp_path, monitor_age=600)
    result = script("fund-start.sh", tmp_path, env, "-y")
    assert result.returncode == 1 and "监控还没有就绪" in result.stderr
    assert (book / "STOP").exists()


def test_start_creates_the_env_file_and_asks_for_the_keys_first(tmp_path):
    env = tmp_path / "missing.env"
    result = script("fund-start.sh", tmp_path, env)
    assert result.returncode == 1 and "DEEPSEEK_API_KEY" in result.stdout
    assert env.is_file() and oct(env.stat().st_mode & 0o777) == "0o600"


def test_stop_writes_an_operator_stop_then_stops_the_services(tmp_path):
    book, env = account_book(tmp_path, stop=None)
    result = script("fund-stop.sh", tmp_path, env, "--disable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert stops.read(book)["class"] == "operator"  # Never lifted automatically.
    assert ("+ sudo systemctl stop lumibot-fund-runner.service lumibot-fund-monitor.service "
            "lumibot-fund-desk.service") in result.stdout
    assert "+ sudo systemctl disable --now" in result.stdout and "lumibot-fund-backup.timer" in result.stdout


def test_logs_status_explains_the_state_in_one_screen(tmp_path):
    book, env = account_book(tmp_path)
    result = script("fund-logs.sh", tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    # Account amounts use dollars, grouping and cents in the separate holdings block.
    for text in ("模式：local", "交易：已停止（人工停止：新账户首次启动", "fund-start.sh", "独立监控：stopped",
                 "账户：净值 $10,000.00（", "📌 当前实际持仓", "最近通知：", "stop_active"):
        assert text in result.stdout, result.stdout
    # The shared CLI renderer measures elapsed time from the persisted account,
    # so re-querying an old observation cannot make its equity look fresh.
    [saved] = [row["body"] for row in Journal(book, "synthetic-script-account").all_events() if row["kind"] == "account"]
    from scripts.autonomous_fund.status import summary
    later = datetime.fromisoformat(saved["as_of"]) + timedelta(minutes=3)
    assert "净值 $10,000.00（3分钟前）" in summary(book, "local", now=later)
    assert "前） ｜ 现金" in result.stdout
    follow = script("fund-logs.sh", tmp_path, env, "logs")
    assert ("+ journalctl -u lumibot-fund-runner.service -u lumibot-fund-monitor.service "
            "-u lumibot-fund-desk.service -f") in follow.stdout
    assert script("fund-logs.sh", tmp_path, env, "unknown").returncode == 2


def test_status_command_flags_for_scripts(tmp_path, monkeypatch, capsys):
    from scripts.autonomous_fund import run
    for name in ("FUND_MODE", "FUND_RUN_DIR"):
        monkeypatch.delenv(name, raising=False)
    book, env = account_book(tmp_path)
    run.main(["status", "--run-dir", str(book), "--stop-class"])
    assert capsys.readouterr().out.strip() == "operator"
    run.main(["status", "--run-dir", str(book), "--check-monitor"])  # Fresh and stopped: ready.
    stale_book, _ = account_book(tmp_path / "stale", monitor_age=600, stop=None)
    with pytest.raises(SystemExit) as exit_code:
        run.main(["status", "--run-dir", str(stale_book), "--check-monitor"])
    assert exit_code.value.code == 3
    run.main(["status", "--run-dir", str(stale_book), "--stop-class"])
    assert capsys.readouterr().out.strip() == "none"
    missing = tmp_path / "never-started"
    run.main(["status", "--run-dir", str(missing)])
    assert "账本：尚未创建" in capsys.readouterr().out and not missing.exists()  # Read-only.


def test_units_only_writes_the_unit_files(tmp_path):
    from scripts.autonomous_fund import deploy
    deploy.main(["units", "--user", "nobody", "--env-file", str(tmp_path / ".env"), "--output-dir", str(tmp_path)])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lumibot-fund-backup.service", "lumibot-fund-backup.timer",
                                                          "lumibot-fund-desk.service", "lumibot-fund-monitor.service",
                                                          "lumibot-fund-runner.service"]


def test_rotating_backups_keep_the_newest_snapshots_only(tmp_path):
    from datetime import timedelta
    from scripts.autonomous_fund import deploy
    book, _ = account_book(tmp_path)
    start = datetime(2026, 11, 2, 22, 45, tzinfo=timezone.utc)
    made = [deploy.rotate(book, tmp_path / "backups", keep=3, now=start + timedelta(days=i)) for i in range(5)]
    (tmp_path / "backups" / "notes").mkdir()  # Never ours: never pruned.
    kept = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert kept == sorted([m.name for m in made[-3:]] + ["notes"])
    assert stops.read(made[-1])["reason"] == "restored_backup"  # A restore always starts stopped.
    with pytest.raises(Exception):
        deploy.rotate(book, tmp_path / "backups", keep=0)


def watchdog_book(tmp_path, exhausted):
    book, env = account_book(tmp_path, stop=None)
    stops.write(book, "watchdog", "iteration_stalled")
    if exhausted:
        journal = Journal(book, "synthetic-script-account")
        journal.event(datetime.now(timezone.utc), "notification", {"kind": "auto_resume_exhausted", "details": {
            "stop_reason": "iteration_stalled", "resumes_last_24h": 3}})
    return book, env


def test_exhausted_automatic_resumes_hand_the_stop_to_the_operator(tmp_path):
    book, env = watchdog_book(tmp_path / "exhausted", exhausted=True)
    status = script("fund-logs.sh", tmp_path / "exhausted", env)
    assert "自动恢复次数已用完" in status.stdout and "会自动恢复" not in status.stdout.split("交易：")[1].split("\n")[0]
    result = script("fund-start.sh", tmp_path / "exhausted", env, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "已恢复交易" in result.stdout and not (book / "STOP").exists()


def test_a_resumable_stop_with_resumes_left_is_left_to_the_monitor(tmp_path):
    book, env = watchdog_book(tmp_path / "waiting", exhausted=False)
    result = script("fund-start.sh", tmp_path / "waiting", env, "-y")
    assert result.returncode == 0 and "会自动恢复" in result.stdout and (book / "STOP").exists()


def test_an_old_exhaustion_notice_does_not_cover_a_newer_stop(tmp_path):
    from scripts.autonomous_fund.status import needs_operator
    book, _ = watchdog_book(tmp_path, exhausted=True)
    stops.clear(book)
    stops.write(book, "watchdog", "heartbeat_stale", now=datetime.now(timezone.utc).replace(year=2099))
    assert not needs_operator(book)  # The notice predates this STOP: the monitor may still resume it.


def test_restart_really_restarts_running_services_then_resumes(tmp_path):
    book, env = account_book(tmp_path)
    result = script("fund-restart.sh", tmp_path, env, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert out.index("+ sudo systemctl enable --now") < out.index(
        "+ sudo systemctl restart lumibot-fund-runner.service lumibot-fund-monitor.service lumibot-fund-desk.service")
    assert "已恢复交易" in out and not (book / "STOP").exists()
    plain = script("fund-start.sh", tmp_path, env)
    assert "systemctl restart" not in plain.stdout  # A plain start never restarts a running fund.


def test_restart_with_update_pulls_fast_forward_and_reinstalls_pinned_dependencies(tmp_path):
    _, env = account_book(tmp_path)
    result = script("fund-restart.sh", tmp_path, env, "--update")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pull --ff-only" in result.stdout and "constraints-py312-linux.txt" in result.stdout
    assert "交易保持停止" in result.stdout  # No -y and no terminal: never resumes by itself.
    assert script("fund-restart.sh", tmp_path, env, "--force").returncode == 2


def test_a_restart_is_confirmed_only_by_a_newer_monitor_state(tmp_path):
    from scripts.autonomous_fund.status import monitor_ready
    book, _ = account_book(tmp_path)
    written = datetime.now(timezone.utc).timestamp()
    assert monitor_ready(book) and monitor_ready(book, after=written - 60)
    assert not monitor_ready(book, after=written + 1)  # The previous monitor's last write does not count.
