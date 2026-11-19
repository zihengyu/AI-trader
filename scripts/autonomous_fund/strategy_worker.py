"""Private isolated-interpreter entry point. Not an arbitrary Python sandbox."""
import json
from pathlib import Path
import sys


def main():
    raw = sys.stdin.buffer.read(32_000_001)
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
    sys.stdout.write(json.dumps(evaluate(payload), allow_nan=False, default=str))


def evaluate(payload):
    from scripts.autonomous_fund.strategy_validation import (COVERAGE_FIELDS, check_dataset, forward_days,
                                                           implementation_hash, metrics, regime_outcomes, replay,
                                                           test_excess, validate, volume_participation)
    from scripts.autonomous_fund.strategy_definition import daily_sample_requirements
    from scripts.autonomous_fund.strategy_program import StrategyRuleError
    try:
        expected = payload["policy"].get("research_execution", {}).get("source_hash")
        if expected is not None and expected != implementation_hash():
            raise ValueError("Research execution implementation changed; a new version and validation are required")
        proposal, data = payload["proposal"], payload["data"]
        if payload["kind"] in {"historical", "exploratory"}:
            if payload["kind"] == "historical" and expected is not None and not payload.get("locked_sessions"):
                return {"status": "needs_data", "passed": False, "reason": "The historical window was not locked"}
            result = validate(proposal, data, payload["policy"],
                              initial_capital=payload["initial_capital"], trials=payload["trials"],
                              allow_final=payload["kind"] == "historical", locked_sessions=payload.get("locked_sessions"),
                              require_receipts=expected is not None)
            if result["passed"]:
                from scripts.autonomous_fund.strategy_definition import StrategyProposal
                normalized = StrategyProposal.model_validate(proposal).model_dump()
                try:
                    initial = replay(normalized, data, payload["policy"], [],
                                     initial_capital=payload["initial_capital"], initial_signal=True)
                except StrategyRuleError:
                    raise
                except ValueError as exc:
                    result["forward_signal_error"] = str(exc)[:500]
                else:
                    result["next_signal"] = next_signal(initial)
        else:
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
                frozen = payload.get("forward_plan") or {
                    "required_sessions": daily_sample_requirements(plan)["forward_sessions"],
                    "trials": payload["trials"],
                }
                required = frozen["required_sessions"]
                qualification = {**frozen, "status": "collecting"}
                if len(days) >= required:
                    prefix = test_excess(metrics(actual["series"][:required], payload["initial_capital"]),
                                         metrics(baseline["series"][:required], payload["initial_capital"]),
                                         plan, frozen["trials"])
                    qualification.update(status="passed" if prefix["passed"] else "failed",
                                         start=days[0], end=days[required - 1], test=prefix)
                complete = all(data.get("coverage", {}).get(k) is True for k in COVERAGE_FIELDS)
                window = plan["monitoring_window_sessions"]
                relative_loss = 0
                if len(days) >= window:
                    recent_a = metrics(actual["series"][-window:], actual["series"][-window - 1]["net_economic_equity"]
                                       if len(days) > window else payload["initial_capital"])
                    recent_b = metrics(baseline["series"][-window:], baseline["series"][-window - 1]["net_economic_equity"]
                                       if len(days) > window else payload["initial_capital"])
                    relative_loss = recent_b["net_return"] - recent_a["net_return"]
                result = {"status": "forward", "passed": complete and test["passed"] and qualification["status"] == "passed",
                          "series": actual["series"], "reference_series": baseline["series"], "tests": {"forward": test},
                          "qualification": qualification,
                          "regimes": regime_outcomes(days, a, b, data["bars"][plan["benchmark"]]),
                          "volume_participation": volume_participation(actual["series"], data["bars"]),
                          "strategy_summary": {k: actual["summary"][k] for k in ("fees", "friction", "turnover", "orders")},
                          "next_targets": actual["next_targets"], "observations": len(days),
                          "degraded": actual["risk_paused"] or a["max_drawdown"] > plan["max_drawdown"]
                                      or relative_loss > plan["max_relative_loss"],
                          "coverage_complete": complete, "recent_relative_loss": relative_loss,
                          "cost_usd": actual["cost_usd"], "next_signal": next_signal(actual)}
    except StrategyRuleError as exc:
        result = {"status": "needs_formalization", "passed": False, "reason": str(exc)[:500]}
    except Exception as exc:
        # No private payload/paths/provider responses in controller diagnostics.
        result = {"status": "needs_data", "passed": False, "error": type(exc).__name__,
                  "reason": str(exc)[:500] if isinstance(exc, ValueError) else "Research input unavailable"}
    return result


def next_signal(simulation):
    return {"as_of": simulation["next_signal_as_of"], "raw_targets": simulation["raw_next_targets"],
            "targets": simulation["next_targets"]}


if __name__ == "__main__":
    main()
