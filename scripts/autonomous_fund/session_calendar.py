"""Exchange-session boundaries shared by offline reports and the market-data adapter.

Keep calendars independent of LumiBot/broker imports so reading a report does not
initialize the trading runtime or its caches and loggers.
"""
from datetime import timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from .risk import instant


@lru_cache(maxsize=32)
def _session_bounds(day):
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    return (schedule.iloc[0].market_open, schedule.iloc[0].market_close) if not schedule.empty else (None, None)


def _session_close(day):
    return _session_bounds(day)[1]


def regular_session_open(now):
    now = instant(now)
    opened, closed = _session_bounds(now.astimezone(ZoneInfo("America/New_York")).date())
    return opened is not None and opened <= now < closed


def completed_daily_cutoff(now):
    """Exclusive New York date bound; today's bar is usable at its actual exchange close."""
    now = instant(now)
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    closed = _session_close(day)
    return day + timedelta(days=1) if closed is not None and now >= closed else day
