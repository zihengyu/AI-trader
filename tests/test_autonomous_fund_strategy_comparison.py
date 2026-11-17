"""Compare archived books on the exact sessions and evidence they share."""
import math
from datetime import timedelta

import pytest

from scripts.autonomous_fund.journal import encode
from scripts.autonomous_fund.lab import report as operator_report
from scripts.autonomous_fund.strategy_competition import compare
from tests.test_autonomous_fund_strategy_lab import NOW, dataset, lab, proposal


def nav(days, returns):
    equity = 10000.0
    rows = []
    for day, change in zip(days, returns, strict=True):
        equity *= 1 + change
        rows.append({"day": day, "net_economic_equity": str(equity)})
    return rows


def save_forward(engine, key, own, passive, *, replayed=(), at=NOW, late=()):
    engine.submit(proposal(key), NOW - timedelta(days=1))
    body = {"series": own, "reference_series": passive, "status": "forward",
            "coverage_complete": True, "prospective_sessions": len(own) - len(replayed),
            "replayed_sessions": len(replayed)}
    with engine.journal.connection() as db:
        db.execute("INSERT INTO fund_strategy_jobs VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)",
                   (key, key, "v1", at.isoformat(), "forward", key, "finished"))
        db.execute("INSERT INTO fund_strategy_results VALUES(?,?,?)", (key, at.isoformat(), encode(body)))
        for row in own:
            day = row["day"]
            evidence = "replay" if day in replayed else "prospective"
            receipt_at = at + timedelta(microseconds=1) if day in late else at
            db.execute("INSERT INTO fund_strategy_forward VALUES(?,?,?,?,?,?)",
                       (key, "v1", day, receipt_at.isoformat(), key, encode({"evidence": evidence, "receipt": row})))


@pytest.fixture(scope="module")
def market():
    days = [row["day"] for row in dataset(45)["bars"]["BASE"]]
    returns = [.018 if index % 2 == 0 else -.010 for index in range(len(days))]
    return days, returns


def test_manager_and_operator_see_risk_matched_performance_on_later_sessions(tmp_path, market):
    days, market_returns = market
    engine = lab(tmp_path)
    passive = nav(days, market_returns)
    market_only = [2 * value for value in market_returns]
    later_edge = [value + (.001 if i >= 21 else 0) for i, value in enumerate(market_only)]
    save_forward(engine, "market-exposure", nav(days, market_only), passive)
    save_forward(engine, "later-effect", nav(days, later_edge), passive)

    comparison = engine.context(NOW)["competition"]
    entries = {item["strategy_id"]: item for item in comparison["strategies"]}
    same_risk = entries["market-exposure"]["risk_matched"]
    later = entries["later-effect"]["risk_matched"]
    assert entries["market-exposure"]["net_excess"] > 0
    assert same_risk["beta_to_passive_reference"] == pytest.approx(2)
    assert later["beta_to_passive_reference"] == pytest.approx(2)
    assert later["calibration_end"] == days[20] and later["evaluation_start"] == days[21]
    assert same_risk["net_difference_at_frozen_beta"] == pytest.approx(0, abs=1e-12)
    expected = math.prod(1 + value for value in later_edge[21:]) - math.prod(1 + value for value in market_only[21:])
    assert later["net_difference_at_frozen_beta"] == pytest.approx(expected)
    assert later["evaluation_sessions"] == 24
    correlation, = comparison["correlations"]
    assert correlation["paired_sessions"] == 44 and correlation["correlation"] > .99
    assert any("固定 Beta 2.00" in line for line in operator_report(tmp_path))
    assert engine.journal.pending() == []


def test_replayed_or_later_receipt_breaks_the_prospective_comparison(tmp_path, market):
    days, market_returns = market
    engine = lab(tmp_path)
    own = nav(days, market_returns)
    save_forward(engine, "recorded", own, own)
    save_forward(engine, "backfilled", own, own, replayed=[days[10]])
    save_forward(engine, "later-receipt", own, own, late=[days[3]])
    save_forward(engine, "future-result", own, own, at=NOW + timedelta(microseconds=1))

    comparison = compare(engine.journal, NOW)
    entries = {item["strategy_id"]: item for item in comparison["strategies"]}
    assert "future-result" not in entries
    assert entries["backfilled"]["risk_matched"]["prospective_prefix_sessions"] == 10
    assert entries["later-receipt"]["risk_matched"]["prospective_prefix_sessions"] == 3
    assert entries["backfilled"]["observations"] == 45
    assert entries["backfilled"]["return_basis"] == "includes_unconfirmed_sessions"
    assert "net_difference_at_frozen_beta" not in entries["backfilled"]["risk_matched"]
    assert comparison["correlations"] == []


def test_equal_lengths_and_shared_endpoints_do_not_repair_missing_sessions(tmp_path, market):
    days, market_returns = market
    engine = lab(tmp_path)
    series = nav(days, market_returns)
    baseline = [dict(row) for row in series]
    baseline[2]["day"] = days[3]
    save_forward(engine, "misaligned", series, baseline)
    skipped = series[:10] + series[11:]
    save_forward(engine, "shared-gap", skipped, skipped)
    save_forward(engine, "complete", series, series)

    comparison = compare(engine.journal, NOW)
    assert [item["strategy_id"] for item in comparison["strategies"]] == ["complete"]
    assert {item["strategy"]: item["reason"] for item in comparison["unavailable"]} == {
        "misaligned / v1": "unpaired_forward_sessions", "shared-gap / v1": "nonconsecutive_exchange_sessions"}


def test_flat_reference_does_not_create_a_beta_or_risk_adjusted_return(tmp_path, market):
    days, market_returns = market
    engine = lab(tmp_path)
    save_forward(engine, "no-benchmark-variance", nav(days, market_returns), nav(days, [0] * len(days)))
    entry, = compare(engine.journal, NOW)["strategies"]
    assert entry["risk_matched"]["status"] == "undefined_benchmark_beta"
    assert "beta_to_passive_reference" not in entry["risk_matched"]
