"""Operator diagnostics/import/run entry point. Never connects to a broker or a model."""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .risk import RiskError, instant, trading_day


def forward_readiness(db, now, policy):
    from .market_data import SessionClock
    from .strategy_validation import following_session

    now = instant(now)
    session = SessionClock(now=lambda: now).status()
    opened = instant(session["next_open"])
    if opened <= now:
        _, opened = following_session(trading_day(now))
    day = trading_day(opened)
    rows = db.execute("SELECT v.id,v.version,v.digest,v.policy_hash,s.at,s.body FROM fund_strategy_versions v "
                      "LEFT JOIN fund_strategy_signals s ON s.id=v.id AND s.version=v.version AND s.day=? "
                      "WHERE v.state IN ('shadow','paper_active') ORDER BY v.id,v.version", (day,)).fetchall()
    ready, waiting, previous_policy = [], [], []
    for key, version, digest, fingerprint, at, body in rows:
        identity = f"{key} / {version}"
        if fingerprint != policy.get("policy_hash"):
            previous_policy.append(identity)
            continue
        try:
            signal = json.loads(body) if body else {}
            bound = (at and instant(at) <= now and instant(at) < opened and instant(signal["open_at"]) == opened
                     and signal.get("proposal_digest") == digest and signal.get("policy_hash") == fingerprint)
        except (KeyError, TypeError, ValueError):
            bound = False
        (ready if bound else waiting).append(identity)
    attempted = db.execute("SELECT count(*) FROM fund_research_reservations "
                           "WHERE day=? AND category='validation_compute'", (trading_day(now),)).fetchone()[0]
    queued = db.execute("SELECT count(*) FROM fund_strategy_jobs WHERE kind='forward' AND state='queued'").fetchone()[0]
    return {"next_open": opened.isoformat(), "ready": ready, "waiting": waiting, "previous_policy": previous_policy,
            "queued_forward_jobs": queued,
            "daily_slots_left": max(0, policy.get("max_jobs_per_day", 0) - attempted)}


def check_data(root, data, key, version, now):
    from .strategy_validation import check_dataset, snapshot_limitations

    fingerprint = check_dataset(data)
    path = Path(root) / "state.sqlite3"
    if not path.is_file():
        raise RiskError("An existing, explicitly selected account journal is required")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT proposal,policy,state FROM fund_strategy_versions WHERE id=? AND version=?",
                         (key, version)).fetchone()
    if row is None:
        raise RiskError("Unknown strategy version in the selected account")
    if any(instant(item["available_at"]) > instant(now) for collection in ("bars", "features")
           for series in data.get(collection, {}).values() for item in series):
        raise RiskError("A research snapshot contains observations from the future")
    proposal, policy = json.loads(row[0]), json.loads(row[1])
    required = set(proposal["instruments"]) | {proposal["validation"]["benchmark"]}
    if policy.get("cash_symbol"):
        required.add(policy["cash_symbol"])
    features = set(proposal.get("data_symbols", ()))
    gaps = snapshot_limitations(data, require_receipts=True)
    missing = sorted((required - data["bars"].keys()) |
                     (features - data["bars"].keys() - data.get("features", {}).keys()))
    return {"strategy_id": key, "version": version, "state": row[2], "snapshot_digest": fingerprint,
            "receipt_gaps": gaps, "symbols_missing_from_this_snapshot": missing,
            "benchmark_sessions_in_snapshot": len(data["bars"].get(proposal["validation"]["benchmark"], ())),
            "ready_for_validation_input": not gaps and not missing}


