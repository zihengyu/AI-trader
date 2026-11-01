"""Run with python -m scripts.autonomous_fund.run --help. No live mode exists."""
import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock, Timeout as FileLockTimeout

from .risk import RiskError, instant


class ScriptedRuntime:
    """Explicit mechanical test double, NEVER used for paper trading."""
    def run(self, request):
        from lumibot.components.agents import AgentRunResult
        if any(tool.metadata.get("mutates_trading") for tool in request.bound_tools):
            raise RiskError("Read-only model unexpectedly received a trading mutation tool")
        context = request.context
        now = instant(context["as_of"])
        if request.agent_name == "researcher":
            body = {"summary": "SCRIPTED mechanical test; no investment research performed.", "evidence": [{
                "source": "Scripted fixture, not news or fundamentals", "available_at": (now - timedelta(days=1)).isoformat(),
                "observed_at": now.isoformat(), "estimated": False, "estimate_reason": ""}]}
        elif context.get("phase") == "postmarket_review":
            body = {"session_date": context["session"]["session_date"], "as_of": now.isoformat(),
                    "summary": "SCRIPTED 复盘流程测试；未进行真实模型研究或策略优化。",
                    "selection_adjustments": [], "timing_adjustments": [],
                    "retained_lessons": ["样本不足，保留现金与独立风控约束。"], "retired_hypotheses": [],
                    "data_gaps": ["脚本模型仅验证流程，不能验证投资判断。"], "confidence": "low",
                    "strategy_memo": "SCRIPTED：后续研究应复查证据与执行情况；没有经过验证的选股或择时优势。"}
        else:
            symbol = sorted(context["eligible_universe"])[0]
            body = {"as_of": now.isoformat(), "expires_at": (now + timedelta(hours=8)).isoformat(),
                    "next_review_at": (now + timedelta(hours=1)).isoformat(),
                    "rationale": "SCRIPTED test target; not an AI investment decision.",
                    "targets": [{"symbol": symbol, "weight": 0.05}]}
        return AgentRunResult(summary=json.dumps(body), model="scripted/no-inference", events=[],
                              usage={"input_tokens": 0, "output_tokens": 0})


def paper_config():
    missing = [name for name in ("ALPACA_API_KEY", "ALPACA_API_SECRET") if not os.environ.get(name)]
    if missing:
        raise RiskError("Missing environment variables: " + ", ".join(missing))
    if os.environ.get("ALPACA_IS_PAPER", "true").lower() not in {"true", "1", "yes"}:
        raise RiskError("ALPACA_IS_PAPER must be true; live trading is unavailable")
    return {"API_KEY": os.environ["ALPACA_API_KEY"], "API_SECRET": os.environ["ALPACA_API_SECRET"], "PAPER": True}


def model_ready(model):
    prefix = model.split("/", 1)[0]
    names = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}
    if prefix not in names:
        raise RiskError("Supported model prefixes: openai/, anthropic/, deepseek/")
    if not os.environ.get(names[prefix]):
        raise RiskError("Missing model environment variable: " + names[prefix])


def selected_models(args, config):
    from .strategy import model_selection
    return model_selection(config, args.model, args.research_model, args.decision_model)


def models_ready(args, config):
    selected = selected_models(args, config)
    missing = []
    for role, model in selected.items():
        try:
            model_ready(model)
        except RiskError as exc:
            missing.append(f"{role}: {exc}")
    if missing:
        raise RiskError("; ".join(missing))
    return selected


def mock_data():
    import pandas as pd
    from lumibot.entities import Asset, Data
    index = pd.bdate_range("2024-11-01", "2025-02-14", tz="America/New_York")
    result = {}
    for offset, symbol in enumerate(("ALFA", "BETA", "SPY", "QQQ")):
        prices = [100 + offset * 10 + (i % 9 - 4) * 0.2 + i * 0.03 for i in range(len(index))]
        frame = pd.DataFrame({"open": prices, "close": prices, "volume": 5_000_000}, index=index)
        frame["high"], frame["low"] = frame["open"] * 1.01, frame["open"] * .99
        asset = Asset(symbol, asset_type="stock")
        result[asset] = Data(asset, frame, timestep="day")
    return result


