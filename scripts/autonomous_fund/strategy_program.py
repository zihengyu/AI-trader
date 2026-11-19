"""Small interpreted daily strategy programs, with no Python execution capability.

The interpreter owns every operation. Names cannot reach objects, attributes,
files, imports, network or brokers. Novel mechanisms remain open research tasks
when they require an adapter this language cannot express.
"""
import ast
import math
import operator
import statistics
from bisect import insort
from dataclasses import dataclass

from .risk import RiskError, instant


class Warmup(RiskError):
    pass


class StrategyRuleError(RiskError):
    """An expression needs correction before its saved strategy can be evaluated."""


DATA_CALLS = {"field", "mean", "ret", "stdev"}
FUNCTIONS = DATA_CALLS | {"min", "max", "abs"}
NAMES = {"holding_sessions", "position_weight", "entry_price", "session_index"}
HISTORY_LIMIT = 5000
BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
          ast.Div: operator.truediv, ast.Mod: operator.mod}
COMPARE = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
           ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}


def parse(expression, parameters=()):
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise StrategyRuleError("Strategy expression syntax is invalid") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 200:
        raise StrategyRuleError("Strategy expression exceeds its operation bound")
    allowed = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Call, ast.BinOp,
               ast.UnaryOp, ast.BoolOp, ast.Compare, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
               *BINARY, *COMPARE)
    for node in nodes:
        if not isinstance(node, allowed):
            raise StrategyRuleError("Unsupported strategy expression operation")
        if isinstance(node, ast.Name) and node.id not in NAMES | FUNCTIONS | set(parameters):
            raise StrategyRuleError(f"Unknown strategy expression name: {node.id}")
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or
                                         node.func.id not in FUNCTIONS or node.keywords or len(node.args) > 4):
            raise StrategyRuleError("Unsupported strategy function call")
        if isinstance(node, ast.Constant):
            if type(node.value) not in {str, bool, float, int} or (
                    isinstance(node.value, str) and len(node.value) > 80) or (
                    type(node.value) in {int, float} and (abs(node.value) > 1e100 or not math.isfinite(node.value))):
                raise StrategyRuleError("Invalid strategy expression constant")
    return tree.body


def _constant(node, parameters):
    if any((isinstance(part, ast.Name) and part.id in NAMES) or
           (isinstance(part, ast.Call) and part.func.id in DATA_CALLS) for part in ast.walk(node)):
        return None
    return Evaluator({}, None, parameters).value(node)


def _history_call(call, parameters):
    name, args = call.func.id, call.args
    allowed = {"field": (2, 3), "mean": (3,), "stdev": (3,), "ret": (2,)}
    if len(args) not in allowed[name]:
        raise StrategyRuleError(f"Invalid {name} argument count")
    selectors = args[:1] if name == "ret" else args[:2]
    if any(not isinstance(arg, ast.Constant) or type(arg.value) is not str for arg in selectors):
        raise StrategyRuleError(f"{name} requires literal data-series and field names")
    symbol = args[0].value
    column = "close" if name == "ret" else args[1].value
    window = args[-1] if len(args) > len(selectors) else None
    if window is None:
        return (symbol, column), 1
    if _expression_type(window, parameters) is not float:
        raise StrategyRuleError(f"{name} requires an integer lookback or lag")
    value = _constant(window, parameters)
    if value is None:
        return (symbol, column), None  # The runtime state determines the lookback.
    low, high = {"ret": (1, HISTORY_LIMIT - 1), "field": (0, HISTORY_LIMIT)}.get(name, (1, HISTORY_LIMIT))
    if type(value) is not int or not low <= value <= high:
        raise StrategyRuleError(f"{name} requires an integer lookback or lag within [{low},{high}]")
    return (symbol, column), value + (name in {"field", "ret"})


