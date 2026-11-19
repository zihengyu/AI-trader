"""Deterministic strategy replay and predeclared tests; no model or broker calls.

Reuses the existing independent whole-share Book. Signals formed at a completed
close execute at the following open. Unpaid declared dividends are receivables,
not spendable cash. Missing evidence never becomes a successful validation.
"""
import copy
import hashlib
import json
import math
import os
import re
import stat
import statistics
import sys
from collections import defaultdict
from importlib.metadata import version as package_version
from functools import lru_cache
from datetime import date, timedelta
from statistics import NormalDist
from zoneinfo import ZoneInfo
from pathlib import Path

from .comparison import Book, project
from .risk import RiskError, instant, number
from .strategy_definition import daily_sample_requirements, proposal_body
from .strategy_program import (HistoryIndex, StrategyRuleError, compile_targets, parameter_values, strategy_inputs,
                               targets, validate_program)


COVERAGE_FIELDS = ("raw_prices", "corporate_actions_complete", "point_in_time_membership",
                   "publication_times_complete")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False, default=str).encode()).hexdigest()


def verify_reference(proposal, reference):
    identity = proposal["validation"].get("reference_strategy")
    if identity is None and reference is None:
        return
    if identity is None or reference is None or identity != {
            "strategy_id": reference["strategy_id"], "version": reference["version"], "digest": digest(reference)}:
        raise RiskError("Reference strategy does not match its frozen identity")
    if (reference["strategy_id"], reference["version"]) == (proposal["strategy_id"], proposal["version"]):
        raise RiskError("A strategy version cannot be its own executable reference")
    if proposal["validation"]["holding_sessions"] < reference["validation"]["holding_sessions"]:
        raise RiskError("The comparison holding horizon must cover both strategies")


def comparison_inputs(proposal, reference, policy):
    verify_reference(proposal, reference)
    if reference is None:
        return policy, None
    # The candidate owns the window and calendar. The reference contributes
    # only its frozen rules, inputs and costs, regardless of its own study plan.
    paired_reference = {**reference, "validation": proposal["validation"]}
    shared = {**policy}
    for fee in ("fee_rate", "slippage"):
        if policy.get(fee) is not None:
            shared[fee] = str(max(number(policy[fee]), number(proposal["costs"][fee]), number(reference["costs"][fee])))
    data_cost = policy.get("shared_data_cost_usd_per_session", "0")
    if data_cost is not None:
        shared["shared_data_cost_usd_per_session"] = str(max(number(data_cost), number(proposal["costs"]["daily_data_usd"]),
                                                           number(reference["costs"]["daily_data_usd"])))
    return shared, paired_reference


