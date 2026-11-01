"""Telegram reply approvals, live streamed quotes and the real-time information watch.

HTTP, websocket and Telegram are replaced by in-memory doubles: no network.
"""
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.autonomous_fund.execution import EXECUTION_REVISION, approval_code, resolve_approval
from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.monitor import Monitor, approvers
from scripts.autonomous_fund.risk import RiskError
from .test_autonomous_fund_execution import NOW, envelope, setup
from .test_autonomous_fund_monitor import heartbeat


# ----- Telegram: reply 同意 / 拒绝 -------------------------------------------------------

class Bot:
    def __init__(self, chat_id="1001", approver_ids=None):
        self.chat_id, self.replies_enabled = chat_id, True
        self.approver_ids = approvers(chat_id, approver_ids if approver_ids is not None else "")
        self.sent, self.batches, self.offsets = [], [], []

    def send(self, text, reply_to=None):
        self.sent.append((text, reply_to))
        return {"ok": True, "message_id": 100 + len(self.sent)}

    def send_document(self, caption, filename, document):
        return self.send(caption)

    def updates(self, offset):
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []


def message(text, when, update_id=1, sender="1001", chat="1001", reply_to=None):
    body = {"message_id": 900 + update_id, "chat": {"id": int(chat)}, "from": {"id": int(sender)},
            "date": int(when.timestamp()), "text": text}
    if reply_to is not None:
        body["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": body}


def delivered(tmp_path, bot=None):
    """A pending plan request whose alert reached the operator's Telegram."""
    journal, state, account, active = setup(tmp_path)
    request = state.request(active, envelope(), NOW)
    heartbeat(tmp_path, NOW)
    journal.event(NOW, "iteration_finished", {})
    bot = bot or Bot()
    monitor = Monitor(tmp_path, bot)
    monitor.poll(NOW)
    alert_id = 100 + next(i for i, (text, _) in enumerate(bot.sent, 1) if approval_code(request["id"]) in text)
    return journal, request, bot, monitor, alert_id


def state(journal, key):
    with journal.connection() as db:
        return db.execute("SELECT state FROM fund_approvals WHERE id=?", (key,)).fetchone()[0]


def test_replying_to_the_alert_with_agree_approves_that_exact_request(tmp_path):
    journal, request, bot, monitor, alert = delivered(tmp_path)
    later = NOW + timedelta(minutes=2)
    heartbeat(tmp_path, later)
    bot.batches = [[message("同意", later, reply_to=alert)]]
    monitor.poll(later)
    assert state(journal, request["id"]) == "approved"
    assert "已批准" in bot.sent[-1][0] and bot.sent[-1][1] == 901
    audit = [row["body"] for row in journal.all_events() if row["kind"] == "operator_approval"]
    assert audit[-1]["authority"] == "telegram:1001"
    with journal.connection() as db:
        assert db.execute("SELECT value FROM fund_monitor_meta WHERE key='telegram_offset'").fetchone()[0] == "2"


def test_code_reject_and_plain_text_with_a_single_pending_request(tmp_path):
    journal, request, bot, monitor, _ = delivered(tmp_path)
    later = NOW + timedelta(minutes=1)
    bot.batches = [[message(f"拒绝 {approval_code(request['id']).lower()}", later)]]
    monitor.poll(later)
    assert state(journal, request["id"]) == "rejected" and "已拒绝" in bot.sent[-1][0]
    bot.batches = [[message("同意", later + timedelta(seconds=5), update_id=2)]]
    monitor.poll(later + timedelta(seconds=5))
    assert "没有待审批" in bot.sent[-1][0]  # The rejected request cannot be revived.


