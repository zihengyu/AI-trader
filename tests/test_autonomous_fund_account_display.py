"""Observed sleeve holdings reach Telegram and status without becoming target fills."""
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import pytest

from lumibot.entities import Asset
from scripts.autonomous_fund.holdings import average_prices
from scripts.autonomous_fund.report import report
from scripts.autonomous_fund.risk import Account, number
from scripts.autonomous_fund.status import account_lines, summary
from .test_autonomous_fund_autonomy import snapshot_harness
from .test_autonomous_fund_telegram_messages import deciding, decision, delivered, live_open, notices
from .test_autonomous_fund_monitor import NOW


def position(symbol, quantity, average):
    return SimpleNamespace(asset=Asset(symbol, asset_type="stock"), quantity=quantity, avg_fill_price=average)


def test_snapshot_retains_only_fully_observed_sleeve_entry_prices(tmp_path):
    strategy, state = snapshot_harness(tmp_path, 48000, 50000, {"TSLA": 10},
                                       {"capital_usd": "10000", "allow_external_holdings": True})
    strategy._snapshot()  # Establish the operator's frozen holdings before AI purchases.
    state.update(cash=47000)
    strategy.get_positions = lambda: [position("TSLA", 10, 150), position("ALFA", 10, "97.30")]
    account = strategy._snapshot()
    assert account.position_average_prices == {"ALFA": number("97.30")}
    assert account.positions == {"ALFA": number(10)}
    saved = report(tmp_path, write_files=False)["latest_account"]
    assert saved["position_average_prices"] == {"ALFA": "97.30"}
    assert saved["as_of"] == str(account.as_of)
    text = summary(tmp_path, "local", now=account.as_of)
    assert "ALFA：10 股 ｜ 买入均价 $97.30" in text and "TSLA" not in text


def test_missing_lot_prices_are_not_replaced_with_quotes_or_partial_averages():
    lots = [position("ALFA", 2, 90), position("ALFA", 3, 110),
            position("BETA", 1, 80), position("BETA", 1, None), position("GAMMA", 1, "NaN")]
    # A different asset class sharing a symbol must never enter a stock's cost.
    lots.append(SimpleNamespace(asset=Asset("ALFA", asset_type="forex"), quantity=1000, avg_fill_price=1))
    assert average_prices(lots, {"ALFA": 5, "BETA": 2, "GAMMA": 1}) == {"ALFA": number(102)}


def test_target_notice_has_observed_shares_and_cost_before_the_rationale(tmp_path):
    strategy = deciding(tmp_path)
    account = Account(number("9635.00"), number("10002.40"), {"BETA": number("4.0")},
                      NOW - timedelta(minutes=2), position_average_prices={"BETA": number("91.25")})
    strategy._announce_decision(decision([{"symbol": "BETA", "weight": .08}], "计划加仓，尚未成交"), NOW, account)
    [details] = notices(strategy.journal, "decision_changed")
    assert details["account"]["positions"] == {"BETA": "4.0"}
    [text] = delivered(tmp_path / "telegram", "decision_changed", details).messages
    assert "买入／加仓：BETA 0%→8%" in text and "目标现金约 92%" in text
    assert "BETA：4 股 ｜ 买入均价 $91.25" in text
    # Relative freshness measures the actual snapshot's age at delivery,
    # including delays; it must not restart at the notification's creation.
    # This delivery fixture is a Sunday: closed-session NAV has no running age.
    assert "净值 $10,002.40（休市） ｜ 现金 $9,635.00" in text
    from scripts.autonomous_fund.notifications import readable
    open_at = NOW + timedelta(days=1)
    delayed_details = {**details, "account": {**details["account"], "as_of": str(open_at - timedelta(minutes=2))}}
    delayed = readable("decision_changed", delayed_details, now=open_at + timedelta(minutes=5))
    assert "净值 $10,002.40（7分钟前）" in delayed
    assert text.index("目标现金") < text.index("📌 当前实际持仓") < text.index("理由：")
    assert details["account"]["as_of"] == str(account.as_of)


def test_open_announcement_preserves_position_cost_and_snapshot_time(tmp_path):
    strategy = live_open(tmp_path, stopped=False)
    account = Account(number(9000), number(10000), {"ALFA": 10}, NOW,
                      position_average_prices={"ALFA": number("97.30")})
    strategy._announce_session_open(NOW, account)
    [opened] = notices(strategy.journal, "session_open")
    assert opened["account"]["position_average_prices"] == {"ALFA": "97.30"}
    assert opened["account"]["as_of"] == str(NOW)


