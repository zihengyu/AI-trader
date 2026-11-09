"""Conservation and time-boundary regressions using synthetic accounts and prices."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.performance import component_return, net_returns
from scripts.autonomous_fund.scorecard import score_ideas
from scripts.autonomous_fund.validation import interval_model_costs


NOW = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)


def intervals():
    return [{"start": NOW + timedelta(days=i), "end": NOW + timedelta(days=i, hours=1),
             "equity_start": 10000, "return": .01} for i in range(3)]


def test_an_interrupted_native_call_keeps_system_net_returns_unknown(tmp_path):
    from scripts.autonomous_fund.strategy import AutonomousFund
    strategy = object.__new__(AutonomousFund)
    strategy.journal = Journal(tmp_path, "synthetic-interrupted-account")
    strategy.get_datetime = lambda: NOW + timedelta(minutes=1)
    strategy.agent_models = {"manager": "synthetic/model"}
    strategy.scripted = False
    strategy.agents = {"manager": SimpleNamespace(
        run=lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))}
    with pytest.raises(KeyboardInterrupt):
        strategy._agent_call("manager")
    events = [(r["at"], r["kind"], r["body"]) for r in strategy.journal.all_events()]
    costs = interval_model_costs(events, intervals())
    assert costs == [None, 0, 0]
    result = net_returns(intervals(), costs, 0)
    assert result["system"]["status"] == "unknown_costs"
    assert result["costs_usd"]["complete"] is False
    # Exercise the report's real journal reader, as well as the numerical helper.
    for day in ("2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04"):
        at = f"{day}T21:05:00+00:00"
        strategy.journal.event(at, "account", {"as_of": at, "cash": "10000", "equity": "10000", "positions": {}})
    from scripts.autonomous_fund.validation import acceptance
    report = acceptance(tmp_path, now=datetime(2026, 11, 4, 22, tzinfo=timezone.utc))
    assert report["net_returns"]["system"]["status"] == "unknown_costs"
    assert report["net_returns"]["system"]["sessions_with_unknown_costs"] == 1


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 15}])
def test_an_unpriced_call_is_unknown_regardless_of_usage_shape(usage):
    event = ((NOW + timedelta(minutes=1)).isoformat(), "agent",
             {"model": "synthetic/model", "usage": usage, "cost_usd": None})
    assert interval_model_costs([event], intervals()) == [None, 0, 0]


@pytest.mark.parametrize("body", [
    {"model": "scripted/fixture", "usage": None},
    {"model": "synthetic/model", "mock": True},
    {"model": "synthetic/model", "cache_hit": True},
    {"model": "synthetic/model", "cost_usd": 0},
])
def test_explicitly_free_or_cached_calls_still_cost_zero(body):
    event = ((NOW + timedelta(minutes=1)).isoformat(), "agent", body)
    assert interval_model_costs([event], intervals()) == [0, 0, 0]


def test_cost_intervals_keep_their_open_start_and_closed_end():
    events = [(at.isoformat(), "agent", {"model": "synthetic/model", "usage": None})
              for at in (NOW, NOW + timedelta(hours=1))]
    assert interval_model_costs(events[:1], intervals()) == [0, 0, 0]
    assert interval_model_costs(events[1:], intervals()) == [None, 0, 0]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("actions, end_price", [
    ([{"date": "2026-11-03", "dividend": 2}, {"date": "2026-11-04", "split": 2}], 49),
    ([{"date": "2026-11-03", "split": 2}, {"date": "2026-11-04", "dividend": 1}], 49),
    ([{"date": "2026-11-03", "dividend": 2}, {"date": "2026-11-04", "split": .5}], 196),
    ([{"date": "2026-11-03", "split": 2}, {"date": "2026-11-04", "dividend": 1},
      {"date": "2026-11-05", "split": 2}], 24.5),
])
def test_split_and_dividend_returns_conserve_original_share_wealth(actions, end_price, reverse):
    # Each path leaves one original share worth 100 in shares plus paid cash.
    prices = {"2026-11-02": 100, "2026-11-06": end_price}
    result = component_return(prices, list(reversed(actions)) if reverse else actions,
                              "2026-11-02", "2026-11-06")
    assert result == pytest.approx(0)


@pytest.mark.parametrize("actions", [
    [{"date": "2026-11-03", "dividend": 1}, {"date": "2026-11-03", "split": 2}],
    [{"date": "2026-11-03", "split": 2}, {"date": "2026-11-03", "dividend": 1}],
    [{"date": "2026-11-03", "split": 2, "dividend": 1}],
])
def test_same_day_dividends_use_post_split_shares(actions):
    assert component_return({"2026-11-02": 100, "2026-11-03": 49}, actions,
                            "2026-11-02", "2026-11-03") == pytest.approx(0)


def test_benchmark_actions_outside_the_return_interval_are_excluded():
    actions = [{"date": "2026-11-02", "dividend": 20, "split": 2},
               {"date": "2026-11-04", "dividend": 20, "split": 2}]
    assert component_return({"2026-11-02": 100, "2026-11-03": 101}, actions,
                            "2026-11-02", "2026-11-03") == pytest.approx(.01)


def idea(formed_at, horizon=1):
    return {"symbol": "ALFA", "benchmark": "BENCH", "direction": "up", "horizon_sessions": horizon,
            "confidence": .7, "formed_at": formed_at}


@pytest.mark.parametrize("formed_at, start_day, end_day, status", [
    ("2026-11-27T12:59:00-05:00", "2026-11-27", "2026-11-30", "correct"),
    ("2026-11-27T13:00:00-05:00", "2026-11-30", "2026-12-01", "incorrect"),
    ("2026-11-27T14:00:00-05:00", "2026-11-30", "2026-12-01", "incorrect"),
    ("2026-11-30T15:59:00-05:00", "2026-11-30", "2026-12-01", "incorrect"),
    ("2026-11-28T10:00:00-05:00", "2026-11-30", "2026-12-01", "incorrect"),
])
def test_idea_formation_uses_actual_exchange_close_including_half_days(formed_at, start_day, end_day, status):
    days = ["2026-11-25", "2026-11-27", "2026-11-30", "2026-12-01"]
    closes = {"ALFA": list(zip(days, [100, 100, 110, 99])), "BENCH": [(day, 100) for day in days]}
    [result] = score_ideas([idea(formed_at)], closes)["recent"]
    assert (result["start_day"], result["end_day"], result["status"]) == (start_day, end_day, status)


def test_mature_ideas_use_common_endpoints_despite_missing_intermediate_bars():
    days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]
    closes = {"ALFA": [(days[0], 100), (days[1], 100), (days[-1], 110)],
              "BENCH": [(day, 100) for day in days]}
    [result] = score_ideas([idea("2026-11-03T08:00:00-05:00", horizon=5)], closes)["recent"]
    assert result["status"] == "correct" and result["end_day"] == days[-1]


def test_a_mature_idea_with_a_missing_symbol_endpoint_is_unavailable():
    closes = {"ALFA": [("2026-11-02", 100), ("2026-11-03", 100)],
              "BENCH": [("2026-11-02", 100), ("2026-11-03", 100), ("2026-11-04", 100)]}
    [result] = score_ideas([idea("2026-11-03T08:00:00-05:00")], closes)["recent"]
    assert result["status"] == "unavailable"


def test_historical_calendar_work_is_reused_for_repeated_formation_dates(monkeypatch):
    from scripts.autonomous_fund import market_data, scorecard
    days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    closes = {symbol: [(day, 100 + i) for i, day in enumerate(days)] for symbol in ("ALFA", "BENCH")}
    calls, original = [], market_data._session_close

    def closed(day):
        calls.append(day)
        return original(day)
    monkeypatch.setattr(market_data, "_session_close", closed)
    scorecard._idea_session_close.cache_clear()
    try:
        ideas = [idea(f"{day}T08:00:00-05:00") for day in days[1:3]] * 40
        for _ in range(2):
            assert score_ideas(ideas, closes)["scored"] == len(ideas)
        assert sorted(calls) == days[1:3]
    finally:
        scorecard._idea_session_close.cache_clear()
