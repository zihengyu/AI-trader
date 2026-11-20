"""Issuer-confirmed releases from the fund's scheduled economic calendar."""
import re
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from .risk import instant


ZONE = ZoneInfo("America/New_York")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
DATE = "(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})"
SOURCES = (
    ("monthly sales for retail", "https://www.census.gov/retail/sales.html", time(8, 30),
     r"FOR IMMEDIATE RELEASE:\s*(?:[A-Za-z]+,\s*)?", "Advance estimates of U.S. retail and food services sales"),
    ("industrial production and capacity utilization",
     "https://www.federalreserve.gov/releases/g17/Current/default.htm", time(9, 15),
     r"Release Date:\s*", "Industrial production (IP)"),
)


class _ReleasePage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks = [[]]
        self.hidden = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden = True
        if tag in {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.blocks.append([])

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = False
        if tag in {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.blocks.append([])

    def handle_data(self, data):
        if not self.hidden:
            self.blocks[-1].append(data)


def _publication(html, date_prefix, summary_prefix):
    page = _ReleasePage()
    page.feed(html)
    blocks = [" ".join(" ".join(block).split()) for block in page.blocks]
    match = re.search(date_prefix + DATE, " ".join(block for block in blocks if block), re.IGNORECASE)
    if match is None:
        raise ValueError("Issuer release date unavailable")
    month, day, year = match.groups()
    published = date(int(year), MONTHS.index(month.title()) + 1, int(day))
    summary = next((row[:350] for row in blocks if row.lower().startswith(summary_prefix.lower())), None)
    return published, summary


def release_status(releases, now, get, clock, prior=(), *, before_fetch=None):
    now = instant(now)
    today = now.astimezone(ZONE).date()
    prior = {(row["date"], row["release"]): row for row in prior}
    result, retry = [], 6 * 3600
    for release in releases:
        row = {key: value for key, value in release.items() if key in {"date", "release", "source", "release_id"}}
        row["publication_status"] = "scheduled"
        result.append(row)
        spec = next((item for item in SOURCES if item[0] in row["release"].lower()), None)
        if spec is None:
            continue
        _, url, scheduled_time, marker, prefix = spec
        day = date.fromisoformat(row["date"])
        scheduled = datetime.combine(day, scheduled_time, ZONE)
        row["issuer_url"] = url
        saved = prior.get((row["date"], row["release"]))
        if saved and saved.get("publication_status") == "confirmed" and instant(saved["checked_at"]) <= now:
            row.update(saved)
        elif now < scheduled:
            retry = min(retry, max(1, int((scheduled - now).total_seconds())))
        elif day == today:
            if before_fetch is not None:
                before_fetch()
            try:
                response = get(url, timeout=8, headers={"User-Agent": "LumiBot-AutonomousFund/1.0"})
                response.raise_for_status()
                published, summary = _publication(response.text, marker, prefix)
                if published == today and summary:
                    row.update(publication_status="confirmed", issuer_release_date=published.isoformat(), summary=summary)
                else:
                    row.update(publication_status="unconfirmed", issuer_release_date=published.isoformat())
            except Exception as exc:
                row.update(publication_status="fetch_failed", error=type(exc).__name__)
            row["checked_at"] = instant(clock()).isoformat()
            if row["publication_status"] != "confirmed" and now < scheduled + timedelta(hours=7):
                retry = min(retry, 300)
    return result, retry
