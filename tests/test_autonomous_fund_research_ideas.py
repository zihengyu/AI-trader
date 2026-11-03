"""V2 3.2: structured research ideas, registered unchanged before the open and scored with calibration.

Synthetic symbols, closes and account identities only; no network and no model inference.
"""
import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.scorecard import MIN_INFORMATIVE_IDEAS, binomial_tail, score_ideas
from scripts.autonomous_fund.strategy import AutonomousFund, Decision, Research

ZONE = ZoneInfo("America/New_York")
EVIDENCE = [{"source": "synthetic filing", "available_at": "2026-11-02T08:00:00-05:00",
             "observed_at": "2026-11-02T08:05:00-05:00", "estimated": False, "estimate_reason": ""},
            {"source": "synthetic headline", "available_at": None, "observed_at": "2026-11-03T07:30:00-05:00",
             "estimated": False, "estimate_reason": ""}]
PACKET = {"completed_at": "2026-11-03T08:40:00-05:00", "session_open": "2026-11-03T09:30:00-05:00",
          "phase": "before_market_opens", "model": "synthetic/model"}


def idea(symbol="ALFA", direction="up", horizon=5, confidence=0.6, **extra):
    return {"symbol": symbol, "direction": direction, "horizon_sessions": horizon, "thesis": "Synthetic thesis",
            "edge_source": "Synthetic edge", "invalidation": "Synthetic invalidation", "confidence": confidence,
            **extra}


def research(*ideas):
    return Research.model_validate({"summary": "Synthetic research", "evidence": EVIDENCE,
                                    "ideas": list(ideas)}).model_dump()


def sessions(count, start=date(2026, 6, 1)):
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def formed(day, hour=8):
    return datetime.combine(date.fromisoformat(day), time(hour), ZONE).isoformat()


def walk(days, seed, drift=0.0, vol=0.01):
    rng, value, rows = random.Random(seed), 100.0, []
    for day in days:
        value *= 1 + drift + rng.gauss(0, vol)
        rows.append((day, value))
    return rows


# ----- Schema --------------------------------------------------------------------------------------

def test_no_ideas_is_valid_confidence_is_kept_in_range_and_extra_ideas_are_dropped():
    assert research()["ideas"] == []
    assert Research.model_validate({"summary": "s", "evidence": EVIDENCE}).ideas == []  # Older saved research.
    many = research(idea(confidence=1), idea(confidence=0.2), *[idea(f"S{i}") for i in range(10)])["ideas"]
    assert len(many) == 8 and many[0]["confidence"] == 0.95 and many[1]["confidence"] == 0.5
    assert many[0]["benchmark"] == "SPY" and many[0]["catalyst"] == "" and many[0]["evidence_refs"] == []
    for bad in (idea(confidence="high"), idea(horizon=30), idea(direction="sideways")):
        with pytest.raises(ValidationError):
            research(bad)
    plan = {"as_of": "x", "expires_at": "x", "next_review_at": "x", "rationale": "r", "targets": []}
    assert Decision.model_validate(plan).idea_refs == []
    assert Decision.model_validate({**plan, "idea_refs": ["2026-11-03#1"]}).idea_refs == ["2026-11-03#1"]


# ----- Registration --------------------------------------------------------------------------------

def fund(tmp_path):
    strategy = object.__new__(AutonomousFund)
    strategy.journal = Journal(tmp_path, "synthetic-ideas-account")
    strategy.prompt_version = "synthetic-version"
    return strategy


