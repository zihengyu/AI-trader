"""Professional factor research: separate cross-sections, point-in-time fundamentals,
costs, redundancy and tradable-only idea lists.

Synthetic panels and synthetic SEC company-facts documents only; no network.
"""
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.autonomous_fund.factors import FactorLab, FactorSettings, compute, evaluate
from scripts.autonomous_fund.fundamentals import SecFundamentals, as_of, extract, quarters, shares_series, ttm_series
from scripts.autonomous_fund.journal import Journal
from tests.test_autonomous_fund_factors import NOW, panel, proposal

SETTINGS = FactorSettings()


def fact(start, end, val, filed, form="10-Q"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


# A calendar fiscal year: 10-Q income statements report the quarter AND year-to-date,
# the cash-flow statement only year-to-date; the 10-K reports the year.
INCOME = [
    fact("2025-01-01", "2025-03-31", 10, "2025-04-30"),
    fact("2025-04-01", "2025-06-30", 11, "2025-07-30"), fact("2025-01-01", "2025-06-30", 21, "2025-07-30"),
    fact("2025-07-01", "2025-09-30", 12, "2025-10-30"), fact("2025-01-01", "2025-09-30", 33, "2025-10-30"),
    fact("2025-01-01", "2025-12-31", 46, "2026-02-10", "10-K"),
    fact("2026-01-01", "2026-03-31", 14, "2026-04-30"),
]
CASHFLOW = [
    fact("2025-01-01", "2025-03-31", 5, "2025-04-30"), fact("2025-01-01", "2025-06-30", 12, "2025-07-30"),
    fact("2025-01-01", "2025-09-30", 20, "2025-10-30"), fact("2025-01-01", "2025-12-31", 30, "2026-02-10", "10-K"),
    fact("2026-01-01", "2026-03-31", 9, "2026-04-30"),
]


def test_quarters_come_from_direct_values_ytd_differences_and_annual_minus_nine_months():
    q = quarters(CASHFLOW)
    assert {end: versions[-1][1] for end, versions in q.items()} == {
        "2025-03-31": 5, "2025-06-30": 7, "2025-09-30": 8, "2025-12-31": 10, "2026-03-31": 9}
    assert {end: sorted(v)[-1][1] for end, v in quarters(INCOME).items()}["2025-12-31"] == 13  # 46 - 33.


def test_ttm_is_known_only_after_the_fourth_consecutive_quarter_is_filed():
    assert ttm_series(INCOME) == [("2026-02-10", 46), ("2026-04-30", 50)]  # Unknown until four quarters.
    assert ttm_series(CASHFLOW)[-1] == ("2026-04-30", 9 + 10 + 8 + 7)


def test_a_restatement_counts_only_from_its_own_filing_date():
    restated = INCOME + [fact("2025-10-01", "2025-12-31", 20, "2026-03-15", "10-K/A"),
                         fact("2025-01-01", "2025-12-31", 53, "2026-03-15", "10-K/A")]
    series = ttm_series(restated)
    assert ("2026-02-10", 46) in series and ("2026-03-15", 53) in series


def test_values_are_used_only_from_the_day_after_the_filing_was_accepted():
    index = pd.bdate_range("2026-02-09", "2026-02-12")
    aligned = as_of([("2026-02-10", 46.0)], index)
    assert np.isnan(aligned.iloc[0]) and np.isnan(aligned.iloc[1])  # Not on the filing day itself.
    assert aligned.iloc[2] == 46.0


def test_several_share_classes_on_one_date_are_ambiguous_not_summed():
    payload = {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {"end": "2026-04-20", "val": 1000, "filed": "2026-04-30", "form": "10-Q"},
        {"end": "2026-07-20", "val": 600, "filed": "2026-07-30", "form": "10-Q"},
        {"end": "2026-07-20", "val": 400, "filed": "2026-07-30", "form": "10-Q"}]}}}}}
    assert shares_series(payload) == [("2026-04-30", 1000.0), ("2026-07-30", None)]


def payload():
    rows = lambda facts: [dict(f) for f in facts]
    return {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": rows(INCOME)}},
                                  "StockholdersEquity": {"units": {"USD": [
                                      {"end": "2025-12-31", "val": 300, "filed": "2026-02-10", "form": "10-K"}]}}},
                      "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                          {"end": "2026-01-31", "val": 100, "filed": "2026-02-10", "form": "10-K"}]}}}}}


