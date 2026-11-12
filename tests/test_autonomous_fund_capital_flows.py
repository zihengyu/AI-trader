"""Operator subscriptions and redemptions (capital.py): unit NAV, risk state, liquidity waterfall, reports.

Synthetic accounts, prices and journals only: no network, broker or inference.
"""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from lumibot.entities import Asset, Order
from scripts.autonomous_fund import capital
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.risk import Account, Intent, RiskError, number
from .test_autonomous_fund_quant import LIMITS, NOW, account, default_frames, harness, quote


def mandate_journal(tmp_path, cash=50000, capital_usd="10000"):
    journal = Journal(tmp_path, "synthetic-capital-account")
    journal.apply_mandate(NOW, capital_usd, number(cash), number(cash), {}, False, {}, True)
    return journal


# ----- The failure an operator hits today -------------------------------------------------

def test_lowering_the_configured_capital_never_latches_the_drawdown_lock_or_the_daily_pause(tmp_path):
    journal = mandate_journal(tmp_path)
    assert journal.mark_equity(NOW, number(10000), LIMITS) is False
    journal.observe_session(NOW, account(cash=10000), LIMITS.drawdown)
    # The operator takes 5,000 out by lowering FUND_CAPITAL_USD. Before: the reserve moved at once and the
    # sleeve "lost" half its value, which latched the 8% drawdown lock and the daily loss pause.
    reserve = journal.apply_mandate(NOW, "5000", number(50000), number(50000), {}, False, {}, True)
    sleeve = number(50000) - reserve
    assert journal.mark_equity(NOW, sleeve, LIMITS) is False
    assert reserve == number(40000)  # Queued: the fund applies it at its next valuation.
    [request] = capital.pending(journal)
    assert (request["kind"], request["amount"], request["source"]) == ("redemption", "5000.00", "configuration")
    # The same configured value again queues nothing more.
    journal.apply_mandate(NOW + timedelta(minutes=1), "5000", number(50000), number(50000), {}, False, {}, True)
    assert len(capital.pending(journal)) == 1
    later = NOW + timedelta(minutes=2)
    flow = capital.apply(journal, later, request["id"], equity=number(10000), cash=number(10000), mode="paper")
    assert number(flow["reserved_cash_after"]) == 45000 and number(flow["capital_basis_after"]) == 5000
    assert journal.reserve_cash() == number(45000)
    after = NOW + timedelta(minutes=3)
    assert journal.mark_equity(after, number(5000), LIMITS) is False
    session = journal.observe_session(after, account(cash=5000), LIMITS.drawdown)
    assert session["day_loss"] == 0 and session["drawdown"] == 0 and not session["loss_paused"]
    assert session["cash_spent"] == 0  # The cash that left was not a purchase.
    # A real loss after the flow is still measured against the scaled peak.
    assert journal.mark_equity(after, number(4500), LIMITS) is True


# ----- Units and NAV ----------------------------------------------------------------------------

def test_flows_are_priced_at_the_nav_per_unit_and_leave_it_unchanged(tmp_path):
    journal = mandate_journal(tmp_path)
    first = capital.request(journal, NOW, "subscription", "10000")
    flow = capital.apply(journal, NOW, first["id"], equity=number(10200), cash=number(10200), mode="paper")
    # Before the first flow one unit per dollar of capital: NAV 1.02 after a 2% gain.
    assert flow["units_before"] == "10000" and flow["nav_per_unit"] == "1.020000"
    units = number(flow["units_after"])
    assert abs(units - (10000 + number(10000) / number("1.02"))) < number("0.00000001")
    assert abs(number(flow["equity_after"]) / units - number("1.02")) < number("0.0000001")
    assert journal.reserve_cash() == number(30000)
    second = capital.request(journal, NOW, "redemption", "5000")
    flow = capital.apply(journal, NOW, second["id"], equity=number(20604), cash=number(9000), mode="paper")
    nav = number(20604) / units
    assert abs(number(flow["equity_after"]) / number(flow["units_after"]) - nav) < number("0.0000001")
    assert capital.pending(journal) == []
    state = capital.status(journal)
    assert state["units"] == flow["units_after"] and number(state["capital_basis"]) == 15000


@pytest.mark.parametrize("value, message", [("0", "above 0"), ("-5", "above 0"), ("1.234", "two decimals"),
                                            ("abc", "number"), ("20000000", "at most")])
def test_amounts_are_positive_dollars_and_cents(tmp_path, value, message):
    journal = mandate_journal(tmp_path)
    with pytest.raises(RiskError, match=message):
        capital.request(journal, NOW, "subscription", value)


def test_requests_need_a_capital_mandate_and_can_be_canceled(tmp_path):
    with pytest.raises(RiskError, match="No capital mandate"):
        capital.request(Journal(tmp_path / "none", "synthetic-no-mandate"), NOW, "subscription", "100")
    journal = mandate_journal(tmp_path)
    request = capital.request(journal, NOW, "redemption", "100")
    assert capital.cancel(journal, NOW, request["id"])["id"] == request["id"]
    assert capital.pending(journal) == []
    with pytest.raises(RiskError, match="No waiting"):
        capital.cancel(journal, NOW, request["id"])
    kinds = [row["kind"] for row in journal.all_events()]
    assert kinds.count("capital_request") == 1 and kinds.count("capital_request_cancelled") == 1


def test_paper_subscription_needs_reserved_cash_and_changes_nothing_when_refused(tmp_path):
    journal = mandate_journal(tmp_path, cash=12000)  # Reserve 2,000.
    request = capital.request(journal, NOW, "subscription", "5000")
    with pytest.raises(RiskError, match="deposit"):
        capital.apply(journal, NOW, request["id"], equity=number(10000), cash=number(10000), mode="paper")
    assert journal.reserve_cash() == number(2000) and len(capital.pending(journal)) == 1
    assert not [row for row in journal.all_events() if row["kind"] == "capital_flow"]


def test_configuration_subscription_resumes_after_reserve_is_recorded(tmp_path):
    strategy = running(tmp_path, cash=12000)
    journal = strategy.journal
    assert journal.apply_mandate(NOW, "15000", 12000, 12000, {}, False, {}, True) == 2000
    [queued] = capital.pending(journal)
    assert not strategy._capital_step(sleeve(10000), NOW)
    assert not strategy._capital_step(sleeve(10000), NOW)
    [waiting] = capital.pending(journal)
    assert (waiting["id"], waiting["state"]) == (queued["id"], "waiting_reserve")
    assert [kind for kind, _ in strategy.notices] == ["subscription_waiting"]
    capital.deposit(journal, NOW, "3000", subscribe=False)
    assert strategy._capital_step(sleeve(10000), NOW)
    assert capital.status(journal)["capital_basis"] == "15000.00"
    assert journal.reserve_cash() == 0 and not capital.pending(journal)


def test_revised_config_target_can_replace_an_unfunded_increase(tmp_path):
    strategy = running(tmp_path, cash=12000)
    journal = strategy.journal
    journal.apply_mandate(NOW, "15000", 12000, 12000, {}, False, {}, True)
    assert not strategy._capital_step(sleeve(10000), NOW)
    [old] = capital.pending(journal)
    journal.apply_mandate(NOW, "11000", 12000, 12000, {}, False, {}, True)
    [new] = capital.pending(journal)
    assert old["id"] != new["id"] and new["amount"] == "1000.00"
    assert strategy._capital_step(sleeve(10000), NOW)
    assert capital.status(journal)["capital_basis"] == "11000.00"