def _expression_type(node, parameters):
    # Use float as the numeric type marker. Lookbacks enforce integer values separately.
    if isinstance(node, ast.Constant):
        return float if type(node.value) in {int, float} else type(node.value)
    if isinstance(node, ast.Name):
        if node.id not in NAMES and node.id not in parameters:
            raise StrategyRuleError("A function name must be called")
        return float
    if isinstance(node, ast.Call):
        if node.func.id in DATA_CALLS:
            _history_call(node, parameters)
        elif (len(node.args) not in ((1,) if node.func.id == "abs" else (1, 2, 3, 4)) or
              any(_expression_type(arg, parameters) is not float for arg in node.args)):
            raise StrategyRuleError(f"{node.func.id} requires numeric arguments")
        return float
    if isinstance(node, ast.Compare):
        types = [_expression_type(value, parameters) for value in (node.left, *node.comparators)]
        for op, left, right in zip(node.ops, types, types[1:]):
            if not isinstance(op, (ast.Eq, ast.NotEq)) and (left is str) != (right is str):
                raise StrategyRuleError("Comparison operands have incompatible types")
        return bool
    if isinstance(node, ast.BoolOp) or (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)):
        operands = node.values if isinstance(node, ast.BoolOp) else [node.operand]
        if any(_expression_type(arg, parameters) is not bool for arg in operands):
            raise StrategyRuleError("Boolean operators require Boolean operands")
        return bool
    operands = [node.operand] if isinstance(node, ast.UnaryOp) else [node.left, node.right]
    if any(_expression_type(arg, parameters) is not float for arg in operands):
        raise StrategyRuleError("Arithmetic operators require numeric operands")
    return float


def _rule_expressions(rule, parameters):
    for field in ("entry", "exit", "weight"):
        try:
            yield field, parse(rule[field], parameters)
        except StrategyRuleError as exc:
            raise StrategyRuleError(f"{rule['symbol']}.{field}: {exc}") from exc


def _history_requirements(program):
    parameters = parameter_values(program)
    depths, known, session_index = {}, {}, False
    for rule in program["rules"]:
        for _, expression in _rule_expressions(rule, parameters):
            for node in ast.walk(expression):
                if isinstance(node, ast.Name) and node.id == "session_index":
                    session_index = True
                if isinstance(node, ast.Call) and node.func.id in DATA_CALLS:
                    key, depth = _history_call(node, parameters)
                    previous = depths.get(key, 0)
                    depths[key] = None if previous is None or depth is None else max(previous, depth)
                    if depth is not None:
                        known[key] = max(known.get(key, 0), depth)
    return depths, known, session_index


def history_requirements(program):
    """Return the observation depths and whether the rules use an absolute session index."""
    depths, _, session_index = _history_requirements(program)
    return depths, session_index


@dataclass
class StrategyInputs:
    prices: set[str]
    declared: set[str]
    fields: dict[tuple[str, str], int | None]
    known_depths: dict[tuple[str, str], int]
    uses_session_index: bool

    @property
    def symbols(self):
        return self.prices | self.declared | {symbol for symbol, _ in self.fields}

    def describe(self):
        return {"prices": sorted(self.prices), "declared": sorted(self.declared),
                "fields": [{"symbol": symbol, "field": column, "observations": depth,
                            "known_observations": self.known_depths.get((symbol, column), 0)}
                           for (symbol, column), depth in sorted(self.fields.items())],
                "uses_session_index": self.uses_session_index}


def strategy_inputs(proposal, cash_symbol=None, reference=None):
    """Derive input dependencies without changing the frozen proposal text."""
    prices = {proposal["validation"]["benchmark"]}
    if cash_symbol:
        prices.add(cash_symbol)
    result = StrategyInputs(prices, set(), {}, {}, False)
    for arm in (proposal, reference):
        if arm is None:
            continue
        result.prices.update(arm["instruments"])
        result.declared.update(arm.get("data_symbols", ()))
        if arm.get("implementation") is None:
            continue
        fields, known, uses_index = _history_requirements(arm["implementation"])
        result.uses_session_index |= uses_index
        for key, depth in fields.items():
            previous = result.fields.get(key, 0)
            result.fields[key] = None if previous is None or depth is None else max(previous, depth)
        for key, depth in known.items():
            result.known_depths[key] = max(result.known_depths.get(key, 0), depth)
    return result


