"""Application risk contracts and native-engine evidence; no network/inference."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import (
    Account, Intent, Limits, Price, RiskError, number, plan, validate_decision, validate_evidence,
)


NOW = datetime(2025, 1, 6, 15, tzinfo=timezone.utc)
UNIVERSE = {"ALFA", "BETA", "SPY", "QQQ"}
LIMITS = Limits()


def quote(price=100, age=0):
    return Price(number(price), number(price), NOW - timedelta(seconds=age), number(50_000_000))


def account(cash=10000, equity=10000, positions=None, **kwargs):
    return Account(number(cash), number(equity), positions or {}, NOW, **kwargs)


def decision(targets=None):
    return {"as_of": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "next_review_at": (NOW + timedelta(minutes=30)).isoformat(), "rationale": "test",
            "targets": [{"symbol": symbol, "weight": weight} for symbol, weight in (targets or [])]}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", "Infinity", True, None])
def test_nonfinite_and_untyped_financial_input_is_rejected(value):
    with pytest.raises(RiskError):
        number(value)


@pytest.mark.parametrize("targets", [
    [("ALFA", .11)], [("ALFA", -.1)], [("ALFA", .1), ("ALFA", .1)],
    [("OUTSIDE", .1)], [("ALFA", float("nan"))],
])
def test_model_cannot_bypass_symbol_or_weight_policy(targets):
    with pytest.raises(RiskError):
        validate_decision(decision(targets), NOW, UNIVERSE, LIMITS)


def test_total_exposure_is_enforced_independent_of_prompt():
    universe = {f"STOCK{i}" for i in range(8)}
    with pytest.raises(RiskError, match="exposure"):
        validate_decision(decision([(s, .1) for s in universe]), NOW, universe, LIMITS)


def test_stale_future_expired_and_naive_decisions_are_rejected():
    for field, value in [("as_of", NOW + timedelta(minutes=1)), ("as_of", NOW - timedelta(minutes=6)),
                         ("expires_at", NOW), ("next_review_at", NOW + timedelta(days=2)),
                         ("as_of", NOW.replace(tzinfo=None))]:
        body = decision()
        body[field] = value.isoformat()
        with pytest.raises(RiskError):
            validate_decision(body, NOW, UNIVERSE, LIMITS)


def test_cash_and_position_limit_include_limit_price_and_fee_reserve():
    intent = plan({"ALFA": number(.1)}, account(), {"ALFA": quote()}, UNIVERSE, LIMITS, NOW)[0]
    assert intent.quantity == 9
    assert intent.quantity * intent.limit <= 1000
    low_cash = plan({"ALFA": number(.1)}, account(500, 500, {}), {"ALFA": quote(10)}, UNIVERSE, LIMITS, NOW)[0]
    assert low_cash.quantity * low_cash.limit * (1 + LIMITS.fee_buffer) <= 500


def test_new_purchase_cannot_reuse_existing_exposure():
    holdings = {f"STOCK{i}": 10 for i in range(7)}
    universe = set(holdings) | {"BETA"}
    targets = {s: number(.1) for s in universe}
    prices = {s: quote() for s in universe}
    # Existing positions are at the cap. Execution must offer reductions first,
    # never allow another buy against already occupied total exposure.
    orders = plan(targets, account(3000, 10000, holdings), prices, universe, LIMITS, NOW)
    assert all(o.side == "sell" for o in orders)


def test_drawdown_stops_new_risk_but_allows_reductions():
    assert not plan({"ALFA": number(.1)}, account(), {"ALFA": quote()}, UNIVERSE, LIMITS, NOW, paused=True)
    order = plan({}, account(9000, 10000, {"ALFA": 10}), {"ALFA": quote()}, UNIVERSE, LIMITS, NOW, paused=True)[0]
    assert order.side == "sell" and order.quantity == 10


@pytest.mark.parametrize("bad_account", [
    account(open_orders=True), account(-1), account(10000, 0), account(10000, 10000, {"ALFA": -1}),
    account(9000, 10000, {"OTHER": 10}), replace(account(), as_of=NOW - timedelta(minutes=2)),
    account(9000, 10000, {}), account(9950, 10000, {"ALFA": .5}),
])
def test_unknown_orders_stale_account_margin_short_and_mismatch_fail_closed(bad_account):
    with pytest.raises(RiskError):
        plan({"ALFA": number(.1)}, bad_account, {"ALFA": quote()}, UNIVERSE, LIMITS, NOW)


@pytest.mark.parametrize("bad_price", [quote(age=61), quote(age=-1), replace(quote(), bid=number(0)),
                                     replace(quote(), dollar_volume=number(100)),
                                     replace(quote(), ask=number(110))])
def test_stale_future_illiquid_and_wide_markets_fail_closed(bad_price):
    with pytest.raises(RiskError):
        plan({"ALFA": number(.1)}, account(), {"ALFA": bad_price}, UNIVERSE, LIMITS, NOW)


def test_future_publication_is_blocked_only_in_backtests_and_estimates_are_disclosed():
    evidence = [{"available_at": (NOW + timedelta(days=1)).isoformat(), "observed_at": NOW.isoformat(),
                 "estimated": False, "estimate_reason": ""}]
    with pytest.raises(RiskError):
        validate_evidence(evidence, NOW, True)
    validate_evidence(evidence, NOW, False)
    evidence[0].update(available_at=(NOW - timedelta(days=1)).isoformat(), estimated=True)
    with pytest.raises(RiskError):
        validate_evidence(evidence, NOW, True)
    evidence[0]["estimate_reason"] = "Context identifies the preceding public reporting period; exact date unknown."
    validate_evidence(evidence, NOW, True)


def test_restart_preserves_reservation_quota_and_drawdown_latch(tmp_path):
    journal = Journal(tmp_path, "paper-account-A")
    intent = Intent("ALFA", "buy", 1, number(100))
    key = journal.reserve(intent, NOW, LIMITS)
    restored = Journal(tmp_path, "paper-account-A")
    with pytest.raises(RiskError):
        restored.reserve(intent, NOW, LIMITS)
    restored.update(key, "broker-id", True, NOW)
    for _ in range(4):
        key = restored.reserve(intent, NOW, LIMITS)
        restored.update(key, "broker-id", True, NOW)
    with pytest.raises(RiskError, match="daily"):
        restored.reserve(intent, NOW, LIMITS)
    assert not restored.mark_equity(NOW, 10000, LIMITS)
    assert restored.mark_equity(NOW, 9200, LIMITS)  # inclusive 8% boundary
    assert Journal(tmp_path, "paper-account-A").mark_equity(NOW, 11000, LIMITS)
    with pytest.raises(RiskError, match="another"):
        Journal(tmp_path, "paper-account-B")


def test_concurrent_reservations_cannot_exceed_one_unresolved_order(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    journals = [Journal(tmp_path, "same") for _ in range(8)]
    def reserve(journal):
        try:
            return journal.reserve(Intent("ALFA", "buy", 1, number(100)), NOW, LIMITS)
        except RiskError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(x is not None for x in pool.map(reserve, journals)) == 1


def test_paper_runner_rejects_live_configuration_before_any_network(monkeypatch):
    from scripts.autonomous_fund.run import main, paper_config
    monkeypatch.setenv("ALPACA_API_KEY", "synthetic-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "synthetic-secret")
    monkeypatch.setenv("ALPACA_IS_PAPER", "false")
    with pytest.raises(RiskError, match="live"):
        paper_config()
    with pytest.raises(SystemExit):
        main(["live"])


def harness(tmp_path, mode="mock"):
    from scripts.autonomous_fund.strategy import AutonomousFund
    strategy = object.__new__(AutonomousFund)
    strategy.mode = mode
    strategy._name = "test"
    strategy.journal = Journal(tmp_path, "test")
    strategy.halt_path = tmp_path / "STOP"
    strategy.limits = LIMITS
    strategy.session_orders = {}
    strategy.get_datetime = lambda: NOW
    strategy.notify = lambda *args: None
    return strategy


def test_timeout_then_restart_never_replays_submission(tmp_path, monkeypatch):
    from lumibot.entities import Order
    from lumibot.strategies import Strategy
    strategy = harness(tmp_path)
    strategy.create_order = lambda *args, **kw: Order("test", "ALFA", 1, "buy", limit_price=100)
    calls = []
    def timeout(*args, **kwargs):
        calls.append("attempted")
        raise TimeoutError("acknowledgement lost")
    monkeypatch.setattr(Strategy, "submit_order", timeout)
    with pytest.raises(TimeoutError):
        strategy._execute(Intent("ALFA", "buy", 1, number(100)), NOW)
    assert len(strategy.journal.pending()) == 1
    restored = harness(tmp_path)
    with pytest.raises(RiskError, match="identity"):
        restored._reconcile()
    assert calls == ["attempted"]


def test_manual_stop_blocks_submission_and_attempts_known_order_cancellation(tmp_path):
    from lumibot.entities import Order
    strategy = harness(tmp_path)
    strategy.halt_path.touch()
    with pytest.raises(RiskError, match="stop"):
        strategy._execute(Intent("ALFA", "buy", 1, number(100)), NOW)
    key = strategy.journal.reserve(Intent("ALFA", "buy", 1, number(100)), NOW, LIMITS)
    order = Order("test", "ALFA", 1, "buy", limit_price=100, status="cancelling")
    strategy.session_orders[key] = order
    canceled = []
    strategy.cancel_order = canceled.append
    strategy.on_trading_iteration()
    assert canceled == [order]  # CANCELLING must still send the cancel request.
    assert strategy.journal.pending()


def test_stop_arriving_during_preparation_prevents_broker_call(tmp_path, monkeypatch):
    from lumibot.entities import Order
    from lumibot.strategies import Strategy
    strategy = harness(tmp_path)
    def prepare(*args, **kwargs):
        strategy.halt_path.touch()
        return Order("test", "ALFA", 1, "buy", limit_price=100)
    strategy.create_order = prepare
    calls = []
    monkeypatch.setattr(Strategy, "submit_order", lambda *args, **kwargs: calls.append("sent"))
    with pytest.raises(RiskError, match="before submission"):
        strategy._execute(Intent("ALFA", "buy", 1, number(100)), NOW)
    assert not calls
    assert not strategy.journal.pending()


def test_restart_reconciles_exact_fill_without_duplicate_report_rows(tmp_path):
    from types import SimpleNamespace
    from lumibot.entities import Order
    from scripts.autonomous_fund.report import report
    strategy = harness(tmp_path, "paper")
    key = strategy.journal.reserve(Intent("ALFA", "buy", 1, number(100)), NOW, LIMITS)
    strategy.journal.update(key, "order-123", False, NOW)
    order = Order("test", "ALFA", 1, "buy", limit_price=100, identifier="order-123",
                  status="fill", avg_fill_price=99.95)
    strategy.broker = SimpleNamespace(_pull_order=lambda *args: order)
    strategy.on_filled_order(None, order, 99.95, 1, 1)
    strategy._reconcile()
    strategy._reconcile()
    assert not strategy.journal.pending()
    result = report(tmp_path)
    assert result["fills_observed"] == 1
    assert result["observed_fees"] is None


def test_wrong_order_identity_keeps_recovery_locked(tmp_path):
    from types import SimpleNamespace
    from lumibot.entities import Order
    strategy = harness(tmp_path, "paper")
    key = strategy.journal.reserve(Intent("ALFA", "buy", 1, number(100)), NOW, LIMITS)
    strategy.journal.update(key, "expected-id", False, NOW)
    wrong = Order("test", "BETA", 1, "buy", limit_price=100, identifier="different-id", status="fill")
    strategy.broker = SimpleNamespace(_pull_order=lambda *args: wrong)
    with pytest.raises(RiskError, match="exact"):
        strategy._reconcile()
    assert strategy.journal.pending()


def test_sdk_actual_destination_must_be_paper_even_when_label_says_paper():
    from alpaca.trading.client import TradingClient
    from lumibot.brokers import Alpaca
    from scripts.autonomous_fund.strategy import assert_paper
    broker = Alpaca({"PAPER": True, "API_KEY": "synthetic", "API_SECRET": "synthetic"},
                    trading_client=TradingClient("synthetic", "synthetic", paper=True),
                    connect_stream=False, start_orders_thread=False)
    assert_paper(broker)
    broker.api = TradingClient("synthetic", "synthetic", paper=False)
    with pytest.raises(RiskError, match="endpoint"):
        assert_paper(broker)


def test_first_paper_start_requires_requested_capital_and_no_foreign_holdings(tmp_path):
    journal = Journal(tmp_path, "test")
    with pytest.raises(RiskError, match="10,000"):
        journal.confirm_initial_paper_account(account(100000, 100000))
    with pytest.raises(RiskError):
        journal.confirm_initial_paper_account(account(9900, 10000, {"ALFA": 1}))
    journal.confirm_initial_paper_account(account())
    journal.confirm_initial_paper_account(account(9000, 10010, {"ALFA": 10}))  # Normal funded-account restart.


def test_current_and_future_daily_bars_do_not_enter_liquidity_estimate(tmp_path):
    import pandas as pd
    from types import SimpleNamespace
    strategy = harness(tmp_path)
    dates = pd.bdate_range(end=NOW.date() + timedelta(days=1), periods=25, tz="UTC")
    frame = pd.DataFrame({"close": 100, "volume": 500000}, index=dates)
    frame.loc[frame.index.date >= NOW.date(), "close"] = 1_000_000
    strategy.get_historical_prices = lambda *args, **kwargs: SimpleNamespace(df=frame)
    strategy.get_last_price = lambda *args: 100
    price = strategy._price("ALFA")
    assert price.dollar_volume == 50_000_000


def test_usage_cost_and_unknown_fees_are_not_fabricated(tmp_path):
    from scripts.autonomous_fund.report import report
    journal = Journal(tmp_path, "test")
    journal.event(NOW, "agent", {"model": "synthetic", "cache_hit": False,
                                "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                          "completion_tokens_details": {"reasoning_tokens": 40}}})
    result = report(tmp_path, {"input": 1, "output": 2})
    assert result["model_usage"]["output_tokens"] == 50
    assert result["model_cost_estimate_usd"] == pytest.approx(.0002)
    journal.event(NOW, "agent", {"usage": None})
    assert report(tmp_path, {"input": 1, "output": 2})["model_cost_estimate_usd"] is None


def test_research_sql_cannot_access_risk_files_or_unlock_engine(tmp_path):
    import duckdb
    import pandas as pd
    from types import SimpleNamespace
    from scripts.autonomous_fund.boundary import isolate_research
    connection = duckdb.connect()
    strategy = SimpleNamespace(agents=SimpleNamespace(duckdb=SimpleNamespace(connection=connection)))
    isolate_research(strategy, tmp_path)
    connection.register("research", pd.DataFrame({"price": [100, 110]}))
    assert connection.execute("SELECT avg(price) FROM research").fetchone()[0] == 105
    target = str(tmp_path / "risk-config.csv").replace("\\", "/")
    for sql in [f"SELECT 1; COPY (SELECT 1) TO '{target}'", "SELECT 1; SET enable_external_access=true",
                f"SELECT * FROM read_text('{target}')"]:
        with pytest.raises(duckdb.Error):
            connection.execute(sql)
    assert not (tmp_path / "risk-config.csv").exists()
    connection.close()


@pytest.mark.parametrize("url", ["file:///risk-config.json", "javascript:alert(1)", "data:text/html,local"])
def test_browser_cannot_navigate_to_local_files_or_script_urls(url):
    from scripts.autonomous_fund.boundary import web_url
    with pytest.raises(RiskError):
        web_url(url)
    web_url("https://example.com/research/report.pdf")


def test_csv_history_does_not_invent_timezone_for_naive_input(tmp_path):
    from scripts.autonomous_fund.run import csv_data
    (tmp_path / "ALFA.csv").write_text(
        "timestamp,open,high,low,close,volume\n2025-01-06,100,101,99,100,500000\n", encoding="utf-8")
    with pytest.raises(RiskError, match="explicit timezone"):
        csv_data(tmp_path)


def test_real_native_backtest_runs_agents_fills_and_reports(tmp_path, monkeypatch):
    from scripts.autonomous_fund.run import main
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    main(["mock", "--run-dir", str(tmp_path / "run"), "--start", "2025-01-06", "--end", "2025-01-14"])
    result = json.loads((tmp_path / "run" / "report.json").read_text())
    assert result["mode"] == "mock" and result["scripted_model"] is True
    assert result["fills_observed"] >= 1
    assert result["observed_fees"] > 0
    assert result["benchmark_price_returns"]["SPY"] is not None
    assert result["benchmark_price_returns"]["QQQ"] is not None
    events = [json.loads(line) for line in (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    assert {row["body"]["name"] for row in events if row["kind"] == "agent"} == {"researcher", "manager"}
    assert any(row["kind"] == "decision" for row in events)
    assert (tmp_path / "run" / "native_trade_events.csv").exists()
