"""Operator settings from the environment (.env): keys, user limits and external parameters.

`config.json` holds the complete reviewed policy and its defaults. The operator
normally changes a small set of knobs; those may also come from the process
environment or the explicitly selected `.env` file. Precedence: process
environment > `.env` > `config.json`.

Every override is parsed strictly, validated by the same policy constructors as
config.json, and reported (without secret values) in the startup audit event.
An unknown FUND_* name in the selected .env file is an error with a suggestion:
a mistyped limit must never be silently ignored.
"""
import copy
import difflib
import json
import sqlite3
from pathlib import Path

from .risk import LEVERAGED_KIND, LEVERAGED_OR_INVERSE, RiskError

SEPARATOR = "|"


def _decimal(text):
    from .risk import number
    number(text)  # Finite decimal; range is checked by the policy constructors.
    return str(text).strip()


def _int(text):
    try:
        return int(str(text).strip())
    except ValueError:
        raise RiskError("expected a whole number") from None


def _bool(text):
    value = str(text).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RiskError("expected true/false")


def _list(text):
    return [item.strip() for item in str(text).split(SEPARATOR) if item.strip()]


def _capital(text):
    value = str(text).strip()
    return None if value.lower() in {"account", "all", "none"} else _decimal(value)


def _optional(text):
    value = str(text).strip()
    return value or None


def _feeds(text):
    feeds = []
    for item in _list(text):
        name, _, url = item.partition("=")
        if not name.strip() or not url.strip():
            raise RiskError("use name=https://... entries separated by |")
        feeds.append({"name": name.strip(), "url": url.strip()})
    return feeds


def _json(text):
    try:
        return json.loads(text)
    except ValueError:
        raise RiskError("expected JSON") from None


