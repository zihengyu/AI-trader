"""Operator settings from .env / environment: mapping, validation, precedence, redaction."""
import json
import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

from scripts.autonomous_fund.risk import RiskError
from scripts.autonomous_fund.settings import SETTINGS
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


# ----- Startup mode from .env: FUND_MODE / FUND_RUN_DIR / FUND_OBSERVE_ONLY --------------

def test_fund_mode_selects_the_simulation_and_its_own_account_directory(tmp_path, monkeypatch):
    from scripts.autonomous_fund.settings import startup
    monkeypatch.chdir(tmp_path)
    assert startup("start", None, {"FUND_MODE": "paper"})[:2] == ("paper", Path("artifacts/fund-paper"))
    assert startup("start", None, {"FUND_MODE": " Local "})[:2] == ("local", Path("artifacts/fund-local"))
    assert startup("start", None, {})[:2] == ("local", Path("artifacts/fund-local"))  # Virtual money by default.
    # Account commands without --run-dir act on the account FUND_MODE runs.
    assert startup("stop", None, {"FUND_MODE": "paper"})[1] == Path("artifacts/fund-paper")
    book = {"FUND_MODE": "paper", "FUND_RUN_DIR": "artifacts/book"}
    assert startup("approvals", None, book)[1] == startup("start", None, book)[1] == Path("artifacts/book")
    # An explicit command or directory wins; FUND_RUN_DIR belongs to the FUND_MODE account only.
    assert startup("local", None, book)[:2] == ("local", Path("artifacts/fund-local"))
    assert startup("start", tmp_path / "chosen", book)[1] == tmp_path / "chosen"
    # Historical and offline commands never write into an account directory.
    for command in ("mock", "backtest", "qualify", "validate-risk", "show-config", "schwab-check", "check"):
        assert startup(command, None, {"FUND_MODE": "local", "FUND_RUN_DIR": "artifacts/book"})[1] == \
            Path("artifacts/autonomous-fund")


@pytest.mark.parametrize("value", ["live", "schwab", "real", "实盘", "paper-live"])
def test_no_mode_value_reaches_real_money(tmp_path, value):
    from scripts.autonomous_fund.settings import startup
    with pytest.raises(RiskError, match="no live"):
        startup("start", None, {"FUND_MODE": value})
    with pytest.raises(RiskError, match="no live"):
        startup("approvals", None, {"FUND_MODE": value})
    # The emergency stop is never blocked by a configuration error.
    assert startup("stop", None, {"FUND_MODE": value, "FUND_RUN_DIR": str(tmp_path)})[1] == tmp_path
    assert startup("stop", tmp_path, {"FUND_MODE": value})[1] == tmp_path
    with pytest.raises(RiskError, match="--run-dir"):
        startup("stop", None, {"FUND_MODE": value})


def test_observe_only_from_the_env_file_is_a_local_option():
    from scripts.autonomous_fund.settings import startup
    assert startup("start", None, {"FUND_MODE": "local", "FUND_OBSERVE_ONLY": "true"})[2] is True
    assert startup("start", None, {"FUND_MODE": "local", "FUND_OBSERVE_ONLY": ""})[2] is False
    with pytest.raises(RiskError, match="local"):
        startup("start", None, {"FUND_MODE": "paper", "FUND_OBSERVE_ONLY": "true"})
    assert startup("stop", None, {"FUND_MODE": "paper", "FUND_OBSERVE_ONLY": "true"})[2] is False


def test_an_account_in_the_earlier_default_directory_keeps_being_used(tmp_path, monkeypatch):
    from scripts.autonomous_fund.journal import Journal
    from scripts.autonomous_fund.settings import startup
    monkeypatch.chdir(tmp_path)
    Journal(tmp_path / "artifacts/autonomous-fund", "synthetic-earlier").event("2026-10-30", "startup", {"mode": "local"})
    assert startup("start", None, {"FUND_MODE": "local"})[1] == Path("artifacts/autonomous-fund")
    assert startup("stop", None, {"FUND_MODE": "local"})[1] == Path("artifacts/autonomous-fund")
    assert startup("start", None, {"FUND_MODE": "paper"})[1] == Path("artifacts/fund-paper")  # Another account.
    Journal(tmp_path / "artifacts/fund-local", "synthetic-new")
    assert startup("start", None, {"FUND_MODE": "local"})[1] == Path("artifacts/fund-local")  # The new book wins.


def cli(tmp_path, monkeypatch, argv, env_text):
    import logging
    import os
    from scripts.autonomous_fund import run
    for name in list(SETTINGS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    env = tmp_path / ".env"
    env.write_text(env_text, encoding="utf-8")
    env.chmod(0o600)
    before, handlers = set(os.environ), list(logging.getLogger().handlers)
    try:
        run.main([*argv, "--env-file", str(env)])
    finally:
        for name in set(os.environ) - before:  # The .env loader writes the process environment.
            os.environ.pop(name, None)
        for handler in set(logging.getLogger().handlers) - set(handlers):
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_starting_without_a_command_runs_the_mode_from_the_env_file(tmp_path, monkeypatch, capsys):
    from scripts.autonomous_fund import run
    import scripts.autonomous_fund.report as report_module
    seen = []
    monkeypatch.setattr(run, "run_local", lambda args, config: seen.append((args.mode, args.run_dir, args.observe_only)))
    monkeypatch.setattr(report_module, "report", lambda *args, **kwargs: {})
    cli(tmp_path, monkeypatch, [], "FUND_MODE=local\nFUND_OBSERVE_ONLY=true\n")
    assert seen == [("local", Path("artifacts/fund-local"), True)]
    out = capsys.readouterr().out
    assert "FUND_MODE" in out and "artifacts/fund-local" in out


def test_stop_works_even_when_the_env_file_has_a_mistake(tmp_path, monkeypatch, capsys):
    cli(tmp_path, monkeypatch, ["stop"], "FUND_MODE=paper\nFUND_MAX_POSITON=0.05\n")
    assert (tmp_path / "artifacts/fund-paper/STOP").exists()
    assert "FUND_MAX_POSITON" in capsys.readouterr().out  # The mistake is still reported.
