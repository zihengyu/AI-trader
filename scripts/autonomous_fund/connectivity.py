"""Can this server reach every service the fund uses? Read-only probes for `check`.

Each probe is one small authenticated or public GET that costs nothing: no model
inference, no order, no message sent. Results carry the HTTP status and timing,
never a key, token or full URL, and a Chinese hint when a failure has a common
cause (for example a mainland-China server that cannot reach Telegram or Yahoo).
"""
import os
import time
from datetime import datetime, timedelta, timezone

from .risk import RiskError
from .settings import ALPACA_KEY_NAMES, alpaca_keys, configured_mode, paper_config

MAINLAND = "如果服务器在中国大陆地域，该服务通常无法直连：换香港／新加坡等境外地域，或在 .env 设置 HTTPS_PROXY"


def _probes(environ, models, mode, paper_ready):
    probes = [("yahoo", "Yahoo 行情（local 模式实时报价和历史）", "https://query1.finance.yahoo.com/v8/finance/chart/SPY",
               {"range": "5d", "interval": "1d"}, {"User-Agent": "Mozilla/5.0"}, mode == "local", MAINLAND)]
    token = str(environ.get("FUND_TELEGRAM_BOT_TOKEN", "")).strip()
    if token:
        probes.append(("telegram", "Telegram（审批和告警的唯一通道）", f"https://api.telegram.org/bot{token}/getMe",
                       None, None, True, MAINLAND + "；返回 401 说明 Token 错误"))
    providers = {m.split("/", 1)[0] for m in models}
    keys = {"deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/models", "Authorization", "Bearer "),
            "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1/models", "Authorization", "Bearer "),
            "anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/models", "x-api-key", "")}
    for provider in sorted(providers & set(keys)):
        name, url, header, prefix = keys[provider]
        key = str(environ.get(name, "")).strip()
        if key:
            headers = {header: prefix + key, **({"anthropic-version": "2023-06-01"} if provider == "anthropic" else {})}
            probes.append((provider, f"{provider} 模型 API（只列出模型，不调用推理）", url, None, headers, True,
                           "返回 401 说明 Key 错误；超时请检查网络或代理"))
    key, secret = alpaca_keys(environ)
    if key and secret:
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        if paper_ready:
            probes.append(("alpaca_paper", "Alpaca Paper 账户", "https://paper-api.alpaca.markets/v2/account", None,
                           headers, True, "返回 401／403 说明 Key 不是 Paper Key 或已失效"))
        purpose = ("Alpaca 历史行情（日线交叉核对，以及 Yahoo 不可用时的备用源）" if mode == "local"
                   else "Alpaca 历史行情 API")
        probes.append(("alpaca_data", purpose,
                       "https://data.alpaca.markets/v2/stocks/SPY/bars", {"timeframe": "1Day", "limit": 1},
                       headers, False, "返回 401／403 时请核对行情凭证和权限"))
    agent = str(environ.get("FUND_SEC_USER_AGENT", "")).strip()
    if agent:
        probes.append(("sec", "SEC EDGAR（8-K 公告和财报基本面）", "https://www.sec.gov/files/company_tickers.json",
                       None, {"User-Agent": agent}, False, "返回 403 通常是 FUND_SEC_USER_AGENT 没有写姓名和邮箱"))
    from .index_membership import SOURCE_URL, USER_AGENT
    probes.append(("index_membership", "标普 500 成分加入日期（因子检验按加入日期计入）", SOURCE_URL, None,
                   {"User-Agent": USER_AGENT}, False, "不可用时继续用缓存的列表；没有缓存时历史横截面不按加入日期筛选"))
    start = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    probes.append(("fred", "FRED 宏观数据", "https://fred.stlouisfed.org/graph/fredgraph.csv",
                   {"id": "DGS3MO", "cosd": start}, {"User-Agent": "LumiBot-AutonomousFund/1.0"}, False,
                   "不可用时 AI 只是少了宏观参考信息"))
    finnhub = str(environ.get("FINNHUB_API_KEY", "")).strip()
    if finnhub:
        probes.append(("finnhub", "Finnhub 财报日历", "https://finnhub.io/api/v1/quote",
                       {"symbol": "SPY", "token": finnhub}, None, False, "返回 401 说明 Key 错误"))
    return probes


def connectivity(environ=None, models=(), get=None, clock=time.monotonic):
    """{name: {"ok", "status", "milliseconds", "required", "purpose", "hint"?, "error"?}}."""
    import requests
    environ = os.environ if environ is None else environ
    get = get or requests.get
    mode = configured_mode(environ) or "local"
    results = _alpaca_config_issues(environ, mode)
    for name, purpose, url, params, headers, required, hint in _probes(
            environ, models, mode, mode == "paper" and "alpaca_paper" not in results):
        started = clock()
        row = {"purpose": purpose, "required": required}
        try:
            response = get(url, params=params, headers=headers, timeout=8)
            status = getattr(response, "status_code", None)
            row.update(ok=status is not None and 200 <= status < 300, status=status)
        except Exception as exc:  # Never echo the URL: it may carry a token.
            row.update(ok=False, status=None, error=type(exc).__name__)
        row["milliseconds"] = round((clock() - started) * 1000)
        if not row["ok"]:
            row["hint"] = hint
        results[name] = row
    return results


def _alpaca_config_issues(environ, mode):
    if mode == "paper":
        try:
            paper_config(environ)
        except RiskError as exc:
            return {"alpaca_paper": {"ok": False, "status": None, "milliseconds": 0, "required": True,
                                     "purpose": "Alpaca Paper 账户", "error": "configuration", "hint": str(exc)}}
    else:
        keys = alpaca_keys(environ)
        if any(keys) and not all(keys):
            missing = next(name for name, value in zip(ALPACA_KEY_NAMES, keys) if not value)
            return {"alpaca_data": {"ok": False, "status": None, "milliseconds": 0, "required": False,
                                    "purpose": "Alpaca 历史行情", "error": "configuration",
                                    "hint": f"缺少 {missing}，补齐后才能核对日线和使用备用行情"}}
    return {}


def summary(results):
    failed = [name for name, row in results.items() if not row["ok"]]
    blocking = [name for name in failed if results[name]["required"]]
    return {"all_ok": not failed, "failed": failed, "blocking": blocking}
