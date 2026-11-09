"""V2 3.6: the weekly report from a synthetic ledger, its labels, missing sections and its delivery.

Synthetic account, prices, costs and review texts only; no network, broker or model inference.
"""
from datetime import datetime, timedelta, timezone

from scripts.autonomous_fund.execution import EXECUTION_REVISION
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.monitor import Monitor
from scripts.autonomous_fund.notifications import NO_DATA, week_of, weekly_document
from scripts.autonomous_fund.performance import PerformanceSettings
from tests.test_autonomous_fund_monitor import FakeTelegram, heartbeat

WEEK = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]


def close_of(day, minutes=0):
    year, month, dom = map(int, day.split("-"))
    return datetime(year, month, dom, 21, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)  # 16:00 New York.


def review(journal, day, summary, attribution=None, **extra):
    body = {"session_date": day, "completed_at": close_of(day, 30).isoformat(), "version": 1,
            "attribution": attribution,
            "review": {"summary": summary, "confidence": "low", "data_gaps": extra.pop("gaps", []),
                       "selection_adjustments": extra.pop("adjustments", []), "timing_adjustments": [],
                       "predictions": extra.pop("predictions", []), "strategy_memo": extra.pop("memo", "")}}
    journal.event(close_of(day, 30), "postmarket_review", body)
    with journal.connection() as db:
        return db.execute("SELECT max(sequence) FROM events").fetchone()[0]


def attribution(day, selection):
    return {"status": "complete", "session_date": day, "observed_return": selection + 0.001, "unexplained": 0.0001,
            "components": {"market": 0.0009, "selection": selection, "cash_equivalent": 0.0, "timing": 0.0,
                           "slippage": -0.00002, "fees": -0.00001}}


def ledger(tmp_path):
    journal = Journal(tmp_path, "synthetic-weekly-account")
    journal.event(close_of("2026-10-30", -600), "startup", {
        "mode": "local", "execution_revision": EXECUTION_REVISION,
        "performance": {"benchmarks": PerformanceSettings().definitions(), "data_costs_usd_per_month": "0"}})
    equity, spy = 10000.0, 500.0
    for day in ["2026-10-30"] + WEEK:
        journal.event(close_of(day), "account", {"as_of": "x", "cash": "0", "equity": str(equity), "positions": {}})
        journal.event(close_of(day), "benchmark", {"SPY": str(spy), "QQQ": "400", "SGOV": "100"})
        equity *= 1.003
        spy *= 1.001
    journal.event(close_of("2026-11-03", -300), "fill", {"broker_id": "b1", "symbol": "ALFA", "side": "buy",
                                                         "quantity": "4", "price": "100", "trade_cost": None})
    journal.event(close_of("2026-11-03", -290), "fill_reconciled", {"broker_id": "b1", "symbol": "ALFA", "side": "buy",
                                                                    "quantity": "10", "price": "100", "trade_cost": "0.5"})
    journal.event(close_of("2026-11-04", -400), "agent", {"name": "manager", "model": "m", "usage": {"x": 1},
                                                          "cost_usd": 0.25})
    journal.event(close_of("2026-11-04", -390), "agent", {"name": "researcher", "model": "m", "usage": {"x": 1},
                                                          "cost_usd": None})
    journal.event(close_of("2026-11-05", -100), "notification", {"kind": "decision_drought", "details": {}})
    journal.event(close_of("2026-11-05", -50), "learning_evaluation", {
        "champion": "memo", "memo_minus_baseline_1d_excess": {"pairs": 4, "t_stat": 0.4}})
    journal.event(close_of("2026-11-02", -50), "factor_negative_control", {
        "as_of": "2026-11-02", "in_sample_pass_rate": 0.05, "validated_rate": 0.0, "significance": 0.05})
    review(journal, "2026-11-04", "周三：选股贡献为正，样本不足。", attribution("2026-11-04", 0.002))
    sequence = review(journal, "2026-11-06", "周五：市场贡献为主。", attribution("2026-11-06", -0.001),
                      gaps=["没有收盘净值"], predictions=[{"symbol": "ALFA", "direction": "up", "horizon_sessions": 5}],
                      adjustments=[{"hypothesis": "动量延续", "validation": "五日后跑赢 SPY", "invalidated_when": "跌破均线"}],
                      memo="下周继续观察动量。")
    journal.event(close_of("2026-11-06", 31), "scorecard", {
        "session_date": "2026-11-06", "predictions": {"total": 3, "scored": 2, "hit_rate": 0.5},
        "ideas": {"total": 4, "scored": 2, "hit_rate": 1.0, "pending": 2, "brier": 0.1, "verdict": "insufficient_sample",
                  "minimum_sample": 30, "baselines": {"coin": {}, "always_up": {"hit_rate": 0.5}},
                  "selection": {"hit_rate": 0.5}, "by_confidence": {"0.6-0.70": {"scored": 2, "hit_rate": 1.0,
                                                                                 "mean_confidence": 0.65}}},
        "construction": {"champion": "ai", "test": {"pairs": 5, "t_stat": -0.2}}})
    journal.event(close_of("2026-11-06", 32), "factor_tests", {"session_date": "2026-11-06", "results": [
        {"name": "momentum", "expression": "rank(ret(21))", "status": "validated"},
        {"name": "noise", "expression": "rank(ret(2))", "status": "rejected"}]})
    return journal, sequence


