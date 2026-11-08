"""Reports and decision contexts read light fields of every NAV mark, and full bodies only for the marks shown.

The oracle below is the former computation, which parsed and kept every mark
and benchmark. Journals are synthetic histories on a real local test book; no
network, broker or model inference.
"""
import json
import math
import random
import sqlite3
import tracemalloc
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund.evaluation import forward_outcomes, outcome_events
from scripts.autonomous_fund.ledger_audit import audit_connection
from scripts.autonomous_fund.report import report
from scripts.autonomous_fund.review import _events
from scripts.autonomous_fund.risk import instant, trading_day
from .test_autonomous_fund_closed_ledger import traded_book

EASTERN = timezone(timedelta(hours=-5))


def history(root, seed, days=30):
    """Intraday and closing marks, off-hours clones of the last close, broken marks, mixed offsets,
    missing and duplicate benchmarks and review packets, inserted in shuffled order for odd seeds."""
    rng, db = random.Random(seed), sqlite3.connect(root / "state.sqlite3")
    first_at, raw = db.execute("SELECT at,body FROM events WHERE kind='account' ORDER BY sequence").fetchone()
    template = {**json.loads(raw), "positions": {}, "position_average_prices": {}, "receivables": "0"}
    day, rows, equity = datetime(2026, 11, 2, tzinfo=timezone.utc), [], 10000.0

    def mark(at, value, **valuation):
        return {**template, "cash": f"{value:.6f}", "equity": f"{value:.6f}", "as_of": valuation.pop("as_of", at),
                "valuation": {"quotes": {}, **valuation}}

    for n in range(days):
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        for _ in range(rng.randint(0, 5)):
            at = day.replace(hour=14, minute=31) + timedelta(minutes=rng.randint(0, 380))
            at = at.astimezone(EASTERN) if rng.random() < 0.3 else at  # The same instant, another offset.
            equity *= 1 + rng.gauss(0, 0.004)
            body = mark(at.isoformat(), equity, source="local_broker_poll")
            if rng.random() < 0.05:
                body["equity"] = f"{equity + 1:.6f}"  # Inconsistent: the audit excludes it.
            rows.append((at.isoformat(), "account", body))
            if rng.random() < 0.8:
                rows.append((at.isoformat(), "benchmark", {"SPY": f"{600 + rng.random():.4f}", "QQQ": "500"}))
        if rng.random() < 0.6:
            close = day.replace(hour=21)
            observed = close + timedelta(minutes=rng.randint(1, 400))  # Can fall on the next UTC date.
            rows.append((observed.isoformat(), "account", mark(observed.isoformat(), equity, as_of=close.isoformat(),
                         source="local_session_close", session_close=close.isoformat())))
            rows.append((observed.isoformat(), "benchmark", {"SPY": "601", "QQQ": "502"}))
        if rng.random() < 0.3:
            done = day.replace(hour=22)
            rows.append((done.isoformat(), "postmarket_review", {
                "completed_at": done.isoformat(), "version": f"v{n}", "mock": True, "session_date": str(day.date()),
                "market_close": day.replace(hour=21).isoformat(), "models": {}, "review": {"summary": "synthetic"}}))
    close = day.replace(hour=21)
    final = mark((close + timedelta(minutes=45)).isoformat(), 9999, as_of=close.isoformat(),
                 source="local_session_close", session_close=close.isoformat())
    for minutes in (5, 30):  # Older runners' off-hours clones; the recovered close supersedes them.
        at = (close + timedelta(minutes=minutes)).isoformat()
        rows.append((at, "account", {**final, "as_of": at, "valuation": {"source": "local_broker_poll", "quotes": {}}}))
    rows += [((close + timedelta(minutes=45)).isoformat(), "account", final),
             ((close + timedelta(minutes=45)).isoformat(), "benchmark", {"SPY": "610", "QQQ": "505"}),
             (first_at, "benchmark", {"SPY": "600", "QQQ": "500"}), (first_at, "benchmark", {"SPY": "599", "QQQ": "499"})]
    if seed % 2:
        rng.shuffle(rows)  # Insertion order no longer follows time.
    db.executemany("INSERT INTO events(at,kind,body) VALUES(?,?,?)", [(a, k, json.dumps(b)) for a, k, b in rows])
    db.commit()
    db.close()