@pytest.mark.parametrize("mode", ["local", "paper"])
def test_existing_sleeve_cannot_absorb_its_reserve_in_whole_account_mode(tmp_path, mode):
    from scripts.autonomous_fund.strategy import open_journal

    journal = open_journal(tmp_path, mode, "synthetic-bound-account", {})
    journal.apply_mandate(NOW, "10000", 50000, 50000, {}, False, {}, True)
    with pytest.raises(RiskError, match="numeric capital mandate"):
        open_journal(tmp_path, mode, "synthetic-bound-account", {})
    reopened = open_journal(tmp_path, mode, "synthetic-bound-account", {"mandate": {"capital_usd": "10000"}})
    assert reopened.reserve_cash() == 40000


def test_operation_id_retries_the_same_request_after_it_was_applied(tmp_path):
    journal = mandate_journal(tmp_path)
    first = capital.request(journal, NOW, "subscription", "100", operation_id="add-001")
    capital.apply(journal, NOW, first["id"], equity=10000, cash=10000, mode="paper")
    assert capital.request(journal, NOW, "subscription", "100.00", operation_id="add-001") == first
    assert not capital.pending(journal)
    with pytest.raises(RiskError, match="different terms"):
        capital.request(journal, NOW, "redemption", "100", operation_id="add-001")


def test_flow_and_outbox_confirmation_roll_back_together(tmp_path, monkeypatch):
    journal = mandate_journal(tmp_path)
    queued = capital.request(journal, NOW, "subscription", "100")
    emit = capital._event

    def failed_outbox(db, at, kind, body):
        if kind == "notification":
            raise RuntimeError("test outbox write failure")
        emit(db, at, kind, body)

    with monkeypatch.context() as patch:
        patch.setattr(capital, "_event", failed_outbox)
        with pytest.raises(RuntimeError, match="outbox write failure"):
            capital.apply(journal, NOW, queued["id"], equity=10000, cash=10000, mode="paper")
    assert capital.pending(journal) == [queued] and journal.reserve_cash() == 40000
    assert not [row for row in journal.all_events() if row["kind"] == "capital_flow"]
    capital.apply(journal, NOW, queued["id"], equity=10000, cash=10000, mode="paper")
    assert sum(row["kind"] == "notification" for row in journal.all_events()) == 1


def test_transfer_retry_keys_protect_the_reserve_and_default_subscription(tmp_path):
    journal = mandate_journal(tmp_path)
    reserve, queued = capital.deposit(journal, NOW, "200", operation_id="transfer-001")
    assert capital.deposit(journal, NOW, "200.0", operation_id="transfer-001") == (reserve, queued)
    assert len(capital.pending(journal)) == 1 and journal.reserve_cash() == 40200
    with pytest.raises(RiskError, match="different terms"):
        capital.deposit(journal, NOW, "200", subscribe=False, operation_id="transfer-001")
    assert capital.withdraw(journal, NOW, "100", operation_id="transfer-002") == 40100
    assert capital.withdraw(journal, NOW, "100", operation_id="transfer-002") == 40100
    assert journal.reserve_cash() == 40100
    assert sum(row["kind"] == "operator_reserve_adjusted" for row in journal.all_events()) == 2


def test_stock_sale_keeps_the_stock_weight_within_the_drift_and_the_hard_limit():
    # 60% invested, 5,000 of 10,000 redeemed: paying all from cash would leave 6,000/5,000 = 120% invested.
    assert capital.stock_sale_needed(10000, 6000, 5000, "0.6", "0.7") == number(6000) - number("0.65") * 5000
    # A small redemption from a 40% invested fund is paid from cash (44.4% invested afterwards).
    assert capital.stock_sale_needed(10000, 4000, 1000, "0.4", "0.7") == 0
    # Never above the hard exposure limit, even when the fund started near it.
    assert capital.stock_sale_needed(10000, 6900, 1000, "0.69", "0.7") == number(6900) - number("0.7") * 9000


def test_slice_plan_sells_every_holding_in_proportion_in_whole_shares():
    plan = capital.slice_plan({"ALFA": 30, "BETA": 10, "GAMMA": 3}, {"ALFA": 100, "BETA": 300, "GAMMA": 50}, 3000)
    raised = plan.get("ALFA", 0) * 100 + plan.get("BETA", 0) * 300 + plan.get("GAMMA", 0) * 50
    assert raised >= 3000
    total = 30 * 100 + 10 * 300 + 3 * 50
    for symbol, held, price in (("ALFA", 30, 100), ("BETA", 10, 300), ("GAMMA", 3, 50)):
        assert abs(plan.get(symbol, 0) - held * 3000 / total) <= 1  # Within one share of its pro-rata part.
    assert capital.slice_plan({"ALFA": 5}, {"ALFA": 100}, 10_000) == {"ALFA": 5}  # Never more than held.
    assert capital.slice_plan({}, {}, 100) == {}


# ----- The running fund ---------------------------------------------------------------------------

def running(tmp_path, cash=50000, sweep=None):
    from scripts.autonomous_fund.strategy import Mandate
    strategy = harness(tmp_path, default_frames(), mode="paper")
    # The default harness exercises the aggregate limit with concentrated
    # holdings. Tests of the single-security cap set their own lower limit.
    strategy.limits = replace(strategy.limits, max_position=strategy.limits.max_exposure)
    strategy.mandate = Mandate.from_config({"capital_usd": "10000"})
    strategy.journal.apply_mandate(NOW, "10000", number(cash), number(cash), {}, False, {}, True)
    strategy.broker = SimpleNamespace(is_market_open=lambda: True)
    strategy.next_review = None
    strategy.cash_sweep = sweep
    strategy._price = lambda symbol, record=True: quote(100)
    strategy.sent = []
    def submit(intent, now, decision_id, **kw):
        if kw.get("guard"):
            with strategy.journal.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                kw["guard"](db)
        strategy.sent.append((intent, decision_id, kw))
    strategy._submit = submit
    return strategy


def sleeve(cash, positions=None, sweep=0):
    positions = positions or {}
    value = sum(q * 100 for q in positions.values())
    return Account(number(cash + sweep * 100), number(cash + sweep * 100 + value), positions, NOW,
                   cash_equivalents={"SGOV": sweep} if sweep else {}, cash_equivalent_value=number(sweep * 100),
                   position_prices={symbol: "100" for symbol in positions})


def test_subscription_is_applied_at_the_next_valuation_and_asks_the_manager_to_replan(tmp_path):
    strategy = running(tmp_path)
    capital.request(strategy.journal, NOW, "subscription", "10000")
    assert strategy._capital_step(sleeve(4000, {"ALFA": 60}), NOW) is True
    assert strategy.journal.reserve_cash() == number(30000)
    # The confirmation is committed to the durable outbox with the units.
    [notice] = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "notification"]
    assert notice["kind"] == "capital_flow" and number(notice["details"]["equity_after"]) == 20000
    assert strategy.next_review == NOW and strategy._triggers[-1]["reason"] == "capital_flow"
    assert strategy._capital_step(sleeve(14000, {"ALFA": 60}), NOW) is False  # Nothing waiting.


def test_closed_market_or_an_open_order_defers_every_request(tmp_path):
    strategy = running(tmp_path)
    capital.request(strategy.journal, NOW, "subscription", "100")
    strategy.broker = SimpleNamespace(is_market_open=lambda: False)
    assert strategy._capital_step(sleeve(10000), NOW) is False
    strategy.broker = SimpleNamespace(is_market_open=lambda: True)
    busy = Account(number(10000), number(10000), {}, NOW, open_orders=True)
    assert strategy._capital_step(busy, NOW) is False
    assert len(capital.pending(strategy.journal)) == 1