@pytest.mark.parametrize("case", ["other_chat", "unauthorized", "before_request", "stop", "wrong_code"])
def test_unsafe_or_ambiguous_replies_never_approve(tmp_path, case):
    journal, request, bot, monitor, alert = delivered(tmp_path)
    later, sent_before = NOW + timedelta(minutes=1), len(bot.sent)
    if case == "other_chat":
        bot.batches = [[message("同意", later, chat="2002", sender="2002")]]
    elif case == "unauthorized":
        bot.batches = [[message("同意", later, sender="3003", reply_to=alert)]]
    elif case == "before_request":
        bot.batches = [[message("同意", NOW - timedelta(minutes=5), reply_to=alert)]]
    elif case == "stop":
        (tmp_path / "STOP").touch()
        bot.batches = [[message("同意", later, reply_to=alert)]]
    else:
        bot.batches = [[message("同意 ZZZZZZ", later)]]
    monitor.poll(later)
    assert state(journal, request["id"]) == "pending"
    replies = [text for text, _ in bot.sent[sent_before:] if "监控" not in text and "watchdog" not in text]
    if case == "other_chat":
        assert not any("已批准" in t or "未授权" in t for t in replies)
    else:
        assert replies and not any("已批准" in t for t in replies)


def test_group_chats_need_explicit_approver_ids():
    assert approvers("1001", "") == {"1001"}
    assert approvers("-100200", "") == set()
    assert approvers("-100200", "11, 22") == {"11", "22"}


def test_status_command_reports_pending_requests_and_the_sleeve(tmp_path):
    journal, request, bot, monitor, _ = delivered(tmp_path)
    journal.event(NOW, "account", {"cash": "9000", "equity": "10000", "positions": {"ALFA": 10}})
    bot.batches = [[message("状态", NOW + timedelta(minutes=1))]]
    monitor.poll(NOW + timedelta(minutes=1))
    assert "待审批请求：1" in bot.sent[-1][0] and "10000" in bot.sent[-1][0]


def test_replies_can_be_disabled_by_the_operator(tmp_path, monkeypatch):
    from scripts.autonomous_fund.monitor import Telegram
    monkeypatch.setenv("FUND_TELEGRAM_APPROVALS", "off")
    assert Telegram("synthetic-token", "1001", post=lambda *a, **k: None).replies_enabled is False


# ----- Live streamed quotes -------------------------------------------------------------

def frame(symbol, price, when, bid=0.0, ask=0.0):
    from yfinance.pricing_pb2 import PricingData
    data = PricingData(id=symbol, price=price, time=int(when.timestamp() * 1000), bid=bid, ask=ask, day_volume=12345)
    return json.dumps({"type": "pricing", "message": base64.b64encode(data.SerializeToString()).decode()})


def test_tape_decodes_stream_frames_maps_share_classes_and_ignores_out_of_order(tmp_path):
    from scripts.autonomous_fund.market_data import LiveTape
    tape = LiveTape(connect=lambda: None)
    tape._wanted = {"BRK.B"}
    tape.handle(frame("BRK-B", 480.25, NOW))
    tape.handle(frame("BRK-B", 470.0, NOW - timedelta(seconds=5)))
    tick = tape.tick("BRK.B")
    assert str(tick["price"]) == "480.25" and tick["time"] == NOW and tick["bid"] is None


def test_tape_subscribes_reconnects_and_reports_errors():
    from scripts.autonomous_fund.market_data import LiveTape
    events = []

    class Client:
        def __init__(self, frames):
            self.frames = frames

        def send(self, text):
            events.append(json.loads(text))

        def recv(self, timeout):
            if self.frames:
                return self.frames.pop(0)
            if len(events) >= 2:
                tape.close()
                raise TimeoutError
            raise ConnectionError("synthetic disconnect")

        def close(self):
            pass

    clients = [Client([frame("SPY", 600.5, NOW)]), Client([])]
    tape = LiveTape(connect=lambda: clients.pop(0))
    tape._wanted = {"SPY", "BRK.B"}
    tape._stop.wait = lambda seconds: False  # No real backoff sleep in the test.
    tape._run()
    assert events[0] == {"subscribe": ["BRK-B", "SPY"]} and events[1] == {"subscribe": ["BRK-B", "SPY"]}
    assert tape.state["reconnects"] == 1 and tape.state["last_error"] == "ConnectionError"
    assert str(tape.tick("SPY")["price"]) == "600.5"


