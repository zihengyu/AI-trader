"""Sampled check denominators, frozen reports and nonblocking writes; synthetic feeds only."""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.notifications import daily_document, weekly_document
from scripts.autonomous_fund.quote_diagnostics import inspect as quotes
from scripts.autonomous_fund.quote_quality import inspect, lines, record, sample
from scripts.autonomous_fund.risk import MarketDataUnavailable, ObservedPrice, RiskError, number
from .test_autonomous_fund_alert_recovery import alert_strategy, fail_quote
from .test_autonomous_fund_autonomy import accept, at, watching, sent_orders  # noqa: F401
from .test_autonomous_fund_notifications import completed
from .test_autonomous_fund_quant import NOW, account, quote
from .test_autonomous_fund_weekly_report import close_of, ledger


def check(at=NOW, *, symbol="ALFA", purpose="price_alert", age=3, reason=None):
    observation = ObservedPrice(number(105), at - timedelta(seconds=age)) if age is not None else None
    if reason:
        detail = {"reason": reason, "raw": "PRIVATE_PROVIDER_TEXT"}
        if observation:
            detail["source_as_of"] = observation.as_of.isoformat()
        return sample(symbol, purpose, at, error=MarketDataUnavailable("PRIVATE_PROVIDER_TEXT", diagnostic=detail))
    return sample(symbol, purpose, at, observation=observation)


def groups(root, **kwargs):
    return inspect(root, until=kwargs.pop("until", NOW + timedelta(minutes=10)), **kwargs)["groups"]


def test_recorded_denominator_rejections_recovery_and_age_distribution(tmp_path):
    journal = Journal(tmp_path, "synthetic-quality")
    ages = [0, 1, 2, 10, 11, 30, 31, 60, 61, -1, None]
    checks = [check(NOW + timedelta(seconds=index), age=age,
                    reason="stale" if age == 61 else "future" if age == -1 else "source_unavailable" if age is None else None)
              for index, age in enumerate(ages)]
    record(journal, checks)
    record(journal, [check(NOW + timedelta(minutes=1))])
    [group] = groups(tmp_path)
    assert (group["checks"], group["accepted"], group["rejected"], group["recoveries"]) == (12, 9, 3, 1)
    assert group["accepted_share"] == .75
    age = group["age"]
    assert age["samples"] == 10 and age["future"] == 1 and age["unknown"] == 1
    assert age["max_seconds"] == 61 and age["mean_seconds"] == 20.9
    assert list(age["buckets"].values()) == [2, 3, 2, 2, 1]
    text = "\n".join(lines(inspect(tmp_path, until=NOW + timedelta(minutes=2))))
    assert "75.0%" in text and "不是独立行情请求数或全天可用率" in text and "失败后首次通过 1 次" in text
    assert "PRIVATE_PROVIDER_TEXT" not in json.dumps(checks) + json.dumps(group)
    assert b"PRIVATE_PROVIDER_TEXT" not in journal.path.read_bytes()


@pytest.mark.parametrize("at_value,day", [("2026-11-03T00:30:00+00:00", "2026-11-02"),
                                         ("2026-07-03T03:30:00+00:00", "2026-07-02")])
def test_day_uses_new_york_and_not_server_or_utc_date(tmp_path, at_value, day):
    journal = Journal(tmp_path, "synthetic-date")
    when = datetime.fromisoformat(at_value)
    record(journal, [check(when)])
    report = inspect(tmp_path, until=when)
    assert report["start_day"] == day and report["groups"][0]["checks"] == 1


def test_checks_are_separate_by_consumer_symbol_account_and_exact_cutoff(tmp_path):
    selected, unrelated = tmp_path / "selected", tmp_path / "unrelated"
    one, two = Journal(selected, "synthetic-one"), Journal(unrelated, "synthetic-two")
    record(one, [check(purpose="portfolio_watch"), check(symbol="BETA", reason="stale", age=75)])
    record(two, [check(reason="future", age=-10)])
    before = inspect(selected, until=NOW)
    record(one, [check(NOW + timedelta(microseconds=1)), check(NOW + timedelta(days=1))])
    assert inspect(selected, until=NOW) == before
    assert [g["symbol"] for g in groups(selected, symbol="BETA")] == ["BETA"]
    assert groups(unrelated)[0]["reasons"] == {"future": 1}
    assert groups(selected, symbol="UNSEEN") == []


def test_table_created_after_cutoff_does_not_relabel_legacy_reports(tmp_path):
    journal = Journal(tmp_path, "synthetic-legacy")
    before = inspect(tmp_path, until=NOW)
    record(journal, [check(NOW + timedelta(seconds=1))])
    assert inspect(tmp_path, until=NOW) == before
    assert inspect(tmp_path, start_day="2026-11-03", until=NOW + timedelta(days=1))["status"] == "empty"