@pytest.mark.parametrize("delay", [timedelta(seconds=61), timedelta(days=1)])
def test_stale_and_prior_session_marks_cannot_price_a_flow(tmp_path, delay):
    strategy = running(tmp_path)
    journal = strategy.journal
    queued = capital.request(journal, NOW, "subscription", "2000")
    strategy.get_datetime = lambda: NOW + delay
    assert strategy._capital_step(sleeve(10000), NOW + delay)
    assert journal.reserve_cash() == 40000 and capital.pending(journal) == [queued]
    with pytest.raises(capital.ValuationChanged, match="stale"):
        capital.apply(journal, NOW + delay, queued["id"], equity=10000, cash=10000,
                      mode="paper", observed_at=NOW, max_age_seconds=60)


def test_subscription_after_an_overnight_gap_preserves_that_days_loss(tmp_path):
    journal = mandate_journal(tmp_path)
    previous = NOW - timedelta(days=1)
    journal.mark_equity(previous, 10000, LIMITS)
    journal.observe_session(previous, replace(sleeve(10000), as_of=previous), number("0.08"))
    journal.mark_equity(NOW, 9000, LIMITS)
    before = journal.observe_session(NOW, sleeve(9000), number("0.08"))
    queued = capital.request(journal, NOW, "subscription", "9000")
    capital.apply(journal, NOW, queued["id"], equity=9000, cash=9000, mode="paper", observed_at=NOW)
    after = journal.observe_session(NOW, sleeve(18000), number("0.08"))
    assert before["day_loss"] == after["day_loss"] == number("0.1")
    assert after["loss_paused"] and after["drawdown_latched"] and after["cash_spent"] == 0
    following = NOW + timedelta(days=1)
    next_session = journal.observe_session(following, replace(sleeve(17820), as_of=following), number("0.08"))
    assert next_session["start_equity"] == 18000 and next_session["day_loss"] == number("0.01")


def test_stop_written_after_valuation_defers_cash_and_unit_changes(tmp_path):
    strategy = running(tmp_path)
    capital.request(strategy.journal, NOW, "subscription", "5000")
    strategy.halt_path.touch()
    assert strategy._capital_step(sleeve(10000), NOW)
    assert strategy.journal.reserve_cash() == 40000
    assert capital.pending(strategy.journal)


def test_small_redemption_is_paid_from_cash_without_trading(tmp_path):
    strategy = running(tmp_path)
    capital.request(strategy.journal, NOW, "redemption", "1000")
    assert strategy._capital_step(sleeve(6000, {"ALFA": 40}), NOW) is True
    assert strategy.sent == [] and strategy.journal.reserve_cash() == number(41000)
    [notice] = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "notification"]
    assert notice["kind"] == "capital_flow"


def test_redemption_trims_a_single_security_even_with_enough_cash_and_aggregate_room(tmp_path):
    strategy = running(tmp_path)
    strategy.limits = replace(strategy.limits, max_position=number("0.1"))
    capital.request(strategy.journal, NOW, "redemption", "1200")
    assert strategy._capital_step(sleeve(8500, {"ALFA": 10, "BETA": 5}), NOW)
    [order] = [item[0] for item in strategy.sent]
    assert (order.symbol, order.side, order.quantity) == ("ALFA", "sell", 2)
    assert strategy.journal.reserve_cash() == 40000
    assert strategy._capital_step(sleeve(8700, {"ALFA": 8, "BETA": 5}), NOW)
    assert strategy.journal.reserve_cash() == 41200 and not capital.pending(strategy.journal)


def test_large_redemption_sells_a_pro_rata_slice_then_the_cash_equivalent(tmp_path):
    from scripts.autonomous_fund.cash import CashSweep
    strategy = running(tmp_path, sweep=CashSweep("SGOV"))
    request = capital.request(strategy.journal, NOW, "redemption", "5000")
    # 10,000: 1,000 cash, 6,000 in the T-bill ETF, 30% in two stocks.
    assert strategy._capital_step(sleeve(1000, {"ALFA": 15, "BETA": 15}, sweep=60), NOW) is True
    [(intent, decision_id, kw)] = strategy.sent
    # The submission guard also checkpoints the unfilled part of the slice.
    assert decision_id == f"redemption:{request['id']}" and kw["kind"] == "redemption"
    assert (intent.symbol, intent.side, intent.limit) == ("ALFA", "sell", number("99.90"))
    assert [kind for kind, _ in strategy.notices] == ["redemption_liquidating"]
    waiting = capital.pending(strategy.journal)[0]
    assert waiting["state"] == "liquidating" and waiting["exposure_start"] == "0.3"
    assert set(waiting["plan"]) == {"BETA"}  # The rest of the slice, after ALFA.
    # Stock part: 3,000 - 0.35 x 5,000 = 1,250, split across both holdings.
    alfa = intent.quantity
    assert strategy._capital_step(sleeve(1000 + alfa * 100, {"ALFA": 15 - alfa, "BETA": 15}, sweep=60), NOW)
    beta = strategy.sent[-1][0]
    assert beta.symbol == "BETA" and abs(alfa - beta.quantity) <= 1 and (alfa + beta.quantity) * 99.9 >= 1250
    # Cash part: the T-bill ETF, not more stock.
    cash = 1000 + (alfa + beta.quantity) * 100
    assert strategy._capital_step(sleeve(cash, {"ALFA": 15 - alfa, "BETA": 15 - beta.quantity}, sweep=60), NOW)
    sgov = strategy.sent[-1][0]
    assert sgov.symbol == "SGOV" and sgov.quantity * 99.9 >= 5000 - cash
    paid = sleeve(cash + sgov.quantity * 100, {"ALFA": 15 - alfa, "BETA": 15 - beta.quantity}, sweep=60 - sgov.quantity)
    assert strategy._capital_step(paid, NOW) is True and len(strategy.sent) == 3
    assert capital.pending(strategy.journal) == [] and strategy.journal.reserve_cash() == number(45000)
    stocks = (30 - alfa - beta.quantity) * 100
    assert stocks / (float(paid.equity) - 5000) <= 0.35 + 0.01  # The stock weight stayed within the drift.


def test_partially_filled_redemption_slice_resumes_only_the_unfilled_shares(tmp_path):
    from scripts.autonomous_fund.cash import CashSweep

    strategy = running(tmp_path, sweep=CashSweep("SGOV"))
    capital.request(strategy.journal, NOW, "redemption", "5000")
    assert strategy._capital_step(sleeve(1000, {"ALFA": 15, "BETA": 15}, sweep=60), NOW)
    first = strategy.sent[-1][0]
    assert first.symbol == "ALFA" and first.quantity > 2
    assert strategy._capital_step(sleeve(1200, {"ALFA": 13, "BETA": 15}, sweep=60), NOW)
    retry = strategy.sent[-1][0]
    assert retry.symbol == "ALFA" and retry.quantity == first.quantity - 2


def test_redemption_can_sell_one_leg_while_another_bid_is_unavailable(tmp_path):
    from scripts.autonomous_fund.cash import CashSweep

    strategy = running(tmp_path, sweep=CashSweep("SGOV"))
    queued = capital.request(strategy.journal, NOW, "redemption", "5000")

    def price(symbol, record=True):
        if symbol == "ALFA":
            raise RiskError("no executable bid")
        return quote()

    strategy._price = price
    assert strategy._capital_step(sleeve(1000, {"ALFA": 15, "BETA": 15}, sweep=60), NOW)
    [first] = [sent[0] for sent in strategy.sent]
    assert first.symbol == "BETA"
    [waiting] = capital.pending(strategy.journal)
    assert waiting["id"] == queued["id"] and set(waiting["plan"]) == {"ALFA"}
    filled = sleeve(1000 + first.quantity * 100, {"ALFA": 15, "BETA": 15 - first.quantity}, sweep=60)
    assert not strategy._capital_step(filled, NOW) and len(strategy.sent) == 1
    strategy._price = lambda symbol, record=True: quote()
    assert strategy._capital_step(filled, NOW) and strategy.sent[-1][0].symbol == "ALFA"


