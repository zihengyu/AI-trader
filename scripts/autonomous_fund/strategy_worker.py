"""Private isolated-interpreter entry point. Not an arbitrary Python sandbox."""
import json
from pathlib import Path
import sys


def main():
    raw = sys.stdin.read(32_000_001)
    if len(raw) > 32_000_000:
        raise ValueError("Strategy worker input exceeds its bound")
    payload = json.loads(raw)
    # Only trusted repository modules are imported. Strategy implementations are
    # parsed as expressions by the capability-limited interpreter, never imported.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        import resource
        seconds = min(1200, max(1, int(payload["timeout_seconds"])))
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds + 1))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        if sys.platform != "darwin":
            resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
    except ImportError:
        pass  # Parent still imposes wall-clock/input/output bounds on Windows.
    from scripts.autonomous_fund.strategy_validation import check_dataset, forward_days, implementation_hash, metrics, replay, test_excess, validate
    try:
        expected = payload["policy"].get("research_execution", {}).get("source_hash")
        if expected is not None and expected != implementation_hash():
            raise ValueError("Research execution implementation changed; a new version and validation are required")
        if payload["kind"] in {"historical", "exploratory"}:
            result = validate(payload["proposal"], payload["data"], payload["policy"],
                              initial_capital=payload["initial_capital"], trials=payload["trials"],
                              allow_final=payload["kind"] == "historical")
        else:
            proposal, data = payload["proposal"], payload["data"]
            check_dataset(data)
            plan = proposal["validation"]
            days = forward_days(data, plan["benchmark"], payload["forward_after"])
            if not days:
                result = {"status": "awaiting_forward", "passed": False}
            else:
                args = dict(initial_capital=payload["initial_capital"])
                actual = replay(proposal, data, payload["policy"], days, **args)
                baseline = replay(proposal, data, payload["policy"], days,
                                  static_weight=plan["benchmark_weight"], **args)
                a, b = metrics(actual["series"], payload["initial_capital"]), metrics(baseline["series"], payload["initial_capital"])
                test = test_excess(a, b, plan, payload["trials"])
                complete = all(data.get("coverage", {}).get(k) is True for k in (
                    "raw_prices", "corporate_actions_complete", "point_in_time_membership", "publication_times_complete"))
                window = plan["monitoring_window_sessions"]
                relative_loss = 0
                if len(days) >= window:
                    recent_a = metrics(actual["series"][-window:], actual["series"][-window - 1]["net_economic_equity"]
                                       if len(days) > window else payload["initial_capital"])
                    recent_b = metrics(baseline["series"][-window:], baseline["series"][-window - 1]["net_economic_equity"]
                                       if len(days) > window else payload["initial_capital"])
                    relative_loss = recent_b["net_return"] - recent_a["net_return"]
                result = {"status": "forward", "passed": complete and test["passed"] and len(days) >= plan["forward_sessions"],
                          "series": actual["series"], "reference_series": baseline["series"], "tests": {"forward": test},
                          "next_targets": actual["next_targets"], "observations": len(days),
                          "degraded": actual["risk_paused"] or a["max_drawdown"] > plan["max_drawdown"]
                                      or relative_loss > plan["max_relative_loss"],
                          "coverage_complete": complete, "recent_relative_loss": relative_loss,
                          "cost_usd": actual["cost_usd"]}
    except Exception as exc:
        # No private payload/paths/provider responses in controller diagnostics.
        result = {"status": "needs_data", "passed": False, "error": type(exc).__name__,
                  "reason": str(exc)[:500] if isinstance(exc, ValueError) else "Research input unavailable"}
    sys.stdout.write(json.dumps(result, allow_nan=False, default=str))


if __name__ == "__main__":
    main()