def test_quotes_prefer_a_fresh_regular_session_trade_and_fall_back_honestly(tmp_path):
    from lumibot.entities import Asset
    from scripts.autonomous_fund.market_data import LiveTape, YahooRealtimeData
    from .test_autonomous_fund_local import Clock
    clock = Clock()
    tape = LiveTape(connect=lambda: None)
    tape.watch = lambda symbols: None
    source = YahooRealtimeData(now=clock, fetch=clock.fetch, tape=tape, live_seconds=30)
    asset = Asset("ALFA")
    bar = source.get_quote(asset)
    assert bar.raw_data["price_kind"] == "completed_minute_close_with_spread_proxy"
    from scripts.autonomous_fund.risk import number
    tape._ticks["ALFA"] = {"price": number("101.5"), "time": clock() - timedelta(seconds=3), "received": 0,
                           "bid": number("101.4"), "ask": number("101.6"), "day_volume": 5}
    live = source.get_quote(asset)
    assert live.raw_data["price_kind"] == "live_trade_with_streamed_bid_ask"
    assert live.price == 101.5 and live.raw_data["original_response"].timestamp == clock() - timedelta(seconds=3)
    tape._ticks["ALFA"].update(bid=None, ask=None)
    assert source.get_quote(asset).raw_data["price_kind"] == "live_trade_with_spread_proxy"
    tape._ticks["ALFA"]["time"] = clock() - timedelta(seconds=31)
    assert source.get_quote(asset).raw_data["price_kind"] == "completed_minute_close_with_spread_proxy"
    tape._ticks["ALFA"]["time"] = clock() + timedelta(seconds=1)  # A future-stamped tick is not evidence.
    assert source.get_quote(asset).raw_data["price_kind"] == "completed_minute_close_with_spread_proxy"
    assert source.diagnostics()["quotes_by_kind"] == {"live": 2, "completed_bar": 3}


@pytest.mark.parametrize("settings", [{"live_stream": "yes"}, {"live_seconds": 120}, {"unknown": 1}])
def test_invalid_market_data_settings_fail_explicitly(settings):
    from scripts.autonomous_fund.market_data import MarketDataSettings
    with pytest.raises(RiskError):
        MarketDataSettings.from_config(settings)


# ----- Real-time information watch ---------------------------------------------------------

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Fed holds rates steady</title><link>https://www.federalreserve.gov/a</link>
<pubDate>Mon, 02 Nov 2026 14:00:00 GMT</pubDate></item></channel></rss>"""
ATOM_FEED = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>8-K - Current report</title><link href="https://www.sec.gov/f1"/><updated>2026-11-02T13:00:00-05:00</updated>
</entry></feed>"""


def test_feeds_parse_rss_and_atom_and_refuse_entity_declarations():
    from scripts.autonomous_fund.news import parse_feed
    rss = parse_feed(RSS, "federal_reserve", "macro")
    assert rss[0]["title"] == "Fed holds rates steady" and rss[0]["published_at"] == "2026-11-02T14:00:00+00:00"
    atom = parse_feed(ATOM_FEED, "sec_edgar", "filing", ["ALFA"])
    assert atom[0]["symbols"] == ["ALFA"] and atom[0]["published_at"] == "2026-11-02T18:00:00+00:00"
    with pytest.raises(RiskError, match="DTD"):
        parse_feed('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss/>', "x", "headline")


def test_yahoo_ticker_news_parses_current_and_legacy_shapes():
    from scripts.autonomous_fund.news import yahoo_items
    rows = yahoo_items("ALFA", lambda s: [
        {"content": {"title": "ALFA beats estimates", "pubDate": "2026-11-02T13:30:00Z",
                     "canonicalUrl": {"url": "https://finance.yahoo.com/x"}}},
        {"title": "Legacy shape", "link": "https://finance.yahoo.com/y", "providerPublishTime": 1793626200}])
    assert [r["title"] for r in rows] == ["ALFA beats estimates", "Legacy shape"]
    assert rows[0]["published_at"] == "2026-11-02T13:30:00+00:00" and rows[1]["published_at"] is not None


class Web:
    def __init__(self):
        self.pages, self.calls = {}, []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        body = next((v for k, v in self.pages.items() if url.startswith(k)), None)
        if isinstance(body, Exception):
            raise body
        if body is None:
            raise ConnectionError("synthetic: unknown URL")
        return SimpleNamespace(text=body, content=body.encode(), raise_for_status=lambda: None)


def watch(tmp_path, web, yahoo, environ=None, **settings):
    from scripts.autonomous_fund.news import NewsSettings, NewsWatch
    clock = [NOW]
    journal = Journal(tmp_path, "synthetic-news")
    feed = NewsWatch(journal, NewsSettings(enabled=True, **settings), clock=lambda: clock[0], get=web,
                     yahoo=yahoo, environ=environ or {})
    return feed, clock


