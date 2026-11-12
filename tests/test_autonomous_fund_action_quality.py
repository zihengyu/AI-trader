"""Synthetic corporate-action observations exercise the provider and research path."""
from datetime import date, timedelta
from types import SimpleNamespace

from scripts.autonomous_fund.action_quality import KEY, compare, refresh
from scripts.autonomous_fund.datastore import DailyStore
from scripts.autonomous_fund.history import AlpacaDailyHistory


def test_alpaca_parses_pages_filters_ex_dates_and_retains_unresolved_currency():
    calls = []
    split = {"symbol": "ALFA", "ex_date": "2026-10-29", "old_rate": "5", "new_rate": "1", "id": "one"}
    dividend = {"symbol": "ALFA", "ex_date": "2026-10-28", "rate": "0.4", "foreign": False}

    def get(url, headers, params, timeout):
        calls.append((url, params))
        if not params.get("page_token"):
            result = {"corporate_actions": {"reverse_splits": [split]}, "next_page_token": "p2"}
        else:
            result = {"corporate_actions": {"reverse_splits": [split], "cash_dividends": [
                dividend, {**dividend, "foreign": True}, {**dividend, "ex_date": "2026-11-03"}]}}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)

    api = AlpacaDailyHistory("synthetic-key", "synthetic-secret", get=get)
    result = api.corporate_actions(["ALFA", "BETA"], date(2026, 10, 1), date(2026, 11, 2))
    # The foreign observation is present even without enough currency information to compare it.
    assert result == {"ALFA": [{"date": "2026-10-29", "split": "0.2"},
                               {"date": "2026-10-28", "dividend": "0.4"},
                               {"date": "2026-10-28", "unverified_dividend": {
                                   "rate": "0.4", "currency": None, "foreign": True,
                               }}], "BETA": []}
    assert calls[0][0].endswith("/v1/corporate-actions") and calls[0][1]["symbols"] == "ALFA,BETA"
    assert calls[1][1]["page_token"] == "p2" and calls[1][1]["end"] == "2026-11-02"


def test_corporate_actions_compare_on_same_share_basis_and_keep_unconfirmed_separate():
    start, end = date(2026, 10, 1), date(2026, 11, 2)
    split = {"date": "2026-10-29", "split": "2"}
    left = [{"date": "2026-10-20", "dividend": "0.20"}, split]
    right = [{"date": "2026-10-20", "dividend": "0.40"}, split]
    assert compare(left, right, start, end)["status"] == "agree"
    assert compare(left, right[:1], start, end)["status"] == "unconfirmed"
    worse = [{**right[0], "dividend": "0.60"}, split]
    verdict = compare(left, worse, start, end)
    assert verdict["status"] == "unreliable"
    assert verdict["discrepancies"][0]["yahoo_ex_date_basis"] == "0.40"


def test_published_corporate_mismatch_invalidates_cached_research_and_notifies(tmp_path):
    from scripts.autonomous_fund.notifications import readable
    from scripts.autonomous_fund.risk import trading_day
    from tests.test_autonomous_fund_quant import NOW, default_frames, harness

    strategy = harness(tmp_path, default_frames(), mode="local")
    strategy._factor_cache = None
    strategy.daily_store = store = DailyStore(tmp_path / "daily")
    assert "ALFA" in strategy._factor_panel(NOW)["stocks"]["close"]
    strategy.journal.event(NOW - timedelta(days=5), "decision_validated", {
        "targets": [{"symbol": "ALFA", "weight": 0.1}]})
    before = strategy._scorecards(NOW)
    assert before["decisions"]["by_horizon"]["1"]["observed"] == 1
    day = trading_day(NOW)
    action = {"date": "2026-10-30", "split": "2"}
    source = SimpleNamespace(history_actions=lambda asset, length: [action] if asset.symbol == "ALFA" else [])
    reference = SimpleNamespace(corporate_actions=lambda symbols, start, end: {
        "ALFA": [{**action, "split": "3"}], "BETA": [action]})
    assert refresh(store, day, ["ALFA", "BETA"], 30, source, reference)["statuses"] == {
        "unreliable": 1, "unconfirmed": 1}
    assert store.get(day, KEY)["ALFA"]["status"] == "unreliable"
    assert strategy._daily_bars("ALFA", NOW) == ([], "bar_quality_unreliable")
    assert "ALFA" not in strategy._factor_panel(NOW)["stocks"]["close"]
    assert strategy._scorecards(NOW)["decisions"]["by_horizon"]["1"]["observed"] == 0
    assert strategy._daily_bars("BETA", NOW)[0]  # One-sided observations do not withhold data.
    notice = strategy._report_bar_quality(NOW)
    assert notice["withheld"] == ["ALFA"] and notice["corporate_actions"]["unconfirmed"] == {"BETA": 1}
    assert "拆股比例" in readable("bar_quality_issues", notice)
    assert "BETA 2026-10-30 拆股 · Yahoo 缺少同日记录" in readable("bar_quality_issues", notice)


