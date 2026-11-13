"""Economic reports align observations in time and retain explicit gaps.

Neutral synthetic account receipts and native application harnesses only.
"""
import json
from datetime import timedelta

import pytest

from scripts.autonomous_fund.economics import assess, saved_closes
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.performance import PerformanceSettings
from scripts.autonomous_fund.risk import instant


def closing_book(root, *, late=False, embedded=True):
    journal = Journal(root, "synthetic-same-instant-comparison")
    journal.event(instant("2026-11-09T14:00:00Z"), "startup", {
        "cash_management_symbol": None,
        "performance": {"benchmarks": PerformanceSettings().definitions(), "data_costs_usd_per_month": 0}})
    for day, price in (("2026-11-09", 100), ("2026-11-10", 110)):
        closed = instant(day + "T21:00:00Z")
        receipt = instant("2026-11-11T06:00:00Z") if late and day.endswith("10") else closed
        body = {"equity": 6000 + 40 * price, "cash": 6000, "positions": {"SPY": 40},
                "position_prices": {"SPY": price}, "as_of": closed.isoformat(),
                "valuation": {"session_close": closed.isoformat()}}
        if embedded:
            body["benchmark_observations"] = {"SPY": {"price": price, "as_of": closed.isoformat()}}
        journal.event(receipt, "account", body)
        journal.event(receipt, "benchmark", {"SPY": price})
    return journal


def market_return(report):
    return next(row["return"] for row in report["fixed_benchmarks_and_fund"]["benchmarks"].values()
                if row["weights"] == {"SPY": "1"})


def manager_board(journal, now):
    from types import SimpleNamespace
    from scripts.autonomous_fund.strategy import AutonomousFund
    from scripts.autonomous_fund.session_calendar import completed_daily_cutoff
    return AutonomousFund._compute_fund_scoreboard(SimpleNamespace(journal=journal), instant(now),
                                                 completed_daily_cutoff(now))


@pytest.mark.parametrize("consumer", ["economics", "manager"])
def test_postmarket_price_cannot_replace_the_benchmark_at_the_account_close(tmp_path, consumer):
    journal = closing_book(tmp_path)
    journal.event(instant("2026-11-10T22:00:00Z"), "benchmark", {"SPY": 200})
    now = "2026-11-10T23:00:00Z"
    report = assess(tmp_path, now=now) if consumer == "economics" else {"fixed_benchmarks_and_fund": manager_board(journal, now)}
    assert market_return(report) == pytest.approx(.1)
    if consumer == "economics":
        assert report["opening_reference"]["reference_return"] == pytest.approx(.04)


@pytest.mark.parametrize("consumer", ["economics", "manager"])
def test_late_closing_receipts_keep_the_source_session_not_the_receipt_date(tmp_path, consumer):
    journal = closing_book(tmp_path, late=True)
    now = "2026-11-11T07:00:00Z"
    report = assess(tmp_path, now=now) if consumer == "economics" else {"fixed_benchmarks_and_fund": manager_board(journal, now)}
    assert market_return(report) == pytest.approx(.1)
    if consumer == "economics":
        assert report["opening_reference"]["reference_return"] == pytest.approx(.04)


def test_legacy_marks_stop_at_the_valuation_instant_and_keep_partial_symbol_success(tmp_path):
    from scripts.autonomous_fund.evaluation import session_benchmarks
    journal = closing_book(tmp_path, embedded=False)
    closed = instant("2026-11-10T21:00:00Z")
    journal.event(closed - timedelta(minutes=1), "benchmark", {"SPY": 109, "PARK": 100})
    journal.event(closed, "benchmark", {"SPY": 110})
    journal.event(closed + timedelta(seconds=1), "benchmark", {"PARK": 500})
    with journal.connection() as db:
        marks = session_benchmarks(db, [(1, closed, {})], until=closed + timedelta(minutes=1))
    assert marks["2026-11-10"] == {"SPY": 110., "PARK": 100.}


