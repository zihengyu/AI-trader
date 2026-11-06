"""A bookkeeping failure must never stop the trading process.

LumiBot ends a live strategy (on_bot_crash -> gracefully_exit) when an exception
escapes a lifecycle hook. Journal exports and report rebuilds are rebuildable
summaries: a transient failure in them (a locked database, a full disk, a bad
report row) is disclosed and retried on the next iteration instead of ending
the process. Historical runs still raise, so tests surface real bugs.
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

import scripts.autonomous_fund.report as report_module
from .test_autonomous_fund_premarket import harness


def failing(*args, **kwargs):
    raise OSError("No space left on device")


def notices(strategy):
    with strategy.journal.connection() as db:
        return [json.loads(r[0])["kind"] for r in db.execute("SELECT body FROM events WHERE kind='notification'")]


def live(tmp_path):
    strategy, _ = harness(tmp_path)
    strategy.broker = SimpleNamespace(is_market_open=lambda: False, do_polling=lambda: None)
    strategy.session_orders = {}
    strategy.halt_path.touch()  # A STOP iteration walks the whole hook without any order logic.
    return strategy


def test_an_iteration_survives_export_and_report_failures(tmp_path, monkeypatch):
    strategy = live(tmp_path)
    monkeypatch.setattr(strategy.journal, "export", failing)
    monkeypatch.setattr(report_module, "report", failing)
    strategy.on_trading_iteration()  # Must not raise: raising ends the live process.
    assert "bookkeeping_error" in notices(strategy)
    strategy.on_trading_iteration()  # And the next iteration runs normally.


def test_a_locked_journal_at_iteration_start_does_not_end_the_process(tmp_path, monkeypatch):
    strategy = live(tmp_path)
    original = strategy.journal.event

    def locked(at, kind, body):
        if kind in {"iteration_started", "iteration_finished"}:
            raise sqlite3.OperationalError("database is locked")
        return original(at, kind, body)
    monkeypatch.setattr(strategy.journal, "event", locked)
    strategy.on_trading_iteration()
    assert "blocked" in notices(strategy) or "bookkeeping_error" in notices(strategy)


def test_close_and_open_hooks_survive_report_failures(tmp_path, monkeypatch):
    strategy = live(tmp_path)
    strategy.halt_path.unlink()
    monkeypatch.setattr(strategy.journal, "export", failing)
    monkeypatch.setattr(report_module, "report", failing)
    strategy.after_market_closes()
    strategy.before_market_opens()
    assert notices(strategy).count("bookkeeping_error") >= 2


def test_even_a_broken_notification_path_does_not_end_the_process(tmp_path, monkeypatch):
    strategy = live(tmp_path)
    monkeypatch.setattr(strategy.journal, "export", failing)
    monkeypatch.setattr(report_module, "report", failing)
    # The protected path is the app's operational outbox, not native notify().
    strategy._notify_fund = failing
    strategy.on_trading_iteration()
    strategy.after_market_closes()


def test_historical_runs_still_raise_bookkeeping_bugs(tmp_path, monkeypatch):
    strategy = live(tmp_path)
    strategy.mode = "backtest"
    monkeypatch.setattr(strategy.journal, "export", failing)
    with pytest.raises(OSError):
        strategy.on_trading_iteration()


def test_a_crash_that_still_escapes_is_announced_to_the_monitor(tmp_path):
    strategy = live(tmp_path)
    strategy.on_abrupt_closing = lambda: None
    strategy.on_bot_crash(RuntimeError("synthetic"))
    assert "bot_crash" in notices(strategy)
