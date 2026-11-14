"""What each AI call bought (V2 spec 4.3): was it used, did it change anything, what did it cost.

Every model call is journaled as an `agent` event with a `call_id` and a
`purpose` (decision, shadow, critique, premarket_research, research,
postmarket_review). Where the call's result is actually used, the strategy
writes an `agent_outcome` event for that call_id: a validated decision (and
whether it changed the target portfolio), saved research (and how many
verifiable ideas it registered), a saved review (and its predictions), a
scored shadow decision, a critique the manager saw, an event screen applied to
scheduling or a saved model-identity observation. A missing outcome alone does
not prove non-use: older instrumentation or a failed outcome write can leave
adoption unknown. Explicit failure/refusal outcomes remain non-adoptions. A
correction retry is a call of its own, so a first attempt that needed one
shows as not used.

Calls journaled before call ids existed appear in overall coverage and costs,
but cannot enter per-purpose adoption groups. Output length is never used as a
measure of quality. Known costs are partitioned by recorded adoption, failure,
explicit non-adoption and unknown adoption; those buckets do not establish ROI.
"""
from .costs import scripted

PURPOSE_LABELS = {"decision": "决策复核", "shadow": "影子对照", "critique": "反方审查",
                  "critique_correction": "反方审查格式纠正",
                  "premarket_research": "盘前研究", "research": "研究（回放）", "postmarket_review": "盘后复盘",
                  "triage": "事件判断", "risk_approval": "独立风控审批", "decision_correction": "决策格式纠正", "stability": "稳定性测试", "model_probe": "模型身份核对"}
PURPOSES = tuple(PURPOSE_LABELS)
OUTCOME_LABELS = {"used": "已采用", "failed": "失败", "not_used": "明确未采用", "unknown": "采用未知"}


def _known_cost(call):
    # A native answer-cache hit or scripted result is not a provider charge,
    # even when a reused record carries a previous cost. Explicit zero is known.
    if call.get("cache_hit") or scripted(call):
        return 0.0
    from .scoreboard import _cost
    return _cost(call.get("cost_usd"))


def summarize(agents, outcomes, *, quiet_skips=0, critiques_reused=0):
    """Per-purpose value of the calls in `agents` (agent event bodies) given `outcomes` (agent_outcome bodies)."""
    agents = list(agents)
    coverage = {"total_calls": len(agents), "calls_with_id": 0, "legacy_calls": 0,
                "known_cost_usd": 0.0, "unpriced_calls": 0, "legacy_known_cost_usd": 0.0,
                "legacy_unpriced_calls": 0, "tracked_known_cost_usd": 0.0, "tracked_unpriced_calls": 0}
    for call in agents:
        legacy = not call.get("call_id")
        coverage["legacy_calls" if legacy else "calls_with_id"] += 1
        cost = _known_cost(call)
        if cost is None:
            coverage["unpriced_calls"] += 1
            coverage["legacy_unpriced_calls" if legacy else "tracked_unpriced_calls"] += 1
        else:
            coverage["known_cost_usd"] += cost
            coverage["legacy_known_cost_usd" if legacy else "tracked_known_cost_usd"] += cost
    for key in ("known_cost_usd", "legacy_known_cost_usd", "tracked_known_cost_usd"):
        coverage[key] = round(coverage[key], 6)
    recorded = {}
    for outcome in outcomes:
        if outcome.get("call_id"):
            recorded[outcome["call_id"]] = outcome
    used = {key: outcome for key, outcome in recorded.items() if outcome.get("valid")}
    groups = {}
    for call in agents:
        identifier = call.get("call_id")
        if not identifier:
            continue  # Before call ids: its use cannot be attributed.
        purpose = call.get("purpose") or "unknown"
        group = groups.setdefault(purpose, {"calls": 0, "paid": 0, "cache_hits": 0, "failed": 0, "used": 0,
                                            "changed": 0, "ideas": 0, "predictions": 0, "cost_usd": 0.0,
                                            "unpriced": 0, "unused_cost_usd": 0.0, "outcome_unknown": 0,
                                            "known_cost_by_outcome": dict.fromkeys(OUTCOME_LABELS, 0.0),
                                            "unpriced_by_outcome": dict.fromkeys(OUTCOME_LABELS, 0)})
        group["calls"] += 1
        cost = _known_cost(call)
        if call.get("cache_hit"):
            group["cache_hits"] += 1
        elif cost is not None and not scripted(call):
            group["paid"] += 1
            group["cost_usd"] += float(cost)
        elif cost is None:
            group["unpriced"] += 1  # Includes a successful response with no usage at all.
        failed = bool(call.get("parse_error") or call.get("answer_diagnostic"))
        if failed:
            group["failed"] += 1
        outcome = used.get(identifier)
        status = ("used" if outcome is not None else "failed" if failed else
                  "not_used" if identifier in recorded else "unknown")
        if cost is None:
            group["unpriced_by_outcome"][status] += 1
        else:
            group["known_cost_by_outcome"][status] += cost
        if outcome is None:
            if identifier not in recorded and not failed:
                group["outcome_unknown"] += 1
            # Retain the legacy field name; it means costs without a recorded
            # adoption, which cannot by itself establish that a call was wasted.
            if not call.get("cache_hit") and not scripted(call):
                group["unused_cost_usd"] += float(cost or 0)
            continue
        group["used"] += 1
        group["changed"] += bool(outcome.get("changed_targets"))
        group["ideas"] += int(outcome.get("new_ideas") or 0)
        group["predictions"] += int(outcome.get("predictions") or 0)
    for group in groups.values():
        group["used_share"] = group["used"] / group["calls"] if group["calls"] else None
        group["changed_share"] = group["changed"] / group["used"] if group["used"] else None
        complete = not group["unpriced"] and not group["outcome_unknown"]
        group["cost_per_used_call"] = group["cost_usd"] / group["used"] if group["used"] and complete else None
        group["cost_per_change"] = (group["cost_usd"] / group["changed"]
                                    if group["changed"] and complete else None)
        group["cost_usd"] = round(group["cost_usd"], 6)
        group["unused_cost_usd"] = round(group["unused_cost_usd"], 6)
        group["known_cost_by_outcome"] = {k: round(v, 6) for k, v in group["known_cost_by_outcome"].items()}
    return {"by_purpose": groups, "coverage": coverage, "calls_saved": {"quiet_reviews_skipped": quiet_skips,
                                                  "critiques_reused": critiques_reused},
            "interpretation": "used: the call's result was validated and used; changed: a used decision changed the "
                              "target portfolio (a symbol added or removed, or a weight moved by at least one point). "
                              "Per-purpose adoption excludes legacy calls without IDs; coverage retains their known costs. "
                              "Missing outcomes do not prove non-use. "
                              "Unpriced calls or unknown adoption make cost ratios unknown."}


