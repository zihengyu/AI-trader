"""Portfolio studies use frozen research sleeves and prospective receipt dates."""
import json
import math
from datetime import timedelta

import pytest

from scripts.autonomous_fund.journal import encode
from scripts.autonomous_fund.lab import main, report
from scripts.autonomous_fund.risk import RiskError, instant
from scripts.autonomous_fund.strategy_portfolio import STUDY_EVENT, declare_study
from tests.test_autonomous_fund_strategy_comparison import nav
from tests.test_autonomous_fund_strategy_lab import dataset, lab, proposal


@pytest.fixture(scope="module")
def tape():
    return [(row["day"], instant(row["at"])) for row in dataset(49)["bars"]["BASE"]]


def spec(name="example", *, fees=0, slip=0):
    return {"study_id": name, "base": [{"strategy_id": "base", "version": "v1", "weight": 1}],
            "candidate": {"strategy_id": "candidate", "version": "v1"}, "candidate_weight": .2,
            "fee_bps": fees, "slippage_bps": slip}


def store(engine, key, tape, returns, *, replayed_index=None):
    engine.submit(proposal(key), tape[0][1] - timedelta(days=1))
    series = nav([day for day, _ in tape], returns)
    with engine.journal.connection() as db:
        for index, (row, (_, close)) in enumerate(zip(series, tape, strict=True)):
            at = close + timedelta(minutes=1)
            receipt = {"evidence": "replay" if index == replayed_index else "prospective", "receipt": row}
            db.execute("INSERT INTO fund_strategy_forward VALUES(?,?,?,?,?,?)",
                       (key, "v1", row["day"], at.isoformat(), "synthetic-snapshot", encode(receipt)))


def declare(engine, plan, at):
    with engine.journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        return declare_study(db, plan, at)


def studies(engine, at):
    return engine.context(at)["competition"]["portfolio_comparison"]["studies"]


def test_frozen_risk_budget_removes_extra_market_exposure_on_new_dates(tmp_path, tape):
    engine = lab(tmp_path)
    prices = [.012 if index % 2 == 0 else -.006 for index in range(len(tape))]
    store(engine, "base", tape, prices)
    store(engine, "candidate", tape, [2 * value for value in prices])
    declared = tape[25][1] + timedelta(hours=1)
    plan = declare(engine, spec(), declared)
    assert plan["calibration_end"] == tape[25][0]
    assert plan["first_evaluation_session"] == tape[27][0]
    assert plan["scales"] == pytest.approx({"base": 1, "combined": 1 / 1.2})
    pending, = studies(engine, tape[26][1] + timedelta(hours=1))
    assert pending["status"] == "awaiting_prospective_sessions"

    observed, = studies(engine, tape[-1][1] + timedelta(hours=1))
    assert observed["status"] == "observed" and observed["evaluation_sessions"] == len(tape) - 27
    assert observed["incremental_net_return"] == pytest.approx(0, abs=1e-12)
    assert observed["change_in_max_drawdown"] == pytest.approx(0, abs=1e-12)
    assert observed["combined"]["mean_cash_weight"] == pytest.approx(1 - 1 / 1.2)
    assert observed["base"]["net_return"] == pytest.approx(math.prod(1 + value for value in prices[27:]) - 1)
    assert engine.journal.pending() == []


def test_extra_transfer_cost_is_charged_and_replay_truncates_both_arms(tmp_path, tape):
    engine = lab(tmp_path)
    prices = [.009 if index % 2 == 0 else -.005 for index in range(len(tape))]
    store(engine, "base", tape, prices)
    store(engine, "candidate", tape, prices, replayed_index=32)
    declared = tape[25][1] + timedelta(hours=1)
    plan = spec(fees=60, slip=40)
    declare(engine, plan, declared)
    result, = studies(engine, tape[-1][1] + timedelta(hours=1))
    assert result["evaluation_end"] == tape[31][0]
    assert result["evaluation_sessions"] == 5 and result["status"] == "limited_sample"
    # Both arms hold identical net sleeve returns. Only the initial transfer
    # incurs costs, and the starting capital finances its own fee and slip.
    capital_after_entry_cost = 1 / 1.01
    assert result["base"]["transfer_turnover_fraction"] == pytest.approx(capital_after_entry_cost)
    assert result["combined"]["transfer_fees_fraction"] == pytest.approx(capital_after_entry_cost * .006)
    assert result["combined"]["transfer_slippage_fraction"] == pytest.approx(capital_after_entry_cost * .004)
    assert result["combined"]["net_return"] == pytest.approx(
        capital_after_entry_cost * math.prod(1 + value for value in prices[27:32]) - 1)
    assert result["incremental_net_return"] == pytest.approx(0, abs=1e-12)
    assert result["transfer_cost_stress"]["status"] == "already_nonpositive"
    assert result["paired_advantage"]["non_overlapping"] == 1
    assert result["paired_advantage"]["interval_95"] is None