def validate_program(program, instruments):
    if program["adapter"] not in {"daily_rules_v1", "daily_cash_rules_v1"}:
        return "needs_adapter"
    rules = program.get("rules") or []
    if not rules:
        return "needs_formalization"
    symbols = [row["symbol"] for row in rules]
    if len(symbols) != len(set(symbols)) or set(symbols) - set(instruments):
        raise StrategyRuleError("Strategy rules must uniquely name proposed instruments")
    parameters = parameter_values(program)
    if any(not k.startswith("p_") or not k.isidentifier() or len(k) > 40 or type(v) not in {int, float}
           or not math.isfinite(v) or abs(v) > 1e6 for k, v in parameters.items()):
        raise StrategyRuleError("Strategy parameters must have p_ names and bounded numeric values")
    for rule in rules:
        for field, expression in _rule_expressions(rule, parameters):
            try:
                required = float if field == "weight" else bool
                if _expression_type(expression, parameters) is not required:
                    expected = "numeric" if field == "weight" else "Boolean"
                    raise StrategyRuleError(f"Expression must return a {expected} value")
                value = _constant(expression, parameters)
                if field == "weight" and value is not None and not 0 <= value <= 1:
                    raise StrategyRuleError("Strategy target weight is outside [0,1]")
            except StrategyRuleError as exc:
                raise StrategyRuleError(f"{rule['symbol']}.{field}: {exc}") from exc
    return "ready"


def parameter_values(program):
    raw = program.get("parameters") or []
    if isinstance(raw, dict):
        return raw  # Read pre-release prototype artifacts without losing their type.
    result = {}
    for parameter in raw:
        key, value = parameter["name"], parameter["value"]
        if key in result or parameter.get("integer") and (type(value) not in {int, float} or int(value) != value):
            raise StrategyRuleError("Strategy parameters must be unique and preserve declared integer values")
        result[key] = int(value) if parameter.get("integer") else value
    return result


class _ObservationIndex:
    def __init__(self, rows):
        self.rows = rows
        # Eligibility is independent of the original ordering used by lookbacks.
        self.releases = sorted((max(instant(row["at"]), instant(row["available_at"])), index)
                               for index, row in enumerate(rows))
        self.visible = []
        self.cursor = 0
        self.cutoff = None

    def window(self, cutoff, count, lag):
        if self.cutoff is not None and cutoff < self.cutoff:
            self.visible.clear()
            self.cursor = 0
        self.cutoff = cutoff
        while self.cursor < len(self.releases) and self.releases[self.cursor][0] <= cutoff:
            _, index = self.releases[self.cursor]
            if not self.visible or self.visible[-1] < index:
                self.visible.append(index)
            else:
                insort(self.visible, index)
            self.cursor += 1
        if len(self.visible) < count + lag:
            return ()
        stop = len(self.visible) - lag
        return tuple(self.rows[index] for index in self.visible[stop - count:stop])


class HistoryIndex:
    def __init__(self, histories):
        self.histories = histories
        self.indexes = {}

    def window(self, symbol, cutoff, count=1, lag=0):
        if symbol not in self.indexes:
            self.indexes[symbol] = _ObservationIndex(self.histories.get(symbol, ()))
        return self.indexes[symbol].window(cutoff, count, lag)


