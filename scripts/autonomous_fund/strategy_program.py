"""Small interpreted daily strategy programs, with no Python execution capability.

The interpreter owns every operation. Names cannot reach objects, attributes,
files, imports, network or brokers. Novel mechanisms remain open research tasks
when they require an adapter this language cannot express.
"""
import ast
import math
import operator
import statistics

from .risk import RiskError, instant


class Warmup(RiskError):
    pass


FUNCTIONS = {"field", "mean", "ret", "stdev", "min", "max", "abs"}
NAMES = {"holding_sessions", "position_weight", "entry_price", "session_index"}
BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
          ast.Div: operator.truediv, ast.Mod: operator.mod}
COMPARE = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
           ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}


def parse(expression, parameters=()):
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise RiskError("Strategy expression syntax is invalid") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 200:
        raise RiskError("Strategy expression exceeds its operation bound")
    allowed = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Call, ast.BinOp,
               ast.UnaryOp, ast.BoolOp, ast.Compare, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
               *BINARY, *COMPARE)
    for node in nodes:
        if not isinstance(node, allowed):
            raise RiskError("Unsupported strategy expression operation")
        if isinstance(node, ast.Name) and node.id not in NAMES | FUNCTIONS | set(parameters):
            raise RiskError(f"Unknown strategy expression name: {node.id}")
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or
                                         node.func.id not in FUNCTIONS or node.keywords or len(node.args) > 4):
            raise RiskError("Unsupported strategy function call")
        if isinstance(node, ast.Constant):
            if type(node.value) not in {str, bool, float, int} or (
                    isinstance(node.value, str) and len(node.value) > 80) or (
                    type(node.value) in {int, float} and (abs(node.value) > 1e100 or not math.isfinite(node.value))):
                raise RiskError("Invalid strategy expression constant")
    return tree.body


def validate_program(program, instruments):
    if program["adapter"] not in {"daily_rules_v1", "daily_cash_rules_v1"}:
        return "needs_adapter"
    rules = program.get("rules") or []
    if not rules:
        return "needs_formalization"
    symbols = [row["symbol"] for row in rules]
    if len(symbols) != len(set(symbols)) or set(symbols) - set(instruments):
        raise RiskError("Strategy rules must uniquely name proposed instruments")
    parameters = parameter_values(program)
    if any(not k.startswith("p_") or not k.isidentifier() or len(k) > 40 or type(v) not in {int, float}
           or not math.isfinite(v) or abs(v) > 1e6 for k, v in parameters.items()):
        raise RiskError("Strategy parameters must have p_ names and bounded numeric values")
    for rule in rules:
        for name in ("entry", "exit", "weight"):
            parse(rule[name], parameters)
    return "ready"


def parameter_values(program):
    raw = program.get("parameters") or []
    if isinstance(raw, dict):
        return raw  # Read pre-release prototype artifacts without losing their type.
    result = {}
    for parameter in raw:
        key, value = parameter["name"], parameter["value"]
        if key in result or parameter.get("integer") and (type(value) not in {int, float} or int(value) != value):
            raise RiskError("Strategy parameters must be unique and preserve declared integer values")
        result[key] = int(value) if parameter.get("integer") else value
    return result


