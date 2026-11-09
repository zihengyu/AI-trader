"""End-to-end soak of one simulated session through the native local executor.

Real application code paths (native Strategy lifecycle, local virtual broker,
journal, risk construction, watcher, review, learning evaluation, acceptance)
with a scripted model and a controllable synthetic market. No network.
"""
import json
from datetime import datetime, timedelta, timezone

from scripts.autonomous_fund.risk import Limits


def test_one_session_decides_fills_stops_out_wakes_ai_reviews_and_reports(tmp_path, monkeypatch):
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import AutonomousFund, open_journal
    from scripts.autonomous_fund.validation import acceptance
    from .test_autonomous_fund_local import Clock
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    clock = Clock()
    clock.price, clock.high = 100.0, 100.01
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "mandate": {"capital_usd": "10000"},
              "validation": {"shadow_enabled": True},
              "universe": {"ALFA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
    journal = open_journal(tmp_path, "local", "local-virtual-account", config)
    seen = []

    class Market(ScriptedRuntime):
        def run(self, request):
            seen.append((request.agent_name, request.context.get("phase"), request.context))
            result = super().run(request)
            if request.agent_name == "manager" and request.context.get("phase") != "postmarket_review":
                body = json.loads(result.summary)
                body["targets"] = [{"symbol": "ALFA", "weight": .05, "stop_loss": 98.0}]
                result.summary = json.dumps(body)
            return result

    broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
    strategy = AutonomousFund(broker=broker, name="Soak", parameters={
        "config": config, "run_dir": str(tmp_path), "mode": "local", "account_binding": "local-virtual-account",
        "runtime": Market()})
    strategy._executor._initialize()

    def step(minutes, poll=True):
        clock.value += timedelta(minutes=minutes)
        source._cache.clear()
        if poll:
            broker.do_polling()

    # 1. Decision (+ one blind shadow), automatic purchase inside the envelope, fill.
    strategy.on_trading_iteration()
    # Opening assessments use saved research and no new researcher call; the
    # optional critique remains shared by the trading and shadow managers.
    assert [(name, phase) for name, phase, _ in seen] == [("critic", None), ("manager", None),
                                                          ("manager", None)]
    assert seen[2][2]["strategy_memory"] is None  # The shadow arm is the no-memo baseline.
    assert seen[2][2]["critique"] == seen[1][2]["critique"] is not None  # Both arms see the same critique.
    step(2)
    assert [p.quantity for p in broker._pull_positions(strategy.name)] == [4]  # 5% of $10,000 at ~$100 with buffer.
    # 2. The price falls through the AI's stop: a protective sale needs no approval.
    clock.price, clock.high = 97.0, 97.01
    step(1, poll=False)
    strategy.on_trading_iteration()
    submitted = [row["body"] for row in journal.all_events() if row["kind"] == "order_submitted"]
    assert submitted[-1]["side"] == "sell" and submitted[-1]["kind"] == "protective"
    step(2)
    assert broker._pull_positions(strategy.name) == []
    # 3. The stop brought the next AI review forward to the 15-minute floor.
    step(13, poll=False)
    strategy.on_trading_iteration()
    managers = [ctx for name, phase, ctx in seen if name == "manager" and phase is None]
    assert len(managers) == 3 and managers[-1]["watch_triggers"][0]["reason"] == "stop_loss"
    # 4. Close: post-market review, learning evaluation and the acceptance evidence.
    clock.value = datetime(2026, 11, 2, 21, 5, tzinfo=timezone.utc)
    source._cache.clear()
    strategy._executor._after_market_closes()
    rows = journal.all_events()
    kinds = [row["kind"] for row in rows]
    assert "postmarket_review" in kinds and "shadow_decision" in kinds
    review_at = max(row["sequence"] for row in rows if row["kind"] == "postmarket_review")
    evaluations = [row for row in rows if row["kind"] == "learning_evaluation"]
    assert len(evaluations) == 1 and evaluations[0]["sequence"] > review_at  # Evaluated after the close review.
    assert sum(1 for name, phase, _ in seen if phase == "postmarket_review") == 2
    # The experiment registry (V2 4.1) syncs after the close review, without an error.
    from scripts.autonomous_fund.experiments import Registry
    states = {row["id"]: row["state"] for row in Registry(journal).rows()}
    assert {"learning:memo": "paper_active", "learning:baseline": "shadow"}.items() <= states.items()
    assert states.get("construction:ai") == "paper_active" and "predictions:research_ideas" in states
    assert not [row for row in rows if row["kind"] == "operation_error" and "experiment" in json.dumps(row["body"])]
    result = acceptance(tmp_path, {"min_sessions": 1, "min_iteration_ratio": 0}, now=clock())
    metrics = result["metrics"]
    assert metrics["protective_orders"] == 1 and metrics["fills"] >= 2 and metrics["review_ratio"] == 1
    assert metrics["unresolved_intents"] == 0 and metrics["mean_slippage_bps_vs_arrival"] is not None
    assert result["verdict"] == "ready", result["criteria"]
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["protective_triggers"] and report["mandate"]["capital_usd"] == "10000"
    broker._close_connection()