def test_first_poll_is_a_baseline_then_new_filings_releases_and_query_hits_are_material(tmp_path):
    web = Web()
    web.pages["https://www.federalreserve.gov"] = RSS
    web.pages["https://news.google.com"] = RSS.replace("Fed holds rates steady", "Rate decision preview")
    web.pages["https://www.sec.gov/files"] = json.dumps({"0": {"ticker": "ALFA", "cik_str": 42}})
    web.pages["https://www.sec.gov/cgi-bin"] = ATOM_FEED
    headlines = {"ALFA": []}
    feed, clock = watch(tmp_path, web, lambda s: headlines.get(s, []), {"FUND_SEC_USER_AGENT": "Synthetic Operator ops@example.com"},
                        queries=("rate decision",))
    feed.watch({"ALFA"}, {"ALFA"}, ["AI chosen query"])
    feed.poll_once()
    assert feed.take_material() == []  # Pre-existing items are context, not triggers.
    web.pages["https://www.federalreserve.gov"] = RSS.replace("Fed holds rates steady", "FOMC statement")
    web.pages["https://www.sec.gov/cgi-bin"] = ATOM_FEED.replace("https://www.sec.gov/f1", "https://www.sec.gov/f2")
    clock[0] = NOW + timedelta(minutes=5)
    feed.poll_once()
    kinds = sorted(row["material"] for row in feed.take_material())
    assert kinds == ["macro_release", "sec_filing"]
    assert feed.take_material() == []  # Consumed exactly once.
    sec_calls = [headers for url, headers in web.calls if "sec.gov" in url]
    assert all(h["User-Agent"] == "Synthetic Operator ops@example.com" for h in sec_calls)
    google = [url for url, _ in web.calls if "news.google.com" in url]
    assert any("AI+chosen+query" in url for url in google) and any("rate+decision" in url for url in google)


def test_headline_burst_on_a_held_symbol_is_material_once_per_window(tmp_path):
    web = Web()
    stories = []
    feed, clock = watch(tmp_path, web, lambda s: list(stories), sources=("yahoo",), burst_threshold=3)
    feed.watch({"ALFA"}, {"ALFA"}, [])
    stories.append({"title": "old", "link": "https://finance.yahoo.com/0", "pubDate": NOW.isoformat()})
    feed.poll_once()
    for i in range(1, 4):
        stories.append({"title": f"story {i}", "link": f"https://finance.yahoo.com/{i}", "pubDate": NOW.isoformat()})
    clock[0] = NOW + timedelta(minutes=5)
    feed.poll_once()
    assert [row["material"] for row in feed.take_material()] == ["headline_burst"]
    stories.append({"title": "story 4", "link": "https://finance.yahoo.com/4", "pubDate": NOW.isoformat()})
    clock[0] = NOW + timedelta(minutes=10)
    feed.poll_once()
    assert feed.take_material() == []


def test_sec_is_skipped_without_a_declared_contact_and_failing_sources_back_off(tmp_path):
    web = Web()
    web.pages["https://www.federalreserve.gov"] = ConnectionError("synthetic outage")
    feed, _ = watch(tmp_path, web, lambda s: [], sources=("fed", "sec"))
    feed.watch({"ALFA"}, {"ALFA"}, [])
    feed.poll_once()
    feed.poll_once()
    assert not any("sec.gov" in url for url, _ in web.calls)
    assert len([url for url, _ in web.calls if "federalreserve" in url]) == 1  # Backed off on the second poll.
    assert feed.stats["errors"]["fed"] == "ConnectionError"


def test_recent_context_is_bounded_relevant_and_marked_untrusted(tmp_path):
    web = Web()
    web.pages["https://www.federalreserve.gov"] = RSS
    stories = [{"title": f"{s} story", "link": f"https://finance.yahoo.com/{s}", "pubDate": NOW.isoformat()}
               for s in ("ALFA", "BETA")]
    feed, _ = watch(tmp_path, web, lambda s: [x for x in stories if x["title"].startswith(s)], sources=("yahoo", "fed"))
    feed.watch({"ALFA", "BETA"}, set(), [])
    feed.poll_once()
    context = feed.recent({"ALFA"}, NOW)
    titles = {item["title"] for item in context["items"]}
    assert titles == {"ALFA story", "Fed holds rates steady"} and "Untrusted" in context["note"]
    future = feed.recent({"ALFA"}, NOW - timedelta(minutes=1))
    assert future["items"] == []  # Nothing observed after the requested time is shown.


