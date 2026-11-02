"""Regressions from a code review of the restart, stop and reporting paths.

Each test reproduces one confirmed finding. Synthetic journals, heartbeats and
child processes only; no network, broker or model.
"""
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund import stops
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import RiskError
from .test_autonomous_fund_autonomy import sent_orders  # noqa: F401  (pytest fixture)
from .test_autonomous_fund_monitor import NOW, FakeTelegram, heartbeat, setup


def fresh(root, filename="runner_health.json", age=0):
    root.mkdir(parents=True, exist_ok=True)
    at = datetime.now(timezone.utc) - timedelta(seconds=age)
    (root / filename).write_text(json.dumps({"heartbeat_at": at.isoformat(), "runner_state": "running"}))


def run_cli(tmp_path, monkeypatch, argv, env_text, mode=0o600):
    from scripts.autonomous_fund import run
    from scripts.autonomous_fund.settings import SETTINGS
    for name in list(SETTINGS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    env = tmp_path / ".env"
    env.write_text(env_text, encoding="utf-8")
    env.chmod(mode)
    before = set(os.environ)
    try:
        run.main([*argv, "--env-file", str(env)])
    finally:
        for name in set(os.environ) - before:
            os.environ.pop(name, None)


# 1. A brand-new account's first start is verified by a person.
def test_a_brand_new_account_waits_for_the_operator_on_its_first_start(tmp_path, monkeypatch):
    from scripts.autonomous_fund.service import main
    from .test_autonomous_fund_server import service_harness
    env, _ = service_harness(tmp_path, monkeypatch)
    new, existing = tmp_path / "artifacts-new", tmp_path / "artifacts-existing"
    Journal(existing, "synthetic-existing-account")
    main(["runner", "--run-dir", str(new), "--env-file", str(env)])
    main(["runner", "--run-dir", str(existing), "--env-file", str(env)])
    assert stops.read(new)["class"] == "operator" and stops.read(new)["reason"] == "first_start"
    assert stops.resumable(stops.read(existing))  # An existing book that crashed still resumes.


# 2. A liveness reason never hides a fault that needs the operator.
def test_a_liveness_reason_never_replaces_a_fault_that_needs_the_operator(tmp_path):
    stops.write(tmp_path, "watchdog", "matching_error", NOW)
    stops.write(tmp_path, "watchdog", "heartbeat_stale", NOW)
    stops.write(tmp_path, "watchdog", "independent_monitor_stale", NOW)
    assert stops.read(tmp_path)["reason"] == "matching_error" and not stops.resumable(stops.read(tmp_path))
    stops.clear(tmp_path)
    stops.write(tmp_path, "watchdog", "heartbeat_stale", NOW)
    stops.write(tmp_path, "watchdog", "low_disk_space", NOW)  # A fault needing a person may replace it.
    assert stops.read(tmp_path)["reason"] == "low_disk_space"


# 3. and 5. The emergency stop finds the account that is actually running.
def test_stop_reads_the_account_from_an_env_file_it_cannot_load(tmp_path, monkeypatch, capsys):
    run_cli(tmp_path, monkeypatch, ["stop"], "FUND_MODE=paper\n", mode=0o644)
    assert stops.read(tmp_path / "artifacts/fund-paper")["class"] == "operator"
    assert not (tmp_path / "artifacts/fund-local/STOP").exists()
    assert "chmod 600" in capsys.readouterr().out  # The problem is still reported.


def test_stop_without_a_directory_also_stops_every_running_account(tmp_path, monkeypatch, capsys):
    fresh(tmp_path / "artifacts/fund-paper")  # Started explicitly in paper while .env says local.
    fresh(tmp_path / "artifacts/autonomous-fund", "status.json", age=3600)  # Idle for an hour.
    run_cli(tmp_path, monkeypatch, ["stop"], "FUND_MODE=local\n")
    assert stops.read(tmp_path / "artifacts/fund-local")["class"] == "operator"
    assert stops.read(tmp_path / "artifacts/fund-paper")["class"] == "operator"
    assert not (tmp_path / "artifacts/autonomous-fund/STOP").exists()
    assert "artifacts/fund-paper" in capsys.readouterr().out


# 4. An unreadable earlier book fails closed instead of starting a new one.
def test_an_unreadable_earlier_book_fails_closed(tmp_path, monkeypatch):
    from scripts.autonomous_fund.settings import startup
    monkeypatch.chdir(tmp_path)
    earlier = tmp_path / "artifacts/autonomous-fund"
    earlier.mkdir(parents=True)
    (earlier / "state.sqlite3").write_bytes(b"not a database at all" * 10)
    with pytest.raises(RiskError, match="--run-dir"):
        startup("start", None, {"FUND_MODE": "local"})


# 6. The recovery message only promises an automatic resume that will happen.
def test_recovery_message_does_not_promise_a_resume_that_cannot_happen(tmp_path):
    from scripts.autonomous_fund.monitor import Monitor
    journal = setup(tmp_path)
    stops.write(tmp_path, "operator", "operator_stop", NOW)
    monitor = Monitor(tmp_path, FakeTelegram(), resume_policy=stops.ResumePolicy())
    monitor.poll(NOW + timedelta(minutes=10))  # Stale heartbeat: unhealthy.
    heartbeat(tmp_path, NOW + timedelta(minutes=11))
    monitor.poll(NOW + timedelta(minutes=11))
    recovered = [r["body"]["details"] for r in journal.all_events()
                 if r["kind"] == "notification" and r["body"]["kind"] == "watchdog_recovered"]
    assert recovered and "explicit" in recovered[0]


# 7. Each STOP gets its own notice, labelled with who stopped trading.
def test_every_new_stop_is_announced_with_its_reason(tmp_path):
    from .test_autonomous_fund_stability import live
    strategy = live(tmp_path)
    del strategy.notify  # The real, deduplicating notify.
    strategy._notices = {}
    stops.clear(tmp_path)  # live() leaves an older empty STOP, which counts as an operator stop.
    stops.write(tmp_path, "service", "service_start", NOW)
    strategy.on_trading_iteration()
    stops.clear(tmp_path)
    stops.write(tmp_path, "operator", "operator_stop", NOW + timedelta(hours=2))
    strategy.on_trading_iteration()
    stopped = [r["body"]["details"] for r in strategy.journal.all_events()
               if r["kind"] == "notification" and r["body"]["kind"] == "stop_active"]
    assert [d["reason"] for d in stopped] == ["service_start", "operator_stop"]
    assert stopped[1]["class"] == "operator"


# 8. On Windows, stopping serve ends the whole process tree.
def test_windows_stop_ends_the_child_process_tree(monkeypatch):
    import scripts.autonomous_fund.service as service
    calls = []

    class Child:
        pid, waited = 4242, 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.waited += 1
            if self.waited == 1:
                raise subprocess.TimeoutExpired("child", timeout)
            return 0
    monkeypatch.setattr(service.os, "name", "nt")
    monkeypatch.setattr(service.subprocess, "run", lambda command, **kwargs: calls.append(command))
    service.stop_child(Child(), timeout=1)
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


# 9. A report never presents an earlier session's scorecard as today's.
def test_daily_report_never_shows_an_earlier_sessions_scorecard(tmp_path):
    from scripts.autonomous_fund.notifications import daily_document
    from .test_autonomous_fund_notifications import completed
    from .test_autonomous_fund_review import TRADE
    strategy, _ = completed(tmp_path)
    strategy.journal.event(TRADE - timedelta(days=1), "scorecard", {
        "session_date": "2026-10-30", "predictions": {"total": 777, "scored": 0, "correct": 0, "hit_rate": None,
                                                      "pending": 777}, "decisions": {"by_horizon": {}}})
    with strategy.journal.connection() as db:
        sequence = db.execute("SELECT sequence FROM events WHERE kind='postmarket_review'").fetchone()[0]
    document = daily_document(tmp_path, sequence)[1]
    assert "777" not in document and "本交易日没有记分卡" in document


# 10. One daily-history request per symbol and day serves both risk and the factor lab.
def test_risk_and_the_factor_lab_share_one_history_request_per_symbol(tmp_path):
    from scripts.autonomous_fund.factors import FactorLab, FactorSettings
    from .test_autonomous_fund_quant import NOW as QUANT_NOW, default_frames, harness
    strategy = harness(tmp_path, default_frames())
    strategy.factor_lab, strategy._factor_cache = FactorLab(strategy.journal, FactorSettings()), None
    original, requests = strategy.get_historical_prices, []

    def counted(asset, length, timestep="day"):
        requests.append((asset.symbol, length))
        return original(asset, length, timestep)
    strategy.get_historical_prices = counted
    closes = strategy._daily_closes("ALFA", QUANT_NOW)
    strategy._factor_panel(QUANT_NOW)
    strategy._daily_closes("ALFA", QUANT_NOW)
    assert sorted(s for s, _ in requests) == sorted(strategy.universe)  # Each symbol fetched once.
    assert all(length == FactorSettings().history_days for _, length in requests)
    assert len(closes) <= strategy.quant.es_lookback_days + 5 and all(d < str(QUANT_NOW.date()) for d, _ in closes)


# ----- Second review -------------------------------------------------------------------------

def test_one_failing_account_does_not_stop_the_emergency_stop_of_the_others(tmp_path, monkeypatch, capsys):
    fresh(tmp_path / "artifacts/fund-paper")
    real = stops.write

    def flaky(root, kind, reason, now=None, detail=None):
        if Path(root).name == "fund-local":
            raise PermissionError("synthetic: owned by the service user")
        return real(root, kind, reason, now, detail)
    monkeypatch.setattr(stops, "write", flaky)
    with pytest.raises(RiskError, match="fund-local"):
        run_cli(tmp_path, monkeypatch, ["stop"], "FUND_MODE=local\n")
    assert stops.read(tmp_path / "artifacts/fund-paper")["class"] == "operator"  # Still stopped.


def test_stop_also_reaches_an_account_that_just_exited_and_will_be_restarted(tmp_path, monkeypatch):
    exited = tmp_path / "artifacts/fund-paper"
    fresh(exited, age=60)
    body = json.loads((exited / "runner_health.json").read_text())
    (exited / "runner_health.json").write_text(json.dumps({**body, "runner_state": "exited"}))
    fresh(tmp_path / "artifacts/autonomous-fund", "status.json", age=2 * 3600)  # Idle for two hours.
    run_cli(tmp_path, monkeypatch, ["stop"], "FUND_MODE=local\n")
    assert stops.read(exited)["class"] == "operator"  # The supervisor's restart cannot lift it.
    assert not (tmp_path / "artifacts/autonomous-fund/STOP").exists()


def test_stop_refuses_a_mistyped_run_directory_instead_of_reporting_success(tmp_path, monkeypatch):
    fresh(tmp_path / "artifacts/fund-paper")
    with pytest.raises(RiskError, match="fund-paper"):  # The error names the account that is running.
        run_cli(tmp_path, monkeypatch, ["stop", "--run-dir", "artifacts/fund-papr"], "FUND_MODE=local\n")
    assert not (tmp_path / "artifacts/fund-papr").exists()


class Hanging:
    def __init__(self, pid, exits_after=None):
        self.pid, self.timeouts, self.exits_after = pid, [], exits_after

    def poll(self):
        return 0 if self.exits_after is not None and len(self.timeouts) >= self.exits_after else None

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        if self.poll() is None:
            raise subprocess.TimeoutExpired("child", timeout)
        return 0


def test_windows_children_share_one_short_grace_period_and_a_stuck_one_never_blocks_the_rest(monkeypatch):
    import scripts.autonomous_fund.service as service
    calls = []
    monkeypatch.setattr(service.os, "name", "nt")
    monkeypatch.setattr(service.subprocess, "run", lambda command, **kwargs: calls.append(command[2]))
    stuck, other = Hanging(1), Hanging(2)
    clock = [0.0]

    def tick():
        clock[0] += 1
        return clock[0]
    service.stop_children([stuck, other], timeout=30, clock=tick)  # Must not raise.
    assert calls == ["1", "2"]  # Both trees ended, even though the first never exited.
    assert max(stuck.timeouts[0], other.timeouts[0]) <= service.WINDOWS_GRACE_SECONDS


def test_repeated_stops_with_the_same_reason_are_announced_once(tmp_path):
    from .test_autonomous_fund_stability import live
    strategy = live(tmp_path)
    del strategy.notify
    strategy._notices = {}
    stops.clear(tmp_path)
    for minutes in (0, 1, 2):  # Restart loop: a fresh service STOP record each time.
        stops.clear(tmp_path)
        stops.write(tmp_path, "service", "service_start", NOW + timedelta(minutes=minutes))
        strategy.on_trading_iteration()
    stopped = [r for r in strategy.journal.all_events()
               if r["kind"] == "notification" and r["body"]["kind"] == "stop_active"]
    assert len(stopped) == 1


def test_recovery_message_respects_the_daily_resume_limit(tmp_path):
    from scripts.autonomous_fund.monitor import Monitor
    journal = setup(tmp_path)
    for minutes in range(3):
        journal.event(NOW + timedelta(minutes=minutes), "auto_resume", {"stop": {}})
    monitor = Monitor(tmp_path, FakeTelegram(), resume_policy=stops.ResumePolicy())
    monitor.poll(NOW + timedelta(minutes=10))  # Stale heartbeat: a resumable liveness STOP.
    heartbeat(tmp_path, NOW + timedelta(minutes=11))
    monitor.poll(NOW + timedelta(minutes=11))
    recovered = [r["body"]["details"] for r in journal.all_events()
                 if r["kind"] == "notification" and r["body"]["kind"] == "watchdog_recovered"]
    assert recovered and "explicit" in recovered[0]


def test_a_bad_volume_value_never_costs_a_symbols_risk_history(tmp_path):
    import pandas as pd
    from .test_autonomous_fund_quant import NOW as QUANT_NOW, harness
    days = pd.bdate_range(end=QUANT_NOW.date() - timedelta(days=1), periods=5, tz="America/New_York")
    frame = pd.DataFrame({"close": [100.0, 101.0, 102.0, 103.0, 104.0],
                          "volume": [1e6, None, "n/a", 1e6, 1e6]}, index=days)
    strategy = harness(tmp_path)
    strategy.get_historical_prices = lambda asset, length, timestep="day": type("Bars", (), {"df": frame})()
    assert [c for _, c in strategy._daily_closes("ALFA", QUANT_NOW)] == [100.0, 101.0, 102.0, 103.0, 104.0]
    assert not any(r["kind"] == "risk_history_unavailable" for r in strategy.journal.all_events())


# ----- Third review: the order, approval, level and local-book core ------------------------

def test_a_reused_approval_must_cover_the_new_exposure_and_turnover(tmp_path):
    from .test_autonomous_fund_execution import NOW as EXEC_NOW, authorize, envelope, setup as exec_setup
    journal, state, account, active = exec_setup(tmp_path)
    first = state.request(active, envelope(max_exposure_after="0.45"), EXEC_NOW)
    authorize(tmp_path, first)
    second = state.request(active, envelope(max_exposure_after="0.47"), EXEC_NOW + timedelta(minutes=1))
    assert second["id"] != first["id"]  # A new request the operator is told about, not a silent stall.
    third = state.request(active, envelope(max_exposure_after="0.47", max_buy_turnover_usd="12000"),
                          EXEC_NOW + timedelta(minutes=2))
    assert third["id"] != second["id"]
    asked = [r for r in journal.all_events() if r["kind"] == "notification" and
             r["body"]["kind"] == "approval_required"]
    assert len(asked) == 3


def test_a_triggered_stop_keeps_selling_after_a_new_decision_and_blocks_rebuying_meanwhile(tmp_path, sent_orders):
    from .test_autonomous_fund_autonomy import accept, account, at, quote, watching
    strategy = watching(tmp_path, {"ALFA": quote(94)})
    accept(strategy, [{"symbol": "ALFA", "weight": .05, "stop_loss": 95}], when=at(-30))
    assert strategy._watch(account(9530, {"ALFA": 5}, price=94), at(0))  # Stop triggered, sale sent.
    first = strategy.journal.pending()[0]["id"]
    # The stop woke the AI: a new decision arrives before the sale fills, keeping ALFA with a new stop.
    new = accept(strategy, [{"symbol": "ALFA", "weight": .05, "stop_loss": 90}], when=at(0))
    level = strategy.execution.levels()["ALFA"]
    assert level["state"] == "triggered" and level["protective_target"] == "0"
    strategy.journal.update(first, "order-1", True, at(2))  # The first sale was canceled unfilled.
    assert strategy._watch(account(9530, {"ALFA": 5}, price=94), at(3))  # The exit is resumed, not abandoned.
    assert str(sent_orders[-1].side) == "sell"
    strategy.journal.update(strategy.journal.pending()[0]["id"], "order-2", True, at(4))
    rebuy = strategy._candidates(new, account(), {"ALFA": quote(96)}, at(0))
    assert rebuy["orders"] == []  # No purchase while the stop exit is still in progress.
    strategy._watch(account(10000, {}, price=96), at(5))  # The exit completed.
    level = strategy.execution.levels()["ALFA"]
    assert level["state"] == "active" and level["stop_loss"] == "90"  # The new decision's levels now apply.


def test_a_stale_mark_on_another_holding_never_blocks_a_risk_reducing_sale(tmp_path):
    from lumibot.entities import Asset
    from .test_autonomous_fund_local import advance, setup_book, submit
    clock, source, journal, broker = setup_book(tmp_path)
    key, bought = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    journal.update(key, bought.identifier, True, clock.value)  # The purchase is reconciled and terminal.
    with journal.connection() as db:
        db.execute("INSERT INTO local_positions VALUES('BETA', 5, '100', ?)", (str(clock.value),))
    stale = source.get_quote(Asset("BETA"))
    original = source.get_quote
    source.get_quote = lambda asset, *a, **k: stale if asset.symbol == "BETA" else original(asset, *a, **k)
    _, order = submit(broker, journal, clock.value, quantity=5, side="sell", limit=99)
    advance(clock, source, 10)
    broker.do_polling()
    assert broker._pull_order(order.identifier, "LocalTest").status == "fill"


def test_a_split_adjusted_dividend_restatement_does_not_halt_the_local_book(tmp_path):
    from .test_autonomous_fund_local import advance, setup_book, submit
    clock, source, journal, broker = setup_book(tmp_path)
    submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    paid = (clock.value + timedelta(days=1)).date().isoformat()
    clock.value += timedelta(days=3)
    clock.actions = [(paid, 0, 0.40)]
    source._cache.clear()
    broker._corporate_actions()
    assert broker.receivables() == pytest.approx(2.0)
    split = clock.value.date().isoformat()
    clock.price, clock.high = 50, 50.01
    clock.actions = [(paid, 0, 0.20), (split, 2, 0)]  # Yahoo restates the earlier dividend for the split.
    source._cache.clear()
    broker._get_balances_at_broker(None, "LocalTest")
    assert broker.receivables() == pytest.approx(2.0) and broker._pull_positions("LocalTest")[0].quantity == 10
    clock.actions = [(paid, 0, 0.35), (split, 2, 0)]  # A genuine revision still needs the operator.
    source._cache.clear()
    with pytest.raises(RiskError, match="revision"):
        broker._corporate_actions()


def test_a_take_profit_must_be_above_the_entry_limit():
    from scripts.autonomous_fund.risk import validate_decision
    from .test_autonomous_fund_autonomy import LIMITS, UNIVERSE, decision_body
    from .test_autonomous_fund_autonomy import NOW as AUTO_NOW
    with pytest.raises(RiskError, match="take-profit"):
        validate_decision(decision_body([{"symbol": "ALFA", "weight": .05, "entry_limit": 100, "stop_loss": 95,
                                          "take_profit": 99}], when=AUTO_NOW), AUTO_NOW, UNIVERSE, LIMITS)


def test_an_explicit_zero_submission_budget_allows_nothing(tmp_path):
    from scripts.autonomous_fund.risk import Intent, Limits, number
    journal = Journal(tmp_path, "synthetic-zero-budget")
    with pytest.raises(RiskError, match="daily submission limit"):
        journal.reserve(Intent("ALFA", "sell", 1, number(100)), NOW, Limits(), max_submissions=0)


def test_a_missing_session_never_becomes_a_two_day_return_in_the_risk_scenarios():
    from scripts.autonomous_fund.quant import daily_returns
    rows = [("2026-10-26", 100.0), ("2026-10-28", 110.0), ("2026-10-29", 111.1)]  # 10-27 missing here only.
    sessions = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
    assert daily_returns(rows, sessions) == pytest.approx({"2026-10-29": 0.01})
    assert set(daily_returns(rows)) == {"2026-10-28", "2026-10-29"}  # Without a calendar, as before.


def test_a_sale_is_never_filled_at_a_price_from_before_an_applied_split(tmp_path):
    import pandas as pd
    from types import SimpleNamespace
    from .test_autonomous_fund_local import advance, setup_book, submit
    clock, source, journal, broker = setup_book(tmp_path)
    key, bought = submit(broker, journal, clock.value)
    advance(clock, source)
    broker.do_polling()
    journal.update(key, bought.identifier, True, clock.value)
    split_day = (clock.value + timedelta(days=1)).date().isoformat()
    with journal.connection() as db:  # A 2:1 split already applied to the book (shares doubled).
        db.execute("INSERT INTO local_actions VALUES(?,?)", (f"ALFA:{split_day}:split", "2"))
    _, order = submit(broker, journal, clock.value, quantity=5, side="sell", limit=1)
    pre_split_bar = SimpleNamespace(high=100.0, low=100.0, volume=1e6)
    with pytest.raises(RiskError, match="predates"):
        broker._fill_bar(order.identifier, order, pd.Timestamp(clock.value), pre_split_bar)