class Evaluator:
    def __init__(self, histories, now, names):
        self.histories, self.now, self.names = histories, instant(now), names

    def observations(self, symbol, column, count, lag=0):
        if type(symbol) is not str or type(column) is not str:
            raise RiskError("A data field requires a symbol and column name")
        if type(count) is not int or type(lag) is not int or not 1 <= count <= 5000 or not 0 <= lag <= 5000:
            raise RiskError("Strategy history windows must be bounded nonnegative integers")
        # No fill-forward: a missing point-in-time value stays missing.
        rows = [r for r in self.histories.get(symbol, ()) if instant(r["available_at"]) <= self.now
                and instant(r["at"]) <= self.now]
        if len(rows) < count + lag:
            raise Warmup("Insufficient point-in-time strategy observations")
        selected = rows[len(rows) - count - lag:len(rows) - lag if lag else None]
        result = []
        for row in selected:
            value = row.get(column)
            # Optional external features carry their own publication time; a
            # current financial statement may not be backdated into a price bar.
            stamp = (row.get("feature_available_at") or {}).get(column, row["available_at"])
            if instant(stamp) > self.now or type(value) not in {float, int} or not math.isfinite(value):
                raise Warmup("Missing or not-yet-published strategy feature")
            result.append(float(value))
        return result

    def call(self, name, args):
        if name == "field" and len(args) in {2, 3}:
            return self.observations(args[0], args[1], 1, args[2] if len(args) == 3 else 0)[0]
        if name in {"mean", "stdev"} and len(args) == 3:
            values = self.observations(*args)
            return statistics.fmean(values) if name == "mean" else statistics.pstdev(values)
        if name == "ret" and len(args) == 2:
            if type(args[1]) is not int or args[1] < 1:
                raise RiskError("Return window must be a positive integer")
            values = self.observations(args[0], "close", args[1] + 1)
            return values[-1] / values[0] - 1
        if name in {"min", "max"} and len(args) >= 1 and all(type(v) in {int, float} for v in args):
            return (min if name == "min" else max)(args)
        if name == "abs" and len(args) == 1 and type(args[0]) in {int, float}:
            return abs(args[0])
        raise RiskError("Invalid strategy function arguments")

    def value(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id in self.names:
            return self.names[node.id]
        if isinstance(node, ast.Call):
            return self.checked(self.call(node.func.id, [self.value(arg) for arg in node.args]))
        if isinstance(node, ast.BoolOp):
            # Short circuiting makes explicit warm-up checks useful.
            return all(self.boolean(v) for v in node.values) if isinstance(node.op, ast.And) else any(
                self.boolean(v) for v in node.values)
        if isinstance(node, ast.UnaryOp):
            value = self.value(node.operand)
            if isinstance(node.op, ast.Not):
                return not self.require_bool(value)
            return self.checked(-self.numeric(value) if isinstance(node.op, ast.USub) else self.numeric(value))
        if isinstance(node, ast.BinOp):
            left, right = self.numeric(self.value(node.left)), self.numeric(self.value(node.right))
            try:
                return self.checked(BINARY[type(node.op)](left, right))
            except (ArithmeticError, OverflowError) as exc:
                raise RiskError("Invalid strategy arithmetic") from exc
        if isinstance(node, ast.Compare):
            left = self.value(node.left)
            for op, target in zip(node.ops, node.comparators):
                right = self.value(target)
                if not COMPARE[type(op)](left, right):
                    return False
                left = right
            return True
        raise RiskError("Unsupported strategy expression")

    @staticmethod
    def numeric(value):
        if type(value) not in {float, int} or not math.isfinite(value):
            raise RiskError("Strategy expression needs a finite number")
        return value

    @staticmethod
    def require_bool(value):
        if type(value) is not bool:
            raise RiskError("Entry and exit expressions must return Boolean values")
        return value

    def boolean(self, node):
        return self.require_bool(self.value(node))

    @staticmethod
    def checked(value):
        if type(value) in {int, float} and (not math.isfinite(value) or abs(value) > 1e100):
            raise RiskError("Non-finite or unbounded strategy expression")
        return value


def targets(program, histories, now, *, session_index, book, held_since):
    result = {}
    parameters = parameter_values(program)
    compiled = [(rule, {k: parse(rule[k], parameters) for k in ("entry", "exit", "weight")}) for rule in program["rules"]]
    for rule, expressions in compiled:
        symbol = rule["symbol"]
        held = book.positions.get(symbol, 0)
        rows = [r for r in histories.get(symbol, ()) if instant(r["at"]) <= instant(now)
                and instant(r["available_at"]) <= instant(now)]
        mark = rows[-1]["close"] if rows else 0
        names = {**parameters, "holding_sessions": max(0, session_index - held_since.get(symbol, session_index)),
                 "position_weight": float(held * mark) / float(book.last["equity"]) if held and book.last else 0,
                 "entry_price": float(book.averages.get(symbol, 0)), "session_index": session_index}
        evaluator = Evaluator(histories, now, names)
        try:
            if held and evaluator.boolean(expressions["exit"]):
                continue
            if held or evaluator.boolean(expressions["entry"]):
                weight = evaluator.numeric(evaluator.value(expressions["weight"]))
                if not 0 <= weight <= 1:
                    raise RiskError("Strategy target weight is outside [0,1]")
                result[symbol] = weight
        except Warmup:
            # Missing data must not become an implicit sell instruction.
            if held:
                raise
    if sum(result.values()) > 1 + 1e-12:
        raise RiskError("Strategy targets exceed capital")
    return result
