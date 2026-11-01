"""Operator settings from .env / environment: mapping, validation, precedence, redaction."""
import json
import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

from scripts.autonomous_fund.risk import RiskError
from scripts.autonomous_fund.settings import SETTINGS, apply_environment
from scripts.autonomous_fund.strategy import configuration, legacy_account_digest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "scripts/autonomous_fund/config.json"
TEMPLATE = ROOT / "scripts/autonomous_fund/.env.example"


def effective(environ):
    return configuration(CONFIG, environ)


def test_user_limits_models_and_external_parameters_come_from_the_environment():
    config = effective({
        "FUND_CAPITAL_USD": "10000", "FUND_MAX_POSITION": "0.08", "FUND_MAX_EXPOSURE": "0.60",
        "FUND_MAX_DRAWDOWN": "0.06", "FUND_DAILY_LOSS_LIMIT": "0.015", "FUND_DAILY_ORDERS": "12",
        "FUND_INSTRUCTIONS": "不追涨|财报前不建仓", "FUND_RESEARCH_MODEL": "deepseek/deepseek-flash",
        "FUND_AUTO_DAILY_EXPOSURE_INCREASE": "0", "FUND_STRESS_REQUIRES_APPROVAL": "false",
        "FUND_ITERATION_SECONDS": "20", "FUND_LIVE_STREAM": "off", "FUND_NEWS_QUERIES": "AAPL earnings|CPI",
        "FUND_NEWS_RSS_FEEDS": "macro=https://example.com/rss.xml", "FUND_RESEARCH_RESOLVER": "doh",
        "FUND_SHADOW_AB": "no", "FUND_MAX_AGENT_RUNS_PER_DAY": "30"})
    assert config["limits"] == {"max_position": "0.08", "max_exposure": "0.60", "daily_orders": 12, "drawdown": "0.06"}
    assert config["mandate"]["capital_usd"] == "10000"
    assert config["mandate"]["instructions"] == ["不追涨", "财报前不建仓"]
    assert config["quant_risk"]["daily_loss_limit"] == "0.015"
    assert config["approval"]["auto_daily_exposure_increase"] == "0" and config["approval"]["stress_requires_approval"] is False
    assert config["execution"]["iteration_seconds"] == 20 and config["market_data"]["live_stream"] is False
    assert config["news"]["queries"] == ["AAPL earnings", "CPI"]
    assert config["news"]["rss_feeds"] == [{"name": "macro", "url": "https://example.com/rss.xml"}]
    assert config["research_network"]["resolver"] == "doh" and config["validation"]["shadow_enabled"] is False
    assert config["model_budget"]["max_agent_runs_per_day"] == 30
    audited = {row["variable"] for row in config["_environment_overrides"]}
    assert {"FUND_CAPITAL_USD", "FUND_MAX_POSITION", "FUND_INSTRUCTIONS"} <= audited


def test_blank_values_keep_config_json_and_account_means_the_whole_account():
    assert effective({"FUND_CAPITAL_USD": "", "FUND_MAX_POSITION": "  "}) == effective({})
    assert effective({"FUND_CAPITAL_USD": "account"})["mandate"]["capital_usd"] is None


@pytest.mark.parametrize("name, value, message", [
    ("FUND_MAX_POSITION", "ten percent", "FUND_MAX_POSITION"),
    ("FUND_DAILY_ORDERS", "1.5", "FUND_DAILY_ORDERS"),
    ("FUND_LIVE_STREAM", "maybe", "FUND_LIVE_STREAM"),
    ("FUND_NEWS_RSS_FEEDS", "no-equals-sign", "FUND_NEWS_RSS_FEEDS"),
    ("FUND_MODEL_PRICES_JSON", "{not json", "FUND_MODEL_PRICES_JSON"),
])
def test_unparseable_values_name_the_variable(name, value, message):
    with pytest.raises(RiskError, match=message):
        effective({name: value})


@pytest.mark.parametrize("environ", [
    {"FUND_MAX_POSITION": "0.90"},                 # Above the exposure cap.
    {"FUND_DAILY_ORDERS": "500"},                  # Outside the hard range.
    {"FUND_ITERATION_SECONDS": "1"},
    {"FUND_CAPITAL_USD": "-5"},
    {"FUND_RESEARCH_RESOLVER": "magic"},
    {"FUND_NEWS_RSS_FEEDS": "x=http://insecure"},
])
def test_overrides_pass_through_the_same_policy_validation_as_config_json(environ):
    with pytest.raises(RiskError):
        effective(environ)