# name: (config path or None for a runtime-only value, parser, secret, section, Chinese description)
SETTINGS = {
    # ----- Startup mode (runtime selection; never part of the risk policy)
    "FUND_MODE": (None, None, False, "启动", "启动模式：local（真实行情本地模拟盘）或 paper（Alpaca 模拟账户）；没有实盘选项"),
    "FUND_RUN_DIR": (None, None, False, "启动", "账户运行目录（账本、报告、STOP）；留空为 artifacts/fund-<模式>"),
    "FUND_OBSERVE_ONLY": (None, None, False, "启动", "仅 local：只读观察，不调用模型、不下单"),
    "FUND_AUTO_RESUME": (("service", "auto_resume"), _bool, False, "启动", "服务重启或进程卡死造成的 STOP 在确认健康后自动解除；人工 stop 永不自动解除"),
    # ----- Model keys and model choice
    "DEEPSEEK_API_KEY": (None, None, True, "模型", "DeepSeek API Key（默认研究员和基金经理都用 DeepSeek）"),
    "OPENAI_API_KEY": (None, None, True, "模型", "OpenAI API Key（仅当选用 openai/ 模型时需要）"),
    "ANTHROPIC_API_KEY": (None, None, True, "模型", "Anthropic API Key（仅当选用 anthropic/ 模型时需要）"),
    "AI_MODEL": (None, None, False, "模型", "同时覆盖两个角色的模型（通常不设置）"),
    "FUND_RESEARCH_MODEL": (("agent_models", "researcher"), str, False, "模型", "研究员模型，例如 deepseek/deepseek-flash"),
    "FUND_DECISION_MODEL": (("agent_models", "manager"), str, False, "模型", "基金经理模型，例如 deepseek/deepseek-v4-pro"),
    "FUND_REASONING_EFFORT": (("reasoning_effort",), _optional, False, "模型", "推理强度 low/medium/high，留空为模型默认"),
    "FUND_MANAGER_RUN_TIMEOUT_SECONDS": (("manager_run_timeout_seconds",), _int, False, "模型",
                                         "基金经理每次尝试的整轮超时（秒，默认 300），适用于盘中决策和盘后复盘"),
    "FUND_CRITIC": (("critic", "enabled"), _bool, False, "模型", "是否在研究员和基金经理之间加入反方审查（每次决策多一次模型调用）"),
    "FUND_CRITIC_MODEL": (("critic", "model"), _optional, False, "模型", "反方审查使用的模型，留空用研究员模型"),
    "FUND_MAX_AGENT_RUNS_PER_DAY": (("model_budget", "max_agent_runs_per_day"), _int, False, "模型", "每个交易日最多 Agent 调用次数（控制费用）"),
    "FUND_MIN_REVIEW_MINUTES": (("model_budget", "min_review_minutes"), _int, False, "模型", "两次 AI 复核的最短间隔（分钟）"),
    "FUND_SKIP_QUIET_REVIEWS": (("model_budget", "skip_quiet_reviews"), _bool, False, "模型", "到点复核时若无任何变化（无提醒、无成交、持仓和研究不变）则跳过本次模型调用"),
    "FUND_MAX_QUIET_MINUTES": (("model_budget", "max_quiet_minutes"), _int, False, "模型", "连续跳过复核的最长时间（分钟），到时一定调用基金经理"),
    "FUND_MODEL_PRICES_JSON": (("model_prices_per_million",), _json, False, "模型", "每百万 token 价格 JSON（input、output、cached_input 缓存命中价），用于费用估算和盈亏平衡"),
    "FUND_DATA_COSTS_USD_PER_MONTH": (("performance", "data_costs_usd_per_month"), _decimal, False, "模型",
                                      "付费数据订阅的月费（美元），计入系统净收益；默认 0"),
    "FUND_MAX_DAILY_COST_USD": (("model_budget", "max_daily_cost_usd"), _decimal, False, "模型", "每个交易日模型费用上限（美元，按 FUND_MODEL_PRICES_JSON 估算）；到达后当天不再发起新决策、反方审查和盘前研究"),
    # ----- User constraints (the operator's mandate and hard limits)
    "FUND_CAPITAL_USD": (("mandate", "capital_usd"), _capital, False, "约束", "AI 可用的授权资金（美元）；account 表示整个专用账户"),
    "FUND_ALLOW_EXTERNAL_HOLDINGS": (("mandate", "allow_external_holdings"), _bool, False, "约束", "账户里是否允许你自己的持仓（AI 不碰、数量冻结）"),
    "FUND_LOCAL_ACCOUNT_USD": (("mandate", "local_account_usd"), _decimal, False, "约束", "本地虚拟账户初始资金（仅新账本生效）"),
    "FUND_INSTRUCTIONS": (("mandate", "instructions"), _list, False, "约束", "给 AI 的文字约束，多条用 | 分隔"),
    "FUND_EXCLUDE_SYMBOLS": (None, None, False, "约束", "禁止交易的代码，逗号分隔（从投资范围和分组中移除）"),
    "FUND_EXTRA_STOCKS": (None, None, False, "约束", "加入投资范围的股票，逗号分隔；写成 代码:分组（例如 SHOP:technology）会计入该行业上限"),
    "FUND_EXTRA_ETFS": (None, None, False, "约束", "加入投资范围的非杠杆 ETF，格式同上；杠杆、反向和波动率产品请用下一项"),
    "FUND_EXTRA_LEVERAGED_ETFS": (None, None, False, "约束", "加入投资范围的杠杆、反向或波动率 ETF（如 SH、SDS、UVXY），格式同上；受下面两个上限约束"),
    "FUND_MAX_LEVERAGED_TOTAL": (("quant_risk", "max_leveraged_total"), _decimal, False, "约束", "杠杆、反向和波动率 ETF 合计最大权重，例如 0.15"),
    "FUND_MAX_LEVERAGED_POSITION": (("quant_risk", "max_leveraged_position"), _decimal, False, "约束", "单只杠杆、反向或波动率 ETF 最大权重，例如 0.05"),
    "FUND_RESEARCH_SYMBOLS": (None, None, False, "外部", "只供因子研究、不交易的额外股票，逗号分隔"),
    "FUND_GROUP_LIMITS": (None, None, False, "约束", "调整分组合计仓位上限，例如 technology:0.40,energy:0.10（只能改已有分组，0 到 1 之间）"),
    "FUND_MAX_POSITION": (("limits", "max_position"), _decimal, False, "约束", "单只证券最大权重，例如 0.10"),
    "FUND_MAX_EXPOSURE": (("limits", "max_exposure"), _decimal, False, "约束", "证券总敞口上限，例如 0.70"),
    "FUND_MAX_DRAWDOWN": (("limits", "drawdown"), _decimal, False, "约束", "回撤硬锁阈值，例如 0.08"),
    "FUND_DAILY_ORDERS": (("limits", "daily_orders"), _int, False, "约束", "每日提交上限（含改价重挂；卖出另有独立额度）"),
    "FUND_DAILY_LOSS_LIMIT": (("quant_risk", "daily_loss_limit"), _decimal, False, "约束", "当日亏损达到该比例停止买入"),
    "FUND_TARGET_VOL": (("quant_risk", "target_portfolio_vol"), _decimal, False, "约束", "组合年化波动率目标"),
    "FUND_MAX_ES": (("quant_risk", "max_portfolio_es"), _decimal, False, "约束", "1 日 97.5% 预期尾部损失上限"),
    "FUND_MAX_SCENARIO_LOSS": (("quant_risk", "max_scenario_loss"), _decimal, False, "约束", "历史危机情景（2008/2020/2022）下组合最大允许亏损"),
    "FUND_MAX_PORTFOLIO_BETA": (("quant_risk", "max_portfolio_beta"), _decimal, False, "约束", "组合相对 SPY 的 beta 上限（各持仓权重×beta 之和）"),
    "FUND_MAX_STYLE_EXPOSURE": (("quant_risk", "max_style_exposure"), _decimal, False, "约束", "动量、波动、规模三种风格暴露的上限（权重×标准分之和的绝对值）"),
    "FUND_CASH_SWEEP": (("cash_sweep", "enabled"), _bool, False, "约束", "闲置现金自动买入短期国债 ETF（SGOV），买入证券前先卖出筹款"),
    "FUND_EARNINGS_GUARD": (("events", "enabled"), _bool, False, "约束", "个股财报日（当天或下一个交易日）前自动把权重降到上限以内"),
    "FUND_EARNINGS_MAX_POSITION": (("events", "earnings_max_position"), _decimal, False, "约束", "财报前单只个股最大权重，例如 0.05"),
    "FUND_CONSTRUCTION_MODE": (("construction", "mode"), str, False, "约束", "组合构建：ai（AI 原始权重）、risk_budget（按 AI 权重分配风险贡献）或 auto（配对检验显著后自动切换）"),
    # ----- Approval envelope
    "FUND_AUTO_DAILY_EXPOSURE_INCREASE": (("approval", "auto_daily_exposure_increase"), _decimal, False, "审批", "无需审批的单日敞口增幅；0 表示所有买入都要审批"),
    "FUND_AUTO_DAILY_BUY_TURNOVER": (("approval", "auto_daily_buy_turnover"), _decimal, False, "审批", "无需审批的单日买入现金流（占净值）"),
    "FUND_STRESS_REQUIRES_APPROVAL": (("approval", "stress_requires_approval"), _bool, False, "审批", "市场压力下买入是否需要审批"),
    "FUND_APPROVAL_VALID_MINUTES": (("approval", "valid_minutes"), _int, False, "审批", "审批请求有效分钟数"),
    "FUND_TELEGRAM_BOT_TOKEN": (None, None, True, "通知", "Telegram 机器人 Token"),
    "FUND_TELEGRAM_CHAT_ID": (None, None, False, "通知", "接收通知并审批的 Telegram 会话 ID"),
    "FUND_TELEGRAM_APPROVER_IDS": (None, None, False, "通知", "群组中可审批的用户 ID，逗号分隔（私聊不需要）"),
    "FUND_TELEGRAM_APPROVALS": (None, None, False, "通知", "on/off：是否接受 Telegram 回复审批"),
    # ----- External data and runtime parameters
    "FUND_ITERATION_SECONDS": (("execution", "iteration_seconds"), _int, False, "外部", "盯盘迭代间隔（秒，15–300）"),
    "FUND_LIVE_STREAM": (("market_data", "live_stream"), _bool, False, "外部", "是否使用 Yahoo 实时推送报价"),
    "FUND_RESEARCH_RESOLVER": (("research_network", "resolver"), str, False, "外部", "研究网页解析方式 system/doh（Fake-IP 代理用 doh）"),
    "FUND_DATA_DESK": (("data_desk", "enabled"), _bool, False, "外部", "数据员：常驻进程持续拉取整理资讯、盘中行情、宏观和财报，研究员和基金经理随时读最新数据"),
    "FUND_DESK_INTRADAY_SECONDS": (("data_desk", "intraday_seconds"), _int, False, "外部", "数据员刷新盘中行情的间隔（秒，60–3600）"),
    "FUND_NEWS_ENABLED": (("news", "enabled"), _bool, False, "外部", "是否开启实时资讯监控"),
    "FUND_NEWS_POLL_SECONDS": (("news", "poll_seconds"), _int, False, "外部", "资讯轮询间隔（秒）"),
    "FUND_NEWS_QUERIES": (("news", "queries"), _list, False, "外部", "长期关注的新闻搜索词，多条用 | 分隔"),
    "FUND_NEWS_RSS_FEEDS": (("news", "rss_feeds"), _feeds, False, "外部", "自定义 RSS：名称=https://...，多条用 | 分隔"),
    "FUND_SEC_USER_AGENT": (None, None, False, "外部", "SEC 要求的联系人声明（姓名＋邮箱），用于读取 8-K 公告和按披露日的财报基本面"),
    "FRED_API_KEY": (None, None, True, "外部", "FRED Key（推荐，免费）：数据员的经济日历（CPI、非农、GDP、FOMC 公布日期）需要它；宏观数据不填时用公开 CSV"),
    "FUND_MACRO": (("macro", "enabled"), _bool, False, "外部", "是否给 AI 提供 FRED 宏观环境（美债收益率、利差、高收益债利差、VIX）"),
    "FINNHUB_API_KEY": (None, None, True, "外部", "Finnhub Key（可选）：更可靠的财报日历；不填用 Yahoo"),
    "FUND_SHADOW_AB": (("validation", "shadow_enabled"), _bool, False, "外部", "是否开启影子 A/B 验证 AI 的自我优化"),
    "FUND_FACTOR_LAB": (("factor_lab", "enabled"), _bool, False, "外部", "是否开启因子实验室：AI 复盘时提出因子，代码客观检验"),
    "FUND_MODEL_KNOWLEDGE_CUTOFF_JSON": (("factor_lab", "model_knowledge_cutoff"), _json, False, "外部",
                                         "各模型训练数据截止日 JSON，例如 {\"deepseek-chat\": \"YYYY-MM-DD\"}；之后的样本单独记为干净样本外，未填写的模型标为可能受记忆影响"),
    "ALPACA_API_KEY": (None, None, True, "券商", "Alpaca Paper Key（仅 paper 模式）"),
    "ALPACA_API_SECRET": (None, None, True, "券商", "Alpaca Paper Secret（仅 paper 模式）"),
    "ALPACA_IS_PAPER": (None, None, False, "券商", "必须为 true；false 会拒绝启动"),
    "LUMIWEALTH_API_KEY": (None, None, True, "券商", "可选：BotSpot／云端报告，本地交易不需要"),
    "SCHWAB_ACCOUNT_NUMBER": (None, None, True, "Schwab", "AI 使用的 Schwab 账户号码（建议单独开一个账户）"),
    "SCHWAB_APP_KEY": (None, None, True, "Schwab", "你自己的 Schwab 开发者应用 App Key（不填则用 LumiBot 默认应用）"),
    "SCHWAB_APP_SECRET": (None, None, True, "Schwab", "App Secret，用于自动续期 token；不填约 7 天需重新登录"),
    "SCHWAB_BACKEND_CALLBACK_URL": (None, None, False, "Schwab", "开发者应用中登记的回调地址，例如 https://127.0.0.1:8182"),
    "SCHWAB_TOKEN": (None, None, True, "Schwab", "无浏览器服务器首次登录时粘贴的一次性 token"),
    "SCHWAB_TOKEN_PATH": (None, None, False, "Schwab", "token.json 的保存位置（请放在私有目录）"),
}