def test_next_session_rebuilds_a_slice_after_a_reverse_split(tmp_path):
    strategy = running(tmp_path)
    capital.request(strategy.journal, NOW, "redemption", "5000")
    assert strategy._capital_step(sleeve(1000, {"ALFA": 45, "BETA": 45}), NOW)
    initial = strategy.sent[-1][0]
    assert initial.symbol == "ALFA"
    [waiting] = capital.pending(strategy.journal)
    assert waiting["plan"]["BETA"] > 9

    next_session = NOW + timedelta(days=1)
    strategy.get_datetime = lambda: next_session
    strategy._price = lambda symbol, record=True: replace(
        quote(500 if symbol == "BETA" else 100), as_of=next_session)
    refreshed = Account(number(1000 + initial.quantity * 100), number(10000),
                        {"ALFA": 45 - initial.quantity, "BETA": 9}, next_session,
                        position_prices={"ALFA": "100", "BETA": "500"})
    assert strategy._capital_step(refreshed, next_session)
    assert strategy.sent[-1][0].symbol == "ALFA"
    [replanned] = capital.pending(strategy.journal)
    assert 0 < replanned["plan"]["BETA"] < 9


def test_cash_equivalent_replaces_unneeded_stock_sales_after_revaluation(tmp_path):
    from scripts.autonomous_fund.cash import CashSweep

    strategy = running(tmp_path, sweep=CashSweep("SGOV"))
    capital.request(strategy.journal, NOW, "redemption", "5000")
    assert strategy._capital_step(sleeve(1000, {"ALFA": 15, "BETA": 15}, sweep=60), NOW)
    first = strategy.sent[-1][0]
    assert first.symbol == "ALFA"
    held = sleeve(1000 + first.quantity * 100, {"ALFA": 15 - first.quantity, "BETA": 15}, sweep=60)
    # BETA's mark fell from 100 to 35. The new exposure is within the
    # limit, even though BETA was still on the earlier risk-reduction slice.
    refreshed = replace(held, equity=number(9025), position_prices={"ALFA": "100", "BETA": "35"})
    assert strategy._capital_step(refreshed, NOW)
    assert strategy.sent[-1][0].symbol == "SGOV"
    [waiting] = capital.pending(strategy.journal)
    assert waiting["plan"] == {} and waiting["slice_sale"] is None


def test_cash_sweep_sale_sizes_by_net_proceeds_after_fee_buffer(tmp_path):
    from scripts.autonomous_fund.cash import CashSweep

    strategy = running(tmp_path, sweep=CashSweep("SGOV"))
    strategy.limits = replace(strategy.limits, fee_buffer=number("0.05"), limit_buffer=number(0))
    capital.request(strategy.journal, NOW, "redemption", "9501")
    assert strategy._capital_step(sleeve(0, sweep=110), NOW)
    [sale] = [sent[0] for sent in strategy.sent]
    assert sale.symbol == "SGOV" and sale.quantity == 101  # 100 shares net only 9500.


def test_fresh_bid_cannot_authorize_a_sale_after_the_account_snapshot_expires(tmp_path):
    strategy = running(tmp_path)
    queued = capital.request(strategy.journal, NOW, "redemption", "5000")
    clock = NOW
    strategy.get_datetime = lambda: clock

    def slow_quote(symbol, record=True):
        nonlocal clock
        clock += timedelta(seconds=61)
        return replace(quote(), as_of=clock)

    strategy._price = slow_quote
    assert strategy._capital_step(sleeve(1000, {"ALFA": 90}), NOW)
    assert not strategy.sent
    [waiting] = capital.pending(strategy.journal)
    assert waiting["id"] == queued["id"] and "slice_sale" not in waiting


def test_redemption_waits_for_the_daily_sale_budget_and_a_quote(tmp_path):
    strategy = running(tmp_path)
    capital.request(strategy.journal, NOW, "redemption", "5000")
    strategy.journal.blocked = lambda db, intent, now, cap: True
    strategy._submit = Journal.reserve  # Never reached.
    assert strategy._capital_step(sleeve(1000, {"ALFA": 90}), NOW) is False
    del strategy.journal.blocked
    def missing(symbol, record=True):
        raise RiskError("quote unavailable")
    strategy._price = missing
    assert strategy._capital_step(sleeve(1000, {"ALFA": 90}), NOW) is False
    reasons = [row["body"]["reason"] for row in strategy.journal.all_events() if row["kind"] == "capital_waiting"]
    assert reasons == ["redemption_sale_budget", "redemption_quote_unavailable"]
    # Purchases wait meanwhile, so the operator is told (once a day and reason), not left with a silent stall.
    waiting = [details["reason"] for kind, details in strategy.notices if kind == "redemption_waiting"]
    assert waiting == reasons
    assert strategy._capital_step(sleeve(1000, {"ALFA": 90}), NOW) is False
    assert len([kind for kind, _ in strategy.notices if kind == "redemption_waiting"]) == 2


def test_redemption_above_the_fund_share_is_refused_and_nothing_moves(tmp_path):
    strategy = running(tmp_path)
    capital.request(strategy.journal, NOW, "redemption", "9800")
    assert strategy._capital_step(sleeve(10000), NOW) is True
    assert capital.pending(strategy.journal) == [] and strategy.journal.reserve_cash() == number(40000)
    # Terminal state and its notification must be recoverable together.
    [notice] = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "notification"]
    [outcome] = capital.status(strategy.journal)["recent_outcomes"]
    assert notice["kind"] == outcome["outcome"] == capital.REJECTED and "95%" in notice["details"]["reason"]


def test_purchases_and_cash_parking_wait_while_a_redemption_raises_cash(tmp_path):
    from scripts.autonomous_fund.cash import CashSweep
    from .test_autonomous_fund_quant import accept
    strategy = running(tmp_path, sweep=CashSweep("SGOV"))
    active = accept(strategy, [("ALFA", .1)])
    executed = []
    strategy._execute = lambda *args, **kwargs: executed.append(args)
    strategy._candidates = lambda *args: {"orders": [Intent("ALFA", "buy", 10, number(101))], "request": None,
                                          "complete": False, "requirement": None}
    capital.request(strategy.journal, NOW, "redemption", "500")
    strategy._continue_allocation(active, sleeve(9000))
    assert executed == []
    assert strategy._park_idle_cash(sleeve(9000), NOW, active["id"]) is None and strategy.sent == []
    # A sale still goes through.
    strategy._candidates = lambda *args: {"orders": [Intent("ALFA", "sell", 1, number(99))], "request": None,
                                          "complete": False, "requirement": None}
    strategy._continue_allocation(active, sleeve(9000, {"ALFA": 10}))
    assert len(executed) == 1


def test_redemption_orders_outlive_an_expired_decision(tmp_path):
    strategy = running(tmp_path)
    request = capital.request(strategy.journal, NOW, "redemption", "5000")
    canceled = []
    strategy.cancel_order = canceled.append
    for decision_id in (f"redemption:{request['id']}", "decision-1"):
        intent = Intent("ALFA", "sell", 1, number(99))
        key = strategy.journal.reserve(intent, NOW, LIMITS)
        strategy.journal.update(key, f"broker-{decision_id}", False, NOW)
        strategy.execution.record_submission(key, decision_id, intent, NOW)
        strategy.session_orders[key] = Order("quant-test", Asset("ALFA"), 1, "sell", limit_price=99)
        strategy.mode = "mock"
        strategy._manage_open_orders(NOW)
        strategy.journal.update(key, f"broker-{decision_id}", True, NOW)
    assert len(canceled) == 1  # Only the ordinary order of an expired (absent) decision.


