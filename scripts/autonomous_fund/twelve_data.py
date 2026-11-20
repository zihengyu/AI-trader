"""Twelve Data US equity minute bars for read-only research observations."""
from datetime import datetime, timezone

import requests

from .risk import RiskError


URL = "https://api.twelvedata.com/time_series"


class TwelveDataMinutes:
    def __init__(self, api_key, get=None):
        self._headers = {"Authorization": f"apikey {api_key}"}
        self._get = get or requests.get

    def minutes(self, symbol):
        # Query two bars so a still-forming bar cannot displace the latest completed one.
        response = self._get(URL, headers=self._headers, timeout=5, allow_redirects=False,
                             params={"symbol": symbol, "interval": "1min", "timezone": "UTC",
                                     "country": "United States", "outputsize": 2, "prepost": "false",
                                     "adjust": "none"})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RiskError("Invalid Twelve Data response")
        if payload.get("status") == "error":
            # Some API errors are returned as JSON. Keep only the numeric status.
            code = payload.get("code")
            raise requests.HTTPError("Twelve Data rejected request",
                                     response=response if type(code) is not int else _status_only(code))
        meta = payload.get("meta") or {}
        values = payload.get("values")
        if (not isinstance(meta, dict) or meta.get("symbol") != symbol or meta.get("currency") != "USD" or
                meta.get("interval") != "1min" or not isinstance(values, list)):
            raise RiskError("Twelve Data response does not match the requested security and interval")
        bars = []
        for row in values:
            try:
                # The API returns naive candle-open strings in the requested timezone.
                start = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                bars.append({"start": start, **{field: row[field] for field in ("open", "high", "low", "close", "volume")}})
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(bars, key=lambda row: row["start"], reverse=True)


def _status_only(code):
    response = requests.Response()
    response.status_code = code
    return response