# ----- Startup mode ---------------------------------------------------------------------
MODES = {"local": "真实行情本地模拟盘：Yahoo 行情 + 本地虚拟资金，不连接券商",
         "paper": "Alpaca 模拟账户：券商模拟撮合，不是真钱"}
# Commands that act on one trading account's directory. Historical, offline and
# preflight commands keep their own directory and never write into an account.
# Commands that act on the FUND_MODE account. The rehearsal, the prefetch and the data desk
# belong here too: otherwise they used the earlier default directory, not the running account.
ACCOUNT_COMMANDS = frozenset({"local", "paper", "report", "stop", "resume", "attach-order", "reset-drawdown",
                              "adjust-reserve", "acceptance", "approvals", "approve", "reject", "monitor",
                              "notify-test", "status", "decisions", "rehearse", "prefetch", "datadesk"})
EARLIER_DEFAULT = Path("artifacts/autonomous-fund")


def configured_mode(environ):
    """FUND_MODE, validated. No value of it can select real-money trading."""
    raw = str(environ.get("FUND_MODE") or "").strip().lower()
    if not raw:
        return None
    if raw not in MODES:
        raise RiskError(f"FUND_MODE={raw}: choose local or paper. This application has no live (real-money) "
                        "mode, and no setting can enable one.")
    return raw


