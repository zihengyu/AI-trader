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
    for name in ("news", "intraday", "premarket", "global", "calendar", "earnings", "bars", "snapshot"):
        setattr(desk, f"_job_{name}", lambda now, n=name: ran.append(n) or {"ok": True})

    def broken(now):
        ran.append("macro")
        raise ConnectionError("FRED down")
    desk._job_macro = broken
    desk.tick()
    # No SEC contact: no SEC job; the market is open: no pre-market job.
    assert sorted(ran) == ["bars", "calendar", "earnings", "global", "intraday", "macro", "news", "snapshot"]
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
    # Desk context must not overwrite Strategy.macro, which owns the FRED tools.
    strategy.macro_watch = MacroWatch(MacroSettings(), environ={}, get=offline, store=store)
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


# ----- Trading has priority on Yahoo -------------------------------------------------------------------

def test_the_trading_processes_yahoo_failures_make_the_desk_stand_back(tmp_path):
    from scripts.autonomous_fund.datadesk import Idle, cooldown_path, yahoo_cooldown_writer, yahoo_paused_until
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from lumibot.entities import Asset
    write = yahoo_cooldown_writer(tmp_path)
    source = YahooRealtimeData(now=lambda: NOW, fetch=lambda *a: (_ for _ in ()).throw(ConnectionError("429")),
                               on_backoff=write)
    with pytest.raises(RiskError):
        source.execution_bars(Asset("ALFA", asset_type="stock"))
    until = yahoo_paused_until(tmp_path, datetime.now(timezone.utc))
    assert until is not None and cooldown_path(tmp_path).is_file()
    write((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())  # An older end never shortens it.
    assert yahoo_paused_until(tmp_path, datetime.now(timezone.utc)) == until
    clock = datetime.now(timezone.utc)
    desk = DataDesk(tmp_path, config(), "local", clock=lambda: clock, environ={},
                    source=SimpleNamespace(execution_bars=offline, session_bars=offline))
    for job in (desk._job_intraday, desk._job_premarket, desk._job_global):
        with pytest.raises(Idle, match="trading process"):
            job(clock)


def test_desk_requests_are_paced_and_a_failing_round_stops_early():
    desk = object.__new__(DataDesk)
    pauses, asked = [], []
    desk._sleep = pauses.append

    def fetch(symbol):
        asked.append(symbol)
        raise ConnectionError("throttled")
    frames = DataDesk._requests(desk, ["A", "B", "C", "D", "E"], fetch)
    assert asked == ["A", "B", "C"] and "three times in a row" in str(frames["E"])  # D and E never asked.
    assert pauses and all(p == 0.3 for p in pauses)


def test_the_pulse_uses_todays_minutes_only_and_reads_closes_from_the_store(tmp_path):
    asked = {"session": 0, "execution": 0}
    today = minutes("2026-11-03 14:30", 20, first=101.0)

    def session(asset):
        asked["session"] += 1
        return today

    def execution(asset):
        asked["execution"] += 1
        return today
    source = SimpleNamespace(session_bars=session, execution_bars=execution,
                             get_historical_prices=lambda *a, **k: SimpleNamespace(df=None))
    desk = DataDesk(tmp_path, config(), "local", source=source, clock=lambda: NOW, environ={}, sleep=lambda s: None)
    DailyStore(tmp_path / "cache" / "daily").save_bars(DAY, "SPY", 1, [(datetime(2026, 11, 2).date(), 100.0, 1, 1, 1, 1)])
    desk._job_intraday(NOW)
    assert asked["session"] > 0 and asked["execution"] == 0
    pulse = DailyStore(tmp_path / "cache" / "daily").get(DAY, "intraday")
    assert pulse["symbols"]["SPY"]["previous_close"] == 100.0 and "previous_close" not in pulse["symbols"]["QQQ"]


def test_an_implausible_gap_is_flagged_and_kept_out_of_the_movers():
    frames = {"ALFA": minutes("2026-11-03 14:30", 20, first=100.0), "BETA": minutes("2026-11-03 14:30", 20, first=50.0)}
    pulse = intraday_pulse(frames, {"ALFA": ("2026-11-02", 99.0), "BETA": ("2026-11-02", 100.0)}, NOW)
    assert "possibly a split" in pulse["symbols"]["BETA"]["suspect"] and "suspect" not in pulse["symbols"]["ALFA"]
    assert [m["symbol"] for m in pulse["leaders"] + pulse["laggards"]] == ["ALFA", "ALFA"]


def test_a_stale_pulse_is_labeled_not_shown_as_current(tmp_path):
    strategy = runner(tmp_path)
    strategy.daily_store.put(DAY, "intraday", {"as_of": (NOW - timedelta(minutes=40)).isoformat(), "symbols": {"ALFA": {}}})
    stale = strategy._intraday(NOW)
    assert stale["stale"] and stale["age_minutes"] == 40 and "symbols" not in stale
    strategy.daily_store.put(DAY, "intraday", {"as_of": (NOW - timedelta(minutes=5)).isoformat(), "symbols": {}})
    assert not strategy._intraday(NOW).get("stale")


def test_daily_bars_are_stored_only_after_six_in_the_morning_new_york(tmp_path):
    from tests.test_autonomous_fund_prefetch import frame
    from scripts.autonomous_fund.strategy import history_length
    early = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)  # 04:30 New York.
    desk = DataDesk(tmp_path, config(), "local", clock=lambda: early, environ={})
    assert not desk.enabled("bars", early) and desk.enabled("bars", NOW)
    strategy = runner(tmp_path)
    strategy.get_historical_prices = lambda asset, length, timestep="day": SimpleNamespace(df=frame())
    rows, error = strategy._daily_bars("ALFA", early)
    assert rows and error is None
    assert strategy.daily_store.bars(DAY, "ALFA", history_length(strategy.quant)) is None  # Used, not stored.


