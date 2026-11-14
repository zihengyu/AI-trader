"""Read-only quote evidence keeps source clocks, account boundaries and offline behavior."""
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.autonomous_fund.quote_diagnostics import inspect, lines, symbol_name
from scripts.autonomous_fund.risk import RiskError
from .test_autonomous_fund_alert_recovery import alert_strategy, fail_quote
from .test_autonomous_fund_autonomy import accept
from .test_autonomous_fund_quant import NOW, account


def snapshot(root, *, at=None, mode="local", **changes):
    checked = NOW + timedelta(seconds=77)
    body = {"mode": mode, "heartbeat_at": (at or checked).isoformat(), "runner_state": "running",
            "account_private": "SYNTHETIC_PRIVATE_VALUE", "market_data": {"quotes_by_symbol": {
                "ALFA": {"price_kind": "completed_minute_close_with_spread_proxy", "source_as_of": NOW.isoformat(),
                    "checked_at": checked.isoformat(), "age_seconds": 0, "stream_as_of": None,
                    "chart_as_of": (NOW - timedelta(minutes=3)).isoformat(), "raw": "SYNTHETIC_PRIVATE_VALUE"},
                "BETA": {"price_kind": "chart_last_trade_with_spread_proxy", "checked_at": checked.isoformat(),
                         "source_as_of": checked.isoformat()}}}}
    body.update(changes)
    path = root / ("status.json" if mode == "local" else "runner_health.json")
    path.write_text(json.dumps(body), encoding="utf-8")
    return body


def failed_watch(root):
    strategy = alert_strategy(root)
    accept(strategy, [], [{"symbol": "ALFA", "above": 110, "reason": "Synthetic watch"}])
    fail_quote(strategy, 77)
    strategy._watch(account(), NOW)
    return strategy


def test_selected_symbol_reports_its_saved_clock_and_pending_check_without_mutation(tmp_path):
    strategy = failed_watch(tmp_path)
    snapshot(tmp_path)
    files = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    report = inspect(tmp_path, symbol=" alfa ", now=NOW + timedelta(minutes=10))
    text = "\n".join(lines(report))
    assert len(report["symbols"]) == 1 and report["symbols"][0]["symbol"] == "ALFA"
    assert "BETA" not in text and "SYNTHETIC_PRIVATE_VALUE" not in json.dumps(report)
    assert "已过期" in text and "检查时已过去 77.0 秒" in text
    assert "尚未记录恢复" in text and "1 次失败检查" in text
    assert all(path.read_bytes() == before for path, before in files.items())
    assert strategy.execution.alerts() and not strategy.journal.pending()


def test_no_snapshot_or_journal_never_creates_an_account_or_claims_good_quotes(tmp_path):
    root = tmp_path / "absent"
    text = "\n".join(lines(inspect(root, symbol="CHAR", now=NOW)))
    assert "快照缺失" in text and "尚无账户账本" in text and '"CHAR"' in text
    assert not root.exists()


@pytest.mark.parametrize("clock", [None, "not-a-time", "2026-11-02T15:00:00", {}, 123])
def test_missing_invalid_or_naive_source_clocks_remain_unknown(tmp_path, clock):
    body = snapshot(tmp_path)
    body["market_data"]["quotes_by_symbol"]["ALFA"]["source_as_of"] = clock
    snapshot(tmp_path, market_data=body["market_data"])
    text = "\n".join(lines(inspect(tmp_path, symbol="ALFA", now=NOW + timedelta(seconds=80))))
    assert "报价源：未知或时间无效" in text


def test_future_source_and_exited_runner_are_not_presented_as_live_health(tmp_path):
    body = snapshot(tmp_path, at=NOW + timedelta(minutes=5), runner_state="exited")
    body["market_data"]["quotes_by_symbol"]["ALFA"]["source_as_of"] = (NOW + timedelta(seconds=82)).isoformat()
    snapshot(tmp_path, at=NOW + timedelta(minutes=5), runner_state="exited", market_data=body["market_data"])
    text = "\n".join(lines(inspect(tmp_path, symbol="ALFA", now=NOW)))
    assert "时间超前" in text and "进程已退出" in text and "检查时源时间超前 5.0 秒" in text


def test_paper_mode_does_not_borrow_the_local_snapshot(tmp_path):
    snapshot(tmp_path)
    text = "\n".join(lines(inspect(tmp_path, mode="paper", now=NOW)))
    assert "快照缺失" in text and '"ALFA"' not in text
    snapshot(tmp_path, mode="paper")
    body = json.loads((tmp_path / "runner_health.json").read_text(encoding="utf-8"))
    body["mode"] = "local"
    (tmp_path / "runner_health.json").write_text(json.dumps(body), encoding="utf-8")
    assert "模式不一致" in "\n".join(lines(inspect(tmp_path, mode="paper", now=NOW)))