def test_ideas_are_registered_once_with_their_formation_time_and_never_revised(tmp_path):
    strategy = fund(tmp_path)
    first = research(idea(evidence_refs=[1, 2, 9]), idea("beta", "underperform", 3, 0.7))
    assert strategy._register_ideas(first, PACKET) == ["2026-11-03#1", "2026-11-03#2"]
    later = {**PACKET, "completed_at": "2026-11-03T09:10:00-05:00"}
    restated = research(idea(confidence=0.9, thesis="A changed story"), idea("GAMMA", "down", 2, 0.55))
    assert strategy._register_ideas(restated, later) == ["2026-11-03#3"]
    rows = [r["body"] for r in strategy.journal.all_events() if r["kind"] == "research_idea"]
    assert [r["idea_id"] for r in rows] == ["2026-11-03#1", "2026-11-03#2", "2026-11-03#3"]
    original = rows[0]
    # The restatement registered nothing and changed nothing: the first call is the one scored.
    assert original["confidence"] == 0.6 and original["thesis"] == "Synthetic thesis"
    assert original["source"] == "research" and original["formed_at"] == PACKET["completed_at"]
    assert original["prompt_version"] == "synthetic-version" and original["model"] == "synthetic/model"
    assert original["evidence_refs"] == [1, 2] and original["dropped_evidence_refs"] == [9]
    assert [e["source"] for e in original["cited_evidence"]] == ["synthetic filing", "synthetic headline"]
    assert rows[1]["symbol"] == "BETA" and rows[1]["benchmark"] == "SPY"
    assert [i["idea_id"] for i in strategy._session_ideas(PACKET["session_open"])] == [r["idea_id"] for r in rows]
    assert strategy._register_ideas(research(), PACKET) == []
    raw = {"summary": "Raw dict answer", "evidence": EVIDENCE, "ideas": [  # No benchmark, catalyst or refs given.
        idea("DELTA", "up", 1, 0.7)]}
    [raw_id] = strategy._register_ideas(raw, PACKET)
    stored = [r["body"] for r in strategy.journal.all_events() if r["kind"] == "research_idea"][-1]
    assert raw_id == "2026-11-03#4" and stored["benchmark"] == "SPY" and stored["evidence_refs"] == []
    with pytest.raises(ValidationError):  # The caller discloses idea_registration_error; the research is kept.
        strategy._register_ideas({"summary": "x", "evidence": EVIDENCE, "ideas": [idea(confidence="sure")]}, PACKET)
    tomorrow = {**PACKET, "session_open": "2026-11-04T09:30:00-05:00", "completed_at": "2026-11-04T08:40:00-05:00"}
    assert strategy._register_ideas(first, tomorrow) == ["2026-11-04#1", "2026-11-04#2"]  # A new session's call.


# ----- Scoring -------------------------------------------------------------------------------------

DAYS = sessions(40)


def test_an_idea_is_measured_from_the_first_close_after_it_was_formed():
    spy = walk(DAYS, 1)
    alfa = [(d, 100.0) for d in DAYS[:30]] + [(d, 110.0) for d in DAYS[30:]]  # Jumps before day 30's open.
    closes = {"ALFA": alfa, "SPY": spy}
    pre_open = {**idea(horizon=1), "idea_id": "a", "formed_at": formed(DAYS[30])}
    [item] = score_ideas([pre_open], closes)["recent"]
    # The gap the researcher already saw before the open is not credited.
    assert item["start_day"] == DAYS[30] and item["end_day"] == DAYS[31] and item["status"] == "incorrect"
    after_close = {**pre_open, "formed_at": formed(DAYS[29], hour=17)}
    assert score_ideas([after_close], closes)["recent"][0]["start_day"] == DAYS[30]
    card = score_ideas([{**pre_open, "formed_at": formed(DAYS[-1])},  # No close after it yet.
                        {**pre_open, "formed_at": formed(DAYS[0], hour=7)},  # Older than the close window.
                        {**pre_open, "symbol": "NOPE"}], closes)
    assert [i["status"] for i in card["recent"]] == ["pending", "unavailable", "unavailable"]
    assert card["scored"] == 0 and card["verdict"] == "insufficient_sample" and card["brier"] is None


def test_calibration_brier_and_both_naive_baselines():
    spy = walk(DAYS, 2)
    spy[31] = (DAYS[31], spy[30][1] * 1.01)
    closes = {"SPY": spy}
    moves = {"A": 1.02, "B": 0.98, "C": 0.97, "D": 1.03}
    for seed, (symbol, move) in enumerate(moves.items()):
        rows = walk(DAYS, 10 + seed)
        rows[31] = (DAYS[31], rows[30][1] * move)
        closes[symbol] = rows
    when = formed(DAYS[30])
    ideas = [{**idea("A", "up", 1, 0.9), "idea_id": "A", "formed_at": when},           # Right.
             {**idea("B", "down", 1, 0.6), "idea_id": "B", "formed_at": when},         # Right; always-up is wrong.
             {**idea("C", "up", 1, 0.8), "idea_id": "C", "formed_at": when},           # Wrong.
             {**idea("D", "underperform", 1, 0.5), "idea_id": "D", "formed_at": when}]  # Wrong; always-up is right.
    card = score_ideas(ideas, closes)
    assert card["scored"] == 4 and card["correct"] == 2 and card["hit_rate"] == 0.5
    assert card["brier"] == pytest.approx((0.01 + 0.16 + 0.64 + 0.25) / 4)
    assert card["brier_skill_vs_coin"] == pytest.approx(1 - card["brier"] / 0.25)
    up = card["baselines"]["always_up"]
    assert up["hit_rate"] == 0.5 and (up["better"], up["worse"], up["ideas_where_different"]) == (1, 1, 2)
    assert up["p_value"] == pytest.approx(0.75) and card["baselines"]["coin"]["p_value"] == pytest.approx(11 / 16)
    buckets = card["by_confidence"]
    assert buckets["0.5-0.60"] == {"scored": 1, "hit_rate": 0.0, "mean_confidence": 0.5, "calibration_gap": -0.5}
    assert buckets["0.7-0.80"]["scored"] == 0 and buckets["0.8-0.95"]["scored"] == 2
    assert buckets["0.8-0.95"]["calibration_gap"] == pytest.approx(0.5 - 0.85)
    assert card["selection"]["scored"] == 4 and all(i["beta"] is not None for i in card["recent"])
    assert card["verdict"] == "insufficient_sample"