def _earlier_book(mode):
    """An account the operator started before per-mode directories existed."""
    path = EARLIER_DEFAULT / "state.sqlite3"
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute("SELECT body FROM events WHERE kind='startup'").fetchall()
    except sqlite3.Error as exc:
        # Never fall through to a fresh book while an existing one cannot be read.
        raise RiskError(f"Cannot read the account book in {EARLIER_DEFAULT} ({type(exc).__name__}); "
                        "fix it or pass --run-dir") from None
    return any(json.loads(body).get("mode") == mode for (body,) in rows)


def startup(command, run_dir, environ):
    """Resolve (mode, run directory, observe-only, source) for a command.

    `start` (or no command) runs FUND_MODE, local by default. An explicit command
    or --run-dir always wins. Without --run-dir, account commands such as stop or
    approvals act on the FUND_MODE account: FUND_RUN_DIR, else artifacts/fund-<mode>,
    so local and paper books never share a directory. An account already kept in
    the earlier default directory stays there. The emergency stop never fails on
    a configuration mistake when it can still find the account directory.
    """
    custom = str(environ.get("FUND_RUN_DIR") or "").strip()
    try:
        configured = configured_mode(environ)
    except RiskError:
        if command != "stop":
            raise
        if run_dir is None and not custom:
            raise RiskError("FUND_MODE is invalid, so the account directory is unknown: run stop with --run-dir") from None
        return command, Path(run_dir or custom), False, "command line"
    mode, source = command, "command line"
    if command == "start":
        mode, source = (configured, "FUND_MODE") if configured else ("local", "default (FUND_MODE unset)")
    elif command in MODES and configured and configured != command:
        source = f"command line (FUND_MODE={configured} not used)"
    observe = False
    if mode in MODES and str(environ.get("FUND_OBSERVE_ONLY") or "").strip():
        try:
            observe = _bool(environ["FUND_OBSERVE_ONLY"])
        except RiskError as exc:
            raise RiskError(f"FUND_OBSERVE_ONLY: {exc}") from None
        if observe and mode != "local":
            raise RiskError("FUND_OBSERVE_ONLY=true is a local-mode option; set it to false for paper")
    if run_dir is None:
        account = mode if mode in MODES else (configured or "local")
        if mode not in ACCOUNT_COMMANDS:
            run_dir = EARLIER_DEFAULT
        elif custom and (mode not in MODES or configured in {None, mode}):
            run_dir = Path(custom)
        else:
            run_dir = Path(f"artifacts/fund-{account}")
            if not (run_dir / "state.sqlite3").exists() and _earlier_book(account):
                run_dir = EARLIER_DEFAULT
    return mode, Path(run_dir), observe, source


