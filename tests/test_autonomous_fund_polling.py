"""Bounded market I/O, single-flight caching, and source-time execution safety."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
import time

import pytest

from lumibot.entities import Asset
from scripts.autonomous_fund.market_data import YahooRealtimeData
from scripts.autonomous_fund.risk import RiskError
from .test_autonomous_fund_local import Clock


def test_same_symbol_concurrent_reads_share_one_download_and_copy_results():
    clock, calls = Clock(), []
    def fetch(*args):
        calls.append(args)
        time.sleep(.03)
        return clock.fetch(*args)
    source = YahooRealtimeData(now=clock, fetch=fetch)
    asset = Asset('ALFA', asset_type='stock')
    with ThreadPoolExecutor(max_workers=8) as executor:
        frames = list(executor.map(lambda _: source.execution_bars(asset), range(8)))
    assert len(calls) == 1 and source.diagnostics()['cache_hits'] == 7
    frames[0].iloc[-1, 0] = -1
    assert source.execution_bars(asset).iloc[-1].open > 0


def test_independent_symbols_are_parallel_but_never_exceed_two_downloads():
    clock, lock = Clock(), threading.Lock()
    counts = {'active': 0, 'peak': 0}
    def fetch(*args):
        with lock:
            counts['active'] += 1
            counts['peak'] = max(counts['peak'], counts['active'])
        time.sleep(.04)
        result = clock.fetch(*args)
        with lock:
            counts['active'] -= 1
        return result
    source = YahooRealtimeData(now=clock, fetch=fetch)
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda s: source.execution_bars(Asset(s, asset_type='stock')), ['ALFA','BETA','GAMM','DELT','EPSI','ZETA']))
    assert counts['peak'] == 2 and source.diagnostics()['requests'] == 6


def test_backoff_never_serves_previous_cached_success_as_fresh(monkeypatch):
    clock, monotonic = Clock(), [100.]
    monkeypatch.setattr('scripts.autonomous_fund.market_data.time.monotonic', lambda: monotonic[0])
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    asset = Asset('ALFA', asset_type='stock')
    source.get_quote(asset)
    monotonic[0] += 16
    calls = []
    def failed(*args):
        calls.append(args)
        raise TimeoutError('synthetic endpoint with sensitive query')
    source.fetch = failed
    with pytest.raises(RiskError, match='TimeoutError'):
        source.get_quote(asset)
    with pytest.raises(RiskError, match='cooling down'):
        source.get_quote(asset)
    assert len(calls) == 1 and source.diagnostics()['backoff_blocks'] == 1
    monotonic[0] += 16
    source.fetch = clock.fetch
    source.get_quote(asset)
    assert not source._failures


def test_cache_does_not_refresh_source_timestamp_or_expose_forming_bar():
    clock = Clock()
    old = clock.fetch('ALFA', '1m', '5d')
    old = old[old.index + timedelta(minutes=1) <= clock.value]
    # A new minute now refreshes the cache. Keep the provider's observations
    # unchanged to verify that a new receipt still cannot rejuvenate old data.
    source = YahooRealtimeData(now=clock, fetch=lambda *args: old.copy())
    asset = Asset('ALFA', asset_type='stock')
    quote = source.get_quote(asset)
    clock.value += timedelta(seconds=75)
    repeated = source.get_quote(asset)
    assert repeated.timestamp == quote.timestamp
    assert (source.get_datetime() - repeated.timestamp).total_seconds() > 60
    assert source.diagnostics()['requests'] == 2
    assert source.execution_bars(asset).index[-1] + timedelta(minutes=1) <= clock.value
