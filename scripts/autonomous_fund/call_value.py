"""What each AI call bought (V2 spec 4.3): was it used, did it change anything, what did it cost.

Every model call is journaled as an `agent` event with a `call_id` and a
`purpose` (decision, shadow, critique, premarket_research, research,
postmarket_review). Where the call's result is actually used, the strategy
writes an `agent_outcome` event for that call_id: a validated decision (and
whether it changed the target portfolio), saved research (and how many
verifiable ideas it registered), a saved review (and its predictions), a
scored shadow decision, a critique the manager saw. A call without an outcome
was not used: it failed, its answer was rejected, or a later step failed. A
correction retry is a call of its own, so a first attempt that needed one
shows as not used.

Calls journaled before call ids existed are not counted. Output length is
never used as a measure of quality.
"""

PURPOSE_LABELS = {"decision": "决策复核", "shadow": "影子对照", "critique": "反方审查",
                  "premarket_research": "盘前研究", "research": "研究（回放）", "postmarket_review": "盘后复盘"}
PURPOSES = tuple(PURPOSE_LABELS)


def summarize(agents, outcomes, *, quiet_skips=0, critiques_reused=0):
    """Per-purpose value of the calls in `agents` (agent event bodies) given `outcomes` (agent_outcome bodies)."""
    used = {}
    for outcome in outcomes:
        if outcome.get("call_id") and outcome.get("valid"):
            used[outcome["call_id"]] = outcome
    groups = {}
    for call in agents:
        identifier = call.get("call_id")
        if not identifier:
            continue  # Before call ids: its use cannot be attributed.
        purpose = call.get("purpose") or "unknown"
        group = groups.setdefault(purpose, {"calls": 0, "paid": 0, "cache_hits": 0, "failed": 0, "used": 0,
                                            "changed": 0, "ideas": 0, "predictions": 0, "cost_usd": 0.0,
                                            "unpriced": 0, "unused_cost_usd": 0.0})
        group["calls"] += 1
        cost = call.get("cost_usd")
        if call.get("cache_hit"):
            group["cache_hits"] += 1
        elif cost is not None:
            group["paid"] += 1
            group["cost_usd"] += float(cost)
        elif not call.get("mock") and (call.get("usage") or call.get("parse_error")):
            group["unpriced"] += 1  # Usage without a price, or a failure with unknown usage: never 0.
        if call.get("parse_error"):
            group["failed"] += 1
        outcome = used.get(identifier)
        if outcome is None:
            group["unused_cost_usd"] += float(cost or 0)
            continue
        group["used"] += 1
        group["changed"] += bool(outcome.get("changed_targets"))
        group["ideas"] += int(outcome.get("new_ideas") or 0)
        group["predictions"] += int(outcome.get("predictions") or 0)
    for group in groups.values():
        group["used_share"] = group["used"] / group["calls"] if group["calls"] else None
        group["changed_share"] = group["changed"] / group["used"] if group["used"] else None
        group["cost_per_used_call"] = group["cost_usd"] / group["used"] if group["used"] and not group["unpriced"] else None
        group["cost_per_change"] = (group["cost_usd"] / group["changed"]
                                    if group["changed"] and not group["unpriced"] else None)
        group["cost_usd"] = round(group["cost_usd"], 6)
        group["unused_cost_usd"] = round(group["unused_cost_usd"], 6)
    return {"by_purpose": groups, "calls_saved": {"quiet_reviews_skipped": quiet_skips,
                                                  "critiques_reused": critiques_reused},
            "interpretation": "used: the call's result was validated and used; changed: a used decision changed the "
                              "target portfolio (a symbol added or removed, or a weight moved by at least one point). "
                              "Calls journaled before call ids are not counted; unpriced calls make cost ratios unknown."}


def from_events(events):
    """summarize() over (at, kind, body) rows of the kinds VALUE_KINDS."""
    return summarize([b for _, k, b in events if k == "agent"], [b for _, k, b in events if k == "agent_outcome"],
                     quiet_skips=sum(1 for _, k, _ in events if k == "review_skipped"),
                     critiques_reused=sum(1 for _, k, _ in events if k == "critic_reused"))


VALUE_KINDS = ("agent", "agent_outcome", "review_skipped", "critic_reused")


def lines(result):
    """Chinese lines for reports; empty when no call carries an id yet."""
    groups = result["by_purpose"]
    if not groups:
        return []
    out = []
    for purpose in [p for p in PURPOSES if p in groups] + sorted(set(groups) - set(PURPOSES)):
        g = groups[purpose]
        text = f"- {PURPOSE_LABELS.get(purpose, purpose)}：{g['calls']} 次，被采用 {g['used']} 次"
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
            text += f"；费用未知（{g['unpriced']} 次未定价，不按 0 计）"
        else:
            text += f"；费用 {g['cost_usd']:.4f} 美元"
            if g["cost_per_used_call"] is not None:
                text += f"，每次被采用的调用 {g['cost_per_used_call']:.4f} 美元"
            if g["cost_per_change"] is not None:
                text += f"，每次改变组合 {g['cost_per_change']:.4f} 美元"
            if g["unused_cost_usd"]:
                text += f"，未被采用的调用花了 {g['unused_cost_usd']:.4f} 美元"
        out.append(text)
    saved = result["calls_saved"]
    if saved["quiet_reviews_skipped"] or saved["critiques_reused"]:
        out.append(f"- 省下的调用：没有新情况而跳过的复核 {saved['quiet_reviews_skipped']} 次，"
                   f"复用的反方审查 {saved['critiques_reused']} 次")
    return out
