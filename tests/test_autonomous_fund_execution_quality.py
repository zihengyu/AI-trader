"""Execution evidence keeps quote chronology, censored orders and fees explicit."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund.execution_quality import analyze, lines, prior_cost_reference

NOW = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)


def database():
    db = sqlite3.connect(":memory:")
    db.executescript("""
      CREATE TABLE events(sequence INTEGER PRIMARY KEY, at TEXT, kind TEXT, body TEXT);
      CREATE TABLE fund_orders(intent_id TEXT PRIMARY KEY, submitted_at TEXT, reference TEXT);
      CREATE TABLE fund_order_quotes(intent_id TEXT PRIMARY KEY, bid TEXT, ask TEXT, as_of TEXT, observed_at TEXT);
    """)
    return db


def ev(db, kind, body, seconds=0):
    db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)",
               ((NOW + timedelta(seconds=seconds)).isoformat(), kind, json.dumps(body)))


def order(db, key="a", side="buy", quantity=10, bid=98, ask=100, seconds=0):
    body = {"broker_id": key, "intent_id": key, "symbol": "ALFA", "side": side, "quantity": quantity}
    ev(db, "order_submitted", body, seconds)
    stamp = (NOW + timedelta(seconds=seconds)).isoformat()
    db.execute("INSERT INTO fund_orders VALUES(?,?,?)", (key, stamp, "999"))
    if bid is not None:
        db.execute("INSERT INTO fund_order_quotes VALUES(?,?,?,?,?)", (key, str(bid), str(ask), stamp, stamp))


def fill(db, key="a", side="buy", quantity=10, price=101, fee=1, seconds=1, execution="one", cumulative=False):
    ev(db, "fill_reconciled" if cumulative else "fill",
       {"broker_id": key, "symbol": "ALFA", "side": side, "quantity": quantity, "price": price,
        "trade_cost": fee, "execution_id": execution}, seconds)


def test_new_native_clocks_measure_each_stage_and_keep_unfilled_orders_in_ack_sample():
    with database() as db:
        ev(db, "startup", {"mode": "local"})
        ev(db, "decision_accepted", {"id": "planned"}, 1)
        for key in ("order-one", "order-two"):
            ev(db, "order_submitted", {"intent_id": key, "broker_id": key, "kind": "allocation",
               "symbol": "ALFA", "quantity": 10, "side": "buy", "decision_id": "planned",
               "send_at": (NOW + timedelta(seconds=3)).isoformat()}, 5)
        fill(db, key="order-one", seconds=11)
        metrics = analyze(db, NOW + timedelta(seconds=12))["groups"]["local"]
    assert metrics["stage_latency"] == {
        "decision_to_send": {"samples": 2, "p95_seconds": 2},
        "send_to_ack": {"samples": 2, "p95_seconds": 2},
        "send_to_first_fill": {"samples": 1, "p95_seconds": 8},
    }


def test_notional_weighted_buy_sell_shortfall_and_known_fees_are_separate_from_spread():
    db = database()
    ev(db, "startup", {"mode": "local"})
    order(db)
    fill(db)
    order(db, "b", "sell", 1, 199, 201)
    fill(db, "b", "sell", 1, 199, 2)
    row = analyze(db, NOW + timedelta(seconds=10))["groups"]["local"]
    assert row["priced_notional"] == 1190
    assert row["implementation_shortfall_bps"] == pytest.approx(21 / 1190 * 10000)
    assert row["beyond_executable_quote_bps"] == pytest.approx(10 / 1190 * 10000)
    assert row["known_fees_usd"] == 3


def test_cumulative_fills_replace_callbacks_but_preserve_earliest_partial_observation():
    db = database()
    order(db)
    fill(db, quantity=2, fee=.2, seconds=1)
    fill(db, quantity=2, fee=.2, seconds=5)  # Duplicate delivery, not another partial fill.
    fill(db, quantity=10, price=102, fee=1, seconds=8, cumulative=True)
    report = analyze(db, NOW + timedelta(seconds=10))
    group, row = report["groups"]["unknown"], report["recent"][0]
    assert row["quantity"] == "10" and row["fee_usd"] == 1
    assert row["first_fill_observation_seconds"] == 1
    assert group["latency_samples"] == 1 and group["first_fill_observation_p95_seconds"] == 1
    earlier = analyze(db, NOW + timedelta(seconds=6))["recent"][0]
    assert earlier["quantity"] == "2" and earlier["first_fill_observation_seconds"] == 1


def test_censored_unfilled_denominator_missing_fees_and_arrivals_never_use_order_limits():
    db = database()
    order(db, "filled", bid=None)
    fill(db, "filled", fee=None)
    order(db, "canceled")
    ev(db, "order_observed", {"broker_id": "canceled", "status": "canceled"}, 2)
    order(db, "pending")
    row = analyze(db, NOW + timedelta(seconds=10))["groups"]["unknown"]
    assert row["orders"] == 3 and row["with_fills"] == 1 and row["unfilled_observations"] == 2
    assert row["terminal_unfilled"] == 1 and row["open_or_unknown"] == 1
    assert row["fees_unknown_orders"] == row["arrival_unknown_orders"] == 1
    assert row["implementation_shortfall_bps"] is None


@pytest.mark.parametrize("fault", ["negative_fee", "oversized", "wrong_side", "conflicting_duplicate", "predated_fill"])
def test_invalid_execution_evidence_is_counted_without_plausible_cost_metrics(fault):
    db = database()
    order(db)
    if fault == "conflicting_duplicate":
        fill(db, price=101)
        fill(db, price=102, seconds=2)
    else:
        fill(db, fee=-1 if fault == "negative_fee" else 1, quantity=11 if fault == "oversized" else 10,
             side="sell" if fault == "wrong_side" else "buy", seconds=-1 if fault == "predated_fill" else 1)
    row = analyze(db, NOW + timedelta(seconds=10))["groups"]["unknown"]
    assert row["invalid_fill_orders"] == 1 and row["latency_samples"] == 0
    assert row["implementation_shortfall_bps"] is None


def test_future_quote_fill_and_mode_cannot_leak_into_earlier_report():
    db = database()
    ev(db, "startup", {"mode": "local"})
    order(db, "a")
    fill(db, "a", seconds=10)
    ev(db, "startup", {"mode": "paper"}, seconds=20)
    order(db, "b", seconds=21)
    fill(db, "b", seconds=22)
    earlier = analyze(db, NOW + timedelta(seconds=9))
    assert set(earlier["groups"]) == {"local"} and earlier["groups"]["local"]["unfilled_observations"] == 1
    db.execute("UPDATE fund_order_quotes SET observed_at=? WHERE intent_id='a'", ((NOW + timedelta(seconds=25)).isoformat(),))
    later = analyze(db, NOW + timedelta(seconds=30))
    assert set(later["groups"]) == {"local", "paper"}
    assert later["groups"]["local"]["arrival_unknown_orders"] == 1
    assert later["groups"]["paper"]["arrival_unknown_orders"] == 0
    assert any("本地与券商模拟" in text for text in lines(later))


def test_native_iteration_timestamp_does_not_hide_valid_send_time_quotes_or_backdate_orders():
    db = database()
    order(db)
    prepared = (NOW + timedelta(seconds=5)).isoformat()
    db.execute("UPDATE fund_orders SET submitted_at=?", (prepared,))
    db.execute("UPDATE fund_order_quotes SET as_of=?,observed_at=?", (prepared, prepared))
    fill(db, seconds=6)
    assert analyze(db, NOW + timedelta(seconds=4))["groups"] == {}
    row = analyze(db, NOW + timedelta(seconds=10))["recent"][0]
    assert row["arrival"] == "recorded_bid_ask" and row["quote_age_seconds"] == 0
    assert row["first_fill_observation_seconds"] == 1
    assert row["submission_clock"] == "send_preparation_receipt"


def test_equivalent_numeric_execution_redelivery_keeps_one_fill_and_first_clock():
    db = database()
    order(db)
    fill(db, quantity=2, price=101, fee=1, seconds=1)
    fill(db, quantity="2.0", price="101.0", fee="1.0", seconds=2)
    report = analyze(db, NOW + timedelta(seconds=10))
    assert report["groups"]["unknown"]["invalid_fill_orders"] == 0
    assert report["recent"][0]["quantity"] == "2.0"
    assert report["recent"][0]["first_fill_observation_seconds"] == 1
    assert report["groups"]["unknown"]["known_fees_usd"] == 1


def test_terminal_partial_missing_callbacks_is_incomplete_instead_of_zero_filled():
    db = database()
    order(db)
    ev(db, "order_observed", {"broker_id": "a", "status": "canceled", "filled_quantity": "4"}, 2)
    ev(db, "fill_details_unavailable", {"broker_id": "a", "status": "canceled", "average_price": "100"}, 2)
    row = analyze(db, NOW + timedelta(seconds=10))["groups"]["unknown"]
    assert row["incomplete_fill_orders"] == row["fees_unknown_orders"] == 1
    assert row["terminal_unfilled"] == row["unfilled_observations"] == 0
    assert row["implementation_shortfall_bps"] is None


def test_all_in_cost_uses_only_jointly_observed_quote_quantity_and_fee():
    db = database()
    ev(db, "startup", {"mode": "paper"})
    order(db, "complete")
    fill(db, "complete", fee=1)
    order(db, "price-improvement", "sell", 1, 199, 201)
    fill(db, "price-improvement", "sell", 1, 201, 0)
    order(db, "no-fee")
    fill(db, "no-fee", price=105, fee=None)
    order(db, "no-quote", bid=None)
    fill(db, "no-quote", price=110, fee=40)
    order(db, "still-open")
    fill(db, "still-open", quantity=2, price=100, fee=2)
    order(db, "terminal-partial")
    fill(db, "terminal-partial", quantity=2, price=100, fee=1)
    ev(db, "order_observed", {"broker_id": "terminal-partial", "status": "canceled", "filled_quantity": 2}, 3)
    report = analyze(db, NOW + timedelta(seconds=10))
    costs = report["groups"]["paper"]["matched_costs"]
    arrival = 990 + 200 + 198
    matched_execution = 1010 + 201 + 200
    assert costs["matched_orders"] == 3 and costs["unclosed_quantity_orders"] == 1
    assert costs["matched_arrival_notional_usd"] == arrival
    assert costs["matched_execution_notional_usd"] == matched_execution
    assert costs["verified_notional_coverage"] == pytest.approx(matched_execution / (matched_execution + 1050 + 1100 + 200))
    assert costs["fee_bps"] == pytest.approx(2 / arrival * 10000)
    assert costs["midpoint_shortfall_bps"] == pytest.approx((20 - 1 + 2) / arrival * 10000)
    assert costs["all_in_bps"] == pytest.approx((20 - 1 + 2 + 2) / arrival * 10000)
    assert any("覆盖已核实成交金额" in line for line in lines(report))


def test_later_order_quantity_excludes_an_incomplete_older_cumulative_fill():
    db = database()
    order(db)
    fill(db, quantity=2, seconds=1, cumulative=True)
    ev(db, "order_observed", {"broker_id": "a", "status": "canceled", "filled_quantity": 5}, 5)
    early = analyze(db, NOW + timedelta(seconds=2))["groups"]["unknown"]
    assert early["matched_costs"]["unclosed_quantity_orders"] == 1
    late = analyze(db, NOW + timedelta(seconds=10))["groups"]["unknown"]
    assert late["incomplete_fill_orders"] == 1 and late["matched_costs"]["matched_orders"] == 0
    assert late["matched_costs"]["all_in_bps"] is None


def test_cost_reference_windows_modes_and_receipt_times_stay_distinct():
    db = database()
    ev(db, "startup", {"mode": "local"}, seconds=-100 * 86400)
    order(db, "older", seconds=-91 * 86400)
    fill(db, "older", price=900, seconds=-91 * 86400 + 1)
    order(db, "local")
    fill(db, "local", price=98, fee=0)
    ev(db, "startup", {"mode": "paper"}, seconds=8)
    order(db, "paper", seconds=9)
    fill(db, "paper", seconds=10, fee=0)
    before = prior_cost_reference(db, NOW + timedelta(seconds=10, microseconds=-1))
    assert before["groups"]["local"]["orders_submitted"] == 1
    assert before["groups"]["local"]["all_in_bps"] < 0
    assert before["groups"]["paper"]["orders_without_observed_fills"] == 1
    assert before["groups"]["paper"]["all_in_bps"] is None
    after = prior_cost_reference(db, NOW + timedelta(seconds=10))
    assert after["groups"]["paper"]["all_in_bps"] > 0
    assert "broker_id" not in json.dumps(after)


def test_unreadable_archived_execution_packet_leaves_optional_reference_unavailable():
    db = database()
    ev(db, "order_submitted", [])
    reference = prior_cost_reference(db, NOW)
    assert reference["status"] == "execution_ledger_unavailable" and reference["groups"] == {}
