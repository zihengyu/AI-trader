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
    for name in ("LUMIBOT_SEC_CACHE_DIR", "LUMIBOT_FRED_CACHE_DIR", "LUMIBOT_TRADING_DAYS_CACHE_DIR"):
        monkeypatch.setenv(name, str(tmp_path / "account" / "cache" / name.lower()))  # As the CLI sets them.
    clock = Clock()

    def run(account, **kw):
        kw.setdefault("live_data", False)  # Offline: the scripted AI must not bring live data sources.
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
    assert names[0] == "memory directory writable" and "critic and manager decision (scripted AI)" in names
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
        # A background unit, not tied to the terminal: a dropped SSH session cannot stop it.
        assert "--unit=lumibot-fund-rehearsal" in result.stdout and "--pipe" not in result.stdout
    assert subprocess.run(["bash", str(ROOT / "scripts" / "fund-rehearse.sh"), "--live"], capture_output=True,
                          env=variables).returncode == 2


def test_research_caches_left_in_the_home_directory_fail_the_rehearsal(tmp_path, rehearsal, monkeypatch):
    monkeypatch.delenv("LUMIBOT_SEC_CACHE_DIR")
    report = rehearsal(tmp_path / "account", models=False)
    stage = next(r for r in report["stages"] if r["stage"] == "research cache directories writable")
    assert report["verdict"] == "fail" and stage["status"] == "fail" and "LUMIBOT_SEC_CACHE_DIR" in stage["detail"]


def test_the_newest_report_can_be_shown_after_reconnecting(tmp_path, rehearsal, monkeypatch):
    from scripts.autonomous_fund.rehearsal import latest, text
    report = rehearsal(tmp_path / "account", models=False)
    saved = latest(tmp_path / "rehearsal")
    assert saved["copy"] == report["copy"] and "盘前彩排" in text(saved)


def test_model_call_durations_are_reported_and_a_call_past_the_monitor_limit_fails(tmp_path):
    from datetime import datetime, timedelta, timezone
    from scripts.autonomous_fund.rehearsal import Stages, _finish, agent_calls, text
    journal = Journal(tmp_path / "copy", "synthetic-calls")
    start = datetime(2026, 11, 3, 2, tzinfo=timezone.utc)
    journal.event(start, "agent_call_started", {"id": "a", "name": "researcher"})
    journal.event(start + timedelta(seconds=950), "agent_call_finished", {"id": "a", "name": "researcher"})
    journal.event(start, "agent_call_started", {"id": "b", "name": "manager"})
    calls = agent_calls(journal)
    assert calls[0] == {"name": "researcher", "seconds": 950.0} and calls[1]["unfinished"]
    report = _finish(tmp_path / "out", Stages(), tmp_path, 5, extra={"agent_calls": calls})
    assert report["verdict"] == "fail" and "researcher 950.0 秒" in text(report)


def test_each_model_call_reports_its_tokens_or_a_cache_hit(tmp_path):
    from datetime import datetime, timedelta, timezone
    from scripts.autonomous_fund.rehearsal import agent_calls, text
    journal = Journal(tmp_path / "copy", "synthetic-tokens")
    start = datetime(2026, 11, 3, 2, tzinfo=timezone.utc)
    for key, name, usage, hit in (("a", "researcher", {"prompt_tokens": 1200, "completion_tokens": 300}, False),
                                  ("b", "critic", None, True)):
        journal.event(start, "agent_call_started", {"id": key, "name": name})
        journal.event(start + timedelta(seconds=40), "agent_call_finished", {"id": key, "name": name})
        journal.event(start + timedelta(seconds=40), "agent", {"name": name, "usage": usage, "cache_hit": hit})
    calls = agent_calls(journal)
    assert calls[0] == {"name": "researcher", "seconds": 40.0, "input_tokens": 1200, "output_tokens": 300}
    assert calls[1]["cache_hit"] is True
    shown = text({"verdict": "ready", "copy": "c", "stages": [], "agent_calls": calls})
    assert "输入 1200 / 输出 300 tokens" in shown and "critic 40.0 秒（缓存命中）" in shown


def test_the_default_rehearsal_is_free_and_real_models_are_opt_in():
    from scripts.autonomous_fund.run import ScriptedRuntime, rehearsal_options
    models, runtime = rehearsal_options()
    assert models and isinstance(runtime, ScriptedRuntime)  # No inference, no model cost.
    assert rehearsal_options(real_models=True) == (True, None)
    assert rehearsal_options(no_models=True) == (False, None)


def test_a_scripted_rehearsal_still_uses_live_data_and_says_how_it_ran(tmp_path, rehearsal, monkeypatch):
    from scripts.autonomous_fund import events, macro
    from scripts.autonomous_fund.rehearsal import text
    fred = []
    monkeypatch.setattr(macro.MacroWatch, "_fetch", lambda self, name, today: fred.append(name) or [("2026-10-30", 4.0)])
    monkeypatch.setattr(events.EarningsCalendar, "fetch", lambda self, symbol, today: ("yahoo", [], None))
    report = rehearsal(tmp_path / "account", live_data=True)
    stage = {row["stage"]: row for row in report["stages"]}
    assert report["ai"] == "scripted" and fred  # The scripted AI ran on real (here: stubbed) FRED data.
    assert stage["FRED macro context"]["detail"] != "disabled"
    assert stage["critic and manager decision (scripted AI)"]["status"] == "ok"
    assert "模拟 AI：完整流程、真实数据、不调用模型、0 模型费用" in text(report)


def test_the_report_text_is_chinese_and_marks_scripted_calls(tmp_path):
    from datetime import datetime, timedelta, timezone
    from scripts.autonomous_fund.rehearsal import agent_calls, text
    journal = Journal(tmp_path / "copy", "synthetic-scripted")
    start = datetime(2026, 11, 3, 8, 40, tzinfo=timezone.utc)
    journal.event(start, "agent_call_started", {"id": "a", "name": "manager"})
    journal.event(start + timedelta(seconds=1), "agent_call_finished", {"id": "a", "name": "manager"})
    journal.event(start, "agent", {"name": "manager", "model": "scripted/no-inference",
                                   "usage": {"input_tokens": 0, "output_tokens": 0}})
    report = {"verdict": "ready", "copy": "c", "ai": "scripted", "agent_calls": agent_calls(journal), "stages": [
        {"stage": "data desk", "status": "ok", "seconds": 0.0, "detail": "running; calendar 1.6 min, news 2 min"},
        {"stage": "earnings calendar", "status": "ok", "seconds": 0.0, "detail": "0 capped, 0 unknown"},
        {"stage": "critic and manager decision (scripted AI)", "status": "ok", "seconds": 1.5,
         "detail": "models: researcher, critic, manager; order sizes need live quotes and are planned at the open"}]}
    shown = text(report)
    assert "运行中；经济日历 1.6 分钟、资讯 2 分钟" not in shown  # Separators stay as written.
    assert "运行中；经济日历 1.6 分钟, 资讯 2 分钟" in shown and "0 只财报前受限, 0 只日期未知" in shown
    assert "模型：researcher, critic, manager；" not in shown and "下单数量需要实时报价" in shown
    assert "manager 1.0 秒（模拟 AI）" in shown and "tokens" not in shown.split("每次模型调用")[1]


def test_translation_never_breaks_words_inside_error_messages():
    from scripts.autonomous_fund.rehearsal import _zh
    assert _zh("quote older than one minute; 3 symbols") == "quote older than one minute; 3 个标的"
