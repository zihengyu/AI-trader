"""Operator diagnostics/import/run entry point. Never connects to a broker or a model."""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .risk import RiskError


def report(root):
    path = Path(root) / "state.sqlite3"
    if not path.exists():
        return ["Strategy Lab：尚无账户账本。"]
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fund_strategy_versions'").fetchone():
            return ["Strategy Lab：尚未初始化；原有账户与交易状态未改变。"]
        policy = db.execute("SELECT value FROM meta WHERE key='strategy_lab_policy'").fetchone()
        policy = json.loads(policy[0]) if policy else {}
        lines = ["Strategy Lab：完整策略研究（历史结果不代表已证明 Alpha）",
                 f"准入模式：{policy.get('admission', 'research_only')}；资金分配：{policy.get('allocation_mode', 'legacy_mandate')}"]
        for row in db.execute("SELECT * FROM fund_strategy_versions ORDER BY created_at,rowid"):
            proposal = json.loads(row["proposal"])
            lines.append(f"- {row['id']} / {row['version']} [{row['state']}] 研究任务：{row['task_state']}｜{proposal['name']}")
            job = db.execute("SELECT j.kind,r.at,r.body FROM fund_strategy_jobs j JOIN fund_strategy_results r "
                             "ON r.job_id=j.id WHERE j.strategy_id=? AND j.version=? ORDER BY r.at DESC LIMIT 1",
                             (row["id"], row["version"])).fetchone()
            if job:
                result = json.loads(job["body"])
                lines.append(f"  最近验证：{job['kind']} / {result.get('status')} / {job['at']}")
                if result.get("limitations") or result.get("reason"):
                    lines.append("  缺口：" + str(result.get("limitations") or result.get("reason")))
                for name, test in result.get("tests", {}).items():
                    lines.append(f"  {name}: passed={test.get('passed')}；净超额={test.get('net_excess')}；"
                                 f"非重叠样本={test.get('nonoverlapping_blocks')}；HAC t={test.get('hac_t')}")
        states = dict(db.execute("SELECT state,count(*) FROM fund_strategy_jobs GROUP BY state"))
        lines.append("后台任务：" + json.dumps(states, ensure_ascii=False))
        costs = db.execute("SELECT r.category,sum(coalesce(c.actual,r.reserved))/1000000.0 "
                           "FROM fund_research_reservations r LEFT JOIN fund_research_charges c ON c.id=r.id "
                           "GROUP BY r.category").fetchall()
        lines.append("资源累计占用（已知费用或未知预留）：" + json.dumps(dict(costs), ensure_ascii=False))
        lines.append("待补数据/待适配不等于研究无价值；研究结果和订单审批均不能放宽硬性风控。")
        return lines


def open_lab(root, config):
    from .journal import Journal
    from .research_budget import ResearchBudgetSettings
    from .strategy_definition import StrategyLabSettings
    from .strategy_lab import StrategyLab
    path = Path(root) / "state.sqlite3"
    if not path.exists():
        raise RiskError("An existing, explicitly selected account journal is required")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        binding = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0]
        row = db.execute("SELECT body FROM events WHERE kind='startup' ORDER BY sequence DESC LIMIT 1").fetchone()
    if not row or not json.loads(row[0]).get("comparison_protocol"):
        raise RiskError("No frozen startup research policy is available")
    startup = json.loads(row[0])
    policy = startup.get("strategy_lab_policy") or startup["comparison_protocol"]
    settings = StrategyLabSettings.from_config(config.get("strategy_lab"))
    if not settings.enabled:
        raise RiskError("Strategy Lab is disabled")
    return StrategyLab(Journal(root, binding), policy, settings,
                       ResearchBudgetSettings.from_config(config.get("research_budget")))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "import-data", "run-once"], default="status", nargs="?")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dataset", type=Path, help="Operator-owned JSON snapshot with provenance and coverage receipts")
    args = parser.parse_args(argv)
    if args.action == "status":
        print("\n".join(report(args.run_dir)))
        return
    config = json.loads(args.config.read_text(encoding="utf-8"))
    lab, now = open_lab(args.run_dir, config), datetime.now(timezone.utc)
    if args.action == "import-data":
        if args.dataset is None or args.dataset.stat().st_size > 32_000_000:
            parser.error("import-data requires a JSON snapshot of at most 32 MB")
        data = json.loads(args.dataset.read_text(encoding="utf-8"))
        print(json.dumps({"queued": lab.schedule(data, now)}, ensure_ascii=False))
    else:
        result = lab.run_one(now)
        print(json.dumps({"status": (result or {}).get("status", "no_ready_job")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
