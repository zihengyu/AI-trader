"""V2 4.1: the experiment registry records every experiment version, its state and an append-only history.

Synthetic journals, factor rows, shadow decisions and closes only; no network, broker or model inference.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scripts.autonomous_fund.experiments import Registry, lines, observations, report
from scripts.autonomous_fund.journal import Journal

NOW = datetime(2026, 11, 9, 21, 30, tzinfo=timezone.utc)


def entry(state, **extra):
    return {"id": "learning:memo", "kind": "learning_ab", "hypothesis": "memo helps",
            "dependencies": ["shadow decisions"], "config": {"arm": "memo", "prompt_version": "p1"},
            "state": state, **extra}


def test_states_are_recorded_once_and_every_change_keeps_its_reason(tmp_path):
    registry = Registry(Journal(tmp_path, "synthetic-registry-account"))
    assert registry.observe(NOW, [entry("shadow")]) == [("learning:memo", registry.rows()[0]["version"], None,
                                                         "shadow", "registered")]
    assert registry.observe(NOW + timedelta(days=1), [entry("shadow")]) == []  # Unchanged: no new history.
    written = registry.observe(NOW + timedelta(days=2), [entry("paper_active", reason="t = 2.4 over 20 pairs",
                                                               evidence={"t_stat": 2.4})])
    assert [w[2:] for w in written] == [("shadow", "paper_active", "t = 2.4 over 20 pairs")]
    history = registry.history()
    assert [(h["from_state"], h["to_state"]) for h in history] == [(None, "shadow"), ("shadow", "paper_active")]
    assert json.loads(history[-1]["evidence"]) == {"t_stat": 2.4}
    assert registry.state("learning:memo") == "paper_active"
    with pytest.raises(ValueError, match="unknown experiment state"):
        registry.observe(NOW, [entry("winning")])


def test_a_changed_configuration_is_a_new_version_and_the_old_one_retires(tmp_path):
    registry = Registry(Journal(tmp_path, "synthetic-registry-account"))
    registry.observe(NOW, [entry("paper_active")])
    first = registry.rows()[0]["version"]
    newer = entry("paper_active", config={"arm": "memo", "prompt_version": "p2"})
    written = registry.observe(NOW + timedelta(days=1), [newer])
    rows = {row["version"]: row for row in registry.rows()}
    assert len(rows) == 2 and rows[first]["state"] == "retired"  # Never overwritten: both versions stay.
    second = next(version for version in rows if version != first)
    assert written[0] == ("learning:memo", first, "paper_active", "retired", f"superseded by version {second}")
    assert json.loads(rows[first]["config"])["prompt_version"] == "p1"
    assert registry.state("learning:memo") == "paper_active"


def factor_row(identifier, status, expression="rank(ret(21))", reason=None):
    return {"id": identifier, "name": identifier, "expression": expression, "horizon": 5, "universe": "stocks",
            "hypothesis": "momentum persists", "status": status, "reason": reason or status,
            "evidence": json.dumps({"status": status, "direction": 1, "in_sample": {"n": 40, "t": 3.1},
                                    "walk_forward": {"windows": [], "agreeing": 3, "required": 3}})}


def test_observations_map_each_experiment_to_one_state():
    seen = {}
    entries = observations(
        factors=[factor_row("f1", "validated"), factor_row("f2", "rejected"),
                 factor_row("f3", "insufficient_data", "rank(earnings / (close * shares))"), factor_row("f4", "retired")],
        learning={"enabled": True, "champion": "baseline", "version_config": {"prompt_version": "p1"},
                  "evaluation": {"memo_minus_baseline_1d_excess": {"pairs": 22, "t_stat": -2.3}}},
        construction={"trading": "ai", "config": {"mode": "auto"}, "evaluation": {"test": {"pairs": 4}}},
        scorecards={"ideas": {"verdict": "insufficient_sample", "scored": 3, "minimum_sample": 30},
                    "predictions": {"total": 5, "scored": 2, "hit_rate": 0.5}},
        previous=seen.get)
    states = {item["id"]: item["state"] for item in entries}
    assert states == {"factor:f1": "paper_active", "factor:f2": "rejected", "factor:f3": "historical_test",
                      "factor:f4": "retired", "learning:memo": "shadow", "learning:baseline": "paper_active",
                      "construction:ai": "paper_active", "construction:risk_budget": "shadow",
                      "predictions:research_ideas": "shadow", "predictions:review": "shadow"}
    fundamentals = next(item for item in entries if item["id"] == "factor:f3")
    assert "SEC point-in-time fundamentals" in fundamentals["dependencies"]
    assert next(item for item in entries if item["id"] == "factor:f1")["evidence"]["walk_forward_agreeing"] == 3
    assert next(item for item in entries if item["id"] == "learning:baseline")["evidence"]["t_stat"] == -2.3


def test_prediction_statistics_pause_only_after_they_were_active():
    def ideas(verdict, previous):
        return observations(scorecards={"ideas": {"verdict": verdict}}, previous=lambda _: previous)[0]["state"]
    assert ideas("informative", None) == "paper_active"
    assert ideas("not_informative", None) == "shadow"  # Never trusted, so nothing to pause.
    assert ideas("not_informative", "paper_active") == "paused"


def fund(tmp_path):
    from scripts.autonomous_fund.construction import ConstructionSettings
    from scripts.autonomous_fund.factors import FactorLab, FactorSettings
    from tests.test_autonomous_fund_validation import closes_for, learning_harness, sessions
    days = sessions(30)
    strategy = learning_harness(tmp_path, days, closes_for(days, .001, -.001))
    strategy.prompt_version = "synthetic-prompt"
    strategy.construction = ConstructionSettings()
    strategy.factor_lab = FactorLab(strategy.journal, FactorSettings())
    with strategy.journal.connection() as db:
        db.execute("INSERT INTO fund_factors(id,expression,horizon,name,hypothesis,proposed_at,session_date,status,"
                   "direction,evidence,validated_at,retired_at,reason,universe) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("f1", "rank(ret(21))", 5, "momentum", "momentum persists", NOW.isoformat(), "2026-11-09",
                    "validated", 1, "{}", "2026-11-09", None, "agree", "stocks"))
    strategy.get_datetime = lambda: NOW
    return strategy


def test_each_review_syncs_the_registry_from_the_experiments_own_records(tmp_path):
    strategy = fund(tmp_path)
    written = strategy._sync_experiments(NOW)
    states = {item[0]: item[3] for item in written}
    assert states["factor:f1"] == "paper_active" and states["learning:memo"] == "paper_active"
    assert states["learning:baseline"] == "shadow" and states["construction:ai"] == "paper_active"
    assert strategy._sync_experiments(NOW + timedelta(days=1)) == []  # Nothing changed.
    strategy.journal.set_meta("learning_champion", "baseline")  # The learning comparison switched.
    changed = {item[0]: item[2:4] for item in strategy._sync_experiments(NOW + timedelta(days=2))}
    assert changed == {"learning:memo": ("paper_active", "shadow"), "learning:baseline": ("shadow", "paper_active")}
    strategy.prompt_version = "edited-prompt"  # A new prompt is a new version of both arms.
    retired = [item for item in strategy._sync_experiments(NOW + timedelta(days=3)) if item[3] == "retired"]
    assert {item[0] for item in retired} == {"learning:memo", "learning:baseline"}
    text = "\n".join(report(tmp_path))
    assert "[模拟盘生效] 学习对照 baseline" in text and "退役" in text and "superseded by version" in text


def test_the_report_is_read_only_and_says_when_nothing_is_registered(tmp_path):
    assert report(tmp_path) == ["实验登记：账户目录里还没有账本"]
    Journal(tmp_path, "synthetic-registry-account")
    assert report(tmp_path) == ["实验登记：", "- 还没有登记的实验（第一次盘后复盘后开始登记）"]
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='fund_experiments'").fetchone()
    assert lines([], []) == ["实验登记：", "- 还没有登记的实验（第一次盘后复盘后开始登记）"]


def test_the_weekly_report_lists_this_weeks_registry_changes(tmp_path):
    from scripts.autonomous_fund.notifications import weekly_document
    from tests.test_autonomous_fund_weekly_report import close_of, ledger
    journal, sequence = ledger(tmp_path)
    registry = Registry(journal)
    registry.observe(close_of("2026-11-06", 31), [entry("shadow")])
    registry.observe(close_of("2026-11-06", 32), [entry("paper_active", reason="t = 2.4")])
    registry.observe(close_of("2026-11-09", 600), [entry("paused", reason="next week")])
    _, text = weekly_document(tmp_path, sequence)
    # Synced just after the review was saved (21:31 and 21:32 against a 21:30 cutoff), still this week's.
    assert "实验登记本周的状态变化（2 条" in text and "learning:memo：影子观察 → 模拟盘生效（t = 2.4）" in text
    assert "next week" not in text
