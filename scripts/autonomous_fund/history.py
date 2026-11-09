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
ZONE = ZoneInfo("America/New_York")


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
            raise RiskError(f"Alpaca returned no completed daily bars for {symbol}")
        return sorted(rows)[-length:]