# ----- New data: pre-market, global markets, the economic calendar ---------------------------------------

def test_premarket_moves_use_trades_before_the_open_only():
    from scripts.autonomous_fund.datadesk import premarket_moves
    at = datetime(2026, 11, 3, 13, 50, tzinfo=timezone.utc)  # 08:50 New York.
    frames = {"ALFA": pd.concat([minutes("2026-11-02 20:50", 5, first=99.0), minutes("2026-11-03 12:00", 100, first=101.0)]),
              "BETA": minutes("2026-11-02 20:50", 5, first=50.0)}
    moves = premarket_moves(frames, {"ALFA": ("2026-11-02", 100.0)}, at)
    alfa = moves["symbols"]["ALFA"]
    assert alfa["change"] == pytest.approx(110.9 / 100 - 1, abs=1e-5) and alfa["volume"] == 100_000
    assert moves["unavailable"] == {"BETA": "no pre-market trades yet"}
    desk = object.__new__(DataDesk)
    desk._calendar_clock = None
    assert DataDesk.before_open(desk, at) and not DataDesk.before_open(desk, NOW)
    assert not DataDesk.before_open(desk, datetime(2026, 11, 7, 13, 50, tzinfo=timezone.utc))  # A Saturday.


def test_global_markets_report_the_move_since_the_previous_day_and_keep_the_last_good_view(tmp_path):
    def chart(symbol, period, interval, prepost):
        assert prepost and interval == "15m"
        if symbol == "^HSI":
            raise ConnectionError("unreachable")
        return pd.concat([minutes("2026-11-02 20:00", 4, first=100.0), minutes("2026-11-03 13:00", 4, first=102.0)])
    desk = DataDesk(tmp_path, config(), "local", clock=lambda: NOW, environ={}, chart=chart, sleep=lambda s: None)
    desk._job_global(NOW)
    view = DailyStore(tmp_path / "cache" / "daily").get(DAY, "global_markets")
    futures = view["markets"]["ES=F"]
    assert futures["previous"] == pytest.approx(100.3) and futures["change"] == pytest.approx(102.3 / 100.3 - 1, abs=1e-5)
    assert futures["meaning"] == "S&P 500 E-mini futures" and view["unavailable"] == {"^HSI": "ConnectionError"}
    desk._gets["chart"] = lambda *a: (_ for _ in ()).throw(ConnectionError("down"))
    with pytest.raises(RiskError):
        desk._job_global(NOW)
    assert DailyStore(tmp_path / "cache" / "daily").get(DAY, "global_markets")["markets"]  # Not replaced by nothing.


