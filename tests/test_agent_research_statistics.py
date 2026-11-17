"""Generic research contracts: native DuckDB, served tools, no inference/provider/broker I/O."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from lumibot.components.agents import BuiltinTools
from lumibot.components.agents.duckdb_tools import DuckDBQueryLayer
from lumibot.components.agents.research_statistics import AnalysisUnsupported
from lumibot.components.agents.runtime import _wrap_tool_callable


NOW = datetime(2026, 8, 10, 16, tzinfo=timezone.utc)


@pytest.fixture
def layer():
    strategy = SimpleNamespace(is_backtesting=False, get_datetime=lambda: NOW)
    result = DuckDBQueryLayer(strategy)
    yield result
    result.connection.close()


def loaded(layer, values, *, name="prices", groups=None, times=None, column="close", zone="UTC"):
    times = times if times is not None else pd.date_range("2026-08-01", periods=len(values), tz=zone)
    frame = pd.DataFrame({"datetime": times, column: values})
    if groups is not None:
        frame["symbol"] = groups
    info = layer.register_document_table(name, frame, source="synthetic-data")
    return info["table_name"], frame


def chronological(result, series=None):
    rows = [row for row in result["rows"] if series is None or row["series"] == series]
    return sorted(rows, key=lambda row: row["datetime"])


def served(layer):
    context = {"tool_calls": [], "available_tool_names": ["duckdb_describe", "duckdb_analyze", "duckdb_query"]}
    manager = SimpleNamespace(duckdb=layer)
    tools = {definition.name: _wrap_tool_callable(definition.binder(layer.strategy, manager), context)
             for definition in (BuiltinTools.duckdb.describe(), BuiltinTools.duckdb.analyze(), BuiltinTools.duckdb.query())}
    return tools, context


def test_loaded_and_described_schema_uses_actual_duckdb_decimal_types_without_changing_sources(layer):
    values = [Decimal("100.123456789012345"), None, Decimal("101.123456789012345")]
    table, frame = loaded(layer, values)
    info = layer._table_meta[table]
    assert info["column_types"]["close"].startswith("DECIMAL")
    assert info["columns"] == [row["name"] for row in info["schema"]]
    assert layer.describe(table_name=table)["column_types"] == info["column_types"]
    assert frame["close"].tolist() == values
    assert layer.query(sql=f'SELECT * FROM "{table}" LIMIT 0')["column_types"]["close"].startswith("DECIMAL")


def test_schema_feedback_never_exposes_native_backing_frame_values_or_tables(layer):
    layer._register_frame("backing", pd.DataFrame({"future_price": [999999]}), {"kind": "source_frame"})
    table, _ = loaded(layer, [100, 101])
    assert [row["table_name"] for row in layer.describe()["tables"]] == [table]
    with pytest.raises(ValueError, match="Unknown loaded table"):
        layer.describe(table_name="backing")


def test_served_query_error_retains_failure_schema_and_later_success(layer):
    table, _ = loaded(layer, [100, 101])
    tools, context = served(layer)
    failure = tools["duckdb_query"](sql=f'SELECT imaginary_column FROM "{table}"')
    assert failure["tool_error"] and failure["error"]["type"] == "BinderException"
    assert failure["tables"][0]["column_types"]["close"]
    assert "rows" not in failure
    success = tools["duckdb_query"](sql=f'SELECT MAX(close) AS observed_max FROM "{table}"')
    assert success["rows"] == [{"observed_max": 101}]
    assert [call["ok"] for call in context["tool_calls"]] == [False, True]
    assert layer.get_metrics()["query_attempts"] == 2 and layer.get_metrics()["query_failures"] == 1


def test_simple_returns_and_forward_maturity_are_explicit(layer):
    table, _ = loaded(layer, [100, 110, 99, 118.8])
    backward = chronological(layer.analyze(table_name=table, metric="returns"))
    assert backward[0]["value"] is None
    assert [row["value"] for row in backward[1:]] == pytest.approx([.1, -.1, .2])
    forward = layer.analyze(table_name=table, metric="forward_returns", horizon=2)
    rows = chronological(forward)
    assert [row["value"] for row in rows[:2]] == pytest.approx([-.01, .08])
    assert rows[2]["value"] is None and rows[3]["value"] is None
    assert forward["convention"]["horizon_grid_steps"] == 2
    assert forward["convention"]["grid"] == "union_of_observed_timestamps_not_an_exchange_calendar"


def test_rolling_volatility_matches_independent_sample_standard_deviation(layer):
    table, _ = loaded(layer, [100, 110, 99, 103.95])
    rows = chronological(layer.analyze(table_name=table, metric="volatility", window=3, annualization=252))
    expected = np.std([.1, -.1, .05], ddof=1)
    assert [row["samples"] for row in rows] == [0, 1, 2, 3]
    assert rows[2]["value"] is None and rows[3]["value"] == pytest.approx(expected)
    assert rows[3]["annualized_value"] == pytest.approx(expected * np.sqrt(252))
    assert "annualized_value" not in layer.analyze(table_name=table, metric="volatility", window=3)["rows"][0]


@pytest.mark.parametrize("metric,expected", [("correlation", 1), ("beta", 2)])
def test_paired_statistics_match_known_returns_after_timestamp_alignment(layer, metric, expected):
    asset, _ = loaded(layer, [100, 120, 96, 105.6], name="asset")
    market, _ = loaded(layer, [100, 110, 99, 103.95], name="reference")
    rows = chronological(layer.analyze(table_name=asset, metric=metric, benchmark_table=market, window=3))
    assert rows[-1]["samples"] == 3 and rows[-1]["value"] == pytest.approx(expected)
    assert rows[-2]["value"] is None


def test_grouped_scan_uses_requested_benchmark_without_mixing_series(layer):
    dates = list(pd.date_range("2026-08-01", periods=4, tz="UTC"))
    table, _ = loaded(layer, [100, 120, 96, 105.6, 100, 110, 99, 103.95],
        groups=["ALFA"] * 4 + ["INDEX"] * 4, times=dates + dates)
    result = layer.analyze(table_name=table, metric="beta", benchmark_table=table,
        benchmark_symbol="INDEX", group_column="symbol", window=3)
    assert chronological(result, "ALFA")[-1]["value"] == pytest.approx(2)
    assert chronological(result, "INDEX")[-1]["value"] == pytest.approx(1)


@pytest.mark.parametrize("metric,expected", [("correlation", 1), ("beta", 2)])
def test_paired_statistics_require_a_single_visible_benchmark_series(layer, metric, expected):
    dates = list(pd.date_range("2026-08-01", periods=4, tz="UTC"))
    asset, _ = loaded(layer, [100, 120, 96, 105.6], name="candidate", times=dates)
    refs, _ = loaded(layer, [100, 110, 99, 103.95, 100, 98, 101, 105], name="references",
                     groups=["REF_A"] * 4 + ["REF_B"] * 4, times=dates + dates)
    tools, _ = served(layer)
    args = dict(table_name=asset, metric=metric, benchmark_table=refs, benchmark_group_column="symbol", window=3)
    # Joining two benchmark series by time alone duplicates the asset's rows
    # and counts two otherwise unrelated returns for each timestamp.
    ambiguous = tools["duckdb_analyze"](**args)
    assert ambiguous["error"]["code"] == "ambiguous_benchmark"
    assert ambiguous["fallback"]["available"]
    selected = tools["duckdb_analyze"](**args, benchmark_symbol="REF_A")
    assert selected["row_count"] == 4
    assert chronological(selected)[-1]["samples"] == 3
    assert chronological(selected)[-1]["value"] == pytest.approx(expected)
    assert tools["duckdb_query"](sql=f'SELECT COUNT(*) AS n FROM "{refs}"')["rows"] == [{"n": 8}]


def test_a_benchmark_group_with_only_future_rows_does_not_change_backtest_results(layer):
    dates = list(pd.date_range("2026-08-01", periods=4, tz="UTC"))
    layer.strategy.is_backtesting = True
    layer.strategy.get_datetime = lambda: dates[-1]
    asset, _ = loaded(layer, [100, 120, 96, 105.6], name="candidate", times=dates)
    refs, _ = loaded(layer, [100, 110, 99, 103.95, 9999], name="references",
                     groups=["REF_A"] * 4 + ["REF_B"], times=[*dates, dates[-1] + pd.Timedelta(days=1)])
    result = layer.analyze(table_name=asset, metric="beta", benchmark_table=refs,
                           benchmark_group_column="symbol", window=3)
    assert result["benchmark_coverage"]["future_rows_withheld"] == 1
    assert result["row_count"] == 4
    assert chronological(result)[-1]["value"] == pytest.approx(2)


def test_missing_timestamp_does_not_turn_a_two_step_move_into_a_one_step_return(layer):
    dates = list(pd.date_range("2026-08-01", periods=4, tz="UTC"))
    table, _ = loaded(layer, [100, 110, 121, 10, 11, 12, 13], groups=["ALFA"] * 3 + ["OTHER"] * 4,
        times=[dates[0], dates[1], dates[3], *dates])
    result = layer.analyze(table_name=table, metric="returns", group_column="symbol")
    rows = chronological(result, "ALFA")
    assert rows[1]["value"] == pytest.approx(.1)
    assert rows[2]["value"] is None  # The missing common-grid endpoint is not skipped.


def test_missing_values_are_not_forward_filled_or_replaced_with_zero(layer):
    table, _ = loaded(layer, [100, None, 110, 121])
    result = layer.analyze(table_name=table, metric="returns")
    assert result["coverage"]["invalid_value_rows"] == 1
    assert [row["value"] for row in chronological(result)[:3]] == [None, None, None]
    assert chronological(result)[-1]["value"] == pytest.approx(.1)


@pytest.mark.parametrize("metric", ["correlation", "beta"])
def test_constant_benchmark_is_undefined_rather_than_zero_or_a_fake_signal(layer, metric):
    asset, _ = loaded(layer, [100, 110, 99, 103.95], name="asset")
    reference, _ = loaded(layer, [100] * 4, name="reference")
    row = chronological(layer.analyze(table_name=asset, metric=metric, benchmark_table=reference, window=3))[-1]
    assert row["samples"] == 3 and row["value"] is None and "zero_variance" in row["status"]


def test_decimal_research_helper_avoids_decimal_arithmetic_without_rounding_source(layer):
    table, original = loaded(layer, [Decimal("100.123456789012345678"), Decimal("101.123456789012345678")])
    result = layer.analyze(table_name=table, metric="returns")
    expected = float(original["close"].iloc[1] / original["close"].iloc[0] - 1)
    assert chronological(result)[-1]["value"] == pytest.approx(expected)
    assert result["convention"]["numeric_mode"] == "approximate_double_for_research_only"
    assert layer._frames[table]["close"].iloc[0] == original["close"].iloc[0]


def test_invalid_numeric_values_are_counted_and_preserved_as_gaps(layer):
    table, _ = loaded(layer, ["100", "110", "bad", "NaN", "Infinity", "0", "-1", None])
    result = layer.analyze(table_name=table, metric="returns")
    assert result["coverage"]["invalid_value_rows"] == 6
    assert chronological(result)[1]["value"] == pytest.approx(.1)
    assert all(row["value"] is None for row in chronological(result)[2:])


def test_duplicate_timestamps_require_explicit_grouping_and_keep_free_sql_available(layer):
    dates = [pd.Timestamp("2026-08-01", tz="UTC")] * 2
    table, _ = loaded(layer, [100, 200], times=dates, groups=["ALFA", "BETA"])
    tools, context = served(layer)
    failed = tools["duckdb_analyze"](table_name=table, metric="returns")
    assert failed["tool_error"] and failed["error"]["code"] == "duplicate_observations"
    assert failed["fallback"]["available"] and failed["fallback"]["tool"] == "duckdb_query"
    # The model can decide the custom grouping/analysis itself; no mode switch
    # or operator approval is needed and no SQL is rewritten on its behalf.
    custom = tools["duckdb_query"](sql=f'SELECT symbol,AVG(close) AS custom_average FROM "{table}" GROUP BY symbol ORDER BY symbol')
    assert custom["rows"] == [{"symbol": "ALFA", "custom_average": 100}, {"symbol": "BETA", "custom_average": 200}]
    assert [call["ok"] for call in context["tool_calls"]] == [False, True]


def test_unsupported_research_can_fall_back_to_arbitrary_complete_sql(layer):
    table, _ = loaded(layer, [100, 101, 102])
    tools, _ = served(layer)
    failure = tools["duckdb_analyze"](table_name=table, metric="custom_regime_test")
    assert failure["error"]["code"] == "unsupported_metric"
    success = tools["duckdb_query"](sql=f'WITH own_research AS (SELECT close FROM "{table}") SELECT MEDIAN(close) AS result FROM own_research')
    assert success["rows"] == [{"result": 101}]


def test_historical_future_endpoints_stay_unavailable_even_in_an_already_loaded_document(layer):
    layer.strategy.is_backtesting = True
    layer.strategy.get_datetime = lambda: datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
    table, frame = loaded(layer, [100, 110, 999999, 9999999])
    result = layer.analyze(table_name=table, metric="forward_returns")
    rows = chronological(result)
    assert result["coverage"]["future_rows_withheld"] == 2 and result["row_count"] == 2
    assert rows[0]["value"] == pytest.approx(.1) and rows[1]["value"] is None
    assert frame["close"].iloc[-1] == 9999999


def test_naive_market_time_needs_explicit_timezone_for_historical_comparison(layer):
    layer.strategy.is_backtesting = True
    layer.strategy.get_datetime = lambda: datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)
    times = pd.date_range("2026-11-16 09:29", periods=3, freq="min")
    table, _ = loaded(layer, [100, 101, 999999], times=times)
    with pytest.raises(AnalysisUnsupported, match="time_zone"):
        layer.analyze(table_name=table, metric="returns")
    result = layer.analyze(table_name=table, metric="returns", time_zone="America/New_York")
    assert result["row_count"] == 2 and result["coverage"]["future_rows_withheld"] == 1
    assert chronological(result)[-1]["value"] == pytest.approx(.01)


def test_live_document_statistics_have_no_historical_date_ceiling(layer):
    table, _ = loaded(layer, [100, 110], times=pd.date_range("2027-01-01", periods=2, tz="UTC"))
    result = layer.analyze(table_name=table, metric="returns")
    assert result["row_count"] == 2 and result["coverage"]["future_rows_withheld"] == 0


def test_result_limit_reports_total_without_losing_latest_observations(layer):
    table, _ = loaded(layer, [100, 110, 121, 133.1])
    result = layer.analyze(table_name=table, metric="returns", limit=2)
    assert result["row_count"] == 4 and len(result["rows"]) == 2 and result["truncated"]
    assert result["rows"][0]["datetime"] > result["rows"][1]["datetime"]


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True])
def test_annualization_never_silently_infers_a_market_or_accepts_invalid_values(layer, bad):
    table, _ = loaded(layer, [100, 101])
    with pytest.raises(AnalysisUnsupported, match="annualization"):
        layer.analyze(table_name=table, metric="volatility", annualization=bad)


def test_independent_accounts_do_not_share_same_named_tables_or_research_results(layer):
    other = DuckDBQueryLayer(SimpleNamespace(is_backtesting=False, get_datetime=lambda: NOW))
    try:
        first, _ = loaded(layer, [100, 110])
        second, _ = loaded(other, [100, 90])
        assert first == second
        assert chronological(layer.analyze(table_name=first, metric="returns"))[-1]["value"] == pytest.approx(.1)
        assert chronological(other.analyze(table_name=second, metric="returns"))[-1]["value"] == pytest.approx(-.1)
    finally:
        other.connection.close()


def test_optional_statistics_and_free_sql_are_all_in_default_builtin_inventory():
    tools = {tool.name: tool for tool in BuiltinTools.all()}
    assert {"duckdb_describe", "duckdb_analyze", "duckdb_query"} <= set(tools)
    assert all(not tools[name].metadata.get("mutates_trading") for name in ("duckdb_describe", "duckdb_analyze"))


@pytest.mark.parametrize("bad_sql,error_type", [
    ("SELECT nonexistent_column FROM {table}", "BinderException"),
    ("SELECT AVG(LAG(close) OVER (ORDER BY datetime)) FROM {table}", "BinderException"),
    ("SELECT 100.12345678901234567890 * 100.12345678901234567890 AS product", "OutOfRangeException"),
])
def test_original_error_is_not_hidden_or_automatically_rewritten(layer, bad_sql, error_type):
    import hashlib
    table, _ = loaded(layer, [100, 101, 102])
    tools, _ = served(layer)
    sql = bad_sql.format(table=table)
    failure = tools["duckdb_query"](sql=sql)
    assert failure["tool_error"] and failure["error"]["type"] == error_type
    assert failure["sql_sha256"] == hashlib.sha256(sql.encode()).hexdigest()
    assert layer.get_metrics()["query_attempts"] == 1  # No implicit repair/retry.
    correct = tools["duckdb_query"](sql="SELECT CAST(100.12345678901234567890 AS DOUBLE) * CAST(100.12345678901234567890 AS DOUBLE) AS product")
    assert correct["rows"][0]["product"] == pytest.approx(float(Decimal("100.12345678901234567890") ** 2))


def test_schema_feedback_failure_cannot_replace_the_original_query_error(layer, monkeypatch):
    loaded(layer, [100, 101])
    tools, _ = served(layer)
    monkeypatch.setattr(layer, "describe", lambda **kw: (_ for _ in ()).throw(RuntimeError("synthetic unavailable schema")))
    failed = tools["duckdb_query"](sql="SELECT definitely_missing_column")
    assert failed["error"]["type"] == "BinderException" and failed["tables"][0]["schema_unavailable"]


def test_quoted_column_names_remain_identifiers_and_sources_are_not_mutated(layer):
    table, _ = loaded(layer, [100, 110], column='price" with space')
    result = layer.analyze(table_name=table, metric="returns", value_column='price" with space')
    assert chronological(result)[-1]["value"] == pytest.approx(.1)
    assert layer.describe(table_name=table)["columns"] == ["datetime", 'price" with space']


def test_date_only_historical_cutoff_discloses_its_coarse_basis(layer):
    from datetime import date
    layer.strategy.is_backtesting = True
    layer.strategy.get_datetime = lambda: datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
    table, _ = loaded(layer, [100, 110, 999999], times=[date(2026, 8, day) for day in (1, 2, 3)])
    result = layer.analyze(table_name=table, metric="returns")
    assert result["coverage"]["historical_cutoff_basis"] == "strategy_date_only"
    assert result["row_count"] == 2 and result["coverage"]["future_rows_withheld"] == 1


def test_native_market_loading_and_default_readonly_handle_serve_the_new_tools(tmp_path, monkeypatch):
    from scripts.agent_eval_production_fixture import ProductionFixture
    from scripts.run_agent_evals import build_fixture
    # Binding the native tool inventory can initialize FRED's local cache.
    monkeypatch.setenv("LUMIBOT_FRED_CACHE_DIR", str(tmp_path / "fred"))
    fixture = ProductionFixture(build_fixture("orb_breakout"))
    try:
        case = {"model": "chatgpt/gpt-6-luna", "fixture": "orb_breakout", "systemPrompt": "Synthetic research"}
        handle = fixture.create_agent(case, None, allow_trading=False)
        names = {tool.name for tool in handle._ensure_bound_tools()}
        assert {"duckdb_query", "duckdb_describe", "duckdb_analyze"} <= names
        assert "orders_submit" not in names
        manager = fixture.manager
        definitions = [BuiltinTools.market.load_history_table(), BuiltinTools.duckdb.describe(), BuiltinTools.duckdb.analyze(), BuiltinTools.duckdb.query()]
        tools = {definition.name: _wrap_tool_callable(definition.binder(fixture.strategy, manager)) for definition in definitions}
        loaded_table = tools["market_load_history_table"](symbol="AAPL", length=30, timestep="minute", table_name="recorded")
        assert loaded_table["column_types"]["datetime"] and loaded_table["schema"]
        result = tools["duckdb_analyze"](table_name="recorded", metric="volatility", window=5)
        assert result["ok"] and any(row["status"] == "ok" for row in result["rows"])
        # The served tool JSON-serializes timestamps; compare parsed instants.
        assert max(pd.Timestamp(row["datetime"]) for row in result["rows"]) < fixture.strategy.get_datetime()
        assert all(row["table_name"] != loaded_table.get("source_table_name") for row in tools["duckdb_describe"]()["tables"])
        assert tools["duckdb_analyze"](table_name="recorded", metric="median")["tool_error"]
        custom = tools["duckdb_query"](sql="SELECT MEDIAN(close) AS own_calculation FROM recorded")
        assert custom["rows"][0]["own_calculation"] > 0 and not fixture.fixture.submissions
    finally:
        fixture.close()


def test_new_statistics_source_participates_in_eval_freshness(monkeypatch):
    from scripts import run_agent_evals as evals
    seen = []
    monkeypatch.setattr(evals, "sha256_files", lambda paths: seen.extend(paths) or "source")
    evals.runtime_fingerprint()
    assert evals.REPO_ROOT / "lumibot/components/agents/research_statistics.py" in seen


def test_empty_loaded_table_does_not_invent_a_timestamp_or_a_zero_result(layer):
    table, _ = loaded(layer, [], times=pd.DatetimeIndex([], tz="UTC"))
    result = layer.analyze(table_name=table, metric="returns")
    assert result["row_count"] == 0 and result["rows"] == []
    assert result["coverage"]["first_timestamp"] is None and result["coverage"]["last_timestamp"] is None


def test_schema_inspection_failure_preserves_document_research(layer):
    import duckdb
    original = layer.connection
    class MissingSchema:
        def __getattr__(self, name):
            return getattr(original, name)

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("DESCRIBE"):
                raise duckdb.CatalogException("Synthetic unavailable metadata")
            return original.execute(sql, *args, **kwargs)
    layer.connection = MissingSchema()
    frame = pd.DataFrame({"value": [1, 2]})
    info = layer.register_document_table("values", frame, source="synthetic")
    assert info["row_count"] == 2 and len(info["sample_rows"]) == 2
    assert info["schema_error"]["type"] and info["column_types"] == {}
    tools, _ = served(layer)
    failure = tools["duckdb_analyze"](table_name=info["table_name"], metric="returns")
    assert failure["error"]["code"] == "schema_unavailable" and failure["fallback"]["available"]


def test_eval_fixture_discovery_keeps_all_research_caches_in_its_artifacts_without_home(tmp_path, monkeypatch):
    import os
    from scripts.agent_eval_isolation import configure_fixture_environment
    from lumibot.macro.fred import FREDMacroData
    monkeypatch.setattr(os, "environ", {"PATH": os.environ.get("PATH", "")})
    configure_fixture_environment(tmp_path, artifact_root=tmp_path / "run")
    assert "HOME" not in os.environ
    expected = tmp_path / "run" / "runtime_cache" / "fred"
    assert os.environ["LUMIBOT_FRED_CACHE_DIR"] == str(expected)
    assert FREDMacroData().cache_dir == expected


def test_statistics_respects_caller_query_deadline_and_retains_free_sql_after_timeout(layer, monkeypatch):
    table, _ = loaded(layer, [100, 110, 121])
    tools, _ = served(layer)
    original = layer.query
    calls = []
    def deadline(*, sql, limit=200):
        calls.append(sql)
        raise TimeoutError("Synthetic caller-owned research deadline")
    monkeypatch.setattr(layer, "query", deadline)
    failure = tools["duckdb_analyze"](table_name=table, metric="returns")
    assert calls and failure["error"]["type"] == "TimeoutError" and failure["fallback"]["available"]
    monkeypatch.setattr(layer, "query", original)
    assert tools["duckdb_query"](sql=f"SELECT COUNT(*) AS n FROM {table}")["rows"] == [{"n": 3}]


def test_fallback_respects_explicit_tool_selection_without_granting_sql(layer):
    from lumibot.components.agents.tool_context import agent_tool_context
    bound = BuiltinTools.duckdb.analyze().binder(layer.strategy, SimpleNamespace(duckdb=layer))
    with agent_tool_context({"available_tool_names": ["duckdb_analyze"]}):
        result = bound.function(table_name="unused", metric="custom_research")
    assert result["tool_error"] and result["fallback"]["available"] is False
    # An absent inventory is unknown, rather than an invented availability claim.
    result = bound.function(table_name="unused", metric="custom_research")
    assert result["fallback"]["available"] is None
