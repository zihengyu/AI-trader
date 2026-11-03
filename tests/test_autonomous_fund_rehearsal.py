"""Pre-open dress rehearsal: a full live decision on a copy of the account, never an order.

Uses the real LocalPaperBroker, StrategyExecutor initialization and decision path
with a synthetic clock/data source and the scripted model double. No network.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.autonomous_fund import stops
from scripts.autonomous_fund.journal import Journal

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {"model": "deepseek/deepseek-flash", "reasoning_effort": None, "limits": {},
          "universe": {"ALFA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.rehearsal import rehearse
    from scripts.autonomous_fund.run import ScriptedRuntime
    from tests.test_autonomous_fund_local import Clock
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_MEMORY_DIR", str(tmp_path / "account" / "memory"))
    clock = Clock()

    def run(account, **kw):
        source = YahooRealtimeData(now=clock, fetch=clock.fetch)
        return rehearse(account, CONFIG, runtime=ScriptedRuntime(), source=source, root=tmp_path / "rehearsal", **kw)
    return run


def test_a_full_decision_is_rehearsed_on_a_copy_and_no_order_is_sent(tmp_path, rehearsal):
    account = tmp_path / "account"
    account.mkdir()
    journal = Journal(account, "real-account-sentinel")
    journal.event("2026-11-02T14:00:00+00:00", "startup", {"mode": "local"})
    stops.write(account, "watchdog", "iteration_stalled")
    before = (account / "state.sqlite3").read_bytes()
    report = rehearsal(account)
    assert report["verdict"] == "ready", json.dumps(report["stages"], indent=2, default=str)
    names = [row["stage"] for row in report["stages"]]
    assert names[0] == "memory directory writable" and "critic and manager decision (real models)" in names
    assert report["construction"]["constructed"] and report["decision"]["targets"]
    assert report["orders_intercepted"]  # The plan was computed; nothing was submitted.
    assert (account / "state.sqlite3").read_bytes() == before  # The real account is untouched...
    assert stops.read(account)["reason"] == "iteration_stalled"  # ...including its STOP.
    copy = Path(report["copy"])
    assert (copy / "rehearsal.json").is_file() and (copy / "memory").is_dir()


def test_an_unwritable_memory_directory_fails_the_rehearsal_first(tmp_path, rehearsal, monkeypatch):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("a file where the memory directory should be")
    monkeypatch.setenv("LUMIBOT_MEMORY_DIR", str(blocked))
    report = rehearsal(tmp_path / "account", models=False)
    first = report["stages"][0]
    assert report["verdict"] == "fail" and first["stage"] == "memory directory writable" and first["status"] == "fail"


def test_without_models_only_data_stages_run(tmp_path, rehearsal):
    report = rehearsal(tmp_path / "account", models=False)
    decision = next(r for r in report["stages"] if r["stage"].startswith("critic and manager"))
    assert decision["status"] == "skipped" and "decision" not in report


def test_slow_required_work_fails_with_the_monitor_reason():
    from scripts.autonomous_fund.rehearsal import Stages
    ticks = iter([0.0, 130.0])
    stages = Stages(clock=lambda: next(ticks))
    stages.run("account snapshot", lambda: None, kind="required")
    assert stages.rows[0]["status"] == "fail" and "180 s" in stages.rows[0]["detail"]


def test_old_rehearsal_copies_are_pruned(tmp_path):
    from scripts.autonomous_fund.rehearsal import _prune
    for day in range(1, 8):
        (tmp_path / f"2026110{day}-120000").mkdir()
    (tmp_path / "keep-me").mkdir()
    _prune(tmp_path, 5)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["20261103-120000", "20261104-120000", "20261105-120000",
                                                          "20261106-120000", "20261107-120000", "keep-me"]


def test_the_script_runs_the_rehearsal_inside_the_service_sandbox(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FUND_MODE=local\n")
    variables = {k: v for k, v in os.environ.items() if not k.startswith("FUND_")}
    variables.update(AI_FUND_PYTHON=sys.executable, AI_FUND_ENV=str(env), AI_FUND_DRY_RUN="1", AI_FUND_USER="nobody")
    result = subprocess.run(["bash", str(ROOT / "scripts" / "fund-rehearse.sh"), "--no-models"], capture_output=True,
                            text=True, env=variables, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    if "systemd-run" in result.stdout:
        assert "-p ProtectSystem=strict" in result.stdout and "-p ProtectHome=read-only" in result.stdout
        assert "--uid=nobody" in result.stdout and "rehearse" in result.stdout and "--no-models" in result.stdout
    assert subprocess.run(["bash", str(ROOT / "scripts" / "fund-rehearse.sh"), "--live"], capture_output=True,
                          env=variables).returncode == 2
