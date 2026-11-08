"""Retention for data nothing reads after a while: old headlines, agent debug traces and export archives.

Everything the fund decides, reviews or learns from is kept: the ledger,
orders and fills, decisions, research, reviews, scorecards, factor tests,
shadow decisions, model calls, notices and NAV marks (docs/AUTONOMOUS_PAPER_FUND.md,
"数据保留"). This module only removes:

- headlines older than NEWS_DAYS. Decisions read the last 24 hours, bursts
  the last minutes, and a review trigger keeps the title and source it acted
  on. Material items (filings, macro releases, bursts) are kept MATERIAL_NEWS_DAYS;
- LumiBot's per-call agent traces (debugging JSON under cache/agent_runtime/traces)
  older than TRACE_DAYS. Nothing reads them back;
- monthly events.jsonl archives older than EXPORT_ARCHIVE_MONTHS. The SQLite
  journal is authoritative and still holds every event.

A dry run (the default) reports what would go without changing anything.
Deleted rows leave reusable space in the database file; this never runs VACUUM.
"""
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

NEWS_DAYS = 30
MATERIAL_NEWS_DAYS = 180
TRACE_DAYS = 30
EXPORT_ARCHIVE_MONTHS = 12
_ARCHIVE = re.compile(r"events-(\d{4})-(\d{2})\.jsonl\.gz")
_BATCH = 2000  # Rows per write transaction: the runner and the data desk write this table too.


def _months_before(now, months):
    year, month = now.year, now.month - months
    while month <= 0:
        year, month = year - 1, month + 12
    return year, month


def prune(root, now=None, *, apply=False, news_days=NEWS_DAYS, material_news_days=MATERIAL_NEWS_DAYS,
          trace_days=TRACE_DAYS, archive_months=EXPORT_ARCHIVE_MONTHS):
    """What is (or with apply=True, was) removed from the account directory `root`, as a report dict."""
    root, now = Path(root), (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report = {"applied": bool(apply), "checked_at": now.isoformat(), "news": {}, "traces": {}, "archives": {}}
    book = root / "state.sqlite3"
    if book.is_file():
        # Stored news times are UTC ISO strings, so string order is time order.
        cutoffs = {"headlines": ((now - timedelta(days=news_days)).isoformat(), "material IS NULL"),
                   "material": ((now - timedelta(days=material_news_days)).isoformat(), "material IS NOT NULL")}
        with closing(sqlite3.connect(book, timeout=30)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_news'").fetchone():
                for name, (cutoff, condition) in cutoffs.items():
                    where = f"observed_at < ? AND {condition}"
                    count = db.execute(f"SELECT count(*) FROM fund_news WHERE {where}", (cutoff,)).fetchone()[0]
                    report["news"][name] = {"older_than": cutoff, "rows": count}
                    while apply and count:
                        with db:
                            removed = db.execute(f"DELETE FROM fund_news WHERE rowid IN (SELECT rowid FROM fund_news "
                                                 f"WHERE {where} LIMIT {_BATCH})", (cutoff,)).rowcount
                        count = removed and count - removed
    traces = root / "cache" / "agent_runtime" / "traces"
    oldest = (now - timedelta(days=trace_days)).timestamp()
    files, size = 0, 0
    if traces.is_dir():
        for path in traces.glob("*/*.json"):
            try:
                stat = path.stat()
                if stat.st_mtime >= oldest or not path.is_file():
                    continue
                if apply:
                    path.unlink()
                files, size = files + 1, size + stat.st_size
            except FileNotFoundError:
                continue  # Removed meanwhile.
    report["traces"] = {"older_than_days": trace_days, "files": files, "bytes": size}
    keep_from = _months_before(now, archive_months)
    archives, size = [], 0
    for path in sorted(root.glob("events-*.jsonl.gz")):
        match = _ARCHIVE.fullmatch(path.name)
        if match and (int(match[1]), int(match[2])) < keep_from:
            archives.append(path.name)
            size += path.stat().st_size
            if apply:
                path.unlink()
    report["archives"] = {"older_than_months": archive_months, "files": archives, "bytes": size}
    return report


def lines(report):
    """Chinese summary of a prune report."""
    verb = "已删除" if report["applied"] else "将删除（预览，未改动；加 --apply 执行）"
    news = report["news"]
    def mb(value):
        return f"{value / 1e6:.1f} MB"
    out = [f"数据清理{verb}："]
    if news:
        out.append(f"- 普通资讯 {news['headlines']['rows']} 条（早于 {news['headlines']['older_than'][:10]}），"
                   f"重要资讯 {news['material']['rows']} 条（早于 {news['material']['older_than'][:10]}）")
    else:
        out.append("- 资讯：没有资讯表")
    out.append(f"- 模型调用追踪文件 {report['traces']['files']} 个，{mb(report['traces']['bytes'])}"
               f"（超过 {report['traces']['older_than_days']} 天）")
    out.append(f"- 事件导出归档 {len(report['archives']['files'])} 个，{mb(report['archives']['bytes'])}"
               f"（超过 {report['archives']['older_than_months']} 个月）")
    out.append("账本、订单、成交、决策、研究、复盘、记分卡、因子检验、通知和净值记录不会被删除。")
    return out
