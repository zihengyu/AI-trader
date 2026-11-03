"""Real calendar waits for local simulation and live adapters, without broker I/O."""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from lumibot.brokers import broker as broker_module
from lumibot.brokers.alpaca import Alpaca
from lumibot.brokers.interactive_brokers import InteractiveBrokers
from lumibot.brokers.interactive_brokers_rest import InteractiveBrokersREST
from lumibot.brokers.schwab import Schwab
from lumibot.brokers.tradier import Tradier
from scripts.autonomous_fund.local_broker import LocalPaperBroker
from scripts.autonomous_fund.market_data import SessionClock


@pytest.fixture(params=["UTC", "Asia/Shanghai", "America/New_York", "America/Los_Angeles"])
def wall_clock(request, monkeypatch):
    host_zone = ZoneInfo(request.param)

    class Clock(datetime):
        instant = datetime(2026, 11, 2, 8, 55, 38, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            value = cls.instant.astimezone(tz or host_zone)
            if tz is None:
                value = value.replace(tzinfo=None)
            return cls.fromisoformat(value.isoformat())

        def astimezone(self, tz=None):
            # A naive wall clock belongs to the simulated host, including on
            # Windows where changing TZ does not change the process timezone.
            value = self if self.tzinfo else self.replace(tzinfo=host_zone)
            return datetime.astimezone(value, tz or host_zone)

    monkeypatch.setattr(broker_module, "datetime", Clock)
    monkeypatch.setattr(broker_module, "_local_tz", lambda: host_zone)
    return Clock


@pytest.fixture(
    params=[LocalPaperBroker, Schwab, Tradier, InteractiveBrokers, InteractiveBrokersREST],
    ids=lambda cls: cls.__name__,
)
def calendar_broker(request, wall_clock):
    # Skip constructors only: they authenticate and start streams. Exercise the
    # adapters' actual inherited waits and the real NYSE calendar below.
    broker = object.__new__(request.param)
    broker.market = "NYSE"
    broker.extended_trading_minutes = 0
    broker.logger = logging.getLogger(__name__)
    broker.data_source = SimpleNamespace(session=SessionClock(now=lambda: wall_clock.instant))
    return broker


@pytest.mark.parametrize(("now", "opening"), [
    ("2026-11-02T08:55:38+00:00", "2026-11-02T14:30:00+00:00"),
    ("2026-10-30T08:55:38+00:00", "2026-10-30T13:30:00+00:00"),
    ("2026-10-31T16:00:00+00:00", "2026-11-02T14:30:00+00:00"),
    ("2026-11-02T22:00:00+00:00", "2026-11-03T14:30:00+00:00"),
])
def test_open_and_premarket_wait_use_the_same_instant_in_every_host_zone(calendar_broker, wall_clock, now, opening):
    wall_clock.instant = datetime.fromisoformat(now)
    opening = datetime.fromisoformat(opening)
    assert calendar_broker.get_time_to_open() == (opening - wall_clock.instant).total_seconds()
    sleeps = []
    calendar_broker.sleep = sleeps.append
    calendar_broker._await_market_to_open(timedelta=60)
    assert wall_clock.instant + timedelta(seconds=sleeps.pop()) == opening - timedelta(hours=1)
    calendar_broker._await_market_to_open(timedelta=0)
    assert wall_clock.instant + timedelta(seconds=sleeps.pop()) == opening


def test_restart_inside_premarket_window_does_not_wait_past_the_hook(calendar_broker, wall_clock):
    wall_clock.instant = datetime(2026, 11, 2, 13, 45, tzinfo=timezone.utc)
    sleeps = []
    calendar_broker.sleep = sleeps.append
    calendar_broker._await_market_to_open(timedelta=60)
    assert sleeps == [0]


@pytest.mark.parametrize(("now", "closing"), [
    ("2026-11-02T15:00:00+00:00", "2026-11-02T21:00:00+00:00"),
    ("2026-10-30T15:00:00+00:00", "2026-10-30T20:00:00+00:00"),
    ("2026-12-24T15:00:00+00:00", "2026-12-24T18:00:00+00:00"),
])
def test_open_session_and_close_waits_keep_regular_dst_and_early_close_hours(calendar_broker, wall_clock, now, closing):
    wall_clock.instant = datetime.fromisoformat(now)
    closing = datetime.fromisoformat(closing)
    assert calendar_broker.is_market_open()
    assert calendar_broker.get_time_to_open() == 0
    assert calendar_broker.get_time_to_close() == (closing - wall_clock.instant).total_seconds()
    sleeps = []
    calendar_broker.sleep = sleeps.append
    calendar_broker._await_market_to_open(timedelta=60)
    assert not sleeps
    calendar_broker._await_market_to_close(timedelta=5)
    assert wall_clock.instant + timedelta(seconds=sleeps.pop()) == closing - timedelta(minutes=5)


def test_alpaca_keeps_provider_clock_overrides(wall_clock):
    broker = object.__new__(Alpaca)
    broker.api = SimpleNamespace(get_clock=lambda: SimpleNamespace(
        timestamp=wall_clock.instant,
        next_open=datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc),
        next_close=datetime(2026, 11, 2, 21, tzinfo=timezone.utc),
    ))
    assert broker.get_time_to_open() == 20062
    assert broker.get_time_to_close() == 43462