def _set(config, path, value):
    node = config
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def validate_names(names, origin="the selected .env file"):
    """Refuse unknown FUND_* names: a mistyped limit must not be silently ignored."""
    unknown = sorted(name for name in names if name.startswith("FUND_") and name not in SETTINGS)
    if unknown:
        hints = []
        for name in unknown:
            close = difflib.get_close_matches(name, [k for k in SETTINGS if k.startswith("FUND_")], n=1)
            hints.append(f"{name}" + (f" (did you mean {close[0]}?)" if close else ""))
        raise RiskError(f"Unknown FUND_* setting in {origin}: " + ", ".join(hints))


def validate_env_file(path):
    """Check the operator's .env names; other process variables are not this file's typos."""
    from dotenv import dotenv_values
    validate_names(dotenv_values(path).keys(), f"{path}")


def _symbol_items(raw, name, grouped=True, leveraged=False):
    """[(SYMBOL, group or None)] from "AMD, SHOP:technology"; duplicates are dropped."""
    import re
    items, seen = [], set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        symbol, _, group = part.partition(":")
        symbol, group = symbol.strip().upper(), group.strip().lower() or None
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            raise RiskError(f"{name}: {part!r} is not a ticker symbol")
        if group is not None and (not grouped or not re.fullmatch(r"[a-z_]{1,40}", group)):
            raise RiskError(f"{name}: {part!r}: " + ("a research symbol takes no group" if not grouped
                                                     else "group names are lower-case letters and _"))
        if symbol in LEVERAGED_OR_INVERSE and not leveraged:
            raise RiskError(f"{name}: {symbol} is a leveraged, inverse or volatility product; "
                            "admit it with FUND_EXTRA_LEVERAGED_ETFS, under its own caps")
        if symbol not in seen:
            seen.add(symbol)
            items.append((symbol, group))
    return items


