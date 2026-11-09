"""Offline regression tests for claims, crowding and regime uncertainty."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.autonomous_fund.claims import check_claims
from scripts.autonomous_fund.crowding import diagnose
from scripts.autonomous_fund.notifications import evidence_lines, regime_trend_lines
from scripts.autonomous_fund.risk import RiskError
from scripts.autonomous_fund.scorecard import _idea_regimes, score_decisions
from scripts.autonomous_fund.short_volume import HEADER, parse, read


def test_finra_job_uses_last_published_session_and_rejects_future_and_truncated_data(tmp_path):
    from scripts.autonomous_fund.datadesk import DataDesk
    from tests.test_autonomous_fund_datadesk import config

    friday = "2026-11-06"
    before_publication = datetime(2026, 11, 9, 22, 59, tzinfo=timezone.utc)
    friday_text = f"{HEADER}\n20261106|ALFA|40.5|10|81|N\n20261106|BETA|100|0|10|Q\n2\n"
    requests = []

    def get(url, timeout):
        requests.append(url)
        return SimpleNamespace(text=friday_text, raise_for_status=lambda: None)

    desk = DataDesk(tmp_path, config(universe={"ALFA": "stock", "BETA": "stock"}, quant_risk={}), "paper", environ={},
                    short_volume_get=get)
    desk._job_short_volume(before_publication)
    desk._job_short_volume(before_publication + timedelta(seconds=1))
    assert len(requests) == 1 and requests[0].endswith("CNMSshvol20261106.txt")
    data = read(tmp_path / "cache", before_publication)
    assert data["session_date"] == friday and data["ratios"] == {"ALFA": 0.5}
    assert read(tmp_path / "cache", before_publication - timedelta(seconds=1)) is None
    with pytest.raises(RiskError, match="record count"):
        parse(friday_text.rsplit("2", 1)[0], friday, {"ALFA"})
    with pytest.raises(RiskError, match="trade date"):
        desk._job_short_volume(before_publication + timedelta(minutes=2))
    assert requests[-1].endswith("CNMSshvol20261109.txt")
    assert read(tmp_path / "cache", before_publication + timedelta(minutes=2))["session_date"] == friday


def test_factor_quintiles_show_valuation_correlation_and_dated_finra_coverage():
    names = [f"N{i}" for i in range(10)]
    changes = np.tile([0.01, -0.005, 0.02], 22)[:60]
    base = np.r_[100, 100 * np.cumprod(1 + changes)]
    closes = pd.DataFrame({s: base * (1 + i) for i, s in enumerate(names)},
                          index=pd.bdate_range(end="2026-11-06", periods=61))
    shares = pd.DataFrame(100.0, index=closes.index, columns=names)
    book = closes * shares * pd.Series({s: (i + 1) / 10 for i, s in enumerate(names)})
    ranks = pd.Series(range(10), index=names)
    finra = {"session_date": "2026-11-06", "ratios": {"N8": .2, "N9": .4, "N0": .6}}
    result = diagnose(ranks, closes, {"book": book, "shares": shares}, finra)
    assert result["book_to_price_spread"] == pytest.approx(.8)
    assert result["favored"]["mean_pairwise_return_correlation"] == pytest.approx(1)
    assert result["favored"]["mean_finra_short_volume_ratio"] == pytest.approx(.3)
    assert result["disfavored"]["mean_finra_short_volume_ratio"] is None
    assert diagnose(ranks, closes, {}, {**finra, "session_date": "2026-11-09"})["finra_session"] is None
    assert diagnose(ranks * 0, closes, {})["status"] == "unavailable"
    [line] = evidence_lines({"factor_crowding": [{"name": "Synthetic", **result}]})
    assert "FINRA" in line and "2026-11-06" in line and "覆盖 2／1" in line


def test_manager_claim_verdict_is_saved_and_scored_against_the_snapshot_it_saw(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, account, decision, model_harness

    strategy = model_harness(tmp_path)
    ordered = ["ALFA", "BETA", "GAMMA", "DELTA", "SPY"]
    assert set(ordered) <= strategy.universe
    day = "2026-10-30"
    snapshot = {"as_of_close": day, "symbols": {s: {"as_of_close": day, "ret_60d": i / 10}
                                                for i, s in enumerate(ordered)}}
    claim = {"symbol": "ALFA", "metric": "ret_60d", "minimum_percentile": 80}
    strategy._market_snapshot = lambda now: snapshot

    def call(name, **kwargs):
        if name == "researcher":
            return {"summary": "synthetic", "evidence": [{"source": "fixture",
                    "available_at": (NOW - timedelta(days=1)).isoformat(), "observed_at": NOW.isoformat(),
                    "estimated": False, "estimate_reason": ""}]}
        return {**decision([("ALFA", .05)]), "factual_claims": [claim]}

    strategy._agent_call = call
    strategy._decide(account(), NOW, False)
    [event] = strategy.journal.events(("decision_validated",))
    assert event["body"]["claim_checks"][0]["actual_percentile"] == 20
    snapshot["symbols"]["ALFA"]["ret_60d"] = 1  # A later market read cannot rewrite the verdict.
    assert strategy._scorecards(NOW)["claims"]["incorrect_rate"] == 1.0
    assert check_claims([claim], {**snapshot, "deferred": ["SPY"]}, strategy.universe)[0]["status"] == "unavailable"
    assert len(check_claims([claim, claim], snapshot, strategy.universe)) == 1


def test_regime_intervals_require_enough_non_overlapping_observations():
    dates = [d.date().isoformat() for d in pd.bdate_range("2025-01-02", periods=80)]
    market = [(d, 100 + i) for i, d in enumerate(dates)]
    stock = [(d, 100 + i + i % 7) for i, d in enumerate(dates)]
    decisions = [(f"{d}T15:00:00+00:00", {"targets": [{"symbol": "ALFA", "weight": 1}]}) for d in dates[:65]]
    regimes = dict.fromkeys(dates, "up_calm")
    group = score_decisions(decisions, {"SPY": market, "ALFA": stock}, regimes=regimes)["by_regime"]["up_calm"]
    assert group["5"]["observed"] == 65 and group["5"]["uncertainty"]["non_overlapping"] == 13
    interval = group["5"]["uncertainty"]
    assert interval["interval_95"][0] < interval["sample_mean"] < interval["interval_95"][1]
    assert group["20"]["uncertainty"]["status"] == "unreliable"
    ideas = [{"start_day": dates[i], "end_day": dates[i + 2]} for i in range(0, 24, 2) for _ in range(3)]
    grouped = _idea_regimes(ideas, [True] * len(ideas), regimes)["up_calm"]
    assert grouped["scored"] == 36 and grouped["uncertainty"]["non_overlapping"] == 12
    assert 0 < grouped["uncertainty"]["interval_95"][0] < 1 == grouped["uncertainty"]["interval_95"][1]
    lines = regime_trend_lines({"decisions": {"by_regime": {"up_calm": group}}})
    assert any("95% 区间" in line and "非重叠样本 13" in line for line in lines)