def test_canceled_redemption_cannot_reserve_a_sale_or_keep_a_sale_open(tmp_path):
    strategy = running(tmp_path)
    request = capital.request(strategy.journal, NOW, "redemption", "5000")
    intent = Intent("ALFA", "sell", 1, number(99))
    key = strategy.journal.reserve(intent, NOW, LIMITS)
    strategy.execution.record_submission(key, f"redemption:{request['id']}", intent, NOW)
    strategy.session_orders[key] = Order("quant-test", Asset("ALFA"), 1, "sell", limit_price=99)
    capital.cancel(strategy.journal, NOW, request["id"])
    with strategy.journal.connection() as db:
        with pytest.raises(RiskError, match="no longer authorizes"):
            capital.guard_redemption_sale(db, request["id"])
    canceled = []
    strategy.cancel_order = canceled.append
    strategy._manage_open_orders(NOW)
    assert canceled == [strategy.session_orders[key]]


def test_queued_redemption_blocks_new_purchase_reservations(tmp_path):
    from scripts.autonomous_fund.strategy import AutonomousFund

    strategy = running(tmp_path)
    strategy.mode = "local"
    request = capital.request(strategy.journal, NOW, "redemption", "1000")
    intent = Intent("ALFA", "buy", 1, number(100))
    with pytest.raises(RiskError, match="Purchases wait"):
        AutonomousFund._submit(strategy, intent, NOW, "decision-1")
    assert not strategy.journal.pending()
    capital.cancel(strategy.journal, NOW, request["id"])
    assert strategy.journal.reserve(intent, NOW, LIMITS, guard=capital.guard_purchase)


def test_redemption_rounding_never_bypasses_the_hard_exposure_limit(tmp_path):
    from dataclasses import replace

    strategy = running(tmp_path)
    strategy.limits = replace(strategy.limits, max_exposure=number("0.70"))
    capital.request(strategy.journal, NOW, "redemption", "200")
    # Paying from cash would leave 6,900 / 9,800 = 70.4%, within rounding tolerance
    # but beyond the hard limit.
    assert strategy._capital_step(sleeve(3100, {"ALFA": 69}), NOW)
    assert strategy.sent[0][0].side == "sell"
    assert strategy.journal.reserve_cash() == 40000
    assert capital.pending(strategy.journal)


def test_newer_account_mark_defers_instead_of_rejecting_or_pricing_a_flow(tmp_path):
    strategy = running(tmp_path)
    capital.request(strategy.journal, NOW, "subscription", "5000")
    later = NOW + timedelta(seconds=1)
    strategy.journal.event(later, "account", {"equity": "10500"})
    assert strategy._capital_step(sleeve(10000), NOW)
    assert not strategy.notices
    assert strategy.journal.reserve_cash() == 40000
    assert capital.pending(strategy.journal)


def test_manager_context_explains_flows_and_a_waiting_redemption(tmp_path):
    strategy = running(tmp_path)
    capital.request(strategy.journal, NOW, "redemption", "500")
    context = strategy._capital_context()
    assert context["pending_requests"][0]["kind"] == "redemption" and "not gains or losses" in context["meaning"]
    from scripts.autonomous_fund.strategy import Mandate
    strategy.mandate = Mandate()
    assert strategy._capital_context() is None


# ----- Returns exclude flows ---------------------------------------------------------------------

def flow_event(at, kind, amount, units_before, units_after):
    return (at.isoformat(), "capital_flow", {"kind": kind, "amount": str(amount),
                                             "units_before": str(units_before), "units_after": str(units_after)})


def test_session_returns_and_drawdowns_use_the_nav_per_unit():
    from scripts.autonomous_fund.validation import session_pairs
    zone = ZoneInfo("America/New_York")
    day = lambda n: NOW + timedelta(days=n)  # noqa: E731
    events = [(day(0).isoformat(), "account", {"equity": "10000"}),
              flow_event(day(1) - timedelta(hours=1), "subscription", 10000, 10000, 20000),
              (day(1).isoformat(), "account", {"equity": "20200"}),
              flow_event(day(2) - timedelta(hours=1), "redemption", 15150, 20000, 5000),
              (day(2).isoformat(), "account", {"equity": "5000"})]
    sessions = {day(n).astimezone(zone).date() for n in range(3)}
    pairs, _ = session_pairs(events, sessions, zone)
    assert [round(p["return"], 6) for p in pairs] == [0.01, round(5000 / 5000 / 1.01 - 1, 6)]
    assert pairs[1]["equity_start"] == 20200  # Dollars: costs are divided by what was actually managed.


def test_report_return_and_drawdowns_exclude_a_redemption(tmp_path):
    from scripts.autonomous_fund.report import report
    journal = mandate_journal(tmp_path)
    for n, equity in ((0, "10000"), (1, "10100")):
        journal.event(NOW + timedelta(days=n), "account", {"as_of": (NOW + timedelta(days=n)).isoformat(),
                                                           "cash": equity, "equity": equity, "positions": {}})
    request = capital.request(journal, NOW + timedelta(days=1, hours=1), "redemption", "5050")
    capital.apply(journal, NOW + timedelta(days=1, hours=1), request["id"], equity=number(10100),
                  cash=number(10100), mode="paper")
    journal.event(NOW + timedelta(days=2), "account", {"as_of": (NOW + timedelta(days=2)).isoformat(),
                                                       "cash": "5050", "equity": "5050", "positions": {}})
    summary = report(tmp_path, write_files=False)
    assert summary["return"] == pytest.approx(0.01)
    assert summary["max_daily_drawdown"] == 0 and summary["max_observed_drawdown"] == 0
    assert summary["ending_equity"] == 5050  # Dollars include the flow.
    assert summary["capital"]["flows"] == 1 and summary["capital"]["net_flows_usd"] == -5050
    assert summary["capital"]["nav_per_unit"] == pytest.approx(1.01)
    assert summary["mandate"]["capital_usd"] == "4950.00"


def test_weekly_return_excludes_a_subscription():
    from scripts.autonomous_fund.notifications import _weekly_facts
    zone = ZoneInfo("America/New_York")
    monday = NOW.astimezone(zone).date()
    events = [((NOW - timedelta(days=3)).isoformat(), "account", {"equity": "10000"}),
              flow_event(NOW, "subscription", 10000, 10000, 20000),
              ((NOW + timedelta(hours=1)).isoformat(), "account", {"equity": "20400"})]
    facts = _weekly_facts(events, monday.isoformat(), (monday + timedelta(days=6)).isoformat(), zone)
    assert facts["week_return"] == pytest.approx(0.02)
    assert facts["capital_flows_usd"] == 10000 and facts["end_equity"] == 20400


def test_forward_outcomes_exclude_flows():
    from scripts.autonomous_fund.evaluation import forward_outcomes
    days = [NOW + timedelta(days=n) for n in range(0, 12)]
    days = [d for d in days if d.weekday() < 5][:8]  # The last one sets the clock: seven completed sessions.
    review = {"version": "v", "completed_at": (days[0] - timedelta(hours=1)).isoformat(), "mock": True}
    events = [((days[0] - timedelta(hours=1)).isoformat(), "postmarket_review", review)]
    for index, moment in enumerate(days):
        equity = 10000 * (1.01 ** index) * (2 if index >= 3 else 1)
        events.append((moment.isoformat(), "account", {"equity": str(equity)}))
    events.insert(4, flow_event(days[3] - timedelta(hours=1), "subscription", 10000 * 1.01 ** 3, 10000, 20000))
    [row] = forward_outcomes(sorted(events, key=lambda row: row[0]))
    assert row["horizons"]["5"]["portfolio_return"] == pytest.approx(1.01 ** 5 - 1)


# ----- Local virtual book and its ledger audit -----------------------------------------------------

