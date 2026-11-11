"""The data-cost ledger reads each New York session's first benchmark mark without parsing every mark.

Benchmarks are marked about every 30 seconds, so a year holds about 200,000 marks and the ledger is read
before every model call. New York midnight is always a whole UTC hour, so the first mark of each UTC hour
is enough to find the first mark of each New York day. Synthetic marks only.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import instant
from scripts.autonomous_fund.scoreboard import cost_ledger, session_data_charges

NY = ZoneInfo("America/New_York")


def marks():
    """Sessions around the DST change, with pre-market, regular and after-hours marks (past UTC midnight)."""
    out = []
    for day in (datetime(2026, 10, 30), datetime(2026, 11, 2), datetime(2026, 11, 3)):  # DST ends Nov 1.
        for hour, minute in ((7, 5), (9, 30), (9, 30), (12, 0), (15, 59), (19, 45), (21, 10)):
            local = day.replace(hour=hour, minute=minute, tzinfo=NY)
            out.append(local)
            out.append(local + timedelta(seconds=30))
    return out


@pytest.mark.parametrize("utc_strings", [False, True])
def test_first_marks_per_session_match_reading_every_mark(tmp_path, utc_strings):
    journal = Journal(tmp_path, f"ledger-{utc_strings}")
    observed = marks()
    with journal.connection() as db:
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)",
                   (str(observed[0] - timedelta(days=1)), "startup", '{"performance": {"data_costs_usd_per_month": "21"}}'))
        for moment in observed:
            text = str(moment.astimezone(timezone.utc)) if utc_strings else str(moment)
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (text, "benchmark", '{"SPY": "1"}'))
    until = (observed[-1] + timedelta(hours=1)).isoformat()
    with journal.connection() as db:
        _, charges = cost_ledger(db, until)
    rates = [(instant(observed[0] - timedelta(days=1)), 0, 21.0)]
    assert charges == session_data_charges(iter(observed), rates)  # Same as reading every mark.
    assert [moment.astimezone(NY).date().isoformat() for moment, _ in charges] == ["2026-10-30", "2026-11-02", "2026-11-03"]
    assert all(moment.astimezone(NY).hour == 7 and moment.astimezone(NY).minute == 5 for moment, _ in charges)
    assert all(cost == pytest.approx(1.0) for _, cost in charges)  # $21 a month over 21 sessions.


def test_marks_after_the_cutoff_are_never_charged(tmp_path):
    journal = Journal(tmp_path, "ledger-cutoff")
    observed = marks()
    with journal.connection() as db:
        for moment in observed:
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (str(moment), "benchmark", "{}"))
    cutoff = datetime(2026, 11, 2, 9, 0, tzinfo=NY)  # Before that session's 09:30 mark, after its 07:05 mark.
    with journal.connection() as db:
        _, charges = cost_ledger(db, cutoff.isoformat())
    assert [moment.astimezone(NY).date().isoformat() for moment, _ in charges] == ["2026-10-30", "2026-11-02"]
