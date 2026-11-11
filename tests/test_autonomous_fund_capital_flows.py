"""Operator subscriptions and redemptions (capital.py): unit NAV, risk state, liquidity waterfall, reports.

Synthetic accounts, prices and journals only: no network, broker or inference.
"""
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
    strategy.mandate = Mandate.from_config({"capital_usd": "10000"})
    strategy.journal.apply_mandate(NOW, "10000", number(cash), number(cash), {}, False, {}, True)
    strategy.broker = SimpleNamespace(is_market_open=lambda: True)
    strategy.next_review = None
    strategy.cash_sweep = sweep
    strategy._price = lambda symbol, record=True: quote(100)
    strategy.sent = []
    strategy._submit = lambda intent, now, decision_id, **kw: strategy.sent.append((intent, decision_id, kw))
    return strategy


def sleeve(cash, positions=None, sweep=0):
    positions = positions or {}
    value = sum(q * 100 for q in positions.values())
    return Account(number(cash + sweep * 100), number(cash + sweep * 100 + value), positions, NOW,
                   cash_equivalents={"SGOV": sweep} if sweep else {}, cash_equivalent_value=number(sweep * 100))


def test_subscription_is_applied_at_the_next_valuation_and_asks_the_manager_to_replan(tmp_path):
    strategy = running(tmp_path)
    capital.request(strategy.journal, NOW, "subscription", "10000")
    assert strategy._capital_step(sleeve(4000, {"ALFA": 60}), NOW) is True
    assert strategy.journal.reserve_cash() == number(30000)
    kinds = [kind for kind, _ in strategy.notices]
    assert kinds == ["capital_flow"] and number(strategy.notices[0][1]["equity_after"]) == 20000
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


def test_small_redemption_is_paid_from_cash_without_trading(tmp_path):
    strategy = running(tmp_path)
    capital.request(strategy.journal, NOW, "redemption", "1000")
    assert strategy._capital_step(sleeve(6000, {"ALFA": 40}), NOW) is True
    assert strategy.sent == [] and strategy.journal.reserve_cash() == number(41000)
    assert [kind for kind, _ in strategy.notices] == ["capital_flow"]


def test_large_redemption_sells_a_pro_rata_slice_then_the_cash_equivalent(tmp_path):
    from scripts.autonomous_fund.cash import CashSweep
    strategy = running(tmp_path, sweep=CashSweep("SGOV"))
    request = capital.request(strategy.journal, NOW, "redemption", "5000")
    # 10,000: 1,000 cash, 6,000 in the T-bill ETF, 30% in two stocks.
    assert strategy._capital_step(sleeve(1000, {"ALFA": 15, "BETA": 15}, sweep=60), NOW) is True
    [(intent, decision_id, kw)] = strategy.sent
    assert decision_id == f"redemption:{request['id']}" and kw == {"kind": "redemption"}
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
    [(kind, details)] = strategy.notices
    assert kind == "capital_request_rejected" and "95%" in details["reason"]


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
    canceled = []
    strategy.cancel_order = canceled.append
    for decision_id in ("redemption:abc", "decision-1"):
        intent = Intent("ALFA", "sell", 1, number(99))
        key = strategy.journal.reserve(intent, NOW, LIMITS)
        strategy.journal.update(key, f"broker-{decision_id}", False, NOW)
        strategy.execution.record_submission(key, decision_id, intent, NOW)
        strategy.session_orders[key] = Order("quant-test", Asset("ALFA"), 1, "sell", limit_price=99)
        strategy.mode = "mock"
        strategy._manage_open_orders(NOW)
        strategy.journal.update(key, f"broker-{decision_id}", True, NOW)
    assert len(canceled) == 1  # Only the ordinary order of an expired (absent) decision.


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
    result = script("fund-capital.sh", tmp_path, env, "subscribe", "2500")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "申购请求" in result.stdout and "等待中：申购 2,500.00 美元" in result.stdout
    assert "单位净值 1.0000" in result.stdout
    [request] = capital.pending(Journal(book, "synthetic-script-account"))
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
    # Waiting requests count: 18000 while 15000 is already on its way adds 3000.
    journal.apply_mandate(NOW, "18000", number(50000), number(50000), {}, False, {}, True)
    assert [(r["kind"], number(r["amount"])) for r in capital.pending(journal)] == [
        ("redemption", 5000), ("subscription", 3000)]


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