def report(root):
    from .execution_quality import reference_lines
    from .strategy_competition import compare_saved

    path = Path(root) / "state.sqlite3"
    if not path.exists():
        return ["Strategy Lab：尚无账户账本。"]
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fund_strategy_versions'").fetchone():
            return ["Strategy Lab：尚未初始化；原有账户与交易状态未改变。"]
        policy = db.execute("SELECT value FROM meta WHERE key='strategy_lab_policy'").fetchone()
        policy = json.loads(policy[0]) if policy else {}
        now = datetime.now(timezone.utc)
        comparison = compare_saved(db, now)
        matched = {(item["strategy_id"], item["version"]): item["risk_matched"] for item in comparison["strategies"]}
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
                if "prospective_sessions" in result:
                    lines.append(f"  开盘前留证 {result['prospective_sessions']} 个交易日，事后回放 {result['replayed_sessions']} 个交易日")
                diagnostic = matched.get((row["id"], row["version"]))
                if diagnostic and diagnostic["status"] in {"estimated", "limited_sample"}:
                    suffix = "（样本较少）" if diagnostic["status"] == "limited_sample" else ""
                    lines.append(f"  固定 Beta {diagnostic['beta_to_passive_reference']:.2f}，"
                                 f"后续 {diagnostic['evaluation_sessions']} 日的净收益差 "
                                 f"{diagnostic['net_difference_at_frozen_beta']:+.2%}{suffix}")
                elif diagnostic and diagnostic["status"] == "insufficient_prospective_sessions":
                    lines.append(f"  连续前瞻记录 {diagnostic['prospective_prefix_sessions']} 日，"
                                 "同风险比较待积累样本。")
                elif diagnostic:
                    lines.append(f"  同风险比较暂不可用（{diagnostic['status']}）")
                for name, test in result.get("tests", {}).items():
                    lines.append(f"  {name}: passed={test.get('passed')}；净超额={test.get('net_excess')}；"
                                 f"非重叠样本={test.get('nonoverlapping_blocks')}；HAC t={test.get('hac_t')}")
        states = dict(db.execute("SELECT state,count(*) FROM fund_strategy_jobs GROUP BY state"))
        for study in comparison["portfolio_comparison"]["studies"]:
            title = f"组合对照 {study['plan']['study_id']}（{study['status']}）"
            if "incremental_net_return" in study:
                title += (f"，{study['evaluation_sessions']} 日净收益差 {study['incremental_net_return']:+.2%}，"
                          f"最大回撤变化 {study['change_in_max_drawdown']:+.2%}")
            lines.append(title)
            if "execution_cost_reference" in study:
                lines.extend(reference_lines(study["execution_cost_reference"]))
            else:
                lines.append("  这项声明尚未记录成交成本参考。")
            if "incremental_net_return" in study:
                for arm, label in (("base", "基准组合"), ("combined", "加入候选")):
                    row = study[arm]
                    lines.append(f"  {label} 外层现金权重 {row['mean_cash_weight']:.2%}，"
                                 f"资金转移换手 {row['transfer_turnover_fraction']:.2%}，"
                                 f"额外费用和滑点 {row['transfer_fees_fraction'] + row['transfer_slippage_fraction']:.3%}")
                stress = study["transfer_cost_stress"]
                bracket = stress["breakeven_bracket"]
                if bracket:
                    lines.append(f"  加入候选后的组合每笔转移额外加收 {bracket['positive_at_extra_bps']} 基点时仍有优势，"
                                 f"加收 {bracket['nonpositive_at_extra_bps']} 基点时优势消失。")
                elif stress["status"] == "above_tested_range":
                    lines.append(f"  额外转移成本加至 {stress['scenarios'][-1]['extra_transfer_bps']} 基点仍有优势。")
                elif stress["status"] == "already_nonpositive":
                    lines.append("  已声明成本下，加入候选的净收益差接近零或为负。")
                paired = study["paired_advantage"]
                if paired["interval_95"]:
                    low, high = paired["interval_95"]
                    lines.append(f"  {paired['non_overlapping']} 个不重叠五日区间，平均净收益差的"
                                 f"近似 95% 区间为 {low:+.2%} 至 {high:+.2%}。")
                else:
                    lines.append(f"  已有 {paired['non_overlapping']} 个不重叠五日区间，平均净收益差区间暂不可估计。")
                risk = study["benchmark_risk"]
                if risk["status"] == "observed":
                    corr = risk["candidate_base_correlation"]
                    correlation = f"，候选与原组合收益相关 {corr:.2f}" if corr is not None else ""
                    lines.append(f"  {risk['benchmark']} Beta {risk['base_beta']:.2f} → {risk['combined_beta']:.2f}"
                                 f"{correlation}，匹配 {risk['matched_sessions']}/{risk['evaluation_sessions']} 日。")
                else:
                    lines.append(f"  共同基准 Beta 暂不可用（{risk['status']}）。")
                exposure = study["position_exposure"]
                coverage = f"{exposure['covered_sessions']}/{exposure['evaluation_sessions']} 日"
                if exposure["status"] == "unavailable":
                    lines.append(f"  证券持仓覆盖 {coverage}，暂无可核对的收盘持仓。")
                else:
                    latest = exposure["latest"]
                    base, combined = latest["base"], latest["combined"]
                    lines.append(f"  证券持仓覆盖 {coverage}。{latest['day']} 收盘最大单只风险证券权重"
                                 f" {base['max_single_security_weight']:.2%} → {combined['max_single_security_weight']:.2%}，"
                                 f"跨策略共同持有的风险证券权重"
                                 f" {base['shared_security_weight']:.2%} → {combined['shared_security_weight']:.2%}，"
                                 f"候选分配至原有风险证券 {combined['candidate_overlap_weight']:.2%}。")
                    peak = exposure["peak_single_security_weight"]
                    lines.append(f"  已覆盖收盘的最高单只权重 {peak['base']:.2%} → {peak['combined']:.2%}。"
                                 f"加入候选后的收盘外层现金 {combined['outside_usd_cash_weight']:.2%}，"
                                 f"策略内现金 {combined['sleeve_usd_cash_weight']:.2%}，"
                                 f"现金等价证券 {combined['cash_equivalent_weight']:.2%}，"
                                 f"应收款 {combined['receivables_weight']:.2%}。")
                    largest = latest["largest_risk_securities"][:3]
                    if largest:
                        holdings = "，".join(f"{row['symbol']} {row['base_weight']:.2%} → {row['combined_weight']:.2%}"
                                             for row in largest)
                        lines.append(f"  主要风险证券收盘权重 {holdings}。")
                    if groups := latest.get("risk_groups"):
                        labels = "，".join(f"{name} {values['base_weight']:.2%} → {values['combined_weight']:.2%}"
                                          for name, values in groups.items())
                        lines.append(f"  冻结风险分组（可能重叠）{labels}。")
                if exposure["missing_by_reason"]:
                    labels = {"missing_or_inconsistent_policy": "版本策略配置缺失或无法核验",
                              "missing_or_invalid_closing_book": "收盘账本缺失或无法核对",
                              "inconsistent_shared_marks_or_classification": "相同证券的估值或分类冲突"}
                    gaps = "，".join(f"{labels[reason]} {count} 日" for reason, count in exposure["missing_by_reason"].items())
                    lines.append(f"  持仓覆盖缺口包括{gaps}。")
        lines.append("后台任务：" + json.dumps(states, ensure_ascii=False))
        forward = forward_readiness(db, now, policy)
        lines.append(f"前瞻截止 {forward['next_open']}，已留证 {len(forward['ready'])}，待留证 {len(forward['waiting'])}，"
                     f"前瞻排队 {forward['queued_forward_jobs']}，今日剩余任务名额 {forward['daily_slots_left']}。")
        if forward["waiting"]:
            lines.append("  待留证版本 " + "、".join(forward["waiting"]))
        if forward["previous_policy"]:
            lines.append("  旧配置版本，须重验 " + "、".join(forward["previous_policy"]))
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
        saved = db.execute("SELECT value FROM meta WHERE key='strategy_lab_policy'").fetchone()
    if not row or not saved:
        raise RiskError("No frozen startup research policy is available")
    startup = json.loads(row[0])
    policy, current = startup.get("strategy_lab_policy"), json.loads(saved[0])
    if not policy or not isinstance(current, dict) or not current.get("enabled"):
        raise RiskError("Strategy Lab is disabled")
    settings = StrategyLabSettings.from_config({k: v for k, v in current.items() if k != "policy_hash"})
    return StrategyLab(Journal(root, binding), policy, settings,
                       ResearchBudgetSettings.from_config(startup.get("research_budget", config.get("research_budget"))),
                       publish_policy=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "check-data", "import-data", "run-once", "declare-portfolio"],
                        default="status", nargs="?")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dataset", type=Path, help="Operator-owned JSON snapshot with provenance and coverage receipts")
    parser.add_argument("--strategy-id", help="Existing candidate for read-only check-data")
    parser.add_argument("--version", help="Exact version for read-only check-data")
    parser.add_argument("--portfolio", type=Path, help="Explicit base and candidate strategy versions, allocations and costs")
    args = parser.parse_args(argv)
    if args.action == "status":
        print("\n".join(report(args.run_dir)))
        return
    if args.action == "check-data":
        from .strategy_lab import load_snapshot
        if args.dataset is None or not args.strategy_id or not args.version:
            parser.error("check-data requires --dataset, --strategy-id and --version")
        result = check_data(args.run_dir, load_snapshot(args.dataset), args.strategy_id, args.version,
                            datetime.now(timezone.utc))
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.action == "declare-portfolio":
        from .strategy_lab import load_snapshot
        from .strategy_portfolio import declare_study

        if args.portfolio is None:
            parser.error("declare-portfolio requires --portfolio")
        spec = load_snapshot(args.portfolio)
        path = args.run_dir / "state.sqlite3"
        if not path.is_file():
            raise RiskError("An existing, explicitly selected account journal is required")
        with sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True) as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fund_strategy_versions'").fetchone():
                raise RiskError("Strategy Lab is not initialized in the selected journal")
            registered = declare_study(db, spec, datetime.now(timezone.utc))
        print(json.dumps(registered, ensure_ascii=False))
        return
    config = json.loads(args.config.read_text(encoding="utf-8"))
    lab, now = open_lab(args.run_dir, config), datetime.now(timezone.utc)
    if args.action == "import-data":
        from .strategy_lab import load_snapshot
        if args.dataset is None:
            parser.error("import-data requires --dataset")
        data = load_snapshot(args.dataset)
        print(json.dumps({"queued": lab.schedule(data, now)}, ensure_ascii=False))
    else:
        result = lab.run_one(now)
        print(json.dumps({"status": (result or {}).get("status", "no_ready_job")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
