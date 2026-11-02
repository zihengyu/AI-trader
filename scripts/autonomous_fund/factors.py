"""Factor lab: the AI proposes factor expressions; deterministic code tests them.

The manager writes candidate factors in a small expression language over the
universe's completed daily closes and volumes (for example
``rank(ret(126) - ret(21))``). Nothing is executed: an expression is parsed into
a whitelisted syntax tree and evaluated with pandas. Every operator only looks
backwards, so a factor value on date t uses data through the close of t, and it
is scored against the return from the close of t to the close of t+h.

Evidence standard, so that "mining" does not become overfitting:
- cross-sectional rank IC (Spearman) on non-overlapping dates, one every h sessions;
- the in-sample t-statistic must clear a Bonferroni bar that rises with every
  distinct expression ever tried in this account;
- the later holdout must agree in sign with t >= holdout_min_t;
- after validation, the IC on dates after the validation day is tracked as the
  true out-of-sample record, and a factor whose live IC turns against its
  direction is retired.
Factors never place orders. They are evidence for the AI's own decisions; the
quantitative risk engine and the approval policy are unchanged.
"""
import ast
import hashlib
import json
import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
import pandas as pd

from .risk import RiskError

MAX_WINDOW = 504
MAX_LENGTH = 300
MAX_NODES = 60

# name: argument kinds; "x" is a sub-expression and "n" a whole-number window.
FUNCTIONS = {
    "ret": ("n",), "vol": ("n",), "delay": ("x", "n"), "delta": ("x", "n"), "ts_mean": ("x", "n"),
    "ts_std": ("x", "n"), "ts_max": ("x", "n"), "ts_min": ("x", "n"), "ts_rank": ("x", "n"),
    "log": ("x",), "abs": ("x",), "sign": ("x",), "rank": ("x",), "zscore": ("x",), "demean": ("x",),
}
MIN_WINDOW = {"vol": 2, "ts_std": 2, "ts_rank": 2}
DATA = ("close", "volume")

GRAMMAR = {
    "data": {"close": "completed daily close", "volume": "completed daily share volume"},
    "time_series": {
        "ret(n)": "close / close n sessions earlier - 1", "vol(n)": "annualized std of the last n daily returns",
        "delay(x, n)": "x from n sessions earlier", "delta(x, n)": "x - delay(x, n)",
        "ts_mean(x, n) / ts_std / ts_max / ts_min": "trailing n-session statistic of x",
        "ts_rank(x, n)": "percentile of today's x within its last n values",
        "log(x) / abs(x) / sign(x)": "elementwise"},
    "cross_section": {"rank(x)": "percentile rank across the universe each day", "zscore(x)": "daily z-score",
                      "demean(x)": "minus the daily universe mean"},
    "operators": "+ - * / and unary -, numeric constants",
    "limits": f"windows 1..{MAX_WINDOW} sessions; at most {MAX_LENGTH} characters and {MAX_NODES} syntax nodes; "
              "everything looks backwards only",
    "examples": ["rank(ret(126) - ret(21))", "-vol(60)", "rank(close / ts_mean(close, 200) - 1)",
                 "-rank(ret(5))", "rank(ts_mean(volume, 5) / ts_mean(volume, 60))"],
}


def parse(expression):
    """Validated syntax tree, or RiskError. Never evaluates Python."""
    text = str(expression or "").strip()
    if not text or len(text) > MAX_LENGTH:
        raise RiskError(f"Factor expression must be 1..{MAX_LENGTH} characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise RiskError("Factor expression is not valid syntax") from None
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise RiskError(f"Factor expression exceeds {MAX_NODES} syntax nodes")
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Constant, ast.Load,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd)
    for node in nodes:
        if not isinstance(node, allowed):
            raise RiskError(f"Factor expressions do not allow {type(node).__name__}")
    _check(tree.body)
    if not any(isinstance(n, ast.Name) and n.id in DATA for n in nodes) and not any(
            isinstance(n, ast.Call) and n.func.id in {"ret", "vol"} for n in nodes):
        raise RiskError("A factor must depend on market data")
    return tree.body


