"""The investment universe: wider defaults, .env additions, the leveraged/inverse guard and selling rules.

Configuration only; no network, broker or model inference.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.autonomous_fund.risk import LEVERAGED_OR_INVERSE, RiskError
from scripts.autonomous_fund.strategy import MANAGER_PROMPT, configuration

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "scripts" / "autonomous_fund" / "config.json"


def test_the_default_universe_is_wide_sector_capped_and_free_of_leveraged_products():
    config = configuration(CONFIG, environ={})
    universe = config["universe"]
    kinds = [kind for kind in universe.values()]
    assert (len(universe), kinds.count("stock"), kinds.count("unlevered_etf"), kinds.count("cash_equivalent")) == \
        (76, 56, 19, 1)
    members = {m for group in config["quant_risk"]["groups"].values() for m in group["members"]}
    # Every stock and sector ETF counts against a sector cap; only gold and the cash sweep stand alone.
    assert sorted(set(universe) - members) == ["GLD", "SGOV"]
    research = config["factor_lab"]["research_universe"]
    assert len(research) == 40 and not set(research) & set(universe)
    assert not set(universe) & LEVERAGED_OR_INVERSE and not set(research) & LEVERAGED_OR_INVERSE


def test_env_adds_tradable_and_research_symbols_with_optional_sector_groups():
    config = configuration(CONFIG, environ={
        "FUND_EXTRA_STOCKS": " shop:technology, UBER ,SHOP", "FUND_EXTRA_ETFS": "SMH:technology",
        "FUND_RESEARCH_SYMBOLS": "SNOW, crwd", "FUND_EXCLUDE_SYMBOLS": "UBER"})
    universe, groups = config["universe"], config["quant_risk"]["groups"]
    assert universe["SHOP"] == "stock" and universe["SMH"] == "unlevered_etf" and "UBER" not in universe
    assert {"SHOP", "SMH"} <= set(groups["technology"]["members"])
    assert config["factor_lab"]["research_universe"][-2:] == ["SNOW", "CRWD"]
    audit = {row["variable"]: row["value"] for row in config["_environment_overrides"]}
    assert audit["FUND_EXTRA_STOCKS"] == ["SHOP:technology", "UBER"] and audit["FUND_RESEARCH_SYMBOLS"] == ["SNOW", "CRWD"]


@pytest.mark.parametrize("environ, message", [
    ({"FUND_EXTRA_ETFS": "TQQQ"}, "leveraged, inverse or volatility"),
    ({"FUND_EXTRA_STOCKS": "TSLL"}, "leveraged, inverse or volatility"),  # A leveraged single-stock ETF.
    ({"FUND_RESEARCH_SYMBOLS": "UVXY"}, "leveraged, inverse or volatility"),
    ({"FUND_EXTRA_STOCKS": "SHOP:tech"}, "unknown group tech"),
    ({"FUND_EXTRA_STOCKS": "SPY"}, "already admitted as unlevered_etf"),
    ({"FUND_EXTRA_STOCKS": "BAD SYMBOL"}, "not a ticker symbol"),
    ({"FUND_RESEARCH_SYMBOLS": "SNOW:technology"}, "takes no group"),
])
def test_env_additions_are_validated(environ, message):
    with pytest.raises(RiskError, match=message):
        configuration(CONFIG, environ=environ)


def test_a_leveraged_product_in_config_json_is_refused(tmp_path):
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    config["universe"]["SQQQ"] = "unlevered_etf"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(RiskError, match="cannot be admitted: SQQQ"):
        configuration(path, environ={})


def test_the_desk_pulse_stretches_with_the_universe_instead_of_raising_the_request_rate(tmp_path):
    from scripts.autonomous_fund.datadesk import GAUGES, INTRADAY_SECONDS_PER_SYMBOL, DataDesk
    now = datetime(2026, 11, 4, 15, tzinfo=timezone.utc)
    narrow = configuration(CONFIG, environ={"FUND_EXCLUDE_SYMBOLS": ",".join(
        s for s in json.loads(CONFIG.read_text())["universe"] if s not in {"SPY", "QQQ", "AAPL", "SGOV"})})
    assert DataDesk(tmp_path / "a", narrow, "local", clock=lambda: now, environ={}).interval("intraday", now) == 300
    wide = DataDesk(tmp_path / "b", configuration(CONFIG, environ={}), "local", clock=lambda: now, environ={})
    symbols = len(set(wide.tradable) | set(GAUGES))
    assert wide.interval("intraday", now) == round(symbols * INTRADAY_SECONDS_PER_SYMBOL) > 300


def test_only_borrowed_share_short_selling_is_banned_never_ordinary_selling():
    assert "no short selling (selling borrowed shares)" in MANAGER_PROMPT
    assert "Selling what you hold is ordinary and never restricted" in MANAGER_PROMPT
    rule = json.loads(CONFIG.read_text(encoding="utf-8"))["mandate"]["instructions"][1]
    assert "融券卖空（借股票卖出）" in rule and "卖出、减仓、清仓自己持有的证券不受限制" in rule
    example = (ROOT / "scripts" / "autonomous_fund" / ".env.example").read_text(encoding="utf-8")
    assert "融券卖空（借股票卖出）" in example and "FUND_EXTRA_STOCKS" in example
