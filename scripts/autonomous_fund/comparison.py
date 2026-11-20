"""Declared rule references and independently financed close-price shadow books.

Offline diagnostic scenarios only. They never submit orders or select the trading
arm. Raw valuation receipts determine share quantities; adjusted research closes
must never be substituted for historical executable share prices.
"""
import hashlib
import json
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR

from .risk import RiskError, instant, number
from .scorecard import _idea_session_close

VERSION = "independent_close_books_v2"
ZERO, ONE = Decimal(0), Decimal(1)
UNMODELED = ["intraday_limits_stops_and_queue", "live_quote_liquidity_and_market_impact",
             "quantitative_vol_es_style_and_stress_guards", "exception_approval",
             "cash_sweep_and_cash_interest", "unrecorded_corporate_actions", "live_rebalance_band",
             "decision_expiry_and_review_schedule"]


def declaration(limits, quant, universe, definitions, *, cash_symbol=None, fee_rate=None, slippage=None):
    """Deterministic pre-declaration; metadata only, without changing account/policy identity.

    Existing operator-defined references are projected into the static envelope;
    residual capital stays in USD. No substitute securities or winning historical
    baskets are invented. Missing costs remain unknown rather than free.
    """
    payload = {"version": VERSION, "limits": limits, "quant_risk": quant, "groups": quant.get("groups", []),
               "min_trade_notional": quant.get("min_trade_notional", "0"),
               "max_leveraged_position": quant.get("max_leveraged_position", "0"),
               "max_leveraged_total": quant.get("max_leveraged_total", "0"),
               "universe": dict(universe), "references": definitions, "cash_symbol": cash_symbol,
               "fee_rate": str(fee_rate) if fee_rate is not None else None,
               "slippage": str(slippage) if slippage is not None else None,
               "dividend_convention": "recorded_ex_date_immediate_credit_scenario",
               "unmodeled": UNMODELED}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return {**payload, "id": hashlib.sha256(canonical.encode()).hexdigest()[:16]}


def targets(body):
    result = {}
    for row in body.get("targets", []):
        symbol, weight = row["symbol"], number(row["weight"])
        if symbol in result or not ZERO <= weight <= ONE:
            raise RiskError("Invalid comparison target")
        result[symbol] = weight
    if sum(result.values(), ZERO) > ONE:
        raise RiskError("Comparison targets exceed capital")
    return result


def project(weights, policy):
    """Only reduce weights, preserving every already-satisfied static cap."""
    limits, allowed = policy["limits"], policy["universe"]
    name_cap, total_cap = number(limits["max_position"]), number(limits["max_exposure"])
    if not ZERO < name_cap <= ONE or not ZERO < total_cap <= ONE:
        raise RiskError("Invalid comparison limits")
    clean = {}
    for symbol, raw in weights.items():
        value = number(raw)
        if not ZERO <= value <= ONE:
            raise RiskError("Invalid rule weight")
        if symbol not in allowed or symbol == policy.get("cash_symbol") or allowed[symbol] == "cash_equivalent":
            continue
        cap = min(name_cap, number(policy["max_leveraged_position"])) if allowed[symbol] == "leveraged_etf" else name_cap
        clean[symbol] = min(value, cap)

    def scale(members, cap):
        total = sum((clean.get(s, ZERO) for s in members), ZERO)
        if total > cap:
            for symbol in members:
                if symbol in clean:
                    clean[symbol] *= cap / total

    scale(clean, total_cap)
    for _, cap, members in policy.get("groups", []):
        scale(members, number(cap))
    scale([s for s in clean if allowed[s] == "leveraged_etf"], number(policy["max_leveraged_total"]))
    return {s: w for s, w in clean.items() if w > 0}


