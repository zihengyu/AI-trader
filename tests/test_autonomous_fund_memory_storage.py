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