@pytest.mark.parametrize("settings", [{"poll_seconds": 10}, {"sources": ["twitter"]},
                                      {"rss_feeds": [{"name": "x", "url": "http://insecure"}]},
                                      {"queries": ["x" * 101]}, {"unknown": 1}])
def test_invalid_news_settings_fail_explicitly(settings):
    from scripts.autonomous_fund.news import NewsSettings
    with pytest.raises(RiskError):
        NewsSettings.from_config(settings)


def test_material_news_wakes_the_ai_and_ai_queries_are_watched(tmp_path):
    from .test_autonomous_fund_autonomy import accept, watching
    from .test_autonomous_fund_quant import account, quote
    strategy = watching(tmp_path, {"ALFA": quote(100)})
    accept(strategy, [{"symbol": "ALFA", "weight": .05}], when=NOW - timedelta(minutes=30))
    with strategy.journal.connection() as db:
        db.execute("UPDATE fund_decisions SET body=json_set(body,'$.news_watch',json(?))",
                   (json.dumps([{"query": "ALFA FDA approval", "reason": "binary event"}]),))
    calls = {}

    class News:
        def watch(self, symbols, held, queries):
            calls.update(symbols=symbols, held=held, queries=queries)

        def take_material(self):
            return [{"material": "sec_filing", "title": "8-K - Current report", "source": "sec_edgar",
                     "symbols": ["ALFA"], "published_at": NOW.isoformat()}]

    strategy.news = News()
    strategy._watch(account(9500, {"ALFA": 5}), NOW)
    assert calls["queries"] == ["ALFA FDA approval"] and calls["held"] == {"ALFA"}
    assert set(strategy.universe) <= calls["symbols"]
    review = [row["body"] for row in strategy.journal.all_events() if row["kind"] == "review_triggered"]
    assert review[-1]["reason"] == "news" and review[-1]["material"] == "sec_filing"
    assert strategy.next_review == NOW


def test_decision_schema_accepts_news_watch_and_rejects_urls_as_extra_fields():
    from pydantic import ValidationError
    from scripts.autonomous_fund.strategy import Decision
    body = {"as_of": NOW.isoformat(), "expires_at": NOW.isoformat(), "next_review_at": NOW.isoformat(),
            "rationale": "r", "targets": [], "news_watch": [{"query": "CPI report", "reason": "macro risk"}]}
    assert Decision.model_validate(body).news_watch[0].query == "CPI report"
    with pytest.raises(ValidationError):
        Decision.model_validate({**body, "news_watch": [{"query": "x", "reason": "r", "url": "https://evil"}]})


def test_configured_iteration_interval_speeds_up_live_watching(tmp_path, monkeypatch):
    from scripts.autonomous_fund.local_broker import LocalPaperBroker
    from scripts.autonomous_fund.market_data import YahooRealtimeData
    from scripts.autonomous_fund.run import ScriptedRuntime
    from scripts.autonomous_fund.strategy import AutonomousFund, open_journal
    from scripts.autonomous_fund.risk import Limits
    from .test_autonomous_fund_local import Clock
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    clock = Clock()
    config = {"model": "deepseek/deepseek-flash", "limits": {}, "execution": {"iteration_seconds": 20},
              "universe": {"ALFA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}
    journal = open_journal(tmp_path, "local", "local-virtual-account", config)
    broker = LocalPaperBroker(YahooRealtimeData(now=clock, fetch=clock.fetch), journal, Limits(), connect_stream=False)
    strategy = AutonomousFund(broker=broker, name="FastWatch", parameters={"config": config, "run_dir": str(tmp_path),
                              "mode": "local", "account_binding": "local-virtual-account", "runtime": ScriptedRuntime()})
    strategy._executor._initialize()
    assert strategy.sleeptime == "20S"
    broker._close_connection()
