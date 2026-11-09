"""Experiment registry (V2 spec 4.1): every experiment the fund runs, in one table.

The experiments keep running in their own modules: the factor lab, the learning
comparison (memo-informed manager vs a baseline without the memo), the
portfolio-construction comparison and the prediction scorecards. This module
only records, for each experiment version, what it tests, what it depends on and
where it stands, plus an append-only history of every state change with its
reason and evidence.

- A version is never overwritten. A changed configuration (for example a new
  prompt version for the AI arms) is a new version; the old one retires with
  the reason "superseded".
- States: candidate -> historical_test -> rejected | shadow -> paper_active ->
  paused -> retired. `paper_active` means the version affects the paper
  account (it trades, or the AI sees it as validated evidence); `shadow` means
  it is observed and scored but does not trade.
- The registry is derived from the experiments' own records on each sync, so it
  never disagrees with them; it adds the history they do not keep.
"""
import ast
import hashlib
import json

STATES = ("candidate", "historical_test", "rejected", "shadow", "paper_active", "paused", "retired")
FINAL = {"rejected", "retired"}
STATE_LABELS = {"candidate": "候选", "historical_test": "历史检验", "rejected": "未通过", "shadow": "影子观察",
                "paper_active": "模拟盘生效", "paused": "暂停", "retired": "退役"}
KIND_LABELS = {"factor": "因子", "learning_ab": "学习对照", "construction_ab": "组合构建对照",
               "prediction_stats": "预测统计"}
# Factor lab status -> registry state. insufficient_data is re-tested later.
FACTOR_STATES = {"validated": "paper_active", "rejected": "rejected", "invalid": "rejected", "redundant": "rejected",
                 "insufficient_data": "historical_test", "retired": "retired"}


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()[:12]


