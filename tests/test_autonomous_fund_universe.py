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
    ({"FUND_EXTRA_ETFS": "TQQQ"}, "admit it with FUND_EXTRA_LEVERAGED_ETFS"),
    ({"FUND_EXTRA_STOCKS": "TSLL"}, "admit it with FUND_EXTRA_LEVERAGED_ETFS"),  # A leveraged single-stock ETF.
    ({"FUND_EXTRA_LEVERAGED_ETFS": "SPY"}, "already admitted as unlevered_etf"),
    ({"FUND_EXTRA_LEVERAGED_ETFS": "SH", "FUND_MAX_LEVERAGED_POSITION": "0.2"}, "cannot exceed max_leveraged_total"),
    ({"FUND_RESEARCH_SYMBOLS": "UVXY"}, "leveraged, inverse or volatility"),
    ({"FUND_EXTRA_STOCKS": "SHOP:tech"}, "unknown group tech"),
    ({"FUND_EXTRA_STOCKS": "SPY"}, "already admitted as unlevered_etf"),
    ({"FUND_EXTRA_STOCKS": "BAD SYMBOL"}, "not a ticker symbol"),
    ({"FUND_RESEARCH_SYMBOLS": "SNOW:technology"}, "takes no group"),
])
def test_env_additions_are_validated(environ, message):
    with pytest.raises(RiskError, match=message):
        configuration(CONFIG, environ=environ)


def test_a_known_leveraged_product_never_passes_as_an_ordinary_stock_or_etf(tmp_path):
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    config["universe"]["SQQQ"] = "unlevered_etf"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(RiskError, match="only as leveraged_etf .* SQQQ"):
        configuration(path, environ={})
    config["universe"]["SQQQ"] = "leveraged_etf"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert configuration(path, environ={})["universe"]["SQQQ"] == "leveraged_etf"


def test_admitted_leveraged_products_get_a_combined_and_a_per_symbol_cap():
    from scripts.autonomous_fund.quant import QuantPolicy
    from scripts.autonomous_fund.risk import Limits
    config = configuration(CONFIG, environ={"FUND_EXTRA_LEVERAGED_ETFS": "SH, SDS:us_broad_index, UVXY",
                                            "FUND_MAX_LEVERAGED_TOTAL": "0.12", "FUND_MAX_LEVERAGED_POSITION": "0.04"})
    assert [config["universe"][s] for s in ("SH", "SDS", "UVXY")] == ["leveraged_etf"] * 3
    policy = QuantPolicy.from_config(config["quant_risk"], config["universe"], Limits(**config["limits"]))
    groups = {name: (limit, set(members)) for name, limit, members in policy.groups}
    assert groups["leveraged_inverse_volatility"] == ("0.12", {"SDS", "SH", "UVXY"})
    assert groups["leveraged:UVXY"] == ("0.04", {"UVXY"}) and "SDS" in groups["us_broad_index"][1]
    assert "SH" not in policy.etf_symbols  # The stricter single-stock risk budget applies.
    plain = QuantPolicy.from_config(configuration(CONFIG, environ={})["quant_risk"],
                                    configuration(CONFIG, environ={})["universe"], Limits())
    assert not any(name.startswith("leveraged") for name, _, _ in plain.groups)  # Off unless admitted.


def test_construction_holds_leveraged_weights_to_their_caps():
    from tests.test_autonomous_fund_quant import LIMITS, closes, rows
    from scripts.autonomous_fund.quant import QuantPolicy, RiskEstimate, construct
    from scripts.autonomous_fund.risk import number
    universe = {"ALFA": "stock", "SPY": "unlevered_etf", "SH": "leveraged_etf", "SDS": "leveraged_etf"}
    policy = QuantPolicy.from_config({"max_leveraged_total": "0.06", "max_leveraged_position": "0.04",
                                      "max_name_risk": "0.5", "target_portfolio_vol": "0.5", "max_portfolio_es": "0.2"},
                                     universe, LIMITS)
    frames = {s: rows(closes(.008, seed)) for seed, s in enumerate(universe)}
    targets = {"ALFA": number("0.1"), "SPY": number("0.1"), "SH": number("0.1"), "SDS": number("0.1")}
    weights, report = construct(targets, {}, RiskEstimate(frames, policy), policy, LIMITS, LIMITS.max_exposure)
    assert float(weights["SH"]) <= 0.04 + 1e-9 and float(weights["SDS"]) <= 0.04 + 1e-9
    assert float(weights["SH"] + weights["SDS"]) <= 0.06 + 1e-9
    assert any("leveraged" in label for label in report["binding"])


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
    assert "no borrowing to buy (margin)" in MANAGER_PROMPT and "only when listed in leveraged_etfs" in MANAGER_PROMPT
    rule = json.loads(CONFIG.read_text(encoding="utf-8"))["mandate"]["instructions"][1]
    assert "不借钱买入（融资）" in rule and "不借股票卖出（融券卖空）" in rule
    assert "卖出、减仓、清仓自己持有的证券不受限制" in rule and "杠杆、反向和波动率 ETF 只能用投资范围中列出的" in rule
    example = (ROOT / "scripts" / "autonomous_fund" / ".env.example").read_text(encoding="utf-8")
    assert rule in example and "FUND_EXTRA_LEVERAGED_ETFS" in example
