"""Point-in-time company fundamentals from SEC EDGAR XBRL company facts (free, official).

Each reported value carries the date its filing was accepted (`filed`). A value
is used on a date only if its filing was accepted BEFORE that date, and when a
later filing restates a period, the restated value is used only from its own
filing date. That is what "point in time" means, and it is what makes these
fields safe in historical runs.

Fields (USD unless noted):
- earnings, revenue, gross_profit, cashflow: trailing twelve months (TTM) of
  standalone quarters. 10-Q income statements report the quarter; cash flow
  statements report year-to-date. Quarters are derived by differencing
  cumulative values that share a fiscal-year start, and the fourth quarter as
  the annual value minus the first nine months. TTM needs four consecutive
  quarters; otherwise the value is missing, never extrapolated.
- book, assets: the latest balance-sheet value.
- shares: common shares outstanding from the filing cover page (dei). A company
  reporting several share classes on one date has no single count: missing.

SEC asks automated clients to identify themselves (FUND_SEC_USER_AGENT, a name
and e-mail) and to stay under ten requests per second. Company facts are cached
on disk with only the needed concepts and refreshed when a new quarterly filing
is likely (about 80 days after the latest one), at most weekly.
"""
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .risk import RiskError

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "10-KT", "10-QT"}
FLOWS = {
    "earnings": ("NetIncomeLoss",),
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                "RevenuesNetOfInterestExpense"),
    "gross_profit": ("GrossProfit",),
    "cashflow": ("NetCashProvidedByUsedInOperatingActivities",),
}
STOCKS = {"book": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
          "assets": ("Assets",)}
SHARES = "EntityCommonStockSharesOutstanding"
QUARTER = (80, 100)
REFRESH_AFTER_DAYS, MIN_REFRESH_DAYS = 80, 7


def _d(value):
    return date.fromisoformat(str(value)[:10])


def _facts(payload, taxonomy, concepts, unit):
    """Facts of the first concepts (priority order) merged by period, from 10-Q/10-K filings only."""
    merged = {}
    for concept in concepts:
        for row in (((payload.get("facts") or {}).get(taxonomy) or {}).get(concept) or {}).get("units", {}).get(unit, []):
            if row.get("form") not in FORMS or row.get("val") is None or not row.get("filed") or not row.get("end"):
                continue
            key = (row.get("start"), row["end"], row["filed"])
            merged.setdefault(key, {"start": row.get("start"), "end": row["end"], "filed": row["filed"],
                                    "val": float(row["val"])})
    return list(merged.values())


def quarters(facts):
    """{quarter_end: [(filed, value)]}: standalone quarters reported directly or derived from cumulative values."""
    result = {}

    def add(end, filed, value):
        result.setdefault(end, []).append((filed, value))
    by_start = {}
    for fact in facts:
        if not fact.get("start"):
            continue
        days = (_d(fact["end"]) - _d(fact["start"])).days
        if QUARTER[0] <= days <= QUARTER[1]:
            add(fact["end"], fact["filed"], fact["val"])
        by_start.setdefault(fact["start"], []).append(fact)
    for start, group in by_start.items():
        # Cumulative values from one fiscal-year start: consecutive differences are quarters.
        ends = sorted({f["end"] for f in group})
        versions = {end: sorted((f["filed"], f["val"]) for f in group if f["end"] == end) for end in ends}
        for previous, current in zip(ends, ends[1:]):
            if not QUARTER[0] <= (_d(current) - _d(previous)).days <= QUARTER[1]:
                continue
            for filed, value in versions[current]:
                known = [v for f, v in versions[previous] if f <= filed]
                if known:
                    add(current, filed, value - known[-1])
    return result


def ttm_series(facts):
    """[(filed, ttm)]: the trailing-twelve-month value as known after each filing."""
    q = quarters(facts)
    filings = sorted({filed for versions in q.values() for filed, _ in versions})
    series, last = [], None
    for filed in filings:
        known = {}
        for end, versions in q.items():
            values = [v for f, v in sorted(versions) if f <= filed]
            if values:
                known[end] = values[-1]
        ends = sorted(known)[-4:]
        if len(ends) < 4 or any(not QUARTER[0] <= (_d(b) - _d(a)).days <= QUARTER[1] for a, b in zip(ends, ends[1:])):
            value = None
        else:
            value = sum(known[e] for e in ends)
        if value != last:
            series.append((filed, value))
            last = value
    return series


def instant_series(facts):
    """[(filed, value)]: the latest balance-sheet value as known after each filing."""
    filings = sorted({f["filed"] for f in facts})
    series, last = [], None
    for filed in filings:
        known = [f for f in facts if f["filed"] <= filed]
        latest_end = max(f["end"] for f in known)
        value = sorted((f["filed"], f["val"]) for f in known if f["end"] == latest_end)[-1][1]
        if value != last:
            series.append((filed, value))
            last = value
    return series


def shares_series(payload):
    """Cover-page shares outstanding; a date with several class counts is ambiguous (missing)."""
    rows = [r for r in (((payload.get("facts") or {}).get("dei") or {}).get(SHARES) or {}).get("units", {}).get(
        "shares", []) if r.get("form") in FORMS and r.get("val") is not None and r.get("filed") and r.get("end")]
    by_filing = {}
    for row in rows:
        by_filing.setdefault((row["filed"], row["end"]), set()).add(float(row["val"]))
    series, last = [], None
    for (filed, _), values in sorted(by_filing.items()):
        value = next(iter(values)) if len(values) == 1 else None
        if value != last:
            series.append((filed, value))
            last = value
    return series