def test_local_flows_move_virtual_cash_and_the_ledger_replay_verifies_them(tmp_path):
    from .test_autonomous_fund_local import NOW as LOCAL_NOW, setup_book
    _, _, journal, broker = setup_book(tmp_path)
    journal.event(LOCAL_NOW, "local_ledger_origin", {"cash": "10000", "receivables": "0", "positions": {}})
    journal.apply_mandate(LOCAL_NOW, "10000", number(10000), number(10000), {}, False, {}, True)
    assert broker.ledger_audit()["status"] != "failed"
    request = capital.request(journal, LOCAL_NOW, "subscription", "5000")
    capital.apply(journal, LOCAL_NOW, request["id"], equity=number(10000), cash=number(10000), mode="local")
    redemption = capital.request(journal, LOCAL_NOW, "redemption", "2000")
    capital.apply(journal, LOCAL_NOW, redemption["id"], equity=number(15000), cash=number(15000), mode="local")
    result = broker.ledger_audit()
    assert result["status"] != "failed" and number(result["replayed"]["cash"]) == 13000
    assert broker.receivables() == 0
    with journal.connection() as db:
        assert number(db.execute("SELECT cash FROM local_account").fetchone()[0]) == 13000
        db.execute("UPDATE local_account SET cash='14000'")  # Cash with no evidence.
    with pytest.raises(RiskError, match="cash_mismatch"):
        broker.ledger_audit()


def test_local_flow_uses_broker_float_for_freshness_but_preserves_exact_cash(tmp_path):
    from .test_autonomous_fund_local import NOW as LOCAL_NOW, setup_book
    _, _, journal, broker = setup_book(tmp_path)
    cash = number("10000.123456789012345678")
    observed = number(float(cash))
    with journal.connection() as db:
        db.execute("UPDATE local_account SET cash=?", (str(cash),))
    journal.event(LOCAL_NOW, "local_ledger_origin", {"cash": str(cash), "receivables": "0", "positions": {}})
    journal.apply_mandate(LOCAL_NOW, "10000", observed, observed, {}, False, {}, True)
    reserve = journal.reserve_cash()
    queued = capital.request(journal, LOCAL_NOW, "subscription", "1.50")
    with pytest.raises(capital.ValuationChanged, match="cash changed"):
        capital.apply(journal, LOCAL_NOW, queued["id"], equity=10000, cash="9999.99", mode="local",
                      observed_at=LOCAL_NOW)
    capital.apply(journal, LOCAL_NOW, queued["id"], equity=10000, cash=observed - reserve, mode="local",
                  observed_at=LOCAL_NOW)
    assert number(broker.ledger_audit()["replayed"]["cash"]) == cash + number("1.50")


def test_audit_cache_cannot_hide_a_corrupt_unit_event(tmp_path):
    from scripts.autonomous_fund.report import report
    from .test_autonomous_fund_local import NOW as LOCAL_NOW, setup_book
    _, _, journal, broker = setup_book(tmp_path)
    journal.event(LOCAL_NOW, "local_ledger_origin", {"cash": "10000", "receivables": "0", "positions": {}})
    journal.apply_mandate(LOCAL_NOW, "10000", 10000, 10000, {}, False, {}, True)
    queued = capital.request(journal, LOCAL_NOW, "subscription", "100")
    capital.apply(journal, LOCAL_NOW, queued["id"], equity=10000, cash=10000, mode="local")
    assert broker.ledger_audit()["status"] == "verified"
    with journal.connection() as db:
        db.execute("UPDATE events SET body=json_set(body,'$.units_after','20000') WHERE kind='capital_flow'")
    with pytest.raises(RiskError, match="capital_units_mismatch"):
        broker.ledger_audit()
    with journal.connection() as db:
        db.execute("UPDATE events SET body=json_remove(body,'$.units_after') WHERE kind='capital_flow'")
    summary = report(tmp_path, write_files=False)
    assert summary["ledger_audit"]["status"] == "failed"
    assert summary["capital"]["units"] is None and summary["return"] is None


@pytest.mark.parametrize(("key", "corrupt_value", "error"), [
    ("fund_units", '"20000"', "capital_units_projection_mismatch"),
    ("mandate_capital_basis", "20000", "capital_basis_projection_mismatch"),
])
def test_audit_rechecks_restored_capital_metadata_even_with_a_warm_cache(tmp_path, key, corrupt_value, error):
    from .test_autonomous_fund_local import NOW as LOCAL_NOW, setup_book

    _, _, journal, broker = setup_book(tmp_path)
    journal.event(LOCAL_NOW, "local_ledger_origin", {"cash": "10000", "receivables": "0", "positions": {}})
    journal.apply_mandate(LOCAL_NOW, "10000", 10000, 10000, {}, False, {}, True)
    queued = capital.request(journal, LOCAL_NOW, "subscription", "100")
    capital.apply(journal, LOCAL_NOW, queued["id"], equity=10000, cash=10000, mode="local")
    assert broker.ledger_audit()["status"] == "verified"
    with journal.connection() as db:
        db.execute("UPDATE meta SET value=? WHERE key=?", (corrupt_value, key))
    with pytest.raises(RiskError, match=error):
        broker.ledger_audit()


def test_status_and_execution_do_not_use_a_restored_stale_capital_projection(tmp_path):
    strategy = running(tmp_path)
    journal = strategy.journal
    first = capital.request(journal, NOW, "subscription", "100")
    capital.apply(journal, NOW, first["id"], equity=10000, cash=10000, mode="paper")
    second = capital.request(journal, NOW, "subscription", "200")
    with journal.connection() as db:
        db.execute("UPDATE meta SET value='\"10000\"' WHERE key='fund_units'")
    with pytest.raises(capital.CapitalLedgerError, match="disagree with confirmed flows"):
        capital.status(journal)
    with pytest.raises(capital.CapitalLedgerError, match="disagree with confirmed flows"):
        strategy._capital_step(sleeve(10100), NOW)
    assert capital.pending(journal) == [second] and journal.reserve_cash() == 39900


def test_redemption_cannot_round_away_all_outstanding_units(tmp_path):
    journal = mandate_journal(tmp_path, capital_usd="0.00000001")
    queued = capital.request(journal, NOW, "redemption", "9.50")
    with pytest.raises(RiskError, match="remaining fund units to zero"):
        capital.apply(journal, NOW, queued["id"], equity=10, cash=10, mode="paper")
    assert capital.pending(journal) == [queued]


def test_older_ledger_triggers_are_replaced_so_cash_flow_events_invalidate_the_cache(tmp_path):
    from scripts.autonomous_fund.ledger_audit import install_version_triggers, ledger_version
    journal = Journal(tmp_path, "synthetic-trigger-upgrade")
    with journal.connection() as db:
        install_version_triggers(db)
        db.execute("DROP TRIGGER ledger_version_events_insert")
        db.execute("CREATE TRIGGER ledger_version_events_insert AFTER INSERT ON events WHEN NEW.kind IN "
                   "('fill','local_corporate_action','local_ledger_origin','startup') BEGIN "
                   "UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='ledger_version'; END")
    with journal.connection() as db:
        assert ledger_version(db) is None  # A stale trigger is never trusted for reuse.
        install_version_triggers(db)
    with journal.connection() as db:
        before = ledger_version(db)
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (NOW.isoformat(), "local_cash_flow", "{}"))
        assert before is not None and ledger_version(db) != before


# ----- Operator scripts ---------------------------------------------------------------------------