def _add_symbols(result, environ, audit):
    """FUND_EXTRA_STOCKS / FUND_EXTRA_ETFS widen the tradable universe; FUND_RESEARCH_SYMBOLS the factor lab's."""
    groups = (result.get("quant_risk") or {}).get("groups")
    for name, kind in (("FUND_EXTRA_STOCKS", "stock"), ("FUND_EXTRA_ETFS", "unlevered_etf"),
                       ("FUND_EXTRA_LEVERAGED_ETFS", LEVERAGED_KIND)):
        items = _symbol_items(environ.get(name), name, leveraged=kind == LEVERAGED_KIND)
        for symbol, group in items:
            current = result["universe"].get(symbol)
            if current is not None and current != kind:
                raise RiskError(f"{name}: {symbol} is already admitted as {current}")
            result["universe"][symbol] = kind
            if group is None:
                continue
            if not groups or group not in groups:
                raise RiskError(f"{name}: unknown group {group}; groups: " + ", ".join(sorted(groups or {})))
            if symbol not in groups[group]["members"]:
                groups[group]["members"].append(symbol)
        if items:
            audit.append({"variable": name, "path": "universe",
                          "value": [f"{s}:{g}" if g else s for s, g in items]})
    research = _symbol_items(environ.get("FUND_RESEARCH_SYMBOLS"), "FUND_RESEARCH_SYMBOLS", grouped=False)
    if research:
        lab = result.setdefault("factor_lab", {})
        existing = list(lab.get("research_universe") or [])
        lab["research_universe"] = existing + [s for s, _ in research if s not in existing]
        audit.append({"variable": "FUND_RESEARCH_SYMBOLS", "path": "factor_lab.research_universe",
                      "value": [s for s, _ in research]})


