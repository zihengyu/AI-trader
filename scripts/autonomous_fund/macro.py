"""Macro regime context from FRED: Treasury yields, the curve, credit spreads and volatility.

The AI receives each series' latest value, its 20-session change and its
percentile over the last year, plus a few deterministic flags (inverted curve,
credit stress, elevated volatility) and the T-bill yield the cash sweep earns.
It is context for the AI, not a trading rule.

These are market series (yields, option-adjusted spreads, the VIX close) that
FRED does not revise, so keeping only observations dated BEFORE the as-of day is
look-ahead-safe in historical runs as well. With FRED_API_KEY the official API
is used; without it the public fredgraph CSV download. Each series is fetched
at most once per day (once per run in a historical run); a failure only marks
that series unavailable.
"""
import csv
import io
import os
from dataclasses import dataclass
from datetime import date, timedelta

from .risk import RiskError

SERIES = {
    "DGS3MO": "3-month Treasury bill yield, %",
    "DGS2": "2-year Treasury yield, %",
    "DGS10": "10-year Treasury yield, %",
    "T10Y2Y": "10-year minus 2-year Treasury spread, percentage points",
    "BAMLH0A0HYM2": "ICE BofA US high-yield option-adjusted spread, percentage points",
    "VIXCLS": "CBOE VIX index close",
}
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
API_URL = "https://api.stlouisfed.org/fred/series/observations"


@dataclass(frozen=True)
class MacroSettings:
    enabled: bool = True
    history_days: int = 400

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise RiskError("macro.enabled must be true/false")
        if type(self.history_days) is not int or not 60 <= self.history_days <= 4000:
            raise RiskError("macro.history_days must be 60..4000")

    @classmethod
    def from_config(cls, section):
        section = dict(section or {})
        unknown = set(section) - set(cls.__dataclass_fields__)
        if unknown:
            raise RiskError("Unknown macro settings: " + ", ".join(sorted(unknown)))
        return cls(**section)


def parse_csv(text):
    """fredgraph CSV -> [(YYYY-MM-DD, value)]; missing values ('.' or empty) are skipped, never filled."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or len(rows[0]) < 2:
        raise RiskError("FRED returned an unexpected CSV")
    result = []
    for row in rows[1:]:
        if len(row) < 2 or row[1].strip() in {"", "."}:
            continue
        try:
            result.append((date.fromisoformat(row[0].strip()).isoformat(), float(row[1])))
        except ValueError:
            continue
    return result


def summarize(observations, before_day):
    """Latest value strictly before `before_day`, its 20-observation change and 1-year percentile."""
    before = date.fromisoformat(str(before_day)[:10]).isoformat()
    usable = sorted((d, v) for d, v in observations if d < before)
    if not usable:
        return {"unavailable": "no observation before the as-of day"}
    day, latest = usable[-1]
    year = [v for d, v in usable if d >= (date.fromisoformat(day) - timedelta(days=365)).isoformat()]
    change = latest - usable[-21][1] if len(usable) > 20 else None
    return {"latest": round(latest, 4), "date": day,
            "change_20_obs": round(change, 4) if change is not None else None,
            "percentile_1y": round(sum(v <= latest for v in year) / len(year), 3) if year else None}


def regime(series):
    """Deterministic flags from the summaries; None when an input is unavailable."""
    def value(name, key="latest"):
        return (series.get(name) or {}).get(key)
    curve, spread, change, vix = value("T10Y2Y"), value("BAMLH0A0HYM2"), value("BAMLH0A0HYM2", "change_20_obs"), \
        value("VIXCLS")
    return {
        "curve_inverted": None if curve is None else curve < 0,
        "credit_stress": None if spread is None else bool(spread >= 6 or (change is not None and change >= 1)),
        "volatility_elevated": None if vix is None else vix >= 25,
        "cash_yield_pct": value("DGS3MO"),
        "meaning": "curve_inverted: 10y-2y < 0; credit_stress: high-yield spread >= 6 points or up >= 1 point over "
                   "20 observations; volatility_elevated: VIX >= 25. Context for the AI, not orders.",
    }


class MacroWatch:
    def __init__(self, settings, environ=None, get=None, historical=False):
        import requests
        self.settings, self.historical = settings, historical
        self.environ = os.environ if environ is None else environ
        self.get = get or requests.get
        self._raw = {}  # series -> (fetched_on, observations)

    def _fetch(self, name, today):
        start = (today - timedelta(days=self.settings.history_days)).isoformat()
        key = str(self.environ.get("FRED_API_KEY", "")).strip()
        if key:
            response = self.get(API_URL, params={"series_id": name, "api_key": key, "file_type": "json",
                                                 "observation_start": start}, timeout=15)
            response.raise_for_status()
            return [(row["date"], float(row["value"])) for row in response.json().get("observations", [])
                    if row.get("value") not in {None, "", "."}]
        response = self.get(CSV_URL, params={"id": name, "cosd": start}, timeout=15,
                            headers={"User-Agent": "LumiBot-AutonomousFund/1.0"})
        response.raise_for_status()
        return parse_csv(response.text)

    def context(self, as_of_day):
        today = date.fromisoformat(str(as_of_day)[:10])
        series = {}
        for name, meaning in SERIES.items():
            cached = self._raw.get(name)
            # A historical run fetches each series once and slices it by date; live runs refresh daily.
            if cached is None or (not self.historical and cached[0] != today):
                try:
                    self._raw[name] = cached = (today, self._fetch(name, today))
                except Exception as exc:
                    self._raw[name] = cached = (today, exc)
            if isinstance(cached[1], Exception):
                error = cached[1]
                series[name] = {"meaning": meaning,
                                "unavailable": str(error)[:200] if isinstance(error, RiskError) else type(error).__name__}
            else:
                series[name] = {"meaning": meaning, **summarize(cached[1], today)}
        return {"source": "FRED (St. Louis Fed)", "as_of_day": today.isoformat(), "series": series,
                "regime": regime(series)}
