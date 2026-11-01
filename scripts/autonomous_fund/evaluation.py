"""Timestamp-bounded follow-up observations and paired native replay experiments."""
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from .risk import RiskError, instant


def frozen_memory(packet, now):
    from .review import PostmarketReview
    PostmarketReview.model_validate(packet["review"])
    if (instant(packet["completed_at"]) > instant(now) or instant(packet["market_close"]) > instant(now) or
            instant(packet["review"]["as_of"]) > instant(packet["completed_at"])):
        raise RiskError("Frozen strategy memory was not available at this historical time")
    return {"version": packet["version"], "session_date": packet["session_date"],
            "available_at": packet["completed_at"], "model": packet["models"]["manager"],
            "mock": packet["mock"], "review": packet["review"], "qualification_frozen": True}


def forward_outcomes(events):
    """Portfolio outcomes after a memo, explicitly NOT its causal treatment effect."""
    if not events:
        return []
    now = max(instant(at) for at, _, _ in events)
    reviews = [b for at, kind, b in events if kind == "postmarket_review" and instant(b["completed_at"]) <= now]
    accounts = [(instant(at), body) for at, kind, body in events if kind == "account" and instant(at) <= now]
    if not reviews or not accounts:
        return []
    benchmarks = {instant(at): b for at, kind, b in events if kind == "benchmark" and instant(at) <= now}
    zone = ZoneInfo("America/New_York")
    schedule = mcal.get_calendar("NYSE").schedule(start_date=min(at.date() for at, _ in accounts), end_date=now.date())
    completed = {day.date(): market.market_close for day, market in schedule.iterrows() if market.market_close <= now}
    # The last observation of each completed session, computed once. For a memo
    # available at time T, a session counts when its last observation is >= T.
    last_by_day = {}
    for at, body in accounts:
        day = at.astimezone(zone).date()
        if day in completed and (day not in last_by_day or at >= last_by_day[day][0]):
            last_by_day[day] = (at, body)
    sessions = sorted(last_by_day.values(), key=lambda item: item[0])
    result = []
    for packet in reviews:
        available = instant(packet["completed_at"])
        daily = [item for item in sessions if item[0] >= available]
        row = {"version": packet["version"], "available_at": packet["completed_at"], "mock": packet["mock"],
               "observed_sessions": len(daily), "horizons": {},
               "interpretation": "Following portfolio observations include later decisions and memos; no causal attribution. "
                                 "Marks may be intraday. Returns exclude model API charges and are not cash-flow adjusted."}
        for horizon in (5, 20):
            # Need a baseline PLUS horizon observed sessions, never pad missing dates.
            if len(daily) <= horizon:
                row["horizons"][str(horizon)] = {"status": "insufficient_observations", "needed": horizon + 1}
                continue
            first, last = daily[0], daily[horizon]
            baseline = float(first[1]["equity"])
            value = float(last[1]["equity"])
            comparison = {}
            for symbol in ("SPY", "QQQ"):
                begin = benchmarks.get(first[0], {}).get(symbol)
                end = benchmarks.get(last[0], {}).get(symbol)
                comparison[symbol] = float(end) / float(begin) - 1 if begin and end else None
            row["horizons"][str(horizon)] = {"status": "observed", "from": first[0].isoformat(),
                "through": last[0].isoformat(), "portfolio_return": value / baseline - 1 if baseline > 0 else None,
                "benchmark_price_returns": comparison}
        result.append(row)
    return result


def _history_digest(directory):
    paths = sorted(Path(directory).glob("*.csv"))
    if not paths:
        raise RiskError("Qualification requires a frozen CSV history directory")
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def qualify(args, config):
    """Run the same native engine/data/risk twice, with and without one frozen memo."""
    from .run import csv_data, run_backtest
    from .report import report
    if not args.data_dir or not args.memory_file:
        raise RiskError("qualify requires --data-dir and --memory-file; input history must be identical in both runs")
    packet = json.loads(args.memory_file.read_text(encoding="utf-8"))
    start = datetime.fromisoformat(args.start)
    if start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo("America/New_York"))
    frozen_memory(packet, start)
    data = csv_data(args.data_dir)
    if set(config["universe"]) - {asset.symbol for asset in data}:
        raise RiskError("Qualification history must cover the complete configured eligible universe")
    identity = _history_digest(args.data_dir)
    results = {}
    for variant in ("baseline", "candidate"):
        child = copy.copy(args)
        child.mode, child.run_dir = "backtest", args.run_dir / variant
        child.memory_mode = "disabled" if variant == "baseline" else "frozen"
        child.frozen_memory = packet if variant == "candidate" else None
        child.run_dir.mkdir(parents=True, exist_ok=True)
        if _history_digest(args.data_dir) != identity:
            raise RiskError("Qualification input history changed")
        run_backtest(child, config)
        results[variant] = report(child.run_dir, config.get("model_prices_per_million"))
    if _history_digest(args.data_dir) != identity:
        raise RiskError("Qualification input history changed during replay")
    a, b = results["baseline"], results["candidate"]
    sufficient = min(a["observations"], b["observations"]) >= 20
    better = (sufficient and b["return"] is not None and a["return"] is not None and b["return"] > a["return"] and
              b["max_observed_drawdown"] <= a["max_observed_drawdown"] and
              not a["fill_details_incomplete"] and not b["fill_details_incomplete"])
    summary = {"memory_version": packet["version"], "memory_available_at": packet["completed_at"],
        "scripted_model": args.scripted, "history_sha256": identity, "window": {"start": args.start, "end": args.end},
        "baseline": a, "candidate": b, "status": "insufficient_sample" if not sufficient else (
            "candidate_better_in_this_replay" if better else "no_demonstrated_improvement"),
        "auto_promoted": False,
        "limitations": "One paired replay is exploratory, not proof of profitability or an independent holdout guarantee. "
                        "No human approval is assumed; major historical trades remain blocked. "
                        "Model knowledge may contain future outcomes; audit tool evidence and historical versions. "
                        "No risk, universe, live permission or running strategy is changed."}
    (args.run_dir / "qualification.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary
