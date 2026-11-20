"""Read-only, account-scoped observations for qualifying an independent market feed."""
import json
import logging
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .datastore import DailyStore
from .risk import MarketDataUnavailable, ObservedPrice, instant, number, trading_day
from .session_calendar import _session_bounds
from .status import RUNNER_FRESH_SECONDS, _read


LOG = logging.getLogger(__name__)
INTERVAL_SECONDS = 60
PHASES = ("opening", "intraday", "closing", "after_close")
PHASE_NAMES = dict(zip(PHASES, ("开盘", "盘中", "收盘前", "收盘后")))
REFERENCE_DATASET = "source_reference"


def _folder(root):
    return Path(root) / "market-source-observation"


def _symbols(root, references):
    """Read the virtual book only for instrument identities, in one snapshot."""
    path = Path(root) / "state.sqlite3"
    tracked = set(references)
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("BEGIN")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "local_positions" in tables:
                tracked.update(row[0] for row in db.execute("SELECT symbol FROM local_positions WHERE quantity>0"))
            if "local_orders" in tables:
                tracked.update(row[0] for row in db.execute(
                    "SELECT json_extract(body,'$.symbol') FROM local_orders WHERE status IN ('new','partial_fill')"))
            if "fund_decisions" in tables:
                latest = db.execute("SELECT body FROM fund_decisions ORDER BY rowid DESC LIMIT 1").fetchone()
                if latest:
                    decision = json.loads(latest[0])
                    for key in ("targets", "alerts"):
                        tracked.update(row["symbol"] for row in decision.get(key, []))
    return sorted(symbol for symbol in tracked if symbol)


def _phase(now, opened, closed):
    if opened is None or not opened <= now < closed + timedelta(minutes=5):
        return None
    if now >= closed:
        return "after_close"
    if now < opened + timedelta(minutes=15):
        return "opening"
    if now >= closed - timedelta(minutes=15):
        return "closing"
    return "intraday"


def _quote(price, stamp, checked_at, opened, closed, limits, *, kind, received_at=None):
    if price is None or stamp is None:
        return {"status": "missing"}
    row = {"kind": kind}
    try:
        stamp, price = instant(stamp), number(price)
        row.update(source_as_of=stamp.isoformat(), price=str(price), age_seconds=(checked_at - stamp).total_seconds())
        if received_at is not None:
            row["received_at"] = instant(received_at).isoformat()
        if not opened <= stamp <= closed:
            return {**row, "status": "outside_session"}
        ObservedPrice(price, stamp).validate(checked_at, limits)
    except MarketDataUnavailable as exc:
        return {**row, "status": (exc.diagnostic or {}).get("reason", "invalid")}
    except (TypeError, ValueError, OverflowError):
        return {**row, "status": "invalid"}
    return {**row, "status": "valid"}


def _minute(raw, checked_at, opened, closed, limits):
    if not raw:
        return {"status": "missing"}
    row = {}
    try:
        start = instant(raw["start"])
        end = start + timedelta(minutes=1)
        row.update(start=start.isoformat(), end=end.isoformat(), age_seconds=(checked_at - end).total_seconds())
        if raw.get("checked_at"):
            row["received_at"] = instant(raw["checked_at"]).isoformat()
        if start.second or start.microsecond:
            return {**row, "status": "invalid"}
        if end > checked_at:
            return {**row, "status": "forming_or_future"}
        if not opened <= start < end <= closed:
            return {**row, "status": "outside_session"}
        values = {field: number(raw[field]) for field in ("open", "high", "low", "close", "volume")}
        low, high = values["low"], values["high"]
        if not 0 < low <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= high:
            return {**row, "status": "invalid"}
        if values["volume"] < 0:
            return {**row, "status": "invalid"}
        row.update({field: str(value) for field, value in values.items()})
        # After the close, measure whether the exact final bar arrived, preserving its real age.
        if checked_at >= closed:
            status = "valid" if end == closed else "missing_final_minute"
        else:
            status = "valid" if row["age_seconds"] <= limits.quote_max_seconds else "stale"
        return {**row, "status": status}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {**row, "status": "invalid"}