def csv_data(directory):
    import pandas as pd
    from lumibot.entities import Asset, Data
    result = {}
    for path in Path(directory).glob("*.csv"):
        frame = pd.read_csv(path)
        if any(pd.Timestamp(value).tzinfo is None for value in frame["timestamp"]):
            raise RiskError("CSV timestamps require an explicit timezone")
        index = pd.to_datetime(frame.pop("timestamp"), utc=True).dt.tz_convert("America/New_York")
        frame.index = pd.DatetimeIndex(index)
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise RiskError("Historical timestamps must be unique and increasing")
        columns = ["open", "high", "low", "close", "volume"]
        if frame[columns].isna().any().any() or not (frame[columns] > 0).all().all():
            raise RiskError("Historical OHLCV must be positive and complete")
        if (frame["low"] > frame[["open", "close"]].min(axis=1)).any() or (frame["high"] < frame[["open", "close"]].max(axis=1)).any():
            raise RiskError("Invalid historical OHLC envelope")
        asset = Asset(path.stem.upper(), asset_type="stock")
        result[asset] = Data(asset, frame, timestep="day")
    if not result or not {"SPY", "QQQ"} <= {a.symbol for a in result}:
        raise RiskError("CSV history must include SPY and QQQ benchmarks")
    return result


def run_backtest(args, config):
    from lumibot.backtesting import PandasDataBacktesting, YahooDataBacktesting
    from lumibot.entities import TradingFee
    from .strategy import AutonomousFund
    if (args.run_dir / "state.sqlite3").exists():
        raise RiskError("Use a fresh run directory for each historical experiment")
    data = {}
    runtime = ScriptedRuntime() if args.scripted or args.mode == "mock" else None
    source = YahooDataBacktesting
    if args.mode == "mock":
        # Synthetic symbols replace the operator universe, so its symbol groups
        # cannot apply; the default groups filter themselves to this universe.
        quant = {k: v for k, v in (config.get("quant_risk") or {}).items() if k != "groups"}
        config = {**config, "quant_risk": quant,
                  "universe": {"ALFA": "stock", "BETA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
        data, source = {"pandas_data": mock_data()}, PandasDataBacktesting
    elif args.data_dir:
        data, source = {"pandas_data": csv_data(args.data_dir)}, PandasDataBacktesting
    # Explicit nonzero fee assumption. Fill prices come from BacktestingBroker
    # and its OHLC limit-fill rules, never from a manually invented fill.
    _, strategy = AutonomousFund.run_backtest(
        source, datetime.fromisoformat(args.start), datetime.fromisoformat(args.end),
        budget=10_000, benchmark_asset=None, risk_free_rate=0.0, analyze_backtest=False,
        buy_trading_fees=[TradingFee(percent_fee=0.001)], sell_trading_fees=[TradingFee(percent_fee=0.001)],
        parameters={"config": config, "run_dir": str(args.run_dir), "mode": args.mode,
                    "account_binding": "historical", "model": args.model, "runtime": runtime,
                    "research_model": args.research_model, "decision_model": args.decision_model,
                    "memory_mode": getattr(args, "memory_mode", "rolling"), "frozen_memory": getattr(args, "frozen_memory", None)},
        show_plot=False, show_tearsheet=False, save_tearsheet=False, show_indicators=False,
        save_logfile=False, show_progress_bar=False, **data)
    strategy.broker._trade_event_log_df.to_csv(args.run_dir / "native_trade_events.csv", index=False)
    strategy.journal.export()
    from .report import report
    result = report(args.run_dir, config.get("model_prices_per_million"))
    if result["validated_decisions"] == 0:
        raise RiskError("Backtest completed without a validated decision; inspect notifications.json")
    return strategy


def run_paper(args, config):
    from lumibot.brokers import Alpaca
    from lumibot.traders import Trader
    from .strategy import AutonomousFund, assert_paper
    credentials = paper_config()
    models_ready(args, config)
    broker = Alpaca(credentials)
    assert_paper(broker)
    binding = hashlib.sha256(credentials["API_KEY"].encode()).hexdigest()
    strategy = AutonomousFund(broker=broker, name="AutonomousPaperFund", parameters={
        "config": config, "run_dir": str(args.run_dir), "mode": "paper", "account_binding": binding,
        "model": args.model, "research_model": args.research_model, "decision_model": args.decision_model,
        "require_monitor": args.require_monitor})
    trader = Trader()
    trader.add_strategy(strategy)
    try:
        from .monitor import RunnerHeartbeat
        with RunnerHeartbeat(args.run_dir):
            trader.run_all()
    finally:
        strategy.journal.export()


def check_market(args, config):
    """Read public data and credential presence; never invokes a model or broker."""
    from lumibot.entities import Asset
    from .market_data import YahooRealtimeData
    from .risk import Limits
    source = YahooRealtimeData()
    now, observations = source.get_datetime(), {}
    for symbol in ("SPY", "QQQ"):
        asset = Asset(symbol, asset_type="stock")
        quote = source.get_quote(asset)
        daily = source.get_historical_prices(asset, 30, timestep="day").df
        observations[symbol] = {"price": quote.price, "source_as_of": quote.timestamp.isoformat(),
                                "age_seconds": (source.get_datetime() - instant(quote.timestamp)).total_seconds(),
                                "completed_daily_rows": len(daily), "price_kind": quote.raw_data["price_kind"]}
    try:
        models_ready(args, config)
        ready, missing = True, None
    except RiskError as exc:
        ready, missing = False, str(exc)
    result = {"checked_at": now.isoformat(), "mode": "public_market_preflight", "market": source.session.status(),
              "source": source.SOURCE, "requires_broker_key": False, "model": selected_models(args, config)["manager"],
              "models": selected_models(args, config),
              "model_key_present": ready, "model_requirement": missing,
              "model_inference_tested": False, "observations": observations,
              "freshness_limit_seconds": Limits(**config["limits"]).quote_max_seconds}
    (args.run_dir / "preflight.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return result


def run_local(args, config):
    from lumibot.traders import Trader
    from .local_broker import LocalPaperBroker
    from .market_data import YahooRealtimeData
    from .risk import Limits
    from .strategy import AutonomousFund, open_journal
    if not args.observe_only:
        models_ready(args, config)
    if args.scripted:
        raise RiskError("Scripted models are forbidden against the running market")
    journal = open_journal(args.run_dir, "local", "local-virtual-account", config)
    broker = LocalPaperBroker(YahooRealtimeData(), journal, Limits(**config["limits"]),
                              matching_enabled=not args.observe_only, require_monitor=args.require_monitor)
    strategy = AutonomousFund(broker=broker, name="AutonomousLocalFund", parameters={
        "config": config, "run_dir": str(args.run_dir), "mode": "local", "account_binding": "local-virtual-account",
        "model": args.model, "research_model": args.research_model, "decision_model": args.decision_model,
        "observe_only": args.observe_only, "require_monitor": args.require_monitor})
    broker.do_polling()
    print("Local virtual account active. No brokerage account is connected. Observe-only=" + str(args.observe_only))
    print("Market session: " + json.dumps(broker.data_source.session.status()))
    trader = Trader()
    trader.add_strategy(strategy)
    try:
        trader.run_all(run_once=args.once)
    finally:
        broker._close_connection()
        broker.do_polling()  # Publish exited state without matching another order.
        journal.export()
        from .report import report
        report(args.run_dir, config.get("model_prices_per_million"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["mock", "backtest", "paper", "local", "check", "report", "stop", "resume", "attach-order", "reset-drawdown", "approvals", "approve", "reject", "monitor", "notify-test", "qualify"])
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--run-dir", type=Path, default=Path("artifacts/autonomous-fund"))
    parser.add_argument("--model", default=os.environ.get("AI_MODEL"))
    parser.add_argument("--research-model", help="Researcher override; takes precedence over --model/AI_MODEL")
    parser.add_argument("--decision-model", help="Portfolio manager override; takes precedence over --model/AI_MODEL")
    parser.add_argument("--start", default="2025-01-06")
    parser.add_argument("--end", default="2025-02-14")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--scripted", action="store_true", help="Historical mechanical tests only; never paper")
    parser.add_argument("--intent-id")
    parser.add_argument("--broker-order-id")
    parser.add_argument("--once", action="store_true", help="One local scheduler iteration or monitor poll, then exit")
    parser.add_argument("--observe-only", action="store_true", help="Local account monitoring without model calls or orders")
    parser.add_argument("--require-monitor", action="store_true", help="Block local/paper orders unless an independent Telegram monitor is fresh")
    parser.add_argument("--env-file", type=Path, help="Explicit optional secret file; never auto-discovered")
    parser.add_argument("--approval-id")
    parser.add_argument("--approval-digest", help="Exact digest shown by approvals; approval never submits an order")
    parser.add_argument("--console-only", action="store_true", help="Run watchdog without Telegram delivery")
    parser.add_argument("--heartbeat-seconds", type=int, default=180)
    parser.add_argument("--agent-seconds", type=int, default=900)
    parser.add_argument("--memory-file", type=Path, help="Frozen postmarket_review.json, available before a qualification window")
    args = parser.parse_args(argv)
    if args.require_monitor and args.mode not in {"local", "paper"}:
        raise RiskError("--require-monitor is available only for local/paper execution")
    if args.env_file:
        from .environment import load_environment
        load_environment(args.env_file)
        args.model = args.model or os.environ.get("AI_MODEL")
    # Set existing platform controls BEFORE importing its credential loader.
    os.environ["LUMIBOT_DISABLE_DOTENV"] = "true"
    os.environ["BACKTESTING_DATA_SOURCE"] = "none"
    os.environ["IS_BACKTESTING"] = "false" if args.mode in {"paper", "local", "check"} else "true"
    args.run_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LUMIBOT_CACHE_FOLDER", str(args.run_dir.resolve() / "cache"))
    if args.mode == "stop":
        (args.run_dir / "STOP").touch()
        print("STOP active. Running process cancels known pending orders on its next iteration; verify broker cancellations.")
        return
    if args.mode == "resume":
        (args.run_dir / "STOP").unlink(missing_ok=True)
        print("Manual stop removed; drawdown and ambiguous-order locks remain.")
        return
    if args.mode in {"approvals", "approve", "reject"}:
        from .execution import operator_connection, resolve_approval
        now = datetime.now(timezone.utc)
        if args.mode == "approvals":
            with operator_connection(args.run_dir) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='fund_approvals'").fetchone()
                rows = db.execute("SELECT * FROM fund_approvals ORDER BY rowid DESC LIMIT 30").fetchall() if exists else []
                result = [{**dict(row), "body": json.loads(row["body"])} for row in rows]
                for row in result:
                    if row["state"] in {"pending", "approved"} and now >= instant(row["body"]["expires_at"]):
                        row["effective_state"] = "expired"
            print(json.dumps(result, indent=2, ensure_ascii=True))
        else:
            if not args.approval_id or not args.approval_digest:
                raise RiskError("Both --approval-id and --approval-digest from the reviewed request are required")
            print(resolve_approval(args.run_dir, args.approval_id, args.approval_digest, args.mode == "approve", now))
        return
    if args.mode == "monitor":
        from .monitor import run_monitor
        run_monitor(args)
        return
    if args.mode == "notify-test":
        from .monitor import send_test
        print(json.dumps(send_test(args.run_dir), indent=2))
        return
    from .strategy import configuration
    from .report import report
    config = configuration(args.config)
    if args.mode == "report":
        # A read-only report must work while the trading process holds its lock.
        print(json.dumps(report(args.run_dir, config.get("model_prices_per_million"), write_files=False), indent=2))
        return
    if args.mode == "check":
        check_market(args, config)
        return
    with FileLock(str(args.run_dir / "runner.lock"), timeout=0):
        if args.mode == "reset-drawdown":
            with sqlite3.connect(args.run_dir / "state.sqlite3") as db:
                db.execute("DELETE FROM meta WHERE key IN ('peak','drawdown_paused')")
                db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (
                    datetime.now().astimezone().isoformat(), "operator_drawdown_reset", "{}"))
            print("Drawdown latch reset by operator; next account equity becomes the new peak.")
            return
        if args.mode == "attach-order":
            attach_order(args)
            return
        if args.scripted and args.mode == "paper":
            raise RiskError("Scripted models are forbidden in paper mode")
        if (args.observe_only or args.once) and args.mode != "local":
            raise RiskError("--observe-only and --once are local-mode options")
        if args.mode != "mock" and not args.scripted and not args.observe_only:
            models_ready(args, config)
        logging.getLogger().addHandler(RotatingFileHandler(args.run_dir / "runtime.log", encoding="utf-8",
                                                        maxBytes=20_000_000, backupCount=5))
        if args.mode == "qualify":
            from .evaluation import qualify
            print(json.dumps(qualify(args, config), indent=2, ensure_ascii=True))
            return
        elif args.mode == "local":
            run_local(args, config)
        elif args.mode == "paper":
            # One runner per credential, including different output directories.
            # Use a dedicated account: activity through other keys is external.
            credentials = paper_config()
            lock_root = Path("artifacts/autonomous-fund-account-locks")
            lock_root.mkdir(parents=True, exist_ok=True)
            lock_key = hashlib.sha256(credentials["API_KEY"].encode()).hexdigest()
            with FileLock(str(lock_root / (lock_key + ".lock")), timeout=0):
                run_paper(args, config)
        else:
            run_backtest(args, config)
        print(json.dumps(report(args.run_dir, config.get("model_prices_per_million")), indent=2))


def attach_order(args):
    """Operator resolves the crash window by exact broker identity, never a retry."""
    from lumibot.brokers import Alpaca
    from .strategy import assert_paper
    if not args.intent_id or not args.broker_order_id:
        raise RiskError("Both --intent-id and --broker-order-id are required")
    credentials = paper_config()
    broker = Alpaca(credentials, connect_stream=False, start_orders_thread=False)
    assert_paper(broker)
    order = broker._pull_order(args.broker_order_id, "AutonomousPaperFund")
    with sqlite3.connect(args.run_dir / "state.sqlite3") as db:
        from .strategy import configuration, account_digest, legacy_account_digest
        account_key = hashlib.sha256(credentials["API_KEY"].encode()).hexdigest()
        expected = {account_digest("paper", account_key),
                    legacy_account_digest("paper", account_key, configuration(args.config))}
        binding = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
        if not binding or binding[0] not in expected:
            raise RiskError("Recovery credentials/configuration do not match this account journal")
        row = db.execute("SELECT body,updated_at FROM intents WHERE id=? AND status!='terminal'", (args.intent_id,)).fetchone()
        if not row:
            raise RiskError("Unknown unresolved intent")
        body = json.loads(row[0])
        from .risk import number
        if order is None or (order.asset.symbol, str(order.side), number(order.quantity), str(order.order_type)) != (
                body["symbol"], body["side"], body["quantity"], "limit"):
            raise RiskError("Broker order does not match the reserved intent")
        if number(order.limit_price) != number(body["limit"]):
            raise RiskError("Broker limit does not match intent")
        created = instant(order.broker_create_date)
        reserved = instant(row[1])
        if not reserved - timedelta(seconds=5) <= created <= reserved + timedelta(minutes=5):
            raise RiskError("Broker order creation time is outside the submission window")
        db.execute("UPDATE intents SET broker_id=? WHERE id=?", (str(order.identifier), args.intent_id))
        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (
            datetime.now().astimezone().isoformat(), "operator_attached_order", json.dumps({"intent": args.intent_id, "broker_id": str(order.identifier)})))
    print("Exact matching broker order attached; normal startup will reconcile its status.")


if __name__ == "__main__":
    try:
        main()
    except FileLockTimeout as exc:
        raise SystemExit(
            f"Run directory or paper account is already in use ({exc.lock_file}). "
            "Stop the previous runner and wait for it to exit before restarting. "
            "Keep the same account directory; do not delete its lock or journal."
        ) from None
    except (RiskError, ValueError) as exc:
        raise SystemExit(str(exc))