def _check(node):
    if isinstance(node, ast.Name):
        if node.id not in DATA:
            raise RiskError(f"Unknown factor input {node.id}; use close or volume")
    elif isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)) or not math.isfinite(node.value):
            raise RiskError("Factor constants must be finite numbers")
    elif isinstance(node, ast.UnaryOp):
        _check(node.operand)
    elif isinstance(node, ast.BinOp):
        _check(node.left)
        _check(node.right)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            raise RiskError("Unknown factor function; see the factor grammar")
        kinds = FUNCTIONS[node.func.id]
        if node.keywords or len(node.args) != len(kinds):
            raise RiskError(f"{node.func.id} takes {len(kinds)} positional argument(s)")
        for kind, arg in zip(kinds, node.args):
            if kind == "n":
                low = MIN_WINDOW.get(node.func.id, 1)
                if (not isinstance(arg, ast.Constant) or type(arg.value) is not int
                        or not low <= arg.value <= MAX_WINDOW):
                    raise RiskError(f"{node.func.id} window must be a whole number {low}..{MAX_WINDOW}")
            else:
                _check(arg)
    else:
        raise RiskError(f"Factor expressions do not allow {type(node).__name__}")


def canonical(expression):
    return ast.unparse(parse(expression))


def compute(expression, close, volume=None):
    """Factor panel (dates x symbols) from completed daily data; backwards-looking only."""
    data = {"close": close, "volume": volume if volume is not None else close * np.nan}
    value = _eval(parse(expression), data)
    if not isinstance(value, pd.DataFrame):
        raise RiskError("A factor must depend on market data")
    return value.replace([np.inf, -np.inf], np.nan)


def _panel(value, like):
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame(float(value), index=like.index,
                                                                         columns=like.columns)


def _eval(node, data):
    close = data["close"]
    if isinstance(node, ast.Name):
        return data[node.id]
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, data)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, data), _eval(node.right, data)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(right, pd.DataFrame):
            right = right.where(right != 0)
        elif right == 0:
            return _panel(np.nan, close)
        return left / right
    name, args = node.func.id, node.args
    if name == "ret":
        return close / close.shift(args[0].value) - 1
    if name == "vol":
        return close.pct_change(fill_method=None).rolling(args[0].value).std() * math.sqrt(252)
    x = _panel(_eval(args[0], data), close)
    if name in {"delay", "delta", "ts_mean", "ts_std", "ts_max", "ts_min", "ts_rank"}:
        n = args[1].value
        if name == "delay":
            return x.shift(n)
        if name == "delta":
            return x - x.shift(n)
        window = x.rolling(n, min_periods=n)
        if name == "ts_rank":
            return window.rank(pct=True)
        return getattr(window, name[3:])()
    if name == "log":
        return np.log(x.where(x > 0))
    if name == "abs":
        return x.abs()
    if name == "sign":
        return np.sign(x)
    if name == "rank":
        return x.rank(axis=1, pct=True)
    mean = x.mean(axis=1)
    if name == "demean":
        return x.sub(mean, axis=0)
    std = x.std(axis=1)
    return x.sub(mean, axis=0).div(std.where(std > 0), axis=0)


# ----- Objective evaluation ----------------------------------------------------------------

def daily_ic(factor, close, horizon, min_names):
    """Per-date cross-sectional rank IC against the close-to-close return over `horizon` sessions."""
    forward = close.shift(-horizon) / close - 1
    both = factor.notna() & forward.notna()
    f, r = factor.where(both), forward.where(both)
    count = both.sum(axis=1)
    fr, rr = f.rank(axis=1), r.rank(axis=1)
    a, b = fr.sub(fr.mean(axis=1), axis=0), rr.sub(rr.mean(axis=1), axis=0)
    denominator = np.sqrt((a * a).sum(axis=1) * (b * b).sum(axis=1))
    ic = (a * b).sum(axis=1) / denominator.where(denominator > 0)
    pct = f.rank(axis=1, pct=True)
    spread = r.where(pct >= 0.8).mean(axis=1) - r.where(pct <= 0.2).mean(axis=1)
    frame = pd.DataFrame({"ic": ic, "spread": spread, "names": count})
    return frame[(count >= min_names) & ic.notna()]