def test_fund_capital_script_queues_shows_and_cancels_requests(tmp_path):
    from .test_autonomous_fund_scripts import account_book, script
    book, env = account_book(tmp_path)
    with Journal(book, "synthetic-script-account").connection() as db:
        db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
            ("mandate_capital_basis", "10000"), ("mandate_reserve_cash", "0")])
    result = script("fund-capital.sh", tmp_path, env, "subscribe", "2500", "--operation-id", "add-script-01")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "申购请求" in result.stdout and "等待中：申购 2,500.00 美元" in result.stdout
    assert "单位净值 1.0000" in result.stdout
    [request] = capital.pending(Journal(book, "synthetic-script-account"))
    retry = script("fund-capital.sh", tmp_path, env, "subscribe", "2500.00", "--operation-id", "add-script-01")
    assert retry.returncode == 0 and capital.pending(Journal(book, "synthetic-script-account")) == [request]
    result = script("fund-capital.sh", tmp_path, env, "cancel", request["id"])
    assert result.returncode == 0 and "已取消申购" in result.stdout
    result = script("fund-capital.sh", tmp_path, env, "redeem", "1.234")
    assert result.returncode != 0 and "two decimals" in result.stdout + result.stderr
    result = script("fund-capital.sh", tmp_path, env, "redeem")
    assert result.returncode == 2 and "需要一个值" in result.stderr


def test_script_records_broker_transfers_in_one_step(tmp_path):
    from .test_autonomous_fund_scripts import account_book, script
    book, env = account_book(tmp_path)
    with Journal(book, "synthetic-script-account").connection() as db:
        db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
            ("mandate_capital_basis", "10000"), ("mandate_reserve_cash", "0")])
    result = script("fund-capital.sh", tmp_path, env, "deposit", "3000")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "已登记转入 3,000.00 美元" in result.stdout and "同时登记了申购" in result.stdout
    journal = Journal(book, "synthetic-script-account")
    [request] = capital.pending(journal)
    assert (request["kind"], request["source"]) == ("subscription", "deposit") and journal.reserve_cash() == 3000
    result = script("fund-capital.sh", tmp_path, env, "deposit", "500", "--reserve-only")
    assert result.returncode == 0 and "留在保留现金" in result.stdout
    assert len(capital.pending(journal)) == 1 and journal.reserve_cash() == 3500
    result = script("fund-capital.sh", tmp_path, env, "withdraw", "4000")
    assert result.returncode != 0 and "redeem first" in result.stdout + result.stderr
    result = script("fund-capital.sh", tmp_path, env, "withdraw", "500")
    assert result.returncode == 0 and journal.reserve_cash() == 3000


def test_native_capital_commands_queue_cancel_and_record_transfers(tmp_path, capsys):
    from scripts.autonomous_fund.run import main

    journal = mandate_journal(tmp_path)
    cli = ["--run-dir", str(tmp_path)]
    main(["subscribe", "--amount", "500", "--operation-id", "cli-add-01", *cli])
    [request] = capital.pending(journal)
    assert "申购请求" in capsys.readouterr().out
    main(["subscribe", "--amount", "500", "--operation-id", "cli-add-01", *cli])
    assert capital.pending(journal) == [request]
    main(["capital", "--cancel", request["id"], *cli])
    assert "已取消申购" in capsys.readouterr().out
    main(["deposit", "--amount", "200", "--operation-id", "cli-transfer-01", *cli])
    assert journal.reserve_cash() == 40200
    [request] = capital.pending(journal)
    assert request["source"] == "deposit"
    main(["withdraw", "--amount", "100", *cli])
    assert journal.reserve_cash() == 40100


def test_local_books_refuse_broker_transfer_records(tmp_path):
    from .test_autonomous_fund_local import NOW as LOCAL_NOW, setup_book
    _, _, journal, _ = setup_book(tmp_path)
    journal.apply_mandate(LOCAL_NOW, "10000", number(10000), number(10000), {}, False, {}, True)
    with pytest.raises(RiskError, match="subscribe / redeem"):
        capital.deposit(journal, LOCAL_NOW, "1000")
    with pytest.raises(RiskError, match="subscribe / redeem"):
        capital.withdraw(journal, LOCAL_NOW, "1000")
    assert journal.reserve_cash() == 0 and capital.pending(journal) == []


def test_a_changed_env_capital_is_the_target_net_capital_after_script_flows(tmp_path):
    journal = mandate_journal(tmp_path)
    request = capital.request(journal, NOW, "subscription", "10000")
    capital.apply(journal, NOW, request["id"], equity=number(10000), cash=number(10000), mode="paper")
    # .env still says 10000: unchanged, so nothing is queued, although the fund now manages 20000.
    journal.apply_mandate(NOW, "10000", number(50000), number(50000), {}, False, {}, True)
    assert capital.pending(journal) == []
    # Editing it to 15000 means "I want 15000": a 5000 redemption, not a 5000 subscription.
    journal.apply_mandate(NOW, "15000", number(50000), number(50000), {}, False, {}, True)
    [request] = capital.pending(journal)
    assert (request["kind"], number(request["amount"])) == ("redemption", 5000)
    # The latest configuration supersedes the earlier one before it trades.
    journal.apply_mandate(NOW, "18000", number(50000), number(50000), {}, False, {}, True)
    [replacement] = capital.pending(journal)
    assert replacement["id"] != request["id"]
    assert (replacement["kind"], number(replacement["amount"])) == ("redemption", 2000)


@pytest.mark.parametrize("previous_result", ["cancelled", "rejected", "applied"])
def test_config_target_recomputes_even_when_the_estimated_flow_was_zero(tmp_path, previous_result):
    strategy = running(tmp_path)
    journal = strategy.journal
    earlier = capital.request(journal, NOW, "subscription", "2000")
    journal.apply_mandate(NOW, "12000", 50000, 50000, {}, False, {}, True)
    _, target = capital.pending(journal)
    assert target["target_capital"] == "12000" and number(target["amount"]) == 0
    if previous_result == "applied":
        capital.apply(journal, NOW, earlier["id"], equity=10000, cash=10000, mode="paper")
        account = sleeve(12000)
    elif previous_result == "cancelled":
        capital.cancel(journal, NOW, earlier["id"])
        account = sleeve(10000)
    else:
        capital.reject(journal, NOW, earlier["id"], "test rejection")
        account = sleeve(10000)
    assert strategy._capital_step(account, NOW)
    assert capital.status(journal)["capital_basis"] == "12000.00"
    assert not capital.pending(journal)
    assert sum(row["kind"] == capital.FLOW for row in journal.all_events()) == 1


def test_restored_configuration_request_recovers_its_absolute_target(tmp_path):
    strategy = running(tmp_path)
    journal = strategy.journal
    earlier = capital.request(journal, NOW, "subscription", "4000")
    journal.apply_mandate(NOW, "12000", 50000, 50000, {}, False, {}, True)
    _, target = capital.pending(journal)
    assert target["kind"] == "redemption" and target["amount"] == "2000.00"
    # Before this change the amount was saved but the absolute target was
    # available only from the configuration change event.
    with journal.connection() as db:
        db.execute("UPDATE meta SET value=json_remove(value,'$[1].target_capital') WHERE key=?", (capital.REQUESTS,))
    capital.cancel(journal, NOW, earlier["id"])
    [restored] = capital.pending(journal)
    assert restored["id"] == target["id"] and restored["kind"] == "subscription"
    assert strategy._capital_step(sleeve(10000), NOW)
    assert capital.status(journal)["capital_basis"] == "12000.00"
    assert not strategy.sent


def test_new_config_preserves_explicit_requests_while_replacing_an_old_target(tmp_path):
    journal = mandate_journal(tmp_path)
    explicit = capital.request(journal, NOW, "subscription", "1000")
    journal.apply_mandate(NOW, "8000", 50000, 50000, {}, False, {}, True)
    _, old = capital.pending(journal)
    journal.apply_mandate(NOW, "12000", 50000, 50000, {}, False, {}, True)
    waiting, replacement = capital.pending(journal)
    assert waiting == explicit and replacement["id"] != old["id"]
    assert replacement["kind"] == "subscription" and replacement["amount"] == "1000.00"