def _alpaca_minute(bar, clock, opened, closed, limits):
    if not isinstance(bar, dict) or not bar:
        return {"status": "missing"}
    raw = {"start": bar.get("t"), "checked_at": clock,
           **{field: bar.get(short) for field, short in zip(("open", "high", "low", "close", "volume"), "ohlcv")}}
    return _minute(raw, clock, opened, closed, limits)


def _alpaca_row(data, clock, opened, closed, limits):
    data = data if isinstance(data, dict) else {}
    quote, trade, bar = (data.get(key) or {} for key in ("latestQuote", "latestTrade", "minuteBar"))
    quote = quote if isinstance(quote, dict) else {}
    trade = trade if isinstance(trade, dict) else {}
    midpoint = None
    try:
        bid, ask = number(quote.get("bp")), number(quote.get("ap"))
        if 0 < bid <= ask:
            midpoint = (bid + ask) / 2
    except (TypeError, ValueError):
        pass
    mark = _quote(midpoint, quote.get("t"), clock, opened, closed, limits, kind="iex_bid_ask_midpoint", received_at=clock)
    if midpoint is not None:
        mark.update(bid=str(bid), ask=str(ask))
    elif quote:
        mark["status"] = "invalid"
    latest = _quote(trade.get("p"), trade.get("t"), clock, opened, closed, limits, kind="iex_last_trade", received_at=clock)
    return {"quote": mark, "trade": latest, "minute": _alpaca_minute(bar, clock, opened, closed, limits)}


def _yahoo_row(market, symbol, clock, opened, closed, limits):
    quote = market.get("quotes_by_symbol", {}).get(symbol, {})
    minute = market.get("completed_minutes", {}).get(symbol, {})
    quote_row = (_quote(quote.get("price"), quote.get("source_as_of"), clock, opened, closed, limits,
                        kind=quote.get("price_kind"), received_at=quote.get("checked_at")) if quote
                 else {"status": "not_observed"})
    minute_row = _minute(minute, clock, opened, closed, limits) if minute else {"status": "not_observed"}
    return {"quote": quote_row, "minute": minute_row}


def _broker_midpoint(quote, now, opened, closed, limits):
    """Both sides need their own clock. Quote.timestamp may be receipt time."""
    if quote is None or quote.bid_time is None or quote.ask_time is None:
        return {"status": "missing"}
    try:
        bid, ask = number(quote.bid), number(quote.ask)
        bid_at, ask_at = instant(quote.bid_time), instant(quote.ask_time)
    except (TypeError, ValueError, OverflowError):
        return {"status": "invalid"}
    if not 0 < bid <= ask:
        return {"status": "invalid"}
    if max(bid_at, ask_at) > now:
        return {"status": "future"}
    if max(bid_at, ask_at) > closed:
        return {"status": "outside_session"}
    row = _quote((bid + ask) / 2, min(bid_at, ask_at), now, opened, closed, limits,
                 kind="bid_ask_midpoint", received_at=now)
    return {**row, "bid": str(bid), "ask": str(ask), "bid_as_of": bid_at.isoformat(), "ask_as_of": ask_at.isoformat()}


