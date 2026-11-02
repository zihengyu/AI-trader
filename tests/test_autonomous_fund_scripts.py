"""The one-step Ubuntu scripts and the read-only status command.

The shell scripts run for real against a temporary account; only sudo/systemctl
commands are printed instead of executed (AI_FUND_DRY_RUN). No service starts.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
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


@pytest.mark.parametrize("name", ["fund-start.sh", "fund-stop.sh", "fund-logs.sh"])
def test_scripts_are_valid_bash(name):
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)]).returncode == 0


def test_start_installs_starts_waits_for_the_monitor_and_resumes_a_manual_stop(tmp_path):
    book, env = account_book(tmp_path)
    result = script("fund-start.sh", tmp_path, env, "-y")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "+ sudo install -m 644" in result.stdout and "+ sudo systemctl daemon-reload" in result.stdout
    assert "+ sudo systemctl enable --now lumibot-fund-runner.service lumibot-fund-monitor.service" in result.stdout
    assert "已恢复交易" in result.stdout and not (book / "STOP").exists()
    assert sorted(p.name for p in (tmp_path / "build").iterdir()) == [
        "lumibot-fund-monitor.service", "lumibot-fund-runner.service"]
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
    assert "+ sudo systemctl stop lumibot-fund-runner.service lumibot-fund-monitor.service" in result.stdout
    assert "+ sudo systemctl disable" in result.stdout


def test_logs_status_explains_the_state_in_one_screen(tmp_path):
    book, env = account_book(tmp_path)
    result = script("fund-logs.sh", tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    for text in ("模式：local", "交易：已停止（人工停止：新账户首次启动", "fund-start.sh", "独立监控：stopped",
                 "账户：净值 10000", "最近通知：", "stop_active"):
        assert text in result.stdout, result.stdout
    follow = script("fund-logs.sh", tmp_path, env, "logs")
    assert "+ journalctl -u lumibot-fund-runner.service -u lumibot-fund-monitor.service -f" in follow.stdout
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
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lumibot-fund-monitor.service", "lumibot-fund-runner.service"]
