"""Post-session feedback, recovery and time isolation without paid inference."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.journal import encode
from scripts.autonomous_fund.report import report
from scripts.autonomous_fund.review import PostmarketReview, _session, strategy_memory
from scripts.autonomous_fund.strategy import Research
from .test_autonomous_fund_premarket import harness


CLOSE = datetime(2026, 11, 2, 21, 5, tzinfo=timezone.utc)
TRADE = CLOSE.replace(hour=15, minute=0)


def answer(body, model):
    return SimpleNamespace(model=model, cache_hit=False, usage={"input_tokens": 11, "output_tokens": 7},
                           summary=body.model_dump_json(), parsed=body, parse_error=None)


def review_harness(tmp_path):
    strategy, _ = harness(tmp_path)
    strategy.get_datetime = lambda: CLOSE
    strategy.session_orders = {}
    strategy.agent_models = {"researcher": "fixture/flash", "manager": "fixture/pro"}
    strategy.journal.event(TRADE, "account", {
        "as_of": TRADE.isoformat(), "cash": "10000", "equity": "10000", "positions": {}})
    strategy.journal.event(TRADE + timedelta(hours=1), "decision_validated", {
        "rationale": "Synthetic decision before observed outcome", "targets": [{"symbol": "ALFA", "weight": .09}]})
    strategy.journal.event(TRADE + timedelta(hours=2), "account", {
        "as_of": (TRADE + timedelta(hours=2)).isoformat(), "cash": "9100", "equity": "10020", "positions": {"ALFA": 9}})
    calls = []

    def research(**kwargs):
        calls.append(("researcher", kwargs))
        return answer(Research(summary="合成证据：缺少收盘估值，不能验证策略优势。", evidence=[{
            "source": "Synthetic timestamped observation", "available_at": TRADE.isoformat(),
            "observed_at": strategy.get_datetime().isoformat(), "estimated": False, "estimate_reason": ""}]), "fixture/flash")

    def manager(**kwargs):
        calls.append(("manager", kwargs))
        context = kwargs["context"]
        adjustment = {"hypothesis": "复查相对强势的持续性后再考虑入场", "rationale": "仅有日内观测，暂不能推断持续性",
                      "evidence_refs": [f"event:{context['session']['events'][0]['sequence']}",
                                        "Synthetic timestamped observation"],
                      "validation": "积累多个后续交易日样本并比较基准与费用", "invalidated_when": "反证表明优势无法覆盖费用"}
        return answer(PostmarketReview(session_date=context["session"]["session_date"], as_of=context["as_of"],
            summary="选股与择时均需后续样本验证。", selection_adjustments=[adjustment], timing_adjustments=[],
            retained_lessons=["验证后再增加确信程度"], retired_hypotheses=[], data_gaps=["没有收盘净值"],
            confidence="low", strategy_memo="复查相对强势持续性，结合新证据自主决定是否入场；现阶段不提高仓位。"), "fixture/pro")

    strategy.agents = {"researcher": SimpleNamespace(run=research), "manager": SimpleNamespace(run=manager)}
    return strategy, calls


def events(strategy, kind):
    with strategy.journal.connection() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT body FROM events WHERE kind=? ORDER BY sequence", (kind,))]


def test_after_close_review_produces_auditable_feedback_without_orders(tmp_path):
    strategy, calls = review_harness(tmp_path)
    strategy.journal.event(TRADE + timedelta(hours=3), "operator_approval", {"id": "synthetic-request", "state": "rejected"})
    strategy.after_market_closes()
    assert [name for name, _ in calls] == ["researcher", "manager"]
    assert calls[-1][1]["output_schema"] is PostmarketReview
    assert all(call["context"]["orders_enabled"] is False for _, call in calls)
    packet = events(strategy, "postmarket_review")[0]
    # Stable IDs avoid copying long source prose; exact legacy names remain traceable.
    allowed = calls[-1][1]["context"]["allowed_evidence_refs"]
    event_refs = [f"event:{row['sequence']}" for row in packet["session"]["events"]]
    assert allowed[:len(event_refs)] == event_refs
    assert {"research:1", "Synthetic timestamped observation", "context:session"} <= set(allowed)
    assert "context:factor_lab" not in allowed  # Absent input is never advertised as evidence.
    assert "allowed_evidence_refs" in calls[-1][1]["task_prompt"]
    assert packet["version"] == 1 and packet["previous_version"] is None
    assert packet["session"]["observed_pnl_usd"] == 20
    assert any(r["kind"] == "operator_approval" and r["body"]["state"] == "rejected" for r in packet["session"]["events"])
    assert packet["session"]["last_observed_account"]["at"] == str(TRADE + timedelta(hours=2))
    assert any("早于收盘" in gap for gap in packet["session"]["account_observation_gaps"])
    assert not strategy.journal.pending() and not events(strategy, "order_submitted")
    assert "选股与择时" in (tmp_path / "postmarket_review.md").read_text(encoding="utf-8")
    assert (tmp_path / "reviews" / "2026-11-02.json").exists()
    memory = strategy_memory(strategy.journal, CLOSE)
    assert "相对强势" in memory["review"]["strategy_memo"]
    assert memory["mock"] is True
    result = report(tmp_path, write_files=False)
    assert result["strategy_memory_version"] == result["postmarket_review_runs"] == 1
    assert result["model_usage_by_model"]["fixture/pro"]["output_tokens"] == 7


def test_restart_and_duplicate_close_do_not_repay_successful_stages(tmp_path):
    strategy, calls = review_harness(tmp_path)
    pro = strategy.agents["manager"].run
    strategy.agents["manager"].run = lambda **kwargs: (_ for _ in ()).throw(ConnectionError("private response"))
    strategy.after_market_closes()
    assert not events(strategy, "postmarket_review")
    assert events(strategy, "postmarket_research")
    assert events(strategy, "notification")[-1]["details"] == "ConnectionError"
    assert report(tmp_path, write_files=False)["model_usage"]["unknown_calls"] == 1
    strategy.agents["manager"].run = pro
    # Reloading from SQLite is deliberate; no in-memory stage state exists.
    strategy.after_market_closes()
    strategy.after_market_closes()
    assert [name for name, _ in calls] == ["researcher", "manager"]
    assert len(events(strategy, "postmarket_review")) == 1


@pytest.mark.parametrize("failure", ["stop", "observe", "before_close", "weekend_no_activity", "historical_end"])
def test_no_spurious_review_or_orders(tmp_path, failure):
    strategy, calls = review_harness(tmp_path)
    if failure == "stop":
        strategy.halt_path.touch()
    elif failure == "observe":
        strategy.observe_only = True
    elif failure == "before_close":
        strategy.get_datetime = lambda: TRADE + timedelta(hours=3)
    elif failure == "weekend_no_activity":
        strategy.get_datetime = lambda: CLOSE - timedelta(days=1)
    else:
        strategy.mode = "backtest"
        strategy.broker.data_source = SimpleNamespace(datetime_end=TRADE)
    strategy.after_market_closes()
    assert not calls
    assert not events(strategy, "postmarket_review")
    assert not strategy.journal.pending()


def test_read_only_review_survives_missing_closing_snapshot(tmp_path):
    strategy, calls = review_harness(tmp_path)
    strategy._snapshot = lambda: (_ for _ in ()).throw(ConnectionError("private response"))
    strategy.after_market_closes()
    assert len(calls) == 2
    assert events(strategy, "notification")[0]["kind"] == "report_error"
    assert events(strategy, "postmarket_review")[0]["session"]["last_observed_account"]["at"] != str(CLOSE)


def test_next_premarket_research_receives_versioned_memo(tmp_path):
    strategy, calls = review_harness(tmp_path)
    strategy.after_market_closes()
    strategy.get_datetime = lambda: CLOSE + timedelta(hours=16, minutes=25)
    strategy.before_market_opens()
    context = calls[-1][1]["context"]
    assert context["phase"] == "before_market_opens"
    assert context["strategy_memory"]["version"] == 1
    assert "持续性" in context["strategy_memory"]["review"]["strategy_memo"]


def test_future_review_never_teaches_historical_strategy(tmp_path):
    strategy, _ = review_harness(tmp_path)
    strategy.after_market_closes()
    assert strategy_memory(strategy.journal, TRADE) is None
    # Even a forged earlier event timestamp cannot hide a later completion.
    with strategy.journal.connection() as db:
        db.execute("UPDATE events SET at=? WHERE kind='postmarket_review'", (str(TRADE),))
    assert strategy_memory(strategy.journal, TRADE) is None


def test_session_calendar_honors_holidays_early_closes_and_new_york_date():
    def account(at):
        return {"at": at, "kind": "account"}
    # Thanksgiving: Thursday is closed; Friday closes 13:00 NY (18:00 UTC).
    rows = [account("2026-11-26T16:00:00+00:00"), account("2026-11-27T17:00:00+00:00")]
    assert _session(rows, datetime.fromisoformat("2026-11-27T17:59:00+00:00")) is None
    assert _session(rows, datetime.fromisoformat("2026-11-27T18:00:00+00:00"))["session_date"] == "2026-11-27"
    # UTC after midnight is still Monday in New York.
    session = _session([account("2026-11-03T00:10:00+00:00")], CLOSE + timedelta(hours=4))
    assert session["session_date"] == "2026-11-02"


@pytest.mark.parametrize("bad", ["targets", "invalid_reference", "future_as_of", "wrong_session"])
def test_untrusted_review_cannot_inject_executable_or_untraceable_output(tmp_path, bad):
    strategy, calls = review_harness(tmp_path)
    pro = strategy.agents["manager"].run

    def malformed(**kwargs):
        result = pro(**kwargs)
        body = result.parsed.model_dump()
        if bad == "targets":
            body["targets"] = [{"symbol": "OUTSIDE", "weight": 1.0}]
        elif bad == "invalid_reference":
            valid = body["selection_adjustments"][0]
            body["timing_adjustments"] = [{**valid, "evidence_refs": ["event:99999"]}]
            valid["evidence_refs"] += ["research:Synthetic timestamped observation", "untrusted model text"]
        elif bad == "future_as_of":
            body["as_of"] = (CLOSE + timedelta(days=1)).isoformat()
        else:
            body["session_date"] = "2026-11-03"
        result.parsed = body
        return result

    strategy.agents["manager"].run = malformed
    strategy.after_market_closes()
    assert not events(strategy, "postmarket_review")
    assert not strategy.journal.pending()
    notice = events(strategy, "notification")[-1]
    assert notice["kind"] == "postmarket_review_error"
    if bad == "invalid_reference":
        # Identify every bad position while keeping untrusted model text out of alerts.
        assert "2026-11-02" in notice["details"]
        assert "selection_adjustments[0].evidence_refs[2,3]" in notice["details"]
        assert "timing_adjustments[0].evidence_refs[0]" in notice["details"]
        assert "untrusted model text" not in notice["details"]
        # One bounded correction is attempted; the still-forged references remain rejected.
        assert [name for name, _ in calls] == ["researcher", "manager", "manager"]


def test_prior_memo_is_reassessed_and_new_versions_are_persisted(tmp_path):
    strategy, calls = review_harness(tmp_path)
    strategy.after_market_closes()
    tomorrow = CLOSE + timedelta(days=1)
    strategy.get_datetime = lambda: tomorrow
    strategy.journal.event(tomorrow - timedelta(hours=4), "account", {
        "cash": "9100", "equity": "9980", "positions": {"ALFA": 9}})
    strategy.after_market_closes()
    assert calls[-2][1]["context"]["strategy_memory"]["version"] == 1
    assert calls[-1][1]["context"]["strategy_memory"]["version"] == 1
    assert [p["version"] for p in events(strategy, "postmarket_review")] == [1, 2]
    assert (tmp_path / "reviews" / "2026-11-02.md").exists()
    assert json.loads((tmp_path / "strategy_memory.json").read_text(encoding="utf-8"))["version"] == 2


def test_future_journal_observations_are_not_included_in_review_inputs(tmp_path):
    strategy, calls = review_harness(tmp_path)
    strategy.journal.event(CLOSE + timedelta(days=1), "account", {
        "cash": "0", "equity": "99999999", "positions": {"FUTURE": 99}})
    strategy.after_market_closes()
    assert "FUTURE" not in encode(calls[0][1]["context"])
    assert calls[0][1]["context"]["session"]["observed_pnl_usd"] == 20


def test_report_can_rebuild_review_files_without_model_calls(tmp_path):
    strategy, calls = review_harness(tmp_path)
    strategy.after_market_closes()
    (tmp_path / "postmarket_review.md").unlink()
    (tmp_path / "strategy_memory.json").unlink()
    report(tmp_path)
    assert len(calls) == 2
    assert (tmp_path / "postmarket_review.md").exists()
    assert (tmp_path / "strategy_memory.json").exists()


def test_a_misquoted_long_source_gets_one_correction_with_stable_refs(tmp_path):
    strategy, calls = review_harness(tmp_path)
    source = "Synthetic SQL exploration (24 assets, 180 sessions; candidate-return rank correlation; exploratory only)"
    researcher, manager = strategy.agents["researcher"].run, strategy.agents["manager"].run
    strategy._factor_context = lambda now: {"validated": [], "trials": 0, "next_critical_t": 2.1}

    def research(**kwargs):
        result = researcher(**kwargs)
        body = result.parsed.model_copy(deep=True)
        body.evidence[0].source = source
        return answer(body, result.model)

    def correctable(**kwargs):
        result = manager(**kwargs)
        body = result.parsed.model_copy(deep=True)
        correction = kwargs["context"].get("reference_correction")
        body.selection_adjustments[0].evidence_refs = (
            ["research:1", "context:factor_lab"] if correction else ["Paraphrased synthetic SQL exploration"])
        return answer(body, result.model)

    strategy.agents["researcher"].run, strategy.agents["manager"].run = research, correctable
    strategy.after_market_closes()
    assert [name for name, _ in calls] == ["researcher", "manager", "manager"]
    [packet] = events(strategy, "postmarket_review")
    assert packet["research"]["evidence"][0]["source"] == source
    assert packet["review"]["selection_adjustments"][0]["evidence_refs"] == ["research:1", "context:factor_lab"]
    catalog = {row["ref"]: row for row in packet["evidence_catalog"]}
    assert catalog["research:1"]["source"] == source
    assert catalog["research:1"]["observed_at"] == CLOSE.isoformat()
    assert catalog["context:factor_lab"]["context_path"] == "factor_lab"
    assert strategy_memory(strategy.journal, CLOSE)["evidence_catalog"] == packet["evidence_catalog"]
    correction = calls[-1][1]["context"]["reference_correction"]
    assert correction["invalid_paths"] == ["selection_adjustments[0].evidence_refs[0]"]
    assert calls[-1][1]["context"]["research"] == calls[-2][1]["context"]["research"]
    assert len(events(strategy, "postmarket_reference_retry")) == 1
    assert len(events(strategy, "agent")) == 3 and not strategy.journal.pending()
    strategy.after_market_closes()
    assert len(calls) == 3  # Successful correction is durable; it never repeats the researcher.


@pytest.mark.parametrize("blocked", ["stop", "cost", "runs"])
def test_reference_correction_respects_stop_and_existing_model_budgets(tmp_path, blocked):
    strategy, calls = review_harness(tmp_path)
    manager = strategy.agents["manager"].run
    strategy.model_budget = SimpleNamespace(max_agent_runs_per_day=2 if blocked == "runs" else 60,
                                            max_daily_cost_usd=None)
    strategy._cost_exhausted = lambda now, step: blocked == "cost"

    def invalid(**kwargs):
        result = manager(**kwargs)
        body = result.parsed.model_copy(deep=True)
        body.selection_adjustments[0].evidence_refs = ["context:unprovided_results"]
        if blocked == "stop":
            strategy.halt_path.touch()
        return answer(body, result.model)

    strategy.agents["manager"].run = invalid
    strategy.after_market_closes()
    assert [name for name, _ in calls] == ["researcher", "manager"]
    assert not events(strategy, "postmarket_review") and not events(strategy, "postmarket_reference_retry")


def test_resuming_research_does_not_advertise_new_context_as_saved_evidence(tmp_path):
    strategy, calls = review_harness(tmp_path)
    strategy._factor_context = lambda now: None
    manager = strategy.agents["manager"].run
    strategy.agents["manager"].run = lambda **kwargs: (_ for _ in ()).throw(ConnectionError("fixture"))
    strategy.after_market_closes()
    assert len(events(strategy, "postmarket_research")) == 1
    strategy._factor_context = lambda now: {"validated": ["newer synthetic observation"]}

    def invalid(**kwargs):
        assert "context:factor_lab" not in kwargs["context"]["allowed_evidence_refs"]
        assert kwargs["context"]["factor_lab"] is None
        result = manager(**kwargs)
        body = result.parsed.model_copy(deep=True)
        body.selection_adjustments[0].evidence_refs = ["context:factor_lab"]
        return answer(body, result.model)

    strategy.agents["manager"].run = invalid
    strategy.after_market_closes()
    assert [name for name, _ in calls] == ["researcher", "manager", "manager"]
    assert not events(strategy, "postmarket_review")
    assert "Postmarket adjustment refers" in events(strategy, "notification")[-1]["details"]
