"""Parameterized, read-only statistics over already loaded research tables.

No data fetching, trading, SQL repair, source conversion or calendar guessing.
Horizons count the union of observed timestamps, not invented trading sessions.
An absent endpoint stays absent even when the next recorded row is available.
"""
from __future__ import annotations

import math
from typing import Any


METRICS = ("returns", "forward_returns", "volatility", "correlation", "beta")


class AnalysisUnsupported(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _positive_integer(name: str, value: Any, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise AnalysisUnsupported("invalid_parameter", f"{name} must be an integer >= {minimum}")
    return value


def _column(schema: dict, name: str) -> str:
    if name not in schema["column_types"]:
        raise AnalysisUnsupported("unknown_column", f"Column {name!r} is absent from {schema['table_name']!r}; actual columns: {schema['columns']}")
    return _identifier(name)


def _series(layer, table_name, value_column, time_column, group_column, time_zone, *, selector=None):
    schema = layer.describe(table_name=table_name)
    if schema.get("schema_error"):
        raise AnalysisUnsupported("schema_unavailable", "Column types could not be inspected; preserve the loaded evidence and use free SQL or other research tools for explicit interpretation")
    value = _column(schema, value_column)
    if schema["column_types"][value_column] == "BOOLEAN":
        raise AnalysisUnsupported("unsupported_value_type", "Boolean values are not prices; inspect the schema and use duckdb_query for another calculation")
    time = _column(schema, time_column)
    dtype = schema["column_types"][time_column].upper()
    zone = time_zone or schema.get("datetime_timezone")
    date_only = dtype == "DATE"
    if "WITH TIME ZONE" in dtype or dtype == "TIMESTAMPTZ":
        expression, domain = f"TRY_CAST({time} AS TIMESTAMPTZ)", "instant"
    elif dtype.startswith("TIMESTAMP") or date_only:
        expression, domain = f"TRY_CAST({time} AS TIMESTAMP)", "naive"
        if zone:
            expression, domain = f"timezone({_literal(zone)}, {expression})", "instant"
    else:
        raise AnalysisUnsupported("unsupported_time_type", "The time column must be DATE or TIMESTAMP; inspect/parse text dates explicitly with duckdb_query")
    historical = bool(getattr(layer.strategy, "is_backtesting", False))
    clock = layer._current_datetime()
    cutoff, cutoff_basis = "TRUE", None
    if historical:
        if clock is None:
            raise AnalysisUnsupported("missing_historical_clock", "Historical analysis requires the strategy clock")
        if domain == "instant":
            if getattr(clock, "tzinfo", None) is None or clock.utcoffset() is None:
                raise AnalysisUnsupported("ambiguous_historical_clock", "Historical instant comparisons require a timezone-aware strategy clock")
            cutoff = f"t <= {layer._timestamp_literal(clock)}"
            cutoff_basis = "strategy_datetime"
        elif date_only:
            cutoff = f"t < TIMESTAMP {_literal(str(clock.date()) + 'T00:00:00')} + INTERVAL 1 DAY"
            cutoff_basis = "strategy_date_only"
        else:
            raise AnalysisUnsupported("time_zone_required", "Naive historical timestamps need time_zone or loaded timezone metadata; free SQL remains available for explicit time handling")
    group = _column(schema, group_column) if group_column else "'series'"
    predicate = ""
    if selector is not None:
        if not group_column:
            raise AnalysisUnsupported("benchmark_group_required", "benchmark_symbol requires the benchmark group column")
        predicate = f" WHERE CAST({group} AS VARCHAR) = {_literal(selector)}"
    raw = (f"SELECT CAST({group} AS VARCHAR) AS g, {expression} AS t, TRY_CAST({value} AS DOUBLE) AS raw_value "
           f"FROM {_identifier(schema['table_name'])}{predicate}")
    visible = f"SELECT g,t,CASE WHEN isfinite(raw_value) AND raw_value > 0 THEN raw_value END AS v FROM raw WHERE t IS NOT NULL AND ({cutoff})"
    # Validate before calculating: duplicate timestamps must not be silently
    # averaged, deduplicated, or used in a nondeterministically ordered window.
    summary = layer.query(sql=f"""WITH raw AS ({raw}), visible AS ({visible}),
        duplicates AS (SELECT g,t,COUNT(*) AS n FROM visible GROUP BY g,t HAVING COUNT(*)>1)
        SELECT (SELECT COUNT(*) FROM raw) AS input_rows,
               (SELECT COUNT(*) FROM raw WHERE t IS NULL) AS invalid_time_rows,
               (SELECT COUNT(*) FROM raw WHERE t IS NOT NULL AND NOT ({cutoff})) AS future_rows_withheld,
               COUNT(*) AS visible_rows, COUNT(*) FILTER (WHERE v IS NULL) AS invalid_value_rows,
               COUNT(*) FILTER (WHERE g IS NULL) AS missing_group_rows,
               (SELECT COUNT(*) FROM duplicates) AS duplicate_keys,
               MIN(t) AS first_timestamp, MAX(t) AS last_timestamp FROM visible""", limit=1)["rows"][0]
    if not summary["visible_rows"]:
        summary["first_timestamp"] = summary["last_timestamp"] = None
    if summary["duplicate_keys"]:
        raise AnalysisUnsupported("duplicate_observations", "Duplicate (group,time) observations; specify group_column or resolve the intended sampling with free SQL")
    if summary["missing_group_rows"]:
        raise AnalysisUnsupported("missing_group", "Some observations have no group; choose the intended grouping explicitly with free SQL")
    sql = f"WITH raw AS ({raw}) {visible}"
    return sql, {"table_name": table_name, "value_column": value_column, "time_column": time_column,
                 "group_column": group_column, "time_zone": zone, "time_domain": domain,
                 "historical_cutoff_basis": cutoff_basis, **summary}


def analyze(layer, *, table_name: str, metric: str, value_column: str = "close", time_column: str = "datetime",
            group_column: str | None = None, window: int = 20, horizon: int = 1,
            annualization: float | None = None, benchmark_table: str | None = None,
            benchmark_symbol: str | None = None, benchmark_value_column: str | None = None,
            benchmark_time_column: str | None = None, benchmark_group_column: str | None = None,
            time_zone: str | None = None, limit: int = 200) -> dict[str, Any]:
    """Fixed SQL templates; arbitrary questions retain the ordinary SQL tool."""
    if metric not in METRICS:
        raise AnalysisUnsupported("unsupported_metric", f"Supported metrics: {list(METRICS)}; use duckdb_query for other research")
    window = _positive_integer("window", window, 2)
    horizon = _positive_integer("horizon", horizon)
    limit = _positive_integer("limit", limit)
    if annualization is not None and (isinstance(annualization, bool) or
            not isinstance(annualization, (int, float)) or not math.isfinite(annualization) or annualization <= 0):
        raise AnalysisUnsupported("invalid_annualization", "annualization must be a finite positive number; omit it for per-observation volatility")
    main, coverage = _series(layer, table_name, value_column, time_column, group_column, time_zone)
    reference, reference_coverage = None, None
    if metric in {"correlation", "beta"}:
        if benchmark_table is None:
            raise AnalysisUnsupported("missing_benchmark", "correlation/beta require benchmark_table; it can be the same loaded table with benchmark_symbol")
        reference, reference_coverage = _series(layer, benchmark_table, benchmark_value_column or value_column,
            benchmark_time_column or time_column, benchmark_group_column or (group_column if benchmark_symbol is not None else None),
            time_zone, selector=benchmark_symbol)
        if coverage["time_domain"] != reference_coverage["time_domain"]:
            raise AnalysisUnsupported("incompatible_time_domains", "Series timestamps have different timezone semantics; specify time_zone or align them explicitly with free SQL")
    grid_source = "SELECT t FROM main"
    benchmark_cte = ""
    if reference is not None:
        benchmark_cte = f", benchmark AS ({reference})"
        grid_source += " UNION SELECT t FROM benchmark"
    base = f"""WITH main AS ({main}){benchmark_cte},
        grid AS (SELECT t,ROW_NUMBER() OVER (ORDER BY t) AS step FROM ({grid_source}) dates GROUP BY t),
        indexed AS (SELECT m.*,grid.step FROM main m JOIN grid USING(t))"""
    direction = "+" if metric == "forward_returns" else "-"
    distance = horizon if metric in {"returns", "forward_returns"} else 1
    ratio = "endpoint.v / current.v - 1" if metric == "forward_returns" else "current.v / endpoint.v - 1"
    base += f""", raw_returns AS (
        SELECT current.g,current.t,current.step,({ratio}) AS r
        FROM indexed current LEFT JOIN indexed endpoint ON current.g=endpoint.g AND endpoint.step=current.step {direction} {distance}),
        returns AS (SELECT g,t,step,CASE WHEN isfinite(r) THEN r END AS r FROM raw_returns)"""
    fields, invalid_status = "r AS statistic,CASE WHEN r IS NULL THEN 0 ELSE 1 END AS samples", "invalid_or_missing_endpoint"
    if metric == "volatility":
        base += f""", statistics AS (
            SELECT g,t,step,COUNT(r) OVER w AS samples,STDDEV_SAMP(r) OVER w AS statistic
            FROM returns WINDOW w AS (PARTITION BY g ORDER BY step RANGE BETWEEN {window - 1} PRECEDING AND CURRENT ROW))"""
        result_from, valid = "statistics", f"samples={window} AND isfinite(statistic)"
        invalid_status = "insufficient_observations_or_undefined_statistic"
    elif metric in {"correlation", "beta"}:
        function = "CORR" if metric == "correlation" else "REGR_SLOPE"
        base += f""", benchmark_indexed AS (SELECT b.*,grid.step FROM benchmark b JOIN grid USING(t)),
            benchmark_raw_returns AS (
                SELECT b.t,b.step,b.v / previous.v - 1 AS r FROM benchmark_indexed b
                LEFT JOIN benchmark_indexed previous ON previous.g=b.g AND previous.step=b.step-1),
            pairs AS (SELECT r.g,r.t,r.step,
                CASE WHEN isfinite(r.r) AND isfinite(b.r) THEN r.r END AS y,
                CASE WHEN isfinite(r.r) AND isfinite(b.r) THEN b.r END AS x
                FROM returns r LEFT JOIN benchmark_raw_returns b USING(t,step)),
            statistics AS (SELECT g,t,step,COUNT(y) OVER w AS samples,{function}(y,x) OVER w AS statistic
                FROM pairs WINDOW w AS (PARTITION BY g ORDER BY step RANGE BETWEEN {window - 1} PRECEDING AND CURRENT ROW))"""
        result_from, valid = "statistics", f"samples={window} AND isfinite(statistic)"
        invalid_status = "insufficient_pairs_or_zero_variance"
    else:
        base += f", statistics AS (SELECT g,t,step,{fields} FROM returns)"
        result_from, valid = "statistics", "samples=1 AND isfinite(statistic)"
    annual = (f", CASE WHEN {valid} THEN statistic * SQRT({float(annualization)}) END AS annualized_value"
              if metric == "volatility" and annualization is not None else "")
    sql = base + f""" SELECT g AS series,t AS datetime,
        CASE WHEN {valid} THEN statistic END AS value,samples,
        CASE WHEN {valid} THEN 'ok' ELSE '{invalid_status}' END AS status{annual},
        COUNT(*) OVER () AS _total_rows FROM {result_from} ORDER BY t DESC,g LIMIT {limit + 1}"""
    result = layer.query(sql=sql, limit=limit)
    rows = result["rows"]
    total = int(rows[0]["_total_rows"]) if rows else 0
    for row in rows:
        row.pop("_total_rows", None)
        for key, value in list(row.items()):
            if isinstance(value, float) and not math.isfinite(value):
                row[key] = None
    return {"ok": True, "metric": metric, "input_table": table_name, "row_count": total, "rows": rows,
            "truncated": total > limit, "coverage": coverage, "benchmark_coverage": reference_coverage,
            "convention": {"numeric_mode": "approximate_double_for_research_only", "return_type": "simple",
                "horizon_grid_steps": distance, "window_grid_steps": window if metric not in {"returns", "forward_returns"} else None,
                "grid": "union_of_observed_timestamps_not_an_exchange_calendar",
                "missing_values": "remain_missing_no_forward_fill_no_zero_substitution",
                "volatility_ddof": 1 if metric == "volatility" else None,
                "annualization": annualization if metric == "volatility" else None,
                "order": "latest_timestamp_first"},
            "fallback": {"tool": "duckdb_query", "available": True,
                         "when": "Use free SQL for other metrics, calendar/session horizons, custom sampling, conditions or exact decimal arithmetic"}}
