"""Operator mandate, minute-level AI execution levels, scorecards and research network.

Synthetic accounts, prices and model answers only: no network, broker or inference.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from lumibot.entities import Asset, Order
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import Intent, Limits, RiskError, number, validate_decision
from .test_autonomous_fund_quant import DAYS, LIMITS, NOW, UNIVERSE, account, default_frames, harness, quote


def at(minutes=0):
    return NOW + timedelta(minutes=minutes)


def decision_body(targets, alerts=(), when=None):
    when = when or NOW
    return {"as_of": when.isoformat(), "expires_at": (when + timedelta(hours=4)).isoformat(),
            "next_review_at": (when + timedelta(hours=2)).isoformat(), "rationale": "Synthetic plan",
            "targets": targets, "alerts": list(alerts)}


def watching(tmp_path, quotes, mandate=None):
    """Quant harness wired for _watch/_submit with in-memory broker doubles."""
    from scripts.autonomous_fund.strategy import Mandate
    strategy = harness(tmp_path, default_frames())
    strategy.mandate = Mandate.from_config(mandate)
    strategy.next_review = None
    strategy._price = lambda symbol, record=True: quotes[symbol]
    strategy.create_order = lambda asset, quantity, side, **kw: Order("quant-test", asset, quantity, side,
                                                                      limit_price=kw["limit_price"])
    return strategy


@pytest.fixture
def sent_orders(monkeypatch):
    from lumibot.strategies import Strategy
    sent = []
    def submit(self, order, **kwargs):
        order.identifier = f"order-{len(sent) + 1}"
        sent.append(order)
    monkeypatch.setattr(Strategy, "submit_order", submit)
    return sent


def accept(strategy, targets, alerts=(), when=None):
    when = when or NOW
    strategy.execution.accept(decision_body(targets, alerts, when), when, strategy.universe, LIMITS)
    return strategy.execution.active(when, strategy.universe, LIMITS)


# ----- Operator mandate: "the account has $50,000; the AI may use $10,000" ----------

def test_mandate_carves_the_capital_sleeve_and_moves_cash_only_by_operator_policy(tmp_path):
    journal = Journal(tmp_path, "synthetic-mandate")
    reserve = journal.apply_mandate(NOW, "10000", number(50000), number(50000), {}, False, {}, True)
    assert reserve == number(40000)
    assert journal.apply_mandate(NOW, "10000", number(50000), number(50000), {}, False, {}, True) == number(40000)
    assert journal.apply_mandate(NOW, "15000", number(50000), number(50000), {}, False, {}, True) == number(35000)
    with pytest.raises(RiskError, match="exceeds the sleeve's cash"):
        journal.apply_mandate(NOW, "5000", number(38000), number(50000), {"ALFA": 120}, False, {}, True)
    with pytest.raises(RiskError, match="exceeds the account's reserved cash"):
        journal.apply_mandate(NOW, "60000", number(50000), number(50000), {}, False, {}, True)
    assert journal.adjust_reserve(NOW, number(-5000)) == number(30000)
    with pytest.raises(RiskError, match="negative"):
        journal.adjust_reserve(NOW, number(-40000))
    kinds = [row["kind"] for row in journal.all_events()]
    assert kinds.count("mandate_initialized") == 1 and kinds.count("mandate_capital_changed") == 1
    assert kinds.count("operator_reserve_adjusted") == 1


@pytest.mark.parametrize("cash, value, holdings, orders, message", [
    (5000, 5000, {}, False, "below the mandate capital"),
    (1000, 12000, {"ALFA": 110}, False, "exceed the mandate capital"),
    (40000, 50000, {"ALFA": 100}, False, "requires no eligible"),
    (50000, 50000, {}, True, "requires no eligible"),
])
def test_mandate_start_refuses_accounts_it_cannot_attribute(tmp_path, cash, value, holdings, orders, message):
    journal = Journal(tmp_path, "synthetic-mandate")
    clean = cash != 1000  # A local book may adopt existing positions only within capital.
    with pytest.raises(RiskError, match=message):
        journal.apply_mandate(NOW, "10000", number(cash), number(value), holdings, orders, {}, clean)


def snapshot_harness(tmp_path, cash, equity, positions, mandate):
    from scripts.autonomous_fund.strategy import Mandate
    strategy = harness(tmp_path, default_frames())
    strategy.mandate = Mandate.from_config(mandate)
    strategy._quote_asset = Asset("USD", asset_type="forex")
    state = {"cash": cash, "equity": equity, "positions": positions}
    strategy.get_orders = lambda: []
    strategy.get_positions = lambda: [SimpleNamespace(asset=Asset(s, asset_type="stock"), quantity=q)
                                      for s, q in state["positions"].items()]
    strategy.get_cash = lambda: state["cash"]
    strategy.get_portfolio_value = lambda: state["equity"]
    strategy.get_last_price = lambda asset: 200 if asset.symbol == "TSLA" else 100
    return strategy, state


def test_ai_only_sees_its_capital_sleeve_and_external_holdings_stay_frozen(tmp_path):
    strategy, state = snapshot_harness(tmp_path, 48000, 50000, {"TSLA": 10},
                                       {"capital_usd": "10000", "allow_external_holdings": True})
    sleeve = strategy._snapshot()
    assert (sleeve.cash, sleeve.equity, sleeve.positions) == (number(10000), number(10000), {})
    state.update(cash=47000, equity=50000, positions={"TSLA": 10, "ALFA": 10})  # AI bought $1,000 of ALFA.
    sleeve = strategy._snapshot()
    assert (sleeve.cash, sleeve.equity, sleeve.positions) == (number(9000), number(10000), {"ALFA": number(10)})
    state.update(positions={"TSLA": 12, "ALFA": 10})  # The operator traded the external holding.
    with pytest.raises(RiskError, match="External holdings changed"):
        strategy._snapshot()
    broker = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "broker_account"]
    assert broker[-1]["reserved_cash"] == "38000" and broker[-1]["external_value"] == "2000"


def test_external_holdings_are_refused_unless_the_operator_allows_them(tmp_path):
    strategy, _ = snapshot_harness(tmp_path, 48000, 50000, {"TSLA": 10}, {"capital_usd": "10000"})
    with pytest.raises(RiskError, match="unsupported asset"):
        strategy._snapshot()


def test_reserved_cash_can_never_fund_purchases(tmp_path):
    strategy, state = snapshot_harness(tmp_path, 50000, 50000, {}, {"capital_usd": "10000"})
    strategy._snapshot()
    state.update(cash=39000, equity=50000, positions={"ALFA": 110})  # Sleeve over-spent into the reserve.
    with pytest.raises(RiskError, match="Sleeve cash is negative"):
        strategy._snapshot()


def test_local_virtual_fills_use_sleeve_cash_and_sleeve_equity(tmp_path):
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from .test_autonomous_fund_local import Clock, advance, submit
    clock = Clock()
    source = YahooRealtimeData(now=clock, fetch=clock.fetch)
    journal = Journal(tmp_path, "synthetic-local-sleeve")
    broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False, starting_cash="50000")
    journal.apply_mandate(clock(), "300", number(50000), number(50000), {}, False, {}, False)
    submit(broker, journal, clock(), quantity=5, limit=101)  # $505 > the $300 sleeve.
    advance(clock, source)
    broker.do_polling()
    assert not broker._pull_positions("LocalTest")
    assert any(row["kind"] == "local_risk_canceled" for row in journal.all_events())
    broker._close_connection()


def test_mandate_instructions_reach_the_manager_and_review_reports_compliance(tmp_path):
    from scripts.autonomous_fund.review import PostmarketReview
    from scripts.autonomous_fund.strategy import Mandate
    mandate = Mandate.from_config({"capital_usd": "10000", "instructions": ["财报发布前两个交易日内不新建仓位。"]})
    context = mandate.context()
    assert context["no_leverage"] and context["instructions"] == ["财报发布前两个交易日内不新建仓位。"]
    review = PostmarketReview.model_validate({
        "session_date": "2026-11-02", "as_of": NOW.isoformat(), "summary": "s", "selection_adjustments": [],
        "timing_adjustments": [], "retained_lessons": [], "retired_hypotheses": [], "data_gaps": [],
        "confidence": "low", "strategy_memo": "m", "mandate_compliance": "未在财报窗口新建仓位。"})
    assert review.predictions == [] and review.mandate_compliance
    with pytest.raises(RiskError):
        Mandate.from_config({"capital_usd": "-1"})
    with pytest.raises(RiskError):
        Mandate.from_config({"instructions": ["x" * 501]})


# ----- AI execution levels and the minute-level watcher ------------------------------

@pytest.mark.parametrize("target, alerts", [
    ({"symbol": "ALFA", "weight": .05, "entry_limit": 95, "stop_loss": 96}, []),
    ({"symbol": "ALFA", "weight": .05, "stop_loss": 110, "take_profit": 105}, []),
    ({"symbol": "ALFA", "weight": .05, "take_profit_weight": .01}, []),
    ({"symbol": "ALFA", "weight": .05, "take_profit": 110, "take_profit_weight": .05}, []),
    ({"symbol": "ALFA", "weight": .05}, [{"symbol": "OUTSIDE", "above": 1, "reason": "r"}]),
    ({"symbol": "ALFA", "weight": .05}, [{"symbol": "ALFA", "above": 100, "below": 101, "reason": "r"}]),
    ({"symbol": "ALFA", "weight": .05}, [{"symbol": "ALFA", "reason": "r"}]),
])
def test_inconsistent_ai_levels_and_alerts_are_rejected(target, alerts):
    with pytest.raises(RiskError):
        validate_decision(decision_body([target], alerts), NOW, UNIVERSE, LIMITS)


def test_stop_loss_exits_automatically_without_approval_and_blocks_rebuy(tmp_path, sent_orders):
    quotes = {"ALFA": quote(94)}
    strategy = watching(tmp_path, quotes)
    active = accept(strategy, [{"symbol": "ALFA", "weight": .05, "stop_loss": 95}], when=at(-30))
    held = account(9530, {"ALFA": 5}, price=94)
    assert strategy._watch(held, NOW) is True
    order = sent_orders[0]
    assert (order.asset.symbol, str(order.side), order.quantity) == ("ALFA", "sell", 5)
    events = {row["kind"] for row in strategy.journal.all_events()}
    assert {"protective_trigger", "review_triggered", "order_submitted"} <= events
    assert strategy.next_review == NOW  # Woken early: the review floor has already passed.
    with strategy.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_approvals").fetchone()[0] == 0
    strategy.journal.update(strategy.journal.pending()[0]["id"], "order-1", True, NOW)
    rebuy = strategy._candidates(active, account(), {"ALFA": quote(94)}, NOW)
    assert rebuy["orders"] == []  # The stopped symbol is capped at zero for this decision.


def test_take_profit_reduces_to_the_chosen_weight_and_retries_until_done(tmp_path, sent_orders):
    quotes = {"ALFA": quote(111)}
    strategy = watching(tmp_path, quotes)
    accept(strategy, [{"symbol": "ALFA", "weight": .05, "take_profit": 110, "take_profit_weight": .02}], when=at(-30))
    held = account(9445, {"ALFA": 5}, price=111)
    assert strategy._watch(held, NOW)
    assert sent_orders[-1].quantity == 4  # Keep floor($200 / $111.11) = 1 share.
    # The limit was canceled unfilled for repricing: the exit is retried, never forgotten.
    strategy.journal.update(strategy.journal.pending()[0]["id"], "order-1", True, NOW)
    assert strategy._watch(held, at(3)) and sent_orders[-1].quantity == 4
    strategy.journal.update(strategy.journal.pending()[0]["id"], "order-2", True, at(3))
    assert strategy._watch(account(9889, {"ALFA": 1}, price=111), at(6)) is False
    assert strategy.execution.levels() == {}
    with strategy.journal.connection() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='protective_trigger'").fetchone()[0] == 1


def test_protective_exit_survives_an_expired_allocation_and_is_only_repriced_when_stale(tmp_path, sent_orders):
    strategy = watching(tmp_path, {"ALFA": quote(94)})
    accept(strategy, [{"symbol": "ALFA", "weight": .05, "stop_loss": 95}], when=at(-300))  # Expired at -60.
    assert strategy.execution.active(NOW, strategy.universe, LIMITS) is None
    assert strategy._watch(account(9530, {"ALFA": 5}, price=94), NOW)
    key = strategy.journal.pending()[0]["id"]
    strategy.session_orders[key].status = "new"
    canceled = []
    strategy.cancel_order = canceled.append
    strategy._manage_open_orders(at(0.5))
    assert canceled == []  # Not canceled merely because the earlier target expired.
    strategy._manage_open_orders(at(1.5))
    assert len(canceled) == 1  # Stale after the protective reprice interval: cancel so it can be re-placed.


def test_entry_limit_waits_for_the_ai_price_before_buying(tmp_path):
    strategy = watching(tmp_path, {})
    active = accept(strategy, [{"symbol": "ALFA", "weight": .05, "entry_limit": 99}])
    waiting = strategy._candidates(active, account(), {"ALFA": quote(100)}, NOW)
    assert waiting["orders"] == [] and not waiting["complete"] and waiting["request"] is None
    ready = strategy._candidates(active, account(), {"ALFA": quote("98.9")}, NOW)
    assert ready["orders"][0].side == "buy"
    assert sum(row["kind"] == "entry_waiting" for row in strategy.journal.all_events()) == 1


def test_alerts_and_large_moves_wake_the_ai_early_but_respect_the_review_floor(tmp_path):
    quotes = {"ALFA": quote(100), "BETA": quote(100)}
    strategy = watching(tmp_path, quotes)
    accept(strategy, [{"symbol": "BETA", "weight": .05}],
           [{"symbol": "ALFA", "above": 102, "reason": "breakout confirms the thesis"}], when=at(-5))
    strategy.next_review = at(115)
    assert strategy._watch(account(), NOW) is False  # Records reference prices; nothing crossed.
    quotes["ALFA"] = quote("102.5")  # Crosses the alert; a 2.5% move stays below the move trigger.
    strategy._watch(account(), at(1))
    assert strategy.next_review == at(10)  # Accepted at -5; 15-minute floor.
    quotes["BETA"] = quote(104)
    strategy._watch(account(), at(2))
    reasons = [row["body"]["reason"] for row in strategy.journal.all_events() if row["kind"] == "review_triggered"]
    assert reasons == ["price_alert", "price_move"]
    assert [t["reason"] for t in strategy._triggers] == ["price_alert", "price_move"]
    strategy._watch(account(), at(3))
    assert len([r for r in strategy.journal.all_events() if r["kind"] == "review_triggered"]) == 2  # Once each.


def test_failure_backoff_is_not_bypassed_by_an_early_review_trigger(tmp_path):
    strategy = watching(tmp_path, {"ALFA": quote(100)})
    accept(strategy, [{"symbol": "ALFA", "weight": .05}], when=at(-60))
    strategy.journal.set_meta("decision_retry_at", at(30).isoformat())
    strategy._request_review(NOW, "price_alert")
    assert strategy.next_review == at(30)


def test_new_ai_decision_replaces_standing_levels_and_alerts(tmp_path):
    strategy = watching(tmp_path, {})
    accept(strategy, [{"symbol": "ALFA", "weight": .05, "stop_loss": 90}],
           [{"symbol": "BETA", "below": 80, "reason": "r"}], when=at(-30))
    assert set(strategy.execution.levels()) == {"ALFA"} and len(strategy.execution.alerts()) == 1
    accept(strategy, [{"symbol": "BETA", "weight": .05, "take_profit": 120}])
    assert set(strategy.execution.levels()) == {"BETA"} and strategy.execution.alerts() == []


def test_watch_triggers_and_scorecards_reach_the_manager_then_clear(tmp_path):
    from .test_autonomous_fund_quant import model_harness
    strategy = model_harness(tmp_path)
    strategy._triggers = [{"reason": "stop_loss", "symbol": "ALFA"}]
    seen = {}
    evidence = [{"source": "Synthetic", "available_at": (NOW - timedelta(days=1)).isoformat(),
                 "observed_at": NOW.isoformat(), "estimated": False, "estimate_reason": ""}]
    def call(name, **kwargs):
        seen[name] = kwargs["context"]
        return {"summary": "s", "evidence": evidence} if name == "researcher" else decision_body(
            [{"symbol": "ALFA", "weight": .05, "stop_loss": 90}])
    strategy._agent_call = call
    strategy._decide(account(), NOW, False)
    assert seen["manager"]["watch_triggers"] == [{"reason": "stop_loss", "symbol": "ALFA"}]
    assert "stop_loss" in seen["manager"]["execution_levels_supported"]
    assert seen["manager"]["operator_mandate"]["no_leverage"] is True
    assert strategy._triggers == [] and set(strategy.execution.levels()) == {"ALFA"}


# ----- Objective scorecards --------------------------------------------------------

def closes(start, step):
    return [(str(day.date()), start + i * step) for i, day in enumerate(DAYS)]


def test_predictions_are_scored_only_on_completed_closes():
    from scripts.autonomous_fund.scorecard import score_predictions
    day = str(DAYS[-6].date())
    review = {"version": 3, "session_date": day, "review": {"predictions": [
        {"symbol": "ALFA", "direction": "outperform", "horizon_sessions": 5, "benchmark": "SPY", "hypothesis_ref": "h1"},
        {"symbol": "BETA", "direction": "up", "horizon_sessions": 5, "benchmark": "SPY", "hypothesis_ref": "h1"},
        {"symbol": "ALFA", "direction": "up", "horizon_sessions": 6, "benchmark": "SPY", "hypothesis_ref": "h2"},
        {"symbol": "MISSING", "direction": "up", "horizon_sessions": 1, "benchmark": "SPY", "hypothesis_ref": ""}]}}
    data = {"ALFA": closes(100, 1), "BETA": closes(400, -1), "SPY": closes(100, .1)}
    result = score_predictions([review], data)
    assert [r["status"] for r in result["recent"]] == ["correct", "incorrect", "pending", "unavailable"]
    assert result["hit_rate"] == .5 and result["by_hypothesis"]["h1"] == {"scored": 2, "correct": 1, "hit_rate": .5}


def test_decision_scorecard_measures_excess_at_equal_exposure_from_the_decision_close():
    from scripts.autonomous_fund.scorecard import score_decisions
    decided = f"{DAYS[-6].date()}T15:00:00+00:00"
    body = {"targets": [{"symbol": "ALFA", "weight": .5}]}
    data = {"ALFA": closes(100, 1), "SPY": closes(100, 0)}
    result = score_decisions([(decided, body)], data)
    one = result["recent"][0]["horizons"]
    start = 100 + (len(DAYS) - 6)
    assert one["1"]["portfolio_return"] == pytest.approx(.5 * (1 / start))
    assert one["1"]["excess_return"] == pytest.approx(one["1"]["portfolio_return"])
    assert one["5"]["status"] == "observed" and one["20"]["status"] == "pending"
    assert result["by_horizon"]["1"]["positive_excess_rate"] == 1


def test_strategy_scorecards_read_reviews_and_one_decision_per_session(tmp_path):
    strategy = watching(tmp_path, {})
    day = str(DAYS[-3].date())
    strategy.journal.event(f"{day}T21:00:00+00:00", "postmarket_review", {"version": 1, "session_date": day,
        "completed_at": f"{day}T21:00:00+00:00", "market_close": f"{day}T21:00:00+00:00", "mock": True,
        "review": {"predictions": [{"symbol": "ALFA", "direction": "up", "horizon_sessions": 1,
                                    "benchmark": "SPY", "hypothesis_ref": "h"}]}})
    for hour in (14, 15, 16):
        strategy.journal.event(f"{day}T{hour}:00:00+00:00", "decision_validated",
                               {"targets": [{"symbol": "ALFA", "weight": .05}]})
    strategy.journal.event(f"{day}T15:30:00+00:00", "protective_trigger",
                           {"symbol": "BETA", "trigger": "stop_loss", "bid": "100", "level": {}, "target_weight": "0"})
    cards = strategy._scorecards(NOW)
    assert cards["predictions"]["total"] == 1 and cards["predictions"]["scored"] == 1
    assert cards["decisions"]["decisions"] == 1
    stops = cards["timing"]["protective_exits"]["stop_loss"]
    assert stops["count"] == 1 and stops["1"]["observed"] == 1 and stops["5"]["observed"] == 0  # Later: pending.
    summary = strategy._scorecard_summary(NOW)
    assert set(summary) == {"predictions", "decisions", "timing"} and "recent" not in summary["timing"]


def test_timing_scorecard_grades_the_ais_own_exits_and_entry_waits():
    from scripts.autonomous_fund.scorecard import score_timing
    days = [str(d.date()) for d in DAYS[:25]]
    path = lambda values: list(zip(days, values))  # noqa: E731
    falls_then_rebounds = [100, 98, 96, 94, 92, 90] + [95] * 14 + [105] * 5
    rises = [50 + i for i in range(25)]
    closes = {"ALFA": path(falls_then_rebounds), "BETA": path(rises), "GAMMA": path([50] * 25),
              "DELTA": path([20, 20.5, 21, 21.5, 21.8, 22] + [22] * 19)}
    start = f"{days[0]}T15:00:00+00:00"
    events = [
        (start, "protective_trigger", {"symbol": "ALFA", "trigger": "stop_loss", "bid": "100"}),
        (start, "protective_trigger", {"symbol": "BETA", "trigger": "take_profit", "bid": "50"}),
        (start, "entry_waiting", {"decision_id": "d1", "symbol": "GAMMA", "entry_limit": "49", "ask": "50"}),
        (f"{days[1]}T15:00:00+00:00", "order_submitted",
         {"kind": "allocation", "decision_id": "d1", "symbol": "GAMMA", "side": "buy", "limit": "49"}),
        (start, "entry_waiting", {"decision_id": "d1", "symbol": "DELTA", "entry_limit": "19", "ask": "20"}),
    ]
    card = score_timing(events, closes)
    stop = card["protective_exits"]["stop_loss"]
    assert stop["1"]["mean_value_added"] == pytest.approx(0.02) and stop["5"]["mean_value_added"] == pytest.approx(0.10)
    assert stop["20"]["mean_value_added"] == pytest.approx(-0.05)  # The stop gave up the later rebound.
    assert card["protective_exits"]["take_profit"]["5"]["mean_value_added"] == pytest.approx(-0.10)
    waits = card["entry_waits"]
    assert waits["count"] == 2 and waits["entered"] == 1 and waits["mean_entry_improvement"] == pytest.approx(0.02)
    assert waits["not_entered"]["5"]["mean_value_added"] == pytest.approx(-0.10)  # The wait missed a 10% gain.
    late = score_timing([(f"{days[-1]}T15:00:00+00:00", "protective_trigger",
                          {"symbol": "ALFA", "trigger": "stop_loss", "bid": "105"})], closes)
    assert late["protective_exits"]["stop_loss"]["1"]["observed"] == 0  # Never scored before the close exists.


# ----- Research network and data boundary -------------------------------------------

def test_doh_resolver_returns_public_answers_and_caches_by_ttl():
    from scripts.autonomous_fund.boundary import DohResolver
    calls = []
    def get(url, params, headers, timeout):
        calls.append(params["type"])
        answers = {"A": [{"type": 1, "data": "93.184.216.34", "TTL": 120}], "AAAA": []}[params["type"]]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"Answer": answers})
    clock = [0.0]
    resolver = DohResolver("https://1.1.1.1/dns-query", get=get, clock=lambda: clock[0])
    assert resolver("www.sec.gov") == ["93.184.216.34"] and resolver("www.sec.gov") == ["93.184.216.34"]
    assert calls == ["A", "AAAA"]
    clock[0] = 121
    resolver("www.sec.gov")
    assert len(calls) == 4


def test_diagnosis_flags_fake_ip_dns_with_actionable_advice():
    from scripts.autonomous_fund.boundary import diagnose
    fake = diagnose(lambda host: ["198.18.0.42"], hosts=("www.sec.gov",))
    assert fake["hosts"]["www.sec.gov"]["status"] == "fake_ip_dns" and not fake["research_http_usable"]
    assert "doh" in fake["advice"]
    assert diagnose(lambda host: ["93.184.216.34"], hosts=("www.sec.gov",))["research_http_usable"]


def test_doh_mode_uses_the_native_web_client_and_keeps_ssrf_protection(tmp_path):
    import duckdb
    from lumibot.components.agents.web_tools import WebClient
    from scripts.autonomous_fund.boundary import isolate_research
    strategy = SimpleNamespace(agents=SimpleNamespace(duckdb=SimpleNamespace(connection=duckdb.connect())))
    isolate_research(strategy, tmp_path, {"resolver": "doh"})
    client = strategy._agent_web_client
    assert isinstance(client, WebClient)
    client._resolver = lambda host: ["10.0.0.5"]
    with pytest.raises(ValueError, match="private"):
        client._validate_and_pin("https://internal.example/")
    client._resolver = lambda host: ["93.184.216.34"]
    assert client._validate_and_pin("https://www.sec.gov/") == ("www.sec.gov", "93.184.216.34")


def test_research_sql_reads_downloaded_files_but_never_the_journal(tmp_path):
    import duckdb
    from scripts.autonomous_fund.boundary import isolate_research
    connection = duckdb.connect()
    strategy = SimpleNamespace(agents=SimpleNamespace(duckdb=SimpleNamespace(connection=connection)))
    isolate_research(strategy, tmp_path)
    download = tmp_path / "browser" / "filing.csv"
    download.write_text("revenue\n10\n20\n", encoding="utf-8")
    path = str(download.resolve()).replace("\\", "/")
    assert connection.execute(f"SELECT sum(revenue) FROM read_csv('{path}')").fetchone()[0] == 30
    Journal(tmp_path, "synthetic-journal")
    journal = str((tmp_path / "state.sqlite3").resolve()).replace("\\", "/")
    with pytest.raises(duckdb.Error):
        connection.execute(f"SELECT * FROM read_text('{journal}')")
    connection.close()


def test_operator_reserve_command_records_deposits_while_the_runner_is_stopped(tmp_path, capsys):
    from scripts.autonomous_fund import run
    journal = Journal(tmp_path, "synthetic-cli")
    journal.apply_mandate(NOW, "10000", number(50000), number(50000), {}, False, {}, True)
    run.main(["adjust-reserve", "--run-dir", str(tmp_path), "--amount", "2500"])
    assert "42500" in capsys.readouterr().out
    assert journal.reserve_cash() == number(42500)


# ----- Review-pass regressions -------------------------------------------------------------

def pending_order(strategy, symbol="ALFA", side="buy", quantity=5, limit="100.10", decision_id="d", at=None):
    key = strategy.journal.reserve(Intent(symbol, side, quantity, number(limit)), NOW, LIMITS)
    strategy.session_orders[key] = Order("quant-test", Asset(symbol), quantity, side, limit_price=float(limit), status="new")
    strategy.execution.record_submission(key, decision_id, Intent(symbol, side, quantity, number(limit)), at or NOW)
    return key


def test_cancel_requests_are_not_repeated_every_iteration_while_the_broker_confirms(tmp_path):
    strategy = watching(tmp_path, {"ALFA": quote(100)})
    active = accept(strategy, [{"symbol": "ALFA", "weight": .05}], when=at(-30))
    pending_order(strategy, decision_id=active["id"], at=at(-10))
    canceled = []
    strategy.cancel_order = canceled.append
    for minute in range(4):  # The broker keeps reporting "cancelling" for a while.
        strategy._manage_open_orders(at(minute))
    assert len(canceled) == 1
    events = [r for r in strategy.journal.all_events() if r["kind"] == "order_reprice_cancel"]
    assert len(events) == 1
    strategy._manage_open_orders(at(3))
    strategy._manage_open_orders(at(5))  # Re-request only after the retry interval.
    assert len(canceled) == 2


def test_a_pending_buy_is_canceled_at_once_when_its_stop_is_breached(tmp_path):
    strategy = watching(tmp_path, {"ALFA": quote(94)})
    active = accept(strategy, [{"symbol": "ALFA", "weight": .05, "stop_loss": 95}], when=at(-30))
    pending_order(strategy, decision_id=active["id"], at=at(-0.5))  # Fresh: not stale for repricing.
    canceled = []
    strategy.cancel_order = canceled.append
    strategy._manage_open_orders(NOW)
    assert len(canceled) == 1
    assert any(r["kind"] == "buy_canceled_stop_breached" for r in strategy.journal.all_events())


def test_no_purchase_below_the_ais_own_stop(tmp_path):
    strategy = watching(tmp_path, {})
    active = accept(strategy, [{"symbol": "ALFA", "weight": .05, "stop_loss": 95}])
    below = strategy._candidates(active, account(), {"ALFA": quote(94)}, NOW)
    assert below["orders"] == [] and not below["complete"]
    above = strategy._candidates(active, account(), {"ALFA": quote(100)}, NOW)
    assert above["orders"][0].side == "buy"


def test_protective_exits_use_a_marketable_limit_and_reprice_quickly(tmp_path, sent_orders):
    strategy = watching(tmp_path, {"ALFA": quote(94)})
    accept(strategy, [{"symbol": "ALFA", "weight": .05, "stop_loss": 95}], when=at(-30))
    strategy._watch(account(9530, {"ALFA": 5}, price=94), NOW)
    assert sent_orders[-1].limit_price <= 94 * (1 - .005) + .01  # Prioritizes getting out over price.
    canceled = []
    strategy.cancel_order = canceled.append
    key = strategy.journal.pending()[0]["id"]
    strategy.session_orders[key].status = "new"
    strategy._manage_open_orders(at(1.5))
    assert len(canceled) == 1  # Stops reprice after 60 s, not the 180 s used for entries.


def spend(journal, side, count, symbol="ALFA"):
    for i in range(count):
        key = journal.reserve(Intent(symbol, side, 1, number(100)), NOW, LIMITS)
        journal.update(key, f"{side}-{i}", True, NOW)


def test_purchases_cannot_starve_risk_reducing_sales_of_the_daily_budget(tmp_path):
    journal = Journal(tmp_path, "synthetic-budget")
    spend(journal, "buy", LIMITS.daily_orders)  # Purchases and their reprices used the day's budget.
    with pytest.raises(RiskError, match="daily submission limit"):
        journal.reserve(Intent("BETA", "buy", 1, number(100)), NOW, LIMITS)
    spend(journal, "sell", LIMITS.daily_orders)  # De-risking sales (e.g. the drawdown budget) still run.
    with pytest.raises(RiskError, match="daily submission limit"):
        journal.reserve(Intent("ALFA", "sell", 1, number(100)), NOW, LIMITS)
    with pytest.raises(RiskError, match="daily submission limit"):
        journal.reserve(Intent("BETA", "buy", 1, number(100)), NOW, LIMITS)  # Selling never frees buys.


def test_every_stop_exits_on_a_crash_day_after_the_allocation_budget_is_spent(tmp_path, sent_orders):
    quotes = {s: quote(94) for s in ("ALFA", "BETA", "GAMMA", "DELTA")}
    strategy = watching(tmp_path, quotes)
    accept(strategy, [{"symbol": s, "weight": .05, "stop_loss": 95} for s in quotes], when=at(-30))
    spend(strategy.journal, "buy", LIMITS.daily_orders)
    spend(strategy.journal, "sell", LIMITS.daily_orders)
    held = {s: 5 for s in quotes}
    for minute in range(len(quotes)):
        assert strategy._watch(account(9000, dict(held), price=94), at(minute))
        sold = sent_orders[-1]
        strategy.journal.update(strategy.journal.pending()[0]["id"], sold.identifier, True, at(minute))
        held.pop(sold.asset.symbol)
    assert held == {}