def summarize(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0, "mean_ic": None, "t": None, "hit_rate": None, "mean_spread": None}
    ic = rows["ic"].astype(float)
    std = float(ic.std(ddof=1)) if n > 1 else 0.0
    mean = float(ic.mean())
    return {"n": n, "mean_ic": round(mean, 4), "ic_std": round(std, 4) if n > 1 else None,
            "t": round(mean / std * math.sqrt(n), 2) if std > 0 else None,
            "hit_rate": round(float((ic > 0).mean()), 3),
            "mean_spread": round(float(rows["spread"].mean()), 5) if rows["spread"].notna().any() else None}


def critical_t(trials, significance):
    """Bonferroni two-sided bar: every distinct expression tried raises it."""
    return round(NormalDist().inv_cdf(1 - significance / (2 * max(1, trials))), 2)


@dataclass(frozen=True)
class FactorSettings:
    enabled: bool = True
    history_days: int = 504
    holdout_fraction: float = 0.3
    min_names: int = 8
    min_observations: int = 20
    min_holdout_observations: int = 8
    significance: float = 0.05
    holdout_min_t: float = 1.0
    live_min_observations: int = 10
    max_proposals_per_review: int = 5
    max_active: int = 8

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise RiskError("factor_lab.enabled must be true/false")
        for key, low, high in (("history_days", 120, 2520), ("min_names", 3, 500), ("min_observations", 5, 500),
                               ("min_holdout_observations", 3, 500), ("live_min_observations", 3, 500),
                               ("max_proposals_per_review", 0, 10), ("max_active", 1, 20)):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise RiskError(f"factor_lab.{key} must be {low}..{high}")
        for key, low, high in (("holdout_fraction", 0.1, 0.5), ("significance", 0.001, 0.2),
                               ("holdout_min_t", 0.0, 5.0)):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise RiskError(f"factor_lab.{key} must be {low}..{high}")

    @classmethod
    def from_config(cls, section):
        section = dict(section or {})
        unknown = set(section) - set(cls.__dataclass_fields__)
        if unknown:
            raise RiskError("Unknown factor_lab settings: " + ", ".join(sorted(unknown)))
        return cls(**section)


def evaluate(expression, horizon, close, volume, settings, trials):
    """In-sample and holdout evidence with a verdict. close/volume end at the last completed session."""
    if type(horizon) is not int or not 1 <= horizon <= 20:
        raise RiskError("Factor horizon must be 1..20 sessions")
    factor = compute(expression, close, volume)
    rows = daily_ic(factor, close, horizon, settings.min_names)
    sampled = rows.iloc[::horizon]  # Non-overlapping forward windows: honest t-statistics.
    split = int(round(len(sampled) * (1 - settings.holdout_fraction)))
    inside, holdout = sampled.iloc[:split], sampled.iloc[split:]
    result = {"in_sample": summarize(inside), "holdout": summarize(holdout), "trials": trials,
              "critical_t": critical_t(trials, settings.significance),
              "first_date": str(sampled.index[0].date()) if len(sampled) else None,
              "last_date": str(sampled.index[-1].date()) if len(sampled) else None}
    ins, out = result["in_sample"], result["holdout"]
    if ins["n"] < settings.min_observations or out["n"] < settings.min_holdout_observations:
        return {**result, "status": "insufficient_data", "direction": None,
                "reason": f"needs {settings.min_observations} in-sample and {settings.min_holdout_observations} "
                          f"holdout observations with at least {settings.min_names} names"}
    direction = 1 if ins["mean_ic"] > 0 else -1
    if ins["t"] is None or abs(ins["t"]) < result["critical_t"]:
        return {**result, "status": "rejected", "direction": direction,
                "reason": f"in-sample |t| below the trials-adjusted bar {result['critical_t']}"}
    if out["t"] is None or out["mean_ic"] * direction <= 0 or out["t"] * direction < settings.holdout_min_t:
        return {**result, "status": "rejected", "direction": direction,
                "reason": f"holdout does not confirm the in-sample sign with t >= {settings.holdout_min_t}"}
    return {**result, "status": "validated", "direction": direction, "reason": "in-sample and holdout agree"}