def test_provider_currency_evidence_updates_a_previous_missing_notice_without_confirming_the_amount(tmp_path):
    from scripts.autonomous_fund.notifications import readable
    from scripts.autonomous_fund.risk import trading_day
    from tests.test_autonomous_fund_quant import NOW, default_frames, harness

    strategy = harness(tmp_path, default_frames(), mode="local")
    strategy.daily_store = store = DailyStore(tmp_path / "daily")
    day, ex_date = trading_day(NOW), "2026-10-30"
    dividend = {"ex_date": ex_date, "rate": "0.60", "foreign": True}
    records = [
        {**dividend, "symbol": "ALFA", "currency": None},
        {**dividend, "symbol": "ALFA", "currency": "USD"},
        {**dividend, "symbol": "BETA", "currency": "USD"},
        {**dividend, "symbol": "CHAR", "foreign": False, "currency": ""},
        {**dividend, "symbol": "DELTA", "currency": "EUR", "rate": None},
    ]
    payload = {"corporate_actions": {"cash_dividends": records}}
    reference = AlpacaDailyHistory("synthetic-key", "synthetic-secret", get=lambda *a, **kw: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: payload))
    source = SimpleNamespace(history_actions=lambda asset, length: [{"date": ex_date, "dividend": "0.60"}])
    store.put(day, KEY, {"ALFA": {"status": "unconfirmed", "discrepancies": [], "unconfirmed": [
        {"date": ex_date, "kind": "dividend", "reason": "one_source_only", "yahoo": "0.60", "alpaca": None},
    ]}})

    symbols = ["ALFA", "BETA", "CHAR", "DELTA", "ECHO"]
    assert refresh(store, day, symbols, 30, source, reference)["statuses"] == {"unconfirmed": 4, "agree": 1}
    saved = store.get(day, KEY)
    [alfa] = saved["ALFA"]["unconfirmed"]
    assert alfa["reason"] == "currency_not_comparable" and alfa["alpaca"] == "0.60"
    assert alfa["alpaca_currency_observations"] == [{"rate": "0.60", "currency": None, "foreign": True}]
    assert saved["CHAR"]["unconfirmed"][0]["reason"] == "currency_not_comparable"
    assert saved["DELTA"]["unconfirmed"][0]["alpaca_currency_observations"] == [
        {"rate": None, "currency": "EUR", "foreign": True},
    ]
    assert saved["ECHO"]["unconfirmed"][0]["reason"] == "one_source_only"
    assert saved["BETA"]["status"] == "agree"
    notice = strategy._report_bar_quality(NOW)
    assert notice["withheld"] == []
    text = readable("bar_quality_issues", notice)
    assert f"ALFA {ex_date} 现金分红 · Alpaca 有同日记录，币种口径待核对" in text
    assert f"ECHO {ex_date} 现金分红 · Alpaca 缺少同日记录" in text


def test_unconfirmed_notice_identifies_missing_alpaca_and_uncertain_share_basis():
    from scripts.autonomous_fund.notifications import readable

    notice = {"corporate_actions": {"unconfirmed": {"ALFA": 1, "BETA": 1}, "unconfirmed_examples": [
        {"symbol": "ALFA", "date": "2026-10-26", "kind": "dividend", "reason": "one_source_only",
         "yahoo": "1.25", "alpaca": None},
        {"symbol": "BETA", "date": "2026-10-27", "kind": "dividend", "reason": "uncertain_share_basis",
         "yahoo": "0.25", "alpaca": "0.5"},
    ]}}
    text = readable("bar_quality_issues", notice)
    assert "ALFA 2026-10-26 现金分红 · Alpaca 缺少同日记录" in text
    assert "BETA 2026-10-27 现金分红 · 每股计价口径待确认" in text
