"""An open-ended live run keeps bounded agent observability; backtests keep complete artifacts.

Synthetic strategy and runtime; no network, broker or model inference.
"""
from pathlib import Path

import pandas as pd
import pytest

from lumibot.components.agents import AgentManager, AgentRunResult, AgentTraceEvent
from lumibot.components.agents import manager as manager_module


class _Vars(dict):
    def set(self, key, value):
        self[key] = value


class _Strategy:
    parameters = {}

    def __init__(self, backtesting):
        self.is_backtesting = backtesting
        self.vars = _Vars()
        self.memory = _Memory()

    def get_datetime(self):
        from datetime import datetime, timezone
        return datetime(2026, 9, 2, 15, tzinfo=timezone.utc)

    def get_cash(self):
        return 100_000.0

    def get_portfolio_value(self):
        return 100_000.0

    def get_positions(self, include_cash_positions=True):
        return []

    def get_orders(self):
        return []

    def log_message(self, *args, **kwargs):
        return None


class _Memory:
    def __init__(self):
        self.exports = 0

    def export_artifacts(self, output_dir, *, prefix=None):
        self.exports += 1
        return {}


class _Runtime:
    def __init__(self):
        self.calls = 0

    def run(self, request):
        self.calls += 1
        return AgentRunResult(summary=f"Answer {self.calls}.", model=request.model,
                              events=[AgentTraceEvent(kind="text", text=f"Answer {self.calls}.")])


def _run(strategy, calls, name="watcher"):
    manager = AgentManager(strategy)
    agent = manager.create(name=name, tools=[], include_builtin_tools=False, _runtime=_Runtime())
    for _ in range(calls):
        agent.run(task_prompt="Report.")
    return manager, agent


def test_a_live_run_keeps_the_newest_detail_rows_and_rereads_memory_periodically(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setattr(manager_module, "LIVE_DETAIL_MAX_ROWS", 4)
    clock = [1000.0]
    monkeypatch.setattr(manager_module, "_monotonic", lambda: clock[0])
    strategy = _Strategy(backtesting=False)
    manager, agent = _run(strategy, 10)
    rows = manager._observability_rows["watcher"]
    assert len(rows) <= 4 and manager._observability_all_rows == []  # Not every row of the process lifetime.
    detail = pd.read_parquet(Path(tmp_path / "cache" / "agent_runtime" / "watcher_agent_detail.parquet"))
    assert len(detail) <= 4 and detail["call_index"].max() == 10  # The newest calls are kept.
    assert strategy.memory.exports == 1  # Ten calls within the interval: one full memory export.
    clock[0] += manager_module.LIVE_MEMORY_EXPORT_SECONDS
    agent.run(task_prompt="Report.")
    assert strategy.memory.exports == 2  # Refreshed once the interval has passed.


@pytest.mark.parametrize("stats_file", [None, "stats"])
def test_backtests_and_stats_file_runs_keep_complete_artifacts(tmp_path, monkeypatch, stats_file):
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setattr(manager_module, "LIVE_DETAIL_MAX_ROWS", 4)
    strategy = _Strategy(backtesting=stats_file is None)
    if stats_file:
        strategy.stats_file = str(tmp_path / "run_stats.csv")
    manager, _ = _run(strategy, 10)
    calls = {row["call_index"] for row in manager._observability_rows["watcher"]}
    assert calls == set(range(1, 11))  # Every call, as before.
    assert strategy.memory.exports == 10
