"""Notice when the model behind a configured name changes: served versions and a daily fixed probe.

Synthetic answers only; no network, broker or model inference.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from lumibot.components.agents.schemas import AgentRunResult
from scripts.autonomous_fund.factors import FactorSettings
from scripts.autonomous_fund.model_identity import (epochs, knowledge_month, observe_probe, observe_served,
                                                    observed_cutoffs)
from scripts.autonomous_fund.strategy import prompt_version
from tests.test_autonomous_fund_quant import NOW, model_harness

MODEL = "deepseek/synthetic-pro"
DAY = date(2026, 11, 2)
CLOSE = NOW.replace(hour=21)


def answer(cutoff, events=(), name="Synthetic-Pro 4"):
    return {"model_name": name, "knowledge_cutoff": cutoff,
            "latest_events": [{"month": month, "event": "synthetic"} for month in events]}


def test_served_versions_set_a_silent_baseline_and_a_new_one_is_a_change():
    state = {}
    assert observe_served(state, MODEL, ["synthetic-pro-0901"], "t0") == []  # The first seen is the baseline.
    assert observe_served(state, MODEL, ["synthetic-pro-0901"], "t1") == []
    [change] = observe_served(state, MODEL, ["synthetic-pro-1101"], "t2")
    assert change["kind"] == "served_version" and change["before"] == ["synthetic-pro-0901"]
    assert change["after"] == "synthetic-pro-1101" and epochs(state) == {MODEL: 1}
    # A provider that serves both versions in rotation is not a new change each time.
    assert observe_served(state, MODEL, ["synthetic-pro-0901"], "t3") == []
    assert observe_served(state, MODEL, [], "t4") == [] and epochs(state) == {MODEL: 1}


def test_knowledge_months_ignore_invented_future_dates_and_bad_formats():
    assert knowledge_month(answer("2025-06", ["2025-09", "2027-01"]), DAY) == "2025-09"  # 2027 is after the probe.
    assert knowledge_month(answer("June 2025", ["2025-13"]), DAY) is None


def probe_days(state, answers, start=DAY):
    changes = []
    for offset, item in enumerate(answers):
        changes += observe_probe(state, MODEL, item, start + timedelta(days=offset), f"t{offset}")
    return changes


def test_year_zero_is_ignored_in_new_probes_and_legacy_state():
    assert knowledge_month(answer("0000-01", ["0000-12"]), DAY) is None
    state = {}
    probe_days(state, [answer("0000-01")] * 3)
    assert state[MODEL]["baseline_month"] is None
    # Old journals may already contain the invalid month in both fields.
    state[MODEL]["baseline_month"] = state[MODEL]["probes"][0]["month"] = "0000-01"
    assert observed_cutoffs(state) == {}
    probe_days(state, [answer("2024-02")] * 3, DAY + timedelta(days=3))
    assert observed_cutoffs(state) == {MODEL: "2024-02-29"}


def test_sampling_noise_is_not_a_change_but_a_sustained_later_cutoff_is():
    state = {}
    noisy = [answer(m) for m in ("2025-03", "2025-05", "2025-04", "2025-06", "2025-02", "2025-07", "2025-04")]
    assert probe_days(state, noisy) == [] and state[MODEL]["baseline_month"] == "2025-04"  # Median of the first 3.
    # One answer far later is noise too: the median of the last three has to move.
    assert probe_days(state, [answer("2026-01"), answer("2025-04"), answer("2025-05")], DAY + timedelta(days=7)) == []
    [change] = probe_days(state, [answer("2025-10"), answer("2025-11"), answer("2025-10")], DAY + timedelta(days=10))
    assert change["kind"] == "knowledge_later" and (change["before"], change["after"]) == ("2025-04", "2025-10")
    assert state[MODEL]["baseline_month"] == "2025-10" and epochs(state) == {MODEL: 1}
    assert observed_cutoffs(state) == {MODEL: "2025-10-31"}


def test_a_new_self_reported_name_needs_three_agreeing_days_and_must_be_new():
    state = {}
    assert probe_days(state, [answer("2025-04")] * 3) == [] and state[MODEL]["baseline_name"] == "syntheticpro4"
    alternating = [answer("2025-04", name=n) for n in ("Synthetic Pro 4.1", "Synthetic-Pro 4", "Synthetic Pro 4.1")]
    assert probe_days(state, alternating, DAY + timedelta(days=3)) == []
    unknown = [answer("2025-04", name="unknown")] * 3
    assert probe_days(state, unknown, DAY + timedelta(days=6)) == []
    renamed = [answer("2025-04", name="Synthetic Pro 5")] * 3
    [change] = probe_days(state, renamed, DAY + timedelta(days=9))
    assert change["kind"] == "self_reported_name" and change["after"] == "syntheticpro5"


def test_an_observed_cutoff_only_ever_shortens_the_clean_record():
    settings = FactorSettings(model_knowledge_cutoff={MODEL: "2025-06-30"})
    assert settings.cutoff([MODEL], {MODEL: "2025-10-31"}) == date(2025, 10, 31)  # Later: tightened.
    assert settings.cutoff([MODEL], {MODEL: "2024-01-31"}) == date(2025, 6, 30)   # Earlier: never loosened.
    assert FactorSettings().cutoff([MODEL], {MODEL: "2025-10-31"}) is None        # Never invented.


def live(tmp_path):
    from scripts.autonomous_fund.strategy import AutonomousFund
    strategy = model_harness(tmp_path)
    strategy._record_agent = AutonomousFund._record_agent.__get__(strategy)  # The harness stubs it.
    strategy.mode, strategy.scripted = "local", False
    strategy.agent_models = {"researcher": "deepseek/synthetic-flash", "manager": MODEL}
    strategy.triage, strategy.triage_model, strategy.factor_lab = None, None, None
    strategy.model_identity = {}
    strategy.prompt_version = strategy._decision_version()
    strategy._cost_exhausted = lambda now, step: False
    return strategy


def result(served, *, name_model=MODEL, parsed=None):
    return AgentRunResult(summary="{}", model=name_model, events=[], parsed=parsed or {"summary": "s"},
                          served_models=served)


def kinds(strategy, kind):
    return [row["body"] for row in strategy.journal.all_events() if row["kind"] == kind]


def test_a_changed_served_version_starts_a_new_prompt_version_and_tells_the_operator(tmp_path):
    strategy = live(tmp_path)
    original = strategy.prompt_version
    assert original == prompt_version(strategy.agent_models)  # Nothing changed yet: the digest is as before.
    strategy._record_agent("researcher", result(["synthetic-pro-0901"]))
    assert strategy.notices == [] and strategy.prompt_version == original
    strategy._record_agent("researcher", result(["synthetic-pro-1101"]))
    [(kind, details)] = strategy.notices
    assert kind == "model_identity_changed" and details["roles"] == ["manager"] and details["epoch"] == 1
    assert strategy.prompt_version != original
    [changed] = kinds(strategy, "prompt_version_changed")
    assert changed == {"from": original, "to": strategy.prompt_version, "reason": "model_identity"}
    assert strategy.journal.get_meta("model_identity")[MODEL]["epoch"] == 1  # Survives a restart.
    assert kinds(strategy, "agent")[-1]["served_models"] == ["synthetic-pro-1101"]


def test_replays_and_cache_hits_never_count_as_a_served_model(tmp_path):
    strategy = live(tmp_path)
    strategy.mode = "mock"
    strategy._record_agent("researcher", result(["synthetic-pro-0901"]))
    strategy.mode = "local"
    cached = result(["synthetic-pro-1101"])
    cached.cache_hit = True
    strategy._record_agent("researcher", cached)
    assert strategy.model_identity == {} and strategy.notices == []


def test_the_daily_probe_asks_each_model_once_and_isolates_failures(tmp_path):
    strategy = live(tmp_path)
    strategy.get_datetime = lambda: CLOSE
    strategy.probe_agents = {"model_probe_1": "deepseek/synthetic-flash", "model_probe_2": MODEL}
    calls = []

    def call(name, purpose=None, run_counter=None, **kwargs):
        calls.append((name, purpose, run_counter, kwargs["context"]))
        if name == "model_probe_1":
            raise TimeoutError("synthetic")
        return result([], parsed=answer("2025-04"))
    strategy._agent_call = call
    # The daily attempt now starts only once today's regular session has closed.
    answers = strategy._probe_models(CLOSE)
    assert [c[:3] for c in calls] == [("model_probe_1", "model_probe", "probe_runs"),
                                      ("model_probe_2", "model_probe", "probe_runs")]
    assert "ledger_audit" not in calls[1][3]
    assert answers["deepseek/synthetic-flash"] == {"error": "TimeoutError"}
    assert answers[MODEL]["knowledge_cutoff"] == "2025-04"
    assert strategy.model_identity[MODEL]["probes"][0]["month"] == "2025-04"
    assert strategy._probe_models(CLOSE + timedelta(hours=1)) is None and len(calls) == 2  # Once a day.
    strategy.scripted = True
    assert strategy._probe_models(CLOSE + timedelta(days=1)) is None and len(calls) == 2


def test_probe_waits_for_the_exchange_close_and_respects_observe_only(tmp_path):
    strategy = live(tmp_path)
    strategy.probe_agents = {"model_probe_1": MODEL}
    calls = []

    def call(*args, **kwargs):
        calls.append(kwargs["context"]["as_of"])
        return result([], parsed=answer("2025-04"))

    strategy._agent_call = call
    # The Friday after Thanksgiving is a half day. Thanksgiving itself has no session.
    half_close = datetime(2026, 11, 27, 18, tzinfo=timezone.utc)
    for moment in (half_close - timedelta(days=1), half_close - timedelta(seconds=1)):
        strategy.get_datetime = lambda: moment
        assert strategy._probe_models(moment) is None
    strategy.get_datetime = lambda: half_close
    strategy.observe_only = True
    assert strategy._probe_models(half_close) is None
    assert not calls and strategy.journal.get_meta("model_probe_day") is None
    strategy.observe_only = False
    assert MODEL in strategy._probe_models(half_close)
    assert calls == [half_close.isoformat()]


@pytest.mark.parametrize("blocker", ["stop", "cost"])
def test_probe_rechecks_stop_and_cost_between_models(tmp_path, blocker):
    strategy = live(tmp_path)
    strategy.get_datetime = lambda: CLOSE
    strategy.probe_agents = {"model_probe_1": MODEL, "model_probe_2": "deepseek/synthetic-other"}
    calls = []
    strategy._cost_exhausted = lambda now, step: bool(calls) and blocker == "cost"

    def call(name, **kwargs):
        calls.append(name)
        if blocker == "stop":
            strategy.halt_path.write_text("synthetic stop")
        return result([], parsed=answer("2025-04"))

    strategy._agent_call = call
    assert set(strategy._probe_models(CLOSE)) == {MODEL}
    assert calls == ["model_probe_1"]


def test_notice_texts_say_what_changed_and_that_trading_continues():
    from scripts.autonomous_fund.notifications import readable
    text = readable("model_identity_changed", {"model": MODEL, "kind": "served_version", "before": ["v1"],
                                               "after": "v2", "epoch": 1, "roles": ["manager"]})
    assert "模型可能换了" in text and "基金经理" in text and "v1" in text and "v2" in text and "单独统计" in text
    text = readable("model_cutoff_tightened", {"model": MODEL, "configured": "2025-06-30", "observed": "2025-10-31"})
    assert "2025-10-31" in text and "只会更保守" in text


@pytest.mark.parametrize("months", [("2025-04", "2025-04", "2025-04")])
def test_the_lab_receives_observed_cutoffs_when_identity_is_saved(tmp_path, months):
    strategy = live(tmp_path)

    class Lab:
        settings = FactorSettings(model_knowledge_cutoff={MODEL: "2024-12-31"})
        observed_cutoffs = {}
    strategy.factor_lab = Lab()
    probe_days(strategy.model_identity, [answer(m) for m in months])
    strategy._identity_changes([], NOW)
    assert strategy.factor_lab.observed_cutoffs == {MODEL: "2025-04-30"}
    [(kind, details)] = strategy.notices
    assert kind == "model_cutoff_tightened" and details["configured"] == "2024-12-31"
    strategy._identity_changes([], NOW)
    assert len(strategy.notices) == 1  # Told once per observed cutoff.


def test_the_probe_schema_has_a_strict_wire_form_and_accepts_a_plain_answer():
    import jsonschema
    from lumibot.components.agents.deepseek_llm import strict_output_schema
    from scripts.autonomous_fund.model_identity import ModelProbe
    wire = strict_output_schema(ModelProbe.model_json_schema())
    assert set(wire["required"]) == {"model_name", "knowledge_cutoff", "latest_events"}
    jsonschema.validate(answer("2025-04", ["2025-03"]), wire)
    assert ModelProbe.model_validate(answer("2025-04", ["2025-03"])).latest_events[0].month == "2025-03"