def test_future_embedded_observation_is_not_a_closing_price(tmp_path):
    from scripts.autonomous_fund.evaluation import session_benchmarks
    journal = Journal(tmp_path, "synthetic-future-source")
    closed = instant("2026-11-10T21:00:00Z")
    journal.event(closed, "benchmark", {"SPY": 100, "PARK": 50})
    body = {"benchmark_observations": {"SPY": {"price": 900, "as_of": (closed + timedelta(seconds=1)).isoformat()},
                                        "PARK": {"price": True, "as_of": closed.isoformat()}}}
    with journal.connection() as db:
        marks = session_benchmarks(db, [(1, closed, body)], until=closed + timedelta(minutes=1))
    assert marks["2026-11-10"] == {"SPY": 100., "PARK": 50.}


def test_manager_scoreboard_does_not_bridge_a_missing_trading_session(tmp_path):
    from tests.test_autonomous_fund_quant import harness
    strategy = harness(tmp_path, mode="local")
    for day, equity in (("2026-11-09", 10000), ("2026-11-11", 11000)):
        at = instant(day + "T21:00:00Z")
        strategy.journal.event(at, "account", {"equity": equity})
        strategy.journal.event(at, "benchmark", {"SPY": 100, "SGOV": 100})
    board = strategy._fund_scoreboard(instant("2026-11-12T15:00:00Z"))
    assert board["status"] == "insufficient_sample" and board["sessions"] == 0, board
    assert board["missing_account_intervals"] == 2


def test_report_discloses_gaps_when_other_adjacent_intervals_can_still_be_compared(tmp_path):
    from scripts.autonomous_fund.notifications import fund_scoreboard_line
    journal = Journal(tmp_path, "synthetic-gapped-history")
    for day in ("2026-11-02", "2026-11-03", "2026-11-05", "2026-11-06"):
        at = instant(day + "T21:00:00Z")
        journal.event(at, "account", {"equity": 10000})
        journal.event(at, "benchmark", {"SPY": 100, "SGOV": 100})
    board = manager_board(journal, "2026-11-09T15:00:00Z")
    assert board["sessions"] == 2 and board["missing_account_intervals"] == 2
    assert board["account_return"] == 0
    assert "缺 2 个估值区间" in fund_scoreboard_line(board)
    assert "不是连续全期收益" in fund_scoreboard_line(board)


def test_one_corrupt_cached_bar_keeps_other_bars_without_filling_the_gap(tmp_path):
    import sqlite3
    folder = tmp_path / "cache/daily/2026-11-10/bars"
    folder.mkdir(parents=True)
    (folder / "ALFA.json").write_text(json.dumps({"rows": [
        ["2026-11-05", 100], ["2026-11-06", float("nan")], ["2026-11-09", 102],
        [], ["bad-date", 100], ["2026-11-04", True], ["2026-11-03", 0]]}))
    with sqlite3.connect(":memory:") as db:
        diagnostics = []
        series = saved_closes(tmp_path, db, "2026-11-10T20:00:00Z", diagnostics=diagnostics)
    assert series == {"ALFA": [("2026-11-05", 100.), ("2026-11-09", 102.)]}
    assert diagnostics == [{"symbol": "ALFA", "cache_day": "2026-11-10", "reason": "invalid_daily_rows", "rejected_rows": 5}]


def test_history_rejections_are_visible_in_the_readonly_report(tmp_path):
    from scripts.autonomous_fund.economics import lines
    closing_book(tmp_path)
    folder = tmp_path / "cache/daily/2026-11-10/bars"
    folder.mkdir(parents=True)
    (folder / "ALFA.json").write_text(json.dumps({"rows": [["2026-11-09", 100], ["2026-11-10", None]]}))
    (folder / "BETA.json").write_text("null")
    report = assess(tmp_path, now="2026-11-10T23:00:00Z")
    text = "\n".join(lines(report))
    assert "隔离 1 行" in text and "不可读文件 1 个" in text and "缺口不补" in text
