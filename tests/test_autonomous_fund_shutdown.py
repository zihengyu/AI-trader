"""Native shutdown must wake market waits and preserve account exclusion."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
from datetime import datetime, timezone

from filelock import FileLock

from scripts.autonomous_fund.journal import Journal, encode
from scripts.autonomous_fund.local_broker import LocalPaperBroker
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.risk import Limits

from .test_autonomous_fund_local import Clock, setup_book


def test_native_close_hook_wakes_a_weekend_length_sleep(tmp_path):
    _, _, _, broker = setup_book(tmp_path)
    sleeper = threading.Thread(target=broker.sleep, args=(48 * 3600,), daemon=True)
    sleeper.start()
    broker._close_connection()
    sleeper.join(timeout=1)
    assert not sleeper.is_alive()
    assert broker._stop_event.is_set()
    broker._close_connection()  # Native shutdown and application finally both close.


def test_native_trader_stop_exits_while_waiting_for_market_open(tmp_path, monkeypatch):
    from lumibot.strategies.strategy_executor import StrategyExecutor
    from lumibot.traders import Trader
    from scripts.autonomous_fund.strategy import AutonomousFund

    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setattr(StrategyExecutor, "_send_startup_cloud_update", lambda *args: None)
    clock = Clock()
    clock.value = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "reasoning_effort": None, "limits": {},
              "universe": {"ALFA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
    binding = hashlib.sha256(encode({"mode": "local", "account": "local-virtual-account", "config": config}).encode()).hexdigest()
    journal = Journal(tmp_path, binding)
    broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False, matching_enabled=False)
    sleeping = threading.Event()
    original_sleep = broker.sleep

    def observed_sleep(seconds):
        assert seconds > 60
        sleeping.set()
        original_sleep(seconds)

    monkeypatch.setattr(broker, "sleep", observed_sleep)
    strategy = AutonomousFund(broker=broker, name="ShutdownSession", parameters={
        "config": config, "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account",
        "observe_only": True})
    strategy._executor.daemon = True  # A future regression must not hang pytest itself.
    trader = Trader()
    trader.add_strategy(strategy)
    errors = []

    def run():
        try:
            trader.run_all()
        except BaseException as exc:
            errors.append(exc)

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    assert sleeping.wait(timeout=10)
    trader._stop_pool()  # The exact native SIGINT handler used by the launcher.
    runner.join(timeout=10)
    assert not runner.is_alive()
    assert not errors
    assert broker._stop_event.is_set()
    assert not journal.pending()
    assert broker._get_balances_at_broker(None, strategy)[0] == 10000


def test_busy_runner_has_actionable_error_without_touching_the_book(tmp_path):
    journal = Journal(tmp_path, "held-account")
    before = journal.path.read_bytes()
    env = {**os.environ, "LUMIBOT_DISABLE_DOTENV": "true", "LUMIBOT_CACHE_FOLDER": str(tmp_path / "cache")}
    with FileLock(str(tmp_path / "runner.lock"), timeout=0):
        result = subprocess.run(
            [sys.executable, "-m", "scripts.autonomous_fund.run", "local", "--observe-only", "--once", "--run-dir", str(tmp_path)],
            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1
        assert "already in use" in result.stderr
        assert "Stop the previous runner" in result.stderr
        assert "Traceback" not in result.stderr
        assert journal.path.read_bytes() == before
    with FileLock(str(tmp_path / "runner.lock"), timeout=0):
        pass  # Reacquisition relies on OS release; no lock-file deletion helper.
