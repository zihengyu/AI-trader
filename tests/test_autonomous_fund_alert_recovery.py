"""Quote fast paths and durable alert recovery with synthetic clocks and feeds only."""
from datetime import timedelta

import pytest

from lumibot.entities import Asset
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.notifications import readable as notification_text
from scripts.autonomous_fund.risk import MarketDataUnavailable, ObservedPrice, number, observed_quote
from scripts.autonomous_fund.status import alert_health_lines
from scripts.autonomous_fund.strategy import AutonomousFund
from .test_autonomous_fund_autonomy import accept, watching
from .test_autonomous_fund_decision_correction import events
from .test_autonomous_fund_freshness_recovery import inert_tape
from .test_autonomous_fund_local import Clock
from .test_autonomous_fund_quant import LIMITS, NOW, account
from .test_autonomous_fund_realtime import frame


def test_current_minute_stream_quote_does_not_wait_for_history_or_manufacture_a_bar():
    clock, tape, downloads = Clock(), inert_tape(), []
    tape.watch(["ALFA"])
    stamp = clock() - timedelta(seconds=1)
    tape.handle(frame("ALFA", 105, stamp))

    def download(*args):
        downloads.append(args)
        clock.value += timedelta(seconds=90)
        return clock.fetch(*args)

    source = YahooRealtimeData(now=clock, fetch=download, tape=tape)
    quote = source.get_quote(Asset("ALFA"))
    assert downloads == [] and source.diagnostics()["requests"] == 0
    assert observed_quote(quote, clock(), LIMITS).as_of == stamp
    assert quote.volume is None and not source._cache
    # Independent matching must still fetch actual completed bars.
    source.execution_bars(Asset("ALFA"))
    assert len(downloads) == 1 and source.diagnostics()["requests"] == 1


@pytest.mark.parametrize("offset", [-1, 10, 61])
def test_future_stale_or_prior_minute_tick_does_not_bypass_bar_selection(offset):
    clock, tape = Clock(), inert_tape()
    tape.watch(["BETA"])
    tape.handle(frame("BETA", 105, clock() - timedelta(seconds=offset)))
    source = YahooRealtimeData(now=clock, fetch=clock.fetch, tape=tape)
    quote = source.get_quote(Asset("BETA"))
    assert source.diagnostics()["requests"] == 1
    assert quote.raw_data["price_kind"] == "completed_minute_close_with_spread_proxy"
    assert observed_quote(quote, clock(), LIMITS).as_of == clock().replace(second=0, microsecond=0)


@pytest.mark.parametrize("stream_enabled", [False, True])
def test_failed_quote_retains_its_own_stream_and_chart_clocks(stream_enabled):
    clock = Clock()
    chart_at = clock() - timedelta(minutes=3)

    def lagging(*args):
        raw = clock.fetch(*args)
        raw = raw.iloc[:-2].copy()  # Last completed bar now exceeds the independent 60-second ceiling.
        raw.attrs["yahoo_last_trade"] = {"price": 101, "as_of": chart_at}
        return raw

    source = YahooRealtimeData(now=clock, fetch=lagging, tape=inert_tape() if stream_enabled else None)
    quote = source.get_quote(Asset("DELTA"))
    # An unrelated later read cannot overwrite the diagnostic evidence kept on this quote.
    source._quote_observations.clear()
    with pytest.raises(MarketDataUnavailable) as failure:
        observed_quote(quote, clock(), LIMITS)
    detail = failure.value.diagnostic
    assert detail["reason"] == "stale" and detail["age_seconds"] == 65
    assert detail["stream_enabled"] is stream_enabled and detail["stream_as_of"] is None
    assert detail["chart_as_of"] == chart_at.isoformat() and detail["chart_age_seconds"] == 180


def alert_strategy(tmp_path):
    strategy = watching(tmp_path, {})
    strategy._notify_fund = lambda kind, detail: AutonomousFund._notify_fund(strategy, kind, detail)
    strategy.log_message = lambda *a, **kw: None
    strategy._notices = {}
    return strategy


def fail_quote(strategy, age=81):
    strategy._alert_price = lambda symbol: ObservedPrice(105, NOW - timedelta(seconds=age)).validate(NOW, LIMITS)


