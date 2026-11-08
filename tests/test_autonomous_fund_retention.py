"""Retention removes only what nothing reads any more; the ledger, decisions and reviews are never touched.

Synthetic journal, headlines, traces and exports; no network, broker or model inference.
"""
import gzip
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.news import NewsSettings, NewsWatch
from scripts.autonomous_fund.retention import lines, prune

NOW = datetime(2026, 11, 8, 23, tzinfo=timezone.utc)


def book(tmp_path):
    journal = Journal(tmp_path, "synthetic-retention-account")
    NewsWatch(journal, NewsSettings(enabled=True))
    with journal.connection() as db:
        for days, material, ident in ((1, None, "fresh"), (40, None, "old"), (40, "sec_filing", "material-recent"),
                                      (200, "macro_release", "material-old"), (200, None, "older")):
            db.execute("INSERT INTO fund_news(id,observed_at,source,kind,symbols,title,url,material) VALUES(?,?,?,?,?,?,?,?)",
                       (ident, (NOW - timedelta(days=days)).isoformat(), "yahoo", "headline", '["SPY"]', ident,
                        "https://example.com/" + ident, material))
    for kind in ("decision_validated", "postmarket_review", "fill", "account", "notification"):
        journal.event(NOW - timedelta(days=400), kind, {"kept": True})
    return journal


def traces(tmp_path):
    folder = tmp_path / "cache" / "agent_runtime" / "traces" / "manager"
    folder.mkdir(parents=True)
    old, new = folder / "old.json", folder / "new.json"
    for path in (old, new):
        path.write_text("{}" * 500)
    stamp = (NOW - timedelta(days=45)).timestamp()
    os.utime(old, (stamp, stamp))
    os.utime(new, (NOW.timestamp(), NOW.timestamp()))
    (tmp_path / "cache" / "agent_runtime" / "manager_agent_memory_memory_events.parquet").write_text("memory")
    return old, new


def news_ids(journal):
    with journal.connection() as db:
        return {row[0] for row in db.execute("SELECT id FROM fund_news")}


def test_a_preview_changes_nothing_and_says_what_would_go(tmp_path):
    journal = book(tmp_path)
    old, new = traces(tmp_path)
    report = prune(tmp_path, NOW)
    assert report["news"]["headlines"]["rows"] == 2 and report["news"]["material"]["rows"] == 1
    assert report["traces"]["files"] == 1 and old.exists() and new.exists()
    assert len(news_ids(journal)) == 5
    text = "\n".join(lines(report))
    assert "预览" in text and "--apply" in text and "不会被删除" in text


def test_applying_keeps_recent_and_material_news_and_never_touches_the_journal(tmp_path):
    journal = book(tmp_path)
    old, new = traces(tmp_path)
    with journal.connection() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM events ORDER BY sequence")]
    report = prune(tmp_path, NOW, apply=True)
    assert news_ids(journal) == {"fresh", "material-recent"}  # 30 days of headlines; 180 of material items.
    assert not old.exists() and new.exists() and report["traces"]["bytes"] == 1000
    assert (tmp_path / "cache" / "agent_runtime" / "manager_agent_memory_memory_events.parquet").exists()
    with journal.connection() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM events ORDER BY sequence")] == before
    assert "已删除" in "\n".join(lines(report))


def test_the_export_rolls_over_monthly_without_losing_or_repeating_an_event(tmp_path):
    journal = Journal(tmp_path, "synthetic-export-account")
    november, december = datetime(2026, 11, 20, tzinfo=timezone.utc), datetime(2026, 12, 2, tzinfo=timezone.utc)
    for i in range(3):
        journal.event(november, "notification", {"kind": "synthetic", "i": i})
    assert len(journal.export(november)) == 3
    journal.event(december, "notification", {"kind": "synthetic", "i": 3})
    assert len(journal.export(december)) == 1  # Still incremental across the rollover.
    archived = gzip.decompress((tmp_path / "events-2026-11.jsonl.gz").read_bytes()).decode().splitlines()
    current = (tmp_path / "events.jsonl").read_text().splitlines()
    exported = [json.loads(line)["body"]["i"] for line in archived + current]
    assert exported == [0, 1, 2, 3]
    journal.event(december, "notification", {"kind": "synthetic", "i": 4})
    assert len(journal.export(december)) == 1 and len((tmp_path / "events.jsonl").read_text().splitlines()) == 2
    assert prune(tmp_path, datetime(2027, 6, 1, tzinfo=timezone.utc))["archives"]["files"] == []  # Within a year.
    report = prune(tmp_path, datetime(2028, 1, 2, tzinfo=timezone.utc), apply=True)
    assert report["archives"]["files"] == ["events-2026-11.jsonl.gz"] and not (tmp_path / "events-2026-11.jsonl.gz").exists()
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='notification'").fetchone()[0] == 5


def test_an_account_without_news_or_traces_prunes_nothing(tmp_path):
    Journal(tmp_path, "synthetic-empty-account")
    report = prune(tmp_path, NOW, apply=True)
    assert report["news"] == {} and report["traces"]["files"] == 0 and report["archives"]["files"] == []
