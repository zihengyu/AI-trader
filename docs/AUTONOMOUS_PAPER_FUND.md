# AI 自主组合管理：本地模拟交易

基于原生 LumiBot Agent、Strategy、Broker 和 BacktestingBroker 的纸面账户应用。

**Last Updated:** 2026-11-03

**Status:** 公开行情、本地虚拟账户及一次真实 DeepSeek 盘前研究已验证；量化风控、例外审批、授权资金分仓、盯盘、记分卡、Telegram 回复审批、实时推送报价与资讯监控已通过替身测试；Alpaca、真实推送和开盘交易待验证

**Audience:** 本地运行者和维护者

## Overview

本应用位于 `scripts/autonomous_fund/`，不修改 LumiBot 核心、原有 AI 示例、版本或发布流程。
Ubuntu 的 `.env` 模板、systemd 双服务、审批理由、自动日报和恢复验收步骤见
[服务器部署说明](AUTONOMOUS_FUND_UBUNTU.md)。该入口启动时保持 STOP，独立监控正常后由操作员恢复。
用到的每个数据源、局限和升级路线见[数据源与升级路线](AUTONOMOUS_FUND_DATA_SOURCES.md)。
`researcher` 研究行情、技术指标、基本面、新闻与持仓；`critic` 给出研究的反面论证；
`manager` 自主选择完整目标组合、现金比例和下次复核时间。
两者复用原生 `self.agents.create()` 和结构化 `output_schema`，均设置 `allow_trading=False`。
这是用户指定的混合策略：唯一的投资决策者输出目标，独立 Python 代码执行量化风控、风险准入与订单。
AI 的权重是上限：确定性量化风控只会把它们缩小（波动率目标、ES、历史危机情景、按波动率调整的单名上限、行业组上限、
财报前上限、回撤分档预算、单日亏损闸门和流动性），见[量化风控原则](#量化风控原则)。唯一的例外是有证据支持时
自动启用的风险预算构建：同样的证券和总仓位，按 AI 权重重新分配风险贡献，见[反方审查与组合构建 A/B](#反方审查与组合构建-ab)。
本应用不是对官方纯 AI 示例模式的修改。

没有固定买卖信号，也不要求每天交易。默认配置包含多只流动性较高的美股和普通 ETF；
操作员可审核并修改名单，AI 可在名单内自由选择。名单是操作员信任边界，不能让 AI 修改分类或把杠杆／反向基金伪装成普通 ETF。
每次交易前还检查最近 20 个已完成交易日的平均成交额至少 2,000 万美元。
这份当前名单不是历史成分股数据库；历史实验存在存活者偏差，严肃研究应提供当时可投资的名单。

## 安装和启动

在项目根目录执行。示例中不包含真实密钥；请通过当前进程环境或自己的秘密管理器注入凭证。
启动器不使用 LumiBot 的 `.env` 自动发现，只显式选用本仓库根目录的 `.env`（见[`.env`：运营者配置入口](#env运营者配置入口)）。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r scripts/autonomous_fund/requirements.txt
```

应用依赖固定 `litellm==1.105.1`，与框架要求的 MCP 1.x 配合使用；不修改共享依赖或升级 MCP 主版本。
修改依赖前先停止正在运行的模拟盘。以后重新安装共享依赖后，也需重新应用本应用的依赖约束。

### 启动模式：由 `.env` 的 `FUND_MODE` 决定

在 `.env` 中设置 `FUND_MODE` 后，**不带命令**启动（或服务器服务重启）就按该模式运行；修改后重新启动即生效：

| `FUND_MODE` | 含义 | 需要 |
|---|---|---|
| `local`（默认） | 真实行情本地模拟盘：Yahoo 实时行情＋本地虚拟资金，本地保守撮合，不连接任何券商 | 模型 Key |
| `paper` | Alpaca 模拟账户：券商模拟撮合，不是真钱 | 模型 Key＋Alpaca Paper Key |

```powershell
.\scripts\run_autonomous_fund.ps1
```

Linux：`bash scripts/run_autonomous_fund.sh`。启动时第一行会打印模式、来源（`FUND_MODE`／命令行／默认）和账户目录。

- **没有实盘选项**：`FUND_MODE` 只接受 `local`、`paper`，写 `live`、`schwab` 或其他任何值都拒绝启动。
- **账户目录**：`FUND_RUN_DIR` 留空时为 `artifacts/fund-<模式>`，local 和 paper 的账本自动分开，切换模式不会混用账本；
  若以前不带 `-RunDir` 启动过，账本在旧默认目录 `artifacts/autonomous-fund`，系统会识别并继续使用它。
  `stop`、`resume`、`approvals`、`report`、`acceptance` 等命令不带 `--run-dir` 时，也作用于 `FUND_MODE` 对应的账户。
- **只读观察**：`FUND_OBSERVE_ONLY=true`（仅 local）不调用模型、不下单。
- **显式命令优先**：`-Mode local`／`-Mode paper`（Linux 为 `local`／`paper`）或 `-RunDir` 仍然有效，并覆盖 `.env`；
  mock、backtest 等历史实验从不写入账户目录。
- `stop` 即使 `.env` 有拼写错误或文件权限不对也会执行（并提示错误），仍会读出其中的 `FUND_MODE`／`FUND_RUN_DIR`；
  不带 `--run-dir` 时，除了 `FUND_MODE` 对应的账户，还会停止所有最近一小时内有心跳的账户（包括刚退出、可能马上被服务重启的），
  因此用 `-Mode paper` 等显式命令启动的账户也不会漏停；某个目录写入失败不影响其他账户，最后会列出失败的目录。
  指定的 `--run-dir` 不存在时直接报错并列出最近活动的账户，不会因为路径写错而误报“已停止”。紧急停止不会被配置问题挡住。
- `show-config` 会显示最终的启动模式、来源和账户目录。

无需密钥的完整机械检查：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode mock -RunDir artifacts/fund-mock
```

它运行真实 LumiBot 回测引擎与限价订单，但价格和两名 Agent 的回答都是合成测试数据。
输出会明确标识 `mode=mock`、`scripted_model=true`，不证明投资收益。
每次历史实验使用新的目录，防止混合账本。PowerShell 启动器对回测设置 1,200 秒上限。
Python 模块同样可跨平台运行：`python -m scripts.autonomous_fund.run --help`。

## `.env`：运营者配置入口

模型 Key、你给 AI 的约束和外部参数都可以写在仓库根目录的 `.env`（已被 Git 忽略；从
`scripts/autonomous_fund/.env.example` 复制）。优先级为 **进程环境变量 > `.env` > `config.json`**：
`config.json` 保存完整的、经审阅的默认策略，`.env` 只覆盖你要改的项；模板里的风控与参数值与默认值一致，留空表示不覆盖；
模板中的 `FUND_MODE=local` 决定不带命令启动时进入本地模拟盘。
每个覆盖都按与 `config.json` 相同的规则校验，记入启动审计（`startup.environment_overrides`），并且会使旧目标失效、由 AI 按新规则重新决策。
`.env` 中写错的 `FUND_*` 名称会直接拒绝启动并提示正确名称，避免约束被静默忽略。

| 类别 | 变量 | 作用 |
|---|---|---|
| 启动 | `FUND_MODE`、`FUND_RUN_DIR`、`FUND_OBSERVE_ONLY`、`FUND_AUTO_RESUME` | 启动模式 local／paper（没有实盘）、账户目录、只读观察（见[启动模式](#启动模式由-env-的-fund_mode-决定)）、重启后安全条件下自动恢复交易 |
| 模型 | `DEEPSEEK_API_KEY`（及可选 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`） | 模型 Key |
| 模型 | `FUND_RESEARCH_MODEL`、`FUND_DECISION_MODEL`、`FUND_REASONING_EFFORT` | 研究员／基金经理模型与推理强度（角色变量优先于 `AI_MODEL`） |
| 模型 | `FUND_MAX_AGENT_RUNS_PER_DAY`、`FUND_MIN_REVIEW_MINUTES`、`FUND_MODEL_PRICES_JSON` | 每日调用上限、最短复核间隔、费用估算价格 |
| 模型 | `FUND_SKIP_QUIET_REVIEWS`、`FUND_MAX_QUIET_MINUTES` | 到点复核时无任何变化就跳过模型调用、连续跳过的最长分钟数（见[模型调用预算](#模型调用预算与失败退避)） |
| 约束 | `FUND_CAPITAL_USD`、`FUND_ALLOW_EXTERNAL_HOLDINGS`、`FUND_LOCAL_ACCOUNT_USD` | 授权资金（如账户 5 万只给 AI 1 万）、是否有你自己的持仓、本地虚拟账户资金 |
| 约束 | `FUND_MAX_POSITION`、`FUND_MAX_EXPOSURE`、`FUND_MAX_DRAWDOWN`、`FUND_DAILY_ORDERS` | 单只上限、总敞口、回撤硬锁、每日提交次数 |
| 约束 | `FUND_DAILY_LOSS_LIMIT`、`FUND_TARGET_VOL`、`FUND_MAX_ES` | 单日亏损闸门、波动率目标、尾部损失上限 |
| 约束 | `FUND_EXCLUDE_SYMBOLS`、`FUND_INSTRUCTIONS` | 禁止交易的代码（逗号分隔）、给 AI 的文字约束（`|` 分隔） |
| 审批 | `FUND_AUTO_DAILY_EXPOSURE_INCREASE`、`FUND_AUTO_DAILY_BUY_TURNOVER`、`FUND_STRESS_REQUIRES_APPROVAL`、`FUND_APPROVAL_VALID_MINUTES` | 自动执行额度（设 0 则所有买入都审批）、压力下是否审批、有效期 |
| 通知 | `FUND_TELEGRAM_BOT_TOKEN`、`FUND_TELEGRAM_CHAT_ID`、`FUND_TELEGRAM_APPROVER_IDS`、`FUND_TELEGRAM_APPROVALS` | Telegram 通知与回复审批 |
| 外部 | `FUND_ITERATION_SECONDS`、`FUND_LIVE_STREAM`、`FUND_RESEARCH_RESOLVER` | 盯盘间隔、实时推送报价、研究网页解析（Fake-IP 用 `doh`） |
| 外部 | `FUND_NEWS_ENABLED`、`FUND_NEWS_POLL_SECONDS`、`FUND_NEWS_QUERIES`、`FUND_NEWS_RSS_FEEDS`、`FUND_SEC_USER_AGENT`、`FRED_API_KEY` | 实时资讯监控与数据源 |
| 外部 | `FUND_SHADOW_AB`、`FUND_FACTOR_LAB` | 影子 A/B 验证 AI 的复盘学习；因子实验室（AI 提出因子、代码客观检验） |
| 券商 | `ALPACA_API_KEY`、`ALPACA_API_SECRET`、`ALPACA_IS_PAPER` | 仅 paper 模式 |
| Schwab | `SCHWAB_ACCOUNT_NUMBER`、`SCHWAB_APP_KEY`、`SCHWAB_APP_SECRET`、`SCHWAB_BACKEND_CALLBACK_URL`、`SCHWAB_TOKEN`、`SCHWAB_TOKEN_PATH` | 仅用于只读体检 `schwab-check`（见“Schwab 实盘准备”） |

投资范围的新增、行业分组和其余细节参数仍在 `config.json` 中修改（新增证券需要审阅分类）。
查看最终生效的配置及每项来源（密钥只显示已设置／缺失）：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode show-config
```

Windows 和 Linux 启动器都会显式选用仓库根目录的 `.env`（解析而非执行，进程环境优先），也可用 `-EnvFile`／`--env-file` 指定其他文件。
LumiBot 自身的 `.env` 自动发现仍保持关闭。

## 无券商账户：真实行情本地模拟盘

`local` 模式读取 Yahoo 公开行情，将虚拟资金（`mandate.local_account_usd`，默认 10,000 美元，仅在新账本创建时生效）、持仓和订单保存在本地。
它使用原生 `Broker`、`Order`、`Position`、`Strategy` 和 `Trader` 生命周期，
不构造任何券商交易客户端，不需要 Alpaca／Schwab 账户或券商 Key。
它不是券商 Paper 账户，成交属于明确标记的本地虚拟成交。

先检查真实行情与所选模型的凭证是否存在，不调用模型：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode check -RunDir artifacts/fund-market-check
```

默认研究员使用 `deepseek/deepseek-flash`，基金经理使用 `deepseek/deepseek-v4-pro`，共用你自己的 `DEEPSEEK_API_KEY`。
可在当前 PowerShell 中安全输入，输入不会显示，也不会写进命令历史或仓库：

```powershell
$fundKey = Read-Host 'DeepSeek API Key' -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $fundKey).Password
Remove-Variable fundKey
.\scripts\run_autonomous_fund.ps1 -Mode local -RunDir artifacts/fund-local
```

密钥最终仍是进程环境中的字符串；本例没有把它保存到文件。关闭终端后需重新注入。
推荐把 Key 写进仓库根目录的 `.env`（从 `scripts/autonomous_fund/.env.example` 复制），启动器会自动选用它；
也可用 `-EnvFile <your-secret-file>` 指定其他文件。现有进程环境优先，启动器不搜索其他目录的 `.env`。不要把真实密钥发到聊天或提交 Git。

没有模型 Key 时，只读观察模式可先验证启动、资金记录和交易日历，完全不调用模型或提交订单：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode local -ObserveOnly -Once -RunDir artifacts/fund-local-check
```

去掉 `-Once` 可持续运行只读观察；有 Key 后去掉 `-ObserveOnly` 启动 AI 自主研究和虚拟交易。
正常重启继续使用同一目录；不要通过换目录重置风险锁。`local` 的账户与券商账户彼此独立。
进程需保持运行、电脑需联网且不休眠。NYSE 休市时等待下次开盘，日历包含节假日、夏令时与提前收盘。
休市不妨碍研究：正常模式启动时先完成一次只读研究，周末也可运行；
原生 `before_market_opens` 钩子会在正常开盘前一小时更新研究。
同一目标交易时段、同一研究阶段成功完成后持久化去重，正常重启会读取已有结论；不是全天循环收费调用模型。
若在开盘前一小时内启动，启动研究与盘前钩子属于同一阶段，不重复成功调用。
开盘后基金经理直接评估实时账户、行情和风险，参考已保存的盘前结论及其原始时间，
并使用自己的只读研究工具核查信息。缺失、无效或较早交易日的预研会明确标注，
不再同步重跑研究员或等待其成功。经理的结构化目标仍需独立验证，再重新读取账户、行情和风控。
盘前研究不会成为待开盘直接执行的订单。
独立执行入口和本地撮合器均要求正常交易时段；盘前陈旧收盘价只供研究，不能用于成交。
`-ObserveOnly` 和人工 STOP 标记会阻止自动研究；历史回测的研究时间规则不变。

盘前研究可通过运行中的 `report` 命令查看 `latest_premarket_research`，
也会导出为 `premarket_research.json`，含研究起止时间、目标开盘时间、研究结论与证据。
修改代码后，已有进程需要在原 PowerShell 中 Ctrl+C 结束，再用原命令和原运行目录重启；
账户与风控账本保留，不要并行启动第二个交易进程。
`status.json` 的 `heartbeat_at` 表示轮询活性；`runner_state=exited` 表示已退出，
`stopped` 另指人工 STOP 标记。强制终止后应结合心跳年龄判断，旧状态文件不能证明进程仍在运行。

行情与撮合的具体限制：

- Yahoo 提供完成的一分钟 OHLCV，不保证交易所实时性、服务可用性或盘口覆盖；可能延迟或限流。
  报价保留源时间，超过 60 秒时禁止成交，不会放宽阈值或用接收时间冒充新行情。
  当前日线不进入技术分析／流动性样本，未完成的分钟线也不进入研究或撮合。
- 买卖价是围绕最新完成分钟收盘价加减 10 基点总价差的代理值，明确标注为
  `completed_minute_close_with_spread_proxy`，不是交易所实际 bid/ask。
- 下单后必须等待开始时间晚于提交时刻的一根新分钟线完整结束。
  买单用该分钟最高价再加半价差和 5 基点滑点，卖单用最低价减去同等摩擦，向不利方向取整。
  价格超出限价则不成交；单分钟最多取观测成交量的 1%，可部分成交；默认另计 0.1% 模拟费用。
  这些是保守的透明假设，不重放真实排队或订单簿，也不代表可实现收益。
- 成交时重新核验持仓／总敞口、现金、回撤、STOP、有效期和所有持仓报价的新鲜度。
  到期订单撤销，停机期间已过时的分钟线不追补成交。成交、现金、持仓和审计事件同一事务提交，
  重启不会重复成交或重复扣费。本地已接受订单及意图 ID 同一事务保存，没有券商网络确认的不确定窗口。
- 持有期间拆股同步调整数量和成本；无法整股表示的拆并股、供应商修改已处理动作时暂停撮合并记录异常。
  Yahoo 在拆股后按拆股比例改写历史分红金额（如 0.40 变为 0.20），这种改写不算修改；真正的金额修改仍需人工核对。
- 卖出（降风险）成交不要求其他持仓的报价新鲜，只要求本证券的成交价不早于已应用的拆股；买入仍要求所有持仓都有新鲜估值。
  分红仅按除息事件计入不可花费的应收款，因为此来源没有可靠付款日，首版不自动转为可交易现金。
  分红数量从除息日前的成交账本重建，除息日新买入不参与；若迟报时相关持仓已清空或重开，可能漏计，需人工补核。
  拆股迟报且已发生除息日后的交易时暂停撮合。复杂公司行动、退市、并购、换股和税务尚未完整覆盖，
  本地报告不能视作完整的税后总收益账单。

Yahoo 公开数据的使用还须符合其条款；本模式用于个人研究与功能验证。
若正常交易时段行情长期超时或延迟，需要在后续阶段接入有权限的行情服务，届时再配置该服务的 Key。

`Getting historical prices ... 10000 bars` 可来自原生递归指标的预热请求，不代表 AI 主动要求逐条研究一万天。
本地适配器接受正整数长度并返回供应商实际可用的已完成 K 线；不足请求数量时不填充、不伪造历史。
已通过原生 `get_indicator` → Strategy → 本地数据源的 EMA 回归，而不是只测试行情函数本身。
模型出现 `first ADK event` 说明已有模型事件，不代表整轮研究完成；成功标志仍是 `Pre-market research saved`。
`Agent run exceeded 300s timeout` 指整轮研究超时，原生运行时会重试；切勿把重试日志当成完成。
超时或被终止的调用可能没有返回完整 usage，报告中的模型成本不能视为完整账单，应同时核对提供方记录。

若使用 OpenClash 等代理，排查是否有流媒体解锁任务不断切换行情所用的主代理组。
先稳定该代理组，再运行 `check` 验证真实行情；不要通过关闭 TLS 校验或放宽报价时效来掩盖网络故障。

## 模型与数据凭证

模型分工在 `config.json` 的 `agent_models` 中配置：

```json
"agent_models": {
  "researcher": "deepseek/deepseek-flash",
  "manager": "deepseek/deepseek-v4-pro"
}
```

研究员负责休市／盘前及盘后研究；基金经理在正常交易时段自主研究、复核组合并输出结构化目标，
盘后通过同一个原生 Agent 的单次 `output_schema` 覆盖输出复盘，不创建额外的交易 Agent。
两者都没有直接下单权限，订单仍由独立风控执行层处理。决策遵循现有分钟轮询及经理指定的复核时间，
实际耗时还取决于模型和工具响应，不承诺逐笔行情级的延迟。
DeepSeek 官方当前的 `deepseek-flash` 对应 V4.1-Flash；旧 `deepseek-v4-flash` 名称虽兼容，
实际已路由到现行 Flash，不能用旧名字固定已退役的 V4 Flash。`deepseek-v4-pro` 是另一项独立模型。
参见[官方模型说明](https://api-docs.deepseek.com/quick_start/pricing/)。

覆盖优先级依次为：角色专用命令参数、通用 `--model`／`AI_MODEL`、`agent_models`、旧的 `model` 字段。
通用模型参数会同时覆盖两个角色。需要明确指定分工时使用：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode local -RunDir artifacts/fund-local -ResearchModel deepseek/deepseek-flash -DecisionModel deepseek/deepseek-v4-pro
```

Python 对应参数是 `--research-model` 和 `--decision-model`。启动时打印实际选择的两个模型；
每个研究／决策事件保存调用结果的模型 ID 和 usage，报告还提供 `models` 与 `model_usage_by_model`。
模型配置修改需要停掉旧进程后重启。账本只绑定**账户身份**（模式＋账户），不再绑定配置：
同目录恢复现金、持仓、提交次数、不确定订单、高水位和回撤锁；换模型、调整风险参数或投资范围都沿用同一账本。
每次启动把全部风险策略（`limits`、`quant_risk`、`approval`、`execution`、`model_budget`、投资范围）
写入 `startup` 事件并计算摘要；摘要与上次不同时记录 `risk_policy_changed`，旧的有效目标随之失效，
等待 AI 在新规则下重新决策。旧版（revision 2）账本首次启动会自动迁移并记录 `binding_migrated`：
本地虚拟账户以账本自身历史证明身份；Alpaca Paper 账本必须与同一凭证和原配置精确匹配，否则拒绝迁移。
模式或账户不同的账本仍然拒绝打开，不能借用另一个账户的高水位或订单。
历史盘前研究保留原来的模型来源，重启不把既有结论重新标记成新模型生成。

| 用途 | 环境变量 | 说明 |
|---|---|---|
| 模型选择 | `AI_MODEL` | 可选的双角色通用覆盖；默认按 `agent_models` 分工，也支持 `openai/<model-id>`、`anthropic/<model-id>` |
| OpenAI | `OPENAI_API_KEY` | 只需要所选提供方的 Key |
| Claude | `ANTHROPIC_API_KEY` | 原生运行时通过 LiteLLM 调用 |
| DeepSeek | `DEEPSEEK_API_KEY` | 原生运行时通过 LiteLLM 调用；本地真实行情模拟盘只需这个 Key |
| Alpaca Paper | `ALPACA_API_KEY`、`ALPACA_API_SECRET` | 必须使用 Paper 账户自己的凭证 |
| 模拟标识 | `ALPACA_IS_PAPER=true` | 若明确设成 false，应用拒绝启动 |
| 可选宏观研究 | `FRED_API_KEY` | 没有时其他研究工具仍可用 |

不需要同时填写全部模型 Key。模型 ID 必须是账户实际支持的 ID，切换提供方不会伪造兼容性测试。
`AI_MODEL` 只影响此应用，不修改 LumiBot 其他策略的默认模型。
真实模型运行会产生提供方费用；自动化测试使用明确标记的模型替身或 HTTP 替身。
应用附加依赖安装原生免费 `web_search` 所需的 `ddgs`，不需要额外搜索 Key。
浏览器工具还需可选 Playwright 与浏览器运行时；未安装时仍可使用搜索及 HTTP 文档读取，
网页反爬、付费墙或网络限流可能导致研究缺项，模型必须披露。没有把这些研究源伪装成完整市场覆盖。

若模型调用重复报 `cannot import name 'CacheableResult' from 'mcp.types'`，这是已复现的
LiteLLM 1.106.2 与 MCP 1.x 工具调用路径不兼容，不是更换 Key 或等待重试可以解决的问题。
停止旧进程后，在项目根目录执行以下命令，再用原终端、原运行目录重新启动：

```powershell
.\.venv\Scripts\python.exe -m pip install -r scripts/autonomous_fund/requirements.txt
.\scripts\run_autonomous_fund.ps1 -Mode local -RunDir artifacts/fund-local
```

不要升级到框架不支持的 MCP 2.x，也不要手动修改 SDK 源码补造缺失类型。
`pip check` 和单独导入模块不能发现这类运行时不兼容；对应测试会实际经过 LiteLLM 的工具请求路径，
并通过原生 ADK 执行一次工具调用和结果回传。HTTP 响应是合成的，不访问提供方。
上游记录：[LiteLLM issue 46045](https://github.com/BerriAI/litellm/issues/46045)。

## Alpaca Paper 运行

先在 Alpaca 端准备一个独立的 Paper 账户：首次启动时不能持有投资范围内的证券或未完成订单，现金不少于
`mandate.capital_usd`（默认 10,000 美元）。超出部分记为保留现金，AI 永远不能动用（见[授权资金与约束](#授权资金与约束)）。
未设置 `capital_usd` 时沿用旧规则：必须是净值恰好 10,000 美元的空账户。应用不会替你重置账户、转入资金或清理其他交易。
历史回测的初始资金也固定为 10,000 美元。

在安全注入上述 Alpaca 与模型环境变量后执行：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode paper -RunDir artifacts/fund-paper
```

Paper 模式只接受原生 Alpaca 类、`PAPER=True`，并独立检查 SDK 实际交易地址为
`https://paper-api.alpaca.markets`。不存在 `live`、Schwab 或任意券商选择开关。
市场数据仍由 Alpaca 原生适配器读取。其最新报价接口可能受账户数据权限限制；
如无权限、无真实源时间戳、报价超过 60 秒或价差超过 2%，跳过交易并记录异常。
不会把接收时间冒充行情时间，也不会静默替换行情供应商。
免费 IEX 与 SIP 不是相同覆盖面；具体订阅和账户权限需在你的账户验证。

默认每 30 秒检查订单、实际现金和持仓、紧急停止与观测回撤（可配置）。
模型复核时间由基金经理指定，并受最短间隔（默认 15 分钟）和每日预算约束；
市场关闭时由 LumiBot 原生生命周期管理。
AI 的完整目标组合中，遗漏的已有持仓代表卖出，空目标列表代表全部现金。
只处理整股股票／ETF；外部加入的碎股、空仓、衍生品或名单外资产会阻止新增交易，要求人工核查。

## 独立风控与执行合同

默认风险参数集中在 `config.json`，加载成不可变策略对象；模型没有配置写入或交易修改工具。
本应用还锁定研究用 DuckDB 的文件／扩展访问，防止多语句 SQL 读写风险账本或本地凭证；
已加载的行情／文档表仍可分析。研究浏览器只接受 HTTP(S) 导航，不能直接打开本地文件或脚本 URL，
下载和上传保留在独立研究目录。浏览器为原生可选依赖，本次未做真实浏览器联网验证。
禁止融资、卖空、期权和期货；只提交整股、当日有效限价单。
硬上限：单证券 10%、总证券敞口 70%、每日提交上限 20 次（含改价重挂）、观测净值高点回撤 8% 锁定增仓。
提交上限按方向分开计算：买入计入当日**全部**提交（大量卖出不会腾出买入额度）；卖出只计卖出，另有同样大小的额度，保护性止损／止盈再多 5 次。这样买入和追价重挂用尽额度的当天（往往正是大跌日），回撤降仓和止损卖出仍能执行。
拒绝非有限数值、重复证券、超额目标、未来／过期决策、陈旧账户或报价、缺失流动性证据。
日限额按纽约交易日计算，买卖、失败和取消的提交均占用次数。
上涨使持仓超过上限、或回撤使预算下降时，执行中按约束自动减持（超过再平衡带时）；
到达 8% 硬锁后预算降到 25% 敞口并自动卖出超出部分。跳空和停牌造成的实际亏损不保证止于这些阈值。

每笔订单都走同一条确定性路径：AI 目标 → [量化构建](#量化风控原则)（只缩小权重）→ 自动额度／审批判断 →
按净值、现金、实际持仓和限价计算整股数量 → 预留并提交。规划和预留前各独立计算一次，结果不同即拒绝。
先执行减仓，每次最多提交一笔，再等待状态确认、重新读取资金和持仓，继续执行仍有效的目标。
目标在新鲜模型回答通过校验时持久化，后续每次迭代可继续调仓，不必等待下一次模型复核。
新回答仍须在五分钟内接受；已接受目标只保留到 `next_review_at`（不早于最短复核间隔）与 `expires_at` 较早者，
每一笔继续执行都重验账户、报价、流动性、回撤、订单次数和审批。重启恢复目标与复核时间，不重置账户。
目标到期时尝试撤销本应用的未完成订单，并等待券商确认后再接受替代目标。
未成交卖单不能为买单提供资金；账户中任何未完成或未知订单都会阻止新的订单。

执行质量控制（`execution` 段）：

- **改价重挂**：限价单挂出超过 `reprice_after_seconds`（默认 180 秒）仍未完成，就请求撤单；
  券商确认撤单后，下一次迭代按新报价和全部风控重新计算，而不是让一张不成交的单冻结整个组合。
  撤单是异步的（券商会先显示“撤单中”）：同一订单只请求一次，确认前不会每次迭代重复请求；
  若 240 秒后券商仍显示该单在工作中（请求可能丢失），才再请求一次。
- **追价上限**：同一决策内同一证券的买入限价不能高于首次提交（到达价）的 `max_buy_chase`（默认 0.75%）；
  超出即停止追买该证券并记录 `buy_chase_limit`。卖出（降风险）不设追价上限。
- **再平衡带**：小于 `max(min_trade_notional, rebalance_band × 净值)`（默认 max(100 美元, 0.5%)）的调整不交易，
  避免价格小幅波动造成来回买卖；目标为 0 的完全卖出始终执行。
- **流动性参与率**：单笔名义金额不超过最近 20 个已完成交易日平均成交额的 `max_adv_participation`（默认 1%）。

SQLite 在下单前原子保存意图和日限额，写入失败则不下单。账本使用 WAL 和按事件类型的索引，
监控读取不再阻塞交易写入；`events.jsonl` 增量追加（被改动则自动全量重建），报告每 `report_interval_seconds`
（默认 300 秒）重建一次，收盘和退出时总会重建。
提交超时、响应丢失或程序崩溃后不自动重发。同目录进程锁和相同 API Key 的跨目录进程锁避免重复启动。
请只用一个专用 Paper 账户、一个凭证及本应用交易；不同机器、不同 Key、外部手动交易或其他程序不受本地锁控制。
Alpaca 模式只查询**未完成**订单（上限 500 条，达到即拒绝继续），本应用自己的订单逐笔按券商 ID 核对，
历史订单数量不再导致账户在约 100 笔后永久锁死。

正常重启继续使用同一 `--run-dir` 和凭证。账本与账户／模式绑定，不会把另一账户的高水位或订单借来使用。
已保存券商 ID 的订单逐笔向券商查询。完整成交可从券商累计数量和均价恢复，标记为
`broker_cumulative_observation`，时间是观察时间；不会虚构停机期间的逐笔执行时间。
部分成交通过原生回调记录；停机期间部分成交后又取消的明细可能缺失，需要券商成交记录补核。
账户状态重读仍用于风险校验，缺失费用不会按零费用包装。

若崩溃恰好发生在券商接受之后、ID 保存之前，系统保持锁定。
先在 Paper 券商界面确认确切订单，再停掉本应用进程，使用：

```powershell
.\.venv\Scripts\python.exe -m scripts.autonomous_fund.run attach-order --run-dir artifacts/fund-paper --intent-id <intent-id> --broker-order-id <broker-order-id>
```

恢复程序校验相同凭证／账户、证券、方向、完整数量、限价及创建时间窗口，只绑定已有订单，不发单。
若确认从未发出订单，保留证据并人工维护该预留；首版不提供一键“假装失败后重试”。

## 授权资金与约束

人的角色是设定约束、审批例外和阅读复盘。约束写在 `config.json` 的 `mandate` 段：

```json
"mandate": {
  "capital_usd": "10000",
  "allow_external_holdings": false,
  "local_account_usd": "10000",
  "instructions": ["只用授权资金 capital_usd 交易……", "不使用杠杆、融资、卖空……"]
}
```

- **授权资金 `capital_usd`**：账户有 5 万美元而只允许 AI 用 1 万时，设为 `"10000"`。首次启动时把超出部分记为
  **保留现金**（`mandate_reserve_cash`），AI 看到的账户、净值、仓位比例、回撤、单日亏损和全部量化约束都只按这 1 万的分仓计算；
  分仓现金永远不能低于 0，因此不可能动用保留现金，也不可能形成融资或杠杆。本地撮合器在成交瞬间也按分仓复核。
- **调整授权**：修改 `capital_usd` 后重启即在保留现金和分仓之间划转并记录 `mandate_capital_changed`；
  调高不能超过保留现金，调低不能超过分仓当前现金（先减仓）。
- **账户出入金**：先停止交易进程，再记录到保留现金，分仓不受影响：
  `python -m scripts.autonomous_fund.run adjust-reserve --run-dir <run-dir> --amount 5000`（取出用负数）。
- **外部持仓**：默认要求专用账户。`allow_external_holdings: true` 时，投资范围以外的股票视为你自己的持仓，
  不计入分仓、AI 不会交易；它们的数量在首次启动时冻结，之后若有变化（你手动买卖）则停止交易，
  核对后用 `adjust-reserve --rebaseline-external`（必要时同时 `--amount`）重新确认。投资范围以内的证券不能作为外部持仓。
- **文字约束 `instructions`**：最多 20 条，例如“财报前两个交易日不新建仓位”“单一行业不超过 20%”。
  研究员和基金经理每次都收到这些约束并被要求遵守；盘后复盘必须在 `mandate_compliance` 中逐条自评，日报展示。
  能写成数字的约束请优先放进 `limits`、`quant_risk` 或 `approval`，由代码强制执行；文字约束依赖 AI 遵守，需要你抽查日报。

## 分钟级盯盘与 AI 择时

AI 不需要每分钟被调用。基金经理在每次复核时为每个目标可选地给出执行价位，并可设置价格提醒：

| 字段 | 含义 | 执行者 |
|---|---|---|
| `entry_limit` | 只有卖价 ≤ 该价才买入（等回调） | 确定性代码，每分钟检查 |
| `stop_loss` | 买价 ≤ 该价时清掉该持仓 | 自动卖出，无需审批 |
| `take_profit` + `take_profit_weight` | 买价 ≥ 该价时减到指定权重（默认 0） | 自动卖出，无需审批 |
| `alerts`（`above`／`below`／`reason`） | 价格穿越时提前唤醒 AI 复核 | 只唤醒，不下单 |

盯盘循环每次迭代（本地／Paper 为 1 分钟）运行，不调用模型：
- 止损／止盈触发后立即挂**可成交的**卖出限价单：限价为买价下方 `execution.protective_limit_buffer`（默认 0.5%，
  普通调仓只有 0.1%），并在 `execution.protective_reprice_seconds`（默认 60 秒，普通单 180 秒）后未成交就撤单，
  按新报价重挂，直到减到目标，不会“触发一次就忘”。保护性卖出优先保证离场，而不是争取价格。
  同一决策内被止损的证券不会被重新买回，等 AI 下一次复核再决定。
- 有挂单（例如一笔买单在等成交）时，止损／止盈照常检测；需要卖出时，先撤掉本应用的非保护性挂单，让止损优先。
- 止盈减仓的股数在触发时一次算定，价格继续上涨也不会一股一股地追着卖；减仓完成后，剩余股份的**止损继续有效**。
  减仓还没卖完时价格跌破止损，会改为全部卖出。
- 买价已经跌到 AI 自己给出的 `stop_loss` 或以下时，不会再新买入该证券（记录 `stop_breached_no_buy`），
  已挂出的该证券买单会立即撤销（记录 `buy_canceled_stop_breached`），不等待 180 秒改价周期。
- 价格提醒、持仓或目标证券相对本决策首次观测价的波动达到 `execution.review_on_move`（默认 3%）、
  市场进入压力状态时，会把下一次 AI 复核提前；提前复核仍遵守最短复核间隔、失败退避和每日模型预算。
  基金经理在 `watch_triggers` 中看到被唤醒的原因。
- 每次新的 AI 决策替换全部旧价位和提醒，**正在执行的止损／止盈卖出除外**：止损触发后提前唤醒的新决策不会中断它，
  卖出完成前不买入该证券；新决策为该证券设的价位在卖出完成后才生效。
  决策中的价位同样受校验：止损须低于入场价和止盈价，止盈须高于入场价（否则买入后立刻触发止盈，白付价差），
  `take_profit_weight` 须低于目标权重，提醒只能针对投资范围内的证券。

这让“盯盘”成本接近零，而择时（入场价、止损、止盈、何时复核）仍由 AI 决定。
行情仍是 Yahoo 已完成分钟线，触发和成交有一到两分钟延迟，不是逐笔级别的实时盯盘。

## 复盘记分卡：AI 自我优化的客观反馈

为避免 AI 只凭叙述“自我感觉良好”，复盘闭环加入两张只用**已完成收盘价**计算的记分卡（`scorecard.py`）：

- **预测记分卡**：每次盘后复盘可给出最多 10 条可证伪预测（证券、`up`／`down`／`outperform`／`underperform`、
  1–20 个交易日、基准、对应的假设）。到期后自动打分，按假设汇总命中率；未到期为 `pending`，缺数据为 `unavailable`，不估算。
- **决策记分卡**：每个交易日最后一个 AI 目标组合，从当日收盘起算 1／5／20 个交易日的收益，
  减去**按组合 beta 匹配**的 SPY 收益，得到选股超额收益（`excess_return`）。每只证券的 beta 用决策当日及之前
  126 个交易日的收盘价估计（不用之后的数据）；少于 20 个样本的证券按 beta 1 计并列在 `beta_assumed`。
  这样牛市里重仓高 beta 股票不会被记成“选股能力”。`net_excess_return` 再扣掉交易成本：
  与上一个决策相比的目标权重变化 × 每边 10 个基点（第一个决策从全现金算起）。
  旧口径（同等总敞口）保留为 `exposure_excess_return` 供对比。不含风控缩放，专门衡量 AI 的选股能力。
- **提示词版本**：系统提示词、输出结构和模型名的摘要（`prompt_version`，12 位十六进制）写进启动记录和每个
  `decision_validated`；变化时记录 `prompt_version_changed`。决策记分卡按版本分组（`by_prompt_version`），
  改了提示词之后能分开比较新旧版本，而不是混在一起。

另有**择时记分卡**（`scorecards.timing`），客观衡量 AI 自己设定的价位：每次止损／止盈离场后 1／5／20 个交易日的收盘价
相对离场价的变化（继续下跌为正贡献、反弹为负）；每次等待入场价时，若之后买入，比较买入限价与开始等待时的卖价，
若一直没买，比较之后的收盘价（上涨即错过的收益）。复盘据此校准止损和入场价的松紧，但不应因此放弃保护性止损。

这些记分卡进入盘后复盘、研究和决策的上下文，复盘被要求据此保留、修正或撤回假设，结果写入日报和 `report.json` 的 `latest_scorecard`。
这是客观反馈而不是自动改参数：风险参数、投资范围和权限仍只能由你修改。样本少时结论不稳定，重叠期限之间相关。

## 因子实验室：AI 挖掘因子，代码客观检验

因子挖掘只在盘后做：复盘的研究阶段结合以往检验结果研究可能的新规律，基金经理阶段把它写成最多 5 个因子表达式（`factor_proposals`），
代码随即检验。盘中只使用已验证因子的排名：排名来自最近一个已完成收盘，开盘前预热一次，全天复用，不占用盘中盯盘和下单的时间。

这些因子表达式由确定性代码（`factors.py`）用无前视的历史数据检验，
结果在之后的每次决策和复盘中交给 AI。闭环是：**AI 发现规律 → 写成因子 → 代码检验 → 通过的因子及其实盘表现回到 AI → AI 修正或提出新因子**。

**两个横截面，不混排**：每个因子只在一个横截面里排名（`factor_proposals[].universe`）：
`stocks` 是可交易个股加上**不可交易的研究股票池**（`factor_lab.research_universe`，默认 40 只大盘股，只用来增加横截面宽度，
从不作为投资建议出现）；`etfs` 是可交易的普通 ETF（不含现金停放用的 SGOV）。个股和 ETF 混在一起排名时，
低波动之类的因子主要是在区分“ETF 比个股稳”，测到的是资产类型而不是选股能力。升级前在混合横截面上验证的因子会自动退役
（理由写明，仍计入尝试次数），需要按新横截面重新提出。注意研究股票池是今天的大盘股，有幸存者偏差。

**因子语言**（安全白名单，不执行任何 Python 代码）：价格数据 `close`、`volume`、`open`、`high`、`low`（已完成日线）；
基本面数据 `earnings`、`revenue`、`gross_profit`、`cashflow`（近四个季度合计的净利润、营收、毛利、经营现金流）、
`book`、`assets`（最新净资产、总资产）、`shares`（封面披露的普通股股数），来自 SEC EDGAR XBRL（见下）；时序函数
`ret(n)`、`vol(n)`、`delay`、`delta`、`ts_mean`、`ts_std`、`ts_max`、`ts_min`、`ts_rank`、`log`、`abs`、`sign`；
横截面函数 `rank`、`zscore`、`demean`；`+ - * /`。所有运算只看过去，窗口 1–504 个交易日。例如：
`rank(ret(126) - ret(21))`（中期动量）、`-vol(60)`（低波动）、`rank(close / ts_mean(close, 200) - 1)`、`-rank(ret(5))`（短期反转）。
基本面例子：`rank(earnings / (close * shares))`（盈利收益率）、`rank(book / (close * shares))`（账面市值比）、
`rank(gross_profit / assets)`（毛利率质量）、`rank(revenue / delay(revenue, 252) - 1)`（营收增长）。

**按披露日的基本面**（`fundamentals.py`）：SEC 的公司财务数据（company facts）每个数值都带有所在报告的受理日期 `filed`。
某个日期只能用**受理日期早于当天**的报告；后来的报告重述了某个季度，重述值也只从它自己的受理日起生效，所以历史回测没有前视。
10-Q 的利润表给出单季，现金流量表只给年初至今累计，代码按同一财年起点做差得到单季，第四季度 = 全年 − 前三季度；
必须有连续四个季度才计算近四季合计，否则为空，不外推。同一日期披露多个股票类别股数的公司（如双重股权）股数为空。
ETF 没有基本面。需要在 `.env` 设置 `FUND_SEC_USER_AGENT`（姓名＋邮箱，SEC 的要求）；不设置时基本面字段全为空，
使用它们的因子检验结果为数据不足。每家公司只缓存所需科目（`<账户目录>/cache/sec/`），最近一次报告约 80 天后才重新下载，
最多每周一次，请求间隔 0.15 秒（低于 SEC 每秒 10 次的上限）。

**检验标准**（防止“挖掘”变成过拟合）：
- 每个日期在投资范围内计算因子值与之后 `horizon_sessions` 个交易日收益的横截面 rank IC（Spearman），
  每隔 `horizon_sessions` 取一个日期，避免重叠收益虚增 t 值；
- 前 70% 为样本内，`|t|` 必须超过随**累计尝试次数**提高的 Bonferroni 门槛（试 1 个约 1.96，50 个约 3.29，200 个约 3.66）；
  同一表达式重复提出不算新尝试，但也不会重新检验；
- 后 30% 为样本外，方向必须一致且 `t ≥ holdout_min_t`（默认 1.0）；
- 前 20% 与后 20% 的多空收益差扣除交易成本后必须仍与方向一致：成本 = 4 × 五分位换手率 × `cost_bps`（默认每边 10 个基点）；
- 与同一横截面里已验证因子的排名平均相关系数 ≥ `max_redundancy`（默认 0.7）的候选记为 `redundant`（冗余），不算新发现；
- 同时报告 1／5／20 个交易日的 IC 衰减和排名自相关（排名变化越慢，换手越低），都只用样本内数据，样本外保持干净；
- 通过后跟踪**验证日之后**的 IC，这是真正的样本外记录；至少 `live_min_observations`（默认 10）个观测后若平均 IC 与方向相反，自动退役。

**AI 看到什么**（`factor_lab` 上下文）：语法、检验标准、已尝试次数和下一个门槛、已验证因子（样本内／样本外／实盘 IC、
当前排名靠前和靠后的**可交易**证券）、最近被拒绝、冗余或退役的因子及原因、基本面数据状态。因子**只是证据**，不会直接下单；
AI 仍决定完整目标组合，量化风控和审批规则不变。影子 A/B 的对照组看不到因子实验室，因此 A/B 衡量的是包括因子在内的整个学习闭环。

配置在 `config.json` 的 `factor_lab` 段（`.env` 中 `FUND_FACTOR_LAB=false` 可关闭）：历史长度 504 个交易日、
每个日期至少 8 个证券、样本内至少 20 个观测、样本外至少 8 个、显著性 5%。离线检验单个表达式：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode factor-lab -DataDir data -Symbols SPY,TLT,GLD,IEF,DJP -Expression "rank(ret(60))" -Horizon 5 -MinNames 5
```

Linux 用 `--expression=...` 形式（表达式以 `-` 开头时必须如此），例如 `--expression=-vol(20)`。
**实测**：仓库自带的 2019 年 5 只 ETF 日线上，`rank(ret(20))`、`-rank(ret(5))`、`-vol(20)`、`rank(ret(60))` 全部被拒绝
（样本内 |t| 0.11–1.21，低于 1.96）。5 只证券、一年数据远不足以证明任何因子，这正是检验应有的结论；
运行中的基金个股横截面为 16 只可交易个股加 40 只研究股票，ETF 横截面为 14 只 ETF，两年日线。合成数据测试中，真实存在的动量规律在所有种子下都通过，纯噪声在所有种子下都被拒绝。

## 实时行情：Yahoo 推送报价

`market_data.live_stream: true`（默认）时，本地模式在后台订阅 Yahoo 的 WebSocket 推送（yfinance 自带的非官方接口，
`wss://streamer.finance.yahoo.com`），对被报价过的证券持续接收最新成交。报价规则：
- 只在 NYSE 正常交易时段使用；成交的**源时间**须晚于最近一根已完成分钟线、且不超过 `live_seconds`（默认 60 秒）；
  未来时间戳、乱序帧、休市推送都不采用。推送带买卖价且合理时使用，否则仍按披露的 10 基点价差代理。
- 拿不到新鲜推送时退回已完成分钟线，`price_kind` 和 `report`／`status.json` 的 `quotes_by_kind` 记录每次用的是哪种来源。
- 断线自动重连（指数退避），错误次数和最后错误写入诊断，不会把旧价格当成新价格。
- 本地模拟**成交**仍按提交后新完成的一分钟 K 线保守撮合，不因推送而变乐观。

配合 `execution.iteration_seconds`（默认配置 30 秒，可设 15–300），止损、止盈、提醒的反应时间从 1–2 分钟缩短到半分钟左右。
`check` 会尝试接收 SPY／QQQ 推送并报告是否连通。

局限：这是非官方接口，没有服务等级承诺，Yahoo 条款限制个人用途，可能随时变更或被限流；
数据来自部分交易所的最新成交而不是全市场合并报价（SIP），成交量不完整，股票通常没有买卖盘。
用于个人研究和模拟足够；真钱交易应改用券商行情（Alpaca 免费 IEX 实时、Schwab 开户后的免费推送）或付费 SIP 数据。

## 实时资讯：AI 综合考虑的信息流

`news.enabled: true` 时，后台线程（不占用交易线程，不调用模型）每 `poll_seconds`（默认 300 秒，休市时放慢 4 倍）轮询：

| 来源 | 内容 | 触发提前复核 |
|---|---|---|
| Yahoo 个股新闻（yfinance） | 整个投资范围的最新标题 | 持仓证券在 `burst_minutes`（60 分钟）内新增 ≥ `burst_threshold`（3）条 |
| Google News RSS | 运营者 `queries`（默认：美联储利率决议、CPI、非农）和 AI 在决策中给出的 `news_watch` 搜索词 | 出现新的匹配标题 |
| 美联储新闻稿 RSS | 利率决议、讲话、监管公告 | 每一条新发布 |
| SEC EDGAR 8-K | 持仓公司的重大事项公告（需设置 `FUND_SEC_USER_AGENT` 联系人） | 每一份新公告 |
| `rss_feeds` | 运营者自行添加的 https RSS／Atom 源 | 只进入上下文 |

每条资讯去重保存来源发布时间和首次观测时间。启动后每个来源的第一批只作为基线，不会把积压的旧闻当成新事件；
超过 24 小时的旧闻和未来时间戳不触发。研究员、基金经理和盘前研究都会收到最近 24 小时与持仓、目标证券相关的资讯
以及全部宏观／搜索命中（最多 `max_items_per_context` 条），并标注为**未经核实的第三方文本**，要求重大判断回到原始公告核对。
重大资讯通过 `watch_triggers` 告诉 AI 为什么被提前唤醒，仍受最短复核间隔、失败退避和每日模型预算约束。
AI 只能提供搜索词，不能提供网址；资讯监控不下单。来源失败各自退避，不影响交易。
历史回测不启用实时资讯，研究员仍用带发布时间校验的研究工具。

## 反方审查与组合构建 A/B

**反方审查**（`critic` 段，`FUND_CRITIC`）：基金经理决策前可调用一个只读的 AI 角色（默认用研究员的模型，
可用 `FUND_CRITIC_MODEL` 指定），专门给出研究的最强反面论证：反面证据、已被定价或拥挤的逻辑、估值、事件和流动性风险、
观点之间的相关性、陈旧或无日期的证据。输出按证券列出的疑虑和缺失的证据（`critique`），基金经理必须在理由里回应重要疑虑。
盘中评议使用已保存的预研及其可用性信息，不先重跑研究员；历史回放继续评议该轮已验证日期的研究。
它不做决定、不下单。模型预算不够两次调用（反方＋基金经理）时跳过，调用失败只记录 `critic_unavailable`，都不会挡住决策。
同一交易日内研究、持仓和唤醒原因（`watch_triggers`）都没变时复用上一次的审查（`critic_reused`），不重复调用；任何一项变化都重新审查。
影子 A/B 的两组看到同一份反方审查。

**组合构建 A/B**（`construction` 段，`FUND_CONSTRUCTION_MODE`）：专业基金通常把“选什么”和“各买多少”分开。
每个通过校验的决策都同时记录两种构建方式（`construction_variant`）：
- `ai`：AI 给的权重；
- `risk_budget`：同样的证券和总仓位，按 AI 权重分配**风险贡献**（波动低的证券分到更多权重，波动高的更少），
  用协方差的坐标下降法求解，负相关也成立；没有历史的证券保持 AI 权重。
两种方式用同一套已完成收盘价打分（下一交易日相对 beta 匹配基准的超额收益，每个交易日一对），不需要额外的模型调用。
`auto`（默认）先用 `ai`，至少 `min_pairs`（20）个交易日、配对 t ≥ `switch_t_stat`（2）时切换到风险预算并发 Telegram 通知，
之后证据反转再切回；也可以固定为 `ai` 或 `risk_budget`。无论哪种方式，量化风控的全部上限都照常生效。
决策记分卡（`decisions`）只衡量 AI 的选股，构建方式的比较在 `scorecards.construction`。

## 量化风控原则

借鉴量化基金常用的“预测与组合构建分离”：AI 负责观点和相对配置，`scripts/autonomous_fund/quant.py`
负责把目标投影到风险约束内。全部计算只用调用方提供的**已完成交易日**收盘价（不含当日或未来 K 线），
没有模型、券商或网络依赖。每一步都只缩小权重，所以结果同时满足所有约束；函数末尾还独立复核一次。

| 约束 | 方法 | 默认值（`quant_risk`） |
|---|---|---|
| 波动率估计 | 最近 63 个交易日简单收益的 EWMA 方差（RiskMetrics λ=0.94），年化 | 下限 10% |
| 相关性估计 | 相关性比波动率需要多得多的数据：用最近 252 个交易日、衰减更慢的 EWMA（`correlation_lambda` 0.97，等效样本约 65 天），再向平均相关系数收缩 `correlation_shrinkage`（常相关目标，收缩后仍半正定）。λ=0.94 的等效样本只有约 31 天，用来估 27×27 的相关矩阵噪声太大，几周的异常走势就会让组合波动估计大幅摆动 | 252 天、λ=0.97、收缩 25%；平盘序列与其他证券的相关系数按 1 处理且不参与收缩 |
| 单名风险上限 | 权重 ≤ min(10%, 风险预算 ÷ 年化波动)；个股用 `max_name_risk`，普通 ETF 用 `max_etf_name_risk` | 个股 0.025：波动 25% 的股票上限 10%，波动 50% 的上限 5%；ETF 0.05（依据下文 2019 年真实数据回放校准） |
| 行业／主题组 | 运营者定义、可重叠的组，组内权重和 ≤ 组上限 | 科技 30%、通信 20%、可选消费 20%、金融 30%、医疗 25%、能源 15%、必需消费 25%、大型成长股 35%、宽基指数 40% |
| 回撤分档预算 | 回撤 ≤ 4% 满额；4%–8% 线性降到 25%；≥8% 锁定增仓 | 总敞口预算 = 70% × 系数，超出部分自动卖出 |
| 组合波动率目标 | 事前年化波动 √(wᵀΣw) 超过目标时整体等比例缩小 | 12% |
| 尾部风险 | 1 日 97.5% ES = max(252 日历史模拟 ES, 正态 ES)，超过上限时整体缩小 | 2% 净值；历史样本少于 120 天时只用正态 ES |
| 历史情景压力测试 | 把当前目标权重放进已结束的历史危机窗口，按买入持有计算整段亏损（不计止损和回撤保护，偏保守）；最差情景超过上限时整体缩小。某证券在窗口内没有完整历史（如当时尚未上市）时，用基准跌幅 × max(1, beta) 代替，完全没有历史的用 60% 波动对应的倍数，并列在 `scenario_proxied`；基准本身缺历史的情景跳过并列在 `scenarios_unavailable`。窗口只在结束之后才使用，所以历史回测不会“预知”之后的危机。每只证券的长历史只下载一次，存入账本表 `fund_scenario_returns`（下载失败或返回空不记录，次日重试；“窗口内没有历史”的结论 30 天后复查一次） | `max_scenario_loss` 35%；`stress_scenarios`：2008-09-01～2009-03-09（金融危机）、2020-02-19～2020-03-23（新冠暴跌）、2022-01-03～2022-10-12（加息熊市），可在 `quant_risk.stress_scenarios` 改写，最多 10 个 |
| 数据缺失 | 历史不足 20 个交易日的证券不能加仓，只能持有或减持，并按 60% 波动、完全相关计入组合风险 | 不填充、不插值 |
| 单日亏损闸门 | 相对上一交易日最后观测净值亏损 ≥ 2% 时，当日停止买入，次日自动恢复 | 卖出不受影响 |
| 市场压力 | SPY 上一交易日或盘中跌幅 ≥ 3%，或 SPY EWMA 年化波动 ≥ 35%；基准历史缺失按压力处理 | 压力下的买入需要审批 |

构建结果、绑定的约束（情景压力为 `scenario_stress`）、事前波动、ES 和各情景亏损写入 `risk_overlay` 事件，报告的 `latest_risk_overlay` 可查看。
基金经理的风险上下文里有 `stress_scenarios`：各情景窗口、基准跌幅和各证券在窗口内的涨跌，便于直接给出不超限的组合。
基金经理在每次决策前收到风险上下文：各证券波动估计、单名上限、分组、当前回撤和预算、单日亏损、
今日已用额度和市场压力状态，以便直接给出可行的组合，减少被缩小或被拒绝的情况。
这些估计基于历史数据，不能预测跳空、停牌或从未出现过的相关性变化；ES 和波动目标约束的是事前估计，
不是亏损上限保证。修改 `quant_risk` 后重启生效，摘要变化会让旧目标失效。

## 事件风险与宏观环境

**财报前降仓**（`events` 段，`scripts/autonomous_fund/events.py`）：止损挡不住隔夜跳空，而财报是最常见的原因。
个股在财报日当天或下一个交易日（`earnings_window_days`，默认 1 个工作日）之前，权重上限降到 `earnings_max_position`
（默认 5%），超出部分按普通减仓自动卖出；财报日过后上限恢复，AI 可以再买回。ETF 不受影响。
日历来源：设置了 `FINNHUB_API_KEY` 时用 Finnhub，否则用 Yahoo 日历（非官方，每次最多等 10 秒）；每只股票每天只查一次，在开盘前研究时取好，存入账本表 `fund_earnings`。
拿不到日期时不猜测：该股票维持正常上限，并列在风险信息的 `event_risk.unknown`。只在 local／paper 模式生效，
因为历史回测需要“当时已知的日历”，而免费接口不提供。绑定的约束记为 `earnings:<代码>`。

**宏观环境**（`macro` 段，`scripts/autonomous_fund/macro.py`）：每个交易日从 FRED 取 3 月／2 年／10 年美债收益率、
10y−2y 利差、高收益债利差（ICE BofA）和 VIX，给出最新值、20 个观测的变化、一年分位数，以及收益率曲线倒挂、
信用压力（高收益债利差 ≥ 6 个百分点或 20 个观测内上升 ≥ 1 个百分点）、波动偏高（VIX ≥ 25）三个标记和现金停放的国债收益率。
这些是市场数据，FRED 不修订，所以只保留早于当天的观测在历史回测中也没有前视。有 `FRED_API_KEY` 用官方 API，
否则用公开 CSV。它只是给 AI 的参考信息，不是交易规则；某个序列取不到时只标为不可用。

## 现金管理：闲置现金自动买短期国债 ETF

总敞口上限 70% 意味着至少 30% 的资金是现金，AI 选择空仓时更多。专业基金不会让这部分钱零收益地闲着。
`cash_sweep` 段（`.env` 的 `FUND_CASH_SWEEP`）把闲置现金停在运营者认可的一只现金等价 ETF 上（默认 `SGOV`，
0–3 个月美国国债），实现于 `scripts/autonomous_fund/cash.py`：

- 投资范围里把它标为 `"cash_equivalent"`（最多一只）。它**不是 AI 的投资目标**：AI 不能把它写进目标组合，
  账户快照里它的市值直接计入 `account.cash`（另列在 `cash_equivalents`）。所以总敞口、单日风险增量额度、
  审批、每日买入额度、回撤和单日亏损的计算都与停放前完全一样。
- AI 的目标组合配置完成后，真实现金超出 1%（`keep_cash`）的部分若超过净值的 2%（`band`），自动买入 SGOV。
- AI 要买入证券而真实现金不够时，先卖出刚好够用的 SGOV（加上 1% 现金缓冲），成交、新快照确认后，
  买入再走一遍原有的全部风控和审批检查。
- 停放买卖是普通限价单，走同一条提交路径（STOP、独立监控、交易时段、同一时间只有一个未结订单、每日提交上限）。
- 局限：每次买入前多一笔卖出，占用卖出额度；配置完成后才停放，盘中止损卖出的现金要等下一次决策完成后才会停放；
  SGOV 本身有极小的价格波动和每月分红，模拟盘的分红按本地撮合规则入账。设 `FUND_CASH_SWEEP=false` 可关闭，
  `FUND_EXCLUDE_SYMBOLS=SGOV` 也会关闭它。

出厂投资范围同时加入了 `IEF`（7–10 年美债）、`TLT`（20 年以上美债）和 `GLD`（黄金），让组合在股市下跌时有分散的选择，
而不是只能减仓；`treasury_duration` 组限制两只美债合计不超过 30%（在默认单只 10% 的上限下不会触发）。

## 重大交易人工审批（例外审批）

原则：**硬上限由代码执行，审批不能放宽；降风险永不等待；只有超出自动风险增量额度的加仓才需要人。**

- **卖出／减仓**（本账户只做多，卖出一定降低敞口）：始终自动执行，包括清仓和回撤降仓。
- **自动买入额度**（`approval` 段）：以纽约交易日为单位，总敞口相对开盘可提高 `auto_daily_exposure_increase`
  （默认 25%），且当日观测到的买入现金流不超过净值的 `auto_daily_buy_turnover`（默认 40%）。
  额度内的买入直接执行；额度不足时只执行能放进额度的部分。
- **需要审批的例外**：超出上述额度的买入，或任何处于市场压力状态下的买入。
  此时生成**一份覆盖整个调仓计划**的审批请求，而不是逐笔审批。
- **硬性额度**：即使批准，当日敞口增幅也不超过 `max_daily_exposure_increase`（默认 50%），
  买入现金流不超过 `max_daily_buy_turnover`（默认 100%），并且仍受全部量化约束和硬上限限制。

举例：全部现金建仓到 70% 时，第一天自动建到约 25% 敞口，第二天、第三天继续自动建仓，无需任何人工；
如希望更快，批准一次即可在当天建到 50%。只有市场大跌或高波动时的买入、或单日大幅加仓才会打扰你。

审批请求列出：触发原因、拟买入证券及各自的权重上限和限价上界（参考限价 ±`price_collar`，默认 2%）、
批准后最高敞口、当日买入现金流上限、构建后组合的波动和 ES、市场压力详情、AI 的完整理由，
有效期不超过 `valid_minutes`（默认 60 分钟）、目标有效期或本交易日收盘。
同一计划内价格在领口内变化、重复轮询都复用原请求；出现新证券、更高权重、新触发原因或价格超出领口才生成新请求。
一份批准可覆盖该计划内的多笔买入，每次使用都在预留同一事务内复核范围并记录 `approval_used`。
替换目标、改变风险策略或过期都会使旧批准失效。

查看后再复制准确的请求 ID 和 digest；命令只记录批准，交易进程在下一迭代重新风控后才提交：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode approvals -RunDir artifacts/fund-local
Get-Content artifacts/fund-local/approvals.json -Encoding UTF8
.\scripts\run_autonomous_fund.ps1 -Mode approve -RunDir artifacts/fund-local -ApprovalId <request-id> -ApprovalDigest <reviewed-digest>
.\scripts\run_autonomous_fund.ps1 -Mode reject -RunDir artifacts/fund-local -ApprovalId <request-id> -ApprovalDigest <reviewed-digest>
```

不批准则超额部分不下单，额度内的部分不受影响。拒绝会停止该目标，等下一次正常复核；已经提交的订单须通过撤单流程处理。
STOP 激活时不能批准开启交易，但可以拒绝未使用的批准。历史实验不假设人工同意，超额买入仍会挂起。
审批命令可以在运行进程持锁期间使用，不需要模型或券商 Key。

**手机审批**：独立监控进程把审批请求发到 Telegram，你直接**回复这条消息「同意」或「拒绝」**即可；
也可发送「同意 代码」／「拒绝 代码」（提醒里的 6 位审批代码），只有一个待审请求时直接发「同意」也行。
机器人会回复处理结果；交易进程在下一次迭代重新通过全部风控后才执行。安全约束：
只接受 `FUND_TELEGRAM_CHAT_ID` 这个会话；私聊默认由聊天本人审批，群组必须用 `FUND_TELEGRAM_APPROVER_IDS` 列出审批人；
早于请求创建时间的消息、STOP 期间、过期或已被替换的请求一律拒绝；批准与 CLI 完全相同地绑定摘要和范围，不放宽任何硬上限；
每条指令记入账本（`telegram_command`，授权人 `telegram:<用户 ID>`）。发送「状态」可查看监控、待审批数和分仓净值。
控制了你的 Telegram 账号就等于控制了审批，请开启 Telegram 两步验证；设 `FUND_TELEGRAM_APPROVALS=off` 可退回只通知。
希望所有买入都人工确认的运营者可把 `auto_daily_exposure_increase` 设为 `"0"`。

## 离线验证：用真实历史数据回放风控

`validate-risk` 把固定的 AI 目标权重逐日送入同一套量化风控，在真实收盘价上回放，比较“未风控持有”与“风控后”：

```bash
python -m scripts.autonomous_fund.run validate-risk --run-dir artifacts/validation --data-dir data --targets SPY=0.1,TLT=0.1,GLD=0.1
```

回放无前视：第 t 天收盘时只用到 t 天及以前的收盘价决定权重，赚取 t→t+1 的收益；缺数据的日子跳过，不补数；
按换手收取 10 基点成本；结果写入 `validation_risk.json`（含逐日敞口、约束和事前波动）。`--data-dir` 下每个证券一个 CSV，
支持 Yahoo 导出（Date/Close）或分钟线（time/close，自动取每日最后收盘）。

用仓库自带的真实数据（`data/`：2019 年 SPY/TLT/GLD/IEF/DJP 日线；`data/XYZ_1Min.csv`：美国指数期货 2020-01 至 2020-04 分钟线，含新冠暴跌）得到（2026-11-02 改用新的相关性估计后重跑，三组结果不变：前两组只有一个证券，不涉及相关性；2019 年没有约束触发。离线回放不含情景压力测试）：

| 场景 | 未风控 | 风控后 |
|---|---|---|
| 2020 暴跌，70% 单一指数，只开波动率目标／ES／回撤分档 | 收益 −8.5%，最大回撤 25.0%，年化波动 45.4% | 收益 −6.2%，回撤 12.2%，年化波动 13.4%（目标 12%，暴跌初期 EWMA 有滞后） |
| 2020 暴跌，同上，全部默认（含单名风险预算） | 同上 | 收益 −3.1%，回撤 6.3%，年化波动 7.4% |
| 2019 平稳，5 只 ETF 各 10%（出厂配置） | 收益 5.34%，回撤 1.25% | 收益 5.23%，回撤 1.26%，没有任何约束触发，成本仅为再平衡费用 |
| 2019 平稳，SPY 25%/TLT 15%/GLD 15%/IEF 10%/DJP 5%，按个股预算 | 收益 9.56% | 收益 7.57%，回撤反而略高：单名预算把 SPY 压低了 181 天 |
| 同上，按 ETF 预算（现在的默认） | 收益 9.56% | 收益 9.39%，回撤与波动相同 |

结论：波动率目标和回撤分档在真实暴跌中把回撤减半以上；平稳市场几乎没有成本。回放还发现单名风险预算对宽基 ETF 过严，
因此新增 `max_etf_name_risk`（默认 5%），个股仍为 2.5%。这些都是机制验证，不含 AI 的选股、盘中止损或日内跳空，
2019 一年和 2020 一个季度的样本也不能代表所有市场环境。这些数字由 `tests/test_autonomous_fund_validation.py` 固定为回归测试。

## 影子 A/B：验证 AI 的自我优化是否有效

每天的复盘备忘和记分卡会改变 AI 之后的决策，但“改了”不等于“改好了”。`validation` 段开启影子 A/B：

- 每个交易日的第一次决策后，基金经理基于**同一份研究**再做一次对照决策（`shadows_per_session`，默认 1 次）：
  一组带复盘备忘与记分卡（memo 组），一组不带（baseline 组）。只有当前“冠军”组的决策会交易，另一组只记录（`shadow_decision`）。
  两组看到的市场、研究、风控和约束完全相同，也都看不到自己属于哪一组；对照组失败不会影响交易或触发退避。
- 每次盘后复盘之后，用已完成收盘价给两组决策打分：从决策当日收盘起持有一个交易日、相对 beta 匹配的 SPY 的超额收益，
  逐日配对求差（memo − baseline），计算均值、t 统计量和胜率（`learning_evaluation`）。
- 至少 `min_pairs`（默认 20）个交易日、且 |t| ≥ `switch_t_stat`（默认 2）时才切换冠军：memo 组显著更差就改由 baseline 组交易并通知你；
  之后若 memo 组显著更好再切回。样本不足或不显著时维持现状。
- 成本：每个交易日多一次基金经理调用，计入每日模型预算；预算不足时跳过对照组，不影响交易。

局限：日度配对差可能有自相关，20 个样本的 t 检验只是谨慎的门槛，不是严格的因果证明；
对照组仍然是同一个模型，只是去掉了学到的经验，所以它检验的是“复盘学习这一环”是否有价值，而不是模型本身的选股能力。

## 运行验收报告

本地或 Paper 运行一段时间后，用 `acceptance` 从账本生成验收报告（`acceptance.json` 和 `acceptance.md`），作为进入下一阶段的客观依据：

```bash
python -m scripts.autonomous_fund.run acceptance --run-dir artifacts/fund-local
```

| 标准（`validation.acceptance`） | 默认阈值 | 含义 |
|---|---|---|
| `min_sessions` | 20 | 有账户观测的已结束交易日数 |
| `min_iteration_ratio` | 0.9 | 各交易日实际完成的迭代数 ÷ 应完成数的中位数（反映掉线、卡住） |
| `max_watchdog_stops` | 3 | 独立监控或服务监督写 STOP 的次数 |
| `max_unresolved_intents` | 0 | 仍未结束的委托预留（含无法确认的提交） |
| `max_fill_details_unavailable` | 0 | 券商只给累计成交、缺明细的次数 |
| `max_model_failure_rate` | 0.2 | 模型调用失败比例 |
| `min_review_ratio` | 0.9 | 完成盘后复盘的交易日比例 |
| `max_mean_slippage_bps` | 25 | 成交价相对首次提交限价的平均不利滑点（基点） |
| `max_bot_crashes` | 0 | 绕过所有保护导致进程退出的崩溃（`bot_crash`）；记账错误和自动恢复次数只作为参考数据列出 |

报告还列出人工干预次数（审批、复位、补单、出入金、Telegram 指令）、保护性卖单、改价撤单、追价封锁、回撤与单日亏损闸门、
最新的影子 A/B 结论等。它只证明运行层面的可靠性，不证明盈利；本地虚拟账户也不能代替券商 Paper 的证据。

**业绩证据：是能力还是运气**（`performance.py`，报告的 `performance_evidence`）：只用账本里每个交易日最后一次净值和
同时记录的 SPY 价格（跨越出入金调整的那一天不计收益，缺数据不补）：
- 概率夏普比 PSR：考虑样本长度、偏度和峰度后，真实夏普比大于 0 的概率；
- 去试错夏普比 DSR：同上，但门槛换成“试过 N 种配置时单靠运气能得到的最好夏普比”，N = 实际交易过的提示词版本数 × 风控配置数；
- 最短记录期 MinTRL：按目前的夏普比，要多少个交易日才能有 95% 的把握说它大于 0；
- 相对 SPY 的回归：beta、年化 alpha 和 alpha 的 t 值。

**分阶段资金就绪度**（`capital_stage`）：只有在运行验收通过、至少 60 个交易日且不少于 MinTRL、PSR ≥ 0.95、DSR ≥ 0.90、
alpha t ≥ 1.65（单边 95%）、且没有触发回撤上限时，才给出 `eligible_stage_1`（最多投入计划资金的 10%）；否则为 `paper_only`
并列出每一条不满足的原因。25% 和 50% 两个阶段需要实盘记录满足同样的检验。**这只是报告**：本应用没有实盘模式，
任何真金白银的一步都需要你明确授权和单独审核的执行路径。

## 研究网络与数据边界

研究员的原生网页工具会拒绝解析到私有、回环或保留地址的网站（防 SSRF）。OpenClash 等代理的 Fake-IP 模式会让所有域名解析到
`198.18.0.0/15`，这类研究请求因此全部失败。`check` 命令现在会报告 `research_network`：
逐个解析 SEC、Yahoo、Reuters、美联储等研究站点，标出 `fake_ip_dns` 或 `private_answer` 并给出处理建议。

可在 `config.json` 设置 `"research_network": {"resolver": "doh"}`：网页工具改用 DNS-over-HTTPS（默认 `https://1.1.1.1/dns-query`）
获取公开地址，代理仍按规则转发连接；SSRF 检查保持不变，解析到私有地址仍会被拒绝。更稳妥的做法仍是让服务器不走 Fake-IP。

研究用 DuckDB 仍禁止访问账本、配置和密钥，但现在可以读取研究工具自己的下载目录（`cache/browser`、`cache/research`），
下载的 CSV／Parquet 可以直接用 SQL 分析。

## 模型调用预算与失败退避

`model_budget` 段约束推理成本，避免“失败 → 下一分钟重跑”的付费循环：

- 本地／券商模拟盘中，只有基金经理调用或决策验证失败（超时、结构化回答无效、决策过期或超限）后，
  按 `failure_backoff_minutes`（默认 5、15、60 分钟，之后保持 60 分钟）延后下一次复核，成功后清零；
  连续失败次数、失败角色与下次时间持久化，重启不会绕过经理退避。
  研究员的盘前异常只记录／通知，不阻止盘中经理评估。升级时根据匹配的失败事件迁移旧版仅研究员失败的共享退避，
  不清空 STOP、预算、订单、审批或已接受目标；归属不明的旧退避保持不变。
  历史回放保留先研究、验证日期证据、再经理决策的流程及其失败退避，不能把历史研究缺失当成已验证证据。
- 每个纽约交易日最多 `max_agent_runs_per_day`（默认 60）次 Agent 运行，其中 `reserved_review_runs`（默认 4）
  留给盘前研究与盘后复盘。日内额度用完后不再发起新决策，已接受的目标继续执行，次日自动恢复，
  并发送一次 `model_budget_exhausted` 通知。
  盘中一次基础评估只需预留一次经理调用；盘前、盘后及可选评议／影子评估继续计入实际调用预算。
- AI 指定的 `next_review_at` 不早于 `min_review_minutes`（默认 15 分钟）；目标执行窗口也按此下限计算。
- **跳过无变化的定时复核**（`skip_quiet_reviews`，默认 `true`，`FUND_SKIP_QUIET_REVIEWS`）：到了 AI 自己定的复核时间，
  只有同时满足以下条件才不调用模型（记录 `review_skipped`，`min_review_minutes` 后再检查）：没有待处理的盯盘唤醒
  （提醒、大幅波动、市场压力、新闻命中）；没有暂停加仓；上次模型决策在同一纽约交易日、距今不到 `max_quiet_minutes`
  （默认 120 分钟，`FUND_MAX_QUIET_MINUTES`，取值 `min_review_minutes`..1440）；该决策的调仓已完成（`allocation_complete`），
  且持仓与完成时完全一致（现金管理 ETF 视为现金）；已保存的盘前研究仍是它看到的那一份。任何一条不满足就照常调用基金经理。
  已设置的止损、止盈和价格提醒在跳过期间照常生效；提示词告诉基金经理这一规则，让它用提醒和 `news_watch` 安排需要更早唤醒的情况。
- **行情快照**（`market_snapshot`）：每个交易日由代码从已完成日线计算一次可交易标的的 1/5/20/60 日收益、20/60 日年化波动、
  相对压力基准（默认 SPY）的 60 日 beta 和相关性、相对 50/200 日均线的位置、距 252 日最高收盘的回撤和 20 日日均成交额，
  放进盘前研究和盘中决策的上下文，研究员和基金经理不必再用工具重复计算。只用已完成的交易日（历史回放同样不看未来），
  缺数据为 `null`，从不填补；预算用完时尚未读取的标的列在 `deferred`，下一轮补齐。
- **研究数据预取**：每个工作日美东 07:15 的定时任务（`run prefetch`）把当天的日线、FRED、财报日历和 SEC 基本面先存到
  `<账户目录>/cache/daily/<交易日>/`；交易进程先读这里，缺的才联网补并写回。见 [Ubuntu 部署](AUTONOMOUS_FUND_UBUNTU.md)。

## 停止、恢复与异常通知

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode stop -RunDir artifacts/fund-paper
.\scripts\run_autonomous_fund.ps1 -Mode resume -RunDir artifacts/fund-paper
```

`stop` 创建 STOP 文件（不带目录时包括所有正在运行的账户，并打印停止了哪些目录）；运行进程下一次迭代停止新单并尝试取消本应用已知未完成订单。
每种停止原因（谁、为什么）在 6 小时内通知一次（`stop_active`），服务反复重启或原因来回切换不会刷屏。
不要把创建文件当成券商已确认撤单。模型或网络调用进行中可能延迟处理；紧急时同时去券商界面核验并撤单。
Ctrl+C 可以停止进程，但不能保证撤回已经被券商接受的订单。
本地模拟盘的休市等待可被原生关闭钩子唤醒，Ctrl+C 后会退出等待并释放进程锁；
正在进行的模型或数据请求仍可能需要结束后才能退出。`-Once` 结束也会关闭撮合连接，后续撮合需重启同一账本。
若提示 `already in use`／`runner.lock could not be acquired`，说明另一个实例仍持有操作系统锁。
先结束原来的进程并等待退出，再使用原目录重启；不要删除锁文件、账本或更换目录来绕过互斥。
升级前已在休市长时间等待的旧进程不会自动获得修复，必要时由操作员核验命令行、项目路径与运行目录后，仅结束那个实例。
`resume` 只移除人工停止，不清除回撤锁或不确定订单。
回撤在 4%–8% 之间时由量化风控自动降低敞口预算并在回升后自动恢复，不需要人工；只有 8% 硬锁需要人工复位。
本地模式将上述命令中的目录替换为 `artifacts/fund-local`；轮询器也在休市时处理本地 STOP 和到期撤单。
回撤暂停需要操作员停掉进程后显式执行 `reset-drawdown`；新高水位从下一次净值开始，操作会留痕。

**可选数据准备不会卡住交易迭代**：独立监控在开盘期间如果 180 秒内既没有迭代开始／结束、也没有模型调用事件，就判定
“迭代卡住”并写 STOP。因子研究的历史数据（含研究股票池）、SEC 财报、财报日历、FRED 宏观数据和历史危机窗口的长历史都是
可选数据：在 local／paper 的每轮交易迭代里最多占用 `PREPARATION_BUDGET_SECONDS`（45 秒），没下载完的下一轮接着做，
已下载的不会重复下载（日线按天缓存在内存，SEC 摘录缓存在磁盘）。空闲迭代和 STOP 期间也会利用剩余时间继续准备，
恢复交易时数据已经就绪。准备期间因子实验室显示 `warming`（只给出已验证因子，不给排名，排名不完整的横截面会扭曲 IC）；
财报日历未查到的股票列在 `event_risk.deferred`，这段时间不降仓；FRED 未取到的序列标为 `deferred`；
危机窗口未取到历史的证券按基准跌幅 × max(1, beta) 悲观代替，基准本身总是先取。风控必需的数据（可交易证券的日线）仍然同步获取。
开盘前研究和收盘后复盘不受这个时间限制，会一次准备完整；历史回测也不受限制，结果不依赖机器快慢。
这是 2026-11-02 服务器事故的修复：午间更新重启后，第一次决策同步下载了这些数据，超过 180 秒，被判定卡住；
每次重启内存缓存清空又重复一遍，直到 24 小时内 3 次自动恢复用完（见 `docs/investigations/2026-11-03_FUND_ITERATION_STALL.md`）。

**进程稳定性**：LumiBot 在实时模式下，只要有异常逃出生命周期钩子（每轮迭代、开盘前、收盘后），就会调用
`on_bot_crash` 并结束整个策略。本应用的处理：

- 每轮迭代的业务逻辑（读账户、盯盘、调用模型、下单）出错，只记录 `blocked` 通知，下一轮照常运行；
- 账本导出、报告重建、迭代记录等**可重建的记账步骤**失败（数据库暂时被锁、磁盘写入失败、报告数据异常），
  记录 `bookkeeping_error` 通知并在下一轮重试，不会结束进程。如果账本真的无法写入，所有订单预留也会被拒绝，
  继续运行不会增加风险，而进程退出反而会停止盯止损；
- 盘前研究、盘后复盘、因子检验、学习评估各自出错都只发通知，不影响交易进程；
- 新闻轮询、Yahoo 实时推送在后台线程中逐轮捕获错误并自动重连；Paper 模式的心跳线程单次写入失败不会退出；
- 监控进程逐条处理 Telegram 回复：某条回复处理出错只记录 `telegram_command` 错误，不会让监控退出或反复读同一条；
- 模型调用很长（研究加决策超过 3 分钟）但仍在推进的迭代，不会被误判为“迭代卡死”；
- STOP 期间仍会核对券商的订单状态，已撤销或已成交的订单会被标记结束，自动恢复因此不会一直被挡住；撤单请求同样限频；
- 若仍有异常让进程崩溃，`on_bot_crash` 先写入 `bot_crash` 通知，独立监控进程据此（以及心跳超时）通过 Telegram 告警；
- 历史回测中这些错误仍直接抛出，以免测试掩盖真正的 bug。

**进程退出后的自动重启与自动恢复**：

- Ubuntu：两个 systemd 服务（交易、监控）退出后 30 秒自动重启；
- Windows 或没有 systemd 的机器：用 `serve` 代替直接运行，它同时管理交易和监控两个进程，任一退出 30 秒后重启：

  ```powershell
  .\scripts\run_autonomous_fund.ps1 -Mode serve
  ```

  与服务器一样要求在 `.env` 配好 Telegram。开机自动启动可在“任务计划程序”中创建登录时运行的任务，
  例如 `schtasks /Create /TN "LumiBot Fund" /SC ONLOGON /TR "powershell -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\run_autonomous_fund.ps1 -Mode serve"`；
  长期运行请关闭电脑睡眠（`powercfg /change standby-timeout-ac 0`），睡眠期间进程冻结，心跳超时会触发 STOP。
  Windows 下的 `serve` 只在模拟测试中验证过，尚未在真实 Windows 机器上运行。

每次（重新）启动都会先写入 STOP，避免无人监控的成交。**STOP 文件记录原因**，分三级，后写入的不能降级：

| 级别 | 来源 | 自动解除 |
|---|---|---|
| 人工 | `stop` 命令；**手动关闭**服务（`systemctl stop`／`restart`、在 `serve` 窗口按 Ctrl+C）；首次安装、**全新账户的第一次启动**（包括切换 `FUND_MODE`／`FUND_RUN_DIR` 后）、恢复的备份、旧版本留下的空 STOP | 永不，必须 `resume` |
| 监控 | 看门狗或服务监督发现故障 | 仅进程级故障（心跳超时、进程退出或卡死、监控暂时不可用）；磁盘不足、版本不符、撮合错误、未配置通知不解除，且之后出现的进程级故障不能覆盖这些需要人工处理的原因 |
| 服务 | **异常关闭**：进程自己退出（崩溃）、被系统杀死、断电或死机后重新启动、操作系统关机或重启 | 是 |

自动恢复由独立监控进程执行（`config.json` 的 `service` 段，`.env` 中 `FUND_AUTO_RESUME=false` 可关闭），必须同时满足：
交易进程连续健康至少 `auto_resume_after_seconds`（默认 180 秒，故障后从恢复时重新计时）；没有状态不明或未结束的订单预留
（交易进程在 STOP 期间会撤销本应用的未完成订单）；24 小时内自动恢复不超过 `max_auto_resumes_per_day` 次（默认 3）。
恢复、被挡住（`auto_resume_blocked`）或次数用完（`auto_resume_exhausted`）都会通过 Telegram 通知。
手动关闭和异常关闭的区分：服务收到停止信号时，若操作系统正在关机或重启（systemd 报告 `stopping`）视为异常，开机后自动恢复；
其他停止信号（你执行 `systemctl stop`／`restart` 或按 Ctrl+C）视为手动关闭，下次启动保持 STOP，核对后 `resume`，
修改 `.env` 后重启服务同样需要 `resume`。无法判断时按手动处理。直接关闭窗口或在任务管理器中结束进程，程序来不及记录，按异常处理。

`notify(kind, details)` 是通知扩展接口，默认输出控制台及持久化本地通知 outbox。
同一状况（同类型、同内容）在 30 分钟内只记录一次，STOP 提示为 6 小时，避免每分钟写入相同告警；
`report.json` 的 `blocked_iterations` 因此统计的是被记录的阻断通知数，而不是阻断的分钟数。
`notifications.json` 与 SQLite 通知事件是权威 outbox。可用独立进程接入 Telegram；
先在自己的 Telegram 客户端通过 BotFather 创建机器人并向其发送 `/start`，准备明确的 Chat ID。
密钥只放本地；第二个 PowerShell 窗口安全输入：

```powershell
$fundTelegramToken = Read-Host 'Telegram Bot Token' -AsSecureString
$env:FUND_TELEGRAM_BOT_TOKEN = [System.Net.NetworkCredential]::new('', $fundTelegramToken).Password
Remove-Variable fundTelegramToken
$env:FUND_TELEGRAM_CHAT_ID = Read-Host 'Telegram Chat ID'
.\scripts\run_autonomous_fund.ps1 -Mode notify-test -RunDir artifacts/fund-local
.\scripts\run_autonomous_fund.ps1 -Mode monitor -RunDir artifacts/fund-local
```

Telegram 持久化发送状态、重试和限流等待；相同告警在 30 分钟内抑制重复，未变化时保持安静。
`notify-test` 只向所选账户的通知收件人发送一条测试消息，不修改 STOP、不执行订单；
显示 `sent` 表示 Telegram API 已确认，仍须在自己的客户端确认收到。健康恢复后的新一轮故障会再次通知，
不会被重复告警抑制；限流等待对新旧消息全局生效。
发送确认丢失仍可能造成重复通知，事件序号用于辨认；不能保证恰好一次送达。
没有 Telegram 配置时可用 `-Mode monitor -ConsoleOnly`，只有本地保护，不代表手机已收到。
监控第一次启动忽略超过一小时的旧告警；请同时查看现有审批、回撤和订单状态。

独立监控默认检测 180 秒心跳失效、开盘后迭代停滞、900 秒模型调用未结束，以及旧执行器版本。
发现异常就写 STOP 并通知，旧状态未恢复时持续重申 STOP，不能只靠人工删除文件继续交易。
恢复后也不自动解除 STOP；核验订单后由操作员显式 `resume`。
本地模式使用撮合心跳；Alpaca Paper 使用独立运行心跳，原生迭代／模型开始结束事件提供额外进度证据。
监控必须在另一进程运行；本机断电、系统整体挂起、断网或 Telegram 故障仍会影响送达。
写 STOP 不等于券商确认撤单，紧急时仍须核对券商订单。未运行监控时，主程序保留本地告警，但没有独立保护。

## 自动盘后复盘与策略反馈

正常运行的进程在原生 `after_market_closes` 钩子中自动复盘：

1. 先核对订单并尝试记录账户；缺少收盘报价或账户接口失败时，保留真实最后观测时间并告警。
2. Flash 研究员读取当日已有研究、目标持仓、订单、成交、现金和持仓变化，结合只读市场研究整理证据。
3. V4 Pro 基金经理评估选股、择时与执行，输出中文复盘、保留教训、撤回假设、数据缺项和下一交易日策略备忘。
4. 成功完成的备忘按版本持久化；下一轮盘前研究、正常时段研究和基金经理决策都收到 `strategy_memory`。
   后续复盘重新检查前一版本假设，决定保留、修正或撤回；没有证据时允许不调整。

选股与择时调整均包含假设、依据、证据引用、验证条件和失效条件。
这是 AI 在后续决策中利用可追溯经验，不是重新训练模型或自动改写 Python，也不保证每天收益改善。
完整目标组合仍须由正常时段的新决策产生，再通过全部独立风控和新鲜行情校验。
盘后结果 schema 不接受 `targets` 或额外权限／风控字段；研究员与基金经理始终只有只读工具。
收益、回撤、每天订单数、投资范围、实盘入口都不由复盘修改。

按 NYSE 交易日识别休市、提前收盘和纽约日期。每个有本账户观测的已结束交易日最多完成一次复盘。
正常结束、重启启动和下一次盘前钩子都会检查遗漏的最近一个已结束交易日；
不会虚构账户启动前的交易历史，也不会在周末凭空生成交易日。离线跨过多日时只补最近有记录的一天，
没有自动补齐所有历史日期或全天候后台任务。需要保持交易进程运行，关闭进程期间不调用模型。

Flash 成功结果先提交 SQLite；Pro 失败后，下次上述检查复用该阶段，保留原始时间、模型和 Mock 来源。
完成的复盘不会因重复回调、重启或重新导出而再次调用模型。模型失败会写入 `postmarket_review_error` 通知，
旧备忘保持有效，未完成的复盘不激活。外部调用成功、落盘前崩溃的极短窗口仍可能重复付费，不能声称模型请求恰好一次。
STOP 和只读观察模式不触发新复盘。回撤暂停只限制新增风险，不妨碍只读复盘。

回测的日志、复盘完成时间和收盘时间都按策略时钟过滤，未来完成的备忘不能传给过去的决策。
原生 Pandas 日线快路径不调用收盘钩子，因此在下一个日期行、当日决策之前补上一已结束交易日的复盘，
不会把时钟提前到未来收盘。用户结束时间前尚未结束的最后一日不补造盘后结果。
原生回测结束清理若越过用户结束时间，不额外复盘。本地 Yahoo 日线仍保守排除当日 bar，
研究可核对已完成分钟线；缺少收盘净值、新闻、费用或完整样本必须披露，不能伪造。
观察盈亏包含未实现部分，可能只是日内最后观测，并非保证的收盘净值或已实现收益。
基准只有观测时间吻合才计算，未对齐则为 null。
每轮上下文最多带入 120 条、120,000 字符的相关事件，并披露省略数量；完整记录仍保存在 SQLite。
策略备忘有长度上限，并附最近三次复盘摘要，防止运行越久上下文无限增长。

旧进程需要先 Ctrl+C 并等待退出，再在原终端用原目录启动，保留已有模型 Key 和虚拟账户：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode local -RunDir artifacts/fund-local
Get-Content -LiteralPath artifacts/fund-local/postmarket_review.md -Encoding UTF8
Get-Content -LiteralPath artifacts/fund-local/strategy_memory.json -Encoding UTF8
```

第一次完成盘后复盘后才会生成上述两个文件；日期归档位于 `reviews/YYYY-MM-DD.md` 和 `.json`。
正常 `report` 命令可以在运行期间只读查看 `latest_postmarket_review`、`strategy_memory_version` 和模型 usage。
模型仍只需要已配置的 `DEEPSEEK_API_KEY`，不增加券商 Key、模型 Key 或后台服务。
每个成功复盘通常增加一轮 Flash 和一轮 Pro Agent 运行；每轮可能含多个工具调用或提供方重试，使用量按实际模型记录。
盘后调用失败且没有返回 usage 时也记录一次未知用量，费用估算保持 null，不把可能已经计费的失败视为零成本。

## 复盘调整的后续评估与对照回放

`strategy_evaluations.json` 自动跟踪每版备忘之后的实际账户观测，分别显示 5／20 个观测交易日的结果。
没有足够交易日就显示 `insufficient_observations`；不填充休市或缺失数据，不把日内最后观测包装成收盘净值。
该结果与后续研究、决策和复盘共享。组合同时受到后续多次决策影响，不能把这份跟踪当成某版备忘的因果效果。

可显式进行一次原生对照实验：两次回放使用相同冻结 CSV 行情、资金、费用、投资范围、风控和模型设置；
基线不带备忘，候选仅带选定的冻结备忘，期间不激活新的滚动备忘。

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode qualify -RunDir artifacts/fund-qualification -DataDir <frozen-history-directory> -MemoryFile <earlier-postmarket-review.json> -Start <later-start-date> -End <end-date>
```

CSV 须覆盖完整投资范围及 SPY／QQQ，遵循下面的历史数据格式。备忘的完成时间和所评交易日收盘必须不晚于起点，
输入 CSV 在两次回放前后核对 SHA-256，发生变化就拒绝输出可比结论。每天的历史数据仍由原生引擎按模拟时钟提供。
`-Scripted` 可无付费模型调用检查流程，不能验证投资判断；不带此选项会产生真实模型调用费用。
审批规则同样生效，实验不会假设人工批准重大交易，也不会向运行中的模拟账户发单。

输出 `qualification.json` 和 `baseline/`、`candidate/` 的独立成交／净值／用量记录。
少于 20 个日采样标为 `insufficient_sample`；样本足够时仅报告该窗口收益／回撤是否改善。
单次对照含模型随机性、样本选择和训练知识污染风险，不证明未来盈利；不会自动推广候选或改变运行权限。
更严格的多窗口、独立保留样本和统计检验仍需独立研究，不能把这个功能当成已认证的策略优化器。

## 历史回测与记录

Yahoo 真实历史行情、脚本决策替身，无模型费用：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode backtest -Scripted -RunDir artifacts/fund-history -Start 2025-01-06 -End 2025-02-01
```

使用真实模型时去掉 `-Scripted` 并配置模型凭证。历史数据从原生 YahooDataBacktesting 读取。
也可指定 `-DataDir <history-directory>`，复用原生 PandasDataBacktesting。
每只证券一个 `SYMBOL.csv`，必须包含带时区的 `timestamp` 和 `open,high,low,close,volume`，
按时间递增、无重复、价格和成交量为正；包括 SPY、QQQ，并提供至少 30 个交易日的预热历史。
CSV 时间表示纽约交易日开始，不是收盘后才可用的数据时间。

研究沿用 LumiBot 的模拟时钟、价格钳制和已知未来文档拒绝机制。
独立层再次验证研究证据的公开时间／观察时间不晚于模拟时刻，并只用前一交易日及之前的流动性数据。
无日期文档可使用有解释、明确标记的估计日期，不能声称精确。
这些检查无法验证模型虚构的证据时间，也不能消除模型训练中已知后续事件的污染；
真实 AI 回测仍需逐条审计工具追踪、历史新闻／基本面的发布时间和版本，不能宣称严格防止所有认知层面的未来信息。

成交由原生 BacktestingBroker 的日线 OHLC 限价规则产生，绝不把模型建议价格写成成交。
历史运行默认收取成交额 0.1% 的费用／摩擦假设；Paper 模式不把这个假设冒充券商实际费用。
日线不重放盘口、排队、网络延迟、冲击成本；触及限价不保证真实可成交。
因此这些回放是功能证据，不能用来证明可实现收益。

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode report -RunDir artifacts/fund-paper
```

| 输出文件 | 内容 |
|---|---|
| `state.sqlite3` | 权威账本、未决订单、累计提交次数、回撤锁、审计事件 |
| `status.json` | 本地模式的进程心跳、交易日历、人工停止状态、现金／应收款、待处理订单及撮合错误 |
| `preflight.json` | check 命令的真实行情时间戳、行情年龄、下次开盘时间、所缺环境变量；不含密钥 |
| `events.jsonl` | 可重建的事件导出：研究、结构化决策、行情、账户、订单及异常 |
| `premarket_research.json` | 最近一次盘前研究的结论、证据、研究时间及目标交易时段 |
| `postmarket_review.md`、`.json` | 最近完成的中文盘后复盘、选股／择时假设及验证条件 |
| `reviews/YYYY-MM-DD.md`、`.json` | 按交易日归档的不可变复盘导出，可从 SQLite 重建 |
| `strategy_memory.json` | 已激活的策略备忘、版本、可用时间和 Mock 标记 |
| `approvals.json` | 审批 ID、digest、交易范围、有效期与状态；SQLite 是权威状态 |
| `monitor.json`、`runner_health.json` | 独立监控状态、Telegram 确认统计与 Paper 运行心跳 |
| `strategy_evaluations.json` | 每版备忘之后实际组合的观测跟踪，明确样本不足与归因限制 |
| `fills.csv` | 原生回调成交或标记明确的累计成交核对；费用未知保留为空 |
| `native_trade_events.csv` | 历史模式原生引擎交易事件 |
| `equity.csv` | 每日净值、现金、持仓、应收款、收益率，可用于绘制资金曲线 |
| `report.json` | 收益率、观测／日采样最大回撤、零无风险利率日 Sharpe、SPY／QQQ 价格收益比较、最近一次量化构建（`latest_risk_overlay`）和模型退避次数 |
| `notifications.json`、`runtime.log` | 本地通知接口和运行日志 |
| `cache/agent_runtime/traces/` | LumiBot 原生模型、工具调用、时间戳与 usage 明细 |

基准采用同一观察日的买入持有价格收益，暂不包含分红、费用及执行摩擦，不是完整总收益基准。
每日资金曲线采用当天最后一次账户观测，回撤只能覆盖采样到的高点。
首版收益计算不剔除外部出入金；运行期间不要入金、出金或重置 Paper 账户，否则报告需要资金流调整。
模型账本保存提供方原始 usage、缓存标记和已报告 token 数，不重复叠加推理 token，并按实际模型 ID 汇总。
单模型运行仍支持 `model_prices_per_million={"input":...,"output":...}`；多模型须提供
`model_prices_per_million={"<provider/model>":{"input":...,"output":...},...}`，为每个模型填写自己的每百万 token 费率。
混用模型却只给单一费率、缺少某个模型的价格或 usage 时，费用估算为 null，避免把 Flash 的费率套给 Pro。
它始终是粗略估算，绝不声称是提供方实际账单；高低峰、缓存读写和提供方计费 token 口径仍需核对。
运行输出属于私人账户数据，放在已被 Git 忽略的 `artifacts/` 中，不要提交或公开。

## 本地验证与待验证范围

本次已运行官方 `first_backtest`，以及官方 `ai_researcher_trader` 的原生引擎＋脚本模型测试。
新增应用覆盖边界风控、异常数值、账户不一致、回撤锁重启、并发预留、停机未知订单、模拟端点及原生回测成交。
另使用真实 Yahoo 历史价格完成 2025-01-06 至 2025-02-01 的脚本决策回放，生成实际引擎成交、费用和双基准记录。
这是功能回归，不是 AI 收益证明。

2026-11-01（北京时间）联网读取到真实 SPY、QQQ 的分钟与历史日线，且通过本地虚拟账户的
原生调度器只读启动检查。当时为周末休市，源行情是前一交易日收盘；没有伪造成新报价。
本地撮合的开盘、部分成交、费用、崩溃恢复、源数据故障、过期、拆股／分红及风控测试使用可控时钟与行情替身。
真实开盘时段持续运行、真实模型投资决策到本地成交、网络抖动下长期表现仍需后续验证。
相关回归 384 项通过，覆盖分角色模型到正常时段决策下单、原生指标历史请求、休市退出、进程锁冲突、提供方配置、分模型费用及真实 SDK 的工具请求路径；另有原生网络工具单测 27 项通过。
新增运行闭环测试验证原生账户从重大交易挂起、人工批准到重新风控成交，以及十分钟后重启继续第二只证券的目标，均不重复调用模型。
审批、Telegram HTTP、全局限流、重新故障通知、独立 STOP 保护与对照回放使用可控时间、行情或提供方替身；
没有向 Telegram 真实收件人发送消息，也没有验证真实 Telegram 鉴权。部署进程尚未提供 Telegram 环境变量。
其中 18 项新增盘后回归验证逐阶段恢复、跨日记忆、证据引用、未来隔离、休市／提前收盘、STOP、观察模式与可重建导出。
原生本地生命周期测试已从实际虚拟成交进入盘后复盘，并确认下一交易日研究和决策收到备忘，新的目标仍经风控先卖出旧持仓。
另外通过 PowerShell 启动器执行 2025-01-06 至 2025-01-14 的合成行情／脚本模型回放，生成四次复盘及四版备忘，
所有结果明确标记 `mock=true` 和 `scripted/no-inference`；未调用付费模型，不能据此证明选股或择时效果。
新增的完整真实模型盘后复盘及后续市场开盘反馈循环仍待实际运行验证。
另通过 PowerShell `qualify -Scripted` 对同一份四证券合成 CSV 行情执行 25 个日采样的双运行检查，
结果为 `no_demonstrated_improvement`、`auto_promoted=false`；这验证比较流程没有把相同脚本决策包装成策略改善。
SDK 回归先在 LiteLLM 1.106.2 下复现导入异常，再在 1.105.1 下验证修复；
SDK 模拟响应与原生 ADK 的 HTTP 替身测试均不产生模型推理费用，不证明真实提供方已鉴权或返回研究结果。
Flash 和 V4 Pro 均通过原生 ADK＋LiteLLM 的 HTTP 替身工具往返测试，保留思考内容的多轮传递；
PowerShell 分角色启动参数也已通过原生回测引擎＋脚本模型验证。真实 V4 Pro 接口及开盘决策仍待验证。
原生免费网络搜索实际返回了公开结果。自动化盘前研究测试使用模型替身；真实运行的证据另述如下。
持续只读进程的心跳、运行中报告读取和 STOP 标记识别也已验证，测试后已结束该进程。

2026-11-01 后续网络复测成功读取 SPY、QQQ、AAPL、BRK.B、AMZN 的实际 Yahoo 行情，
并读取 AAPL 的 10,000 根历史日线及其他证券实际较短的历史。当天为周末，分钟数据止于前一交易日收盘，
仅可用于研究，未进行模型推理或下单。

随后读取用户自行启动的本地运行账本，确认北京时间 2026-11-01 12:46:02 完成一次真实 DeepSeek 盘前研究：
`mock=false`、`cache_hit=false`，结构化结果成功保存并包含 15 条证据，另保存提供方 usage。
这证明该次模型研究调用和持久化完成，不证明每条研究结论正确，也不包含模型投资决策或成交验证。
该轮工具日志披露了网页 Fake-IP 地址校验、SEC TLS 请求、未安装可选浏览器和 SQL 请求失败；
应结合这些缺项审阅研究，不能把保存成功视为基本面／新闻覆盖完整。

开发测试进程未读取或注入用户的真实模型 Key，也未配置 Alpaca Key。原有账户进程未被开发测试重启或迁移，
新功能需要操作员在原目录重启，并单独启动监控后生效。文档 HTML 构建完成；全站仍报告未修改数据源 docstring 等处的诊断，不能声称全站无警告。
Alpaca 网络登录、Paper 下单／撤单／停机恢复和通知投递尚未验证，需配置相应凭证后单独验证。
没有向任何真实资金账户发送订单。
当前虚拟环境的 DuckDB 2.0 会在退出时打印 nanobind 引用泄漏诊断；测试退出码正常，但长期运行的内存稳定性仍待观察。

### 2026-11-01 Schwab 只读体检与共享账户归属

新增 13 项回归（`tests/test_autonomous_fund_schwab.py`）：首次启动冻结全部已有持仓（含货币基金）并标记共享代码、
之后只有 AI 买入的部分构成分仓、专用账户仍拒绝无法归属的资产、共享代码被移出 AI 可交易范围且你的挂单不阻挡 AI、
你买卖外部持仓会暂停交易、部分持仓读取不会被当成空仓、体检预览保留现金／分仓／共享代码／续期提示、
现金不足／保证金负债／现金账户已交割资金不足／未允许外部持仓被标记、`schwab-check` 生成报告且无法下单、未登录时拒绝。
6 处新保护的变异检查全部被抓到。真实 Schwab 账户、OAuth 与行情未在本环境验证（无凭证、网络受限）。

### 2026-11-01 真实数据回放、影子 A/B 与运行验收

新增 20 项验证回归（`tests/test_autonomous_fund_validation.py`），其中 6 项直接用仓库内真实行情：2020 暴跌中波动率目标与回撤分档把回撤减半、
全部默认时回撤低于 8% 硬锁、2019 平稳市场成本 < 0.3%、ETF 预算修复平稳市场的收益拖累、回放无前视（篡改未来数据不改变此前权重）、
CSV／分钟线加载。其余覆盖配对检验、两组打分方向一致、冠军只在显著变差时切换并可切回、对照组“盲测”且每日一次并受预算约束、
对照组失败不影响交易、验收报告的通过与失败、`acceptance` 命令输出。另新增 1 项端到端浸泡测试（`tests/test_autonomous_fund_soak.py`）：
原生本地模拟一个完整交易日——决策与影子决策、额度内自动买入并成交、跌破 AI 止损后自动卖出、提前唤醒 AI 复核、
收盘复盘后学习评估、生成验收报告。对 8 处新逻辑做变异检查，全部被测试抓到。`tests/test_autonomous_fund*.py` 合计 330 项通过。
本环境的网络策略拦截了 Yahoo、Telegram、资讯源与模型接口，真实联网验证仍待在放开这些域名后进行。

### 2026-11-01 Telegram 回复审批、实时推送报价与资讯监控

新增 30 项回归（`tests/test_autonomous_fund_realtime.py`）：回复提醒「同意」批准对应请求并回执、代码拒绝、
被拒请求不能复活、其他会话／未授权用户／早于请求的回复／STOP／错误代码一律不批准、群组需显式审批人、
「状态」指令、关闭开关；推送帧解码与股票类别映射、乱序帧不回滚、订阅／断线重连／错误记录、
新鲜推送优先而过期／未来／休市回退分钟线；RSS／Atom 解析与拒绝实体声明、Yahoo 新闻两种格式、
首轮基线后公告／美联储／搜索命中才算重大且只消费一次、持仓标题突增每窗口一次、无联系人不访问 SEC、
来源失败退避、资讯上下文只含相关且不晚于当前时间的条目、重大资讯唤醒 AI 并监控 AI 搜索词、
决策中的 `news_watch` 不接受额外字段、可配置的迭代间隔。对 9 处新保护做变异检查，全部被测试抓到。
`tests/test_autonomous_fund*.py` 合计 309 项通过。真实 Yahoo 推送、真实 Telegram 回复、真实资讯源均未在本环境联网验证。

### 2026-11-01 授权资金、分钟级盯盘、记分卡与研究网络

新增 33 项回归（`tests/test_autonomous_fund_autonomy.py`）：授权资金分仓与保留现金、授权调整与出入金、
无法归属的账户拒绝启动、外部持仓冻结、保留现金不能用于买入、本地撮合按分仓复核、文字约束进入上下文、
价位与提醒校验、止损自动卖出且不需审批并禁止买回、止盈减到指定权重并在撤单后重试、保护性卖单不因旧目标过期被撤、入场价等待、
提醒和大幅波动提前唤醒 AI 且不突破最短间隔与失败退避、新决策替换旧价位、预测与决策记分卡、
DoH 解析与 Fake-IP 诊断、DoH 模式下 SSRF 保护仍有效、DuckDB 可读下载目录但不能读账本、`adjust-reserve` 命令。
对 10 处新保护做了变异检查，9 处直接被测试抓到；本地撮合“分仓现金”单项变异与“分仓净值敞口检查”等价
（敞口 ≤100% 已蕴含现金充足），两项同时去掉时被抓到。`tests/test_autonomous_fund*.py` 合计 279 项通过。
同样只使用合成行情、合成账户和模型替身；真实 Fake-IP 网络、真实 DoH 服务和真实模型对价位的使用尚未验证。

### 2026-11-01 量化风控与例外审批

新增 `quant.py` 与 32 项专门回归（`tests/test_autonomous_fund_quant.py`），覆盖波动率目标、
厚尾历史 ES、按波动率调整的单名上限、重叠分组、缺失历史只减不增、平盘序列的保守相关、回撤分档、
市场压力识别与未知状态、单日亏损闸门、自动建仓到额度后一次计划审批、压力下买入审批而卖出不等待、
追价上限、改价撤单、模型失败指数退避与每日预算、通知去重、增量导出、WAL 与索引、Paper 只查未完成订单。
审批测试改为整计划审批合同（范围、价格领口、触发原因、日期、有效期、摘要、账户、STOP 均不可扩张）。
对 7 处关键保护做了变异检查（逐一禁用压力审批、单日亏损、追价、波动率目标、缺失历史、退避、审批敞口范围），
每处都有测试失败。`tests/test_autonomous_fund*.py` 合计 246 项通过。

一年规模（约 49 万事件、250 份复盘）的合成账本复测：每分钟导出从 6.5 秒降到 0.002 秒（增量追加），
报告重建约 1.7 秒且改为每 5 分钟一次，独立监控单次轮询从 9 秒以上降到约 0.15 秒。
以上均为合成行情与模型替身，未验证真实行情下的风险估计质量、真实模型对风险上下文的使用，
也未用真实分钟线统计本地撮合的成交率；这些仍需实盘时段运行验证。

## 本次真实行情模式的代码范围

| 文件 | 变动 |
|---|---|
| `scripts/autonomous_fund/market_data.py` | 新增公开行情、源时间戳、完成 K 线和 NYSE 日历适配器 |
| `scripts/autonomous_fund/local_broker.py` | 新增原生 Broker 接口的持久化虚拟账户及撮合 |
| `tests/test_autonomous_fund_local.py` | 新增可控时钟／行情、原生调度器和恢复回归测试 |
| `scripts/autonomous_fund/run.py`、`scripts/run_autonomous_fund.ps1` | 扩展 local/check、只读观察、单次运行和显式秘密文件参数 |
| `scripts/autonomous_fund/strategy.py` | 接入本地账户、原始行情时间和本地成交核对 |
| `tests/test_autonomous_fund_premarket.py` | 休市启动研究、持久化去重、原生盘前钩子到开盘执行的回归测试 |
| `scripts/autonomous_fund/review.py`、`tests/test_autonomous_fund_review.py` | 两角色盘后复盘、版本化反馈、逐阶段恢复、证据校验与时间隔离回归 |
| `scripts/autonomous_fund/execution.py`、`tests/test_autonomous_fund_execution.py` | 持久化调仓、重大操作审批、范围／摘要绑定、原子消费与重启恢复 |
| `scripts/autonomous_fund/monitor.py`、`tests/test_autonomous_fund_monitor.py` | 独立 STOP 保护、Telegram 脱敏、持久化投递、去重与限流重试 |
| `scripts/autonomous_fund/evaluation.py`、`tests/test_autonomous_fund_evaluation.py` | 时间受限的后验观测、冻结备忘与相同行情的原生对照回放 |
| `tests/test_autonomous_fund_shutdown.py` | 原生关闭钩子唤醒休市等待、SIGINT 退出和真实进程锁冲突回归 |
| `tests/test_autonomous_fund_model.py` | 实际 LiteLLM 工具请求、模拟工具往返和原生 ADK＋HTTP 替身依赖回归 |
| `tests/test_autonomous_fund_model_roles.py` | 分角色模型覆盖、全部凭证检查、旧账本及风险锁保留、按模型记录 usage 和费率 |
| `scripts/autonomous_fund/risk.py` | 区分历史研究日期规则、不可花费的应收款；买入预算、再平衡带、参与率和追价封锁 |
| `scripts/autonomous_fund/schwab.py`、`scripts/autonomous_fund/holdings.py`、`tests/test_autonomous_fund_schwab.py` | Schwab 只读体检与共享账户持仓归属 |
| `scripts/autonomous_fund/validation.py`、`tests/test_autonomous_fund_validation.py`、`tests/test_autonomous_fund_soak.py` | 真实数据风控回放、影子 A/B 统计、运行验收报告与端到端浸泡测试 |
| `scripts/autonomous_fund/news.py`、`tests/test_autonomous_fund_realtime.py` | 实时资讯监控；Telegram 回复审批、推送报价与资讯回归 |
| `scripts/autonomous_fund/scorecard.py`、`tests/test_autonomous_fund_autonomy.py` | 预测与决策记分卡；授权资金、盯盘、研究网络回归 |
| `scripts/autonomous_fund/quant.py`、`tests/test_autonomous_fund_quant.py` | 量化组合构建（波动率目标、ES、单名与分组上限、回撤分档、压力识别）及执行／预算回归 |
| `scripts/autonomous_fund/report.py` | 本地增量费用、观察模式、运行期间只读报告 |
| `scripts/autonomous_fund/config.json` | 设置本应用的 DeepSeek Flash 默认模型 |
| `scripts/autonomous_fund/requirements.txt` | 本应用需要的免费网络搜索依赖与已验证的 LiteLLM 兼容版本约束 |
| `docsrc/environment_variables.rst`、`CHANGELOG.md`、本文 | 同步配置说明、使用限制及变更记录 |

没有修改共享框架、官方 AI 示例、依赖主版本、包版本或发布配置。

## Schwab 实盘准备

本应用**仍然没有实盘下单入口**，`TRADING_BROKER=schwab` 或任何 `.env` 设置都不会启用它。目前提供的是实盘前的零风险准备：

**1. 账户选择**：强烈建议在 Schwab 为 AI 单独开一个账户（同一登录下可有多个账户，用 `SCHWAB_ACCOUNT_NUMBER` 指定），
放入授权资金即可，`FUND_ALLOW_EXTERNAL_HOLDINGS=false`。这样 AI 的成交、税务批次和你的持仓完全分开。

**2. 与你的持仓共用一个账户**（可行但次优）：设 `FUND_ALLOW_EXTERNAL_HOLDINGS=true`。
- 首次启动时，账户里的**全部**持仓（股票、ETF、货币基金等）都记为你的外部持仓并冻结数量，按券商市值估值，不计入 AI 分仓；
  分仓 = 现金 − 保留现金，AI 之后自己买入的才是分仓持仓。
- 你已持有、且在 AI 投资范围内的代码（例如你持有 AAPL）会**自动从 AI 可交易范围中剔除**：否则 AI 卖出时，
  券商可能按先进先出等批次规则卖掉你的长期持仓，带来税务后果，也无法区分归属。你在这些代码上的挂单不会阻挡 AI。
- 你手动买卖了外部持仓，系统会暂停交易，核对后用 `adjust-reserve --rebaseline-external`（出入金同时用 `--amount`）重新确认。
- 券商持仓读取失败或只返回部分结果时直接拒绝，不会把“读失败”当成“没有持仓”而重复买入。

**3. 只读体检 `schwab-check`**：在 `.env` 填好 `SCHWAB_ACCOUNT_NUMBER`（以及自己的 `SCHWAB_APP_KEY`/`SCHWAB_APP_SECRET`/
`SCHWAB_BACKEND_CALLBACK_URL`，或使用 LumiBot 默认应用）后运行：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode schwab-check -RunDir artifacts/schwab-check
```

它使用 LumiBot 原生 Schwab 适配器完成 OAuth 和账户读取，并在该实例上**禁用所有下单、撤单、改单方法**。输出 `schwab_check.json`：
账户类型（现金／保证金）、现金、已交割可用现金、未交割现金、清算价值、每个持仓及是否在 AI 范围、挂单、
预计保留现金与 AI 起始分仓、将被剔除的共享代码，以及需要处理的问题（现金少于授权资金、保证金负债、现金账户已交割现金不足、
已有持仓但未允许外部持仓、存在挂单）和提示（未设 `SCHWAB_APP_SECRET` 时约 7 天需重新登录；未设 `SCHWAB_APP_KEY` 时 OAuth
回调经过 LumiBot 默认应用的 `api.botspot.trade`，如需完全自控请注册自己的 Schwab 开发者应用）。

**4. 拟议的实盘入口（需要你单独明确授权后才会实现）**：
- 前置条件：本地或 Alpaca Paper 连续运行通过 `acceptance`（默认 20 个交易日），`schwab-check` 无问题；
- 独立的 `schwab-live` 模式，必须同时满足：`.env` 中显式确认（如 `FUND_LIVE_CONFIRM=I-UNDERSTAND-REAL-MONEY`）、
  账户号与上次体检一致、监控与 Telegram 健康、授权资金不超过你设定的上限；
- 现金账户只用已交割资金买入（T+1），保证金账户也绝不借款或卖空；
- 初期所有买入都需要审批（`FUND_AUTO_DAILY_EXPOSURE_INCREASE=0`），稳定后再放开自动额度；
- 每笔订单与券商回报逐笔对账，任何不一致即 STOP；首周只允许很小的授权资金；
- AI 仍然没有修改配置、权限或启动实盘的能力。

Schwab 原生适配器的通用说明见 `docsrc/brokers.schwab.rst`（OAuth、token 续期、订单状态）。
不要把框架通用文档中的 paper/live 描述当成 Schwab 提供独立 Paper 交易环境的保证。

官方参考：[Alpaca Paper](https://docs.alpaca.markets/docs/paper-trading)、
[DeepSeek API](https://api-docs.deepseek.com/)、
[Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)、
[yfinance](https://ranaroussi.github.io/yfinance/reference/index.html)、
[Alpaca 行情权限](https://docs.alpaca.markets/us/v1.1/docs/market-data-faq)、
[LumiBot 混合 Agent 流程](https://lumibot.lumiwealth.com/agents_flows.html)、
[Schwab Developer Portal](https://developer.schwab.com/user-guides/individual-developer/become-individual-developer)。
