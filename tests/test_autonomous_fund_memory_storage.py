"""Native AI preparation must fit the fund service's writable account directory."""
import errno
import json
import logging
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.autonomous_fund import run


@pytest.fixture
def entrypoint_environment(monkeypatch, tmp_path):
    # CLI setup writes process defaults; do not let them select another test's
    # account or mask an unconfigured native memory store.
    with patch.dict(os.environ):
        for key in ("LUMIBOT_MEMORY_DIR", "LUMIBOT_CACHE_FOLDER", "FUND_MODE", "FUND_RUN_DIR",
                    "FUND_OBSERVE_ONLY", "AI_MODEL", "FUND_RESEARCH_MODEL", "FUND_DECISION_MODEL"):
            os.environ.pop(key, None)
        monkeypatch.chdir(tmp_path)
        handlers = list(logging.getLogger().handlers)
        yield
        for handler in list(logging.getLogger().handlers):
            if handler not in handlers:
                logging.getLogger().removeHandler(handler)
                handler.close()


def config_file(tmp_path):
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "critic": {"enabled": False},
              "market_data": {"live_stream": False}, "events": {"enabled": False},
              "macro": {"enabled": False}, "news": {"enabled": False},
              "universe": {"ALFA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_cli_native_manager_works_when_repository_memory_is_read_only(
        tmp_path, monkeypatch, entrypoint_environment):
    from lumibot.components.agents.runtime import GoogleADKRuntime
    from lumibot.strategies import Strategy
    from lumibot.strategies.strategy_executor import StrategyExecutor
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from .test_autonomous_fund_local import Clock
    import scripts.autonomous_fund.market_data as market_data

    root = tmp_path / "artifacts" / "book"
    root.mkdir(parents=True)
    config = config_file(tmp_path)
    source_clock = Clock()
    source = YahooRealtimeData(now=source_clock, fetch=source_clock.fetch)
    monkeypatch.setattr(market_data, "YahooRealtimeData", lambda **kwargs: source)
    monkeypatch.setattr(run, "models_ready", lambda *args: {})
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setattr(Strategy, "risk_free_rate", property(lambda self: 0.0))
    monkeypatch.setattr(StrategyExecutor, "_publish_run_once_final_cloud_state", lambda *args, **kwargs: None)
    calls, blocked = [], []
    scripted = run.ScriptedRuntime()

    def model(self, request):
        calls.append(request.agent_name)
        return scripted.run(request)

    # Keep AgentHandle.run's lazy MemoryStore loading, tools and observability;
    # replace only inference and public-market downloads with offline fixtures.
    monkeypatch.setattr(GoogleADKRuntime, "run", model)
    original_mkdir = Path.mkdir
    forbidden = tmp_path / ".lumibot"

    def readonly_repository(path, *args, **kwargs):
        if path.resolve().is_relative_to(forbidden):
            blocked.append(path)
            raise OSError(errno.EROFS, "Synthetic read-only repository", str(path))
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", readonly_repository)
    run.main(["local", "--once", "--run-dir", str(root), "--config", str(config)])
    assert calls == ["manager"]
    assert not blocked
    memory = root / "memory" / "AutonomousLocalFund" / "memory.sqlite"
    assert memory.is_file()
    with sqlite3.connect(root / "state.sqlite3") as db:
        kinds = [r[0] for r in db.execute("SELECT kind FROM events")]
    assert "decision_validated" in kinds and "order_submitted" in kinds
    assert "decision_backoff" not in kinds


@pytest.mark.parametrize("mode", ["local", "paper"])
@pytest.mark.parametrize("override", [False, True])
def test_entrypoints_use_account_memory_and_honor_an_explicit_root(
        tmp_path, monkeypatch, entrypoint_environment, mode, override):
    from lumibot.components.memory import MemoryStore
    import scripts.autonomous_fund.report as reports
    root = tmp_path / "artifacts" / "selected-book"
    chosen = root / "configured-memory" if override else root / "memory"
    if override:
        monkeypatch.setenv("LUMIBOT_MEMORY_DIR", str(chosen))
    seen = []

    def start(args, config, *credentials):
        memory = MemoryStore(SimpleNamespace(name="SyntheticPortfolio"))
        memory.remember("Synthetic research note")
        seen.append(memory.db_path)

    monkeypatch.setattr(run, "models_ready", lambda *args: {})
    monkeypatch.setattr(run, "paper_config", lambda: {"API_KEY": "synthetic-key"})
    monkeypatch.setattr(reports, "report", lambda *args: {})
    monkeypatch.setattr(run, "run_" + mode, start)
    run.main([mode, "--run-dir", str(root), "--config", str(config_file(tmp_path))])
    assert seen == [chosen / "SyntheticPortfolio" / "memory.sqlite"]
    assert seen[0].is_file()
    assert not (tmp_path / ".lumibot").exists()