def snapshot_limitations(data, *, require_receipts=False):
    coverage = data.get("coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    missing = [key for key in COVERAGE_FIELDS if coverage.get(key) is not True]
    if not require_receipts or missing:
        return missing
    manifest = data.get("provenance")
    if not isinstance(manifest, dict) or manifest.get("snapshot_sha256") != digest(
            {key: value for key, value in data.items() if key != "provenance"}):
        return ["snapshot_provenance_unbound"]
    claims = manifest.get("claims")
    if not isinstance(claims, dict):
        return ["coverage_receipts_missing"]
    for key in COVERAGE_FIELDS:
        receipt = claims.get(key)
        if (not isinstance(receipt, dict)
                or not all(isinstance(receipt.get(field), str) and 0 < len(receipt[field].strip()) <= 512
                           for field in ("source", "reference"))
                or not isinstance(receipt.get("artifact_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", receipt["artifact_sha256"])):
            missing.append(f"coverage_receipt_missing:{key}")
    if any(row.get("availability_estimated") for collection in ("bars", "features")
           for rows in data.get(collection, {}).values() for row in rows):
        missing.append("estimated_publication_times")
    return missing


def _open_source_artifact(path):
    if os.name != "nt":
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create_file.restype = wintypes.HANDLE
    get_file_info = kernel32.GetFileInformationByHandleEx
    get_file_info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    get_file_info.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    GENERIC_READ, FILE_SHARE_READ, FILE_SHARE_DELETE, OPEN_EXISTING = 0x80000000, 1, 4, 3
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    handle = create_file(os.fspath(path), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                         None, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # Check the opened object, so a path replacement cannot redirect the read.
        info = (wintypes.DWORD * 2)()  # FILE_ATTRIBUTE_TAG_INFO: FileAttributes, ReparseTag.
        if not get_file_info(handle, FILE_ATTRIBUTE_TAG_INFO_CLASS, info, ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info[0] & (stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_DIRECTORY):
            raise OSError("The source artifact must be a regular file, without a reparse point")
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        close_handle(handle)
        raise


def source_artifact_gaps(data, directory):
    """Match original bytes against bound receipts without following receipt references."""
    gaps = snapshot_limitations(data, require_receipts=True)
    if gaps:
        return gaps
    directory = Path(directory)
    checks = {}
    for key in COVERAGE_FIELDS:
        expected = data["provenance"]["claims"][key]["artifact_sha256"]
        if expected not in checks:
            try:
                fd = _open_source_artifact(directory / expected)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        checks[expected] = "unreadable"
                    else:
                        checksum = hashlib.sha256()
                        with os.fdopen(fd, "rb", closefd=False) as stream:
                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                checksum.update(chunk)
                        checks[expected] = "matched" if checksum.hexdigest() == expected else "mismatch"
                finally:
                    os.close(fd)
            except FileNotFoundError:
                checks[expected] = "missing"
            except OSError:
                checks[expected] = "unreadable"
        if checks[expected] != "matched":
            gaps.append(f"source_artifact_{checks[expected]}:{key}")
    return gaps


def implementation_hash():
    # Version receipts bind execution semantics as well as model-authored rules.
    # A changed verifier/interpreter/book needs revalidation, not silent reuse.
    root = Path(__file__).parent
    sources = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
               for name in ("strategy_validation.py", "strategy_program.py", "strategy_worker.py", "comparison.py",
                            "strategy_cash.py", "cash.py", "risk.py", "strategy_definition.py",
                            "strategy_lab.py", "strategy_admission.py", "strategy_evidence.py")}
    return digest({"sources": sources, "python": list(sys.version_info[:3]),
                   "dependencies": {name: package_version(name) for name in ("pydantic", "pandas-market-calendars")}})


@lru_cache(maxsize=32)
def _daily_sessions(first, last):
    """Authoritative US daily-adapter clocks, including holidays, half days and DST."""
    import pandas_market_calendars as mcal
    if first > last:
        return ()
    schedule = mcal.get_calendar("NYSE").schedule(start_date=first, end_date=last)
    return tuple((day.date().isoformat(), row.market_open.to_pydatetime(), row.market_close.to_pydatetime())
                 for day, row in schedule.iterrows())


def forward_days(data, benchmark, after):
    """Do not erase missing observations by intersecting the forward price series.

    The first required open is fixed by the validation receipt, not by whichever
    row happened to arrive first. A missing initial session is also a data gap.
    """
    rows, after = data["bars"][benchmark], instant(after)
    sessions = _daily_sessions(after.astimezone(ZoneInfo("America/New_York")).date().isoformat(), rows[-1]["day"])
    expected = [day for day, opened, _ in sessions if opened > after]
    actual = [row["day"] for row in rows if instant(row["open_at"]) > after]
    if actual != expected:
        raise RiskError("Forward history has missing exchange sessions")
    return actual


def following_session(day):
    start = date.fromisoformat(day) + timedelta(days=1)
    sessions = _daily_sessions(start.isoformat(), (start + timedelta(days=10)).isoformat())
    if not sessions:
        raise RiskError("No next exchange session in the validation calendar")
    session, opened, _ = sessions[0]
    return session, opened


def planned_sessions(days, count=None):
    expected = [day for day, _, _ in _daily_sessions(days[0], days[-1])]
    return expected if count is None else expected[-count:]


def check_dataset(data):
    if not isinstance(data.get("bars"), dict) or not data["bars"]:
        raise RiskError("Strategy history is unavailable")
    if not isinstance(data.get("source"), str) or not data["source"].strip():
        raise RiskError("Strategy snapshot source provenance is unavailable")
    if data["bars"].keys() & data.get("features", {}).keys():
        raise RiskError("External feature series cannot replace a price-series identity")
    days = {date.fromisoformat(row["day"]).isoformat() for rows in data["bars"].values() for row in rows}
    sessions = {day: (opened, closed) for day, opened, closed in _daily_sessions(min(days), max(days))} if days else {}
    for symbol, rows in data["bars"].items():
        previous = None
        seen_days = set()
        if not rows:
            raise RiskError("Empty strategy history")
        for row in rows:
            at, available = instant(row["at"]), instant(row["available_at"])
            if available < at or previous is not None and at <= previous:
                raise RiskError("Strategy bars must be ordered, unique and available after completion")
            previous = at
            if row["day"] in seen_days:
                raise RiskError("Strategy session identities must be unique")
            seen_days.add(row["day"])
            if row["day"] != at.astimezone(ZoneInfo("America/New_York")).date().isoformat():
                raise RiskError("Strategy session identity disagrees with its timestamp")
            # at is the completed daily bar's exchange close. Publication delays
            # belong in available_at, never in a fabricated earlier price clock.
            if sessions.get(row["day"]) != (instant(row["open_at"]), at):
                raise RiskError("Daily prices must use the actual exchange session open and close")
            for field in ("open", "high", "low", "close", "volume"):
                value = row.get(field)
                if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                    raise RiskError("Strategy needs complete, finite, positive raw OHLCV")
            if not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]:
                raise RiskError("Invalid strategy OHLC envelope")
            if instant(row["open_at"]) >= at:
                raise RiskError("Strategy session open must precede close")
    action_keys = set()
    for action in data.get("actions", []):
        date.fromisoformat(action["ex_date"])
        if action.get("pay_date"):
            date.fromisoformat(action["pay_date"])
        if action["symbol"] not in data["bars"] or action["ex_date"] > action.get("pay_date", action["ex_date"]):
            raise RiskError("Invalid strategy corporate-action receipt")
        if number(action.get("dividend", 0)) < 0 or number(action.get("split", 1)) <= 0:
            raise RiskError("Invalid strategy corporate-action amount")
        # A missing publication time is a real gap; do not manufacture one.
        if not action.get("available_at"):
            raise RiskError("Corporate action publication time unavailable")
        if number(action.get("dividend", 0)) > 0 and not action.get("pay_date"):
            raise RiskError("Dividend payment date unavailable")
        for kind in ("split", "dividend"):
            if kind not in action:
                continue
            key = (action["symbol"], action["ex_date"], kind, action.get("id"))
            if key in action_keys:
                raise RiskError("Duplicate or conflicting corporate-action receipt")
            action_keys.add(key)
    for symbol, rows in data.get("features", {}).items():
        previous = None
        for row in rows:
            at, available = instant(row["at"]), instant(row["available_at"])
            if available < at or previous is not None and at <= previous:
                raise RiskError("Feature observations need ordered dates and original publication times")
            previous = at
            values = [v for k, v in row.items() if k not in {"at", "available_at", "feature_available_at", "source"}]
            if not values or any(type(v) not in {int, float} or not math.isfinite(v) for v in values):
                raise RiskError("External strategy features must be finite numeric observations")
    return digest(data)


def check_snapshot_as_of(data, now, max_rows=None):
    if max_rows is not None and sum(len(rows) for collection in ("bars", "features")
                                    for rows in data.get(collection, {}).values()) > max_rows:
        raise RiskError("Strategy dataset exceeds the configured resource bound")
    fingerprint = check_dataset(data)
    now = instant(now)
    for collection in ("bars", "features"):
        for rows in data.get(collection, {}).values():
            for row in rows:
                at = instant(row["at"])
                if at > now or instant(row["available_at"]) > now:
                    raise RiskError("A research snapshot contains observations from the future")
                if any(not at <= instant(stamp) <= now for stamp in (row.get("feature_available_at") or {}).values()):
                    raise RiskError("An individual feature has an invalid publication time")
    if any(instant(row["available_at"]) > now for row in data.get("actions", [])):
        raise RiskError("A corporate-action publication receipt is from the future")
    return fingerprint


class ResearchBook(Book):
    CHECKPOINT_AMOUNTS = ("peak", "fees", "friction", "turnover", "model_cost")

    def __init__(self, capital, policy):
        self.receivables = []
        super().__init__(capital, policy)

    def value(self, prices):
        return super().value({s: number(p) for s, p in prices.items()}) + sum(
            (number(r["amount"]) for r in self.receivables), number(0))


    def checkpoint(self, binding, held_since, signals, as_of):
        state = {"binding": binding, "last": digest(self.last), "held_since": held_since,
                 "signals": signals, "as_of": as_of, "orders": self.orders,
                 "last_risk_trade": getattr(self, "_last_risk_trade", None),
                 "amounts": {key: str(getattr(self, key)) for key in self.CHECKPOINT_AMOUNTS}}
        return {**state, "checksum": digest(state)}

    def resume(self, saved, binding, days):
        state = dict(saved["checkpoint"])
        series = saved["series"]
        if (not series or len(series) > len(days) or [row["day"] for row in series] != list(days[:len(series)])
                or state.pop("checksum") != digest(state) or state["binding"] != binding
                or state["last"] != digest(series[-1])):
            raise RiskError("Replay checkpoint does not match its frozen prefix")
        self.last = copy.deepcopy(series[-1])
        self.series = list(series)
        self.observations, self.orders = len(series), state["orders"]
        self.cash = number(self.last["cash"])
        self.positions = dict(self.last["positions"])
        self.averages = {symbol: number(value) for symbol, value in self.last["position_average_prices"].items()}
        self.receivables = copy.deepcopy(self.last["receivables"])
        self.paused = self.last["risk_increase_paused"]
        for key in self.CHECKPOINT_AMOUNTS:
            setattr(self, key, number(state["amounts"][key]))
        if state["last_risk_trade"] is not None:
            self._last_risk_trade = state["last_risk_trade"]
        return dict(state["held_since"]), dict(state["signals"]), state["as_of"]


def replay(proposal, data, policy, days, *, initial_capital, static_weight=None, initial_signal=False,
           resume=None, save_checkpoint=False):
    bars = data["bars"]
    histories = HistoryIndex({**bars, **data.get("features", {})})
    by_day = {s: {r["day"]: r for r in rows} for s, rows in bars.items()}
    benchmark = proposal["validation"]["benchmark"]
    all_days = [row["day"] for row in bars[benchmark]]
    indexes = {day: index for index, day in enumerate(all_days)}
    if not (initial_signal and not days) and (
            not days or list(days) != [day for day, _, _ in _daily_sessions(days[0], days[-1])]):
        raise RiskError("Strategy replay has missing exchange sessions")
    owned_policy = copy.deepcopy(policy)
    costs = proposal["costs"]
    shared_data = policy.get("shared_data_cost_usd_per_session", "0")
    if shared_data is None:
        raise RiskError("The account's shared data cost is unknown")
    daily_overhead = number(shared_data)
    if static_weight is None:
        owned_policy["fee_rate"] = str(max(number(policy["fee_rate"]), number(costs["fee_rate"])))
        owned_policy["slippage"] = str(max(number(policy["slippage"]), number(costs["slippage"])))
        daily_overhead = max(daily_overhead, number(costs["daily_data_usd"])) + number(costs["daily_model_usd"])
    if static_weight is not None:
        # A separately financed passive reference, not a trading permission.
        owned_policy["universe"] = {benchmark: "unlevered_etf"}
        owned_policy["limits"] = {**owned_policy["limits"], "max_position": "1", "max_exposure": "1",
                                  "drawdown": "1"}
        owned_policy["groups"] = []
    cash_symbol = policy.get("cash_symbol") if proposal["implementation"]["adapter"] == "daily_cash_rules_v1" else None
    book, held_since = ResearchBook(initial_capital, owned_policy), {}
    program = proposal["implementation"]
    compiled = compile_targets(program) if static_weight is None else None
    signals, previous_close, previous_day = {}, None, None
    nav = number(initial_capital)
    binding = digest([proposal, owned_policy, str(initial_capital), static_weight]) if save_checkpoint or resume else None
    if resume is not None:
        try:
            held_since, signals, previous_close = book.resume(resume, binding, days)
            if instant(previous_close) != instant(by_day[benchmark][book.last["day"]]["at"]):
                raise RiskError("Replay checkpoint close does not match the exchange session")
            nav = number(book.last["equity"])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            # An old or unusable checkpoint is optional. Replay the immutable
            # inputs and let the controller verify their archived prefix again.
            book, held_since = ResearchBook(initial_capital, owned_policy), {}
            signals, previous_close = {}, None
    # Warm-up only forms the initial signal. No trades, no model use and no
    # evaluation-period balance is borrowed from another split.
    prior = all_days if initial_signal else [day for day in all_days if day < days[0]]
    if prior and not book.series:
        previous_day = prior[-1]
        common = [by_day[s][previous_day] for s in proposal["instruments"] if previous_day in by_day[s]]
        if initial_signal and len(common) != len(set(proposal["instruments"])):
            raise RiskError("Initial forward signal is missing the latest close")
        if common:
            previous_close = max(r["at"] for r in common)
            signals = ({benchmark: static_weight} if static_weight is not None else targets(
                program, histories, previous_close, session_index=indexes[previous_day],
                book=book, held_since=held_since, compiled=compiled))
    actions_by_day = defaultdict(list)
    for action in data.get("actions", []):
        actions_by_day[action["ex_date"]].append(action)
    resumed_sessions = len(book.series)
    for day in days[resumed_sessions:]:
        required = set(proposal["instruments"]) | {benchmark} | set(book.positions) | ({cash_symbol} if cash_symbol else set())
        if any(day not in by_day.get(s, {}) for s in required):
            raise RiskError("A strategy split has missing sessions or instruments")
        opened = {s: by_day[s][day]["open"] for s in required}
        closed = {s: by_day[s][day]["close"] for s in required}
        open_at = min(instant(by_day[s][day]["open_at"]) for s in required)
        if previous_close is not None and instant(previous_close) >= open_at:
            raise RiskError("Strategy signal cannot execute before it was formed")
        paid = [r for r in book.receivables if r["pay_date"] <= day]
        book.cash += sum((number(r["amount"]) for r in paid), number(0))
        book.receivables = [r for r in book.receivables if r["pay_date"] > day]
        actions = []
        for action in actions_by_day[day]:
            if instant(action["available_at"]) > open_at:
                raise RiskError("Corporate action was not available at the simulated ex-date")
            symbol, ratio = action["symbol"], number(action.get("split", 1))
            dividend = number(action.get("dividend", 0))
            if ratio != 1:
                actions.append({"symbol": symbol, "date": day, "split": str(ratio)})
            # Entitlement belongs to the position entering ex-date, before trades.
            amount = number(book.positions.get(symbol, 0)) * ratio * dividend
            if amount:
                pay = action.get("pay_date")
                if not pay:
                    raise RiskError("Dividend payment date unavailable")
                if pay == day:
                    book.cash += amount
                else:
                    book.receivables.append({"symbol": symbol, "pay_date": pay, "amount": str(amount)})
        before = set(book.positions)
        # Unpaid distributions cannot finance a purchase. Scale the target
        # budget conservatively while retaining them in reported economic NAV.
        equity = book.value(opened)
        available = equity - sum((number(r["amount"]) for r in book.receivables), number(0))
        desired = {s: number(w) * available / equity for s, w in signals.items()}
        if cash_symbol:
            from .strategy_cash import step
            step(book, day, desired, opened, actions, policy=owned_policy,
                 now=open_at, session_index=indexes[day])
        else:
            book.step(day, desired, opened, actions)
        if book.error:
            raise RiskError(book.error["reason"])
        for symbol in set(book.positions) - before:
            held_since[symbol] = indexes[day]
        held_since = {s: value for s, value in held_since.items() if s in book.positions}
        book.charge(daily_overhead)
        # Execution occurred at the open, valuation at the completed close.
        nav = book.value(closed)
        book.peak = max(book.peak, nav)
        book.last.update(equity=str(nav), position_prices={s: str(closed[s]) for s in book.positions},
                         receivables=copy.deepcopy(book.receivables),
                         net_economic_equity=str(nav - book.model_cost), signal_as_of=previous_close,
                         raw_signal=signals)
        book.series[-1] = copy.deepcopy(book.last)
        previous_close = max(by_day[s][day]["at"] for s in required)
        previous_day = day
        signals = ({benchmark: static_weight} if static_weight is not None else targets(
            program, histories, previous_close, session_index=indexes[day],
            book=book, held_since=held_since, compiled=compiled))
    available = nav - sum((number(r["amount"]) for r in book.receivables), number(0))
    desired = {s: number(w) * available / nav for s, w in signals.items()}
    if cash_symbol:
        from .strategy_cash import risk_targets
        desired = risk_targets(desired, owned_policy)
    # Allocating half a sleeve validated at 10% must authorize at most 5% of
    # fund capital. The raw program may request 100%; it is not the tested cap.
    next_targets = {} if book.paused else project(desired, owned_policy)
    result = {"series": book.series, "summary": book.summary(), "next_targets": next_targets,
            "next_signal_as_of": previous_close,
            "raw_next_targets": signals,
            "cost_usd": str(book.model_cost), "risk_paused": book.paused,
            "implementation": "next_open_whole_share_v1"}
    if save_checkpoint and book.series:
        result["checkpoint"] = book.checkpoint(binding, held_since, signals, previous_close)
        result["resumed_sessions"] = resumed_sessions
    return result


def replay_strategy_reference(reference, data, policy, days, **options):
    try:
        return replay(reference, data, policy, days, **options)
    except StrategyRuleError as exc:
        raise StrategyRuleError(f"Reference strategy: {exc}") from exc
    except RiskError as exc:
        raise RiskError(f"Reference strategy: {exc}") from exc


def metrics(series, initial):
    values = [float(initial), *[float(r["net_economic_equity"]) for r in series]]
    if any(not math.isfinite(v) or v <= 0 for v in values):
        raise RiskError("Nonpositive economic research NAV")
    returns = [b / a - 1 for a, b in zip(values, values[1:])]
    peak, drawdown = values[0], 0
    for value in values:
        peak, drawdown = max(peak, value), max(drawdown, 1 - value / peak)
    vol = statistics.stdev(returns) * math.sqrt(252) if len(returns) > 1 else None
    return {"net_return": values[-1] / values[0] - 1, "max_drawdown": drawdown,
            "annual_volatility": vol, "daily_returns": returns}


def test_excess(strategy, benchmark, plan, trials):
    width = plan["holding_sessions"]
    left, right = strategy["daily_returns"], benchmark["daily_returns"]
    blocks = []
    for start in range(0, len(left) - width + 1, width):
        a = math.prod(1 + v for v in left[start:start + width])
        b = math.prod(1 + v for v in right[start:start + width])
        blocks.append(a - b)
    count = len(blocks)
    # Non-overlapping holding-horizon blocks plus a HAC variance. This does not
    # claim independence of market regimes or establish economic alpha.
    mean = statistics.fmean(blocks) if count else 0
    centered = [v - mean for v in blocks]
    variance = sum(v * v for v in centered) / count if count else 0
    lags = min(count - 1, int(4 * (count / 100) ** (2 / 9))) if count > 1 else 0
    for lag in range(1, lags + 1):
        variance += 2 * (1 - lag / (lags + 1)) * sum(
            centered[i] * centered[i - lag] for i in range(lag, count)) / count
    t = mean / math.sqrt(variance / count) if variance > 1e-20 and count else None
    critical = max(2.0, NormalDist().inv_cdf(1 - 0.025 / max(1, trials)))
    excess = strategy["net_return"] - benchmark["net_return"]
    passed = (count >= plan["min_independent_observations"] and t is not None and t >= critical
              and excess > plan["min_net_excess"] and strategy["max_drawdown"] <= plan["max_drawdown"])
    if plan["purpose"] == "defensive":
        # Defensive use is not an alpha claim: require a smaller drawdown, a
        # positive net return and a predeclared cap. Statistical promotion to
        # excess-return evidence remains separate.
        passed = (count >= plan["min_independent_observations"] and strategy["net_return"] > 0
                  and strategy["max_drawdown"] < benchmark["max_drawdown"]
                  and strategy["max_drawdown"] <= plan["max_drawdown"])
    return {"passed": bool(passed), "nonoverlapping_blocks": count, "hac_t": t,
            "trials_adjusted_threshold": critical, "net_excess": excess,
            "purpose": plan["purpose"], "meaning": "Research evidence, not proof of alpha or account authorization"}


def regime_outcomes(days, strategy, reference, benchmark_bars):
    from .scorecard import market_regimes

    sessions = _daily_sessions(benchmark_bars[0]["day"], days[-1])
    before = {following[0]: previous[0] for previous, following in zip(sessions, sessions[1:])}
    opens = {previous[0]: following[1] for previous, following in zip(sessions, sessions[1:])}
    available = [(row["day"], row["close"]) for row in benchmark_bars
                 if row["day"] in opens
                 and max(instant(row["available_at"]),
                         instant((row.get("feature_available_at") or {}).get("close", row["available_at"])))
                 < opens[row["day"]]]
    labels = market_regimes(available, sessions=tuple(day for day, _, _ in sessions))
    grouped = {}
    for day, own, passive in zip(days, strategy["daily_returns"], reference["daily_returns"], strict=True):
        label = labels.get(before.get(day), "unclassified")
        result = grouped.setdefault(label, {"sessions": 0, "strategy_nav": 1.0, "reference_nav": 1.0})
        result["sessions"] += 1
        result["strategy_nav"] *= 1 + own
        result["reference_nav"] *= 1 + passive
    return {label: {"sessions": group["sessions"], "strategy_net_return": group["strategy_nav"] - 1,
                    "reference_net_return": group["reference_nav"] - 1,
                    "net_difference": group["strategy_nav"] - group["reference_nav"]}
            for label, group in grouped.items()}


def volume_participation(series, bars):
    """Compare modeled traded shares with the same session's eventual total volume."""
    volume = {(symbol, row["day"]): row["volume"] for symbol, rows in bars.items() for row in rows}
    quantities = {}
    for day in series:
        for trade in day["trades"]:
            key = (trade["symbol"], day["day"])
            quantities[key] = quantities.get(key, 0) + trade["quantity"]
    ratios = [quantity / volume[key] for key, quantity in quantities.items() if key in volume and volume[key] > 0]
    return {"symbol_sessions_with_trades": len(quantities), "covered_symbol_sessions": len(ratios),
            "max_fraction_of_daily_volume": max(ratios, default=None),
            "median_fraction_of_daily_volume": statistics.median(ratios) if ratios else None}


def _historical_feature_rows(rows, initial_close, final_close, depth):
    """Select rows that can enter a rolling window during this experiment."""
    visible = [row for row in rows if instant(row["available_at"]) <= final_close]
    if depth is None:
        return visible
    initial = [row for row in visible if instant(row["available_at"]) <= initial_close]
    if len(initial) < depth:
        return visible
    lower = instant(initial[-depth]["at"])
    return [row for row in visible if instant(row["at"]) >= lower]


def historical_input_gap(proposal, data, policy, *, locked_sessions=None, reference=None):
    paired_policy, paired_reference = comparison_inputs(proposal, reference, policy)
    gap = _historical_arm_gap(proposal, data, paired_policy, locked_sessions)
    if not gap and paired_reference is not None:
        gap = _historical_arm_gap(paired_reference, data, paired_policy, locked_sessions)
        if gap:
            return {**gap, "reason": "Reference strategy: " + gap["reason"]}
    return gap


def _historical_arm_gap(proposal, data, policy, locked_sessions):
    """Identify deterministic input gaps before claiming a slot or exposing a final window."""
    if policy.get("fee_rate") is None or policy.get("slippage") is None:
        return {"status": "needs_data", "passed": False, "reason": "Verified execution costs are unavailable"}
    if policy.get("shared_data_cost_usd_per_session", "0") is None:
        return {"status": "needs_data", "passed": False, "reason": "The account's shared data cost is unknown"}
    plan = proposal["validation"]
    cash = None
    if proposal["implementation"]["adapter"] == "daily_cash_rules_v1":
        if not policy.get("cash_symbol") or not (policy.get("cash_management") or {}).get("enabled"):
            return {"status": "needs_data", "passed": False, "reason": "Cash-management policy unavailable"}
        cash = policy["cash_symbol"]
    try:
        inputs = strategy_inputs(proposal, cash)
    except StrategyRuleError as exc:
        return {"status": "needs_formalization", "passed": False, "reason": str(exc)}
    symbols = inputs.prices
    if symbols - data["bars"].keys():
        return {"status": "needs_data", "passed": False, "reason": "Missing required instrument history",
                "missing_symbols": sorted(symbols - data["bars"].keys())}
    bars = data["bars"]
    histories = {**bars, **data.get("features", {})}
    missing = sorted(symbol for symbol in inputs.declared if not histories.get(symbol))
    if missing:
        return {"status": "needs_data", "passed": False, "reason": "A declared data series is unavailable",
                "missing_symbols": missing}
    benchmark = plan["benchmark"]
    days = [row["day"] for row in bars[benchmark]]
    total = plan["train_sessions"] + plan["validation_sessions"] + plan["final_sessions"]
    if locked_sessions is None and len(days) < total:
        return {"status": "needs_data", "passed": False, "reason": "Insufficient predeclared split history",
                "observed_sessions": len(days), "required_sessions": total}
    expected = days[-total:] if locked_sessions is None else list(locked_sessions)
    if locked_sessions is not None and (len(expected) != total or set(expected) - set(days)):
        return {"status": "needs_data", "passed": False, "reason": "Snapshot does not cover the locked validation sessions"}
    sessions = _daily_sessions(expected[0], expected[-1])
    if expected != [day for day, _, _ in sessions]:
        return {"status": "needs_data", "passed": False, "reason": "Benchmark history has missing exchange sessions"}

    prior = _daily_sessions(min(days[0], expected[0]), expected[0])[:-1]
    initial_close = prior[-1][2] if prior else sessions[0][2]
    final_close = sessions[-1][2]
    # The preceding close forms the initial signal. An absolute session index
    # also depends on all earlier benchmark sessions in the supplied history.
    depths = dict.fromkeys([*proposal["instruments"], benchmark], 1) if prior else {}
    if prior and inputs.uses_session_index:
        depths[benchmark] = None
    first_observation = {}
    for (symbol, column), depth in inputs.fields.items():
        rows = _historical_feature_rows(histories.get(symbol, ()), initial_close, final_close, depth)
        if not rows or any(type(value) not in {int, float} or not math.isfinite(value)
                           for value in (row.get(column) for row in rows)):
            return {"status": "needs_data", "passed": False, "reason": "A required strategy feature is unavailable",
                    "missing_feature": {"symbol": symbol, "column": column}}
        if symbol in symbols:
            previous = depths.get(symbol, 0)
            depths[symbol] = None if previous is None or depth is None else max(previous, depth)
            first_observation[symbol] = min(first_observation.get(symbol, expected[0]), rows[0]["day"])

    expected_set = set(expected)
    missing = sorted(symbol for symbol in symbols if expected_set - {row["day"] for row in bars[symbol]})
    if missing:
        return {"status": "needs_data", "passed": False, "reason": "Instruments lack required exchange sessions",
                "missing_symbols": missing}
    for symbol, depth in sorted(depths.items()):
        # Preserve a genuine series start. Detect internal holes in the prior
        # sessions used by a lookback, including older rows used after a delay.
        days = [row["day"] for row in bars[symbol]]
        warmup = planned_sessions([min(days[0], expected[0]), expected[0]])[:-1]
        if depth is not None:
            earlier = first_observation.get(symbol, expected[0])
            warmup = set(warmup[-depth:]) | {day for day in warmup if day >= earlier}
        required = set(warmup) | expected_set
        if required - set(days):
            return {"status": "needs_data", "passed": False, "reason": "Strategy history has missing exchange sessions in its window or warm-up",
                    "missing_symbols": [symbol]}
    return None


def validate(proposal, data, policy, *, initial_capital="10000", trials=1, allow_final=True, locked_sessions=None,
             require_receipts=False, reference=None):
    proposal = proposal_body(proposal)
    if proposal["validation"]["method"] != "daily_forward_v1":
        return {"status": "needs_adapter", "passed": False, "reason": "A suitable independent validation adapter is required"}
    if proposal["implementation"] is None or validate_program(proposal["implementation"], proposal["instruments"]) != "ready":
        return {"status": "needs_adapter", "passed": False}
    identity = check_dataset(data)
    plan = proposal["validation"]
    required = daily_sample_requirements(plan)
    short = {key: required[key] for key in ("validation_sessions", "final_sessions") if plan[key] < required[key]}
    if short:
        return {"status": "needs_formalization", "passed": False, "minimum_sessions": short,
                "reason": "Declared split cannot supply the required nonoverlapping blocks after purging"}
    if gap := historical_input_gap(proposal, data, policy, locked_sessions=locked_sessions, reference=reference):
        return gap
    paired_policy, paired_reference = comparison_inputs(proposal, reference, policy)
    total = plan["train_sessions"] + plan["validation_sessions"] + plan["final_sessions"]
    days = locked_sessions if locked_sessions is not None else [r["day"] for r in data["bars"][plan["benchmark"]]][-total:]
    train_end = plan["train_sessions"]
    valid_end = train_end + plan["validation_sessions"]
    gap = plan["holding_sessions"]
    windows = {"train": days[:train_end], "validation": days[train_end + gap:valid_end],
               "final": days[valid_end + gap:]}
    if any(not values for values in windows.values()):
        return {"status": "needs_data", "passed": False, "reason": "Purge exhausted a split"}
    limitations = snapshot_limitations(data, require_receipts=require_receipts)
    if policy.get("cash_symbol") and proposal["implementation"]["adapter"] != "daily_cash_rules_v1":
        limitations.append("cash_sweep_execution_adapter_required")
    if paired_reference and policy.get("cash_symbol") and paired_reference["implementation"]["adapter"] != "daily_cash_rules_v1":
        limitations.append("reference_cash_sweep_execution_adapter_required")
    results, receipts = {}, {}
    for name, window in windows.items():
        if name == "final" and (not allow_final or limitations):
            continue
        actual = replay(proposal, data, paired_policy, window, initial_capital=initial_capital)
        passive = replay(proposal, data, policy, window, initial_capital=initial_capital,
                         static_weight=plan["benchmark_weight"])
        a, b = metrics(actual["series"], initial_capital), metrics(passive["series"], initial_capital)
        result = test_excess(a, b, plan, trials)
        # Additional equal-to-observed friction is a conservative cost stress;
        # it does not claim to reproduce changed fills or market impact.
        extra = float(actual["summary"]["fees"]) + float(actual["summary"]["friction"])
        result["stress_net_return"] = a["net_return"] - extra / float(initial_capital)
        result["passed"] &= result["stress_net_return"] > 0 and not actual["risk_paused"]
        results[name] = {**result, "strategy": {k: v for k, v in a.items() if k != "daily_returns"},
                         "reference": {k: v for k, v in b.items() if k != "daily_returns"},
                         "regimes": regime_outcomes(window, a, b, data["bars"][plan["benchmark"]]),
                         "start": window[0], "end": window[-1]}
        receipts[name] = {"strategy": actual, "reference": passive}
        if paired_reference is not None:
            control = replay_strategy_reference(paired_reference, data, paired_policy, window, initial_capital=initial_capital)
            c = metrics(control["series"], initial_capital)
            comparison = test_excess(a, c, plan, trials)
            results[name]["strategy_reference"] = {**comparison, "net_return": c["net_return"],
                                                   "max_drawdown": c["max_drawdown"]}
            results[name]["passed"] &= comparison["passed"]
            receipts[name]["strategy_reference"] = control
    # Fixed-version expanding-history evaluations: each subsequent fold may use
    # earlier observations for signals, never later observations or refit params.
    validation_days = windows["validation"]
    splits = plan["walk_forward_splits"]
    folds = [validation_days[i * len(validation_days) // splits:(i + 1) * len(validation_days) // splits]
             for i in range(splits)]
    walk = []
    for window in folds:
        actual = replay(proposal, data, paired_policy, window, initial_capital=initial_capital)
        passive = replay(proposal, data, policy, window, initial_capital=initial_capital,
                         static_weight=plan["benchmark_weight"])
        a, b = metrics(actual["series"], initial_capital), metrics(passive["series"], initial_capital)
        walk.append({"start": window[0], "end": window[-1], "net_excess": a["net_return"] - b["net_return"],
                     "net_return": a["net_return"], "max_drawdown": a["max_drawdown"]})
        if paired_reference is not None:
            control = replay_strategy_reference(paired_reference, data, paired_policy, window, initial_capital=initial_capital)
            walk[-1]["strategy_reference_net_excess"] = a["net_return"] - metrics(control["series"], initial_capital)["net_return"]
    agreeing = sum(r["net_return"] > 0 and r["max_drawdown"] <= plan["max_drawdown"] and (
        min(r["net_excess"], r.get("strategy_reference_net_excess", math.inf)) > plan["min_net_excess"]
        if plan["purpose"] == "excess_return" else True) for r in walk)
    sensitivity = []
    parameters = parameter_values(proposal["implementation"])
    for key, value in parameters.items():
        for scale in (0.8, 1.2):
            changed = copy.deepcopy(proposal)
            trial = round(value * scale) if type(value) is int else value * scale
            if trial == value:
                trial = value + (-1 if scale < 1 else 1) if type(value) is int else value
            for parameter in changed["implementation"]["parameters"]:
                if parameter["name"] == key:
                    parameter["value"] = trial
            try:
                replayed = replay(changed, data, paired_policy, validation_days, initial_capital=initial_capital)
                stats = metrics(replayed["series"], initial_capital)
                good = stats["net_return"] > 0 and stats["max_drawdown"] <= plan["max_drawdown"]
                if plan["purpose"] == "excess_return":
                    validation = results["validation"]
                    good &= stats["net_return"] > max(validation["reference"]["net_return"],
                                                      validation.get("strategy_reference", {}).get("net_return", -math.inf))
                sensitivity.append({"parameter": key, "value": trial, "passed": bool(good)})
            except (RiskError, ArithmeticError, ValueError):
                sensitivity.append({"parameter": key, "value": trial, "passed": False})
    robust = agreeing / len(walk) >= plan["min_robust_fraction"] and (not sensitivity or
             sum(row["passed"] for row in sensitivity) / len(sensitivity) >= plan["min_robust_fraction"])
    passed = "final" in results and all(results[name]["passed"] for name in ("validation", "final")) and robust
    return {"status": "needs_data" if limitations else "awaiting_new_holdout" if not allow_final else
            "historical_pass" if passed else "rejected",
            "passed": passed, "dataset_hash": identity, "protocol_hash": digest(plan),
            "tests": results, "receipts": receipts, "limitations": limitations,
            "walk_forward": {"folds": walk, "agreeing": agreeing, "passed": robust,
                             "meaning": "Fixed implementation; no retraining or independent-regime claim"},
            "parameter_sensitivity": sensitivity,
            "knowledge_limitation": "Designed with a contemporary model: historical evidence remains exploratory; "
                                    "a frozen forward record is mandatory before paper-library admission.",
            "unmodeled": ["intraday_limits_and_stops", "market_impact_and_queue",
                          "asynchronous_cash_sweep_fills_and_approval_delays",
                          "full_production_quant_overlay_and_order_approvals"]}