class Evaluator:
    def __init__(self, histories, now, names):
        self.history = histories if isinstance(histories, HistoryIndex) else HistoryIndex(histories)
        self.now, self.names = instant(now) if now is not None else None, names

    def observations(self, symbol, column, count, lag=0):
        if type(symbol) is not str or type(column) is not str:
            raise StrategyRuleError("A data field requires a symbol and column name")
        if type(count) is not int or type(lag) is not int or not 1 <= count <= HISTORY_LIMIT or not 0 <= lag <= HISTORY_LIMIT:
            raise StrategyRuleError("Strategy history windows must be bounded nonnegative integers")
        # No fill-forward: a missing point-in-time value stays missing.
        selected = self.history.window(symbol, self.now, count, lag)
        if not selected:
            raise Warmup("Insufficient point-in-time strategy observations")
        result = []
        for row in selected:
            value = row.get(column)
            # Optional external features carry their own publication time; a
            # current financial statement may not be backdated into a price bar.
            publications = row.get("feature_available_at") or {}
            if (column in publications and instant(publications[column]) > self.now or type(value) not in {float, int}
                    or not math.isfinite(value)):
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
                raise StrategyRuleError("Return window must be a positive integer")
            values = self.observations(args[0], "close", args[1] + 1)
            return values[-1] / values[0] - 1
        if name in {"min", "max"} and len(args) >= 1 and all(type(v) in {int, float} for v in args):
            return (min if name == "min" else max)(args)
        if name == "abs" and len(args) == 1 and type(args[0]) in {int, float}:
            return abs(args[0])
        raise StrategyRuleError("Invalid strategy function arguments")

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
                raise StrategyRuleError("Invalid strategy arithmetic") from exc
        if isinstance(node, ast.Compare):
            left = self.value(node.left)
            for op, target in zip(node.ops, node.comparators):
                right = self.value(target)
                try:
                    if not COMPARE[type(op)](left, right):
                        return False
                except TypeError as exc:
                    raise StrategyRuleError("Comparison operands have incompatible types") from exc
                left = right
            return True
        raise StrategyRuleError("Unsupported strategy expression")

    @staticmethod
    def numeric(value):
        if type(value) not in {float, int} or not math.isfinite(value):
            raise StrategyRuleError("Strategy expression needs a finite number")
        return value

    @staticmethod
    def require_bool(value):
        if type(value) is not bool:
            raise StrategyRuleError("Entry and exit expressions must return Boolean values")
        return value

    def boolean(self, node):
        return self.require_bool(self.value(node))

    @staticmethod
    def checked(value):
        if type(value) in {int, float} and (not math.isfinite(value) or abs(value) > 1e100):
            raise StrategyRuleError("Non-finite or unbounded strategy expression")
        return value


def compile_targets(program):
    parameters = parameter_values(program)
    return parameters, [(rule, dict(_rule_expressions(rule, parameters))) for rule in program["rules"]]


def targets(program, histories, now, *, session_index, book, held_since, compiled=None):
    result = {}
    parameters, rules = compile_targets(program) if compiled is None else compiled
    indexed = histories if isinstance(histories, HistoryIndex) else HistoryIndex(histories)
    now = instant(now)
    for rule, expressions in rules:
        symbol = rule["symbol"]
        held = book.positions.get(symbol, 0)
        rows = indexed.window(symbol, now) if held else ()
        mark = rows[-1]["close"] if rows else 0
        names = {**parameters, "holding_sessions": max(0, session_index - held_since.get(symbol, session_index)),
                 "position_weight": float(held * mark) / float(book.last["equity"]) if held and book.last else 0,
                 "entry_price": float(book.averages.get(symbol, 0)), "session_index": session_index}
        evaluator = Evaluator(indexed, now, names)
        try:
            field = "exit" if held else "entry"
            condition_met = evaluator.boolean(expressions[field])
            if (held and condition_met) or (not held and not condition_met):
                continue
            field = "weight"
            weight = evaluator.numeric(evaluator.value(expressions[field]))
            if not 0 <= weight <= 1:
                raise StrategyRuleError("Strategy target weight is outside [0,1]")
            result[symbol] = weight
        except Warmup:
            # Missing data must not become an implicit sell instruction.
            if held:
                raise
        except StrategyRuleError as exc:
            raise StrategyRuleError(f"{symbol}.{field}: {exc}") from exc
    if sum(result.values()) > 1 + 1e-12:
        raise StrategyRuleError("Strategy targets exceed capital")
    return result
