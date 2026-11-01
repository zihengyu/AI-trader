"""Memory follow-up and controlled native replay, without provider inference."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.autonomous_fund.evaluation import forward_outcomes, frozen_memory, qualify
from scripts.autonomous_fund.risk import RiskError


AVAILABLE = datetime(2025, 1, 3, 22, tzinfo=timezone.utc)


def packet():
    return {"version": 1, "session_date": "2025-01-03", "completed_at": AVAILABLE.isoformat(),
            "market_close": "2025-01-03T21:00:00+00:00", "mock": True,
            "models": {"researcher": "scripted/no-inference", "manager": "scripted/no-inference"},
            "review": {"session_date": "2025-01-03", "as_of": AVAILABLE.isoformat(), "summary": "Synthetic prior memo",
                "selection_adjustments": [], "timing_adjustments": [], "retained_lessons": [],
                "retired_hypotheses": [], "data_gaps": ["Synthetic fixture"], "confidence": "low",
                "strategy_memo": "Synthetic provisional hypothesis, not a demonstrated advantage."}}


def test_frozen_memory_rejects_known_future_completion_or_close():
    with pytest.raises(RiskError, match="not available"):
        frozen_memory(packet(), AVAILABLE - timedelta(seconds=1))
    body = packet()
    body["market_close"] = (AVAILABLE + timedelta(days=1)).isoformat()
    with pytest.raises(RiskError, match="not available"):
        frozen_memory(body, AVAILABLE)
    assert frozen_memory(packet(), AVAILABLE)["qualification_frozen"] is True


def test_followup_requires_actual_completed_session_observations_and_discloses_attribution():
    events = [(AVAILABLE.isoformat(), "postmarket_review", packet())]
    # No padded observations: a missing account day is not assumed flat.
    dates = ["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10", "2025-01-13"]
    for i, day in enumerate(dates):
        at = day + "T21:01:00+00:00"
        events.append((at, "account", {"equity": str(10000 + i * 10)}))
        events.append((at, "benchmark", {"SPY": str(100 + i), "QQQ": str(200 + i)}))
    result = forward_outcomes(events)[0]
    assert result["observed_sessions"] == 5  # A synthetic Jan 9 row must not become an exchange session.
    assert result["horizons"]["5"]["status"] == "insufficient_observations"
    # Add another actual exchange session, without changing the prior evidence.
    at = "2025-01-15T21:01:00+00:00"
    events.extend([(at, "account", {"equity": "10060"}), (at, "benchmark", {"SPY": "106", "QQQ": "206"})])
    result = forward_outcomes(events)[0]
    assert result["horizons"]["5"]["status"] == "observed"
    assert result["horizons"]["20"]["status"] == "insufficient_observations"
    assert "no causal attribution" in result["interpretation"]


def history(directory):
    directory.mkdir()
    dates = pd.bdate_range("2024-11-01", "2025-02-14", tz="America/New_York")
    for offset, symbol in enumerate(("ALFA", "BETA", "SPY", "QQQ")):
        prices = [100 + offset * 10 + i * .03 for i in range(len(dates))]
        pd.DataFrame({"timestamp": dates, "open": prices, "close": prices,
            "high": [p * 1.01 for p in prices], "low": [p * .99 for p in prices], "volume": 5_000_000}).to_csv(directory / f"{symbol}.csv", index=False)


def args(tmp_path):
    source = tmp_path / "history"
    history(source)
    memory = tmp_path / "frozen.json"
    memory.write_text(json.dumps(packet()), encoding="utf-8")
    root = tmp_path / "qualification"
    root.mkdir()
    return SimpleNamespace(data_dir=source, memory_file=memory, start="2025-01-06", end="2025-01-14",
        run_dir=root, scripted=True, model=None, research_model=None, decision_model=None)


def config():
    return {"model": "deepseek/deepseek-flash", "limits": {}, "universe": {
        "ALFA": "stock", "BETA": "stock", "SPY": "unlevered_etf", "QQQ": "unlevered_etf"}}


def test_paired_replay_uses_same_native_csv_history_and_does_not_promote_small_samples(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMIBOT_CACHE_FOLDER", str(tmp_path / "cache"))
    result = qualify(args(tmp_path), config())
    assert result["status"] == "insufficient_sample"
    assert result["auto_promoted"] is False and result["scripted_model"] is True
    assert result["baseline"]["fills_observed"] >= 1
    assert result["candidate"]["fills_observed"] >= 1
    assert result["baseline"]["return"] == result["candidate"]["return"]
    assert result["baseline"]["postmarket_review_runs"] == 0
    assert result["candidate"]["postmarket_review_runs"] == 0
    assert len(result["history_sha256"]) == 4
    assert (tmp_path / "qualification" / "qualification.json").exists()


def test_missing_universe_data_is_rejected_before_any_model_run(tmp_path):
    values = args(tmp_path)
    (values.data_dir / "BETA.csv").unlink()
    with pytest.raises(RiskError, match="complete configured"):
        qualify(values, config())


def test_future_memory_is_rejected_before_creating_replay_accounts(tmp_path):
    values = args(tmp_path)
    future = packet()
    future["completed_at"] = "2025-02-01T22:00:00+00:00"
    values.memory_file.write_text(json.dumps(future), encoding="utf-8")
    with pytest.raises(RiskError, match="not available"):
        qualify(values, config())
    assert not (values.run_dir / "baseline").exists()
