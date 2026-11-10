"""Every snapshot number the manager relies on can be checked: ranks and values, against what it saw.

Synthetic snapshots only; no network, broker or model inference.
"""
import pytest

from scripts.autonomous_fund.claims import METRICS, SnapshotClaim, check_claims, score_claims
from scripts.autonomous_fund.strategy import Decision

DAY = "2026-11-09"
UNIVERSE = {"A", "B", "C", "D", "E", "F"}


def snapshot(**overrides):
    rows = {s: {"as_of_close": DAY, "vol_20d": 0.10 + 0.05 * i, "beta_60d": 0.5 + 0.3 * i,
                "pct_from_ma200": -0.04 + 0.02 * i, "adv_20d_usd": 1_000_000 * (i + 1), "ret_60d": 0.01 * i}
            for i, s in enumerate(sorted(UNIVERSE))}
    rows["STALE"] = {**rows["A"], "as_of_close": "2026-11-06"}
    return {"as_of_close": DAY, "symbols": {**rows, **overrides}}


def claim(symbol, metric, **bounds):
    return {"symbol": symbol, "metric": metric, **bounds}


def test_every_snapshot_metric_is_claimable_with_rank_or_value_bounds():
    assert {"vol_20d", "beta_60d", "pct_from_ma200", "pct_from_high_252d", "adv_20d_usd", "ret_20d"} <= set(METRICS)
    parsed = Decision.model_validate({
        "as_of": "2026-11-09T15:00:00+00:00", "expires_at": "2026-11-09T17:00:00+00:00",
        "next_review_at": "2026-11-09T16:00:00+00:00", "rationale": "synthetic", "targets": [],
        "factual_claims": [claim("A", "beta_60d", minimum=1), claim("B", "vol_20d", maximum_percentile=20)]})
    assert parsed.factual_claims[0].minimum == 1.0 and parsed.factual_claims[1].maximum_percentile == 20
    with pytest.raises(Exception):
        SnapshotClaim.model_validate(claim("A", "pe_ratio", minimum=1))


def test_rank_value_and_combined_claims_are_checked_against_the_snapshot_seen():
    results = check_claims([
        claim("A", "vol_20d", maximum_percentile=20),          # Lowest volatility of six: the 16.67th percentile.
        claim("F", "vol_20d", maximum_percentile=20),          # Highest: wrong.
        claim("F", "beta_60d", minimum=1.5),                   # 2.0: right.
        claim("A", "beta_60d", minimum=1.5),                   # 0.5: wrong.
        claim("D", "pct_from_ma200", minimum=0.0),             # +0.02: above its 200-session mean.
        claim("E", "adv_20d_usd", minimum_percentile=60, minimum=4_000_000),  # Both bounds hold.
        claim("E", "adv_20d_usd", minimum_percentile=90, minimum=4_000_000),  # The rank bound fails.
    ], snapshot(), UNIVERSE)
    assert [r["status"] for r in results] == ["correct", "incorrect", "correct", "incorrect", "correct", "correct",
                                              "incorrect"]
    assert results[0]["actual_percentile"] == 16.67 and "actual_value" not in results[0]
    assert results[2]["actual_value"] == pytest.approx(2.0) and "actual_percentile" not in results[2]
    assert results[5]["actual_percentile"] == 83.33 and results[5]["actual_value"] == 5_000_000


def test_missing_evidence_and_boundless_claims_are_never_scored_as_right_or_wrong():
    small = {"as_of_close": DAY, "symbols": {s: snapshot()["symbols"][s] for s in ("A", "B")}}
    results = check_claims([
        claim("STALE", "beta_60d", minimum=0.1),               # A row from another session.
        claim("Z", "beta_60d", minimum=0.1),                   # Not in the snapshot.
        claim("A", "beta_60d"),                                # No bound stated.
    ], snapshot(), UNIVERSE | {"STALE", "Z"})
    assert [r["status"] for r in results] == ["unavailable", "unavailable", "no_bound"]
    # A value bound needs no peers; a rank bound needs at least five.
    assert [r["status"] for r in check_claims([claim("A", "beta_60d", minimum=0.1),
                                               claim("A", "beta_60d", minimum_percentile=1)], small, UNIVERSE)] \
        == ["correct", "unavailable"]
    assert check_claims([claim("A", "beta_60d", minimum=0.1)], {**snapshot(), "deferred": ["B"]},
                        UNIVERSE)[0]["status"] == "unavailable"
    # The earlier momentum form keeps working unchanged.
    [momentum] = check_claims([{"symbol": "F", "metric": "ret_60d", "minimum_percentile": 80}], snapshot(), UNIVERSE)
    assert momentum["status"] == "correct" and momentum["actual_percentile"] == 100.0


def test_scores_group_checked_claims_by_metric_and_reports_name_the_weak_ones():
    from scripts.autonomous_fund.notifications import evidence_lines
    checks = check_claims([claim("A", "vol_20d", maximum_percentile=20), claim("F", "vol_20d", maximum_percentile=20),
                           claim("F", "beta_60d", minimum=1.5), claim("A", "beta_60d")], snapshot(), UNIVERSE)
    card = score_claims([("t", {"claim_checks": checks}), ("t2", {})])
    assert card["by_metric"] == {"beta_60d": {"correct": 1, "incorrect": 0}, "vol_20d": {"correct": 1, "incorrect": 1}}
    assert (card["correct"], card["incorrect"], card["unavailable"]) == (2, 1, 1)
    [line] = [text for text in evidence_lines({"claims": card}) if text.startswith("AI 事实核验")]
    assert "对照它看到的行情快照" in line and "vol_20d 1/2" in line and "beta_60d" not in line.split("错得多的指标")[1]
