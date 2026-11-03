"""The data desk (数据员): an always-on process that collects and prepares research data.

Synthetic frames, fake HTTP clients and neutral symbols only; no network, no model.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.autonomous_fund.datadesk import DataDesk, DeskSettings, DeskStatus, follow_list, intraday_pulse
from scripts.autonomous_fund.datastore import DailyStore
from scripts.autonomous_fund.journal import Journal, encode
from scripts.autonomous_fund.risk import RiskError

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)  # 10:00 New York, a regular session (Tuesday).
DAY = "2026-11-03"


def config(**sections):
    from scripts.autonomous_fund.strategy import configuration
    result = configuration(ROOT / "scripts" / "autonomous_fund" / "config.json", environ={})
    result.update(sections)
    return result


def minutes(start, count, first=100.0, step=0.1, volume=1000):
    index = pd.date_range(start, periods=count, freq="1min", tz="UTC")
    close = first + step * np.arange(count)
    return pd.DataFrame({"open": close - .05, "high": close + .2, "low": close - .2, "close": close,
                         "volume": volume}, index=index)


# ----- The intraday pulse ------------------------------------------------------------------------

def test_the_pulse_reports_the_session_so_far_against_completed_closes():
    yesterday = minutes("2026-11-02 20:50", 10, first=90.0)  # 15:50-15:59 New York.
    frames = {
        "ALFA": pd.concat([yesterday, minutes("2026-11-03 14:30", 29, first=101.0)]),
        "BETA": pd.concat([yesterday, minutes("2026-11-03 14:30", 29, first=89.0, step=-.1)]),
        "GAMMA": RiskError("Yahoo market data cooling down for GAMMA; no stale fallback"),
        "DELTA": yesterday,  # Nothing completed today yet.
        "STALE": pd.concat([yesterday, minutes("2026-11-03 14:30", 5, first=95.0)]),
        "SPY": pd.concat([yesterday, minutes("2026-11-03 14:30", 29, first=500.0)]),
    }
    pulse = intraday_pulse(frames, {"ALFA": ("2026-11-02", 100.0), "SPY": ("2026-11-02", 499.0)}, NOW)
    alfa, beta = pulse["symbols"]["ALFA"], pulse["symbols"]["BETA"]
    assert alfa["previous_close_source"] == "daily_close" and alfa["change"] == pytest.approx(103.8 / 100 - 1, abs=1e-5)
    assert alfa["day_open"] == pytest.approx(100.95) and alfa["day_volume"] == 29_000 and not alfa["stale"]
    # Without a completed daily close the previous session's last minute is used, and labeled as such.
    assert beta["previous_close_source"] == "last_minute_close" and beta["previous_close"] == pytest.approx(90.9)
    assert pulse["symbols"]["STALE"]["stale"] and "STALE" not in [m["symbol"] for m in pulse["leaders"] + pulse["laggards"]]
    assert pulse["unavailable"] == {"DELTA": "no completed minute today",
                                    "GAMMA": "Yahoo market data cooling down for GAMMA; no stale fallback"}
    assert "SPY" in pulse["gauges"] and "SPY" not in [m["symbol"] for m in pulse["leaders"]]
    assert pulse["leaders"][0]["symbol"] == "ALFA" and pulse["laggards"][0]["symbol"] == "BETA"
    assert pulse["breadth"] == {"up": 1, "down": 1, "unchanged": 0}


# ----- Scheduling ---------------------------------------------------------------------------------

def test_jobs_run_on_their_own_cadence_and_a_failing_job_backs_off_without_stopping_the_others(tmp_path):
    clock = [NOW]
    desk = DataDesk(tmp_path, config(), "local", clock=lambda: clock[0], environ={})
    ran = []
    for name in ("news", "intraday", "earnings", "bars", "snapshot"):
        setattr(desk, f"_job_{name}", lambda now, n=name: ran.append(n) or {"ok": True})

    def broken(now):
        ran.append("macro")
        raise ConnectionError("FRED down")
    desk._job_macro = broken
    desk.tick()
    assert sorted(ran) == ["bars", "earnings", "intraday", "macro", "news", "snapshot"]  # No SEC contact: no SEC job.
    state = DeskStatus(tmp_path).read()
    assert state["jobs"]["macro"]["error"] == "ConnectionError" and state["jobs"]["news"]["ok_at"]
    assert desk.next_run["news"] == NOW + timedelta(seconds=300) and desk.next_run["macro"] == NOW + timedelta(seconds=60)
    ran.clear()
    clock[0] = NOW + timedelta(seconds=61)
    desk.tick()
    assert ran == ["macro"]  # Only the failed job, after its backoff.
    assert desk.next_run["macro"] == clock[0] + timedelta(seconds=120)  # Doubling, never past the normal interval.
    status = DeskStatus(tmp_path)
    assert status.alive(clock[0]) and status.fresh("news", 600, clock[0])
    assert not status.fresh("macro", 600, clock[0]) and not status.alive(clock[0] + timedelta(minutes=10))
    saturday = datetime(2026, 11, 7, 15, tzinfo=timezone.utc)
    assert "intraday" not in desk.due(saturday)  # The pulse describes the regular session only.
    assert desk.interval("news", saturday) == 1200 and desk.interval("macro", saturday) == 4 * 3600


def test_a_job_with_nothing_to_do_yet_is_neither_a_success_nor_an_error(tmp_path):
    desk = DataDesk(tmp_path, config(), "local", clock=lambda: NOW, environ={})
    desk.run_job("news")  # No account journal yet.
    job = DeskStatus(tmp_path).read()["jobs"]["news"]
    assert job["note"].startswith("no account journal yet") and not job.get("error") and not job.get("ok_at")


def test_bars_and_snapshot_fill_the_store_once_and_the_snapshot_is_rewritten_only_on_change(tmp_path):
    from tests.test_autonomous_fund_prefetch import frame
    fetched = []
    source = SimpleNamespace(get_historical_prices=lambda asset, length, timestep="day": fetched.append(
        asset.symbol) or SimpleNamespace(df=frame()))
    desk = DataDesk(tmp_path, config(), "local", source=source, clock=lambda: NOW, environ={})
    first = desk._job_bars(NOW)
    assert first["failed"] == {} and first["fetched"] == len(fetched) and {"SPY", "IWM", "DIA"} <= set(fetched)
    assert desk._job_bars(NOW)["fetched"] == 0  # Already stored today.
    desk._job_snapshot(NOW)
    store = DailyStore(tmp_path / "cache" / "daily")
    version = store.version(DAY, "market_snapshot")
    snapshot = store.get(DAY, "market_snapshot")
    assert snapshot["benchmark"] == "SPY" and set(desk.tradable) <= set(snapshot["symbols"])
    desk._job_snapshot(NOW)
    assert store.version(DAY, "market_snapshot") == version


# ----- News into the shared journal ------------------------------------------------------------------

RSS = ("<rss><channel>{items}</channel></rss>")
ITEM = "<item><title>{title}</title><link>https://example.test/{slug}</link><pubDate>Tue, 03 Nov 2026 14:5{n}:00 GMT</pubDate></item>"


def journal_with_holdings(tmp_path):
    from scripts.autonomous_fund.execution import ApprovalPolicy, ExecutionState
    journal = Journal(tmp_path, "synthetic-desk-account")
    journal.event(NOW, "account", {"cash": "9000", "equity": "9500", "positions": {"ALFA": "5"}})
    ExecutionState(journal, ApprovalPolicy())
    body = {"targets": [{"symbol": "OMEGA", "weight": .1}], "alerts": [{"symbol": "ZETA", "above": 10}],
            "news_watch": [{"query": "synthetic export rules"}]}
    with journal.connection() as db:
        db.execute("INSERT INTO fund_decisions VALUES(?,?,?,?,?,?)",
                   ("d1", str(NOW), str(NOW + timedelta(hours=1)), encode(body), "p", "complete"))
    return journal


def test_the_desk_follows_holdings_targets_and_ai_phrases_and_its_news_reaches_the_runner(tmp_path):
    from scripts.autonomous_fund.news import NewsSettings, NewsWatch
    journal = journal_with_holdings(tmp_path)
    symbols, held, queries = follow_list(tmp_path, ["ALFA", "BETA"])
    assert {"ALFA", "BETA", "OMEGA", "ZETA"} <= symbols and held == {"ALFA"} and queries == ["synthetic export rules"]
    hits = [[ITEM.format(title="Synthetic rule draft", slug="a", n=1)]]

    def get(url, headers=None, timeout=None):
        assert "synthetic+export+rules" in url
        text = RSS.format(items="".join(hits[-1]))
        return SimpleNamespace(raise_for_status=lambda: None, content=text.encode(), text=text)
    news = {"enabled": True, "sources": ["google"], "queries": []}
    desk = DataDesk(tmp_path, config(news=news), "local", clock=lambda: NOW, environ={}, news_get=get)
    assert desk._job_news(NOW)["material"] == 0  # The first batch is a baseline.
    hits.append(hits[0] + [ITEM.format(title="Synthetic rule adopted", slug="b", n=5)])
    assert desk._job_news(NOW)["material"] == 1
    runner_side = NewsWatch(journal, NewsSettings.from_config(news))
    material = runner_side.take_material()
    assert [m["title"] for m in material] == ["Synthetic rule adopted"] and material[0]["material"] == "news_query"


def test_the_runners_news_thread_stands_by_while_the_desk_collects_news(tmp_path):
    from scripts.autonomous_fund.news import NewsSettings, NewsWatch
    journal = Journal(tmp_path, "synthetic-standby")
    polls = []
    for desk_running in (True, False):
        watch = NewsWatch(journal, NewsSettings(enabled=True), is_open=lambda: True,
                          standby=lambda running=desk_running: running)
        watch.poll_once = lambda: polls.append("runner")
        watch._stop.wait = lambda seconds, w=watch: w._stop.set() or True  # One loop only.
        watch._run()
    assert polls == ["runner"]  # Only when the desk was not collecting.


def test_the_runner_takes_over_news_when_the_desk_misses_three_polls(tmp_path):
    from scripts.autonomous_fund.news import NewsSettings
    from scripts.autonomous_fund.strategy import AutonomousFund
    status = DeskStatus(tmp_path)
    fake = SimpleNamespace(desk_status=status, news=SimpleNamespace(settings=NewsSettings(enabled=True)),
                           get_datetime=lambda: NOW)
    status.write({"heartbeat_at": NOW.isoformat(), "jobs": {"news": {"ok_at": (NOW - timedelta(minutes=5)).isoformat()}}})
    assert AutonomousFund._desk_collects_news(fake)
    status.write({"heartbeat_at": NOW.isoformat(), "jobs": {"news": {"ok_at": (NOW - timedelta(minutes=16)).isoformat()}}})
    assert not AutonomousFund._desk_collects_news(fake)
    status.write({"heartbeat_at": (NOW - timedelta(minutes=4)).isoformat(), "jobs": {"news": {"ok_at": NOW.isoformat()}}})
    assert not AutonomousFund._desk_collects_news(fake)  # The desk process itself stopped.


# ----- The runner reads the desk's refreshes -----------------------------------------------------------

def runner(tmp_path):
    from tests.test_autonomous_fund_quant import default_frames, harness
    strategy = harness(tmp_path, default_frames(), mode="local")
    strategy.get_datetime = lambda: NOW
    strategy.daily_store = DailyStore(tmp_path / "daily")
    strategy.desk_status = DeskStatus(tmp_path)
    return strategy


def offline(*args, **kwargs):
    raise AssertionError("no network expected")


def test_macro_and_earnings_follow_the_desks_intraday_refreshes(tmp_path):
    from scripts.autonomous_fund.events import EarningsCalendar, EventSettings
    from scripts.autonomous_fund.macro import SERIES, MacroSettings, MacroWatch
    strategy = runner(tmp_path)
    store = strategy.daily_store
    for name in SERIES:
        store.put(DAY, f"fred_{name}", [["2026-10-30", 4.0]])
    strategy.macro = MacroWatch(MacroSettings(), environ={}, get=offline, store=store)
    assert strategy._macro_context(NOW)["series"]["DGS10"]["latest"] == 4.0
    store.put(DAY, "fred_DGS10", [["2026-10-30", 4.0], ["2026-11-02", 4.25]])  # The desk's hourly refresh.
    assert strategy._macro_context(NOW)["series"]["DGS10"]["latest"] == 4.25
    strategy.config = {"universe": {"ALFA": "stock", "BETA": "stock"}}
    strategy.earnings = EarningsCalendar(strategy.journal, EventSettings(), environ={}, yahoo=offline, store=store)
    store.put(DAY, "earnings", {s: {"source": "yahoo", "dates": ["2026-12-01"], "error": None} for s in ("ALFA", "BETA")})
    assert strategy._earnings_caps(NOW)["caps"] == {}
    store.put(DAY, "earnings", {"ALFA": {"source": "yahoo", "dates": [DAY], "error": None},
                                "BETA": {"source": "yahoo", "dates": ["2026-12-01"], "error": None}})
    assert set(strategy._earnings_caps(NOW)["caps"]) == {"ALFA"}  # A date moved forward: capped now, not tomorrow.


def test_the_runner_uses_the_desks_snapshot_when_it_covers_its_symbols(tmp_path):
    strategy = runner(tmp_path)
    strategy.get_historical_prices = offline
    prepared = {"benchmark": "SPY", "as_of_close": "2026-11-02", "meaning": "m", "no_history": ["DELTA"],
                "symbols": {s: {"ret_20d": .01} for s in ("ALFA", "BETA", "GAMMA", "SPY", "EXTRA")}}
    strategy.daily_store.put(DAY, "market_snapshot", prepared)
    result = strategy._market_snapshot(NOW)
    assert set(result["symbols"]) == {"ALFA", "BETA", "GAMMA", "SPY"} and result["no_history"] == ["DELTA"]


def test_desk_latest_reads_the_newest_section_at_the_moment_of_the_call(tmp_path):
    from lumibot.components.agents.tools import is_agent_tool
    from scripts.autonomous_fund.strategy import AutonomousFund
    strategy = runner(tmp_path)
    strategy.daily_store.put(DAY, "intraday", {"as_of": "first"})
    assert strategy.desk_latest("intraday")["data"] == {"as_of": "first"}
    strategy.daily_store.put(DAY, "intraday", {"as_of": "second"})
    assert strategy.desk_latest(" Intraday ")["data"] == {"as_of": "second"}
    unknown = strategy.desk_latest("orders")
    assert "unknown section" in unknown["error"] and "news" in unknown["sections"]
    assert is_agent_tool(AutonomousFund.desk_latest)


def test_the_decision_context_shows_freshness_the_pulse_and_news_since_the_last_decision(tmp_path):
    from scripts.autonomous_fund.news import NewsSettings, NewsWatch
    strategy = runner(tmp_path)
    strategy.news = NewsWatch(strategy.journal, NewsSettings(enabled=True))
    strategy.journal.set_meta("last_model_decision", {"at": (NOW - timedelta(minutes=30)).isoformat(), "day": DAY})
    with strategy.journal.connection() as db:
        for key, minutes_ago, material in (("old", 45, None), ("new", 10, None), ("hot", 5, "news_query")):
            db.execute("INSERT INTO fund_news(id,observed_at,published_at,source,kind,symbols,query,title,url,"
                       "material,baseline) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                       (key, (NOW - timedelta(minutes=minutes_ago)).isoformat(), None, "yahoo", "headline", "[]",
                        None, key, f"https://example.test/{key}", material))
    strategy.daily_store.put(DAY, "intraday", {"as_of": NOW.isoformat()})
    DeskStatus(tmp_path).write({"heartbeat_at": NOW.isoformat(), "jobs": {"intraday": {"ok_at": NOW.isoformat()}}})
    context = strategy._desk_context(NOW)
    assert context["desk"]["running"] and context["intraday"] == {"as_of": NOW.isoformat()}
    assert context["news_since_last_decision"]["items"] == 2 and context["news_since_last_decision"]["material"] == 1


# ----- Operator views ------------------------------------------------------------------------------

def test_status_line_monitor_notice_and_metrics(tmp_path):
    from scripts.autonomous_fund.monitor import Monitor, metrics_text
    from scripts.autonomous_fund.notifications import readable
    from scripts.autonomous_fund.status import desk_line
    assert desk_line(tmp_path, NOW).startswith("数据员：还没有运行过")
    status = DeskStatus(tmp_path)
    status.write({"heartbeat_at": NOW.isoformat(), "running": {"job": "news"},
                  "jobs": {"news": {"ok_at": (NOW - timedelta(minutes=2)).isoformat()},
                           "macro": {"ok_at": None, "error": "ConnectionError"}}})
    line = desk_line(tmp_path, NOW)
    assert "运行中" in line and "资讯 2 分钟前" in line and "宏观 尚无成功（出错：ConnectionError）" in line
    from tests.test_autonomous_fund_monitor import setup
    journal = setup(tmp_path)
    monitor = Monitor(tmp_path)
    for minutes_later in (0, 60, 7 * 60):
        monitor.poll(NOW + timedelta(minutes=15 + minutes_later))
    rows = [r for r in journal.all_events()
            if r["kind"] == "notification" and r["body"]["kind"] == "data_desk_stale"]
    assert len(rows) == 2  # Once per six hours, never a stream.
    assert "交易不受影响" in readable("data_desk_stale", rows[0]["body"]["details"])
    assert "fund_desk_heartbeat_timestamp_seconds" in metrics_text(tmp_path, {"at": NOW.isoformat()})
    status.write({"disabled": True})
    assert "已关闭" in desk_line(tmp_path, NOW)


def test_the_rehearsal_warns_about_a_stopped_desk_without_failing(tmp_path):
    from scripts.autonomous_fund.rehearsal import Stages, _desk_judge, _finish, text
    stages = Stages()
    stages.run("data desk", lambda: DeskStatus(tmp_path).summary(NOW), judge=_desk_judge)
    report = _finish(tmp_path / "out", stages, tmp_path, 5)
    assert report["verdict"] == "ready" and "[注意] 数据员（常驻数据服务）" in text(report)


def test_desk_settings_validate_and_come_from_the_env_file():
    from scripts.autonomous_fund.settings import apply_environment
    with pytest.raises(RiskError):
        DeskSettings(intraday_seconds=30)
    with pytest.raises(RiskError):
        DeskSettings.from_config({"poll": 5})
    result, _ = apply_environment({"data_desk": {"enabled": True}}, {"FUND_DATA_DESK": "false",
                                                                      "FUND_DESK_INTRADAY_SECONDS": "120"})
    assert result["data_desk"] == {"enabled": False, "intraday_seconds": 120}


def test_the_desk_prefetch_and_rehearsal_act_on_the_fund_mode_account():
    # Found by running the real desk process: these commands used the earlier default directory,
    # so the desk (and the rehearsal's account copy) never saw the running account.
    from scripts.autonomous_fund.settings import startup
    for command in ("datadesk", "prefetch", "rehearse"):
        assert startup(command, None, {"FUND_MODE": "paper"})[1] == Path("artifacts/fund-paper")
        assert startup(command, None, {"FUND_MODE": "local", "FUND_RUN_DIR": "artifacts/own"})[1] == Path("artifacts/own")


def test_a_job_whose_every_source_failed_is_an_error_not_fresh_data(tmp_path):
    from tests.test_autonomous_fund_prefetch import frame
    def down(asset, length, timestep="day"):
        raise ConnectionError("unreachable")
    source = SimpleNamespace(get_historical_prices=down, execution_bars=lambda asset: (_ for _ in ()).throw(
        ConnectionError("unreachable")))
    desk = DataDesk(tmp_path, config(), "local", source=source, clock=lambda: NOW, environ={},
                    earnings_yahoo=lambda symbol: (_ for _ in ()).throw(ConnectionError("unreachable")))
    store = DailyStore(tmp_path / "cache" / "daily")
    store.put(DAY, "intraday", {"as_of": "earlier", "symbols": {"ALFA": {}}})
    for name in ("bars", "earnings", "intraday"):
        desk.run_job(name)
        job = DeskStatus(tmp_path).read()["jobs"][name]
        assert job["error"] and not job.get("ok_at"), name
    assert store.get(DAY, "intraday")["as_of"] == "earlier"  # The last good pulse is kept.
    good = SimpleNamespace(get_historical_prices=lambda asset, length, timestep="day": SimpleNamespace(df=frame()))
    desk._source = good
    desk.run_job("bars")
    assert DeskStatus(tmp_path).read()["jobs"]["bars"]["ok_at"]