def test_status_does_not_create_or_configure_native_memory(tmp_path, monkeypatch, entrypoint_environment):
    root = tmp_path / "absent-book"
    run.main(["status", "--run-dir", str(root)])
    assert "LUMIBOT_MEMORY_DIR" not in os.environ
    assert not root.exists()


def test_native_research_caches_live_in_the_account_not_the_read_only_home(
        tmp_path, monkeypatch, entrypoint_environment):
    """2026-11-03 rehearsal: the AI's get_company_facts tool failed with EROFS on /home/<user>/.lumibot.

    LumiBot's SEC and FRED clients and the trading-day cache default to the home
    directory, which the service sandbox mounts read-only (ProtectHome=read-only).
    """
    from lumibot.fundamentals.sec import SECFundamentals
    from lumibot.macro.fred import FREDMacroData
    import scripts.autonomous_fund.report as reports
    for key in ("LUMIBOT_SEC_CACHE_DIR", "LUMIBOT_FRED_CACHE_DIR", "LUMIBOT_TRADING_DAYS_CACHE_DIR",
                "LUMIBOT_SEC_USER_AGENT"):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FUND_SEC_USER_AGENT", "Synthetic Operator ops@example.com")
    root = tmp_path / "artifacts" / "selected-book"
    seen = {}

    def start(args, config, *credentials):
        seen["sec"] = SECFundamentals().cache_dir
        seen["fred"] = FREDMacroData().cache_dir
        seen["agent"] = SECFundamentals().user_agent
        seen["days"] = os.environ.get("LUMIBOT_TRADING_DAYS_CACHE_DIR")

    monkeypatch.setattr(run, "models_ready", lambda *args: {})
    monkeypatch.setattr(reports, "report", lambda *args: {})
    monkeypatch.setattr(run, "run_local", start)
    run.main(["local", "--run-dir", str(root), "--config", str(config_file(tmp_path))])
    resolved = root.resolve()
    assert resolved in Path(seen["sec"]).resolve().parents and resolved in Path(seen["fred"]).resolve().parents
    assert resolved in Path(seen["days"]).resolve().parents
    assert seen["agent"] == "Synthetic Operator ops@example.com"  # The AI's SEC tool names the operator.
    assert not (home / ".lumibot").exists() and not (home / ".cache" / "lumibot").exists()


def test_every_agent_call_ends_before_the_monitor_would_stop_the_runner(monkeypatch, entrypoint_environment, tmp_path):
    """2026-11-03 rehearsal: a 300 s run timeout is per ATTEMPT and LumiBot retries a timeout up to 10 times live,

    so one researcher call could run ~54 minutes, past the monitor's 900 s agent_call_stalled limit.
    """
    from scripts.autonomous_fund import strategy
    import scripts.autonomous_fund.report as reports
    monkeypatch.delenv("LUMIBOT_AGENT_MAX_RUN_ATTEMPTS", raising=False)
    assert max(strategy.agent_worst_case_seconds(role) for role in strategy.AGENT_RUN_TIMEOUTS) > 900  # Unbounded.
    seen = {}
    monkeypatch.setattr(run, "models_ready", lambda *args: {})
    monkeypatch.setattr(reports, "report", lambda *args: {})
    monkeypatch.setattr(run, "run_local", lambda args, config, *c: seen.update(
        attempts=os.environ.get("LUMIBOT_AGENT_MAX_RUN_ATTEMPTS"),
        worst={role: strategy.agent_worst_case_seconds(role) for role in strategy.AGENT_RUN_TIMEOUTS}))
    run.main(["local", "--run-dir", str(tmp_path / "artifacts" / "book"), "--config", str(config_file(tmp_path))])
    assert seen["attempts"] == "2"
    assert all(seconds < strategy.MONITOR_AGENT_SECONDS for seconds in seen["worst"].values()), seen
    monkeypatch.setenv("LUMIBOT_AGENT_MAX_RUN_ATTEMPTS", "1")  # An explicit operator choice is kept.
    run.main(["local", "--run-dir", str(tmp_path / "artifacts" / "book"), "--config", str(config_file(tmp_path))])
    assert seen["attempts"] == "1"