class Registry:
    def __init__(self, journal):
        self.journal = journal
        with journal.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fund_experiments (
                    id TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL,
                    hypothesis TEXT NOT NULL, dependencies TEXT NOT NULL, config TEXT NOT NULL,
                    config_hash TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (id, version));
                CREATE TABLE IF NOT EXISTS fund_experiment_history (
                    id TEXT NOT NULL, version TEXT NOT NULL, at TEXT NOT NULL, from_state TEXT,
                    to_state TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS fund_experiment_history_at ON fund_experiment_history(at);
            """)

    def observe(self, now, entries):
        """Record the current state of each entry; returns the transitions written.

        entry: {"id", "kind", "hypothesis", "dependencies", "config", "state", "reason", "evidence"},
        optionally "version" (default: the config hash). A new version of an id retires the
        id's other unfinished versions.
        """
        at, written = str(now), []
        with self.journal.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for entry in entries:
                if entry["state"] not in STATES:
                    raise ValueError(f"unknown experiment state {entry['state']}")
                digest = config_hash(entry["config"])
                version = str(entry.get("version") or digest)
                evidence = json.dumps(entry.get("evidence"), sort_keys=True, default=str)
                row = db.execute("SELECT state FROM fund_experiments WHERE id=? AND version=?",
                                 (entry["id"], version)).fetchone()
                if row is None:
                    for (other, state) in db.execute(
                            "SELECT version,state FROM fund_experiments WHERE id=? AND version!=?",
                            (entry["id"], version)).fetchall():
                        if state not in FINAL:
                            reason = f"superseded by version {version}"
                            db.execute("UPDATE fund_experiments SET state='retired',updated_at=? WHERE id=? AND version=?",
                                       (at, entry["id"], other))
                            db.execute("INSERT INTO fund_experiment_history VALUES(?,?,?,?,?,?,?)",
                                       (entry["id"], other, at, state, "retired", reason, "null"))
                            written.append((entry["id"], other, state, "retired", reason))
                    db.execute("INSERT INTO fund_experiments VALUES(?,?,?,?,?,?,?,?,?,?)", (
                        entry["id"], version, entry["kind"], at, entry["hypothesis"],
                        json.dumps(entry.get("dependencies") or [], ensure_ascii=False),
                        json.dumps(entry["config"], sort_keys=True, default=str, ensure_ascii=False), digest,
                        entry["state"], at))
                    previous = None
                elif row[0] != entry["state"]:
                    previous = row[0]
                    db.execute("UPDATE fund_experiments SET state=?,updated_at=? WHERE id=? AND version=?",
                               (entry["state"], at, entry["id"], version))
                else:
                    continue
                reason = entry.get("reason") or ("registered" if previous is None else "state changed")
                db.execute("INSERT INTO fund_experiment_history VALUES(?,?,?,?,?,?,?)",
                           (entry["id"], version, at, previous, entry["state"], reason, evidence))
                written.append((entry["id"], version, previous, entry["state"], reason))
        return written

    def state(self, experiment_id):
        """The state of the id's newest version, or None."""
        with self.journal.connection() as db:
            row = db.execute("SELECT state FROM fund_experiments WHERE id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                             (experiment_id,)).fetchone()
        return row[0] if row else None

    def rows(self):
        with self.journal.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM fund_experiments ORDER BY kind, id, created_at")]

    def history(self, since=None):
        with self.journal.connection() as db:
            rows = db.execute("SELECT * FROM fund_experiment_history ORDER BY rowid").fetchall()
        rows = [dict(r) for r in rows]
        if since is not None:
            from .risk import instant
            rows = [r for r in rows if instant(r["at"]) >= instant(since)]
        return rows


def observations(*, factors=(), learning=None, construction=None, scorecards=None, previous=None):
    """Registry entries derived from the experiments' own records.

    factors: fund_factors rows. learning: {"enabled", "champion", "version_config", "evaluation"}.
    construction: {"mode", "trading", "config", "evaluation"}. scorecards: the strategy's scorecards.
    previous(id) -> the registry's current state for that id (prediction statistics pause only
    after they were active).
    """
    previous = previous or (lambda experiment_id: None)
    entries = []
    for row in factors:
        from .factors import FUNDAMENTAL_FIELDS, parse
        try:
            names = {node.id for node in ast.walk(parse(row["expression"])) if isinstance(node, ast.Name)}
        except Exception:  # An invalid expression is registered too, without its data.
            names = set()
        uses = sorted(names & set(FUNDAMENTAL_FIELDS))
        evidence = json.loads(row["evidence"]) if row.get("evidence") else {}
        walk = evidence.get("walk_forward") or {}
        entries.append({
            "id": f"factor:{row['id']}", "version": row["id"], "kind": "factor",
            "hypothesis": row["hypothesis"],
            "dependencies": ["completed daily bars", f"cross-section {row.get('universe') or 'stocks'}"]
                            + (["SEC point-in-time fundamentals"] if uses else []),
            "config": {"name": row["name"], "expression": row["expression"], "horizon": row["horizon"],
                       "universe": row.get("universe")},
            "state": FACTOR_STATES.get(row["status"], "historical_test"),
            "reason": row.get("reason") or row["status"],
            "evidence": {**{key: evidence[key] for key in ("status", "direction", "in_sample", "holdout", "critical_t",
                                                         "net_spread") if key in evidence},
                         **({"walk_forward_agreeing": walk.get("agreeing"), "walk_forward_required": walk.get("required")}
                            if walk else {})}})
    if learning and learning.get("enabled"):
        evaluation = learning.get("evaluation") or {}
        stats = evaluation.get("memo_minus_baseline_1d_excess")
        for arm, hypothesis in (("memo", "复盘备忘、跟踪评价和记分卡能改善下一交易日的决策"),
                                ("baseline", "对照组：同样的行情和研究，但不看复盘备忘和记分卡")):
            active = learning["champion"] == arm
            entries.append({
                "id": f"learning:{arm}", "kind": "learning_ab", "hypothesis": hypothesis,
                "dependencies": ["shadow manager decisions", "completed daily closes"],
                "config": {"arm": arm, **learning["version_config"]},
                "state": "paper_active" if active else "shadow",
                "reason": evaluation.get("reason") or ("trading arm" if active else "scored in the shadow only"),
                "evidence": stats})
    if construction:
        test = (construction.get("evaluation") or {}).get("test")
        for arm, hypothesis in (("ai", "按 AI 给出的权重下单"),
                                ("risk_budget", "按与 AI 权重成比例的风险预算下单（风险平价式缩放）")):
            active = construction["trading"] == arm
            entries.append({
                "id": f"construction:{arm}", "kind": "construction_ab", "hypothesis": hypothesis,
                "dependencies": ["accepted AI targets", "risk estimate", "completed daily closes"],
                "config": {"arm": arm, **construction["config"]},
                "state": "paper_active" if active else "shadow",
                "reason": (construction.get("evaluation") or {}).get("reason")
                          or ("trading construction" if active else "scored side by side only"),
                "evidence": test})
    cards = scorecards or {}
    ideas = cards.get("ideas")
    if ideas is not None:
        verdict = ideas.get("verdict")
        if verdict == "informative":
            state = "paper_active"
        elif verdict == "not_informative" and previous("predictions:research_ideas") == "paper_active":
            state = "paused"
        else:
            state = "shadow"
        entries.append({
            "id": "predictions:research_ideas", "kind": "prediction_stats",
            "hypothesis": "研究员的投资想法有校准过的预测能力（把握越高越准，且跑赢抛硬币和“总是看涨”）",
            "dependencies": ["research ideas registered before the open", "completed daily closes"],
            "config": {"minimum_sample": ideas.get("minimum_sample"), "significance": 0.05},
            "state": state, "reason": verdict or "no ideas scored",
            "evidence": {key: ideas.get(key) for key in ("scored", "hit_rate", "brier", "verdict")}})
    predictions = cards.get("predictions")
    if predictions is not None:
        entries.append({
            "id": "predictions:review", "kind": "prediction_stats",
            "hypothesis": "盘后复盘给出的可证伪预测命中率高于随机",
            "dependencies": ["post-market review predictions", "completed daily closes"],
            "config": {"scoring": "completed closes"}, "state": "shadow", "reason": "evidence for the AI only",
            "evidence": {key: predictions.get(key) for key in ("total", "scored", "hit_rate")}})
    return entries


def lines(rows, history, *, limit=20):
    """Chinese summary of the registry: counts, the versions in effect or in the shadow, recent changes."""
    current = [row for row in rows if row["state"] not in FINAL]
    out = ["实验登记："]
    if not rows:
        return out + ["- 还没有登记的实验（第一次盘后复盘后开始登记）"]
    counts = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    out.append("- " + "、".join(f"{STATE_LABELS[state]} {counts[state]}" for state in STATES if state in counts))
    for row in sorted(current, key=lambda r: (STATES.index(r["state"]), r["kind"], r["id"]))[:limit]:
        config = json.loads(row["config"])
        name = config.get("name") or config.get("arm") or row["id"]
        out.append(f"- [{STATE_LABELS[row['state']]}] {KIND_LABELS.get(row['kind'], row['kind'])} {name}"
                   f"（版本 {row['version'][:12]}）：{row['hypothesis']}")
    if history:
        out.append("最近的状态变化：")
        for item in history[-limit:]:
            before = STATE_LABELS.get(item["from_state"], "新登记") if item["from_state"] else "新登记"
            out.append(f"- {item['at'][:16]} {item['id']}：{before} → {STATE_LABELS[item['to_state']]}（{item['reason']}）")
    return out


def report(root, *, limit=20):
    """The registry as Chinese lines, read-only (`fund-logs.sh experiments`); never creates a table."""
    import sqlite3
    from contextlib import closing
    from pathlib import Path
    book = Path(root) / "state.sqlite3"
    if not book.is_file():
        return ["实验登记：账户目录里还没有账本"]
    with closing(sqlite3.connect(book.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_experiments'").fetchone():
            return lines([], [])
        rows = [dict(r) for r in db.execute("SELECT * FROM fund_experiments ORDER BY kind, id, created_at")]
        history = [dict(r) for r in db.execute("SELECT * FROM fund_experiment_history ORDER BY rowid")]
    return lines(rows, history, limit=limit)
