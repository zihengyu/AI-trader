"""Role selection preserves account risk state and attributes separate model usage."""
import copy
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.journal import Journal, encode
from scripts.autonomous_fund.report import report
from scripts.autonomous_fund.risk import Intent, Limits, RiskError, number
from scripts.autonomous_fund.run import models_ready
from scripts.autonomous_fund.strategy import account_digest, configuration, model_selection


FLASH = "deepseek/deepseek-flash"
PRO = "deepseek/deepseek-v4-pro"
NOW = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)


def test_interrupted_provider_attempt_records_unknown_usage_and_propagates(tmp_path):
    from scripts.autonomous_fund.strategy import AutonomousFund
    strategy = object.__new__(AutonomousFund)
    strategy.journal = Journal(tmp_path, "synthetic-interrupted-call")
    strategy.get_datetime = lambda: NOW
    strategy.agent_models = {"manager": PRO}
    strategy.scripted = False
    strategy.agents = {"manager": SimpleNamespace(run=lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))}
    with pytest.raises(KeyboardInterrupt):
        strategy._agent_call("manager", context={"as_of": NOW.isoformat()})
    result = report(tmp_path, write_files=False)
    assert result["model_usage"]["unknown_calls"] == 1
    assert result["model_cost_estimate_usd"] is None


def config():
    return {"model": FLASH, "reasoning_effort": None, "limits": {}, "universe": {"ALFA": "stock"}}


def test_default_role_models_and_legacy_single_model_compatibility():
    from pathlib import Path
    current = configuration(Path(__file__).resolve().parents[1] / "scripts/autonomous_fund/config.json")
    assert model_selection(current) == {"researcher": FLASH, "manager": PRO}
    assert model_selection(config()) == {"researcher": FLASH, "manager": FLASH}
    assert model_selection(current, model="anthropic/fixture") == {
        "researcher": "anthropic/fixture", "manager": "anthropic/fixture"}
    assert model_selection(current, model="anthropic/fixture", decision_model=PRO) == {
        "researcher": "anthropic/fixture", "manager": PRO}
    assert model_selection(current, research_model="openai/fixture") == {
        "researcher": "openai/fixture", "manager": PRO}


@pytest.mark.parametrize("roles", [None, [], {"trader": PRO}, {"manager": "deepseek-v4-pro"},
                                  {"manager": "deepseek/"}, {"manager": 3}, {"researcher": "deepseek/a b"}])
def test_invalid_role_configuration_fails_explicitly(roles):
    with pytest.raises(RiskError):
        model_selection({**config(), "agent_models": roles})


def test_switching_role_models_retains_legacy_book_and_drawdown_latch(tmp_path):
    old = config()
    legacy_digest = hashlib.sha256(encode({"mode": "local", "account": "fixture-account", "config": old}).encode()).hexdigest()
    journal = Journal(tmp_path, legacy_digest)
    intent = journal.reserve(Intent("ALFA", "buy", 1, number(100)), NOW, Limits())
    journal.mark_equity(NOW, number(12000), Limits())
    assert journal.mark_equity(NOW, number(10500), Limits())
    new = {**old, "agent_models": {"researcher": FLASH, "manager": PRO}}
    assert account_digest("local", "fixture-account", new) == legacy_digest
    restored = Journal(tmp_path, account_digest("local", "fixture-account", new))
    assert restored.pending()[0]["id"] == intent
    assert restored.mark_equity(NOW, number(10500), Limits())
    with restored.connection() as db:
        assert dict(db.execute("SELECT key,value FROM meta"))["peak"] == "12000"
        assert db.execute("SELECT count(*) FROM intents").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["limits", "universe", "account", "mode"])
def test_role_configuration_cannot_rebind_a_different_account_or_risk_policy(tmp_path, change):
    base = config()
    Journal(tmp_path, account_digest("local", "fixture-account", base))
    modified = copy.deepcopy(base)
    modified["agent_models"] = {"manager": PRO}
    account, mode = "fixture-account", "local"
    if change == "limits":
        modified["limits"] = {"max_position": "0.20"}
    elif change == "universe":
        modified["universe"]["BETA"] = "stock"
    elif change == "account":
        account = "other-account"
    else:
        mode = "paper"
    with pytest.raises(RiskError, match="another mode, account or configuration"):
        Journal(tmp_path, account_digest(mode, account, modified))


def test_selected_providers_require_all_role_keys(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    args = SimpleNamespace(model=None, research_model=None, decision_model=None)
    current = {**config(), "agent_models": {"researcher": FLASH, "manager": PRO}}
    assert models_ready(args, current)["manager"] == PRO
    args.decision_model = "anthropic/fixture"
    with pytest.raises(RiskError, match="manager: Missing model environment variable: ANTHROPIC_API_KEY"):
        models_ready(args, current)


def test_per_model_usage_and_prices_do_not_blend_flash_and_pro(tmp_path):
    journal = Journal(tmp_path, "usage-fixture")
    journal.event(NOW, "startup", {"mode": "local", "models": {"researcher": FLASH, "manager": PRO}})
    journal.event(NOW, "agent", {"name": "researcher", "model": FLASH,
        "usage": {"prompt_token_count": 100, "candidates_token_count": 20, "cached_content_token_count": 30}})
    journal.event(NOW, "agent", {"name": "manager", "model": PRO,
        "usage": {"prompt_tokens": 200, "completion_tokens": 40, "cached_content_token_count": None}})
    journal.event(NOW, "agent", {"model": PRO, "cache_hit": True, "usage": {"prompt_tokens": 99999}})
    flat = report(tmp_path, {"input": 1, "output": 2}, write_files=False)
    assert flat["model_cost_estimate_usd"] is None
    assert flat["model_usage"]["input_tokens"] == 300
    assert flat["model_usage_by_model"][FLASH]["cached_input_tokens"] == 30
    assert flat["model_usage_by_model"][PRO]["output_tokens"] == 40
    rates = {FLASH: {"input": 1, "output": 2}, PRO: {"input": 3, "output": 4}}  # Synthetic rates.
    result = report(tmp_path, rates, write_files=False)
    assert result["model_cost_estimate_usd"] == pytest.approx(.00090)
    assert result["model_cost_estimate_by_model_usd"][PRO] == pytest.approx(.00076)
    assert report(tmp_path, {FLASH: rates[FLASH]}, write_files=False)["model_cost_estimate_usd"] is None
    journal.event(NOW, "agent", {"model": PRO, "usage": None})
    assert report(tmp_path, rates, write_files=False)["model_cost_estimate_usd"] is None
