"""Biases that distort review and learning: survivorship, sector bets, the manager's own reversals, day trades.

Synthetic closes, factor panels, decisions and fills only; no network, broker or model inference.
"""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from scripts.autonomous_fund.factors import FactorSettings, _evidence_flags, daily_ic, daily_ic_within, evaluate_factor
from scripts.autonomous_fund.holdings import day_trades
from scripts.autonomous_fund.quant import sectors
from scripts.autonomous_fund.scorecard import score_consistency, score_decisions


# ----- Sector bets are not stock selection ----------------------------------------------------------------

def test_a_symbol_in_two_sector_groups_takes_the_first_gics_sector():
    groups = (("us_growth_megacap", "0.35", ("NVDA", "AAPL")), ("financials", "0.30", ("NVDA", "JPM")),
              ("technology", "0.30", ("NVDA", "AAPL")))
    assert sectors(groups) == {"AAPL": "technology", "NVDA": "technology", "JPM": "financials"}


def closes(series):
    days = pd.bdate_range("2026-06-01", periods=len(next(iter(series.values()))))
    return {symbol: [(d.date().isoformat(), value) for d, value in zip(days, values)] for symbol, values in series.items()}


def test_the_decision_excess_splits_exactly_into_within_sector_selection_and_the_sector_bet():
    data = closes({"TECH1": [100, 102], "TECH2": [100, 104], "BOND": [100, 100.5], "SPY": [100, 101]})
    decision = [("2026-06-01T15:00:00+00:00", {"targets": [{"symbol": "TECH1", "weight": 0.1},
                                                         {"symbol": "BOND", "weight": 0.2}]})]
    result = score_decisions(decision, data, sectors={"TECH1": "technology", "TECH2": "technology"})
    one = result["recent"][0]["horizons"]["1"]
    # beta is assumed 1 (too little history): TECH1 2% vs its sector 3% vs SPY 1%.
    assert abs(one["within_sector_selection"] - 0.1 * (0.02 - 0.03)) < 1e-12
    assert abs(one["sector_allocation"] - 0.1 * (0.03 - 0.01)) < 1e-12
    assert abs(one["unassigned_excess"] - 0.2 * (0.005 - 0.01)) < 1e-12
    total = one["within_sector_selection"] + one["sector_allocation"] + one["unassigned_excess"]
    assert abs(total - one["excess_return"]) < 1e-12 and one["sector_assigned_weight"] == 0.1
    summary = result["by_horizon"]["1"]
    assert summary["mean_within_sector_selection"] < 0 < summary["mean_sector_allocation"]  # A sector bet, not picking.
    assert "within_sector_selection" not in score_decisions(decision, data)["recent"][0]["horizons"]["1"]


