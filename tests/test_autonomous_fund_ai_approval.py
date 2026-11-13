"""Full/semi automation through the native paper-fund lifecycle, without inference or APIs."""
from datetime import timedelta
import json

import pytest

from scripts.autonomous_fund.execution import resolve_approval
from scripts.autonomous_fund.risk import RiskError
from scripts.autonomous_fund.risk_review import AutomationSettings


@pytest.fixture
def native(tmp_path, monkeypatch):
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import AutonomousFund, open_journal
    from tests.test_autonomous_fund_local import Clock
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    made = []

    def create(*, mode="full", verdict=True, hook=None, invalid=None, force_review=True, valid_minutes=60,
               symbols=("ALFA",), reviewer_model=None, manager_model=None):
        root = tmp_path / f"book-{len(made)}"
        clock = Clock()
        source = YahooRealtimeData(now=clock, fetch=clock.fetch)
        config = {"model": "deepseek/deepseek-flash", "limits": {}, "automation": {"mode": mode},
                  "approval": {"auto_daily_exposure_increase": "0" if force_review else "0.25",
                               "valid_minutes": valid_minutes},
                  "universe": {**{s: "stock" for s in symbols}, "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
        if reviewer_model is not None:
            config["automation"]["model"] = reviewer_model
        if manager_model is not None:
            config["agent_models"] = {"manager": manager_model}
        journal = open_journal(root, "local", "synthetic-approval-account", config)
        calls = []
        requested_models = {}

        class Runtime(ScriptedRuntime):
            def run(self, request):
                calls.append((request.agent_name, request.context))
                requested_models[request.agent_name] = request.model
                if request.agent_name == "risk_reviewer" and hook is not None:
                    hook(request, strategy, clock)
                result = super().run(request)
                if request.agent_name == "manager":
                    body = json.loads(result.summary)
                    body["targets"] = [{"symbol": s, "weight": .09} for s in symbols]
                    result.summary = json.dumps(body)
                elif request.agent_name == "risk_reviewer":
                    body = json.loads(result.summary)
                    body["approve"] = verdict
                    body["reason"] = "Synthetic independent assessment"
                    if invalid == "id":
                        body["approval_id"] = "unrelated-request"
                    elif invalid == "digest":
                        body["request_digest"] = "0" * 64
                    elif invalid == "extra":
                        body["relax_hard_limits"] = True
                    result.summary = json.dumps(body)
                return result

        from scripts.autonomous_fund.risk import Limits
        broker = LocalPaperBroker(source, journal, Limits(), connect_stream=False)
        strategy = AutonomousFund(broker=broker, name="ApprovalLifecycle", parameters={"config": config,
            "run_dir": str(root), "mode": "local", "account_binding": "synthetic-approval-account", "runtime": Runtime()})
        strategy._test_requested_models = requested_models
        strategy._executor._initialize()
        made.append(broker)
        return strategy, clock, source, journal, calls

    yield create
    for broker in made:
        broker._close_connection()


def request_row(journal):
    with journal.connection() as db:
        return dict(db.execute("SELECT * FROM fund_approvals ORDER BY rowid DESC LIMIT 1").fetchone())


def advance(clock, source, seconds=20):
    clock.value += timedelta(seconds=seconds)
    source._cache.clear()  # Only this synthetic source's request cache.


def money(journal):
    with journal.connection() as db:
        return (tuple(db.execute("SELECT cash,receivables FROM local_account").fetchone()),
                [tuple(r) for r in db.execute("SELECT * FROM local_positions ORDER BY symbol")],
                db.execute("SELECT count(*) FROM local_fills").fetchone()[0])


def test_full_mode_ai_approves_exact_scope_then_a_fresh_iteration_sends_and_fills(native):
    strategy, clock, source, journal, calls = native()
    strategy.on_trading_iteration()
    approval = request_row(journal)
    assert approval["state"] == "approved" and json.loads(approval["body"])["approver"] == "ai"
    assert not journal.pending()  # The review does not submit using its pre-inference snapshot.
    assert [name for name, _ in calls] == ["critic", "manager", "risk_reviewer"]
    with journal.connection() as db:
        result = json.loads(db.execute("SELECT body FROM events WHERE kind='ai_approval'").fetchone()[0])
        assert result["id"] == approval["id"] and result["digest"] == approval["digest"]
        assert db.execute("SELECT count(*) FROM events WHERE kind='operator_approval'").fetchone()[0] == 0
    advance(clock, source)
    strategy.on_trading_iteration()
    identifier = journal.pending()[0]["broker_id"]
    advance(clock, source, 120)
    strategy.broker.do_polling()
    assert strategy.broker._pull_broker_order(identifier)["status"] == "fill"
    with journal.connection() as db:
        submitted = json.loads(db.execute("SELECT body FROM events WHERE kind='order_submitted'").fetchone()[0])
        assert submitted["authorization"]["source"] == "ai"
        assert submitted["authorization"]["approval_id"] == approval["id"]
        assert db.execute("SELECT count(*) FROM events WHERE kind='approval_used'").fetchone()[0] == 1
        outcome = json.loads(db.execute("SELECT body FROM events WHERE kind='agent_outcome' "
                            "AND json_extract(body,'$.role')='risk_reviewer'").fetchone()[0])
        assert outcome["valid"] and outcome["approval_id"] == approval["id"]


def test_semi_mode_waits_for_human_approval(native):
    strategy, clock, source, journal, calls = native(mode="semi")
    strategy.on_trading_iteration()
    approval = request_row(journal)
    assert approval["state"] == "pending" and not journal.pending()
    assert "risk_reviewer" not in [name for name, _ in calls]
    resolve_approval(journal.root, approval["id"], approval["digest"], True, clock())
    advance(clock, source)
    strategy.on_trading_iteration()
    assert journal.pending()
    with journal.connection() as db:
        proof = json.loads(db.execute("SELECT body FROM fund_order_authority").fetchone()[0])
        assert proof["source"] == "operator"


def test_inside_automatic_envelope_never_calls_an_approver(native):
    strategy, _, _, journal, calls = native(force_review=False)
    strategy.on_trading_iteration()
    assert journal.pending() and "risk_reviewer" not in [name for name, _ in calls]
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM fund_approvals").fetchone()[0] == 0
        proof = json.loads(db.execute("SELECT body FROM fund_order_authority").fetchone()[0])
        assert proof["source"] == "automatic" and proof["audit"]["automatic_buy_budget_usd"]


def test_semi_pending_exception_can_coexist_with_only_the_automatic_portion(native):
    from scripts.autonomous_fund.risk import number
    strategy, clock, source, journal, calls = native(mode="semi", force_review=False,
        symbols=("ALFA", "BETA", "GAMMA", "DELTA", "EPSILON", "ZETA"))
    for _ in range(6):
        strategy.on_trading_iteration()
        advance(clock, source, 140)
        strategy.broker.do_polling()
    assert request_row(journal)["state"] == "pending"
    assert "risk_reviewer" not in [name for name, _ in calls]
    account = strategy._snapshot()
    exposure = (account.equity - account.cash - account.receivables) / account.equity
    assert 0 < exposure <= number(".252")
    with journal.connection() as db:
        receipts = [json.loads(row[0]) for row in db.execute("SELECT body FROM fund_order_authority")]
        assert receipts and {row["source"] for row in receipts} == {"automatic"}
        assert not db.execute("SELECT 1 FROM events WHERE kind='approval_used'").fetchone()


def test_ai_denial_cannot_send_a_purchase_or_silently_become_approval(native):
    strategy, clock, source, journal, calls = native(verdict=False)
    strategy.on_trading_iteration()
    assert request_row(journal)["state"] == "rejected" and not journal.pending()
    before = money(journal)
    advance(clock, source)
    strategy.on_trading_iteration()
    assert money(journal) == before
    assert sum(name == "risk_reviewer" for name, _ in calls) == 1


@pytest.mark.parametrize("change", ["stop", "expired", "mode", "human_reject", "restart_policy"])
def test_review_cannot_approve_after_its_authority_changes(native, change):
    def hook(request, strategy, clock):
        if change == "stop":
            strategy.halt_path.touch()
        elif change == "expired":
            clock.value += timedelta(minutes=61)
        elif change == "mode":
            strategy.automation = AutomationSettings(mode="semi")
        elif change == "restart_policy":
            # A replacement runner has published a new policy while the old
            # process's provider call is still returning.
            strategy.journal.set_meta("execution_policy_digest", "replacement-policy")
        else:
            body = request.context["approval_request"]
            resolve_approval(strategy.run_dir, body["id"], body["digest"], False, clock())
    strategy, _, _, journal, _ = native(hook=hook)
    strategy.on_trading_iteration()
    assert request_row(journal)["state"] != "approved" and not journal.pending()
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='ai_approval'").fetchone()[0] == 0


def test_timeout_keeps_request_pending_with_persistent_backoff(native):
    def timeout(*args):
        raise TimeoutError("Synthetic risk-review timeout")
    strategy, clock, source, journal, calls = native(hook=timeout)
    strategy.on_trading_iteration()
    assert request_row(journal)["state"] == "pending" and not journal.pending()
    advance(clock, source)
    strategy.on_trading_iteration()
    assert sum(name == "risk_reviewer" for name, _ in calls) == 1
    with journal.connection() as db:
        review = db.execute("SELECT attempts,state FROM fund_ai_reviews").fetchone()
        assert tuple(review) == (1, "failed")


def test_persistent_attempt_limit_never_becomes_fallback_approval(native):
    def timeout(*args):
        raise TimeoutError("Synthetic risk-review timeout")
    strategy, clock, source, journal, calls = native(hook=timeout)
    strategy.on_trading_iteration()
    approval = request_row(journal)
    for _ in range(4):
        advance(clock, source, 301)
        strategy._review_purchase_request(approval, strategy._snapshot(), clock())
    assert sum(name == "risk_reviewer" for name, _ in calls) == 3
    assert request_row(journal)["state"] == "pending" and not journal.pending()
    with journal.connection() as db:
        assert tuple(db.execute("SELECT attempts,state FROM fund_ai_reviews").fetchone()) == (3, "failed")


def test_late_result_cannot_overwrite_a_newer_leased_attempt(native):
    from scripts.autonomous_fund import risk_review
    def replacement(request, strategy, clock):
        clock.value += timedelta(seconds=strategy._risk_review_lease_seconds + 1)
        assert risk_review.claim(strategy.journal, request_row(strategy.journal), strategy.execution.digest,
            strategy.automation, clock, 60) == 2
    strategy, _, _, journal, _ = native(hook=replacement)
    strategy.on_trading_iteration()
    assert request_row(journal)["state"] == "pending" and not journal.pending()
    with journal.connection() as db:
        assert tuple(db.execute("SELECT attempts,state FROM fund_ai_reviews").fetchone()) == (2, "running")


@pytest.mark.parametrize("invalid", ["id", "digest", "extra"])
def test_ai_cannot_approve_another_request_or_expand_the_schema(native, invalid):
    strategy, _, _, journal, _ = native(invalid=invalid)
    strategy.on_trading_iteration()
    assert request_row(journal)["state"] == "pending" and not journal.pending()
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='ai_approval'").fetchone()[0] == 0


def test_an_ai_yes_cannot_bypass_the_hard_daily_exposure_limit(native):
    from scripts.autonomous_fund.risk import number
    strategy, clock, source, journal, calls = native(symbols=("ALFA", "BETA", "GAMMA", "DELTA", "EPSILON", "ZETA"))
    strategy.on_trading_iteration()
    approval = request_row(journal)
    assert number(json.loads(approval["body"])["max_exposure_after"]) <= number("0.50")
    for _ in range(9):
        advance(clock, source, 140)
        strategy.broker.do_polling()
        strategy.on_trading_iteration()
    account = strategy._snapshot()
    exposure = (account.equity - account.cash - account.receivables) / account.equity
    assert 0 < exposure <= number("0.502")
    assert sum(name == "risk_reviewer" for name, _ in calls) == 1


@pytest.mark.parametrize("boundary", ["quote", "acceptance", "approval_expiry", "policy_change"])
def test_submission_rechecks_approval_after_slow_or_concurrent_work(native, boundary):
    strategy, clock, source, journal, _ = native(valid_minutes=1 if boundary == "approval_expiry" else 60)
    strategy.on_trading_iteration()
    approval = request_row(journal)
    fired = []

    def revoke():
        if not fired:
            fired.append(True)
            if boundary == "approval_expiry":
                clock.value += timedelta(seconds=61)
            elif boundary == "policy_change":
                journal.set_meta("execution_policy_digest", "replacement-policy")
            else:
                resolve_approval(journal.root, approval["id"], approval["digest"], False, clock())

    if boundary == "acceptance":
        original = strategy.broker._accept_order
        def accept(order):
            revoke()
            return original(order)
        strategy.broker._accept_order = accept
    else:
        original = strategy._price
        def price(*args, **kwargs):
            if kwargs.get("record") is False:
                revoke()
            return original(*args, **kwargs)
        strategy._price = price
    before = money(journal)
    advance(clock, source)
    strategy.on_trading_iteration()
    assert fired and not journal.pending() and money(journal) == before
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM local_orders").fetchone()[0] == 0


def test_mode_status_uses_the_loaded_startup_not_a_later_environment_edit(native, monkeypatch):
    from scripts.autonomous_fund.status import summary
    strategy, clock, _, journal, _ = native(mode="semi")
    monkeypatch.setenv("FUND_AUTOMATION_MODE", "full")
    text = summary(journal.root, "local", clock())
    assert "半自动（人工审批例外买入）" in text


@pytest.mark.parametrize("price,hard,known_fee", [("100", False, False), ("100", True, False),
                                                ("99.90", True, False), ("100", True, True)])
def test_purchase_cash_turnover_reserves_fees_before_authorizing_whole_shares(tmp_path, price, hard, known_fee):
    from dataclasses import replace
    from scripts.autonomous_fund.execution import ApprovalPolicy, ExecutionState
    from scripts.autonomous_fund.risk import number
    from tests.test_autonomous_fund_quant import NOW, accept, account, default_frames, harness, quote
    strategy = harness(tmp_path, default_frames())
    # Keep the positive $99.90 case above the no-trade band so this test
    # exercises the cash/fee boundary rather than the default $100 floor.
    strategy.quant = replace(strategy.quant, rebalance_band=number(0), min_trade_notional=number(1))
    strategy.limits = replace(strategy.limits, limit_buffer=number(0), fee_buffer=number(".001"))
    if known_fee:
        from scripts.autonomous_fund.local_broker import LocalPaperBroker
        broker = object.__new__(LocalPaperBroker)
        broker.fee_rate = number(".002")
        strategy.broker = broker
        strategy.limits = replace(strategy.limits, fee_buffer=number(0))
    strategy.execution = ExecutionState(strategy.journal, ApprovalPolicy(
        auto_daily_buy_turnover=".01", max_daily_buy_turnover=".01" if hard else "1"))
    account = account()
    strategy.journal.observe_session(NOW, account)
    active = accept(strategy, [("ALFA", .02)])
    result = strategy._candidates(active, account, {"ALFA": quote(float(price))}, NOW)
    if number(price) == 100:
        assert not result["orders"]  # $100 notional plus a fee cannot fit a $100 cash budget.
        assert (result["request"] is None) is hard
    else:
        order = result["orders"][0]
        assert order.quantity * order.limit * number("1.001") <= 100


@pytest.mark.parametrize("partial", [False, True])
def test_revoked_ai_approval_cancels_before_another_simulated_fill(native, partial):
    strategy, clock, source, journal, _ = native()
    strategy.on_trading_iteration()
    approval = request_row(journal)
    advance(clock, source)
    strategy.on_trading_iteration()
    identifier = journal.pending()[0]["broker_id"]
    if partial:
        clock.volume = 100
        advance(clock, source, 120)
        strategy.broker.do_polling()
    prefix = strategy.broker._pull_broker_order(identifier)["filled"]
    assert prefix == int(partial)
    before = money(journal)
    resolve_approval(journal.root, approval["id"], approval["digest"], False, clock())
    source.corporate_actions = lambda *a, **k: (_ for _ in ()).throw(RiskError("Synthetic action outage"))
    strategy.broker.do_polling()
    stored = strategy.broker._pull_broker_order(identifier)
    assert stored["status"] == "canceled" and stored["filled"] == prefix
    assert money(journal) == before


@pytest.mark.parametrize("legacy", [False, True])
def test_mode_policy_change_cancels_existing_allocation_purchases(native, legacy):
    from scripts.autonomous_fund.execution import ExecutionState
    strategy, clock, source, journal, _ = native()
    strategy.on_trading_iteration()
    advance(clock, source)
    strategy.on_trading_iteration()
    identifier = journal.pending()[0]["broker_id"]
    if legacy:
        with journal.connection() as db:
            db.execute("DELETE FROM fund_order_authority")
    before = money(journal)
    ExecutionState(journal, strategy.execution.policy, risk_digest="replacement-semi-mode-policy")
    advance(clock, source, 120)
    strategy.broker.do_polling()
    assert strategy.broker._pull_broker_order(identifier)["status"] == "canceled"
    assert money(journal) == before


def test_full_is_the_default_and_environment_can_select_semi():
    from pathlib import Path
    from scripts.autonomous_fund.strategy import configuration
    config = Path(__file__).resolve().parents[1] / "scripts/autonomous_fund/config.json"
    assert AutomationSettings().mode == "full"
    assert configuration(config, environ={})["automation"]["mode"] == "full"
    assert configuration(config, environ={"FUND_AUTOMATION_MODE": "semi"})["automation"]["mode"] == "semi"
    with pytest.raises(RiskError):
        configuration(config, environ={"FUND_AUTOMATION_MODE": "unknown"})


def test_custom_reviewer_requires_its_explicit_provider_key(monkeypatch):
    from types import SimpleNamespace
    from scripts.autonomous_fund.run import models_ready
    args = SimpleNamespace(model=None, research_model=None, decision_model=None)
    config = {"model": "deepseek/deepseek-flash",
              "automation": {"model": "anthropic/synthetic-review-model"}}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-value")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RiskError, match="risk_reviewer.*ANTHROPIC_API_KEY"):
        models_ready(args, config)
    config["automation"]["mode"] = "semi"
    assert "risk_reviewer" not in models_ready(args, config)


@pytest.mark.parametrize("environ,common,decision,expected", [
    ({}, None, None, "deepseek/deepseek-v4-pro"),
    ({"FUND_DECISION_MODEL": "deepseek/synthetic-manager"}, None, None, "deepseek/synthetic-manager"),
    ({}, None, "anthropic/synthetic-manager", "anthropic/synthetic-manager"),
    ({}, "openai/synthetic-common", None, "openai/synthetic-common"),
])
def test_shipped_flash_reviewer_is_independent_of_manager_overrides(environ, common, decision, expected):
    from pathlib import Path
    from types import SimpleNamespace
    from scripts.autonomous_fund.run import selected_models
    from scripts.autonomous_fund.strategy import configuration
    config = configuration(Path(__file__).resolve().parents[1] / "scripts/autonomous_fund/config.json", environ=environ)
    args = SimpleNamespace(model=common, research_model=None, decision_model=decision, mode="local")
    selected = selected_models(args, config)
    assert config["automation"]["model"] == "deepseek/deepseek-flash"
    assert selected["risk_reviewer"] == "deepseek/deepseek-flash"
    assert selected["manager"] == expected


def test_native_reviewer_call_records_flash_while_manager_uses_another_model(native):
    strategy, _, _, journal, _ = native(reviewer_model="deepseek/deepseek-flash", manager_model="deepseek/synthetic-manager")
    strategy.on_trading_iteration()
    assert request_row(journal)["state"] == "approved"
    with journal.connection() as db:
        approval = json.loads(db.execute("SELECT body FROM events WHERE kind='ai_approval'").fetchone()[0])
    # The mock response deliberately identifies itself as scripted/no-inference;
    # inspect the actual native runtime requests rather than relabeling that evidence.
    assert strategy._test_requested_models["manager"] == "deepseek/synthetic-manager"
    assert strategy._test_requested_models["risk_reviewer"] == approval["model"] == "deepseek/deepseek-flash"


def test_environment_selected_approval_model_reaches_the_native_runtime(native):
    from pathlib import Path
    from scripts.autonomous_fund.strategy import configuration
    chosen = "openai/synthetic-approval"
    config = configuration(Path(__file__).resolve().parents[1] / "scripts/autonomous_fund/config.json",
                           environ={"FUND_APPROVAL_MODEL": chosen})
    strategy, _, _, journal, _ = native(reviewer_model=config["automation"]["model"], manager_model="deepseek/synthetic-manager")
    strategy.on_trading_iteration()
    assert request_row(journal)["state"] == "approved"
    assert strategy._test_requested_models["risk_reviewer"] == chosen
    assert strategy._test_requested_models["manager"] == "deepseek/synthetic-manager"
    with journal.connection() as db:
        startup = json.loads(db.execute("SELECT body FROM events WHERE kind='startup' ORDER BY sequence DESC LIMIT 1").fetchone()[0])
    assert startup["automation"]["model"] == startup["models"]["risk_reviewer"] == chosen


@pytest.mark.parametrize("mode", ["backtest", "mock", "qualify"])
def test_historical_experiments_do_not_require_unused_reviewer_credentials(monkeypatch, mode):
    from types import SimpleNamespace
    from scripts.autonomous_fund.run import models_ready
    args = SimpleNamespace(model=None, research_model=None, decision_model=None, mode=mode)
    config = {"model": "deepseek/deepseek-flash",
              "automation": {"mode": "full", "model": "anthropic/synthetic-review-model"}}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-value")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "risk_reviewer" not in models_ready(args, config)


def test_selected_event_exposes_recorded_ai_authority_and_exact_request(native):
    from scripts.autonomous_fund.decision_activity import activity, lines
    strategy, clock, source, journal, _ = native()
    strategy.on_trading_iteration()
    approval = request_row(journal)
    advance(clock, source)
    strategy.on_trading_iteration()
    with journal.connection() as db:
        event = db.execute("SELECT sequence FROM events WHERE kind='decision_validated'").fetchone()[0]
    rendered = "\n".join(lines(activity(journal.root, now=clock()), timeline=True, event_sequence=event))
    assert "独立 AI 审批" in rendered and approval["id"] in rendered
    assert "自动额度依据" in rendered and "查询时状态 approved" in rendered


def test_historical_report_does_not_backdate_an_ai_approval(native):
    from scripts.autonomous_fund.report import report
    def delay(request, strategy, clock):
        clock.value += timedelta(seconds=5)
    strategy, clock, _, journal, _ = native(hook=delay)
    strategy.on_trading_iteration()
    approval = request_row(journal)
    created = json.loads(approval["body"])["created_at"]
    before = report(journal.root, write_files=False, as_of=created)
    after = report(journal.root, write_files=False, as_of=clock())
    assert before["approval_requests"][0]["state"] == "pending"
    assert after["approval_requests"][0]["state"] == "approved"
