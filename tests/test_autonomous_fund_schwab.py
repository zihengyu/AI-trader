"""Shared-account attribution and the read-only Schwab check (synthetic accounts, no network)."""
import json
from types import SimpleNamespace

import pytest

from lumibot.entities import Asset
from scripts.autonomous_fund.holdings import split_holdings
from scripts.autonomous_fund.risk import RiskError, number

UNIVERSE = {"AAPL", "MSFT", "SPY"}


def position(symbol, quantity, kind="stock", value=None):
    payload = {"marketValue": value} if value is not None else {}
    return SimpleNamespace(asset=Asset(symbol, asset_type=kind) if kind == "stock" else SimpleNamespace(symbol=symbol, asset_type=kind),
                           quantity=quantity, raw_broker_payload=payload)


def test_first_start_freezes_all_existing_holdings_and_marks_shared_symbols():
    positions = [position("AAPL", 100, value=23000), position("SWVXX", 5000, kind="mutual_fund", value=5000),
                 position("TSLA", 10, value=2500)]
    sleeve, external, value, shared = split_holdings(positions, UNIVERSE, allow_external=True, baseline=None,
                                                     initialized=False)
    assert sleeve == {} and external == {"AAPL": 100, "SWVXX": 5000, "TSLA": 10}
    assert value == number(30500) and shared == {"AAPL"}


def test_after_start_only_ai_purchases_form_the_sleeve():
    baseline = {"AAPL": "100", "SWVXX": "5000"}
    positions = [position("AAPL", 100, value=23500), position("SWVXX", 5000, kind="mutual_fund", value=5000),
                 position("MSFT", 3, value=1200)]
    sleeve, external, value, shared = split_holdings(positions, UNIVERSE, allow_external=True, baseline=baseline,
                                                     initialized=True)
    assert sleeve == {"MSFT": 3} and external == {"AAPL": 100, "SWVXX": 5000}
    assert value == number(28500) and shared == {"AAPL"}


def test_dedicated_account_still_refuses_unattributable_assets():
    with pytest.raises(RiskError, match="unsupported"):
        split_holdings([position("SWVXX", 1, kind="mutual_fund", value=1)], UNIVERSE)
    with pytest.raises(RiskError, match="cannot be valued"):
        split_holdings([position("TSLA", 1)], UNIVERSE, allow_external=True, price=lambda s: None)


def snapshot(tmp_path, positions, cash, equity, degraded=False):
    from scripts.autonomous_fund.strategy import Mandate
    from .test_autonomous_fund_quant import harness
    strategy = harness(tmp_path)
    strategy.universe = strategy._configured_universe = frozenset(UNIVERSE)
    strategy.mandate = Mandate.from_config({"capital_usd": "10000", "allow_external_holdings": True})
    strategy._quote_asset = Asset("USD", asset_type="forex")
    strategy.broker = SimpleNamespace(_last_positions_refresh_degraded=degraded)
    state = {"positions": positions, "cash": cash, "equity": equity, "orders": []}
    strategy.get_orders = lambda: state["orders"]
    strategy.get_positions = lambda: state["positions"]
    strategy.get_cash = lambda: state["cash"]
    strategy.get_portfolio_value = lambda: state["equity"]
    strategy.get_last_price = lambda asset: 100
    return strategy, state


def test_shared_symbols_leave_the_ai_universe_and_the_operators_orders_do_not_block(tmp_path):
    from lumibot.entities import Order
    strategy, state = snapshot(tmp_path, [position("AAPL", 100, value=23000)], 27000, 50000)
    sleeve = strategy._snapshot()
    assert (sleeve.cash, sleeve.equity, sleeve.positions) == (number(10000), number(10000), {})
    assert strategy.universe == {"MSFT", "SPY"}
    assert any(row["kind"] == "shared_symbols_excluded" for row in strategy.journal.all_events())
    state["orders"] = [Order("operator", Asset("AAPL"), 5, "sell", limit_price=240, status="new")]
    assert strategy._snapshot().open_orders is False  # Your own AAPL order is yours, not the AI's.
    state["orders"] = [Order("operator", Asset("MSFT"), 1, "buy", limit_price=400, status="new")]
    assert strategy._snapshot().open_orders is True  # An unknown order in an AI symbol blocks the AI.


def test_operator_trading_a_shared_symbol_pauses_the_ai_until_rebaselined(tmp_path):
    strategy, state = snapshot(tmp_path, [position("AAPL", 100, value=23000)], 27000, 50000)
    strategy._snapshot()
    state.update(positions=[position("AAPL", 90, value=20700)], cash=29300, equity=50000)
    with pytest.raises(RiskError, match="External holdings changed"):
        strategy._snapshot()


def test_a_partial_broker_position_read_is_never_treated_as_no_holdings(tmp_path):
    strategy, _ = snapshot(tmp_path, [], 50000, 50000, degraded=True)
    with pytest.raises(RiskError, match="partially refreshed"):
        strategy._snapshot()