class Book:
    """One independent USD cash/whole-share ledger; no real account mutations."""

    def __init__(self, capital, policy):
        self.capital, self.policy = number(capital), policy
        if self.capital <= 0:
            raise RiskError("Comparison capital must be positive")
        self.cash, self.positions, self.peak = self.capital, {}, self.capital
        self.averages = {}
        self.fees, self.friction, self.turnover = ZERO, ZERO, ZERO
        self.orders, self.paused, self.observations = 0, False, 0
        self.last, self.error = None, None
        self.series, self.overlays = [], []
        self.model_cost, self.cost_unknown = ZERO, False

    def charge(self, amount):
        # Model bills accrue outside the broker, as in the real fund scoreboard.
        # They reduce economic NAV, never invent a broker cash withdrawal.
        if amount is None:
            self.cost_unknown = True
        else:
            self.model_cost += number(amount)

    def value(self, prices):
        return self.cash + sum((q * prices[s] for s, q in self.positions.items()), ZERO)

    def _save_step_state(self):
        # Attempts append to the histories. Copy only the mutable working book.
        state = {key: deepcopy(value) for key, value in vars(self).items() if key not in {"policy", "series", "overlays"}}
        return state, len(self.series), len(self.overlays)

    def _restore_step_state(self, saved):
        state, observations, overlays = saved
        series, marks, policy = self.series, self.overlays, self.policy
        del series[observations:]
        del marks[overlays:]
        vars(self).clear()
        vars(self).update(state, policy=policy, series=series, overlays=marks)

    def _retained_positions(self, desired, marks, before, budget):
        held = {s: q for s, q in self.positions.items() if s in desired}
        weights = {s: q * marks[s] / before for s, q in held.items()}
        if len(held) == len(self.positions) and project(weights, self.policy) == weights:
            return held
        # Exits and cap trims still trade. Reserve their friction before checking
        # the surviving positions against the caps.
        weights = {s: q * marks[s] / budget for s, q in held.items()}
        capped = project({s: min(w, ONE) for s, w in weights.items()}, self.policy)
        return {s: held[s] if w == weights[s] else int((budget * w / marks[s]).to_integral_value(rounding=ROUND_FLOOR))
                for s, w in capped.items()}

    def step(self, day, desired, prices, actions=(), risk=None, *, hold_positions=False):
        if self.error:
            return
        # A missing action receipt or cash shortfall must not leave half a virtual
        # rebalance in the evidence. Real trading state is never touched here.
        previous = self._save_step_state()
        try:
            desired = project(desired, self.policy)
            required = set(self.positions) | {s for s, w in desired.items() if w > 0}
            if required - prices.keys():
                raise RiskError("Missing raw comparison close: " + ", ".join(sorted(required - prices.keys())))
            marks = {s: number(prices[s]) for s in required}
            if any(v <= 0 for v in marks.values()):
                raise RiskError("Nonpositive comparison mark")
            for row in sorted(actions, key=lambda r: (r["date"], not bool(r.get("split")))):
                symbol = row["symbol"]
                if row["date"] != day or symbol not in self.positions:
                    continue
                if row.get("conflicting"):
                    raise RiskError("Conflicting comparison corporate actions")
                ratio, dividend = number(row.get("split", 1)), number(row.get("dividend", 0))
                if ratio <= 0 or dividend < 0:
                    raise RiskError("Invalid comparison corporate action")
                shares = self.positions[symbol] * ratio
                if shares != shares.to_integral_value():
                    raise RiskError("Fractional corporate action needs cash-in-lieu evidence")
                self.positions[symbol] = int(shares)
                if symbol in self.averages:
                    self.averages[symbol] /= ratio
                self.cash += shares * dividend
            before = self.value(marks)
            self.peak = max(self.peak, before)
            self.paused |= ONE - before / self.peak >= number(self.policy["limits"]["drawdown"])
            if risk is not None:
                current = {s: q * marks[s] / before for s, q in self.positions.items()}
                desired, overlay = risk(day, desired, current, before, self.peak,
                                        number(self.last["equity"]) if self.last else self.capital, self.paused)
                self.overlays.append({"day": day, **overlay})
            fee, slip = self.policy.get("fee_rate"), self.policy.get("slippage")
            if fee is None or slip is None:
                raise RiskError("Comparison execution costs unavailable")
            fee, slip = number(fee), number(slip)
            # Local matching admits 1% slippage plus half a 1% quote spread.
            if not ZERO <= fee <= Decimal("0.01") or not ZERO <= slip <= Decimal("0.015"):
                raise RiskError("Invalid comparison execution costs")
            # Worst-case two-sided turnover reserves friction before sizing. This
            # prevents fees from pushing rounded positions above post-fee caps.
            budget = before * (ONE - 2 * (fee + slip + fee * slip))
            if hold_positions and not self.paused and risk is None:
                planned = self._retained_positions(desired, marks, before, budget)
            else:
                planned = {s: int((budget * w / marks[s]).to_integral_value(rounding=ROUND_FLOOR))
                           for s, w in desired.items()}
            if self.paused:
                planned = {s: min(q, self.positions.get(s, 0)) for s, q in planned.items()}
            for symbol in planned:
                increase = planned[symbol] - self.positions.get(symbol, 0)
                if increase > 0 and increase * marks[symbol] < number(self.policy["min_trade_notional"]):
                    planned[symbol] = self.positions.get(symbol, 0)
            changes = {s: planned.get(s, 0) - self.positions.get(s, 0) for s in set(planned) | set(self.positions)}
            if sum(bool(q) for q in changes.values()) > int(self.policy["limits"]["daily_orders"]):
                raise RiskError("Comparison daily order allowance exceeded")
            trades = []
            for symbol, delta in sorted(changes.items(), key=lambda row: (row[1] > 0, row[0])):
                if not delta:
                    continue
                fill = marks[symbol] * (ONE + slip if delta > 0 else ONE - slip)
                notional, charge = abs(delta) * fill, abs(delta) * fill * fee
                cost = delta * fill + charge
                if cost > self.cash:
                    raise RiskError("Comparison cash unavailable")
                self.cash -= cost
                held = self.positions.get(symbol, 0)
                if delta > 0:
                    self.averages[symbol] = (held * self.averages.get(symbol, marks[symbol]) + delta * fill) / (held + delta)
                self.positions[symbol] = self.positions.get(symbol, 0) + delta
                self.fees += charge
                self.friction += abs(delta) * abs(fill - marks[symbol])
                self.turnover += notional
                self.orders += 1
                trades.append({"symbol": symbol, "side": "buy" if delta > 0 else "sell", "quantity": abs(delta),
                               "price": str(fill), "trade_cost": str(charge), "day": day, "scope": "virtual_close_scenario"})
            self.positions = {s: q for s, q in self.positions.items() if q}
            self.averages = {s: p for s, p in self.averages.items() if s in self.positions}
            if risk is not None and hasattr(risk, "verify"):
                value = self.value(marks)
                risk.verify({s: q * marks[s] / value for s, q in self.positions.items()})
            self.last = {"day": day, "equity": str(self.value(marks)), "cash": str(self.cash),
                         "position_average_prices": {s: str(v) for s, v in self.averages.items()},
                         "position_prices": {s: str(marks[s]) for s in self.positions}, "trades": trades,
                         "positions": dict(self.positions), "risk_increase_paused": self.paused}
            self.observations += 1
            self.series.append({**self.last, "net_economic_equity":
                str(self.value(marks) - self.model_cost) if not self.cost_unknown else None})
        except (RiskError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
            self._restore_step_state(previous)
            self.error = {"day": day, "reason": str(exc) if isinstance(exc, RiskError) else "missing_or_invalid_comparison_input"}

    def summary(self):
        return {"status": "incomplete" if self.error else "observed" if self.observations > 1 else "warming_up",
                "return_after_trading_costs": float(number(self.last["equity"]) / self.capital - 1)
                    if not self.error and self.observations > 1 else None,
                "capital": str(self.capital), "fees": str(self.fees), "friction": str(self.friction),
                "peak_equity": str(self.peak),
                "turnover": str(self.turnover), "orders": self.orders, "observations": self.observations,
                "last": self.last, "unavailable": self.error}


def _raw_marks(marks, accounts):
    result = {s: dict(rows) for s, rows in marks.items()}
    for day, body in accounts.items():
        for symbol, value in (body.get("position_prices") or {}).items():
            try:
                if number(value) > 0:
                    result.setdefault(symbol, {})[day] = value
            except (RiskError, ValueError, TypeError):
                continue
    return result


def _actions(events):
    """Identical receipts deduplicate; contradictory amounts do not create income."""
    rows = {}
    for _, _, kind, body in events:
        if kind not in {"benchmark_action", "local_corporate_action", "comparison_corporate_action"}:
            continue
        key = body["symbol"], body["date"]
        row = rows.setdefault(key, {"symbol": key[0], "date": key[1]})
        for field in ("split", "dividend"):
            if field in body:
                if number(body[field]) == 0:
                    continue  # Native action packets also contain the other action's absent (zero) field.
                if field in row and number(row[field]) != number(body[field]):
                    row["conflicting"] = True
                row[field] = body[field]
    return list(rows.values())


def evaluate(events, marks, accounts, sessions, *, calls=None):
    """Evaluate only forward-declared epochs; historical profiles are never invented.

    events: sequence-ordered (sequence, at, kind, body) visible at report cutoff.
    Both shadow arms start with equal USD capital and retain separate shares/cash.
    Shared research and daily-close execution remain explicit limitations.
    """
    prices = _raw_marks(marks, accounts)
    protocols = [(seq, instant(at), b["comparison_protocol"]) for seq, at, kind, b in events
                 if kind == "startup" and isinstance(b.get("comparison_protocol"), dict)]
    if not protocols:
        return {"status": "untracked", "reason": "no_forward_comparison_declaration", "references": [], "shadow": []}
    epochs = []
    for seq, at, policy in protocols:
        if not epochs or epochs[-1]["policy"]["id"] != policy["id"]:
            if epochs:
                epochs[-1]["until"] = at
            epochs.append({"sequence": seq, "at": at, "until": None, "policy": policy})
    actions = _actions(events)
    references, shadows = [], []
    for epoch in epochs:
        policy, formed, until = epoch["policy"], epoch["at"], epoch["until"]
        days = [d for d in sessions if formed < _idea_session_close(d) and (until is None or _idea_session_close(d) < until)]
        origin = next((d for d in days if d in accounts and number(accounts[d]["equity"]) > 0), None)
        if origin is None:
            continue
        days = [d for d in days if d >= origin]
        capital = accounts[origin]["equity"]
        from .learning import risk_overlay, pair_costs, managed_summary
        overlay = risk_overlay(events, policy)
        for definition in policy["references"]:
            weights = project(definition["weights"], policy)
            book = Book(capital, policy)
            managed_reference = Book(capital, policy)
            for day in days:
                book.step(day, weights, {s: p[day] for s, p in prices.items() if day in p}, actions)
                managed_reference.step(day, weights, {s: p[day] for s, p in prices.items() if day in p}, actions, risk=overlay)
            references.append({"protocol_id": policy["id"], "declared_at": formed.isoformat(),
                "name": definition["name"], "source_reference": definition["id"], "start_day": origin,
                "weights_after_static_caps": {s: str(w) for s, w in weights.items()},
                "risk_managed": managed_summary(managed_reference), **book.summary()})
        pairs = [(instant(at), b) for seq, at, kind, b in events if kind == "shadow_decision" and b.get("valid")
                 and seq > epoch["sequence"] and instant(at) >= formed and (until is None or instant(at) < until)
                 and b.get("comparison_protocol_id") == policy["id"]]
        # A changed model/prompt defines another contiguous experiment epoch.
        groups = []
        for at, body in pairs:
            version = body.get("comparison_prompt_version")
            signature = version, body.get("comparison_generation")
            if not version or body.get("comparison_equity") is None:
                continue
            if not groups or groups[-1]["signature"] != signature:
                if groups:
                    groups[-1]["until"] = at
                groups.append({"version": version, "signature": signature, "rows": [], "until": until})
            groups[-1]["rows"].append((at, body))
        for group in groups:
            available = group["rows"][0][0]
            valid_days = [d for d in days if available < _idea_session_close(d)
                          and (group["until"] is None or _idea_session_close(d) < group["until"])]
            books = {arm: Book(group["rows"][0][1]["comparison_equity"], policy) for arm in ("memo", "baseline")}
            managed = {arm: Book(group["rows"][0][1]["comparison_equity"], policy) for arm in books}
            charged, independent = set(), True
            cursor, current = 0, None
            for day in valid_days:
                while cursor < len(group["rows"]) and group["rows"][cursor][0] < _idea_session_close(day):
                    current = group["rows"][cursor][1]
                    independent &= current.get("comparison_account_mode") == "independent_prior_close_v1"
                    charges = pair_costs(current, calls or {}, charged)
                    for arm in managed:
                        managed[arm].charge(charges[arm])
                    cursor += 1
                if current is None:
                    continue
                arms = {current["arm"]: current["decision"], current["trading_arm"]: current["trading_decision"]}
                if set(arms) != set(books):
                    raise RiskError("Invalid comparison arms")
                for arm, book in books.items():
                    book.step(day, targets(arms[arm]), {s: p[day] for s, p in prices.items() if day in p}, actions)
                    managed[arm].step(day, targets(arms[arm]), {s: p[day] for s, p in prices.items() if day in p},
                                      actions, risk=overlay)
            rows = {arm: book.summary() for arm, book in books.items()}
            returns = [rows[a]["return_after_trading_costs"] for a in ("memo", "baseline")]
            failed_attempts = sum(1 for _, at, kind, b in events if kind == "shadow_decision" and not b.get("valid")
                and b.get("comparison_protocol_id") == policy["id"] and b.get("comparison_prompt_version") == group["version"]
                and b.get("comparison_generation") == group["signature"][1] and instant(at) >= formed
                and (until is None or instant(at) < until))
            managed_rows = {a: managed_summary(b) for a, b in managed.items()}
            if failed_attempts:
                # The fund ledger keeps those paid calls. Until their failed
                # unpaired path can be attributed, do not call a successful-only
                # cost subset complete or allow it to elect a trading arm.
                for row in managed_rows.values():
                    row.update(costs_complete=False, net_return_after_linked_costs=None,
                               cost_coverage_reason="failed_unpaired_shadow_attempts")
            shadows.append({"protocol_id": policy["id"], "prompt_version": group["version"],
                            "generation": group["signature"][1],
                            "declared_at": formed.isoformat(), "latest_decisions": arms if current else {},
                            "available_at": available.isoformat(), "decisions": len(group["rows"]), "arms": rows,
                            "independent_context": independent,
                            "risk_managed": managed_rows, "failed_pair_attempts": failed_attempts,
                            "memo_minus_baseline": returns[0] - returns[1] if all(r is not None for r in returns) else None})
    return {"status": "computed", "references": references, "shadow": shadows,
            "active_epoch_at": epochs[-1]["at"].isoformat(),
            "cohort_starts": [{"at": at, **b} for _, at, kind, b in events if kind == "comparison_cohort_started"],
            "unmodeled": UNMODELED,
            "meaning": "Forward-declared, independent USD/whole-share price-scenario books with recorded cost assumptions. "
                       "Static envelope only, not a certification of all live quantitative/execution constraints. "
                       "Shared research, changing memos and sampled shadow decisions prevent causal learning claims. "
                       "No actual fund flows enter either book. risk_managed adds frozen native risk construction "
                       "and exactly linked manager/correction/half fresh-critic costs; other operating costs remain excluded. "
                       "Recorded dividends/splits only; USD cash earns zero. Static books never qualify a switch. "
                       "Only the current complete risk-managed cohort can qualify under the existing sample/t-stat thresholds."}


def lines(report):
    if report.get("status") != "computed":
        return ["独立组合情景：等待启动时的前向协议记录；旧记录不倒推规则或费用。"]
    pct = lambda v: "未知" if v is None else f"{v * 100:.3f}%"
    out = ["独立组合情景：整股、独立现金和交易成本；只检验静态仓位包络，不代表通过全部量化与真实执行约束。"]
    for row in report["references"][-6:]:
        out.append(f"规则基准 {row['name']}（{row['protocol_id']}）：{pct(row['return_after_trading_costs'])}，"
                   f"手续费 ${number(row['fees']):.2f}，状态 {row['status']}；目标 {row['weights_after_static_caps']}")
        if row.get("unavailable"):
            out.append(f"  缺项：{row['unavailable']['day']} {row['unavailable']['reason']}")
        if row.get("risk_managed"):
            book = row["risk_managed"]
            out.append(f"  同规则动态风控回放：{pct(book['return_after_trading_costs'])}，状态 {book['status']}。")
    for row in report["shadow"][-3:]:
        out.append(f"影子独立账本（{row['prompt_version']}）：memo {pct(row['arms']['memo']['return_after_trading_costs'])}，"
                   f"baseline {pct(row['arms']['baseline']['return_after_trading_costs'])}，差额 {pct(row['memo_minus_baseline'])}。")
        for arm, book in row.get("risk_managed", {}).items():
            out.append(f"  {arm} 动态风控回放：含关联调用费用收益 {pct(book['net_return_after_linked_costs'])}，"
                       f"费用完整 {book['costs_complete']}，状态 {book['status']}。")
        for arm, book in row["arms"].items():
            if book.get("unavailable"):
                out.append(f"  {arm} 缺项：{book['unavailable']['day']} {book['unavailable']['reason']}")
    if not report["references"] and not report["shadow"]:
        out.append("协议已记录，等待其后的有效收盘证据；影子对照还需有效的配对决策。")
    out.append("日线收盘情景，共享研究；现金收益、完整运营费、未记录公司行动及盘中限价撮合未还原。"
               "静态情景不参与切换；仅当前版本、关联费用和风险证据完整的动态回放可积累切换样本。")
    return out