def test_removed_alert_is_not_reclassified_as_a_current_fault(tmp_path):
    strategy = failed_watch(tmp_path)
    accept(strategy, [])
    report = inspect(tmp_path, symbol="ALFA", now=NOW)
    assert report["symbols"][0]["pending_alert"] is None
    assert "尚未记录恢复" not in "\n".join(lines(report))


def test_source_request_failures_are_visible_without_provider_payloads(tmp_path):
    snapshot(tmp_path, market_data={"source_failures": [{"symbol": "CHAR", "interval": "1m",
        "failed_at": NOW.isoformat(), "consecutive_failures": 3, "error": "SYNTHETIC_PRIVATE_VALUE"}]})
    report = inspect(tmp_path, now=NOW)
    assert [row["symbol"] for row in report["symbols"]] == ["CHAR"]
    assert "3" in "\n".join(lines(report)) and "SYNTHETIC_PRIVATE_VALUE" not in json.dumps(report)


def test_corrupt_heartbeat_or_ledger_is_disclosed_without_writing(tmp_path):
    (tmp_path / "status.json").write_text("broken", encoding="utf-8")
    (tmp_path / "state.sqlite3").write_bytes(b"broken")
    text = "\n".join(lines(inspect(tmp_path, symbol="ALFA", now=NOW)))
    assert "快照缺失、损坏或不可读" in text and "故障记录不可读" in text
    assert (tmp_path / "state.sqlite3").read_bytes() == b"broken"


@pytest.mark.parametrize("value", ["", "  ", "ALFA\nBETA", "ALFA\x1b", "X" * 65])
def test_invalid_console_symbol_is_explicit(value):
    with pytest.raises(RiskError):
        symbol_name(value)


@pytest.mark.parametrize("mode", ["local", "paper"])
def test_native_cli_uses_the_configured_account_offline_and_keeps_unknown_symbols_unknown(tmp_path, mode):
    selected = tmp_path / "selected"
    selected.mkdir()
    snapshot(selected, mode=mode)
    env = {key: value for key, value in os.environ.items() if not key.startswith("FUND_")}
    env.update(FUND_MODE=mode, FUND_RUN_DIR=str(selected), LUMIBOT_DISABLE_DOTENV="1")
    code = r'''
import importlib.abc, socket, sys
class Offline(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'lumibot' or fullname.startswith('lumibot.'):
            raise AssertionError('offline command imported SDK/runtime')
sys.meta_path.insert(0, Offline())
socket.create_connection=lambda *a, **k: (_ for _ in ()).throw(AssertionError('network access'))
from scripts.autonomous_fund.run import main
main(['quotes','--symbol','UNSEEN'])
'''
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code], env=env,
                            cwd=Path(__file__).resolve().parents[1], text=True, encoding="utf-8",
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '"UNSEEN"' in result.stdout and '"ALFA"' not in result.stdout
    assert not (selected / "state.sqlite3").exists()


def test_two_accounts_with_the_same_symbol_keep_independent_source_evidence(tmp_path):
    selected, unrelated = tmp_path / "selected", tmp_path / "unrelated"
    selected.mkdir()
    unrelated.mkdir()
    body = snapshot(selected)
    body["market_data"]["quotes_by_symbol"]["ALFA"]["source_as_of"] = (NOW - timedelta(days=10)).isoformat()
    snapshot(unrelated, market_data=body["market_data"])
    report = inspect(selected, symbol="ALFA", now=NOW + timedelta(seconds=80))
    assert report["symbols"][0]["observation"]["source_as_of"] == NOW.isoformat()
    other = inspect(unrelated, symbol="ALFA", now=NOW + timedelta(seconds=80))
    assert other["symbols"][0]["observation"]["source_as_of"] != NOW.isoformat()


def test_telegram_status_includes_active_failures_even_if_weekly_status_fails(tmp_path, monkeypatch):
    from scripts.autonomous_fund.monitor import Monitor
    from scripts.autonomous_fund import status
    strategy = failed_watch(tmp_path)
    snapshot(tmp_path)
    monitor = Monitor(tmp_path)
    monkeypatch.setattr(status, "weekly_lines", lambda *a, **kw: (_ for _ in ()).throw(ValueError("synthetic")))
    text = monitor._status_text(NOW)
    assert "价位提醒 ALFA" in text and "1 次失败检查" in text
    assert strategy.execution.alerts() and not strategy.journal.pending()