def sector_panel(within_signal, seed=7):
    """Two sectors of six names; returns = a sector move + optionally a within-sector move the factor knows."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2024-01-01", periods=260)
    names = [f"A{i}" for i in range(6)] + [f"B{i}" for i in range(6)]
    sector = {n: n[0] for n in names}
    level = {n: 100.0 for n in names}
    rows, factor_rows = [], []
    for _ in days:
        move = {"A": rng.normal(0, 0.01), "B": rng.normal(0, 0.01)}
        tilt = {n: rng.normal(0, 0.01) for n in names}
        # Without the within-sector signal the factor is the sector's move plus noise unrelated to returns.
        factor_rows.append({n: (tilt[n] if within_signal else move[sector[n]] + rng.normal(0, 0.001)) for n in names})
        rows.append(dict(level))
        for n in names:
            level[n] *= 1 + move[sector[n]] + (tilt[n] if within_signal else rng.normal(0, 0.002))
    close = pd.DataFrame(rows, index=days)
    factor = pd.DataFrame(factor_rows, index=days)  # Known at t, predicts t -> t+1.
    return factor, close, sector


def test_a_factor_that_only_ranks_sectors_has_no_within_sector_ic():
    factor, close, sector = sector_panel(within_signal=False)
    assert daily_ic(factor, close, 1, 8)["ic"].mean() > 0.3  # Across all names it looks strong.
    assert abs(daily_ic_within(factor, close, 1, 4, sector)["ic"].mean()) < 0.1  # Within sectors: nothing.
    result = evaluate_factor(factor, 1, close, FactorSettings(), 1, sectors=sector)
    assert "sector_neutral" in result and _evidence_flags(result)["mostly_sector_bet"] is True


def test_a_factor_that_picks_within_sectors_keeps_its_within_sector_ic():
    factor, close, sector = sector_panel(within_signal=True)
    result = evaluate_factor(factor, 1, close, FactorSettings(), 1, sectors=sector)
    assert result["sector_neutral"]["in_sample"]["t"] > 3 and _evidence_flags(result)["mostly_sector_bet"] is False
    assert "sector_neutral" not in evaluate_factor(factor, 1, close, FactorSettings(), 1)


# ----- Survivorship ---------------------------------------------------------------------------------------

def test_factor_results_say_they_have_survivorship_bias(tmp_path):
    from scripts.autonomous_fund.factors import FactorLab
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.notifications import weekly_document
    from tests.test_autonomous_fund_weekly_report import ledger
    lab = FactorLab(Journal(tmp_path / "lab", "synthetic-lab"), FactorSettings())
    assert "Survivorship bias" in lab.context(datetime(2026, 11, 9, tzinfo=timezone.utc), None)["survivorship"]
    _, sequence = ledger(tmp_path / "weekly")
    _, text = weekly_document(tmp_path / "weekly", sequence)
    assert "幸存者偏差" in text.split("## 五、因子实验进展", 1)[1].split("## 六", 1)[0]


# ----- The manager's own reversals ------------------------------------------------------------------------

def at(day, hour=15):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc).isoformat()


def decision(**weights):
    return {"targets": [{"symbol": s, "weight": w} for s, w in weights.items()]}


def test_a_target_reversed_within_three_sessions_without_a_stop_is_a_reversal():
    decisions = [(at(2), decision(ALFA=0.05)), (at(3), decision(ALFA=0.0)),  # Bought, dropped next day: reversal.
                 (at(4), decision(BETA=0.06)), (at(5), decision(BETA=0.06)), (at(6), decision()),  # BETA sold: a move.
                 (at(9), decision(GAMMA=0.05)), (at(10), decision(GAMMA=0.055)),  # Half a point: not a move.
                 (at(16), decision(GAMMA=0.0))]  # Four sessions later: a change of mind, not a reversal.
    triggers = [(at(5, 18), "protective_trigger", {"symbol": "BETA"})]
    result = score_consistency(decisions, triggers)
    assert result["moves"] == 6 and result["reversals"] == 1  # ALFA up/down, BETA up/down, GAMMA up/down.
    assert result["recent"][0]["symbol"] == "ALFA" and result["recent"][0]["sessions_apart"] == 1
    assert abs(result["estimated_cost"] - 2 * 0.05 * 10 / 10_000) < 1e-12
    unforced = score_consistency(decisions, [])
    assert unforced["reversals"] == 2  # Without the stop, BETA's exit also reverses its purchase within 3 sessions.
    assert score_consistency([], [])["reversal_rate"] is None


# ----- Day trades (the US pattern day trader count) -------------------------------------------------------

def fill(day, hour, side, symbol="ALFA", broker="b"):
    return (datetime(2026, 11, day, hour, tzinfo=timezone.utc).isoformat(), "fill",
            {"broker_id": f"{broker}{day}{hour}", "symbol": symbol, "side": side, "quantity": "1", "price": "100"})


def test_each_buy_then_sell_of_a_symbol_on_one_day_is_a_day_trade():
    fills = [fill(2, 15, "buy"), fill(2, 17, "sell"), fill(2, 18, "buy"), fill(2, 19, "sell"),  # Two round trips.
             fill(3, 15, "sell", "BETA"), fill(3, 16, "buy", "BETA"),  # Sold an overnight holding first: none.
             fill(4, 15, "buy", "GAMMA"), fill(4, 16, "buy", "GAMMA"), fill(4, 18, "sell", "GAMMA"),  # One.
             fill(5, 15, "buy", "DELTA")]
    result = day_trades(fills, datetime(2026, 11, 6, 22, tzinfo=timezone.utc))
    assert result["by_day"] == {"2026-11-02": 2, "2026-11-04": 1}
    assert result["sessions"][-1] == "2026-11-06" and result["last_sessions"] == 3 and not result["would_flag"]
    later = day_trades(fills, datetime(2026, 11, 10, 22, tzinfo=timezone.utc))
    assert later["last_sessions"] == 1  # 11-02 has left the five-session window.


def test_a_reconciled_fill_replaces_its_callbacks_in_the_count():
    callbacks = [fill(2, 15, "buy"), ("2026-11-02T17:00:00+00:00", "fill",
                                      {"broker_id": "s", "symbol": "ALFA", "side": "sell", "quantity": "1", "price": "1"}),
                 ("2026-11-02T17:01:00+00:00", "fill_reconciled",
                  {"broker_id": "s", "symbol": "ALFA", "side": "sell", "quantity": "2", "price": "1"})]
    assert day_trades(callbacks, datetime(2026, 11, 2, 22, tzinfo=timezone.utc))["by_day"] == {"2026-11-02": 1}


def test_status_shows_the_day_trade_count_and_warns_at_the_limit(tmp_path):
    import sqlite3
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.status import day_trade_line
    journal = Journal(tmp_path, "synthetic-pdt-account")
    for row in [fill(2, 15, "buy"), fill(2, 16, "sell"), fill(3, 15, "buy"), fill(3, 16, "sell"),
                fill(4, 15, "buy"), fill(4, 16, "sell")]:
        journal.event(row[0], row[1], row[2])
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        line = day_trade_line(db, datetime(2026, 11, 4, 22, tzinfo=timezone.utc))
    assert "最近 5 个交易日 3 次" in line and "限制交易 90 天" in line  # At the limit: the warning shows.


def test_the_manager_sees_its_reversals_and_day_trades(tmp_path):
    from tests.test_autonomous_fund_quant import NOW, account, decision as quant_decision, model_harness
    strategy = model_harness(tmp_path)
    seen = {}
    evidence = [{"source": "Synthetic", "available_at": (NOW - timedelta(days=1)).isoformat(),
                 "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]

    def call(name, **kwargs):
        seen[name] = kwargs.get("context", {})
        return {"summary": "s", "evidence": evidence} if name == "researcher" else quant_decision([("ALFA", .05)])
    strategy._agent_call = call
    strategy._decide(account(), NOW, False)
    context = seen["manager"]
    assert context["day_trades"]["last_sessions"] == 0 and "25,000 USD" in context["day_trades"]["meaning"]
    assert "consistency" in (context.get("scorecards") or {})
