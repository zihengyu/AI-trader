"""S&P 500 entry dates: a stock counts in historical cross-sections only from the date it joined.

Synthetic CSV text, prices and fake HTTP clients only; no network, broker or model inference.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.autonomous_fund.factors import MEMBERSHIP, FactorSettings, compute, daily_ic
from scripts.autonomous_fund.index_membership import MAX_AGE_DAYS, load, mask, parse, summary
from scripts.autonomous_fund.risk import RiskError

NOW = datetime(2026, 11, 9, 21, 30, tzinfo=timezone.utc)
HEADER = "Symbol,Security,GICS Sector,GICS Sub-Industry,Headquarters Location,Date added,CIK,Founded\n"


def csv_text(count=450, extra=""):
    rows = "".join(f"S{i:03d},Synthetic {i},Industrials,Sub,City,2001-01-{1 + i % 28:02d},{i},1900\n"
                   for i in range(count))
    return HEADER + rows + extra


def test_the_constituents_file_is_parsed_strictly():
    added = parse(csv_text(3, 'BRK.B,Synthetic B,Financials,Sub,"City, State",2010-02-16,1,1900\n'
                              "NODATE,Synthetic N,Industrials,Sub,City,,2,1900\n"
                              "BAD,Synthetic X,Industrials,Sub,City,2010-13-40,3,1900\n"))
    assert added["BRK.B"] == "2010-02-16" and added["S000"] == "2001-01-01"
    assert "NODATE" not in added and "BAD" not in added  # Never estimated.
    with pytest.raises(RiskError, match="Date added"):
        parse("Ticker,Name\nA,B\n")


class Get:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.calls = text, error, 0

    def __call__(self, url, headers=None, timeout=None):
        self.calls += 1
        assert "Mozilla" not in headers["User-Agent"] and "@" not in headers["User-Agent"]
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.text, raise_for_status=lambda: None)


def test_the_list_is_cached_for_a_month_and_a_failed_refresh_keeps_the_old_one(tmp_path):
    get = Get(csv_text(extra="BRK.B,B,Financials,Sub,City,2010-02-16,1,1900\n"))
    first = load(tmp_path, NOW, get)
    assert first["added"]["BRK.B"] == "2010-02-16" and get.calls == 1
    assert load(tmp_path, NOW + timedelta(days=MAX_AGE_DAYS - 1), get)["fetched_at"] == first["fetched_at"]
    assert get.calls == 1  # Within a month: from the cache.
    stale = load(tmp_path, NOW + timedelta(days=MAX_AGE_DAYS + 1), Get(error=ConnectionError()))
    assert stale["stale"] == "ConnectionError" and stale["added"] == first["added"]
    truncated = load(tmp_path, NOW + timedelta(days=MAX_AGE_DAYS + 1), Get(csv_text(50)))
    assert truncated["stale"] == "ValueError"  # A truncated file never replaces the known list.
    assert load(tmp_path / "empty", NOW, Get(error=ConnectionError())) is None


def panel(days=300, names=20, seed=4):
    index = pd.bdate_range(end="2026-11-06", periods=days)
    rng = np.random.default_rng(seed)
    closes = 100 * np.cumprod(1 + rng.normal(0, 0.01, (days, names)), axis=0)
    return pd.DataFrame(closes, index=index, columns=[f"N{i:02d}" for i in range(names)])


def test_factor_values_are_masked_before_entry_but_look_backs_stay_complete():
    close = panel()
    joined = str(close.index[150].date())
    flags = mask(close, {"N00": joined, "N01": "1990-01-02"})
    assert not flags["N00"].iloc[:150].any() and flags["N00"].iloc[150:].all() and flags["N01"].all()
    assert flags["N02"].all()  # Undated: counts throughout.
    plain = compute("ret(20)", close)
    masked = compute("ret(20)", close, extra={MEMBERSHIP: flags})
    assert masked["N00"].iloc[:150].isna().all()
    # The first day after entry already has a full 20-session look-back from pre-entry prices.
    assert masked["N00"].iloc[150] == pytest.approx(plain["N00"].iloc[150])
    assert masked.drop(columns="N00").equals(plain.drop(columns="N00"))


def test_a_future_entrants_pre_entry_rise_no_longer_makes_momentum_look_predictive():
    close = panel(names=25, seed=9)
    winners = [f"N{i:02d}" for i in range(5)]
    rise = pd.Series(np.where(np.arange(len(close)) < 150, 0.006, 0.0), index=close.index)
    for symbol in winners:  # They rose steadily before being added to the index at session 150.
        close[symbol] = close[symbol] * (1 + rise).cumprod()
    joined = str(close.index[150].date())
    flags = mask(close, {s: joined for s in winners})
    before = close.index[:140]
    plain = daily_ic(compute("rank(ret(20))", close), close, 5, 5)
    masked = daily_ic(compute("rank(ret(20))", close, extra={MEMBERSHIP: flags}), close, 5, 5)
    assert plain.loc[plain.index.isin(before), "ic"].mean() > 0.2  # Selection on the outcome looks like skill.
    assert abs(masked.loc[masked.index.isin(before), "ic"].mean()) < 0.1


def test_settings_and_the_strategy_panel_apply_the_list_in_live_modes_only(tmp_path):
    from scripts.autonomous_fund.factors import FactorLab
    from scripts.autonomous_fund.journal import Journal
    from tests.test_autonomous_fund_universe_snapshot import frame
    from tests.test_autonomous_fund_quant import harness
    assert FactorSettings().index_membership is True
    with pytest.raises(RiskError, match="index_membership"):
        FactorSettings(index_membership="yes")
    cache = tmp_path / "run" / "cache"
    cache.mkdir(parents=True)
    (cache / "index_membership.json").write_text(json.dumps({
        "added": {"ALFA": "2026-10-30", "BETA": "1990-01-02"}, "source": "synthetic", "fetched_at": NOW.isoformat()}))
    strategy = harness(tmp_path, mode="local")
    strategy.run_dir = tmp_path / "run"
    strategy.config = {"universe": {"ALFA": "stock", "BETA": "stock", "GAMMA": "stock"}}
    strategy.universe = frozenset({"ALFA", "BETA", "GAMMA"})
    strategy.factor_lab = FactorLab(Journal(tmp_path / "j", "synthetic-membership"), FactorSettings())
    data = {s: frame("2026-10-26", np.arange(100.0, 110.0) + i) for i, s in enumerate(("ALFA", "BETA", "GAMMA"))}
    strategy.get_historical_prices = lambda asset, length, timestep="day": SimpleNamespace(df=data[asset.symbol])
    strategy._factor_cache = None
    now = datetime.fromisoformat("2026-11-05T21:00:00+00:00")
    flags = strategy._factor_panel(now)["stocks"][MEMBERSHIP]
    assert not flags["ALFA"].iloc[:4].any() and flags["ALFA"].iloc[4:].all() and flags["BETA"].all()
    strategy._factor_context_cache = None
    context = strategy._factor_context(now)["index_membership"]
    assert context["dated"] == 2 and context["undated"] == ["GAMMA"]
    assert context["joined_within_history"] == {"ALFA": "2026-10-30"}
    strategy.mode = "mock"
    strategy._membership_cache = None
    assert strategy._index_membership(now) is None  # Replays and tests stay off the network.
    assert summary(pd.DataFrame(), None) == {"applied": False, "reason": "no S&P 500 membership list available yet"}