def notices(strategy, kind):
    return [item["details"] for item in events(strategy, "notification") if item["kind"] == kind]


def test_dedupe_does_not_hide_failed_check_count_and_valid_non_crossing_mark_recovers(tmp_path):
    strategy = alert_strategy(tmp_path)
    accept(strategy, [], [{"symbol": "BETA", "above": 110, "reason": "Synthetic level"}])
    fail_quote(strategy)
    strategy._watch(account(), NOW)
    fail_quote(strategy, 87)
    strategy._watch(account(), NOW)
    saved = strategy.journal.get_meta("price_alert_health")["BETA"]
    assert saved["failed_checks"] == 2 and saved["diagnostic"]["age_seconds"] == 87
    assert len(notices(strategy, "price_alert_unavailable")) == 1
    with strategy.journal.connection() as db:
        text = "\n".join(alert_health_lines(db, NOW))
    assert "2 次失败检查" in text and "87.0 秒" in text and "不代表期间持续失败" in text

    strategy._alert_price = lambda symbol: ObservedPrice(number(105), NOW)
    strategy._watch(account(), NOW)
    strategy._watch(account(), NOW)
    [recovery] = notices(strategy, "price_alert_recovered")
    assert recovery["failed_checks"] == 2 and recovery["source_as_of"] == NOW.isoformat()
    assert not strategy.journal.get_meta("price_alert_health")
    assert not events(strategy, "price_alert") and len(strategy.execution.alerts()) == 1
    assert not strategy.journal.pending()  # Observation does not submit orders.
    with strategy.journal.connection() as db:
        assert alert_health_lines(db, NOW) == []

    # A distinct outage must be disclosed even inside the previous notice's 30-minute window.
    fail_quote(strategy)
    strategy._watch(account(), NOW)
    assert len(notices(strategy, "price_alert_unavailable")) == 2
    assert strategy.journal.get_meta("price_alert_health")["BETA"]["failed_checks"] == 1


def test_recovery_survives_runner_restart_and_still_triggers_a_real_crossing(tmp_path):
    original = alert_strategy(tmp_path)
    accept(original, [], [{"symbol": "CHAR", "above": 104, "reason": "Synthetic level"}])
    fail_quote(original)
    original._watch(account(), NOW)
    restarted = alert_strategy(tmp_path)
    restarted._alert_price = lambda symbol: ObservedPrice(number(105), NOW)
    restarted._watch(account(), NOW)
    assert len(notices(restarted, "price_alert_recovered")) == 1
    assert len(events(restarted, "price_alert")) == 1
    assert not restarted.execution.alerts() and not restarted.journal.pending()


def test_real_local_quote_adapter_recovers_the_watch_without_waiting_for_lagging_bars(tmp_path):
    clock, tape = Clock(), inert_tape()
    clock.value = NOW + timedelta(seconds=5)

    def lagging(*args):
        return clock.fetch(*args).iloc[:-2].copy()

    source = YahooRealtimeData(now=clock, fetch=lagging, tape=tape)
    strategy = alert_strategy(tmp_path)
    strategy.mode, strategy.get_datetime, strategy.get_quote = "local", clock, source.get_quote
    strategy._alert_price = lambda symbol: AutonomousFund._alert_price(strategy, symbol)
    accept(strategy, [], [{"symbol": "DELTA", "above": 104, "reason": "Synthetic level"}])
    strategy._watch(account(), clock())
    assert notices(strategy, "price_alert_unavailable")[0]["price_kind"] == "completed_minute_close_with_spread_proxy"
    assert strategy.journal.get_meta("price_alert_health")["DELTA"]["diagnostic"]["stream_as_of"] is None
    downloads = source.diagnostics()["requests"]
    stamp = clock() - timedelta(seconds=1)
    tape.handle(frame("DELTA", 105, stamp))
    strategy._watch(account(), clock())
    assert source.diagnostics()["requests"] == downloads
    assert len(notices(strategy, "price_alert_recovered")) == 1
    assert events(strategy, "price_alert")[0]["source_as_of"] == str(stamp)
    assert not strategy.journal.pending()


