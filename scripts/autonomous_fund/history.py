"""A second source of completed daily bars for risk and factor history: Alpaca market data.

Yahoo is unofficial and can rate-limit or block a server; then the risk engine
treats a name as having no history and refuses to add to it. When Alpaca keys
are present (free paper keys suffice), local mode fetches the same completed
daily bars from Alpaca's historical API instead, split-adjusted like the Yahoo
closes the fund uses (`adjustment=split`). It serves risk estimation and the
factor lab only: the local virtual broker still matches orders and books
dividends and splits from its own feed. The consolidated (SIP) feed is asked
first; an account without it falls back to IEX bars, and the source is recorded.
"""
import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .risk import RiskError, instant, number

URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
ACTIONS_URL = "https://data.alpaca.markets/v1/corporate-actions"
ZONE = ZoneInfo("America/New_York")


class NoDailyBars(RiskError):
    """The reference returned no completed sessions for this symbol."""


def _cash_dividend(action):
    foreign = action.get("foreign")
    # Legacy domestic records omit currency. An explicitly reported USD rate
    # is also comparable for a foreign issuer.
    currency = action.get("currency", None if foreign else "USD")
    if isinstance(currency, str):
        currency = currency.strip().upper()
    try:
        rate = number(action.get("rate"))
        if rate <= 0:
            raise RiskError("Invalid Alpaca corporate action amount")
    except RiskError:
        if currency == "USD":
            raise
        rate = None  # An unusable amount still proves that Alpaca returned a record.
    if currency == "USD":
        return {"dividend": str(rate)}
    valid_code = isinstance(currency, str) and len(currency) == 3 and currency.isascii() and currency.isalpha()
    return {"unverified_dividend": {
        "rate": str(rate) if rate is not None else None,
        "currency": currency if valid_code else None,
        "foreign": foreign if isinstance(foreign, bool) else None,
    }}


class AlpacaDailyHistory:
    def __init__(self, key, secret, get=None):
        import requests
        if not key or not secret:
            raise RiskError("Alpaca history needs ALPACA_API_KEY and ALPACA_API_SECRET")
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.get = get or requests.get
        self.feed = "sip"

    def _pages(self, symbol, params):
        rows, token = [], None
        for _ in range(20):
            response = self.get(URL.format(symbol=symbol), headers=self.headers, timeout=15,
                                params={**params, **({"page_token": token} if token else {})})
            if getattr(response, "status_code", 200) == 403 and params.get("feed") == "sip":
                raise PermissionError("sip")
            response.raise_for_status()
            body = response.json() or {}
            rows += body.get("bars") or []
            token = body.get("next_page_token")
            if not token:
                return rows
        raise RiskError("Alpaca history returned too many pages")

    def bars(self, symbol, length, today):
        """[(date, close, volume, open, high, low)] for completed sessions before `today`, oldest first."""
        today = today if isinstance(today, date) else date.fromisoformat(str(today)[:10])
        params = {"timeframe": "1Day", "adjustment": "split", "limit": 10000, "feed": self.feed,
                  "start": (today - timedelta(days=int(length * 1.5) + 10)).isoformat(),
                  # Include the whole last eligible session, rather than stopping at UTC midnight.
                  "end": datetime.combine(today, time.min, ZONE).isoformat()}
        try:
            raw = self._pages(symbol, params)
        except PermissionError:
            self.feed = params["feed"] = "iex"  # This account has no consolidated feed.
            raw = self._pages(symbol, params)
        rows = []
        for bar in raw:
            # Source timestamps must identify a real instant. Interpreting a
            # naive value in the host timezone can admit today's bar as yesterday.
            day = instant(bar["t"]).astimezone(ZONE).date()
            if day < today and bar.get("c") is not None:
                close = float(number(bar["c"]))
                if not math.isfinite(close) or close <= 0:
                    raise RiskError("Alpaca history requires finite positive closes")
                rows.append((day, close, float(bar.get("v") or 0), float(bar.get("o") or "nan"),
                             float(bar.get("h") or "nan"), float(bar.get("l") or "nan")))
        if not rows:
            raise NoDailyBars(f"Alpaca returned no completed daily bars for {symbol}")
        return sorted(rows)[-length:]

    def corporate_actions(self, symbols, start, end):
        """Split and cash-dividend observations by ex-date, including unresolved currency evidence."""
        symbols = sorted(set(symbols))
        kinds = {"forward_splits": "split", "reverse_splits": "split", "cash_dividends": "dividend"}
        result = {symbol: [] for symbol in symbols}
        for offset in range(0, len(symbols), 50):
            batch = set(symbols[offset:offset + 50])
            params = {"symbols": ",".join(sorted(batch)), "types": "forward_split,reverse_split,cash_dividend",
                      "start": str(start), "end": str(end), "limit": 1000}
            seen = set()
            for _ in range(10):
                response = self.get(ACTIONS_URL, headers=self.headers, params=params, timeout=10)
                response.raise_for_status()
                payload = response.json()
                groups = payload.get("corporate_actions")
                if not isinstance(groups, dict):
                    raise RiskError("Invalid Alpaca corporate action response")
                for name, kind in kinds.items():
                    for action in groups.get(name) or []:
                        symbol = action.get("symbol")
                        if symbol not in batch:
                            continue
                        day = date.fromisoformat(action["ex_date"])
                        if not start <= day <= end:
                            continue
                        if kind == "split":
                            old = number(action["old_rate"])
                            if old <= 0:
                                raise RiskError("Invalid Alpaca split ratio")
                            amount = number(action["new_rate"]) / old
                            if amount <= 0:
                                raise RiskError("Invalid Alpaca corporate action amount")
                            values = {"split": str(amount)}
                        else:
                            values = _cash_dividend(action)
                        identifier = action.get("id")
                        if identifier and identifier in seen:
                            continue
                        if identifier:
                            seen.add(identifier)
                        result[symbol].append({"date": day.isoformat(), **values})
                token = payload.get("next_page_token")
                if not token:
                    break
                params = {**params, "page_token": token}
            else:
                raise RiskError("Alpaca corporate actions returned too many pages")
        return result
