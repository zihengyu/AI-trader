"""What the operator receives on Telegram: pre-market research, the session open and readable notices.

Synthetic journals and an in-memory Telegram double; nothing is sent.
"""
from datetime import datetime, timedelta, timezone

from scripts.autonomous_fund import stops
from scripts.autonomous_fund.monitor import Monitor
from .test_autonomous_fund_monitor import NOW, FakeTelegram, setup


def notices(journal, kind):
    return [r["body"]["details"] for r in journal.all_events()
            if r["kind"] == "notification" and r["body"]["kind"] == kind]


def test_completed_premarket_research_is_announced_with_its_summary(tmp_path):
    from .test_autonomous_fund_premarket import harness
    strategy, _ = harness(tmp_path)
    strategy.before_market_opens()
    [research] = notices(strategy.journal, "premarket_research")
    assert research["summary"].startswith("SCRIPTED") and research["phase"] == "closed_startup"
    assert research["session_open"] == "2026-11-02T14:30:00+00:00" and research["evidence_count"] == 1


def live_open(tmp_path, stopped):
    from .test_autonomous_fund_stability import live
    strategy = live(tmp_path)
    strategy.broker.is_market_open = lambda: True
    if not stopped:
        strategy.halt_path.unlink()
    return strategy


def test_the_first_iteration_of_a_session_announces_the_open_once(tmp_path):
    strategy = live_open(tmp_path, stopped=False)
    strategy.on_trading_iteration()
    strategy.on_trading_iteration()
    [opened] = notices(strategy.journal, "session_open")
    assert opened["stop"] is None and opened["account"]["equity"] == "10000"
    strategy.get_datetime = lambda: datetime(2026, 11, 3, 15, tzinfo=timezone.utc)  # The next session.
    strategy.on_trading_iteration()
    assert len(notices(strategy.journal, "session_open")) == 2


def test_the_open_is_announced_even_while_trading_is_stopped(tmp_path):
    strategy = live_open(tmp_path, stopped=True)
    strategy.on_trading_iteration()
    [opened] = notices(strategy.journal, "session_open")
    assert opened["stop"]["class"] == "operator"  # The operator learns that today will not trade.


def test_no_open_announcement_while_the_market_is_closed(tmp_path):
    strategy = live_open(tmp_path, stopped=False)
    strategy.broker.is_market_open = lambda: False
    strategy.on_trading_iteration()
    assert notices(strategy.journal, "session_open") == []


def delivered(tmp_path, kind, details, transport=None):
    journal = setup(tmp_path)
    journal.event(NOW, "notification", {"kind": kind, "details": details})
    transport = transport or Documents()
    Monitor(tmp_path, transport).poll(NOW + timedelta(seconds=1))
    return transport