def test_restart_retains_failures_and_first_success_counts_once(tmp_path):
    strategy = alert_strategy(tmp_path)
    strategy.mode = "local"
    accept(strategy, [], [{"symbol": "BETA", "above": 110, "reason": "Synthetic level"}])
    fail_quote(strategy)
    strategy._watch(account(), NOW)
    strategy._watch(account(), NOW)
    restarted = alert_strategy(tmp_path)
    restarted.mode = "local"
    restarted._alert_price = lambda symbol: ObservedPrice(number(105), NOW)
    restarted._watch(account(), NOW)
    restarted._watch(account(), NOW)
    [group] = groups(tmp_path)
    assert (group["checks"], group["accepted"], group["rejected"], group["recoveries"]) == (4, 2, 2, 1)
    assert restarted.execution.alerts() and not restarted.journal.pending()
    report = quotes(tmp_path, symbol="BETA", now=NOW)
    assert report["quality"]["groups"] == [group]


def test_recovery_on_a_later_day_does_not_invent_a_failure_in_that_day(tmp_path):
    journal = Journal(tmp_path, "synthetic-cross-day")
    record(journal, [check(reason="stale", age=75), check(NOW + timedelta(days=1))])
    report = inspect(tmp_path, until=NOW + timedelta(days=1))
    [group] = report["groups"]
    assert (group["checks"], group["accepted"], group["rejected"], group["recoveries"]) == (1, 1, 0, 1)
    assert "前次拒绝可能发生在此前日期" in "\n".join(lines(report))


def test_portfolio_rejection_and_successful_alert_recheck_are_distinct(tmp_path):
    strategy = alert_strategy(tmp_path)
    strategy.mode = "paper"
    accept(strategy, [], [{"symbol": "ALFA", "above": 110, "reason": "Synthetic level"}])
    strategy._price = lambda *a, **kw: (_ for _ in ()).throw(RiskError("PRIVATE_PROVIDER_TEXT"))
    strategy._alert_price = lambda symbol: ObservedPrice(number(105), NOW)
    strategy._watch(account(9000, {"ALFA": 1}), NOW)
    by_purpose = {g["purpose"]: g for g in groups(tmp_path)}
    assert by_purpose["portfolio_watch"]["reasons"] == {"risk_rejected": 1}
    assert by_purpose["price_alert"]["accepted"] == 1 and len(by_purpose) == 2


def test_protective_early_return_commits_observed_checks_but_no_unattempted_alert(tmp_path, sent_orders):
    strategy = watching(tmp_path, {"ALFA": quote(94)})
    strategy.mode = "local"
    # The mock harness needs an explicit open broker to exercise live submission guards.
    strategy.broker = SimpleNamespace(is_market_open=lambda: True)
    accept(strategy, [{"symbol": "ALFA", "weight": .05, "stop_loss": 95}],
           [{"symbol": "BETA", "above": 110, "reason": "Synthetic level"}], when=at(-30))
    assert strategy._watch(account(9530, {"ALFA": 5}, price=94), NOW)
    assert len(sent_orders) == 1 and str(sent_orders[0].side) == "sell"
    [group] = groups(tmp_path)
    assert group["purpose"] == "portfolio_watch" and group["accepted"] == 1


@pytest.mark.parametrize("phase", ["sample", "record"])
def test_stats_failure_cannot_block_a_protective_sale(tmp_path, monkeypatch, sent_orders, phase):
    from scripts.autonomous_fund import quote_quality
    strategy = watching(tmp_path, {"ALFA": quote(94)})
    strategy.mode = "local"
    strategy.broker = SimpleNamespace(is_market_open=lambda: True)
    accept(strategy, [{"symbol": "ALFA", "weight": .05, "stop_loss": 95}], when=at(-30))
    monkeypatch.setattr(quote_quality, phase, lambda *args, **kw: (_ for _ in ()).throw(OSError("synthetic storage failure")))
    assert strategy._watch(account(9530, {"ALFA": 5}, price=94), NOW)
    assert len(sent_orders) == 1 and any(kind == "quote_quality_error" for kind, _ in strategy.notices)


def test_stats_failure_does_not_suppress_a_real_alert_crossing(tmp_path, monkeypatch):
    from scripts.autonomous_fund import quote_quality
    strategy = alert_strategy(tmp_path)
    strategy.mode = "local"
    accept(strategy, [], [{"symbol": "BETA", "above": 104, "reason": "Synthetic level"}])
    strategy._alert_price = lambda symbol: ObservedPrice(number(105), NOW)
    monkeypatch.setattr(quote_quality, "record", lambda *args: (_ for _ in ()).throw(OSError("synthetic storage failure")))
    strategy._watch(account(), NOW)
    assert not strategy.execution.alerts() and not strategy.journal.pending()
    assert any(row["kind"] == "price_alert" for row in strategy.journal.all_events())