def test_mistyped_setting_in_the_env_file_is_refused_with_a_suggestion(tmp_path, monkeypatch, capsys):
    with pytest.raises(RiskError, match=r"FUND_CAPTIAL_USD \(did you mean FUND_CAPITAL_USD\?\)"):
        run_show(tmp_path, monkeypatch, capsys, "FUND_CAPTIAL_USD=10000\n")
    assert "FUND_CAPTIAL_USD" not in __import__("os").environ  # Refused before anything was loaded.


def test_unrelated_fund_variables_in_the_process_environment_do_not_block_startup():
    assert effective({"FUND_SOME_OTHER_TOOL": "x"}) == effective({})


def test_excluded_symbols_leave_the_universe_and_every_group():
    config = effective({"FUND_EXCLUDE_SYMBOLS": "nvda, XOM"})
    assert "NVDA" not in config["universe"] and "XOM" not in config["universe"]
    assert all("NVDA" not in spec["members"] for spec in config["quant_risk"]["groups"].values())
    with pytest.raises(RiskError, match="not in the eligible universe"):
        effective({"FUND_EXCLUDE_SYMBOLS": "TSLA"})


def test_shipped_template_lists_every_setting_and_changes_no_default():
    text = TEMPLATE.read_text(encoding="utf-8")
    named = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, flags=re.M))
    assert named == set(SETTINGS)
    values = {k: v for k, v in dotenv_values(TEMPLATE).items() if v is not None}
    with_template = effective(values)
    with_template.pop("_environment_overrides", None)
    assert with_template == effective({})  # Copying the template is a no-op until the operator edits it.
    assert not any(values.get(name) for name, spec in SETTINGS.items() if spec[2])  # No secret value shipped.


def test_overrides_never_change_the_account_identity_digest():
    plain, overridden = effective({}), effective({"FUND_MAX_POSITION": "0.05"})
    assert legacy_account_digest("local", "x", {**plain, "limits": overridden["limits"]}) == \
        legacy_account_digest("local", "x", {**overridden})


def run_show(tmp_path, monkeypatch, capsys, env_text, environ=None):
    from scripts.autonomous_fund import run
    for name in list(SETTINGS):
        monkeypatch.delenv(name, raising=False)
    for name, value in (environ or {}).items():
        monkeypatch.setenv(name, value)
    env = tmp_path / ".env"
    env.write_text(env_text, encoding="utf-8")
    env.chmod(0o600)
    import os
    before = set(os.environ)
    try:
        run.main(["show-config", "--run-dir", str(tmp_path / "run"), "--env-file", str(env)])
    finally:
        for name in set(os.environ) - before:  # The .env loader writes the process environment.
            os.environ.pop(name, None)
    out = capsys.readouterr().out
    return json.loads(out[out.index("{"):]), out


def test_process_environment_wins_over_the_env_file_and_secrets_are_redacted(tmp_path, monkeypatch, capsys):
    shown, out = run_show(tmp_path, monkeypatch, capsys,
                          "DEEPSEEK_API_KEY=sk-synthetic-secret\nFUND_CAPITAL_USD=12000\n",
                          {"FUND_CAPITAL_USD": "5000"})
    rows = {row["variable"]: row for row in shown["settings"]}
    assert rows["FUND_CAPITAL_USD"]["value"] == "5000" and "environment" in rows["FUND_CAPITAL_USD"]["source"]
    assert rows["DEEPSEEK_API_KEY"]["value"] == "set" and "sk-synthetic-secret" not in out
    assert rows["FUND_MAX_POSITION"]["source"] == "config.json"


def test_role_models_from_the_environment_rank_above_ai_model(tmp_path, monkeypatch, capsys):
    shown, _ = run_show(tmp_path, monkeypatch, capsys, "FUND_RESEARCH_MODEL=openai/fixture-research\n",
                        {"AI_MODEL": "anthropic/fixture-common"})
    assert shown["models"] == {"researcher": "openai/fixture-research", "manager": "anthropic/fixture-common"}