def research_reference(store, now, limits):
    """Read the latest independently observed mark, checking its age again at use."""
    if store is None:
        return None
    now = instant(now)
    day = trading_day(now)
    opened, closed = _session_bounds(day)
    if opened is None or not opened <= now < closed:
        return None
    saved = store.get(day, REFERENCE_DATASET)
    if not isinstance(saved, dict) or not isinstance(saved.get("symbols"), dict):
        return None
    try:
        if not opened <= instant(saved.get("as_of")) <= now:
            return None
    except (TypeError, ValueError):
        return None
    quotes = {}
    for symbol, readings in saved["symbols"].items():
        if not isinstance(readings, dict):
            continue
        for source in ("schwab", "twelve_data"):
            row = readings.get(source)
            if not isinstance(row, dict) or row.get("status") != "valid":
                continue
            try:
                stamp, received = instant(row.get("source_as_of")), instant(row.get("received_at"))
            except (TypeError, ValueError):
                continue
            age = (now - stamp).total_seconds()
            if opened <= stamp <= received <= now and age <= limits.quote_max_seconds:
                quotes[symbol] = {**row, "source": source, "age_seconds": age}
                break
    return {"as_of": saved["as_of"], "symbols": quotes, "requests": saved.get("requests", {}),
            "usage": "Research reference only. A Twelve Data close is a completed candle, not a bid, ask or last trade."}


def _differences(alpaca, yahoo):
    a, y = alpaca["trade"], yahoo["quote"]
    result = {}
    if a["status"] == y["status"] == "valid" and "trade" in (y.get("kind") or ""):
        result["last_trade_bps"] = float((number(a["price"]) / number(y["price"]) - 1) * 10000)
        result["last_trade_skew_seconds"] = (instant(a["source_as_of"]) - instant(y["source_as_of"])).total_seconds()
    a, y = alpaca["minute"], yahoo["minute"]
    if a["status"] == y["status"] == "valid" and instant(a["start"]) == instant(y["start"]):
        result["same_minute_close_bps"] = float((number(a["close"]) / number(y["close"]) - 1) * 10000)
    return result


def _failure(exc):
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return {"status": "rate_limited" if code == 429 else "request_failed", "error_type": type(exc).__name__,
            **({"http_status": code} if type(code) is int else {})}