def test_removing_watch_is_not_reported_as_quote_recovery(tmp_path):
    strategy = alert_strategy(tmp_path)
    accept(strategy, [], [{"symbol": "CHAR", "above": 104, "reason": "Synthetic level"}])
    fail_quote(strategy)
    strategy._watch(account(), NOW)
    accept(strategy, [])
    with strategy.journal.connection() as db:
        assert alert_health_lines(db, NOW) == []  # Read-only status ignores removed watches immediately.
    strategy._watch(account(), NOW)
    assert not strategy.journal.get_meta("price_alert_health")
    assert not notices(strategy, "price_alert_recovered")
    accept(strategy, [], [{"symbol": "CHAR", "above": 104, "reason": "New synthetic watch"}])
    strategy._watch(account(), NOW)
    assert len(notices(strategy, "price_alert_unavailable")) == 2


def test_recovery_clears_dedupe_for_every_cause_in_the_previous_outage(tmp_path):
    strategy = alert_strategy(tmp_path)
    accept(strategy, [], [{"symbol": "BETA", "above": 110, "reason": "Synthetic level"}])
    for age in (81, -1):
        fail_quote(strategy, age)
        strategy._watch(account(), NOW)
    assert len(notices(strategy, "price_alert_unavailable")) == 2
    strategy._alert_price = lambda symbol: ObservedPrice(number(105), NOW)
    strategy._watch(account(), NOW)
    fail_quote(strategy)
    strategy._watch(account(), NOW)
    assert len(notices(strategy, "price_alert_unavailable")) == 3


def test_failed_health_storage_cannot_prevent_an_alert_crossing(tmp_path, monkeypatch):
    strategy = alert_strategy(tmp_path)
    accept(strategy, [], [{"symbol": "CHAR", "above": 104, "reason": "Synthetic level"}])
    original = strategy.journal.get_meta

    def failed(key, *args):
        if key == "price_alert_health":
            raise OSError("synthetic metadata failure")
        return original(key, *args)

    monkeypatch.setattr(strategy.journal, "get_meta", failed)
    strategy._alert_price = lambda symbol: ObservedPrice(number(105), NOW)
    strategy._watch(account(), NOW)
    assert len(events(strategy, "price_alert")) == 1
    assert not notices(strategy, "price_alert_recovered")


def test_failed_recovery_transaction_retains_pending_state_for_retry(tmp_path):
    strategy = alert_strategy(tmp_path)
    accept(strategy, [], [{"symbol": "CHAR", "above": 110, "reason": "Synthetic level"}])
    fail_quote(strategy)
    strategy._watch(account(), NOW)
    with strategy.journal.connection() as db:
        db.execute("""CREATE TRIGGER recovery_failure BEFORE INSERT ON events
            WHEN NEW.kind='notification' AND json_extract(NEW.body,'$.kind')='price_alert_recovered'
            BEGIN SELECT RAISE(ABORT,'synthetic recovery failure'); END""")
    strategy._alert_price = lambda symbol: ObservedPrice(number(105), NOW)
    strategy._watch(account(), NOW)
    assert strategy.journal.get_meta("price_alert_health")["CHAR"]["failed_checks"] == 1
    assert not notices(strategy, "price_alert_recovered") and not strategy.journal.pending()
    with strategy.journal.connection() as db:
        db.execute("DROP TRIGGER recovery_failure")
    strategy._watch(account(), NOW)
    assert len(notices(strategy, "price_alert_recovered")) == 1
    assert not strategy.journal.get_meta("price_alert_health")


@pytest.mark.parametrize("reason,phrase", [("stale", "超过 60 秒"), ("future", "源时间晚于"),
                                            ("nonpositive", "零或负数"), ("invalid_price", "有效数值")])
def test_notice_identifies_the_actual_rejection_cause(reason, phrase):
    text = notification_text("price_alert_unavailable", {"symbol": "CHAR", "reason": reason,
        "max_age_seconds": 60, "error": "Stale, future or nonpositive observation"})
    assert phrase in text


def test_recovery_notice_and_error_selection_disclose_the_scope():
    from scripts.autonomous_fund.log_errors import error_records
    text = notification_text("price_alert_recovered", {"symbol": "CHAR", "failed_checks": 2,
                                                       "source_as_of": NOW.isoformat()})
    assert "恢复检查" in text and "2 次" in text and "不会补认" in text
    lines = error_records([{"MESSAGE": "INFO | FUND price_alert_recovered: synthetic"}])
    assert len(lines) == 1
