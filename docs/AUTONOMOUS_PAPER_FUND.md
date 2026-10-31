# AI 自主组合管理：本地模拟交易

基于原生 LumiBot Agent、Strategy、Broker 和 BacktestingBroker 的纸面账户应用。

**Last Updated:** 2026-10-31

**Status:** 本地测试及脚本决策历史回放通过；真实模型与 Alpaca 账户待凭证验证

**Audience:** 本地运行者和维护者

## Overview

本应用位于 `scripts/autonomous_fund/`，不修改 LumiBot 核心、原有 AI 示例、版本或发布流程。
`researcher` 研究行情、技术指标、基本面、新闻与持仓；`manager` 自主选择完整目标组合、现金比例和下次复核时间。
两者复用原生 `self.agents.create()` 和结构化 `output_schema`，均设置 `allow_trading=False`。
这是用户指定的混合策略：唯一的投资决策者输出目标，独立 Python 代码执行风险准入与订单。
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
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

无需密钥的完整机械检查：

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode mock -RunDir artifacts/fund-mock
```

它运行真实 LumiBot 回测引擎与限价订单，但价格和两名 Agent 的回答都是合成测试数据。
输出会明确标识 `mode=mock`、`scripted_model=true`，不证明投资收益。
每次历史实验使用新的目录，防止混合账本。PowerShell 启动器对回测设置 1,200 秒上限。
Python 模块同样可跨平台运行：`python -m scripts.autonomous_fund.run --help`。

## 模型与数据凭证

| 用途 | 环境变量 | 说明 |
|---|---|---|
| 模型选择 | `AI_MODEL` | 如 `openai/<model-id>`、`anthropic/<model-id>`、`deepseek/<model-id>`；也可用 `--model` 或 JSON 配置 |
| OpenAI | `OPENAI_API_KEY` | 只需要所选提供方的 Key |
| Claude | `ANTHROPIC_API_KEY` | 原生运行时通过 LiteLLM 调用 |
| DeepSeek | `DEEPSEEK_API_KEY` | 原生运行时通过 LiteLLM 调用 |
| Alpaca Paper | `ALPACA_API_KEY`、`ALPACA_API_SECRET` | 必须使用 Paper 账户自己的凭证 |
| 模拟标识 | `ALPACA_IS_PAPER=true` | 若明确设成 false，应用拒绝启动 |
| 可选宏观研究 | `FRED_API_KEY` | 没有时其他研究工具仍可用 |

不需要同时填写全部模型 Key。模型 ID 必须是账户实际支持的 ID，切换提供方不会伪造兼容性测试。
`AI_MODEL` 只影响此应用，不修改 LumiBot 其他策略的默认模型。
真实模型运行可能产生提供方费用；本次交付没有做付费模型推理。

## Alpaca Paper 运行

先在 Alpaca 端准备一个独立、空仓、无未完成订单、净值为 **10,000 美元** 的 Paper 账户。
应用只验证初始金额，不会替你重置账户、转入资金或清理其他交易。
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

默认风险参数集中在 `config.json`，加载成不可变 `Limits`；模型无配置写入或交易修改工具。
本应用还锁定研究用 DuckDB 的文件／扩展访问，防止多语句 SQL 读写风险账本或本地凭证；
已加载的行情／文档表仍可分析。研究浏览器只接受 HTTP(S) 导航，不能直接打开本地文件或脚本 URL，
下载和上传保留在独立研究目录。浏览器为原生可选依赖，本次未做真实浏览器联网验证。
禁止融资、卖空、期权和期货；只提交整股、当日有效限价单。
默认单证券 10%、总证券敞口 70%、每日最多 5 次提交、观测净值高点回撤 8% 暂停增仓。
拒绝非有限数值、重复证券、超额目标、未来／过期决策、陈旧账户或报价、缺失流动性证据。
日限额按纽约交易日计算，买卖、失败和取消的提交均占用次数，采用比“新增买单”更保守的统计。

订单数量根据真实账户净值、现金、实际持仓和限价计算，保留费用缓冲。
先执行减仓，每次最多提交一笔，再等待状态确认、重新读取资金和持仓后进行下一次决策。
未成交卖单不能为买单提供资金；账户中任何未完成或未知订单都会阻止新的订单。
达到回撤阈值后允许减仓，不自动强平，也不保证市场跳空后的实际损失止于 8%。
仓位上限约束下单准入；成交后的价格上涨或跳空仍可能使市值比例超过上限。

SQLite 在下单前原子保存意图和日限额，写入失败则不下单。
提交超时、响应丢失或程序崩溃后不自动重发。同目录进程锁和相同 API Key 的跨目录进程锁避免重复启动。
请只用一个专用 Paper 账户、一个凭证及本应用交易；不同机器、不同 Key、外部手动交易或其他程序不受本地锁控制。
订单列表达到原生适配器的 100 条返回上限时会拒绝继续，避免将截断列表误认为完整账户视图。
这是一项已知运维限制，需要人工核对后安排后续适配器分页改进，不能简单删除账本绕过。

正常重启继续使用同一 `--run-dir`、凭证和配置。账本与账户／模式／配置绑定，不会把另一账户的高水位或订单借来使用。
已保存券商 ID 的订单逐笔向券商查询。完整成交可从券商累计数量和均价恢复，标记为
`broker_cumulative_observation`，时间是观察时间；不会虚构停机期间的逐笔执行时间。
部分成交通过原生回调记录；停机期间部分成交后又取消的明细可能缺失，需要券商成交记录补核。
账户状态重读仍用于风险校验，缺失费用不会按零费用包装。

若崩溃恰好发生在券商接受之后、ID 保存之前，系统保持锁定。
先在 Paper 券商界面确认确切订单，再停掉本应用进程，使用：

```powershell
.\.venv\Scripts\python.exe -m scripts.autonomous_fund.run attach-order --run-dir artifacts/fund-paper --intent-id <intent-id> --broker-order-id <broker-order-id>
```

恢复程序校验相同凭证／配置、证券、方向、完整数量、限价及创建时间窗口，只绑定已有订单，不发单。
若确认从未发出订单，保留证据并人工维护该预留；首版不提供一键“假装失败后重试”。

## 停止、恢复与异常通知

```powershell
.\scripts\run_autonomous_fund.ps1 -Mode stop -RunDir artifacts/fund-paper
.\scripts\run_autonomous_fund.ps1 -Mode resume -RunDir artifacts/fund-paper
```

`stop` 创建 STOP 文件；运行进程下一次迭代停止新单并尝试取消本应用已知未完成订单。
不要把创建文件当成券商已确认撤单。模型或网络调用进行中可能延迟处理；紧急时同时去券商界面核验并撤单。
Ctrl+C 可以停止进程，但不能保证撤回已经被券商接受的订单。
`resume` 只移除人工停止，不清除回撤锁或不确定订单。
回撤暂停需要操作员停掉进程后显式执行 `reset-drawdown`；新高水位从下一次净值开始，操作会留痕。

`notify(kind, details)` 是通知扩展接口，默认输出控制台及持久化本地通知 outbox。
`notifications.json` 可交给你的通知程序处理。当前没有配置邮件、短信或聊天推送，也没有发送外部消息。
本地告警不能等同于已送达手机；无人值守前应单独接入和验证通知送达。

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
| `events.jsonl` | 可重建的事件导出：研究、结构化决策、行情、账户、订单及异常 |
| `fills.csv` | 原生回调成交或标记明确的累计成交核对；费用未知保留为空 |
| `native_trade_events.csv` | 历史模式原生引擎交易事件 |
| `equity.csv` | 每日净值、现金、持仓、收益率，可用于绘制资金曲线 |
| `report.json` | 收益率、观测／日采样最大回撤、零无风险利率日 Sharpe、SPY／QQQ 价格收益比较 |
| `notifications.json`、`runtime.log` | 本地通知接口和运行日志 |
| `cache/agent_runtime/traces/` | LumiBot 原生模型、工具调用、时间戳与 usage 明细 |

基准采用同一观察日的买入持有价格收益，暂不包含分红、费用及执行摩擦，不是完整总收益基准。
每日资金曲线采用当天最后一次账户观测，回撤只能覆盖采样到的高点。
首版收益计算不剔除外部出入金；运行期间不要入金、出金或重置 Paper 账户，否则报告需要资金流调整。
模型账本保存提供方原始 usage、缓存标记和已报告 token 数，不重复叠加推理 token。
若在配置里填写 `model_prices_per_million={"input":...,"output":...}`，报告提供近似费用估算；
无 usage 或无价格时成本显示 null，绝不声称是提供方实际账单。缓存读写费率需另行核对。
运行输出属于私人账户数据，放在已被 Git 忽略的 `artifacts/` 中，不要提交或公开。

## 本地验证与待验证范围

本次已运行官方 `first_backtest`，以及官方 `ai_researcher_trader` 的原生引擎＋脚本模型测试。
新增应用覆盖边界风控、异常数值、账户不一致、回撤锁重启、并发预留、停机未知订单、模拟端点及原生回测成交。
另使用真实 Yahoo 历史价格完成 2025-01-06 至 2025-02-01 的脚本决策回放，生成实际引擎成交、费用和双基准记录。
这是功能回归，不是 AI 收益证明。

未配置真实模型或 Alpaca Key；没有做真实模型研究、Alpaca 网络登录、Paper 下单／撤单／停机恢复或通知投递。
这些需要你在本地填写凭证后单独验证。没有向任何真实资金账户发送订单。
当前虚拟环境的 DuckDB 2.0 会在退出时打印 nanobind 引用泄漏诊断；测试退出码正常，但长期运行的内存稳定性仍待观察。

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
[Alpaca 行情权限](https://docs.alpaca.markets/us/v1.1/docs/market-data-faq)、
[LumiBot 混合 Agent 流程](https://lumibot.lumiwealth.com/agents_flows.html)、
[Schwab Developer Portal](https://developer.schwab.com/user-guides/individual-developer/become-individual-developer)。