def test_sec_client_caches_extracts_and_reports_missing_companies(tmp_path):
    calls = []

    def get(url, headers=None, timeout=None):
        calls.append(url)
        assert "@" in headers["User-Agent"]
        body = ({"0": {"cik_str": 1234, "ticker": "ALFA", "title": "Synthetic"}} if "company_tickers" in url
                else payload())
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
    client = SecFundamentals(tmp_path, "Synthetic Operator ops@example.com", get=get, sleep=lambda s: None,
                             today=lambda: date(2026, 3, 1))
    index = pd.bdate_range("2026-02-09", "2026-02-13")
    first = client.panels({"ALFA", "NOPE"}, index)
    assert first["earnings"]["ALFA"].iloc[-1] == 46 and first["book"]["ALFA"].iloc[-1] == 300
    assert first["shares"]["ALFA"].iloc[-1] == 100 and first["earnings"]["NOPE"].isna().all()
    assert client.errors["NOPE"] == "no SEC CIK"
    client.panels({"ALFA"}, index)
    assert len(calls) == 2  # The extract is cached until a new filing is likely.
    with pytest.raises(Exception, match="FUND_SEC_USER_AGENT"):
        SecFundamentals(tmp_path, "no contact")


def test_fundamental_fields_enter_factor_expressions():
    close, volume = panel(names=10, days=60)
    earnings = pd.DataFrame(1.0, index=close.index, columns=close.columns)
    shares = pd.DataFrame(10.0, index=close.index, columns=close.columns)
    value = compute("earnings / (close * shares)", close, volume, {"earnings": earnings, "shares": shares})
    assert np.allclose(value, 1.0 / (close * 10))
    assert compute("rank(book)", close, volume).isna().all().all()  # A missing field is NaN, never guessed.
    spread = compute("(high - low) / close", close, volume, {"high": close * 1.02, "low": close * 0.99})
    assert np.allclose(spread, 0.03)


# ----- Cross-sections, costs and redundancy --------------------------------------------------

def mixed():
    """Stocks with a genuine momentum edge plus calm ETFs: a low-volatility rank across both
    only separates the ETFs from the stocks."""
    stocks, volume = panel(seed=1, names=16)
    rng = np.random.default_rng(9)
    etfs = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.003, (len(stocks), 8)), axis=0)),
                        index=stocks.index, columns=[f"E{i}" for i in range(8)])
    etf_volume = pd.DataFrame(1e6, index=stocks.index, columns=etfs.columns)
    return {"stocks": {"close": stocks, "volume": volume}, "etfs": {"close": etfs, "volume": etf_volume}}


def test_each_factor_is_tested_and_keyed_in_its_own_cross_section(tmp_path):
    lab = FactorLab(Journal(tmp_path, "synthetic-universes"), SETTINGS)
    panels = mixed()
    stocks = lab.test([proposal("rank(ret(60))")], NOW, "2026-11-02", panels)
    etfs = lab.test([{**proposal("rank(ret(60))"), "universe": "etfs"}], NOW, "2026-11-02", panels)
    assert stocks[0]["universe"] == "stocks" and stocks[0]["status"] == "validated"
    assert etfs[0]["universe"] == "etfs" and etfs[0]["status"] != "validated"  # No edge among the ETFs.
    assert lab.trials() == 2  # Same expression, different cross-section: a separate trial.
    assert lab.test([{**proposal("rank(ret(60))"), "universe": "both"}], NOW, "2026-11-02", panels)[0][
        "status"] == "invalid"


def test_factors_validated_on_a_mixed_cross_section_retire_on_upgrade(tmp_path):
    journal = Journal(tmp_path, "synthetic-legacy")
    with journal.connection() as db:
        db.execute("""CREATE TABLE fund_factors (id TEXT PRIMARY KEY, expression TEXT NOT NULL, horizon INTEGER NOT NULL,
            name TEXT NOT NULL, hypothesis TEXT NOT NULL, proposed_at TEXT NOT NULL, session_date TEXT,
            status TEXT NOT NULL, direction INTEGER, evidence TEXT NOT NULL, validated_at TEXT, retired_at TEXT,
            reason TEXT)""")
        db.execute("INSERT INTO fund_factors VALUES ('old','-vol(20)',5,'lowvol','h','2026-10-01','2026-10-01',"
                   "'validated',1,'{}','2026-10-01',NULL,'ok')")
    lab = FactorLab(journal, SETTINGS)
    (row,) = lab.rows()
    assert row["status"] == "retired" and row["universe"] == "mixed" and "mixed" in row["reason"]
    assert lab.trials() == 1  # It still raises the evidence bar.