def test_week_boundaries():
    assert week_of("2026-11-06") == ("2026-11-02", "2026-11-08", "2026-W45")
    assert week_of("2026-11-02")[2] == "2026-W45" and week_of("2027-01-01")[2] == "2026-W53"


def test_the_weekly_report_separates_facts_inference_judgment_and_hypotheses(tmp_path):
    _, sequence = ledger(tmp_path)
    filename, text = weekly_document(tmp_path, sequence)
    assert filename == "weekly-2026-W45.md"
    for heading in ("## 一、本周收益与风险【事实】", "## 二、交易成本和模型费用【事实】", "## 三、本周归因合计【事实，代码按收盘价计算】",
                    "## 四、预测与研究想法打分【统计推断】", "## 五、因子实验进展【统计推断】", "## 六、对照实验状态【统计推断】",
                    "## 七、异常和缺失数据【事实】", "## 八、AI 本周复盘要点【模型判断】", "## 九、下周关注的假设【待验证假设】"):
        assert heading in text
    assert "本周收益 1.51%（起点：上周最后一次估值，2026-10-30）" in text  # 1.003^5 - 1.
    spy_id = PerformanceSettings().definitions()[0]["id"]
    assert f"基准 {spy_id}：0.50%" in text  # 1.001^5 - 1, rounded.
    assert "成交 1 笔，金额 1000.00 美元，已记录费用 0.50 美元" in text  # The reconciled order only.
    assert "模型调用 2 次，估算费用 0.2500 美元（另有 1 次未定价，不按 0 计）" in text
    assert "- 选股：0.10%" in text and "2 个交易日相加" in text
    assert "命中率 50.00%" in text and "研究想法：共 4 条" in text and "把握 0.6-0.70：2 条" in text
    assert "本周检验：历史检验通过（待样本外确认） 1、未通过 1" in text and "历史检验通过（待样本外确认）：momentum" in text
    assert "负对照（2026-11-02）" in text
    assert "组合构建对照：当前 ai，配对 5 个交易日" in text and "学习对照（影子 A/B）：当前 memo，配对 4 个交易日" in text
    assert "decision_drought × 1" in text and "没有收盘净值" in text and "本周完成复盘 2 次" in text
    assert "2026-11-04（置信度 low）：周三" in text and "动量延续（验证条件：五日后跑赢 SPY" in text
    assert "待打分预测 1 条：ALFA 看涨 5 日" in text and "下周继续观察动量。" in text


def test_sections_without_data_say_so(tmp_path):
    journal = Journal(tmp_path, "synthetic-weekly-account")
    sequence = review(journal, "2026-11-06", "只有一次复盘。")
    _, text = weekly_document(tmp_path, sequence)
    section = lambda title: text.split(title, 1)[1].split("\n## ", 1)[0]  # noqa: E731
    assert NO_DATA in section("一、本周收益与风险【事实】")
    assert NO_DATA in section("三、本周归因合计") and NO_DATA in section("四、预测与研究想法打分")
    assert f"本周检验：{NO_DATA}" in text and f"负对照：{NO_DATA}" in text
    assert f"组合构建对照：{NO_DATA}" in text and f"模型调用：{NO_DATA}" in text
    assert "2026-11-06：这次复盘没有归因" in text and f"调整：{NO_DATA}" in text


class DocumentTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.documents = []

    def send_document(self, caption, filename, document):
        self.documents.append((caption, filename, document))
        return {"ok": True, "message_id": len(self.documents)}


def test_the_monitor_sends_one_weekly_report_after_the_weeks_last_session(tmp_path, monkeypatch):
    import scripts.autonomous_fund.notifications as notifications
    monkeypatch.setattr(notifications, "daily_document", lambda root, sequence: ("daily.md", "daily"))
    journal, _ = ledger(tmp_path)
    transport = DocumentTelegram()
    monitor = Monitor(tmp_path, transport)
    for minutes in (45, 50, 60, 120, 600):
        at = close_of("2026-11-06", minutes)
        heartbeat(tmp_path, at)
        monitor.poll(at)
    weekly = [d for d in transport.documents if d[1].startswith("weekly-")]
    assert [d[1] for d in weekly] == ["weekly-2026-W45.md"] and "2026-W45 周报" in weekly[0][0]
    assert [d[1] for d in transport.documents].count("daily.md") == 2  # Wednesday's and Friday's daily reports.
    rows = [r["body"]["details"] for r in journal.all_events()
            if r["kind"] == "notification" and r["body"]["kind"] == "weekly_report"]
    assert rows == [{"review_sequence": rows[0]["review_sequence"], "week": "2026-W45", "session_date": "2026-11-06"}]


def test_the_week_end_follows_the_exchange_calendar(tmp_path):
    Journal(tmp_path, "synthetic-weekly-account")
    monitor = Monitor(tmp_path)
    assert monitor._week_ends("2026-11-06") and not monitor._week_ends("2026-11-04")
    assert monitor._week_ends("2026-04-02")  # Good Friday 2026: Thursday ends the week.
    assert not monitor._week_ends("2026-04-01")
