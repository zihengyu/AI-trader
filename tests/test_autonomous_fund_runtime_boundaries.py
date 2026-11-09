"""Synthetic recovery and shared-cache regressions; no network or real accounts."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.autonomous_fund import stops
from scripts.autonomous_fund.datastore import DailyStore
from scripts.autonomous_fund.execution import EXECUTION_REVISION
from scripts.autonomous_fund.monitor import Monitor
from tests.test_autonomous_fund_monitor import NOW, FakeTelegram, setup


def heartbeat(root, at, mode="local"):
    name = "status.json" if mode == "local" else "runner_health.json"
    (root / name).write_text(json.dumps({"heartbeat_at": at.isoformat(), "runner_state": "running",
                                       "matching_error": None}), encoding="utf-8")


@pytest.mark.parametrize("mode", ["local", "paper"])
@pytest.mark.parametrize("future_seconds", [1, 3600])
def test_a_future_runner_heartbeat_is_unavailable_in_both_modes(tmp_path, mode, future_seconds):
    journal = setup(tmp_path)
    journal.event(NOW, "startup", {"mode": mode, "execution_revision": EXECUTION_REVISION})
    heartbeat(tmp_path, NOW + timedelta(seconds=future_seconds), mode)
    status = Monitor(tmp_path, FakeTelegram()).poll(NOW)
    assert status["health"] == "heartbeat_unavailable"
    assert status["stop_active"] is True


@pytest.mark.parametrize("age, expected", [(0, "healthy"), (180, "healthy"), (181, "heartbeat_stale")])
def test_existing_heartbeat_freshness_boundaries_are_preserved(tmp_path, age, expected):
    setup(tmp_path)
    heartbeat(tmp_path, NOW - timedelta(seconds=age))
    status = Monitor(tmp_path, FakeTelegram()).poll(NOW)
    assert status["health"] == expected


@pytest.mark.parametrize("offset, expected", [(1, "healthy"), (3600, "heartbeat_unavailable")])
def test_realtime_heartbeat_is_compared_with_the_clock_after_the_file_read(tmp_path, monkeypatch, offset, expected):
    from scripts.autonomous_fund import monitor as module
    setup(tmp_path)
    heartbeat(tmp_path, NOW + timedelta(seconds=offset))
    monitor = Monitor(tmp_path, FakeTelegram())

    class Clock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            # A runner can refresh its heartbeat after a poll begins. By the
            # time it is read, one second of wall-clock time has elapsed.
            current = NOW + timedelta(seconds=bool(cls.calls))
            cls.calls += 1
            return current

    monkeypatch.setattr(module, "datetime", Clock)
    assert monitor.poll()["health"] == expected


def test_a_future_heartbeat_cannot_establish_health_for_automatic_resume(tmp_path):
    setup(tmp_path)
    stops.write(tmp_path, "service", "service_start", NOW)
    monitor = Monitor(tmp_path, FakeTelegram(), resume_policy=stops.ResumePolicy())
    heartbeat(tmp_path, NOW + timedelta(hours=1))
    for seconds in range(0, 181, 30):
        assert monitor.poll(NOW + timedelta(seconds=seconds))["stop_active"] is True
    # A real recovery still resumes after the normal continuous-health period.
    for seconds in range(210, 391, 30):
        at = NOW + timedelta(seconds=seconds)
        heartbeat(tmp_path, at)
        status = monitor.poll(at)
    assert status["health"] == "healthy" and status["stop_active"] is False


@pytest.mark.parametrize("change", [
    {"class": []}, {"class": {}}, {"class": True}, {"class": "unknown"},
    {"reason": ["unexpected"]}, {"reason": {"unexpected": True}}, {"reason": True}, {"reason": ""},
    {"at": []}, {"at": {}}, {"at": 5}, {"at": "not-a-time"}, {"at": "2026-11-01T17:00:00"},
])
def test_malformed_stop_records_remain_operator_stops_and_can_be_cleared(tmp_path, change):
    body = {"class": "service", "reason": "service_start", "at": NOW.isoformat(), **change}
    (tmp_path / "STOP").write_text(json.dumps(body), encoding="utf-8")
    record = stops.read(tmp_path)
    assert record["class"] == "operator" and record["legacy"] is True
    assert stops.resumable(record) is False
    assert stops.write(tmp_path, "service", "service_start", NOW) == record
    assert stops.clear(tmp_path, expected=record) is True
    assert not (tmp_path / "STOP").exists()


def test_a_non_utf8_stop_is_an_operator_stop(tmp_path):
    (tmp_path / "STOP").write_bytes(b"\xff\xfelegacy stop")
    record = stops.read(tmp_path)
    assert record["class"] == "operator" and stops.resumable(record) is False
    assert stops.clear(tmp_path, expected=record) is True


@pytest.mark.parametrize("kind, reason", [("service", "service_start"), ("watchdog", "heartbeat_stale"),
                                         ("operator", "operator_stop")])
def test_valid_stop_records_retain_their_identity_and_priority(tmp_path, kind, reason):
    written = stops.write(tmp_path, kind, reason, NOW)
    assert stops.read(tmp_path) == written
    assert stops.resumable(written) is (kind != "operator")


@pytest.mark.parametrize("include_at", [False, True])
def test_older_recognized_json_stops_keep_missing_timestamp_compatibility(tmp_path, include_at):
    body = {"class": "service", "reason": "service_start", **({"at": None} if include_at else {})}
    (tmp_path / "STOP").write_text(json.dumps(body), encoding="utf-8")
    assert stops.read(tmp_path) == body


def test_same_process_cache_writers_publish_complete_independent_files(tmp_path, monkeypatch):
    store, day = DailyStore(tmp_path), "2026-11-03"
    target = tmp_path / day / "market_snapshot.json"
    barrier, staging = threading.Barrier(2), []
    replace = Path.replace

    def overlapping_replace(path, destination):
        if Path(destination) == target:
            staging.append(path)
            barrier.wait(timeout=5)
        return replace(path, destination)
    monkeypatch.setattr(Path, "replace", overlapping_replace)
    values = [{"writer": i, "payload": str(i) * 4096} for i in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.put, day, "market_snapshot", value) for value in values]
        for future in futures:
            future.result(timeout=10)
    assert len(set(staging)) == 2
    assert store.get(day, "market_snapshot") in values
    assert list(target.parent.glob("*.tmp")) == []


def test_failed_cache_publication_preserves_the_previous_value_and_removes_staging(tmp_path, monkeypatch):
    store, day = DailyStore(tmp_path), "2026-11-03"
    store.put(day, "earnings", {"generation": 1})
    version = store.version(day, "earnings")

    def fail(*args, **kwargs):
        raise OSError("synthetic publication failure")
    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="synthetic publication failure"):
        store.put(day, "earnings", {"generation": 2})
    assert store.get(day, "earnings") == {"generation": 1}
    assert store.version(day, "earnings") == version
    assert list((tmp_path / day).glob("*.tmp")) == []


def test_successful_cache_refresh_changes_version_and_keeps_json_value_types(tmp_path):
    store, day = DailyStore(tmp_path), "2026-11-03"
    value = {"quotes": {"ALFA": 100.5}, "missing": None, "ready": True}
    store.put(day, "intraday", value)
    before = store.version(day, "intraday")
    store.put(day, "intraday", value)
    assert store.version(day, "intraday") != before
    assert store.get(day, "intraday") == value
