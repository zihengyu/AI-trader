"""FINRA consolidated TRF/ADF short-sale volume for listed securities."""
import json
from datetime import date, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from .datastore import write_json
from .risk import RiskError, instant

URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day}.txt"
FILE = "finra_short_volume.json"
HEADER = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"
ZONE = ZoneInfo("America/New_York")


def parse(text, session, symbols):
    """Keep validated ratios for the requested symbols. ShortVolume already includes exempt volume."""
    lines = text.strip().splitlines()
    if len(lines) < 2 or lines[0].lstrip("\ufeff") != HEADER or lines[-1] != str(len(lines) - 2):
        raise RiskError("FINRA file has an invalid header or record count")
    expected = session.replace("-", "")
    ratios = {}
    for line in lines[1:-1]:
        fields = line.split("|")
        if len(fields) != 6 or fields[0] != expected:
            raise RiskError("FINRA file contains an invalid row or trade date")
        _, symbol, short, _, total, _ = fields
        if symbol not in symbols:
            continue
        try:
            short, total = float(short), float(total)
        except ValueError:
            continue
        if isfinite(total) and total > 0 and 0 <= short <= total:
            if symbol in ratios:
                raise RiskError("FINRA consolidated file repeats a requested symbol")
            ratios[symbol] = short / total
    return ratios


def read(cache_dir, now):
    """Read the latest locally observed file, with a bounded age and no future observations."""
    now = instant(now)
    try:
        payload = json.loads((cache_dir / FILE).read_text(encoding="utf-8"))
        session = date.fromisoformat(payload["session_date"])
        age = (now.astimezone(ZONE).date() - session).days
        observed = instant(payload["observed_at"])
        local_observed = observed.astimezone(ZONE)
        if (session.isoformat() != payload["session_date"] or not 0 <= age <= 7 or observed > now
                or not isinstance(payload["ratios"], dict) or session > local_observed.date()
                or (session == local_observed.date() and local_observed.hour < 18)):
            return None
        return payload
    except (OSError, KeyError, TypeError, ValueError):
        return None


def refresh(cache_dir, now, symbols, get=None):
    """Fetch one completed NYSE session at or after its scheduled FINRA publication."""
    import pandas_market_calendars as mcal

    now = instant(now)
    local = now.astimezone(ZONE)
    eligible = local.date() - timedelta(days=local.hour < 18)
    schedule = mcal.get_calendar("NYSE").schedule(start_date=eligible - timedelta(days=10), end_date=eligible)
    if schedule.empty:
        raise RiskError("No completed NYSE session for FINRA short volume")
    session = schedule.index[-1].date().isoformat()
    cached = read(cache_dir, now)
    if cached and cached["session_date"] == session and cached.get("symbols") == sorted(symbols):
        return cached
    if get is None:
        import requests
        get = requests.get
    response = get(URL.format(day=session.replace("-", "")), timeout=10)
    response.raise_for_status()
    payload = {"session_date": session, "observed_at": now.isoformat(), "symbols": sorted(symbols),
               "ratios": parse(response.text, session, set(symbols))}
    write_json(cache_dir / FILE, payload)
    return payload
