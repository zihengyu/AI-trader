# AI 自主组合管理：本地模拟交易

基于原生 LumiBot Agent、Strategy、Broker 和 BacktestingBroker 的纸面账户应用。

**Last Updated:** 2026-11-01

**Status:** 公开行情、本地虚拟账户及一次真实 DeepSeek 盘前研究已验证；量化风控、例外审批、授权资金分仓、盯盘、记分卡、Telegram 回复审批、实时推送报价与资讯监控已通过替身测试；Alpaca、真实推送和开盘交易待验证

**Audience:** 本地运行者和维护者

## Overview

本应用位于 `scripts/autonomous_fund/`，不修改 LumiBot 核心、原有 AI 示例、版本或发布流程。
Ubuntu 的 `.env` 模板、systemd 双服务、审批理由、自动日报和恢复验收步骤见
[服务器部署说明](AUTONOMOUS_FUND_UBUNTU.md)。该入口启动时保持 STOP，独立监控正常后由操作员恢复。
`researcher` 研究行情、技术指标、基本面、新闻与持仓；`manager` 自主选择完整目标组合、现金比例和下次复核时间。
两者复用原生 `self.agents.create()` 和结构化 `output_schema`，均设置 `allow_trading=False`。
这是用户指定的混合策略：唯一的投资决策者输出目标，独立 Python 代码执行量化风控、风险准入与订单。
AI 的权重是上限：确定性量化风控只会把它们缩小（波动率目标、ES、按波动率调整的单名上限、行业组上限、
回撤分档预算、单日亏损闸门和流动性），见[量化风控原则](#量化风控原则)。
本应用不是对官方纯 AI 示例模式的修改。

没有固定买卖信号，也不要求每天交易。默认配置包含多只流动性较高的美股和普通 ETF；
操作员可审核并修改名单，AI 可在名单内自由选择。名单是操作员信任边界，不能让 AI 修改分类或把杠杆／反向基金伪装成普通 ETF。
每次交易前还检查最近 20 个已完成交易日的平均成交额至少 2,000 万美元。
这份当前名单不是历史成分股数据库；历史实验存在存活者偏差，严肃研究应提供当时可投资的名单。

## 安装和启动

在项目根目录执行。示例中不包含真实密钥；请通过当前进程环境或自己的秘密管理器注入凭证。
启动器主动禁用 `.env` 自动发现，避免意外加载已有实盘配置。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r scripts/autonomous_fund/requirements.txt
```

应用依赖固定 `litellm==1.105.1`，与框架要求的 MCP 1.x 配合使用；不修改共享依赖或升级 MCP 主版本。
修改依赖前先停止正在运行的模拟盘。以后重新安装共享依赖后，也需重新应用本应用的依赖约束。

无需密钥的完整机械检查：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode mock -RunDir artifacts/fund-mock
```

它运行真实 LumiBot 回测引擎与限价订单，但价格和两名 Agent 的回答都是合成测试数据。
输出会明确标识 `mode=mock`、`scripted_model=true`，不证明投资收益。
每次历史实验使用新的目录，防止混合账本。PowerShell 启动器对回测设置 1,200 秒上限。
Python 模块同样可跨平台运行：`python -m scripts.autonomous_fund.run --help`。

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
若使用自己管理的未跟踪秘密文件，显式指定 `-EnvFile <your-secret-file>`；
现有进程环境优先，启动器不搜索其他 `.env` 文件。不要把真实密钥发到聊天或提交 Git。

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
开盘后的研究员会参考盘前结论并更新信息，基金经理才产生可执行的目标组合，
随后重新读取账户、行情和风控。盘前研究不会成为待开盘直接执行的订单。
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

研究员负责盘前、正常时段及盘后研究；基金经理在正常交易时段自主复核组合并输出结构化目标，
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

每分钟检查订单、实际现金和持仓、紧急停止与观测回撤。
模型复核频率由基金经理指定，范围为 1 分钟至 1 天；市场关闭时由 LumiBot 原生生命周期管理。
AI 的完整目标组合中，遗漏的已有持仓代表卖出，空目标列表代表全部现金。
只处理整股股票／ETF；外部加入的碎股、空仓、衍生品或名单外资产会阻止新增交易，要求人工核查。

## 独立风控与执行合同

默认风险参数集中在 `config.json`，加载成不可变策略对象；模型没有配置写入或交易修改工具。
本应用还锁定研究用 DuckDB 的文件／扩展访问，防止多语句 SQL 读写风险账本或本地凭证；
已加载的行情／文档表仍可分析。研究浏览器只接受 HTTP(S) 导航，不能直接打开本地文件或脚本 URL，
下载和上传保留在独立研究目录。浏览器为原生可选依赖，本次未做真实浏览器联网验证。
禁止融资、卖空、期权和期货；只提交整股、当日有效限价单。
硬上限：单证券 10%、总证券敞口 70%、每日最多 20 次提交（含改价重挂与卖出）、观测净值高点回撤 8% 锁定增仓。
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
- 止损／止盈触发后立即按新报价挂卖单；未成交被改价撤单后会继续重挂直到减到目标，不会“触发一次就忘”。
  同一决策内被止损的证券不会被重新买回，等 AI 下一次复核再决定。
- 价格提醒、持仓或目标证券相对本决策首次观测价的波动达到 `execution.review_on_move`（默认 3%）、
  市场进入压力状态时，会把下一次 AI 复核提前；提前复核仍遵守最短复核间隔、失败退避和每日模型预算。
  基金经理在 `watch_triggers` 中看到被唤醒的原因。
- 每次新的 AI 决策替换全部旧价位和提醒。决策中的价位同样受校验：止损须低于入场价和止盈价，
  `take_profit_weight` 须低于目标权重，提醒只能针对投资范围内的证券。

这让“盯盘”成本接近零，而择时（入场价、止损、止盈、何时复核）仍由 AI 决定。
行情仍是 Yahoo 已完成分钟线，触发和成交有一到两分钟延迟，不是逐笔级别的实时盯盘。

## 复盘记分卡：AI 自我优化的客观反馈

为避免 AI 只凭叙述“自我感觉良好”，复盘闭环加入两张只用**已完成收盘价**计算的记分卡（`scorecard.py`）：

- **预测记分卡**：每次盘后复盘可给出最多 10 条可证伪预测（证券、`up`／`down`／`outperform`／`underperform`、
  1–20 个交易日、基准、对应的假设）。到期后自动打分，按假设汇总命中率；未到期为 `pending`，缺数据为 `unavailable`，不估算。
- **决策记分卡**：每个交易日最后一个 AI 目标组合，从当日收盘起算 1／5／20 个交易日的收益，
  减去同等总敞口下 SPY 的收益，得到选股超额收益（不含费用和风控缩放，专门衡量 AI 的选股能力）。

两张记分卡进入盘后复盘、研究和决策的上下文，复盘被要求据此保留、修正或撤回假设，结果写入日报和 `report.json` 的 `latest_scorecard`。
这是客观反馈而不是自动改参数：风险参数、投资范围和权限仍只能由你修改。样本少时结论不稳定，重叠期限之间相关。

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

## 量化风控原则

借鉴量化基金常用的“预测与组合构建分离”：AI 负责观点和相对配置，`scripts/autonomous_fund/quant.py`
负责把目标投影到风险约束内。全部计算只用调用方提供的**已完成交易日**收盘价（不含当日或未来 K 线），
没有模型、券商或网络依赖。每一步都只缩小权重，所以结果同时满足所有约束；函数末尾还独立复核一次。

| 约束 | 方法 | 默认值（`quant_risk`） |
|---|---|---|
| 波动率估计 | 最近 63 个交易日简单收益的 EWMA 协方差（RiskMetrics λ=0.94），年化 | 下限 10%；平盘序列相关系数按 1 处理 |
| 单名风险上限 | 权重 ≤ min(10%, `max_name_risk` ÷ 年化波动) | 0.025：波动 25% 的股票上限 10%，波动 50% 的上限 5% |
| 行业／主题组 | 运营者定义、可重叠的组，组内权重和 ≤ 组上限 | 科技 30%、通信 20%、可选消费 20%、金融 30%、医疗 25%、能源 15%、必需消费 25%、大型成长股 35%、宽基指数 40% |
| 回撤分档预算 | 回撤 ≤ 4% 满额；4%–8% 线性降到 25%；≥8% 锁定增仓 | 总敞口预算 = 70% × 系数，超出部分自动卖出 |
| 组合波动率目标 | 事前年化波动 √(wᵀΣw) 超过目标时整体等比例缩小 | 12% |
| 尾部风险 | 1 日 97.5% ES = max(252 日历史模拟 ES, 正态 ES)，超过上限时整体缩小 | 2% 净值；历史样本少于 120 天时只用正态 ES |
| 数据缺失 | 历史不足 20 个交易日的证券不能加仓，只能持有或减持，并按 60% 波动、完全相关计入组合风险 | 不填充、不插值 |
| 单日亏损闸门 | 相对上一交易日最后观测净值亏损 ≥ 2% 时，当日停止买入，次日自动恢复 | 卖出不受影响 |
| 市场压力 | SPY 上一交易日或盘中跌幅 ≥ 3%，或 SPY EWMA 年化波动 ≥ 35%；基准历史缺失按压力处理 | 压力下的买入需要审批 |

构建结果、绑定的约束、事前波动和 ES 写入 `risk_overlay` 事件，报告的 `latest_risk_overlay` 可查看。
基金经理在每次决策前收到风险上下文：各证券波动估计、单名上限、分组、当前回撤和预算、单日亏损、
今日已用额度和市场压力状态，以便直接给出可行的组合，减少被缩小或被拒绝的情况。
这些估计基于历史数据，不能预测跳空、停牌或从未出现过的相关性变化；ES 和波动目标约束的是事前估计，
不是亏损上限保证。修改 `quant_risk` 后重启生效，摘要变化会让旧目标失效。

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

- 研究或决策任何一步失败（超时、结构化回答无效、证据时间不合规、决策过期或超限）后，
  按 `failure_backoff_minutes`（默认 5、15、60 分钟，之后保持 60 分钟）延后下一次复核，成功后清零；
  连续失败次数与下次时间持久化，重启不会绕过退避。
- 每个纽约交易日最多 `max_agent_runs_per_day`（默认 60）次 Agent 运行，其中 `reserved_review_runs`（默认 4）
  留给盘前研究与盘后复盘。日内额度用完后不再发起新决策，已接受的目标继续执行，次日自动恢复，
  并发送一次 `model_budget_exhausted` 通知。
- AI 指定的 `next_review_at` 不早于 `min_review_minutes`（默认 15 分钟）；目标执行窗口也按此下限计算。

## 停止、恢复与异常通知

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode stop -RunDir artifacts/fund-paper
.\scripts\run_autonomous_fund.ps1 -Mode resume -RunDir artifacts/fund-paper
```

`stop` 创建 STOP 文件；运行进程下一次迭代停止新单并尝试取消本应用已知未完成订单。
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
| `scripts/autonomous_fund/news.py`、`tests/test_autonomous_fund_realtime.py` | 实时资讯监控；Telegram 回复审批、推送报价与资讯回归 |
| `scripts/autonomous_fund/scorecard.py`、`tests/test_autonomous_fund_autonomy.py` | 预测与决策记分卡；授权资金、盯盘、研究网络回归 |
| `scripts/autonomous_fund/quant.py`、`tests/test_autonomous_fund_quant.py` | 量化组合构建（波动率目标、ES、单名与分组上限、回撤分档、压力识别）及执行／预算回归 |
| `scripts/autonomous_fund/report.py` | 本地增量费用、观察模式、运行期间只读报告 |
| `scripts/autonomous_fund/config.json` | 设置本应用的 DeepSeek Flash 默认模型 |
| `scripts/autonomous_fund/requirements.txt` | 本应用需要的免费网络搜索依赖与已验证的 LiteLLM 兼容版本约束 |
| `docsrc/environment_variables.rst`、`CHANGELOG.md`、本文 | 同步配置说明、使用限制及变更记录 |

没有修改共享框架、官方 AI 示例、依赖主版本、包版本或发布配置。

## 未来 Schwab 阶段

原生实现已存在于 `lumibot/brokers/schwab.py` 和 `lumibot/data_sources/schwab_data.py`，
本地说明是 `docsrc/brokers.schwab.rst`。现有适配器支持股票／ETF、OAuth、账户读取与订单生命周期。
本应用刻意不构造 Schwab 实例；`TRADING_BROKER=schwab` 不会启用它。
不要把框架通用文档中的 paper/live 描述当成 Schwab 提供独立 Paper 交易环境的保证。

未来需要独立明确授权：申请 Schwab Trader API 访问和应用 OAuth，
由你在券商页面授权并安全保存凭证；选择并核验确切账户，先只读读取现金、持仓和订单。
随后验证账户选择、费用、结算现金、撤单、部分成交、断线恢复、通知及风控边界，
再审阅一个单独的实盘执行入口。不能通过改一个环境变量把本应用切到实盘。
AI 不应拥有配置仓库、券商管理、API 权限或实盘启动入口的写权限。

官方参考：[Alpaca Paper](https://docs.alpaca.markets/docs/paper-trading)、
[DeepSeek API](https://api-docs.deepseek.com/)、
[Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)、
[yfinance](https://ranaroussi.github.io/yfinance/reference/index.html)、
[Alpaca 行情权限](https://docs.alpaca.markets/us/v1.1/docs/market-data-faq)、
[LumiBot 混合 Agent 流程](https://lumibot.lumiwealth.com/agents_flows.html)、
[Schwab Developer Portal](https://developer.schwab.com/user-guides/individual-developer/become-individual-developer)。
