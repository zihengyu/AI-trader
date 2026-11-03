"""Real-time information watch: headlines, filings and macro releases for the AI.

A background thread polls free public sources (the data desk process does it
instead while it runs; this thread then stands by), stores each item once with its
source publication time and observation time, and flags a few deterministic
"material" events that should bring the next AI review forward. It never
trades and never interprets content: the AI reads the items as UNTRUSTED data
alongside prices and research. Sources fail independently with backoff.

Operator-configured URLs and fixed source hosts only; the AI can contribute
search phrases (news_watch) but never a URL.
"""
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlsplit
from xml.etree import ElementTree

import requests

from .journal import encode
from .risk import RiskError, instant

SOURCES = {"yahoo", "google", "fed", "sec"}
FED_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
GOOGLE_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_FILINGS = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}&type=8-K"
               "&dateb=&owner=include&count=10&output=atom")
ATOM = "{http://www.w3.org/2005/Atom}"


class NewsSettings:
    FIELDS = {"enabled", "poll_seconds", "sources", "rss_feeds", "queries", "burst_threshold", "burst_minutes",
              "max_items_per_context"}

    def __init__(self, enabled=False, poll_seconds=300, sources=("yahoo", "google", "fed", "sec"), rss_feeds=(),
                 queries=(), burst_threshold=3, burst_minutes=60, max_items_per_context=30):
        if type(enabled) is not bool:
            raise RiskError("news.enabled must be true or false")
        if type(poll_seconds) is not int or not 60 <= poll_seconds <= 3600:
            raise RiskError("news.poll_seconds must be 60..3600")
        if not set(sources) <= SOURCES:
            raise RiskError("news.sources accepts: " + ", ".join(sorted(SOURCES)))
        feeds = []
        for feed in rss_feeds:
            if not isinstance(feed, dict) or set(feed) != {"name", "url"} or urlsplit(str(feed["url"])).scheme != "https":
                raise RiskError("news.rss_feeds entries need a name and an https url")
            feeds.append((str(feed["name"])[:60], str(feed["url"])))
        if len(feeds) > 20 or len(queries) > 10 or any(not isinstance(q, str) or not 2 <= len(q) <= 100 for q in queries):
            raise RiskError("news allows up to 20 feeds and 10 queries of 2..100 characters")
        for key, value, low in (("burst_threshold", burst_threshold, 2), ("burst_minutes", burst_minutes, 10),
                                ("max_items_per_context", max_items_per_context, 5)):
            if type(value) is not int or not low <= value <= 500:
                raise RiskError(f"news.{key} is out of range")
        self.enabled, self.poll_seconds, self.sources = enabled, poll_seconds, tuple(sources)
        self.rss_feeds, self.queries = tuple(feeds), tuple(queries)
        self.burst_threshold, self.burst_minutes = burst_threshold, burst_minutes
        self.max_items_per_context = max_items_per_context

    @classmethod
    def from_config(cls, section):
        section = dict(section or {})
        unknown = set(section) - cls.FIELDS
        if unknown:
            raise RiskError("Unknown news settings: " + ", ".join(sorted(unknown)))
        return cls(**section)


def _when(text):
    if not text:
        return None
    try:
        value = parsedate_to_datetime(text) if "," in text else datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else None


def parse_feed(text, source, kind, symbols=(), query=None):
    """RSS 2.0 or Atom -> items. Refuses DTDs/entities (no expansion attacks)."""
    head = text[:4000].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        raise RiskError("Feed declares a DTD or entities; refused")
    root = ElementTree.fromstring(text)
    rows = []
    for node in root.iter("item"):
        rows.append((node.findtext("title"), node.findtext("link"), node.findtext("pubDate")))
    for entry in root.iter(ATOM + "entry"):
        link = entry.find(ATOM + "link")
        rows.append((entry.findtext(ATOM + "title"), link.get("href") if link is not None else None,
                     entry.findtext(ATOM + "updated") or entry.findtext(ATOM + "published")))
    return [item(source, kind, title, link, _when(published), symbols, query) for title, link, published in rows
            if title and link]


def item(source, kind, title, url, published, symbols=(), query=None):
    title = " ".join(str(title).split())[:300]
    key = hashlib.sha256(f"{source}|{url}|{title}".encode()).hexdigest()[:32]
    return {"id": key, "source": source, "kind": kind, "symbols": sorted(symbols), "query": query,
            "title": title, "url": str(url)[:500], "published_at": published.isoformat() if published else None}


