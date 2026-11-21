"""Regression coverage for offline factor portfolios with synthetic dated bars."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas_market_calendars as mcal
import pytest

from scripts.autonomous_fund.factor_replay import study
from scripts.autonomous_fund.factors import FactorSettings
from scripts.autonomous_fund.lab import main as strategy_lab
from scripts.autonomous_fund.risk import RiskError, instant
from scripts.autonomous_fund.run import main
from scripts.autonomous_fund.strategy_validation import COVERAGE_FIELDS, digest


@pytest.fixture
def inputs():
    schedule = mcal.get_calendar("NYSE").schedule(start_date="2025-01-02", end_date="2025-04-02").iloc[:60]
    bars = {s: [] for s in ("AAA", "BBB", "CCC")}
    for i, (day, session) in enumerate(schedule.iterrows()):
        for symbol, trend in zip(bars, (0.4, 0.1, -0.1)):
            price = 50 + i * trend
            bars[symbol].append({"day": str(day.date()), "at": session.market_close.isoformat(),
                                 "open_at": session.market_open.isoformat(), "available_at": session.market_close.isoformat(),
                                 "open": price - .25, "close": price, "high": price + .25,
                                 "low": price - .5, "volume": 1_000_000})
    data = {"source": "Synthetic data", "bars": bars, "actions": [], "coverage": {}}
    spec = {"instruments": ["AAA", "BBB", "CCC"], "start": bars["AAA"][25]["day"], "end": bars["AAA"][-1]["day"],
            "capital": "10000", "direction": 1, "top_n": 1, "rebalance_sessions": 5,
            "benchmark": {"BBB": "0.5", "CCC": "0.5"}, "benchmark_rationale": "Same gross equity budget",
            "target_exposure": "0.5", "cash_annual_rate": "0.04", "signal_delay_minutes": 15,
            "fee_bps": "2", "spread_bps": "4", "slippage_bps": "3"}
    config = {"model": "deepseek/deepseek-flash", "universe": dict.fromkeys(bars, "stock"),
              "limits": {"max_position": ".40", "max_exposure": ".60"},
              "quant_risk": {"groups": {"synthetic_group": {"members": ["AAA"], "limit": ".3"}}}}
    return data, spec, config


def run(inputs, **kwargs):
    data, spec, config = inputs
    return study("rank(ret(2))", 5, FactorSettings(), spec, data, config,
                 datetime(2026, 1, 1, tzinfo=timezone.utc), **kwargs)


def test_same_factor_reports_a_paired_whole_share_replay(inputs):
    data, spec, _ = inputs
    result = run(inputs)
    assert set(result["qualification"]) == {"predictive", "theoretical_long_short", "tradable", "economic_alpha"}
    assert result["qualification"]["tradable"]["status"] == "modeled_long_only"
    assert result["qualification"]["economic_alpha"]["status"] == "economic_evidence_insufficient"
    replay = result["long_only_replay"]
    asset, base = replay["factor"]["daily"], replay["benchmark"]["daily"]
    assert replay["status"] == "modeled" and replay["net_excess_return"] > 0
    assert {"corporate_actions_complete", "point_in_time_membership", "publication_times_complete"} <= set(
        replay["source_receipt_gaps"])
    first_row = data["bars"]["AAA"][25]
    assert asset[0]["day"] == first_row["day"] == base[0]["day"]
    assert instant(asset[0]["signal_as_of"]) < instant(first_row["open_at"])
    assert Decimal(asset[0]["targets"]["AAA"]) == sum(map(Decimal, base[0]["targets"].values()))
    trade, = asset[0]["trades"]
    assert trade["symbol"] == "AAA" and trade["side"] == "buy" and type(trade["quantity"]) is int
    assert Decimal(trade["price"]) == Decimal(str(first_row["open"])) * Decimal("1.0005")
    assert not asset[1]["scheduled_rebalance"]
    assert all(trade["side"] == "sell" for row in asset if not row["scheduled_rebalance"] for trade in row["trades"])
    assert Decimal(asset[1]["cash_interest"]) > 0
    assert all(Decimal(row["cash"]) >= 0 and Decimal(row["open_exposure"]) <= Decimal(".30") for row in asset)
    assert replay["protocol_sha256"] == digest(replay["protocol"])
    spec["fee_bps"] = "4"
    assert run(inputs)["long_only_replay"]["protocol_sha256"] != replay["protocol_sha256"]


def test_same_day_liquidity_cannot_rescue_a_non_executable_rebalance(inputs):
    data, _, config = inputs
    config["limits"]["min_dollar_volume"] = "10"
    for row in data["bars"]["AAA"][5:25]:
        row["volume"] = 10
    data["bars"]["AAA"][25]["volume"] = 1_000_000_000
    result = run(inputs)
    replay = result["long_only_replay"]
    assert replay["status"] == "incomplete" and "lagged dollar-volume capacity" in replay["reason"]
    assert "net_excess_return" not in replay and "factor" not in replay
    assert result["qualification"]["tradable"]["status"] == "insufficient_evidence"


def test_an_execution_days_close_cannot_select_that_opens_holdings(inputs):
    data, _, _ = inputs
    before = run(inputs)["long_only_replay"]["factor"]["daily"][0]
    open_day = data["bars"]["AAA"][25]
    open_day["close"], open_day["low"] = 52., 51.
    after = run(inputs)["long_only_replay"]["factor"]["daily"][0]
    assert before["targets"] == after["targets"]
    assert before["trades"] == after["trades"]
    assert before["equity"] != after["equity"]


@pytest.mark.parametrize("gap", ["missing_session", "late_close"])
def test_replay_cannot_skip_market_sessions_or_read_a_late_close(inputs, gap):
    data, _, _ = inputs
    if gap == "missing_session":
        del data["bars"]["BBB"][24]
        expected = "missing exchange sessions"
    else:
        close = data["bars"]["AAA"][24]
        close["feature_available_at"] = {"close": (instant(close["at"]) + timedelta(minutes=16)).isoformat()}
        expected = "unavailable at the signal cutoff"
    with pytest.raises(RiskError, match=expected):
        run(inputs)


def test_split_uses_raw_open_and_dividend_cannot_fund_purchases_before_pay_date(inputs):
    data, _, _ = inputs
    bars = data["bars"]["AAA"]
    for row in bars[26:]:
        for field in ("open", "close", "high", "low"):
            row[field] /= 2
        row["volume"] *= 2
    data["actions"] = [{"symbol": "AAA", "ex_date": bars[26]["day"], "available_at": bars[25]["at"], "split": 2,
                        "dividend": .4, "pay_date": bars[29]["day"]}]
    rows = run(inputs)["long_only_replay"]["factor"]["daily"]
    shares = rows[0]["positions"]["AAA"]
    assert rows[1]["positions"]["AAA"] == shares * 2 and not rows[1]["trades"]
    assert Decimal(rows[1]["receivables"][0]["amount"]) == shares * 2 * Decimal(".4")
    assert Decimal(rows[1]["cash"]) - Decimal(rows[0]["cash"]) == pytest.approx(
        Decimal(rows[1]["cash_interest"]))
    assert rows[4]["receivables"] == []
    sales = sum(Decimal(trade["price"]) * trade["quantity"] - Decimal(trade["trade_cost"])
                for trade in rows[4]["trades"] if trade["side"] == "sell")
    assert Decimal(rows[4]["cash"]) - Decimal(rows[3]["cash"]) == pytest.approx(
        shares * 2 * Decimal(".4") + Decimal(rows[4]["cash_interest"]) + sales)


def test_cli_checks_original_bytes_and_cannot_promote_historical_replay(inputs, tmp_path, capsys):
    data, spec, config = inputs
    data["coverage"] = dict.fromkeys(COVERAGE_FIELDS, True)
    originals = tmp_path / "originals"
    originals.mkdir()
    source = b"synthetic original evidence"
    sha = hashlib.sha256(source).hexdigest()
    (originals / sha).write_bytes(source)
    data["provenance"] = {"snapshot_sha256": digest(data),
                          "claims": {k: {"source": "fixture", "reference": k, "artifact_sha256": sha}
                                     for k in COVERAGE_FIELDS}}
    for name, value in (("snapshot", data), ("plan", spec), ("config", config)):
        (tmp_path / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    main(["factor-lab", "--expression", "rank(ret(2))", "--factor-replay", str(tmp_path / "plan.json"),
          "--dataset", str(tmp_path / "snapshot.json"), "--config", str(tmp_path / "config.json"),
          "--artifacts-dir", str(originals)])
    output = capsys.readouterr().out
    result = json.loads(output[output.index("{"):])
    assert result["long_only_replay"]["source_artifacts_matched"]
    assert result["qualification"]["economic_alpha"]["status"] == "economic_evidence_insufficient"
    (originals / sha).write_bytes(source + b" altered")
    assert "source_artifact_mismatch:raw_prices" in run(inputs, artifacts_dir=originals)["long_only_replay"][
        "source_receipt_gaps"]


def test_snapshot_audit_surfaces_partial_calendar_and_unknown_coverage(inputs, tmp_path, capsys):
    data, _, _ = inputs
    removed = data["bars"]["BBB"].pop(24)["day"]
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    strategy_lab(["audit-data", "--dataset", str(path)])
    output = json.loads(capsys.readouterr().out)
    assert output["history"]["BBB"]["missing_sessions"] == [removed]
    assert output["coverage"]["point_in_time_membership"] == {"declared_complete": False, "receipt": None}
    assert "corporate_actions_complete" in output["receipt_gaps"]
    assert not output["source_artifacts_matched_this_check"]
    assert set(tmp_path.iterdir()) == {path}