class SourceObserver:
    def __init__(self, root, alpaca, references, limits, clock, *, schwab=None, twelve_data=None):
        self.root, self.alpaca, self.references, self.limits, self.clock = Path(root), alpaca, references, limits, clock
        self.schwab, self.twelve_data = schwab, twelve_data
        self.store = DailyStore(self.root / "cache" / "daily")
        self._action_slot = None
        self._next_request = {}
        self._twelve_index = 0

    def _request(self, fetch, now, *, source="alpaca"):
        retry_at = self._next_request.get(source)
        if retry_at is not None and now < retry_at:
            return None, {"status": "rate_limit_cooldown", "retry_at": retry_at.isoformat()}
        try:
            return fetch(), {"status": "ok"}
        except Exception as exc:
            error = _failure(exc)
            if error["status"] == "rate_limited":
                at = instant(self.clock())
                headers = getattr(exc.response, "headers", {})
                retry_at = at + timedelta(seconds=INTERVAL_SECONDS)
                try:
                    retry_at = max(retry_at, datetime.fromtimestamp(int(headers.get("X-RateLimit-Reset", 0)), timezone.utc),
                                   at + timedelta(seconds=int(headers.get("Retry-After", 0))))
                except (OSError, TypeError, ValueError, OverflowError):
                    pass
                self._next_request[source] = retry_at
                error["retry_at"] = retry_at.isoformat()
            return None, error

    def poll(self, now):
        now = instant(now)
        day = trading_day(now)
        opened, closed = _session_bounds(day)
        phase = _phase(now, opened, closed)
        if phase is None:
            return None
        symbols = _symbols(self.root, self.references)
        if not symbols:
            return None

        snapshots, request = (self._request(lambda: self.alpaca.iex_snapshots(symbols), now) if self.alpaca else
                              (None, {"status": "not_configured"}))
        observed = instant(self.clock())
        beat = _read(self.root / "status.json") or {}
        try:
            runner_fresh = (beat.get("mode") == "local" and beat.get("runner_state") == "running"
                            and 0 <= (observed - instant(beat["heartbeat_at"])).total_seconds() <= RUNNER_FRESH_SECONDS)
        except (KeyError, TypeError, ValueError):
            runner_fresh = False
        market = (beat.get("market_data") or {}) if runner_fresh else {}
        yahoo_failures = [{key: row[key] for key in ("symbol", "interval", "period", "first_error_type", "error_type",
                                                    "first_failed_at", "failed_at",
                                                    "consecutive_failures", "retry_in_seconds") if key in row}
                          for row in market.get("source_failures", []) if row.get("symbol") in symbols]
        records = {}
        for symbol in symbols:
            alpaca = _alpaca_row((snapshots or {}).get(symbol), observed, opened, closed, self.limits)
            if snapshots is None:
                alpaca = {kind: {"status": request["status"]} for kind in ("quote", "trade", "minute")}
            yahoo = _yahoo_row(market, symbol, observed, opened, closed, self.limits)
            records[symbol] = {"alpaca": alpaca, "yahoo": yahoo}
        record = {"session": day, "phase": phase, "scheduled_at": now.isoformat(), "observed_at": observed.isoformat(),
                  "feed": "iex", "alpaca_request": request, "yahoo_runner_fresh": runner_fresh,
                  "yahoo_failures": yahoo_failures, "symbols": records}

        if self.alpaca and phase == "after_close":
            pending = [symbol for symbol in symbols if records[symbol]["alpaca"]["minute"]["status"] != "valid"]
            if pending:
                minutes, status = self._request(lambda: self.alpaca.iex_final_minutes(pending, closed), observed)
                record["alpaca_closing_request"] = status
                receipt = instant(self.clock())
                for symbol in pending:
                    records[symbol]["alpaca"]["minute"] = (
                        _alpaca_minute(minutes.get(symbol), receipt, opened, closed, self.limits)
                        if minutes is not None else {"status": status["status"]})
        for readings in records.values():
            readings["differences"] = _differences(readings["alpaca"], readings["yahoo"])

        if phase != "after_close" and (self.schwab or self.twelve_data):
            self._independent_references(record, symbols, opened, closed)

        slot = day, phase, now.hour, tuple(symbols)
        if self.alpaca and slot != self._action_slot:
            end = date.fromisoformat(day)
            start = end - timedelta(days=35)
            actions, status = self._request(lambda: self.alpaca.corporate_actions(symbols, start, end), instant(self.clock()))
            record["corporate_actions"] = {"alpaca_request": status, "observed_at": instant(self.clock()).isoformat(),
                                           "window": [start.isoformat(), end.isoformat()], "alpaca": actions,
                                           "yahoo": {s: market["corporate_actions"][s] for s in symbols
                                                     if s in market.get("corporate_actions", {})}}
            if actions is not None:
                self._action_slot = slot

        folder = _folder(self.root)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{day}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
        return record

    def _independent_references(self, record, symbols, opened, closed):
        requests, readings = {}, {symbol: {} for symbol in symbols}
        if self.schwab:
            quotes, requests["schwab"] = self._request(lambda: self.schwab.quotes(symbols), instant(self.clock()),
                                                       source="schwab")
            checked_at = instant(self.clock())
            for symbol in symbols:
                mark = ({"status": requests["schwab"]["status"]} if quotes is None else
                        _broker_midpoint(quotes.get(symbol), checked_at, opened, closed, self.limits))
                records = record["symbols"][symbol]
                readings[symbol]["schwab"] = records["schwab"] = mark
                iex = records["alpaca"]["quote"]
                if mark["status"] == iex["status"] == "valid":
                    skew = (instant(mark["source_as_of"]) - instant(iex["source_as_of"])).total_seconds()
                    records["differences"]["schwab_iex_midpoint_skew_seconds"] = skew
                    if abs(skew) <= 5:
                        records["differences"]["schwab_iex_midpoint_bps"] = float(
                            (number(mark["price"]) / number(iex["price"]) - 1) * 10000)
        if self.twelve_data:
            # One symbol per round keeps a regular session inside the Basic plan's daily quota.
            missing = [symbol for symbol in symbols if readings[symbol].get("schwab", {}).get("status") != "valid"]
            pool = missing or symbols
            symbol = pool[self._twelve_index % len(pool)]
            self._twelve_index += 1
            bars, requests["twelve_data"] = self._request(lambda: self.twelve_data.minutes(symbol),
                                                           instant(self.clock()), source="twelve_data")
            checked_at = instant(self.clock())
            mark = {"status": requests["twelve_data"]["status"] if bars is None else "missing"}
            for bar in bars or []:
                minute = _minute(bar, checked_at, opened, closed, self.limits)
                if minute["status"] == "forming_or_future":
                    continue
                mark = {"status": minute["status"]}
                if "close" in minute:
                    mark.update(kind="completed_minute_close", price=minute["close"], source_as_of=minute["end"],
                                received_at=checked_at.isoformat(), age_seconds=minute["age_seconds"])
                    if minute["status"] == "valid" and number(minute["volume"]) <= 0:
                        mark["status"] = "zero_volume"
                break
            readings[symbol]["twelve_data"] = record["symbols"][symbol]["twelve_data"] = mark
        record["reference_requests"] = requests
        self.store.put(record["session"], REFERENCE_DATASET, {"as_of": instant(self.clock()).isoformat(),
                       "requests": requests, "symbols": {symbol: items for symbol, items in readings.items() if items}})

    def serve(self, stop):
        while not stop.is_set():
            try:
                self.poll(self.clock())
            except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError, ValueError) as exc:
                LOG.warning("FUND market_source_observer_error: %s", type(exc).__name__)
            stop.wait(INTERVAL_SECONDS)