class Documents(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.documents = []

    def send_document(self, caption, filename, document):
        self.documents.append((caption, filename, document))
        return {"ok": True, "message_id": 500 + len(self.documents)}


def test_premarket_research_reads_as_a_message_not_json(tmp_path):
    bot = delivered(tmp_path, "premarket_research", {
        "session_open": "2026-11-02T14:30:00+00:00", "phase": "before_market_opens",
        "summary": "科技股财报季前景偏强，利率预期稳定。", "evidence_count": 2,
        "evidence": [{"source": "Synthetic filing", "available_at": "2026-11-02T12:00:00+00:00",
                      "observed_at": "2026-11-02T13:00:00+00:00"}]})
    [text] = bot.messages
    assert text.startswith("📊 盘前研究") and "科技股财报季前景偏强" in text and "Synthetic filing" in text
    assert "{" not in text and "开盘前" in text


def test_a_long_research_summary_is_sent_as_a_document(tmp_path):
    bot = delivered(tmp_path, "premarket_research", {
        "session_open": "2026-11-02T14:30:00+00:00", "phase": "before_market_opens",
        "summary": "长篇研究。" * 2000, "evidence_count": 0, "evidence": []})
    assert not bot.messages and len(bot.documents) == 1
    caption, filename, document = bot.documents[0]
    assert caption.startswith("📊 盘前研究") and filename.endswith(".txt") and document.count("长篇研究") == 2000


def test_the_session_open_message_says_whether_today_trades(tmp_path):
    bot = delivered(tmp_path, "session_open", {
        "session_date": "2026-11-02", "mode": "local", "close": "2026-11-02T21:00:00+00:00",
        "stop": None, "account": {"equity": "10000", "cash": "9000", "positions": {"SPY": "2"}},
        "research_summary": "关注大盘科技股。"})
    [text] = bot.messages
    assert text.startswith("🔔 美股开盘") and "交易：运行中" in text and "SPY×2" in text and "关注大盘科技股" in text
    stopped = delivered(tmp_path / "stopped", "session_open", {
        "session_date": "2026-11-02", "mode": "local", "close": None,
        "stop": {"class": "operator", "reason": "operator_stop"}, "account": None, "research_summary": None})
    assert "交易：暂停（人工停止：stop 命令）" in stopped.messages[0] and "fund-start.sh" in stopped.messages[0]


def test_common_notices_are_readable(tmp_path):
    for kind, details, expected in [
        ("stop_active", {"class": "service", "reason": "service_start", "orders": "no new orders"}, "⏸ 交易已停止"),
        ("auto_resumed", {"stop_reason": "heartbeat_stale", "stopped_at": None, "healthy_since": None}, "▶️ 已自动恢复交易"),
        ("watchdog_stop", {"reason": "heartbeat_stale", "action": "STOP written"}, "🚨 监控停止交易：心跳超时"),
        ("bot_crash", "RuntimeError", "🚨 交易进程崩溃"),
        ("blocked", "Stale quote", "⚠️ 本轮未执行"),
    ]:
        bot = delivered(tmp_path / kind, kind, details)
        assert bot.messages[0].startswith(expected), bot.messages[0]
    unknown = delivered(tmp_path / "unknown", "something_new", {"x": 1})
    assert "something_new" in unknown.messages[0]  # Anything else still arrives, as before.


# ----- Intraday portfolio changes ----------------------------------------------------------------

def deciding(tmp_path):
    from .test_autonomous_fund_quant import harness
    strategy = harness(tmp_path, mode="local")
    strategy.log_message = lambda *args, **kwargs: None
    strategy.notify = lambda kind, details: strategy.journal.event(NOW, "notification", {"kind": kind, "details": details})
    return strategy


def decision(targets, rationale="合成理由"):
    return {"targets": targets, "rationale": rationale, "next_review_at": "2026-11-02T16:00:00+00:00"}


def test_the_operator_hears_when_the_ai_changes_the_portfolio_not_on_every_review(tmp_path):
    strategy = deciding(tmp_path)
    day = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    strategy._announce_decision(decision([{"symbol": "SPY", "weight": 0.10}]), day)  # First of the day.
    strategy._announce_decision(decision([{"symbol": "SPY", "weight": 0.105}]), day + timedelta(minutes=20))
    assert len(notices(strategy.journal, "decision_changed")) == 1  # Half a point: not a change worth a message.
    strategy._announce_decision(decision([{"symbol": "SPY", "weight": 0.10},
                                          {"symbol": "ALFA", "weight": 0.05, "stop_loss": 95}], "加入 ALFA"),
                                day + timedelta(minutes=40))
    changed = notices(strategy.journal, "decision_changed")[-1]
    assert {"symbol": "ALFA", "from": "0", "to": "0.05", "stop_loss": "95"} in changed["changes"]
    assert changed["rationale"] == "加入 ALFA" and changed["first_today"] is False
    strategy._announce_decision(decision([{"symbol": "ALFA", "weight": 0.05}]), day + timedelta(minutes=60))
    sold = notices(strategy.journal, "decision_changed")[-1]["changes"]
    assert {"symbol": "SPY", "from": "0.1", "to": "0"} in sold  # A sale is a change too.
    strategy._announce_decision(decision([{"symbol": "ALFA", "weight": 0.05}]), day + timedelta(days=1))
    assert notices(strategy.journal, "decision_changed")[-1]["first_today"] is True  # Each day's first plan.


def test_historical_runs_do_not_announce_decisions(tmp_path):
    strategy = deciding(tmp_path)
    strategy.mode = "mock"
    strategy._announce_decision(decision([{"symbol": "SPY", "weight": 0.10}]), NOW)
    assert notices(strategy.journal, "decision_changed") == []


def test_a_portfolio_change_reads_as_buys_sales_and_reasons(tmp_path):
    bot = delivered(tmp_path, "decision_changed", {
        "at": "2026-11-02T15:15:00+00:00", "first_today": False, "cash_weight": "0.85",
        "changes": [{"symbol": "NVDA", "from": "0", "to": "0.05", "entry_limit": "120", "stop_loss": "110"},
                    {"symbol": "XLE", "from": "0.06", "to": "0"}, {"symbol": "SPY", "from": "0.1", "to": "0.1"}],
        "rationale": "NVDA 财报超预期；能源股动能转弱。", "next_review_at": "2026-11-02T16:00:00+00:00"})
    [text] = bot.messages
    assert text.startswith("🧭 AI 调整目标组合")
    assert "买入／加仓：NVDA 0%→5%（入场 ≤ 120，止损 110）" in text and "卖出／减仓：XLE 6%→0%" in text
    assert "不变：SPY 10%" in text and "现金约 85%" in text and "NVDA 财报超预期" in text
    first = delivered(tmp_path / "first", "decision_changed", {
        "at": "2026-11-02T15:00:00+00:00", "first_today": True, "cash_weight": "0.9",
        "changes": [{"symbol": "SPY", "from": "0.1", "to": "0.1"}], "rationale": "维持。", "next_review_at": None})
    assert first.messages[0].startswith("🧭 今日首次复核：维持目标组合")