def _group_limits(result, environ, audit):
    """FUND_GROUP_LIMITS: the operator's combined weight caps for existing groups, e.g. "technology:0.40".

    Only the operator sets them; the AI decides weights within them. A group that
    does not exist, or a limit outside (0, 1], is refused with the valid choices.
    """
    from decimal import Decimal, InvalidOperation
    raw = str(environ.get("FUND_GROUP_LIMITS") or "").strip()
    if not raw:
        return
    groups = (result.get("quant_risk") or {}).get("groups") or {}
    changed = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition(":")
        name = name.strip().lower()
        if name not in groups:
            raise RiskError(f"FUND_GROUP_LIMITS: unknown group {name or part!r}; groups: " + ", ".join(sorted(groups)))
        try:
            limit = Decimal(value.strip())
        except InvalidOperation:
            raise RiskError(f"FUND_GROUP_LIMITS: {part!r} needs a limit such as {name}:0.30") from None
        if not limit.is_finite() or not 0 < limit <= 1:
            raise RiskError(f"FUND_GROUP_LIMITS: {name} limit must be above 0 and at most 1 (a share of equity)")
        groups[name]["limit"] = format(limit.normalize(), "f")
        changed[name] = groups[name]["limit"]
    if changed:
        audit.append({"variable": "FUND_GROUP_LIMITS", "path": "quant_risk.groups",
                      "value": [f"{n}:{v}" for n, v in changed.items()]})


def apply_environment(config, environ):
    """Return (effective config, audit rows). Raises with the variable name on any bad value."""
    result, audit = copy.deepcopy(config), []
    for name, (path, parser, secret, section, _) in SETTINGS.items():
        raw = environ.get(name)
        if path is None or raw is None or not str(raw).strip():
            continue  # A blank template line means "not set": config.json keeps its value.
        try:
            value = parser(raw)
        except RiskError as exc:
            raise RiskError(f"{name}: {exc}") from None
        _set(result, path, value)
        audit.append({"variable": name, "path": ".".join(path), "value": value})
    _add_symbols(result, environ, audit)
    _group_limits(result, environ, audit)
    excluded = [s.strip().upper() for s in str(environ.get("FUND_EXCLUDE_SYMBOLS", "")).split(",") if s.strip()]
    if excluded:
        missing = sorted(set(excluded) - set(result["universe"]))
        if missing:
            raise RiskError("FUND_EXCLUDE_SYMBOLS: not in the eligible universe: " + ", ".join(missing))
        result["universe"] = {s: kind for s, kind in result["universe"].items() if s not in excluded}
        if not result["universe"]:
            raise RiskError("FUND_EXCLUDE_SYMBOLS removes the whole universe")
        groups = (result.get("quant_risk") or {}).get("groups")
        if groups:
            result["quant_risk"]["groups"] = {name: {**spec, "members": [m for m in spec["members"] if m not in excluded]}
                                              for name, spec in groups.items()}
        sweep = result.get("cash_sweep")
        if isinstance(sweep, dict) and sweep.get("symbol") in excluded:
            # Excluding the sweep symbol turns the sweep off rather than leaving it misconfigured.
            result["cash_sweep"] = {**sweep, "enabled": False}
        audit.append({"variable": "FUND_EXCLUDE_SYMBOLS", "path": "universe", "value": excluded})
    return result, audit


def describe(config, environ, audit):
    """Effective settings with their source; secrets only as set/missing."""
    overridden = {row["path"]: row["variable"] for row in audit}
    rows = []
    for name, (path, _, secret, section, text) in SETTINGS.items():
        present = bool(str(environ.get(name, "")).strip())
        if path is None:
            value = ("set" if present else "missing") if secret else (environ.get(name) or None)
            source = "environment" if present else "unset"
        else:
            node = config
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            value = node
            source = f"environment ({name})" if ".".join(path) in overridden else "config.json"
        rows.append({"section": section, "variable": name, "description": text, "value": value, "source": source})
    return rows