def observation_lines(root, selected_day=None, *, configured=False):
    if selected_day is not None:
        selected_day = date.fromisoformat(selected_day).isoformat()
    folder = _folder(root)
    sessions, totals, deltas, limits = defaultdict(set), defaultdict(Counter), defaultdict(list), Counter()
    during_yahoo_minute_errors = defaultdict(Counter)
    actions, invalid, rows = {}, 0, 0
    output = ["行情来源观察（只读）｜Yahoo、Alpaca IEX 与已配置的独立参考"]
    reference_requests = defaultdict(Counter)
    for path in sorted(folder.glob(f"{selected_day or '????-??-??'}.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    phase, session = record["phase"], record["session"]
                    if phase not in PHASE_NAMES:
                        raise ValueError("Unknown session phase")
                    failed_minutes = {failure["symbol"] for failure in record["yahoo_failures"]
                                      if failure.get("interval") == "1m"}
                    for symbol, feeds in record["symbols"].items():
                        for feed in ("alpaca", "yahoo"):
                            for kind in ("quote", "minute"):
                                totals[symbol, phase, feed, kind][feeds[feed][kind]["status"]] += 1
                        for feed, kind in (("schwab", "quote"), ("twelve_data", "minute")):
                            if feed in feeds:
                                totals[symbol, phase, feed, kind][feeds[feed]["status"]] += 1
                        if symbol in failed_minutes:
                            for kind in ("quote", "minute"):
                                during_yahoo_minute_errors[symbol, phase, kind][feeds["alpaca"][kind]["status"]] += 1
                        for kind, difference in feeds["differences"].items():
                            deltas[symbol, phase, kind].append(difference)
                    sessions[session].add(phase)
                    for feed, request in record.get("reference_requests", {}).items():
                        reference_requests[feed][request["status"]] += 1
                    if record["alpaca_request"]["status"] == "rate_limited":
                        limits["alpaca"] += 1
                    if record.get("alpaca_closing_request", {}).get("status") == "rate_limited":
                        limits["alpaca_close"] += 1
                    if any("RateLimit" in (failure.get(key) or "")
                           for failure in record["yahoo_failures"] for key in ("error_type", "first_error_type")):
                        limits["yahoo"] += 1
                    if evidence := record.get("corporate_actions"):
                        if evidence["alpaca_request"]["status"] == "rate_limited":
                            limits["alpaca_actions"] += 1
                        for feed, side in (("alpaca", evidence.get("alpaca") or {}), ("yahoo", evidence["yahoo"])):
                            for symbol, entries in side.items():
                                events = entries if feed == "alpaca" else entries["actions"]
                                stamp = evidence["observed_at"] if feed == "alpaca" else entries["checked_at"]
                                for event in events:
                                    for kind in ("dividend", "split", "spin_off", "unverified_dividend"):
                                        if str(event.get(kind, "0")) not in {"0", "0.0"}:
                                            actions.setdefault((symbol, event["date"], kind, feed, str(event[kind])), stamp)
                    rows += 1
                except (KeyError, TypeError, ValueError):
                    invalid += 1
    if not rows:
        output.append("暂无观测。" if configured else "暂无观测，未检测到完整的行情对照凭据。")
        return output
    full = sum(set(PHASES[:3]) <= stages for stages in sessions.values())
    output.append(f"已记录 {len(sessions)} 个纽约交易日、{rows} 轮。有开盘、盘中、收盘前样本的交易日 {full} 个。")
    output.append(f"限流记录 Alpaca 快照 {limits['alpaca']} 次、收盘分钟线 {limits['alpaca_close']} 次、"
                  f"公司行动 {limits['alpaca_actions']} 次，"
                  f"Yahoo 有限流证据 {limits['yahoo']} 轮。")
    if invalid:
        output.append(f"有 {invalid} 条记录无法读取。")
    for provider, counts in sorted(reference_requests.items()):
        output.append(f"  {provider} 请求 " + "，".join(f"{state} {count}" for state, count in sorted(counts.items())))
    for (symbol, phase, feed, kind), counts in sorted(totals.items()):
        n = sum(counts.values())
        output.append(f"  {symbol} {PHASE_NAMES[phase]} {feed} {'报价' if kind == 'quote' else '分钟线'} "
                      f"有效 {counts['valid']}/{n} ({counts['valid'] / n:.1%})，未观测 {counts['not_observed']}/{n}")
    for (symbol, phase, kind), counts in sorted(during_yahoo_minute_errors.items()):
        output.append(f"  {symbol} {PHASE_NAMES[phase]} Yahoo 分钟线报错期间，"
                      f"Alpaca {'报价' if kind == 'quote' else '分钟线'}有效 {counts['valid']}/{sum(counts.values())}")
    for (symbol, phase, kind), values in sorted(deltas.items()):
        if kind == "schwab_iex_midpoint_skew_seconds":
            output.append(f"  {symbol} {PHASE_NAMES[phase]} 买卖中点源时间差（Schwab 减 Alpaca IEX）"
                          f" {min(values):+.1f} 至 {max(values):+.1f} 秒，配对 {len(values)} 次")
        elif kind == "schwab_iex_midpoint_bps":
            output.append(f"  {symbol} {PHASE_NAMES[phase]} 买卖中点差（Schwab／Alpaca IEX，源时间差 ≤ 5 秒）"
                          f" {min(values):+.2f} 至 {max(values):+.2f} 基点，配对 {len(values)} 次")
        elif kind == "last_trade_skew_seconds":
            output.append(f"  {symbol} {PHASE_NAMES[phase]} 最新成交源时间差（Alpaca 减 Yahoo）"
                          f" {min(values):+.1f} 至 {max(values):+.1f} 秒，配对 {len(values)} 次")
        elif kind.endswith("_bps"):
            label = "同一分钟收盘差" if kind == "same_minute_close_bps" else "最新成交差"
            output.append(f"  {symbol} {PHASE_NAMES[phase]} {label}（Alpaca／Yahoo）"
                          f" {min(values):+.2f} 至 {max(values):+.2f} 基点，配对 {len(values)} 次")
    for (symbol, day, kind, feed, amount), stamp in sorted(actions.items()):
        output.append(f"  {symbol} {day} {kind} {feed} {amount}，首次留证 {stamp}")
    output.append("Yahoo 未观测表示该轮没有相应的交易进程观测。原始样本保留源时间和观察时间。")
    return output
