"""Connectivity probes for `check`: every service independently, no secret ever printed.

Synthetic credentials and a fake HTTP client only; no network.
"""
import json
from types import SimpleNamespace

from scripts.autonomous_fund.connectivity import connectivity, summary

ENV = {"FUND_TELEGRAM_BOT_TOKEN": "123:synthetic-telegram-token", "DEEPSEEK_API_KEY": "synthetic-model-key",
       "ALPACA_API_KEY": "synthetic-alpaca-id", "ALPACA_API_SECRET": "synthetic-alpaca-secret",
       "FUND_SEC_USER_AGENT": "Synthetic Operator ops@example.com", "FINNHUB_API_KEY": "synthetic-finnhub"}


def fake(blocked=(), status=200):
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if any(host in url for host in blocked):
            raise ConnectionError(f"cannot reach {url}")  # The URL carries the token: it must not leak.
        return SimpleNamespace(status_code=status)
    return get, calls


def test_every_configured_service_is_probed_once():
    get, calls = fake()
    results = connectivity(ENV, ["deepseek/deepseek-flash", "deepseek/deepseek-v4-pro"], get=get)
    assert set(results) == {"yahoo", "telegram", "deepseek", "alpaca_paper", "alpaca_data", "sec", "fred", "finnhub"}
    assert all(row["ok"] for row in results.values()) and summary(results)["all_ok"]
    assert len(calls) == len(results)  # Two DeepSeek models, one probe.


def test_a_blocked_host_is_explained_and_secrets_never_appear():
    get, _ = fake(blocked=("api.telegram.org", "finance.yahoo.com"))
    results = connectivity(ENV, ["deepseek/deepseek-flash"], get=get)
    assert not results["telegram"]["ok"] and results["telegram"]["error"] == "ConnectionError"
    assert "中国大陆" in results["telegram"]["hint"] and results["deepseek"]["ok"]
    assert summary(results)["blocking"] == ["yahoo", "telegram"]
    text = json.dumps(results, ensure_ascii=False)
    for secret in ENV.values():
        assert secret not in text


def test_an_http_error_status_is_a_failure_and_unconfigured_services_are_skipped():
    get, _ = fake(status=401)
    results = connectivity({"DEEPSEEK_API_KEY": "k"}, ["deepseek/deepseek-flash"], get=get)
    assert set(results) == {"yahoo", "deepseek", "fred"}
    assert results["deepseek"]["status"] == 401 and not results["deepseek"]["ok"] and "401" in results["deepseek"]["hint"]


def test_metrics_file_exposes_health_equity_drawdown_and_approvals(tmp_path):
    from datetime import datetime, timezone
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.monitor import metrics_text, write_metrics
    journal = Journal(tmp_path, "synthetic-metrics")
    now = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    journal.event(now, "account", {"as_of": now.isoformat(), "cash": "3000", "equity": "9500", "positions": {},
                                   "cash_equivalent_value": "2500"})
    journal.set_meta("peak", "10000")
    status = {"at": now.isoformat(), "health": "heartbeat_stale", "stop_active": True, "disk_free_bytes": 123,
              "delivery_counts": {"sent": 4, "pending": 1}}
    text = metrics_text(tmp_path, status)
    for line in ("fund_healthy 0", 'fund_health_state{state="heartbeat_stale"} 1', "fund_stop_active 1",
                 "fund_equity_usd 9500.00", "fund_cash_equivalent_usd 2500.00", "fund_drawdown 0.050000",
                 'fund_notifications{state="sent"} 4', "fund_pending_approvals 0"):
        assert line in text, line
    write_metrics(tmp_path, status)
    assert (tmp_path / "metrics.prom").read_text() == text
