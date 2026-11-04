"""The initialized fund retains native FRED tools beside its optional regime context."""
import json

import pytest

from .test_autonomous_fund_memory_storage import config_file, entrypoint_environment  # noqa: F401


@pytest.mark.parametrize("enabled", [False, True])
def test_native_fred_tools_survive_fund_initialization(tmp_path, monkeypatch, entrypoint_environment, enabled):
    from lumibot.components.agents.runtime import GoogleADKRuntime
    from lumibot.macro import FREDMacroData
    from lumibot.strategies import Strategy
    from lumibot.strategies.strategy_executor import StrategyExecutor
    from scripts.autonomous_fund import market_data, run
    from scripts.autonomous_fund.macro import MacroWatch
    from .test_autonomous_fund_local import Clock

    config = config_file(tmp_path)
    body = json.loads(config.read_text())
    body["macro"]["enabled"] = enabled
    config.write_text(json.dumps(body))
    clock = Clock()
    source = market_data.YahooRealtimeData(now=clock, fetch=clock.fetch)
    monkeypatch.setattr(market_data, "YahooRealtimeData", lambda **kwargs: source)
    monkeypatch.setattr(run, "models_ready", lambda *args: {})
    monkeypatch.setenv("IS_BACKTESTING", "false")
    monkeypatch.setenv("FRED_API_KEY", "synthetic-fixture-only")
    monkeypatch.setattr(Strategy, "risk_free_rate", property(lambda self: 0.0))
    monkeypatch.setattr(StrategyExecutor, "_publish_run_once_final_cloud_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(MacroWatch, "_fetch", lambda *args: [("2026-10-30", 4.25)])
    monkeypatch.setattr(FREDMacroData, "_get_json", lambda *args: {
        "observations": [{"date": "2026-10-30", "value": "4.25"}]})
    seen = []

    def model(self, request):
        tools = {tool.name: tool for tool in request.bound_tools}
        series = tools["get_fred_series"].function(series_id="DGS10")
        latest = tools["get_fred_latest"].function(series_id="DGS10")
        snapshot = tools["get_fred_snapshot"].function(series_ids=["DGS10"])
        assert series["observations"][-1]["value"] == 4.25
        assert latest["latest"]["value"] == 4.25
        assert snapshot["values"]["DGS10"]["value"] == 4.25 and not snapshot["errors"]
        seen.append(request.agent_name)
        return run.ScriptedRuntime().run(request)

    monkeypatch.setattr(GoogleADKRuntime, "run", model)
    run.main(["local", "--once", "--run-dir", str(tmp_path / "book"), "--config", str(config)])
    assert seen == ["manager"]