def live_record(expression, horizon, direction, since, close, volume, settings):
    """IC on dates after the validation day: the factor's genuine out-of-sample track record."""
    factor = compute(expression, close, volume)
    rows = daily_ic(factor, close, horizon, settings.min_names)
    live = rows[rows.index.date > pd.Timestamp(since).date()].iloc[::horizon]
    record = summarize(live)
    # Today's ranks come from the latest completed close, which has no forward return yet.
    latest = factor.iloc[-1].dropna().rank(pct=True) if len(factor) else pd.Series(dtype=float)
    if direction == -1:
        latest = 1 - latest
    ordered = latest.sort_values(ascending=False)
    record["favored"] = [{"symbol": s, "percentile": round(float(p), 2)} for s, p in ordered.head(5).items()]
    record["disfavored"] = [{"symbol": s, "percentile": round(float(p), 2)} for s, p in ordered.tail(5).items()]
    record["as_of_close"] = str(close.index[-1].date()) if len(close) else None
    return record


# ----- Persistent registry -------------------------------------------------------------------

class FactorLab:
    def __init__(self, journal, settings):
        self.journal, self.settings = journal, settings
        with journal.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS fund_factors (
                id TEXT PRIMARY KEY, expression TEXT NOT NULL, horizon INTEGER NOT NULL, name TEXT NOT NULL,
                hypothesis TEXT NOT NULL, proposed_at TEXT NOT NULL, session_date TEXT, status TEXT NOT NULL,
                direction INTEGER, evidence TEXT NOT NULL, validated_at TEXT, retired_at TEXT, reason TEXT)""")

    @staticmethod
    def key(expression, horizon):
        return hashlib.sha256(f"{canonical(expression)}|{horizon}".encode()).hexdigest()[:16]

    def rows(self, status=None):
        with self.journal.connection() as db:
            query = "SELECT * FROM fund_factors" + (" WHERE status=?" if status else "") + " ORDER BY proposed_at"
            return [dict(r) for r in db.execute(query, (status,) if status else ())]

    def trials(self):
        with self.journal.connection() as db:
            return db.execute("SELECT count(*) FROM fund_factors").fetchone()[0]

    def test(self, proposals, now, session_date, close, volume):
        """Evaluate the review's proposals; a repeated expression is not a new trial."""
        results = []
        for proposal in list(proposals)[:self.settings.max_proposals_per_review]:
            name, horizon = proposal["name"], proposal["horizon_sessions"]
            try:
                expression = canonical(proposal["expression"])
                key = self.key(expression, horizon)
            except RiskError as exc:
                results.append({"name": name, "expression": proposal["expression"], "status": "invalid",
                                "reason": str(exc)})
                continue
            with self.journal.connection() as db:
                existing = db.execute("SELECT * FROM fund_factors WHERE id=?", (key,)).fetchone()
            if existing and existing["status"] != "insufficient_data":
                results.append({"name": existing["name"], "expression": expression, "status": existing["status"],
                                "reason": "already tested: " + (existing["reason"] or ""), "repeat": True})
                continue
            trials = self.trials() + (0 if existing else 1)
            try:
                outcome = evaluate(expression, horizon, close, volume, self.settings, trials)
            except RiskError as exc:
                outcome = {"status": "invalid", "direction": None, "reason": str(exc)}
            if outcome["status"] == "invalid":
                results.append({"name": name, "expression": expression, **outcome})
                continue
            with self.journal.connection() as db:
                db.execute("INSERT OR REPLACE INTO fund_factors VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    key, expression, horizon, name, proposal.get("hypothesis", ""), str(now), session_date,
                    outcome["status"], outcome["direction"], json.dumps(outcome, default=str),
                    session_date if outcome["status"] == "validated" else None, None, outcome["reason"]))
            results.append({"name": name, "expression": expression, "horizon_sessions": horizon, **outcome})
        if results:
            self.journal.event(now, "factor_tests", {"session_date": session_date, "results": results})
        return results

    def track(self, now, close, volume):
        """Live record of every validated factor; retire those whose live IC turned against them."""
        active = []
        for row in self.rows("validated"):
            try:
                live = live_record(row["expression"], row["horizon"], row["direction"], row["validated_at"],
                                   close, volume, self.settings)
            except RiskError as exc:
                live = {"n": 0, "unavailable": str(exc)}
            if (live.get("n", 0) >= self.settings.live_min_observations and live.get("mean_ic") is not None
                    and live["mean_ic"] * row["direction"] <= 0):
                reason = f"live mean IC {live['mean_ic']} over {live['n']} observations turned against the factor"
                with self.journal.connection() as db:
                    db.execute("UPDATE fund_factors SET status='retired', retired_at=?, reason=? WHERE id=?",
                               (str(now), reason, row["id"]))
                self.journal.event(now, "factor_retired", {"id": row["id"], "expression": row["expression"],
                                                           "reason": reason, "live": live})
                continue
            active.append({**row, "live": live})
        return active

    def context(self, now, close, volume):
        """What the AI sees: grammar, standard, validated factors with live record and current ranks."""
        if close is not None:
            active = self.track(now, close, volume)
        else:
            active = [{**r, "live": {"n": 0, "unavailable": "history"}} for r in self.rows("validated")]

        def strength(item):
            live, evidence = item["live"], json.loads(item["evidence"])
            return (live.get("t") or 0) * item["direction"], abs(evidence["in_sample"]["t"] or 0)
        active.sort(key=strength, reverse=True)
        tried = self.rows()
        return {
            "grammar": GRAMMAR,
            "evidence_standard": (
                "Cross-sectional rank IC versus the next horizon_sessions close-to-close return, on non-overlapping "
                "dates. Validated means the in-sample |t| clears a Bonferroni bar that rises with every distinct "
                "expression tried, and a later holdout agrees in sign. live is the record after validation. "
                "Direction -1 means low factor values were favored. Evidence, not orders: weigh it with research."),
            "trials": len(tried), "next_critical_t": critical_t(len(tried) + 1, self.settings.significance),
            "validated": [{"name": r["name"], "expression": r["expression"], "horizon_sessions": r["horizon"],
                           "direction": r["direction"], "validated_on": r["validated_at"],
                           "in_sample": json.loads(r["evidence"])["in_sample"],
                           "holdout": json.loads(r["evidence"])["holdout"], "live": r["live"]}
                          for r in active[:self.settings.max_active]],
            "recently_rejected": [{"name": r["name"], "expression": r["expression"], "horizon_sessions": r["horizon"],
                                   "status": r["status"], "reason": r["reason"]}
                                  for r in tried if r["status"] in {"rejected", "insufficient_data", "retired"}][-10:],
        }


def panel_from_rows(history):
    """{symbol: [(date, close, volume)]} -> (close, volume) DataFrames indexed by date."""
    closes, volumes = {}, {}
    for symbol, rows in history.items():
        for day, close, volume in rows:
            closes.setdefault(symbol, {})[pd.Timestamp(day)] = close
            volumes.setdefault(symbol, {})[pd.Timestamp(day)] = volume
    close = pd.DataFrame(closes).sort_index()
    volume = pd.DataFrame(volumes).reindex(index=close.index, columns=close.columns)
    return close.astype(float), volume.astype(float)