def extract(payload):
    """{field: [(filed, value)]} for every field from one company-facts document."""
    fields = {name: ttm_series(_facts(payload, "us-gaap", concepts, "USD")) for name, concepts in FLOWS.items()}
    for name, concepts in STOCKS.items():
        facts = _facts(payload, "us-gaap", concepts, "USD")
        fields[name] = instant_series(facts) if facts else []
    fields["shares"] = shares_series(payload)
    return fields


def as_of(series, index):
    """Align [(filed, value)] to trading dates: a filing counts only from the day AFTER it was accepted."""
    values, position, current = [], 0, None
    ordered = sorted(series, key=lambda item: item[0])  # By filing date only: values may be None.
    for stamp in index:
        day = stamp.date() if hasattr(stamp, "date") else _d(stamp)
        while position < len(ordered) and _d(ordered[position][0]) < day:
            current = ordered[position][1]
            position += 1
        values.append(current if current is not None else float("nan"))
    return pd.Series(values, index=index, dtype=float)


class SecFundamentals:
    def __init__(self, cache_dir, user_agent, get=None, sleep=time.sleep, today=None):
        import requests
        if not user_agent or "@" not in str(user_agent):
            raise RiskError("SEC fundamentals need FUND_SEC_USER_AGENT with a name and e-mail address")
        self.root = Path(cache_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.headers = {"User-Agent": str(user_agent), "Accept-Encoding": "gzip, deflate"}
        self.get, self.sleep = get or requests.get, sleep
        self.today = today or (lambda: datetime.now(timezone.utc).date())
        self.errors = {}

    def _download(self, url):
        self.sleep(0.15)  # Well under SEC's ten requests per second.
        response = self.get(url, headers=self.headers, timeout=30)
        response.raise_for_status()
        return response.json()

    def ciks(self):
        path = self.root / "company_tickers.json"
        fresh = path.is_file() and (time.time() - path.stat().st_mtime) < 7 * 86400
        if not fresh:
            try:
                path.write_text(json.dumps(self._download(TICKERS_URL)), encoding="utf-8")
            except Exception:
                if not path.is_file():
                    raise
        rows = json.loads(path.read_text(encoding="utf-8")).values()
        return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in rows}

    def fields(self, symbol, cik, allow_download=True):
        """{field: [(filed, value)]} from the cached extract, refreshed when a new filing is likely."""
        path = self.root / f"{symbol.replace('.', '-')}.json"
        cached = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        today = self.today()
        if cached and not allow_download:  # A due refresh waits for a call with time to download.
            return {k: [tuple(x) for x in v] for k, v in cached["fields"].items()}
        if cached:
            latest = max((filed for series in cached["fields"].values() for filed, _ in series), default=None)
            checked = _d(cached["checked"])
            due = latest is None or (today - _d(latest)).days >= REFRESH_AFTER_DAYS
            if not due or (today - checked).days < MIN_REFRESH_DAYS:
                return {k: [tuple(x) for x in v] for k, v in cached["fields"].items()}
        failed = self.__dict__.setdefault("_failed", set())
        if (today, symbol) in failed:  # One attempt per company and day.
            if cached:
                return {k: [tuple(x) for x in v] for k, v in cached["fields"].items()}
            raise RiskError(f"SEC company facts for {symbol} failed earlier today")
        try:
            fields = extract(self._download(FACTS_URL.format(cik=cik)))
        except Exception as exc:
            failed.add((today, symbol))
            if cached:
                self.errors[symbol] = type(exc).__name__
                return {k: [tuple(x) for x in v] for k, v in cached["fields"].items()}
            raise
        path.write_text(json.dumps({"checked": today.isoformat(), "fields": fields}), encoding="utf-8")
        return fields

    def panels(self, symbols, index, may_download=None):
        """{field: DataFrame(index x symbols)} as-of values; NaN where a company or field is unavailable.

        may_download(): when it returns False, companies without a usable cached
        extract are skipped and listed in self.deferred for a later call.
        """
        self.deferred = []
        try:
            ciks = self.ciks()
        except Exception as exc:
            self.errors["company_tickers"] = type(exc).__name__
            ciks = {}
        columns = {}
        for symbol in sorted(symbols):
            cik = ciks.get(symbol.replace(".", "-").upper())
            if cik is None:
                self.errors.setdefault(symbol, "no SEC CIK")
                continue
            allowed = may_download is None or may_download()
            if not allowed and not (self.root / f"{symbol.replace('.', '-')}.json").is_file():
                self.deferred.append(symbol)  # Nothing cached yet: fetched in a later call.
                continue
            try:
                fields = self.fields(symbol, cik, allow_download=allowed)
            except Exception as exc:
                self.errors[symbol] = type(exc).__name__
                continue
            for name, series in fields.items():
                columns.setdefault(name, {})[symbol] = as_of(series, index)
        names = list(FLOWS) + list(STOCKS) + ["shares"]
        return {name: pd.DataFrame(columns.get(name, {}), index=index).reindex(columns=sorted(symbols))
                for name in names}