def from_events(events):
    """summarize() over (at, kind, body) rows of the kinds VALUE_KINDS."""
    return summarize([b for _, k, b in events if k == "agent"], [b for _, k, b in events if k == "agent_outcome"],
                     quiet_skips=sum(1 for _, k, _ in events if k == "review_skipped"),
                     critiques_reused=sum(1 for _, k, _ in events if k == "critic_reused"))


VALUE_KINDS = ("agent", "agent_outcome", "review_skipped", "critic_reused")


def lines(result, *, include_coverage=False):
    """Chinese lines; reports can opt into overall coverage, including legacy-only histories."""
    groups = result["by_purpose"]
    if not groups and not include_coverage:
        return []
    out = []
    coverage = result.get("coverage")
    if include_coverage and coverage and coverage["total_calls"]:
        out.append(f"- 调用归因覆盖：账本调用记录 {coverage['total_calls']} 次，有编号 {coverage['calls_with_id']} 次，"
                   f"无编号旧记录 {coverage['legacy_calls']} 次；全部已知费用 {coverage['known_cost_usd']:.4f} 美元，"
                   f"其中有编号 {coverage['tracked_known_cost_usd']:.4f}、无编号旧记录 {coverage['legacy_known_cost_usd']:.4f} 美元；"
                   f"未定价 {coverage['unpriced_calls']} 次（有编号 {coverage['tracked_unpriced_calls']} 次、"
                   f"旧记录 {coverage['legacy_unpriced_calls']} 次）。"
                   "下面按用途只归因有编号的记录，不推断旧调用的采用情况。")
    for purpose in [p for p in PURPOSES if p in groups] + sorted(set(groups) - set(PURPOSES)):
        g = groups[purpose]
        text = f"- {PURPOSE_LABELS.get(purpose, purpose)}：{g['calls']} 次，被采用 {g['used']} 次"
        if g.get("outcome_unknown"):
            text = (f"- {PURPOSE_LABELS.get(purpose, purpose)}：{g['calls']} 次，已记录采用 {g['used']} 次，"
                    f"采用结果未记录 {g['outcome_unknown']} 次")
        if purpose == "decision":
            text += f"，其中改变目标组合 {g['changed']} 次"
            if g["changed_share"] is not None:
                text += f"（{g['changed_share'] * 100:.0f}%）"
        if g["ideas"]:
            text += f"，登记可验证想法 {g['ideas']} 条"
        if g["predictions"]:
            text += f"，可证伪预测 {g['predictions']} 条"
        if g["failed"]:
            text += f"，失败 {g['failed']} 次"
        if g["unpriced"]:
            text += f"；已知费用 {g['cost_usd']:.4f} 美元；费用未知（{g['unpriced']} 次未定价，不按 0 计）"
        else:
            text += f"；费用 {g['cost_usd']:.4f} 美元"
            if g["cost_per_used_call"] is not None:
                text += f"，每次被采用的调用 {g['cost_per_used_call']:.4f} 美元"
            if g["cost_per_change"] is not None:
                text += f"，每次改变组合 {g['cost_per_change']:.4f} 美元"
        if g["unused_cost_usd"]:
            label = "未记录采用的调用已知费用" if g.get("outcome_unknown") else "未被采用的调用花了"
            text += f"，{label} {g['unused_cost_usd']:.4f} 美元"
        out.append(text)
        if include_coverage and g.get("known_cost_by_outcome"):
            out.append("  已知费用去向：" + "；".join(
                f"{label} {g['known_cost_by_outcome'][key]:.4f}" for key, label in OUTCOME_LABELS.items()) + " 美元。")
            unknown = g.get("unpriced_by_outcome") or {}
            if any(unknown.values()):
                out.append("  未定价分布：" + "；".join(
                    f"{label} {unknown[key]} 次" for key, label in OUTCOME_LABELS.items() if unknown.get(key)) + "。")
    saved = result.get("calls_saved") or {}
    quiet, reused = saved.get("quiet_reviews_skipped", 0), saved.get("critiques_reused", 0)
    if quiet or reused:
        out.append(f"- 省下的调用：没有新情况而跳过的复核 {quiet} 次，复用的反方审查 {reused} 次")
    if include_coverage and out:
        out.append("- 采用或改变目标组合不代表收益贡献；采用结果缺失不证明浪费，跳过或复用次数不等于已证实的节省金额。")
    return out