def test_manager_sees_later_incremental_return_and_declaration_cannot_be_altered(tmp_path, tape):
    engine = lab(tmp_path)
    base = [.010 if index % 2 == 0 else -.005 for index in range(len(tape))]
    candidate = [value + (.002 if index >= 27 else 0) for index, value in enumerate(base)]
    store(engine, "base", tape, base)
    store(engine, "another-base", tape, base)
    store(engine, "candidate", tape, candidate)
    declared = tape[25][1] + timedelta(hours=1)
    plan = {**spec(), "base": [{"strategy_id": "base", "version": "v1", "weight": .6},
                              {"strategy_id": "another-base", "version": "v1", "weight": .4}]}
    fixed = declare(engine, plan, declared)
    assert declare(engine, plan, tape[-1][1]) == fixed
    with pytest.raises(RiskError, match="cannot be rewritten"):
        declare(engine, {**plan, "candidate_weight": .4}, tape[-1][1])
    declare(engine, {**plan, "study_id": "with-transfer-costs", "fee_bps": 30, "slippage_bps": 20}, declared)
    evaluations = {item["plan"]["study_id"]: item for item in studies(engine, tape[-1][1] + timedelta(hours=1))}
    evaluation = evaluations["example"]
    gross = math.prod(1 + value + .2 * .002 for value in base[27:])
    assert evaluation["incremental_net_return"] == pytest.approx(gross - math.prod(1 + value for value in base[27:]))
    assert evaluation["scales"] == pytest.approx({"base": 1, "combined": 1})
    assert 0 < evaluations["with-transfer-costs"]["incremental_net_return"] < evaluation["incremental_net_return"]
    assert any("组合对照 example" in line for line in report(tmp_path))
    # An incomplete restore can preserve the declaration but lose a source
    # receipt. A later overlapping window cannot silently replace calibration.
    with engine.journal.connection() as db:
        db.execute("DELETE FROM fund_strategy_forward WHERE id=? AND version=? AND day=?",
                   ("another-base", "v1", tape[23][0]))
    assert {item["status"] for item in studies(engine, tape[-1][1] + timedelta(hours=1))} == {
        "missing_or_changed_calibration_evidence"}


def test_declaration_requires_already_available_evidence_and_exact_owned_versions(tmp_path, tape, capsys):
    engine = lab(tmp_path)
    returns = [.013 if index % 2 else -.004 for index in range(len(tape))]
    store(engine, "base", tape, returns)
    store(engine, "candidate", tape, returns)
    with pytest.raises(RiskError, match="20 common"):
        declare(engine, spec(), tape[19][1] + timedelta(hours=1))
    missing = {**spec(), "candidate": {"strategy_id": "candidate", "version": "not-registered"}}
    with pytest.raises(RiskError, match="selected journal"):
        declare(engine, missing, tape[25][1] + timedelta(hours=1))
    with engine.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind=?", (STUDY_EVENT,)).fetchone()[0] == 0
    config = tmp_path / "portfolio.json"
    config.write_text(json.dumps(spec()), encoding="utf-8")
    main(["declare-portfolio", "--run-dir", str(tmp_path), "--portfolio", str(config)])
    declared = json.loads(capsys.readouterr().out)
    assert declared["plan"]["candidate"]["version"] == "v1"
    assert engine.journal.pending() == []


def test_combined_only_transfer_stress_brackets_a_small_edge(tmp_path, tape):
    engine = lab(tmp_path)
    market = [.010 if i % 2 == 0 else -.005 for i in range(len(tape))]
    candidate = [value + (.00016 if i >= 27 else 0) for i, value in enumerate(market)]
    store(engine, "base", tape, market)
    store(engine, "candidate", tape, candidate)
    declare(engine, spec(fees=10, slip=5), tape[25][1] + timedelta(hours=1))

    result, = studies(engine, tape[-1][1] + timedelta(hours=1))
    assert result["base"]["net_return"] == pytest.approx(math.prod(1 + r for r in market[27:]) / 1.0015 - 1)
    stress = result["transfer_cost_stress"]
    assert stress["status"] == "bracketed"
    assert stress["breakeven_bracket"] == {"positive_at_extra_bps": 5, "nonpositive_at_extra_bps": 10}
    scenarios = {row["extra_transfer_bps"]: row["incremental_net_return"] for row in stress["scenarios"]}
    assert scenarios[0] == pytest.approx(result["incremental_net_return"])
    assert scenarios[0] > scenarios[5] > 0 > scenarios[10] > scenarios[100]
    assert any("额外加收 5 基点" in line for line in report(tmp_path))