@pytest.mark.parametrize("account, expected", [
    (None, "暂无可用账户快照"),
    ({"unavailable": "ConnectionError"}, "暂无可用账户快照"),
    ({"positions": {}}, "暂无证券持仓"),
    ({"positions": {"ALFA": "1.25"}}, "ALFA：1.25 股 ｜ 买入均价 暂无"),
])
def test_missing_snapshots_and_legacy_costs_are_disclosed(account, expected):
    text = "\n".join(account_lines(account))
    assert expected in text and "$0.00" not in text


def test_cash_equivalents_survive_report_rebuild_and_remain_separate(tmp_path):
    strategy = deciding(tmp_path)
    account = Account(number(9000), number(10000), {"ALFA": 10}, NOW,
                      cash_equivalents={"BILLS": 80}, cash_equivalent_value=number(8000),
                      position_average_prices={"ALFA": number(99), "BILLS": number("99.80")})
    strategy.journal.event(NOW, "account", asdict(account))
    saved = report(tmp_path, write_files=False)["latest_account"]
    text = "\n".join(account_lines(saved))
    assert "现金 $9,000.00" in text and "现金中含短债 ETF $8,000.00" in text
    assert "BILLS：80 股 ｜ 买入均价 $99.80" in text
    assert saved["positions"] == {"ALFA": 10}


def test_trade_prices_keep_partial_fills_and_replace_callbacks_with_reconciliation(tmp_path):
    strategy = deciding(tmp_path)
    for offset, kind, body in [
        (-5, "fill", {"broker_id": "purchase", "symbol": "ALFA", "side": "buy", "quantity": "2", "price": "92"}),
        (-4, "fill", {"broker_id": "purchase", "symbol": "ALFA", "side": "buy", "quantity": "3", "price": "94"}),
        (-3, "fill_reconciled", {"broker_id": "purchase", "symbol": "ALFA", "side": "buy", "quantity": "5", "price": "93.20"}),
        (-2, "fill", {"broker_id": "sale", "symbol": "ALFA", "side": "sell", "quantity": "1", "price": "98"}),
        (-1, "fill", {"broker_id": "sale", "symbol": "ALFA", "side": "sell", "quantity": "1", "price": "99"}),
        (1, "fill", {"broker_id": "later", "symbol": "BETA", "side": "buy", "quantity": "1", "price": "10"}),
    ]:
        strategy.journal.event(NOW + timedelta(minutes=offset), kind, {"trade_cost": None, **body})
    account = Account(number(9700), number(10100), {"ALFA": 3}, NOW,
                      position_average_prices={"ALFA": number("93.20")})
    strategy.journal.event(NOW, "account", asdict(account))
    strategy._announce_decision(decision([{"symbol": "ALFA", "weight": .05}]), NOW, account)
    [details] = notices(strategy.journal, "decision_changed")
    trades = details["recent_trades"]
    assert [(row["side"], row["quantity"], row["price"]) for row in trades] == [
        ("buy", "5", "93.20"), ("sell", "1", "98"), ("sell", "1", "99")]
    assert report(tmp_path, write_files=False)["recent_trades"] == trades
    [text] = delivered(tmp_path / "telegram", "decision_changed", details).messages
    assert "买入 ALFA：5 股 ｜ 累计成交均价 $93.20" in text
    assert "卖出 ALFA：1 股 ｜ 成交价 $98.00" in text
    assert "卖出 ALFA：1 股 ｜ 成交价 $99.00" in text
    assert "BETA" not in text
    assert "成交价 $98.00" in summary(tmp_path, "local", now=NOW)


def test_the_entire_rationale_is_preserved_when_the_notice_needs_an_attachment(tmp_path):
    strategy = deciding(tmp_path)
    rationale = "完整的合成研究理由。" * 600 + "末尾验证条件必须保留"
    account = Account(number(10000), number(10000), {}, NOW)
    strategy._announce_decision(decision([{"symbol": "ALFA", "weight": .05}], rationale), NOW, account)
    [details] = notices(strategy.journal, "decision_changed")
    assert details["rationale"] == rationale
    transport = delivered(tmp_path / "telegram", "decision_changed", details)
    [caption, filename, document] = transport.documents[0]
    assert "末尾验证条件必须保留" in document
    assert "下次复核：" in document and "权重是上限" in document
    assert "📌 当前实际持仓" in caption