FED_PAGE = """
<div class="panel"><h4><a>2026 FOMC Meetings</a></h4>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date col-xs-4">28-29</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>November</strong></div>
<div class="fomc-meeting__date col-xs-4">16-17*</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>November</strong></div>
<div class="fomc-meeting__date col-xs-4">20 (notation vote)</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>December</strong></div>
<div class="fomc-meeting__date col-xs-4">8-9</div></div></div>
<div class="panel"><h4><a>2027 FOMC Meetings</a></h4>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>Jan</strong></div>
<div class="fomc-meeting__date col-xs-4">26-27</div></div></div>
"""


def test_fomc_decision_days_come_from_the_feds_own_calendar():
    from scripts.autonomous_fund.datadesk import fomc_meetings
    from datetime import date
    assert fomc_meetings(FED_PAGE, date(2026, 11, 3)) == [
        {"date": "2026-11-17", "release": "FOMC meeting decision with economic projections",
         "source": "federalreserve.gov FOMC calendar"}]  # The notation vote is not a meeting.
    assert [m["date"] for m in fomc_meetings(FED_PAGE, date(2026, 12, 1), days=60)] == ["2026-12-09", "2027-01-27"]
    spanning = FED_PAGE.replace("Apr/May", "Apr/May").replace("28-29", "30-1")
    assert fomc_meetings(spanning, date(2026, 4, 25))[0]["date"] == "2026-05-01"
    with pytest.raises(RiskError):
        fomc_meetings("<html>maintenance</html>", date(2026, 11, 3))


def test_a_release_dated_every_day_is_a_data_feed_not_an_event():
    from scripts.autonomous_fund.datadesk import major_releases
    from datetime import date
    rows = [{"release_id": 1, "release_name": "Consumer Price Index Daily Snapshot", "date": f"2026-11-{d:02d}"}
            for d in range(3, 20)] + [{"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-11-10"}]
    major, other = major_releases(rows, date(2026, 11, 3))
    assert [r["release"] for r in major] == ["Consumer Price Index"] and other == 17