def test_original_unexpected_error_still_escapes_and_prior_checks_are_committed(tmp_path):
    strategy = watching(tmp_path, {})
    strategy.mode = "local"
    strategy._price = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic unexpected error"))
    with pytest.raises(RuntimeError, match="synthetic unexpected error"):
        strategy._watch(account(9000, {"ALFA": 1}), NOW)
    [group] = groups(tmp_path)
    assert group["reasons"] == {"check_error": 1}


@pytest.mark.parametrize("mode", ["mock", "backtest"])
def test_historical_and_mock_watches_do_not_create_quality_evidence(tmp_path, mode):
    strategy = alert_strategy(tmp_path)
    strategy.mode = mode
    accept(strategy, [], [{"symbol": "BETA", "above": 110, "reason": "Synthetic level"}])
    fail_quote(strategy)
    strategy._watch(account(), NOW)
    assert inspect(tmp_path, until=NOW)["status"] == "untracked"


def test_batch_is_atomic_and_a_failed_write_cannot_lose_a_recovery(tmp_path):
    journal = Journal(tmp_path, "synthetic-atomic")
    record(journal, [check(reason="stale", age=75)])
    before = inspect(tmp_path, until=NOW)
    with journal.connection() as db:
        db.execute("CREATE TRIGGER sample_failure BEFORE INSERT ON fund_quote_checks WHEN NEW.symbol='BETA' "
                   "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        record(journal, [check(), check(symbol="BETA")])
    assert inspect(tmp_path, until=NOW) == before
    record(journal, [check()])
    assert groups(tmp_path)[0]["recoveries"] == 1


def test_clock_regression_does_not_claim_recovery_from_a_future_check(tmp_path):
    journal = Journal(tmp_path, "synthetic-regression")
    record(journal, [check(reason="stale", age=75), check(NOW - timedelta(seconds=1))])
    assert groups(tmp_path)[0]["recoveries"] == 0
    record(journal, [check(NOW + timedelta(seconds=1)), check(NOW - timedelta(seconds=2), reason="stale", age=75),
                     check(NOW + timedelta(seconds=2))])
    assert groups(tmp_path)[0]["recoveries"] == 1


def test_readonly_missing_and_corrupt_accounts_remain_unknown_without_creation(tmp_path):
    absent = tmp_path / "absent"
    assert inspect(absent, until=NOW)["status"] == "untracked" and not absent.exists()
    path = tmp_path / "state.sqlite3"
    path.write_bytes(b"synthetic corrupt database")
    report = inspect(tmp_path, until=NOW)
    assert report["status"] == "unavailable" and "不能据此判断行情正常" in "\n".join(lines(report))
    assert path.read_bytes() == b"synthetic corrupt database"


def test_real_stream_watch_records_quality_without_new_history_requests(tmp_path):
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from .test_autonomous_fund_freshness_recovery import inert_tape
    from .test_autonomous_fund_local import Clock
    from .test_autonomous_fund_realtime import frame
    clock, tape = Clock(), inert_tape()
    clock.value = NOW + timedelta(seconds=5)
    source = YahooRealtimeData(now=clock, fetch=clock.fetch, tape=tape)
    tape.watch(["BETA"])
    tape.handle(frame("BETA", 105, clock() - timedelta(seconds=1)))
    strategy = alert_strategy(tmp_path)
    strategy.mode, strategy.get_datetime, strategy.get_quote = "local", clock, source.get_quote
    from scripts.autonomous_fund.strategy import AutonomousFund
    strategy._alert_price = lambda symbol: AutonomousFund._alert_price(strategy, symbol)
    accept(strategy, [], [{"symbol": "BETA", "above": 110, "reason": "Synthetic level"}])
    strategy._watch(account(), clock())
    strategy._watch(account(), clock())
    assert source.diagnostics()["requests"] == 0 and not source._cache
    [group] = groups(tmp_path)
    assert group["checks"] == group["accepted"] == 2 and group["age"]["mean_seconds"] == 1
    assert not strategy.journal.pending() and strategy.execution.alerts()


@pytest.mark.parametrize("mode", ["local", "paper"])
def test_native_diagnostic_reads_selected_quality_offline_without_database_changes(tmp_path, mode):
    selected = tmp_path / "selected"
    now = datetime.now(timezone.utc)
    journal = Journal(selected, "synthetic-selected-" + mode)
    record(journal, [check(now, symbol="ALFA")])
    record(Journal(tmp_path / "unrelated", "synthetic-other"), [check(now, symbol="BETA")])
    before = {p: p.read_bytes() for p in selected.iterdir() if p.is_file()}
    env = {k: v for k, v in os.environ.items() if not k.startswith("FUND_")}
    env.update(FUND_MODE=mode, FUND_RUN_DIR=str(selected), LUMIBOT_DISABLE_DOTENV="1")
    code = r'''
import importlib.abc, socket, sys
class Offline(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'lumibot' or fullname.startswith('lumibot.'):
            raise AssertionError('offline diagnostic imported SDK')
sys.meta_path.insert(0, Offline())
socket.create_connection=lambda *a, **k: (_ for _ in ()).throw(AssertionError('network access'))
from scripts.autonomous_fund.run import main
main(['quotes','--symbol','ALFA'])
'''
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code], env=env,
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "检查 1，通过 1，拒绝 0" in result.stdout and "BETA" not in result.stdout
    assert all(p.read_bytes() == content for p, content in before.items())