def load_everything(root, as_of=None):
    """The former report computation over every parsed NAV mark and benchmark."""
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("BEGIN")
        # A historical report is not audited (its status is "not_checked"), so no mark is excluded.
        invalid = set(audit_connection(db).get("invalid_account_sequences", [])) if as_of is None else set()
        events = [(at, kind, json.loads(body)) for sequence, at, kind, body in db.execute(
            "SELECT sequence,at,kind,body FROM events WHERE kind IN ('account','benchmark') ORDER BY sequence")
            if not (kind == "account" and sequence in invalid)]
    if as_of is not None:
        events = [row for row in events if instant(row[0]) <= instant(as_of)]
    accounts, benchmarks = [], {}
    for at, kind, body in events:
        if kind == "account":
            accounts.append({"at": at, "equity": float(body["equity"]), "as_of": body.get("as_of") or at,
                             **({"valuation": body["valuation"]} if "valuation" in body else {})})
        else:
            benchmarks[instant(at)] = body
    accounts.sort(key=lambda row: instant(row["at"]))
    if accounts and (accounts[-1].get("valuation") or {}).get("session_close"):
        closing, cutoff = accounts[-1], instant(accounts[-1]["valuation"]["session_close"])
        accounts = [row for row in accounts if row is closing or not (
            cutoff < instant(row["at"]) <= instant(closing["at"]) and not (row.get("valuation") or {}).get("session_close"))]
    daily = {}
    for row in accounts:
        daily[trading_day(instant(row["as_of"] if (row.get("valuation") or {}).get("session_close") else row["at"]))] = row
    curve = [daily[day] for day in sorted(daily)]
    peak = drawdown = observed_peak = observed = 0
    returns, previous = [], None
    for row in curve:
        peak = max(peak, row["equity"])
        drawdown = max(drawdown, 1 - row["equity"] / peak)
        if previous is not None:
            returns.append(row["equity"] / previous - 1)
        previous = row["equity"]
    for row in accounts:
        observed_peak = max(observed_peak, row["equity"])
        observed = max(observed, 1 - row["equity"] / observed_peak)
    mean = sum(returns) / len(returns)
    variance = sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)
    spy = [benchmarks.get(instant(row["at"]), {}).get("SPY") for row in (accounts[0], accounts[-1])]
    return {"observations": len(curve), "starting_equity": accounts[0]["equity"], "ending_equity": accounts[-1]["equity"],
            "max_observed_drawdown": observed, "max_daily_drawdown": drawdown,
            "daily_sharpe_zero_risk_free": mean / math.sqrt(variance) * math.sqrt(252),
            "spy": float(spy[1]) / float(spy[0]) - 1 if None not in spy else None,
            "latest_at": accounts[-1]["at"], "curve": [row["at"] for row in curve]}


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_the_report_equals_the_computation_over_every_mark(tmp_path, seed, monkeypatch):
    from scripts.autonomous_fund import review
    monkeypatch.setattr(review, "export_reviews", lambda root, reviews: None)  # The packets here are minimal.
    traded_book(tmp_path)
    history(tmp_path, seed)
    for as_of in (None, "2026-11-20T20:00:00+00:00"):
        expected, summary = load_everything(tmp_path, as_of), report(tmp_path, write_files=False, as_of=as_of)
        for key in ("observations", "starting_equity", "ending_equity", "max_observed_drawdown", "max_daily_drawdown",
                    "daily_sharpe_zero_risk_free"):
            assert summary[key] == pytest.approx(expected[key], rel=1e-12, abs=1e-15), key
        assert summary["benchmark_price_returns"]["SPY"] == (
            None if expected["spy"] is None else pytest.approx(expected["spy"]))
        assert summary["latest_account"]["at"] == expected["latest_at"]
    report(tmp_path)
    with (tmp_path / "equity.csv").open(encoding="utf-8") as exported:  # The current (unbounded) report's curve.
        assert [line.split(",", 1)[0] for line in exported.read().splitlines()[1:]] == load_everything(tmp_path)["curve"]


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_outcomes_from_each_days_last_mark_equal_those_from_every_mark(tmp_path, seed):
    _, _, journal, _, _ = traded_book(tmp_path)
    history(tmp_path, seed)
    now = datetime(2027, 1, 15, tzinfo=timezone.utc)
    rows = _events(journal, now, kinds=("postmarket_review", "account", "benchmark"))
    every = forward_outcomes([(row["at"], row["kind"], row["body"]) for row in rows])
    assert any(h["status"] == "observed" for row in every for h in row["horizons"].values())
    assert forward_outcomes(outcome_events(journal, now)) == every
    early = datetime(2026, 11, 25, tzinfo=timezone.utc)  # A clock inside the history.
    rows = _events(journal, early, kinds=("postmarket_review", "account", "benchmark"))
    assert forward_outcomes(outcome_events(journal, early)) == forward_outcomes(
        [(row["at"], row["kind"], row["body"]) for row in rows])


def test_a_report_does_not_hold_every_mark_in_memory(tmp_path):
    traded_book(tmp_path)
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        at, raw = db.execute("SELECT at,body FROM events WHERE kind='account' ORDER BY sequence DESC LIMIT 1").fetchone()
        body = json.dumps({**json.loads(raw), "padding": "x" * 2000})
        db.executemany("INSERT INTO events(at,kind,body) VALUES(?,'account',?)", [(at, body)] * 4000)
    tracemalloc.start()
    try:
        summary = report(tmp_path, write_files=False)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert summary["latest_account"] is not None
    assert peak < 6_000_000  # The marks hold about 11 MB of text; keeping them all needed several times that.