def test_binomial_tail_is_exact():
    assert binomial_tail(0, 7) == 1 and binomial_tail(2, 2) == 0.25 and binomial_tail(5, 5) == 1 / 32
    assert binomial_tail(0, 0) is None and 0 < binomial_tail(700, 1000) < 1e-30  # No underflow to a wrong 0.


def random_ideas(rng, days, symbols, count, pick):
    ideas = []
    for n in range(count):
        start = rng.randrange(25, len(days) - 21)
        ideas.append({**idea(rng.choice(symbols), *pick(rng, start)), "idea_id": str(n), "formed_at": formed(days[start])})
    return ideas


def test_negative_control_random_ideas_are_not_informative():
    """Random directions and confidences: the informative verdict's false-positive rate stays near its level."""
    rng, days = random.Random(7), sessions(160)
    false_positives = trials = 0
    for trial in range(60):
        drift = (0.0, 0.0008)[trial % 2]  # A rising market rewards always-up, never a random caller.
        closes = {"SPY": walk(days, 1000 + trial, drift)}
        symbols = [f"S{i}" for i in range(12)]
        for i, symbol in enumerate(symbols):
            closes[symbol] = walk(days, trial * 100 + i, drift, 0.015)
        ideas = random_ideas(rng, days, symbols, 4 * MIN_INFORMATIVE_IDEAS, lambda r, _: (
            r.choice(["up", "down", "outperform", "underperform"]), r.randint(1, 5), r.uniform(0.5, 0.95)))
        card = score_ideas(ideas, closes)
        assert card["scored"] == len(ideas)
        trials += 1
        false_positives += card["verdict"] == "informative"
    assert false_positives / trials <= 0.1


def test_a_genuinely_skilled_caller_is_informative():
    rng, days = random.Random(11), sessions(160)
    closes = {"SPY": walk(days, 5)}
    symbols = [f"S{i}" for i in range(12)]
    for i, symbol in enumerate(symbols):
        closes[symbol] = walk(days, 50 + i, 0.0, 0.015)
    values = {s: dict(rows) for s, rows in closes.items()}
    ideas = []
    for n in range(150):
        symbol, start, horizon = rng.choice(symbols), rng.randrange(25, len(days) - 21), rng.randint(1, 5)
        rose = values[symbol][days[start + horizon]] > values[symbol][days[start]]
        right = rng.random() < 0.7
        ideas.append({**idea(symbol, "up" if rose == right else "down", horizon, 0.7), "idea_id": str(n),
                      "formed_at": formed(days[start])})
    card = score_ideas(ideas, closes)
    assert 0.6 < card["hit_rate"] < 0.8 and card["brier"] < 0.25
    assert card["baselines"]["always_up"]["better"] > card["baselines"]["always_up"]["worse"]
    assert card["verdict"] == "informative"


# ----- In the strategy -----------------------------------------------------------------------------

def test_scorecards_and_their_summary_score_registered_ideas(tmp_path):
    from tests.test_autonomous_fund_quant import DAYS as HISTORY, NOW, default_frames, harness
    strategy = harness(tmp_path, default_frames(), mode="local")
    strategy.prompt_version = "synthetic-version"
    start = HISTORY[-10]
    packet = {"completed_at": start.replace(hour=8).isoformat(), "session_open": start.replace(hour=9, minute=30).isoformat(),
              "phase": "before_market_opens", "model": "synthetic/model"}
    strategy._register_ideas(research(idea("ALFA", "outperform", 5, 0.65)), packet)
    summary = strategy._scorecard_summary(NOW)["research_ideas"]
    assert summary["total"] == 1 and summary["scored"] == 1 and summary["recent"][0]["idea_id"].endswith("#1")
    assert summary["verdict"] == "insufficient_sample"
    from scripts.autonomous_fund.notifications import idea_text
    assert "研究想法：共 1 条，已打分 1" in idea_text(strategy._scorecards(NOW)["ideas"])
