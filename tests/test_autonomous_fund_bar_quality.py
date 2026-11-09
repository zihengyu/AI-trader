"""Cross-check of the primary daily bars against a second source: drop disputes, withhold contradictions.

Synthetic bars, fake HTTP clients and neutral symbols only; no network, broker or model inference.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.autonomous_fund.bar_quality import TOLERANCE, apply, check, compare
from scripts.autonomous_fund.datastore import DailyStore
from scripts.autonomous_fund.risk import RiskError

DAYS = [d.date() for d in pd.bdate_range(end="2026-11-06", periods=60)]


def rows(closes, days=DAYS):
    return [(day, float(close), 1e6, float(close), float(close) * 1.01, float(close) * .99)
            for day, close in zip(days, closes)]


def walk(seed=1, count=60):
    return 100 * np.cumprod(1 + np.random.default_rng(seed).normal(0, .01, count))


def test_small_feed_differences_agree_and_a_bad_print_is_dropped_never_replaced():
    closes = walk()
    iex = closes * (1 + np.random.default_rng(2).normal(0, .001, len(closes)))  # Last IEX trade vs official close.
    assert compare(rows(closes), rows(iex), "iex")["status"] == "agree"
    bad = closes.copy()
    bad[30] *= 1.25  # One wrong close in the primary source.
    verdict = compare(rows(bad), rows(closes), "iex")
    assert verdict["status"] == "disputed_sessions" and verdict["disputed"] == [DAYS[30].isoformat()]
    assert verdict["largest"][0]["difference"] == pytest.approx(.25)
    kept = apply(rows(bad), verdict)
    assert len(kept) == 59 and DAYS[30] not in [r[0] for r in kept]
    assert all(r in rows(bad) for r in kept)  # Nothing borrowed from the other source.


def test_a_split_applied_by_one_source_only_or_a_disputed_latest_close_withholds_the_symbol():
    closes = walk(3)
    unadjusted = closes.copy()
    unadjusted[:40] *= 2  # A 2:1 split the primary has not applied to older sessions.
    split = compare(rows(unadjusted), rows(closes))
    assert split["status"] == "unreliable" and len(split["disputed"]) == 40 and apply(rows(unadjusted), split) == []
    latest = closes.copy()
    latest[-1] *= 1.05
    verdict = compare(rows(latest), rows(closes))
    assert verdict["status"] == "unreliable" and verdict["disputed"] == [DAYS[-1].isoformat()]


def test_too_little_overlap_is_not_a_verdict_and_missing_sessions_are_only_reported():
    closes = walk(4)
    assert compare(rows(closes), rows(closes[-10:], DAYS[-10:]))["status"] == "insufficient_overlap"
    gappy = [row for i, row in enumerate(rows(closes)) if i not in (10, 11)]
    verdict = compare(gappy, rows(closes))
    assert verdict["status"] == "agree" and verdict["missing_count"] == 2
    assert apply(gappy, verdict) == gappy  # Reported, not filled in.
    assert compare([], rows(closes))["status"] == "no_primary" and TOLERANCE == .02


class Reference:
    feed = "iex"

    def __init__(self, closes, error=None):
        self.closes, self.error, self.calls = closes, error, 0

    def bars(self, symbol, length, today):
        self.calls += 1
        if self.error:
            raise self.error
        return rows(self.closes)


def test_a_verdict_is_cached_per_primary_rows_and_a_missing_symbol_is_recorded_once(tmp_path):
    store, closes = DailyStore(tmp_path), walk(5)
    reference = Reference(closes)
    first = check(store, "2026-11-06", "ALFA", rows(closes), "2026-11-06", reference)
    again = check(store, "2026-11-06", "ALFA", rows(closes), "2026-11-06", reference)
    assert first["status"] == again["status"] == "agree" and reference.calls == 1
    changed = closes.copy()
    changed[5] *= 1.1
    assert check(store, "2026-11-06", "ALFA", rows(changed), "2026-11-06", reference)["status"] == "disputed_sessions"
    assert reference.calls == 2  # New primary rows: checked again.
    missing = Reference(closes, RiskError("Alpaca returned no completed daily bars for GONE"))
    assert check(store, "2026-11-06", "GONE", rows(closes), "2026-11-06", missing)["status"] == "no_reference"
    check(store, "2026-11-06", "GONE", rows(closes), "2026-11-06", missing)
    assert missing.calls == 1
    with pytest.raises(ConnectionError):  # A service failure propagates: the caller pauses.
        check(store, "2026-11-06", "BETA", rows(closes), "2026-11-06", Reference(closes, ConnectionError()))


NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)  # Monday 10:00 New York: completed through Friday.


def runner(tmp_path, primary, reference):
    from tests.test_autonomous_fund_quant import harness
    strategy = harness(tmp_path, mode="local")
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York") + pd.Timedelta(hours=16) for d in DAYS])
    frame = pd.DataFrame({"open": primary, "high": primary, "low": primary, "close": primary, "volume": 1e6},
                         index=index)
    strategy.get_historical_prices = lambda asset, length, timestep="day": SimpleNamespace(df=frame)
    strategy.history_fallback = reference
    strategy.daily_store = DailyStore(tmp_path / "store")
    return strategy


def events(strategy, kind):
    return [row["body"] for row in strategy.journal.all_events() if row["kind"] == kind]


def test_the_runner_drops_disputed_sessions_withholds_contradictions_and_journals_them(tmp_path):
    closes = walk(6)
    bad = closes.copy()
    bad[20] *= 1.3
    strategy = runner(tmp_path / "a", bad, Reference(closes))
    got, error = strategy._daily_bars("ALFA", NOW)
    assert error is None and len(got) == 59 and DAYS[20].isoformat() not in [str(r[0]) for r in got]
    [event] = events(strategy, "bar_quality")
    assert event["status"] == "disputed_sessions" and event["disputed_count"] == 1 and event["feed"] == "iex"
    split = closes.copy()
    split[:45] *= 2
    withheld = runner(tmp_path / "b", split, Reference(closes))
    assert withheld._daily_bars("ALFA", NOW) == ([], "bar_quality_unreliable")
    assert events(withheld, "risk_history_unavailable")[0]["error"] == "bar_quality_unreliable"
    clean = runner(tmp_path / "c", closes, Reference(closes))
    assert len(clean._daily_bars("ALFA", NOW)[0]) == 60 and events(clean, "bar_quality") == []


def test_a_second_source_outage_leaves_bars_unchecked_pauses_and_is_said_once(tmp_path):
    closes = walk(7)
    reference = Reference(closes, ConnectionError("down"))
    strategy = runner(tmp_path, closes, reference)
    assert len(strategy._daily_bars("ALFA", NOW)[0]) == 60
    strategy._bars = None
    strategy._daily_bars("BETA", NOW)
    assert reference.calls == 1  # Paused: the next symbol does not wait on the dead service.
    assert [e["error"] for e in events(strategy, "bar_quality_unavailable")] == ["ConnectionError"]


def test_the_post_close_notice_lists_withheld_and_disputed_symbols_once_a_day(tmp_path):
    from scripts.autonomous_fund.notifications import readable
    closes = walk(8)
    bad, split = closes.copy(), closes.copy()
    bad[10] *= 1.2
    split[:45] *= 2
    strategy = runner(tmp_path, bad, Reference(closes))
    strategy._daily_bars("ALFA", NOW)
    strategy._bars = None
    strategy.get_historical_prices = lambda asset, length, timestep="day": SimpleNamespace(df=pd.DataFrame(
        {"open": split, "high": split, "low": split, "close": split, "volume": 1e6},
        index=pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York") + pd.Timedelta(hours=16) for d in DAYS])))
    strategy._daily_bars("BETA", NOW)
    later = NOW + timedelta(hours=6, minutes=30)
    details = strategy._report_bar_quality(later)
    assert details["withheld"] == ["BETA"] and details["disputed"] == {"ALFA": 1}
    assert strategy._report_bar_quality(later + timedelta(minutes=5)) is None  # Once per day.
    assert [k for k, _ in strategy.notices].count("bar_quality_issues") == 1
    text = readable("bar_quality_issues", details, 9)
    assert "今天不使用这些证券的历史数据" in text and "BETA" in text and "ALFA 1 天" in text
    assert "Yahoo" in text and "Alpaca" in text and "不替换、不插值" in text


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ConnectionError(self.status_code)

    def json(self):
        return self.body


def test_the_data_desk_checks_after_downloading_and_its_snapshot_uses_the_verdicts(tmp_path):
    from scripts.autonomous_fund.datadesk import DataDesk
    from tests.test_autonomous_fund_datadesk import DAY, NOW as DESK_NOW, config
    from tests.test_autonomous_fund_prefetch import frame
    primary = frame()
    source = SimpleNamespace(get_historical_prices=lambda asset, length, timestep="day": SimpleNamespace(df=primary))
    served = primary[primary.index.date < date(2026, 11, 3)]

    def alpaca(url, headers=None, timeout=None, params=None):
        bars = [{"t": stamp.tz_convert("UTC").isoformat(), "c": close * (1.3 if url.endswith("/QQQ/bars") else 1),
                 "v": 1000, "o": close, "h": close, "l": close} for stamp, close in served["close"].items()]
        return Response({"bars": bars, "next_page_token": None})
    environ = {"ALPACA_API_KEY": "synthetic-key", "ALPACA_API_SECRET": "synthetic-secret"}
    desk = DataDesk(tmp_path, config(), "local", source=source, clock=lambda: DESK_NOW, environ=environ,
                    alpaca_get=alpaca)
    result = desk._job_bars(DESK_NOW)
    assert result["quality"]["statuses"].get("unreliable") == 1 and result["quality"]["statuses"]["agree"] >= 1
    store = DailyStore(tmp_path / "cache" / "daily")
    assert store.get(DAY, "quality/QQQ")["status"] == "unreliable" and store.get(DAY, "quality/SPY")["status"] == "agree"
    desk._job_snapshot(DESK_NOW)
    snapshot = store.get(DAY, "market_snapshot")
    assert "QQQ" in snapshot["no_history"] and "QQQ" not in snapshot["symbols"]  # Withheld, not borrowed.
    assert "SPY" in snapshot["symbols"]
    assert desk._previous_close("QQQ", DAY) is None and desk._previous_close("SPY", DAY) is not None
    assert DataDesk(tmp_path / "x", config(), "local", source=source, environ={})._reference() is None
    assert DataDesk(tmp_path / "y", config(), "paper", source=source, environ=environ)._reference() is None