def test_declaration_freezes_cost_reference_without_changing_study_rates(tmp_path, tape):
    engine = lab(tmp_path)
    price_moves = [.013 if i % 2 else -.004 for i in range(len(tape))]
    store(engine, "base", tape, price_moves)
    store(engine, "candidate", tape, price_moves)
    at = tape[25][1] + timedelta(hours=1)
    order_at = at - timedelta(minutes=30)
    with engine.journal.connection() as db:
        db.executescript("CREATE TABLE fund_orders(intent_id TEXT PRIMARY KEY, submitted_at TEXT);"
                         "CREATE TABLE fund_order_quotes(intent_id TEXT PRIMARY KEY, bid TEXT, ask TEXT,"
                         "as_of TEXT, observed_at TEXT);")
        stamp = order_at.isoformat()
        db.execute("INSERT INTO fund_orders VALUES(?,?)", ("before-plan", stamp))
        db.execute("INSERT INTO fund_order_quotes VALUES(?,?,?,?,?)", ("before-plan", "99", "101", stamp, stamp))
    engine.journal.event(order_at - timedelta(days=1), "startup", {"mode": "paper"})
    engine.journal.event(order_at, "order_submitted", {"broker_id": "before-plan", "intent_id": "before-plan",
                         "symbol": "ALFA", "side": "buy", "quantity": 10})
    fill = {"broker_id": "before-plan", "symbol": "ALFA", "side": "buy", "quantity": 10, "price": 101, "trade_cost": 2}
    engine.journal.event(order_at + timedelta(seconds=1), "fill_reconciled", fill)
    registered = declare(engine, spec(fees=2, slip=8), at)
    reference = registered["execution_cost_reference"]
    assert reference["groups"]["paper"]["all_in_bps"] == 120
    assert reference["through"] == at.isoformat()

    # A corrected fee published after declaration belongs to future economic
    # reports, while this study retains the exact evidence it had at formation.
    engine.journal.event(at + timedelta(minutes=1), "fill_reconciled", {**fill, "trade_cost": 20})
    study, = studies(engine, tape[-1][1] + timedelta(hours=1))
    assert study["execution_cost_reference"] == reference
    assert study["plan"]["fee_bps"] == 2 and study["plan"]["slippage_bps"] == 8
    assert any("+120.00 基点" in line for line in report(tmp_path))


def test_paired_interval_covers_complete_five_session_blocks_only(tmp_path):
    engine = lab(tmp_path)
    tape = [(row["day"], instant(row["at"])) for row in dataset(80)["bars"]["BASE"]]
    market = [.010 if i % 2 == 0 else -.005 for i in range(len(tape))]
    candidate = [r + ((.003 if ((i - 27) // 5) % 2 == 0 else -.003) if i >= 27 else 0)
                 for i, r in enumerate(market)]
    store(engine, "base", tape, market)
    store(engine, "candidate", tape, candidate)
    store(engine, "identical-book", tape, market)
    store(engine, "large-edge", tape, [r + (.02 if i >= 27 else 0) for i, r in enumerate(market)])
    at = tape[25][1] + timedelta(hours=1)
    declare(engine, spec(), at)
    declare(engine, {**spec("identical"), "candidate": {"strategy_id": "identical-book", "version": "v1"}}, at)
    declare(engine, {**spec("large"), "candidate": {"strategy_id": "large-edge", "version": "v1"}}, at)

    results = {item["plan"]["study_id"]: item for item in studies(engine, tape[-1][1] + timedelta(hours=1))}
    result = results["example"]
    paired = result["paired_advantage"]
    assert (paired["non_overlapping"], paired["positive_blocks"], paired["trailing_sessions_excluded"]) == (10, 5, 3)
    low, high = paired["interval_95"]
    assert low < 0 < high  # Alternating week-level edges make this direction inconclusive.
    assert low < paired["sample_mean"] < high
    assert result["evaluation_sessions"] == 53 and "daily_returns" not in result["combined"]
    zero = results["identical"]["paired_advantage"]
    assert zero["non_overlapping"] == 10 and zero["positive_blocks"] == 0 and zero["interval_95"] is None
    large = results["large"]["transfer_cost_stress"]
    assert large["status"] == "above_tested_range" and large["breakeven_bracket"] is None
    assert large["scenarios"][-1]["extra_transfer_bps"] == 100 and large["scenarios"][-1]["incremental_net_return"] > 0