def test_the_economic_calendar_keeps_the_market_moving_releases_and_needs_a_fred_key(tmp_path):
    from scripts.autonomous_fund.datadesk import Idle
    pages = []

    def get(url, params=None, timeout=None, headers=None):
        if "federalreserve.gov" in url:
            return SimpleNamespace(raise_for_status=lambda: None, text=FED_PAGE)
        pages.append(params["offset"])
        rows = [{"release_id": i, "release_name": "Some Regional Survey", "date": "2026-11-05"} for i in range(1000)]
        if params["offset"]:
            rows = [{"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-11-12"},
                    {"release_id": 50, "release_name": "Employment Situation", "date": "2026-11-06"},
                    {"release_id": 101, "release_name": "FOMC Press Release", "date": "2026-12-09"},
                    {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-11-12"}]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"release_dates": rows})
    desk = DataDesk(tmp_path, config(), "local", clock=lambda: NOW, environ={}, calendar_get=get)
    with pytest.raises(Idle, match="FRED_API_KEY"):
        desk._job_calendar(NOW)
    desk.environ = {"FRED_API_KEY": "synthetic"}
    assert desk._job_calendar(NOW) == {"major": 2, "fomc": 1, "other": 1, "fomc_unavailable": None}
    assert pages == [0, 1000]
    calendar = DailyStore(tmp_path / "cache" / "daily").get(DAY, "econ_calendar")
    assert [r["release"] for r in calendar["releases"]] == [
        "Employment Situation", "Consumer Price Index", "FOMC meeting decision with economic projections"]


def test_the_context_carries_premarket_before_and_early_in_the_session_and_upcoming_releases(tmp_path):
    strategy = runner(tmp_path)
    store = strategy.daily_store
    store.put(DAY, "premarket", {"as_of": (NOW - timedelta(minutes=40)).isoformat(), "symbols": {"ALFA": {}}})
    store.put(DAY, "econ_calendar", {"releases": [{"date": "2026-11-02", "release": "Past"},
                                                  {"date": "2026-11-06", "release": "Employment Situation"}]})
    early = strategy._desk_context(NOW)  # 30 minutes after the open.
    assert early["premarket"]["symbols"] and [r["release"] for r in early["economic_calendar"]["releases"]] == [
        "Employment Situation"]
    assert strategy._desk_context(NOW + timedelta(hours=2))["premarket"] is None
    assert strategy.desk_latest("calendar")["data"]["releases"][0]["release"] == "Employment Situation"


def test_news_from_several_sources_is_one_story_ranked_by_relevance(tmp_path):
    from scripts.autonomous_fund.news import NewsSettings, NewsWatch
    journal = Journal(tmp_path, "synthetic-digest")
    watch = NewsWatch(journal, NewsSettings(enabled=True))
    rows = [("a", "yahoo", "headline", ["OTHER"], None, "Other company opens a plant", 50),
            ("b", "yahoo", "headline", ["ALFA"], None, "Alfa wins large contract - Reuters", 40),
            ("c", "google_news", "headline", ["ALFA"], None, "Alfa wins large contract | Bloomberg", 30),
            ("d", "federal_reserve", "macro", [], None, "Federal Reserve issues statement", 20),
            ("e", "sec_edgar", "filing", ["ALFA"], "sec_filing", "8-K Alfa Corp", 10)]
    with journal.connection() as db:
        for key, source, kind, symbols, material, title, minutes_ago in rows:
            db.execute("INSERT INTO fund_news(id,observed_at,published_at,source,kind,symbols,query,title,url,"
                       "material,baseline) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                       (key, (NOW - timedelta(minutes=minutes_ago)).isoformat(), None, source, kind,
                        json.dumps(symbols), None, title, f"https://example.test/{key}", material))
    context = watch.recent({"ALFA", "OTHER"}, NOW, held={"ALFA"})
    titles = [item["title"] for item in context["items"]]
    assert titles[0] == "8-K Alfa Corp" and titles[-1] == "Other company opens a plant"
    contract = next(item for item in context["items"] if item["title"].startswith("Alfa wins"))
    assert contract["also_reported"] == {"copies": 2, "sources": ["google_news", "yahoo"]} and "held" in contract["relevance"]
    assert context["duplicates_merged"] == 1 and context["mentions_24h"] == {"ALFA": 2}


def test_the_desk_service_yields_cpu_io_and_memory_to_trading():
    from scripts.autonomous_fund.deploy import generate
    desk = generate("/srv/fund", None, "/srv/fund/.env", "fundbot", None)["lumibot-fund-desk.service"]
    assert all(line in desk.splitlines() for line in ("Nice=10", "CPUWeight=20", "IOWeight=20", "OOMScoreAdjust=500"))


# ----- Findings from the 2026-11-03 server rehearsal ------------------------------------------------------

def test_a_live_evidence_time_span_is_repaired_and_a_historical_one_is_read_conservatively():
    from scripts.autonomous_fund.risk import validate_evidence
    live = [{"source": "s", "available_at": None, "estimated": False, "estimate_reason": "computed",
             "observed_at": "2026-11-03T02:10:22-05:00 ~ 2026-11-03T02:11:45-05:00"},
            {"source": "t", "available_at": "around midday", "observed_at": "yesterday", "estimated": False,
             "estimate_reason": ""}]
    validate_evidence(live, NOW, False)  # Raised ValueError and lost the whole post-market review before.
    assert live[0]["observed_at"] == "2026-11-03T02:11:45-05:00" and live[0]["estimated"]
    assert "reduced to its end" in live[0]["estimate_reason"]
    assert live[1]["observed_at"] == NOW.isoformat() and live[1]["available_at"] is None
    assert "unreadable" in live[1]["estimate_reason"]
    cutoff = datetime(2026, 11, 3, 7, 11, tzinfo=timezone.utc)  # Between the two ends of the span.
    historical = [{"source": "s", "available_at": "2026-11-02T16:00:00-05:00", "estimated": False,
                   "estimate_reason": "", "observed_at": "2026-11-03T02:10:22-05:00 ~ 2026-11-03T02:11:45-05:00"}]
    with pytest.raises(RiskError, match="after the simulated cutoff"):
        validate_evidence(historical, cutoff, True)  # The span's end is after the cutoff.
    with pytest.raises(RiskError, match="readable"):
        validate_evidence([{**historical[0], "observed_at": "yesterday"}], cutoff, True)


def test_an_over_long_answer_is_trimmed_instead_of_thrown_away():
    from scripts.autonomous_fund.review import PostmarketReview
    from scripts.autonomous_fund.strategy import Critique, Decision
    critique = Critique.model_validate({"summary": "x" * 3200, "concerns": [
        {"symbol": "", "concern": "c" * 900, "evidence": "e"}] * 12})  # Rejected outright before (1,500 cap).
    assert len(critique.summary) <= 3000 and critique.summary.endswith("…[truncated]") and len(critique.concerns) == 10
    assert Critique.model_json_schema()["properties"]["summary"]["maxLength"] == 3000  # The model still sees it.
    decision = Decision.model_validate({"as_of": "a", "expires_at": "b", "next_review_at": "c", "rationale": "r",
                                        "targets": [], "news_watch": [{"query": "jobs report", "reason": "r" * 500}]})
    assert len(decision.news_watch[0].reason) <= 200
    with pytest.raises(Exception):
        Critique.model_validate({"summary": ""})  # Required content is still required.
    assert PostmarketReview.model_json_schema()["properties"]["summary"]["maxLength"] == 4000


def test_an_empty_desk_section_says_why(tmp_path):
    strategy = runner(tmp_path)
    answer = strategy.desk_latest("premarket")
    assert answer["data"] is None and "04:00 New York" in answer["unavailable"]
    assert "news watch is not running" in strategy.desk_latest("news")["unavailable"]


def test_the_rehearsal_reports_only_its_own_calls_and_every_lost_answer(tmp_path):
    from scripts.autonomous_fund.rehearsal import _last_sequence, agent_calls, answer_problems
    journal = Journal(tmp_path, "synthetic-rehearsal")
    journal.event(NOW, "agent_call_started", {"id": "old", "name": "critic"})  # The account's history.
    journal.event(NOW, "agent", {"name": "researcher", "parse_error": "OSError"})
    start = _last_sequence(tmp_path)
    journal.event(NOW, "agent_call_started", {"id": "a", "name": "critic"})
    journal.event(NOW + timedelta(seconds=117), "agent_call_finished", {"id": "a", "name": "critic"})
    journal.event(NOW, "agent", {"name": "critic", "parse_error": "String should have at most 1500 characters",
                                 "usage": {"prompt_tokens": 448884, "completion_tokens": 20623}})
    journal.event(NOW, "critic_unavailable", {"error": "Invalid structured agent answer"})
    journal.event(NOW, "operation_error", {"notification": "postmarket_review_error", "error": "ValueError",
                                           "frames": [{"module": "scripts.autonomous_fund.risk",
                                                       "function": "validate_evidence", "line": 99}]})
    journal.event(NOW, "notification", {"kind": "postmarket_review_error", "details": "ValueError"})
    calls = agent_calls(journal, after=start)
    assert calls == [{"name": "critic", "seconds": 117.0, "input_tokens": 448884, "output_tokens": 20623}]
    problems = answer_problems(journal, after=start)
    assert len(problems) == 3 and "at most 1500 characters" in problems[0]
    assert problems[2] == "postmarket_review_error: ValueError at scripts.autonomous_fund.risk.validate_evidence:99"


def test_status_hides_errors_older_than_a_day_and_dates_recent_ones(tmp_path):
    from scripts.autonomous_fund.status import summary
    from tests.test_autonomous_fund_monitor import setup
    journal = setup(tmp_path)
    journal.event(NOW - timedelta(hours=30), "operation_error", {"phase": "trading_iteration", "error": "OSError"})
    assert "最近业务异常" not in summary(tmp_path, "local", now=NOW)
    journal.event(NOW - timedelta(hours=2), "operation_error", {"phase": "premarket_research", "error": "ValueError"})
    assert "最近业务异常（2 小时前）" in summary(tmp_path, "local", now=NOW)


def test_a_planned_restart_is_not_alerted_but_a_crash_is(tmp_path):
    from scripts.autonomous_fund import stops
    from scripts.autonomous_fund.monitor import Monitor
    from tests.test_autonomous_fund_monitor import NOW as MONITOR_NOW, setup
    journal = setup(tmp_path)
    monitor = Monitor(tmp_path)
    monitor.poll(MONITOR_NOW + timedelta(seconds=5))
    stops.write(tmp_path, "operator", "stopped_by_operator", MONITOR_NOW + timedelta(seconds=6))
    (tmp_path / "status.json").write_text(encode({"heartbeat_at": (MONITOR_NOW + timedelta(seconds=6)).isoformat(),
                                                  "runner_state": "exited"}), encoding="utf-8")
    monitor.poll(MONITOR_NOW + timedelta(seconds=10))
    kinds = [r["body"].get("kind") for r in journal.all_events() if r["kind"] == "notification"]
    assert "watchdog_stop" not in kinds and any(r["kind"] == "watchdog_noted" for r in journal.all_events())