def test_oversized_config_delta_is_rejected_without_stopping_valuation_or_selling(tmp_path):
    strategy = running(tmp_path, cash=30000000)
    journal = strategy.journal
    for equity in (10000, 10010000):
        request = capital.request(journal, NOW, "subscription", "10000000")
        capital.apply(journal, NOW, request["id"], equity=equity, cash=equity, mode="paper")
    reserve = journal.reserve_cash()
    assert journal.apply_mandate(NOW, "1000000", 30000000, 30000000, {}, False, {}, True) == reserve
    assert strategy._capital_step(sleeve(20010000), NOW)
    assert not strategy.sent and not capital.pending(journal)
    assert journal.reserve_cash() == reserve
    assert "difference to the target" in capital.status(journal)["recent_outcomes"][-1]["reason"]


def test_legacy_mandate_preserves_its_configured_capital_after_first_flow(tmp_path):
    journal = mandate_journal(tmp_path)
    with journal.connection() as db:
        db.execute("DELETE FROM meta WHERE key='mandate_configured_capital'")
    request = capital.request(journal, NOW, "subscription", "5000")
    capital.apply(journal, NOW, request["id"], equity=10000, cash=10000, mode="paper")
    journal.apply_mandate(NOW, "10000", 50000, 50000, {}, False, {}, True)
    assert capital.status(journal)["capital_basis"] == "15000.00"
    assert not capital.pending(journal)


def test_a_full_queue_does_not_stop_valuation_after_a_config_change(tmp_path):
    journal = mandate_journal(tmp_path)
    requests = [capital.request(journal, NOW, "subscription", "100") for _ in range(capital.MAX_PENDING)]
    assert journal.apply_mandate(NOW, "12000", 50000, 50000, {}, False, {}, True) == 40000
    assert len(capital.pending(journal)) == capital.MAX_PENDING
    capital.apply(journal, NOW, requests[0]["id"], equity=10000, cash=10000, mode="paper")
    assert journal.apply_mandate(NOW, "12000", 50000, 50000, {}, False, {}, True) == 39900
    last = capital.pending(journal)[-1]
    assert (last["source"], last["amount"]) == ("configuration", "1000.00")


def test_nav_status_and_report_use_the_flow_valuation_until_next_account_mark(tmp_path):
    from scripts.autonomous_fund.report import report

    journal = mandate_journal(tmp_path)
    journal.event(NOW, "account", {"equity": "10200", "cash": "10200", "positions": {}})
    request = capital.request(journal, NOW, "redemption", "5000")
    capital.apply(journal, NOW, request["id"], equity=10200, cash=10200, mode="paper")
    state = capital.status(journal)
    assert number(state["equity"]) == 5200 and state["nav_per_unit"] == "1.0200"
    summary = report(tmp_path, write_files=False)
    assert summary["capital"]["nav_per_unit"] == pytest.approx(1.02)
    assert summary["ending_equity"] == 5200
    with pytest.raises(RiskError, match="95%"):
        capital.request(journal, NOW, "redemption", "5000")


def test_weekly_and_daily_returns_order_same_timestamp_marks_by_journal_sequence(tmp_path):
    import json

    from scripts.autonomous_fund.notifications import _weekly_events, _weekly_facts
    from scripts.autonomous_fund.validation import session_pairs

    journal = mandate_journal(tmp_path)
    zone = ZoneInfo("America/New_York")
    previous = NOW - timedelta(days=1)
    journal.event(previous, "account", {"equity": "10000"})
    journal.event(NOW, "account", {"equity": "10000"})
    capital.deposit(journal, NOW, "100", subscribe=False)
    request = capital.request(journal, NOW, "subscription", "5000")
    capital.apply(journal, NOW, request["id"], equity=10000, cash=10000, mode="paper")
    journal.event(NOW, "account", {"equity": "15000"})
    with journal.connection() as db:
        events = _weekly_events(db, NOW, zone)
        raw = [(at, kind, json.loads(body)) for at, kind, body in db.execute(
            "SELECT at,kind,body FROM events ORDER BY sequence")]
    start, end = (at.astimezone(zone).date() for at in (previous, NOW))
    [pair], _ = session_pairs(raw, {start, end}, zone)
    assert pair["return"] == pytest.approx(0)
    assert _weekly_facts(events, start.isoformat(), end.isoformat(), zone)["week_return"] == pytest.approx(0)


def test_weekly_cash_flows_use_the_same_valuation_interval_as_returns(tmp_path):
    from scripts.autonomous_fund.notifications import _weekly_events, _weekly_facts

    journal = mandate_journal(tmp_path)
    zone = ZoneInfo("America/New_York")
    next_day = NOW + timedelta(days=1)
    journal.event(NOW, "account", {"equity": "10000"})
    added = capital.request(journal, NOW, "subscription", "5000")
    capital.apply(journal, NOW, added["id"], equity=10000, cash=10000, mode="paper")
    journal.event(next_day, "account", {"equity": "15000"})
    taken = capital.request(journal, next_day, "redemption", "2000")
    capital.apply(journal, next_day, taken["id"], equity=15000, cash=15000, mode="paper")
    with journal.connection() as db:
        events = _weekly_events(db, next_day, zone)
    start, end = (at.astimezone(zone).date().isoformat() for at in (NOW, next_day))
    facts = _weekly_facts(events, start, end, zone)
    assert facts["end_equity"] == 15000
    assert facts["week_return"] == pytest.approx(0) and facts["capital_flows_usd"] == 5000


def test_a_redemption_the_fund_could_not_pay_is_refused_when_requested(tmp_path):
    journal = mandate_journal(tmp_path)
    journal.event(NOW, "account", {"as_of": NOW.isoformat(), "cash": "10000", "equity": "10000", "positions": {}})
    with pytest.raises(RiskError, match="95%"):
        capital.request(journal, NOW, "redemption", "9600")
    capital.request(journal, NOW, "redemption", "5000")
    with pytest.raises(RiskError, match="waiting requests included"):
        capital.request(journal, NOW, "redemption", "4600")
    capital.request(journal, NOW, "subscription", "1000")
    assert capital.request(journal, NOW, "redemption", "4600")["kind"] == "redemption"


def test_the_manager_plans_with_the_capital_it_manages_not_the_configured_value(tmp_path):
    strategy = running(tmp_path)
    assert strategy._mandate_context()["capital_usd"] == "10000"
    request = capital.request(strategy.journal, NOW, "subscription", "5000")
    capital.apply(strategy.journal, NOW, request["id"], equity=number(10000), cash=number(10000), mode="paper")
    context = strategy._mandate_context()
    assert number(context["capital_usd"]) == 15000 and context["configured_capital_usd"] == "10000"


@pytest.mark.parametrize("mode, expected, absent", [
    ("local", "本地虚拟账户已扣除", "fund-stop.sh"),
    ("paper", "模拟盘的钱不会离开券商账户", "fund-stop.sh"),
    ("live", "withdraw 5000.00", "模拟盘")])
def test_redemption_notice_tells_each_mode_what_happens_to_the_money(mode, expected, absent):
    from scripts.autonomous_fund.notifications import readable
    text = readable("capital_flow", {"kind": "redemption", "amount": "5000.00", "nav_per_unit": "1.012345",
                                     "equity_before": "10123.456789", "equity_after": "5123.456789",
                                     "units_before": "10000", "units_after": "5060.97", "mode": mode,
                                     "reserved_cash_after": "45000.00"})
    assert expected in text and absent not in text
    assert "10,123.46" in text and "1.0123" in text  # Readable money and NAV, not stored decimals.
