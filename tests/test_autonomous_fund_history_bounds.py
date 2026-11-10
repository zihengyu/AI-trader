"""Out-of-history warning: is the latest session outside the history the risk estimates came from?

Synthetic closes only; no network, broker or model inference.
"""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from scripts.autonomous_fund.history_bounds import MIN_NAMES, WINDOW, assess
from scripts.autonomous_fund.risk import trading_day
from tests.test_autonomous_fund_quant import NOW, harness

PATTERN = [0.004, -0.003, 0.006, -0.005, 0.002, -0.001, 0.003, -0.004, 0.005, -0.002, 0.001]  # Repeats: nothing new.


def calendar(n):
    start = date(2024, 1, 2)
    return [start + timedelta(days=i) for i in range(n)]


def series(returns, days=None):
    days = days or calendar(len(returns) + 1)
    price, rows = 100.0, [(str(days[0]), 100.0)]
    for day, move in zip(days[1:], returns):
        price *= 1 + move
        rows.append((str(day), price))
    return rows


def calm(n):
    return [PATTERN[i % len(PATTERN)] for i in range(n)]


def stocks(n, *, common_tail=0, names=MIN_NAMES):
    """Stocks with their own (periodic, shifted) moves; the last `common_tail` moves all follow one factor."""
    days = calendar(n + 1)
    out = {}
    for k in range(names):
        moves = [PATTERN[(i + 3 * k) % len(PATTERN)] for i in range(n)]
        for i in range(n - common_tail, n):
            moves[i] = PATTERN[i % len(PATTERN)] * 3 + 0.0001 * k
        out[f"S{k}"] = series(moves, days)
    return out


def test_a_calm_market_repeating_its_history_is_normal():
    card = assess(series(calm(400)), stocks(400))
    assert card["status"] == "normal" and card["beyond_risk_window"] == card["beyond_history"] == []
    assert set(card["metrics"]) == {"benchmark_vol_20d", "benchmark_return_1d", "benchmark_drawdown",
                                    "stock_mean_correlation_20d"}
    assert card["metrics"]["stock_mean_correlation_20d"]["history"]["sessions"] >= 60
    assert "no limit or order changes" in card["meaning"]


def test_a_crash_day_beyond_everything_is_flagged_on_each_measure():
    moves = calm(399) + [-0.08]
    card = assess(series(moves), stocks(400, common_tail=WINDOW))
    assert card["status"] == "beyond_history"
    assert {"benchmark_return_1d", "benchmark_drawdown", "benchmark_vol_20d",
            "stock_mean_correlation_20d"} <= set(card["beyond_history"])
    day = card["metrics"]["benchmark_return_1d"]
    assert day["value"] == pytest.approx(-0.08) and day["history"]["percentile"] == 0.0


def test_a_shock_older_than_the_risk_window_separates_the_two_flags():
    moves = calm(400)
    moves[50] = -0.10  # Inside the history, outside the last 252 sessions.
    moves[-1] = -0.05  # Larger than anything in the risk window, smaller than the old shock.
    card = assess(series(moves), risk_window=252)
    assert card["status"] == "beyond_risk_window" and "benchmark_return_1d" in card["beyond_risk_window"]
    assert "benchmark_return_1d" not in card["beyond_history"]
    assert card["metrics"]["benchmark_return_1d"]["risk_window"]["sessions"] == 252  # The 252 sessions before.


def test_short_histories_and_thin_cross_sections_are_unavailable_never_normal():
    assert assess(series(calm(50)))["status"] == "unavailable"
    card = assess(series(calm(400)), stocks(400, names=MIN_NAMES - 1))
    assert card["metrics"]["stock_mean_correlation_20d"] == {"status": "unavailable"}
    assert card["status"] == "normal"  # The benchmark measures were still compared.


def strategy_with(tmp_path, benchmark_moves, *, prepare=True):
    strategy = harness(tmp_path)
    days = calendar(len(benchmark_moves) + 1)
    bars = {"SPY": [(d, c, 1_000_000) for (_, c), d in zip(series(benchmark_moves, days), days)]}
    strategy._daily_bars = lambda symbol, now: (bars.get(symbol, []), None if symbol in bars else "no_history")
    strategy._may_prepare = lambda: prepare
    strategy._expire_history_failures = lambda now: None
    return strategy


def test_the_operator_is_told_when_the_market_leaves_its_history_and_when_it_is_back(tmp_path):
    crash = calm(399) + [-0.08]
    strategy = strategy_with(tmp_path, crash)
    result = strategy._market_history(NOW)
    assert result["status"] == "beyond_history"
    [(kind, details)] = strategy.notices
    assert kind == "market_out_of_history" and details["values"]["benchmark_return_1d"] == pytest.approx(-0.08)
    assert strategy._market_history(NOW) is result and len(strategy.notices) == 1  # Cached for the session.
    again = strategy_with(tmp_path, crash)
    again._market_history(NOW)
    assert again.notices == []  # The same state after a restart is not repeated.
    back = strategy_with(tmp_path, calm(400))
    assert back._market_history(NOW)["status"] == "normal"
    assert [kind for kind, _ in back.notices] == ["market_back_within_history"]


def test_history_not_read_yet_waits_and_sends_nothing(tmp_path):
    strategy = strategy_with(tmp_path, calm(399) + [-0.08], prepare=False)
    assert strategy._market_history(NOW)["status"] == "pending" and strategy.notices == []


def test_history_bounds_recompute_when_a_temporary_read_failure_expires(tmp_path):
    strategy = strategy_with(tmp_path, calm(399) + [-0.08])
    strategy.mode = "local"
    del strategy._expire_history_failures  # Exercise the runner's real invalidation.
    read_bars = strategy._daily_bars
    strategy._daily_bars = lambda symbol, now: ([], "temporary")
    assert strategy._market_history(NOW)["status"] == "unavailable"
    strategy._history_retry_at = {(trading_day(NOW), "SPY"): 0}
    strategy._daily_bars = read_bars

    assert strategy._market_history(NOW)["status"] == "beyond_history"
    assert [kind for kind, _ in strategy.notices] == ["market_out_of_history"]


def test_history_only_alert_lists_metrics_when_the_risk_window_is_too_short(tmp_path):
    from scripts.autonomous_fund.notifications import readable

    strategy = strategy_with(tmp_path, calm(399) + [-0.08])
    strategy.quant = replace(strategy.quant, es_lookback_days=30, min_history_days=20)
    result = strategy._market_history(NOW)

    assert result["beyond_history"] and not result["beyond_risk_window"]
    [(kind, details)] = strategy.notices
    assert details["values"].keys() == set(result["beyond_history"])
    assert "基准单日涨跌 -8.00%（超出全部历史）" in readable(kind, details)


def test_notice_text_says_nothing_changes_automatically():
    from scripts.autonomous_fund.notifications import readable
    text = readable("market_out_of_history", {
        "as_of_close": "2026-11-09", "status": "beyond_history", "beyond_risk_window": ["benchmark_return_1d",
                                                                                         "stock_mean_correlation_20d"],
        "beyond_history": ["benchmark_return_1d"], "risk_window_sessions": 252, "history_sessions": 504,
        "values": {"benchmark_return_1d": -0.08, "stock_mean_correlation_20d": 0.82}})
    assert "基准单日涨跌 -8.00%（超出全部历史）" in text and "平均相关性 0.82（超出风险模型窗口）" in text
    assert "不会因此自动改变" in text and "252" in text