def yahoo_items(symbol, fetch=None):
    """Per-ticker Yahoo Finance headlines via yfinance (unofficial)."""
    if fetch is None:
        import yfinance as yf
        fetch = lambda s: yf.Ticker(s.replace(".", "-")).get_news(count=10)  # noqa: E731
    rows = []
    for raw in fetch(symbol) or []:
        body = raw.get("content") if isinstance(raw.get("content"), dict) else raw
        url = ((body.get("canonicalUrl") or {}).get("url") or (body.get("clickThroughUrl") or {}).get("url")
               or body.get("link"))
        published = _when(body.get("pubDate") or body.get("displayTime"))
        if published is None and body.get("providerPublishTime"):
            published = datetime.fromtimestamp(int(body["providerPublishTime"]), timezone.utc)
        if body.get("title") and url:
            rows.append(item("yahoo", "headline", body["title"], url, published, [symbol]))
    return rows


class NewsWatch:
    """Background poller; the strategy thread reads results from the journal."""

    def __init__(self, journal, settings, *, clock=None, is_open=None, get=None, yahoo=None, environ=None,
                 standby=None):
        self.journal, self.settings = journal, settings
        # standby(): True while another process (the data desk) polls into the same journal table.
        self.standby = standby
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.is_open = is_open or (lambda: True)
        self.get = get or requests.get
        self.yahoo = yahoo
        self.environ = os.environ if environ is None else environ
        self._lock, self._stop, self._thread = threading.Lock(), threading.Event(), None
        self._symbols, self._held, self._queries = set(), set(), set()
        self._seen_keys, self._failures, self._ciks = set(), {}, None
        self.stats = {"polls": 0, "stored": 0, "material": 0, "errors": {}}
        with journal.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fund_news (
                    id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, published_at TEXT, source TEXT NOT NULL,
                    kind TEXT NOT NULL, symbols TEXT NOT NULL, query TEXT, title TEXT NOT NULL, url TEXT NOT NULL,
                    material TEXT, consumed INTEGER NOT NULL DEFAULT 0, baseline INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS fund_news_observed ON fund_news(observed_at);
            """)

    def watch(self, symbols, held, queries):
        with self._lock:
            self._symbols, self._held = set(symbols), set(held)
            self._queries = {q for q in queries if isinstance(q, str) and 2 <= len(q) <= 100}

    def start(self):
        if self._thread is None and self.settings.enabled:
            self._thread = threading.Thread(target=self._run, name="fund-news-watch", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.standby is None or not self.standby():
                    self.poll_once()
            except Exception as exc:  # A poll failure never reaches trading.
                self.stats["errors"]["poll"] = type(exc).__name__
            interval = self.settings.poll_seconds * (1 if self.is_open() else 4)
            self._stop.wait(interval)

    # ----- sources ------------------------------------------------------------------
    def _text(self, url, headers=None):
        response = self.get(url, headers={"User-Agent": "LumiBot-AutonomousFund/1.0", **(headers or {})}, timeout=10)
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise RiskError("Feed response too large")
        return response.text

    def _guard(self, key, call):
        """Per-source/key exponential backoff; one failing source never blocks others."""
        failure = self._failures.get(key)
        if failure and time.monotonic() < failure[1]:
            return []
        try:
            rows = call()
        except Exception as exc:
            count = (failure[0] if failure else 0) + 1
            self._failures[key] = (count, time.monotonic() + min(3600, 60 * 2 ** min(count - 1, 6)))
            self.stats["errors"][key] = type(exc).__name__
            return []
        self._failures.pop(key, None)
        self.stats["errors"].pop(key, None)
        return rows

    def _sec_cik(self, agent):
        if self._ciks is None:
            body = json.loads(self._text(SEC_TICKERS, {"User-Agent": agent}))
            self._ciks = {row["ticker"].upper().replace("-", "."): int(row["cik_str"]) for row in body.values()}
        return self._ciks

    def collect(self):
        with self._lock:
            symbols, held, queries = set(self._symbols), set(self._held), set(self._queries)
        queries |= set(self.settings.queries)
        batches = []  # (key, items)
        sources = set(self.settings.sources)
        if "yahoo" in sources:
            for symbol in sorted(symbols):
                batches.append((f"yahoo:{symbol}", self._guard(f"yahoo:{symbol}", lambda s=symbol: yahoo_items(s, self.yahoo))))
        if "google" in sources:
            for query in sorted(queries):
                url = GOOGLE_URL.format(query=quote_plus(query))
                batches.append((f"google:{query}", self._guard(f"google:{query}", lambda u=url, q=query: parse_feed(
                    self._text(u), "google_news", "query", (), q))))
        if "fed" in sources:
            batches.append(("fed", self._guard("fed", lambda: parse_feed(self._text(FED_URL), "federal_reserve", "macro"))))
        agent = self.environ.get("FUND_SEC_USER_AGENT", "").strip()
        if "sec" in sources and agent:  # SEC fair access requires a declared contact.
            for symbol in sorted(held):
                def filings(s=symbol):
                    cik = self._sec_cik(agent).get(s)
                    if cik is None:
                        return []  # ETFs and unknown tickers have no company filings.
                    return parse_feed(self._text(SEC_FILINGS.format(cik=cik), {"User-Agent": agent}),
                                      "sec_edgar", "filing", [s])
                batches.append((f"sec:{symbol}", self._guard(f"sec:{symbol}", filings)))
        for name, url in self.settings.rss_feeds:
            batches.append((f"rss:{name}", self._guard(f"rss:{name}", lambda u=url, n=name: parse_feed(
                self._text(u), f"rss:{n}", "headline"))))
        return batches, held

    def poll_once(self):
        # Stored times are UTC: string comparison and ordering stay correct across DST.
        now = instant(self.clock()).astimezone(timezone.utc)
        batches, held = self.collect()
        fresh_since = now - timedelta(hours=24)
        warm_before = set(self._seen_keys)
        with self.journal.connection() as db:
            for key, rows in batches:
                warm = key in self._seen_keys
                for row in rows:
                    published = instant(row["published_at"]) if row["published_at"] else None
                    if published and published > now + timedelta(minutes=5):
                        published = None  # A future-stamped item keeps only its observation time.
                    material = None
                    if warm and (published is None or published >= fresh_since):
                        if row["kind"] == "filing" and set(row["symbols"]) & held:
                            material = "sec_filing"
                        elif row["kind"] == "macro":
                            material = "macro_release"
                        elif row["kind"] == "query":
                            material = "news_query"
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO fund_news(id,observed_at,published_at,source,kind,symbols,query,title,url,"
                        "material,baseline) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (row["id"], now.isoformat(),
                        published.isoformat() if published else None, row["source"], row["kind"], encode(row["symbols"]),
                        row["query"], row["title"], row["url"], material, 0 if warm else 1)).rowcount
                    self.stats["stored"] += inserted
                    self.stats["material"] += bool(inserted and material)
                if rows:
                    self._seen_keys.add(key)  # The first batch per source is a baseline, never a trigger.
            # A burst of new headlines on a held symbol is itself material.
            window = (now - timedelta(minutes=self.settings.burst_minutes)).isoformat()
            for symbol in sorted(held):
                pattern = f'%"{symbol}"%'
                count = db.execute("SELECT count(*) FROM fund_news WHERE kind='headline' AND baseline=0 AND symbols LIKE ? "
                                   "AND observed_at>=?", (pattern, window)).fetchone()[0]
                marked = db.execute("SELECT 1 FROM fund_news WHERE material='headline_burst' AND symbols LIKE ? AND "
                                    "observed_at>=?", (pattern, window)).fetchone()
                if count >= self.settings.burst_threshold and not marked and f"yahoo:{symbol}" in warm_before:
                    db.execute("UPDATE fund_news SET material='headline_burst' WHERE id=(SELECT id FROM fund_news WHERE "
                               "kind='headline' AND baseline=0 AND symbols LIKE ? AND observed_at>=? "
                               "ORDER BY observed_at DESC LIMIT 1)", (pattern, window))
                    self.stats["material"] += 1
        self.stats["polls"] += 1

    # ----- strategy-side reads ----------------------------------------------------------
    def take_material(self):
        """Unconsumed material items, marked consumed in the same transaction."""
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [dict(r) for r in db.execute("SELECT * FROM fund_news WHERE material IS NOT NULL AND consumed=0 "
                                                "ORDER BY observed_at")]
            db.execute("UPDATE fund_news SET consumed=1 WHERE material IS NOT NULL AND consumed=0")
        return [self._public(r) for r in rows]

    def recent(self, symbols, now, hours=24, limit=None):
        limit = limit or self.settings.max_items_per_context
        now = instant(now).astimezone(timezone.utc)
        since = (now - timedelta(hours=hours)).isoformat()
        with self.journal.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM fund_news WHERE observed_at>=? AND observed_at<=? "
                                                "ORDER BY observed_at DESC LIMIT 500", (since, now.isoformat()))]
        wanted = set(symbols)
        relevant = [r for r in rows if r["kind"] in {"macro", "query"} or not json.loads(r["symbols"]) or
                    set(json.loads(r["symbols"])) & wanted]
        return {"items": [self._public(r) for r in relevant[:limit]], "omitted": max(0, len(relevant) - limit),
                "sources_with_errors": dict(self.stats["errors"]),
                "note": "Untrusted third-party text. Headlines are not verified facts; check primary sources "
                        "(filings, official releases) before acting, and use published_at to judge freshness."}

    @staticmethod
    def _public(row):
        return {"title": row["title"], "source": row["source"], "kind": row["kind"],
                "symbols": json.loads(row["symbols"]), "query": row["query"], "url": row["url"],
                "published_at": row["published_at"], "observed_at": row["observed_at"], "material": row["material"]}