def test_a_spread_that_does_not_survive_costs_is_rejected():
    close, volume = panel(seed=1)
    cheap = evaluate("rank(ret(60))", 5, close, volume, SETTINGS, trials=1)
    assert cheap["status"] == "validated" and cheap["net_spread"] > 0
    assert set(cheap["decay"]) == {"1", "5", "20"} and 0 < cheap["rank_autocorrelation"] <= 1
    expected = cheap["in_sample"]["mean_spread"] - 4 * cheap["quintile_turnover"] * SETTINGS.cost_bps / 10_000
    assert cheap["net_spread"] == pytest.approx(expected, abs=1e-5)
    costly = evaluate("rank(ret(60))", 5, close, volume, FactorSettings(cost_bps=100.0), trials=1)
    if cheap["in_sample"]["mean_spread"] < 4 * cheap["quintile_turnover"] * 0.01:
        assert costly["status"] == "rejected" and "costs" in costly["reason"]


def test_a_near_copy_of_a_validated_factor_is_redundant(tmp_path):
    lab = FactorLab(Journal(tmp_path, "synthetic-redundant"), SETTINGS)
    close, volume = panel(seed=1)
    lab.test([proposal("rank(ret(60))")], NOW, "2026-11-02", close, volume)
    copy = lab.test([proposal("rank(ret(58))", name="copy")], NOW, "2026-11-03", close, volume)[0]
    assert copy["status"] == "redundant" and copy["redundant_with"] == "momentum"
    assert lab.context(NOW, close, volume)["recently_rejected"][-1]["status"] == "redundant"


def test_favored_names_are_tradable_only(tmp_path):
    lab = FactorLab(Journal(tmp_path, "synthetic-tradable"), SETTINGS)
    close, volume = panel(seed=1)
    lab.test([proposal("rank(ret(60))")], NOW, "2026-11-02", close, volume)
    tradable = set(close.columns[:6])
    context = lab.context(NOW, close, volume, tradable=tradable)
    names = {r["symbol"] for r in context["validated"][0]["live"]["favored"] + context["validated"][0]["live"][
        "disfavored"]}
    assert names <= tradable


def test_research_universe_settings_validate():
    assert FactorSettings(research_universe=["ABC", "XYZ"]).research_universe == ("ABC", "XYZ")
    with pytest.raises(Exception, match="research_universe"):
        FactorSettings(research_universe=["abc"])
    with pytest.raises(Exception, match="research_universe"):
        FactorSettings(research_universe=["ABC", "ABC"])


def test_strategy_builds_separate_stock_and_etf_panels_with_research_names_and_ohlc(tmp_path):
    from tests.test_autonomous_fund_quant import harness
    days = pd.date_range("2026-10-26 16:00", periods=6, freq="D", tz="America/New_York")  # Last one is "today".
    frame = pd.DataFrame({"open": np.arange(99.0, 105.0), "high": np.arange(101.0, 107.0),
                          "low": np.arange(98.0, 104.0), "close": np.arange(100.0, 106.0), "volume": 1e6}, index=days)
    strategy = harness(tmp_path)
    strategy.universe = frozenset({"ALFA", "FUND"})
    strategy.config = {"universe": {"ALFA": "stock", "FUND": "unlevered_etf", "BILLS": "cash_equivalent"}}
    strategy.factor_lab = FactorLab(strategy.journal, FactorSettings(research_universe=["WIDE"]))
    strategy._factor_cache = None
    strategy.get_historical_prices = lambda asset, length, timestep="day": SimpleNamespace(df=frame)
    seen = {}
    strategy.sec = SimpleNamespace(errors={}, panels=lambda symbols, index: seen.setdefault("symbols", symbols) and {
        "earnings": pd.DataFrame(5.0, index=index, columns=sorted(symbols))})
    panels = strategy._factor_panel(days[-1].to_pydatetime())
    assert list(panels["stocks"]["close"].columns) == ["ALFA", "WIDE"]  # Research names widen the stocks.
    assert list(panels["etfs"]["close"].columns) == ["FUND"]  # The cash sweep is never ranked.
    assert panels["stocks"]["high"].iloc[-1, 0] == 105.0 and len(panels["stocks"]["low"]) == 5  # No forming bar.
    assert seen["symbols"] == {"ALFA", "WIDE"} and "earnings" not in panels["etfs"]
    strategy._factor_context_cache = None
    context = strategy._factor_context(days[-1].to_pydatetime())
    assert context["fundamentals"]["source"].startswith("SEC")