# ----- Read-only Schwab check -------------------------------------------------------------

def config(**mandate):
    return {"universe": {s: "stock" for s in UNIVERSE},
            "mandate": {"capital_usd": "10000", "allow_external_holdings": True, **mandate}}


def account(kind="MARGIN", cash=27000, settled=None, equity=50000):
    balances = {"cashBalance": cash, "liquidationValue": equity}
    if settled is not None:
        balances["cashAvailableForTrading"] = settled
    return {"securitiesAccount": {"type": kind, "currentBalances": balances}}


def test_summary_previews_the_sleeve_reserve_shared_symbols_and_renewal(tmp_path):
    from scripts.autonomous_fund.schwab import summarize
    positions = [{"symbol": "AAPL", "asset_type": "EQUITY", "quantity": "100", "market_value": "23000"}]
    result = summarize(account(), positions, [], config(), {"SCHWAB_ACCOUNT_NUMBER": "x"})
    assert result["verdict"] == "ready_for_read_only_validation"
    assert result["reserved_cash"] == "17000" and result["ai_sleeve_start"] == "10000"
    assert result["excluded_shared_symbols"] == ["AAPL"]
    assert any("SCHWAB_APP_SECRET" in n for n in result["notices"])
    assert any("api.botspot.trade" in n for n in result["notices"])
    assert any("Margin account" in n for n in result["notices"])


@pytest.mark.parametrize("payload, extra, message", [
    (account(cash=5000), {}, "below FUND_CAPITAL_USD"),
    (account(cash=-100), {}, "margin debt"),
    (account(kind="CASH", cash=12000, settled=6000), {}, "Settled cash"),
])
def test_summary_flags_accounts_the_mandate_cannot_use(payload, extra, message):
    from scripts.autonomous_fund.schwab import summarize
    result = summarize(payload, [], [], config(**extra), {})
    assert result["verdict"] == "needs_attention" and any(message in issue for issue in result["issues"])


def test_existing_holdings_without_external_permission_are_flagged():
    from scripts.autonomous_fund.schwab import summarize
    positions = [{"symbol": "TSLA", "asset_type": "EQUITY", "quantity": "1", "market_value": "250"}]
    result = summarize(account(), positions, [], config(allow_external_holdings=False), {})
    assert any("FUND_ALLOW_EXTERNAL_HOLDINGS" in issue for issue in result["issues"])


class FakeSchwab:
    ACTIVE_SCHWAB_ORDER_STATUSES = {"WORKING"}

    def __init__(self, ready=True):
        self._broker_fully_ready, self.schwab_authorization_error = ready, False
        self.hash_value = "synthetic-hash"
        payload = {**account(), "securitiesAccount": {**account()["securitiesAccount"], "positions": [
            {"instrument": {"symbol": "AAPL", "assetType": "EQUITY"}, "longQuantity": 100, "marketValue": 23000}]}}
        self.client = SimpleNamespace(Account=SimpleNamespace(Fields=SimpleNamespace(POSITIONS="positions")),
                                      get_account=lambda h, fields: SimpleNamespace(status_code=200, json=lambda: payload))
        self.sent = []

    def submit_order(self, order):
        self.sent.append(order)

    def _pull_broker_all_orders(self):
        return [{"status": "WORKING", "orderLegCollection": [{"instrument": {"symbol": "TSLA"}, "instruction": "BUY",
                                                              "quantity": 1}]}, {"status": "FILLED"}]


def test_schwab_check_reads_the_account_writes_a_report_and_cannot_place_orders(tmp_path):
    from scripts.autonomous_fund.schwab import schwab_check
    broker = FakeSchwab()
    result = schwab_check(SimpleNamespace(run_dir=tmp_path), config(), broker_factory=lambda: broker,
                          environ={"SCHWAB_ACCOUNT_NUMBER": "x"})
    assert result["open_orders"] == [{"symbol": "TSLA", "status": "WORKING", "instruction": "BUY", "quantity": 1}]
    assert json.loads((tmp_path / "schwab_check.json").read_text(encoding="utf-8"))["excluded_shared_symbols"] == ["AAPL"]
    with pytest.raises(RiskError, match="read-only"):
        broker.submit_order("anything")
    assert broker.sent == []


def test_schwab_check_requires_an_account_and_a_completed_login(tmp_path):
    from scripts.autonomous_fund.schwab import schwab_check
    with pytest.raises(RiskError, match="SCHWAB_ACCOUNT_NUMBER"):
        schwab_check(SimpleNamespace(run_dir=tmp_path), config(), broker_factory=FakeSchwab, environ={})
    with pytest.raises(RiskError, match="login is not complete"):
        schwab_check(SimpleNamespace(run_dir=tmp_path), config(), broker_factory=lambda: FakeSchwab(ready=False),
                     environ={"SCHWAB_ACCOUNT_NUMBER": "x"}, wait_seconds=0)