@pytest.mark.parametrize("detail", [None, ["PRIVATE_PROVIDER_TEXT"],
                                   {"reason": ["PRIVATE_PROVIDER_TEXT"], "source_as_of": "not a timestamp"}])
def test_malformed_failure_diagnostics_keep_the_denominator_and_drop_private_values(tmp_path, detail):
    journal = Journal(tmp_path, "synthetic-malformed")
    record(journal, [sample("ALFA", "price_alert", NOW, error=MarketDataUnavailable(
        "PRIVATE_PROVIDER_TEXT", diagnostic=detail))])
    [group] = groups(tmp_path)
    assert group["checks"] == group["rejected"] == group["age"]["unknown"] == 1
    assert group["reasons"] == {"source_unavailable": 1} and "PRIVATE_PROVIDER_TEXT" not in json.dumps(group)


def test_no_observation_is_not_a_successful_check():
    result = sample("ALFA", "price_alert", NOW)
    assert not result["accepted"] and result["reason"] == "check_error" and result["age_seconds"] is None


def test_backups_preserve_samples_and_normal_pruning_does_not_remove_report_evidence(tmp_path):
    from scripts.autonomous_fund.deploy import backup
    from scripts.autonomous_fund.retention import prune
    source, saved = tmp_path / "source", tmp_path / "saved"
    journal = Journal(source, "synthetic-preservation")
    record(journal, [check(reason="stale", age=75), check()])
    before = inspect(source, until=NOW)
    backup(source, saved)
    prune(source, now=NOW + timedelta(days=500), apply=True)
    assert inspect(source, until=NOW) == inspect(saved, until=NOW) == before
    assert (saved / "STOP").is_file()


def test_daily_and_weekly_reports_freeze_quote_quality_at_the_selected_review(tmp_path):
    daily_root, weekly_root = tmp_path / "daily", tmp_path / "weekly"
    strategy, _ = completed(daily_root)
    with strategy.journal.connection() as db:
        sequence, body = db.execute("SELECT sequence,body FROM events WHERE kind='postmarket_review'").fetchone()
    cutoff = datetime.fromisoformat(json.loads(body)["completed_at"])
    record(strategy.journal, [check(cutoff - timedelta(seconds=1), reason="stale", age=75)])
    before = daily_document(daily_root, sequence)
    assert "检查 1，通过 0，拒绝 1" in before[1]
    record(strategy.journal, [check(cutoff + timedelta(microseconds=1))])
    assert daily_document(daily_root, sequence) == before
    journal, sequence = ledger(weekly_root)
    cutoff = close_of("2026-11-06", 30)
    record(journal, [check(close_of("2026-11-02")), check(cutoff, reason="future", age=-5)])
    before = weekly_document(weekly_root, sequence)
    assert "检查 2，通过 1，拒绝 1" in before[1]
    record(journal, [check(cutoff + timedelta(microseconds=1))])
    assert weekly_document(weekly_root, sequence) == before


def test_unreadable_statistics_do_not_prevent_delivery_of_the_rest_of_the_daily_report(tmp_path):
    strategy, _ = completed(tmp_path)
    record(strategy.journal, [check()])
    with strategy.journal.connection() as db:
        sequence = db.execute("SELECT sequence FROM events WHERE kind='postmarket_review'").fetchone()[0]
        db.execute("ALTER TABLE fund_quote_checks RENAME COLUMN accepted TO invalid_column")
    document = daily_document(tmp_path, sequence)[1]
    assert "统计记录不可读" in document and "下一交易日预测" in document
