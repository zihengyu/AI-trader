# AI 模拟基金：Ubuntu 服务部署

本应用的显式秘密配置、无人值守进程管理、审批和日报运维说明。

**Last Updated:** 2026-11-14

**Status:** 部署生成器与进程／通知回归已验证；目标 Ubuntu 安装和 Telegram 实际投递待验证

**Audience:** 自行部署本地模拟账户或 Alpaca Paper 的操作员

## Overview

应用继续复用 LumiBot 原生框架，入口在 `scripts/autonomous_fund/`。默认研究用 DeepSeek Flash，
投资决策和复盘用 DeepSeek V4 Pro；只有结构化目标组合能进入独立 Python 风控。
`local` 是真实公开市场行情＋本地虚拟账户；`paper` 才是 Alpaca Paper 券商账户。
两者都没有真实资金入口。Schwab 仍只准备说明，不能通过 `.env` 启用。

以下命令中的 `<repo>`、`<service-user>` 必须替换成自己的 Linux 目录和已有的专用非 root 用户。
示例使用 Ubuntu 24.04 的 Python 3.12；实际 Linux 依赖安装、systemd 启停、重启及网络验收须在目标主机完成。
不要把 Windows 的 `.venv` 搬到 Linux；不要在部署过程中自动升级 LumiBot 主版本。

## 一键脚本（日常使用）

装好 Python 依赖（见下一节）之后，日常只需要六个脚本，都在仓库目录里**以普通用户运行**（不要加 sudo，需要时脚本会自己调用）。
服务以运行脚本的用户身份运行，例如 `ubuntu`：

| 脚本 | 作用 |
|---|---|
| `bash scripts/fund-start.sh` | 启动：检查 `.env` → 安装或更新三个 systemd 服务（交易、独立监控、数据员）和每日备份定时器 → 启动 → 等独立监控就绪（最多 3 分钟）→ 处于人工停止时询问是否恢复交易（`-y` 直接恢复）。旧版本装过的 `lumibot-fund-prefetch.timer` 会自动停用并删除（已被数据员取代） |
| `bash scripts/fund-restart.sh` | **更新代码后用这个**：重新安装有变化的服务文件 → 真正重启三个服务（加载新代码）→ 等到重启后的独立监控写出新状态 → 询问是否恢复交易（`-y` 直接恢复）。`--update` 先 `git pull --ff-only`（只快进，不覆盖本地修改）并按锁定版本重装依赖。`fund-start.sh` 对已经在运行的服务不会重启，所以更新后单独运行它看起来“没反应” |
| `bash scripts/fund-rehearse.sh` | **盘前彩排**：在交易服务完全相同的文件沙箱里，用 `FUND_MODE` 账户的副本（含数据员准备的数据）和真实行情完整走一遍决策；**默认用模拟 AI 回答每次模型调用，不调用模型、0 模型费用**（数据员状态 → 盘前研究 → 风控数据 → 因子研究 → 财报日历 → FRED → 反方审查 → 基金经理 → 决策校验 → 量化风控），在下单前停下，逐段报告耗时、本次彩排中每次模型调用的耗时和 token，并单列“模型回答是否可用”（格式不符、反方审查失败、复盘失败及位置）。在后台临时服务里运行，断开 SSH 不会中断，重连后用 `--show` 看结果。不碰真实账户、STOP、审批和 Telegram。`--real-models` 改用真实模型（会产生费用，换模型或改提示词后用），`--no-models` 只检查数据环节 |
| `bash scripts/fund-logs.sh` | 维护：默认显示运行概况（服务、交易／停止原因、监控、数据员各类数据的更新时间、账户、待审批、周报状态、日内交易次数、最近通知）；`logs` 三个服务的实时日志，`errors` 最近 24 小时内最后 80 条警告／错误记录（保留完整 traceback），`report` 账户报告，`approvals` 待审批；`decisions [YYYY-MM-DD]` 按纽约日期核对每次复核、订单、成交及调仓通知；`weekly` 打印本周到目前为止的周报（不发送）；`experiments` 实验登记表（生效、影子观察、未通过、退役及状态变化）；`prune` 预览可清理的旧资讯、追踪文件和导出归档（`--apply` 执行，每日备份后也会自动执行） |
| `bash scripts/fund-capital.sh` | **加钱／取钱（申购／赎回）**：`subscribe 10000` 加 1 万美元，`redeem 5000` 取 5000 美元，`cancel <编号>` 取消等待中的请求，不带参数查看份额、单位净值和等待中的请求。本地虚拟账户和 Alpaca 模拟盘使用同一套命令。基金在交易时段按单位净值确认，申赎金额不计入收益。详见 `AUTONOMOUS_PAPER_FUND.md` 的“申购与赎回” |
| `bash scripts/fund-logs.sh economics` | **只读经济对照**：新方案与维持旧目标的 1／5／20 日价格方案、估算交易摩擦、已准确关联的决策链费用和非重叠样本数；另列期初贝塔对照，保留固定基准。不新增模型调用或下载。缺少证据显示未知，不是实际成交收益或触发当日日内盈亏；周报也会显示截止时点的全历史评估。详见 `AUTONOMOUS_PAPER_FUND.md` 的“新旧目标的经济对照” |
| `bash scripts/fund-stop.sh` | 手动关停：先停止交易（人工停止，不会自动恢复，交易进程撤销本应用的挂单，默认等 60 秒；`--now` 不等），再停止三个服务；`--disable` 同时取消开机自启 |

第一次运行 `fund-start.sh` 时如果还没有 `.env`，脚本会从模板创建一份并提示填写 Key 后再运行。
服务器重启或程序崩溃后服务会自动拉起，交易在安全条件下自动恢复；用 `fund-stop.sh` 关停的，下次 `fund-start.sh` 时确认恢复。

更新代码并重启（重启会先把交易设为人工停止，监控就绪后确认恢复；`-y` 表示直接恢复）：

```bash
bash scripts/fund-restart.sh --update -y
```

**开盘前检查清单**（美股开盘 = 北京时间 22:30，夏令时 21:30；在那之前一两个小时做完）：

```bash
bash scripts/fund-logs.sh
```
```bash
bash scripts/run_autonomous_fund.sh check
```
```bash
bash scripts/fund-rehearse.sh
```

1. `fund-logs.sh`：三个服务 active，独立监控“新鲜”，“交易”一行是运行中（或会自动恢复），“数据员”一行是运行中、
   资讯和宏观几分钟到一小时内更新过，没有新的业务异常；
2. `check`：`connectivity_summary.blocking` 为空（Yahoo、Telegram、模型 API 都能连上）；
3. `fund-rehearse.sh`：全部“通过”。任何“失败”都带位置和原因，开盘前就能修；数据环节超过 45 秒的会注明在交易时段会分几轮完成，
   必需环节（账户快照、风控数据）超过 120 秒、单次模型调用超过 900 秒会直接判失败，因为开盘后监控会因此停止交易。
   默认的彩排用模拟 AI，不花模型费用，每天开盘前跑都可以。它验证的是数据、权限、账本、上下文、回答解析、决策校验、风控和下单规划，
   不验证真实模型的回答内容、耗时和格式（2026-11-03 的证据时间区间、超长摘要就属于这一类）；换模型、改提示词、
   或想看 AI 实际怎么回答时，再运行 `bash scripts/fund-rehearse.sh --real-models`（一次约调用 3–5 次模型：
   盘后复盘、盘前研究、反方审查、基金经理）。断线后重新连上，运行 `bash scripts/fund-rehearse.sh --show` 查看结果。

**模型调用的时间上限**：LumiBot 的 `run_timeout` 是每次尝试的上限，超时还会被当作临时错误重试，实盘默认最多 10 次，
一次调用可能拖到将近一小时，而独立监控在单次调用超过 900 秒时就会停止交易。启动入口因此默认 `LUMIBOT_AGENT_MAX_RUN_ATTEMPTS=2`，
研究员每次尝试 420 秒、反方审查和基金经理默认各 300 秒，两次都超时时分别约 842 秒和 602 秒。
研究员被要求控制在约 20 次工具调用内。
可在 `.env` 设置 `FUND_MANAGER_RUN_TIMEOUT_SECONDS=360`，将基金经理盘中决策和盘后复盘的每次尝试延长到 360 秒。
重启后生效。默认两次尝试都超时加 2 秒等待约为 722 秒。配置的超时和重试次数可能超过默认 900 秒监控阈值时，
启动会记录 `agent_time_budget_exceeds_monitor` 并提示。

## 数据员：常驻的数据整理服务

研究员和基金经理不再在模型调用里现拉数据，新消息也不用等到某个固定时间。第三个服务 `lumibot-fund-desk`（数据员）
一直在后台运行，只拉数据、整理数据、算好指标；它不调用模型、不下单，也不改 STOP、审批和决策：

| 数据 | 内容 | 多久更新一次 |
|---|---|---|
| 资讯 | Yahoo 个股新闻、SEC 8-K 公告、美联储发布、自定义 RSS，以及 AI 设定的 `news_watch` 搜索词，写进账本的资讯表；跟踪的标的包括持仓、最新目标和价格提醒 | 开盘时每 `FUND_NEWS_POLL_SECONDS`（默认 5 分钟），休市时每 20 分钟 |
| 盘中行情 | 可交易标的、大盘基准、持仓和价格告警的已完成一分钟行情、覆盖率及有效样本的涨跌家数 | 开盘时按 `FUND_DESK_INTRADAY_SECONDS`（默认 5 分钟）及标的数量安排整轮更新，必要时先补查关键标的 |
| 盘前行情 | 每个可交易标的和 SPY/QQQ/IWM/DIA 的盘前成交：最新价、相对昨收的涨跌、盘前成交量、领涨领跌 | 交易日美东 04:00 到开盘，每 15 分钟 |
| 全球市场 | 标普、纳指、罗素股指期货，VIX，10 年期美债收益率，美元指数，欧元、日元，原油、黄金，日经、恒生、欧洲斯托克 50 | 工作日休市时每 15 分钟、盘中每 30 分钟、周末每 4 小时 |
| 经济日历 | 未来三周 CPI、非农、GDP、PCE、PPI、零售销售、初请失业金、JOLTS、工业产出等公布日期（FRED，需要 `FRED_API_KEY`，免费；每天都有日期的“发布”是数据流不是事件，被排除），FOMC 议息决议日来自美联储官网的会议日历 | 每 6 小时 |
| 宏观 | FRED 美债收益率、期限利差、高收益债利差、VIX | 工作日 06:00–20:00（美东）每小时，其余时间每 4 小时 |
| 财报日历 | 每只股票的下一次财报日期（日期变了，财报前的仓位上限当天就生效） | 每 6 小时 |
| 日线 | 本地模式下交易范围、因子研究范围和基准的已完成日线（不含当天正在形成的 K 线；纸面账户模式用券商的数据） | 美东 06:00 以后才拉当天的（新一天的拆股、分红那时已计入），没拉全时每 15 分钟补一次，之后每小时检查 |
| 行情快照 | 由日线算好的收益、波动、beta、均线位置、成交额 | 每 15 分钟检查，有变化才重写 |
| SEC 财报 | 需要更新的公司基本面（需要 `FUND_SEC_USER_AGENT`） | 每 12 小时 |

结果写在账户目录的 `cache/daily/<交易日>/`（保留最近 3 天）和账本的资讯表，状态和心跳写在 `cache/desk/status.json`。
交易进程**先读数据员准备好的数据，只有缺的或过期的才自己联网补**；研究员和基金经理的输入里有 `data_desk`
（各类数据多久前更新、盘中行情、上次决策以来新增的资讯条数），并且可以在思考过程中随时调用 `desk_latest` 工具，
读到那一刻最新的盘中行情、快照、宏观、财报或资讯。重大资讯（AI 关注的搜索词命中、持仓公告、美联储发布、持仓新闻扎堆）
照常提前唤醒基金经理。

**交易优先**：两个进程从同一个服务器 IP 访问 Yahoo，所以数据员让着交易进程。它的盘中行情只下载当天的分钟线（交易进程下载 5 天），
请求之间停 0.3 秒。连续 3 次数据源失败就结束这一轮。Yahoo 已返回数据但没有已收完的分钟线时，单独记为该标的暂缺。
先查大盘基准、持仓和告警标的。若关键标的暂缺且本轮没有数据源失败，可在下一次全量更新前限速补查一次这些标的，其他报价保留原始时间。
交易进程自己的 Yahoo 请求一出错，就写下冷却时间（`cache/desk/yahoo_cooldown.json`）。冷却期间数据员暂停 Yahoo 请求，美联储、SEC、Google 新闻照常。
数据员服务的 CPU、磁盘优先级更低，内存紧张时系统先停它。

**过期和可疑的数据会标出来。** AI 每次读取盘中行情都按各标的的源时间校验。超过 15 分钟、时间无效或超前的报价会标为过期，
和疑似拆股等异常跳空一起排除出涨跌家数与领涨领跌。`coverage` 列出可用标的数、缺少的基准及涨跌家数的实际样本数和应覆盖数。
部分覆盖会显示在数据员状态中。刷新出错时，AI 也会看到最近的错误。超过 1 小时的盘前行情和超过 90 分钟的全球市场会显示过期和更新时间。
资讯会把不同来源的同一条新闻合并成一条（注明几家报道），并按重要程度排序：重大事件、持仓、关注词、宏观、目标、公告，
再按时间。

数据员停了也不影响交易：交易进程发现它连续 3 次没更新资讯就自己接着轮询，其余数据缺了也会自己拉，只是慢一些。
独立监控在数据员 10 分钟没有心跳时发一次 Telegram 提醒（6 小时内最多一次）；`fund-logs.sh` 的概况里有一行“数据员”，
显示每类数据几分钟前更新、是否出错。查看它的日志：

```bash
journalctl -u lumibot-fund-desk -f -o cat
```

不想要数据员时在 `.env` 里写 `FUND_DATA_DESK=false`（交易进程恢复原来的自己拉取方式）。没有数据员的机器上，
也可以手动把当天的日线、FRED、财报日历和 SEC 财报一次性拉好：

```bash
bash scripts/run_autonomous_fund.sh prefetch
```

**修正**：之前 `fund-rehearse.sh`、`prefetch` 没有使用 `FUND_MODE` 的账户目录，而是旧的默认目录 `artifacts/autonomous-fund`，
所以彩排复制的是一个新账户、预取的数据交易进程也读不到。现在都用 `FUND_MODE`／`FUND_RUN_DIR` 指定的账户；
更新后请重新彩排一次。

**更少、更好的模型调用**（不降低风控，也不限制 AI 的研究工具）：

- **行情快照 `market_snapshot`**：每个交易日由代码根据已完成日线算一次每个可交易标的的 1/5/20/60 日收益、20/60 日年化波动、
  相对 SPY 的 60 日 beta 和相关性、相对 50/200 日均线的位置、距 252 日高点的回撤和 20 日日均成交额，放进盘前研究和基金经理的上下文。
  提示词要求直接使用，不再用工具反复计算，把工具调用留给新闻、公告和证据。缺数据就是 `null`，从不填补。
- **反方审查复用**：同一交易日内研究结果、持仓和唤醒原因都没变时，复用上一次的审查（日志 `critic_reused`），不再重复调用；
  任何一项变化都会重新审查。
- **跳过无变化的定时复核**（`FUND_SKIP_QUIET_REVIEWS`，默认开启）：基金经理自己定的复核时间到了，但没有任何提醒、大幅波动、
  市场压力或新闻命中，上次决定的调仓已经完成且持仓完全没变（没有止损、止盈或其他成交），研究也还是那一份，就不调用模型，
  日志记为 `review_skipped`，一个复核间隔（`FUND_MIN_REVIEW_MINUTES`）后再检查。止损、止盈和价格提醒始终有效，有变化立即唤醒；
  同一交易时段内距上次模型决策满 `FUND_MAX_QUIET_MINUTES`（默认 120 分钟）必定调用一次，新的交易日第一次复核也必定调用。
- **模型费用核算**：在 `.env` 填好 `FUND_MODEL_PRICES_JSON`（按你账户的实际单价，含缓存命中价 `cached_input`）后，
  `fund-logs.sh` 和每日报告多一行“模型费用（估算）”：今天、近 30 天、日均、按此速度一年的费用，以及它占净值的比例，也就是
  AI 每年至少要跑赢基准多少才能打平模型费用。想封顶就设 `FUND_MAX_DAILY_COST_USD`：到上限后当天不再发起新决策、反方审查
  和盘前研究，止损止盈和盘后复盘照常。
- **彩排报告每次模型调用的 token**：`fund-rehearse.sh` 的结果里每次调用写明耗时和输入／输出 token（复用的写“缓存命中”），
  便于判断哪一步最费时、最费钱。

用 `fund-stop.sh`／`fund-restart.sh` 有计划地停止或重启时，交易进程退出只记录（`watchdog_noted`），不再发“监控停止交易”的 Telegram 告警；
没有人工停止时进程退出照常告警。概况里的“最近业务异常”只显示 24 小时内的，并注明多久前。

监控停止（例如“迭代卡住”）在 24 小时内最多自动恢复 3 次。次数用完后状态会写明“自动恢复次数已用完”，
`fund-start.sh` 和 `fund-restart.sh` 会像人工停止一样询问是否恢复：先用 `bash scripts/fund-logs.sh errors` 看原因。

## 安装及秘密配置

先把已审阅的当前版本代码放到服务器。使用既有专用用户；安装系统组件需要管理员权限：

```bash
sudo apt-get update
sudo apt-get install python3.12-venv python3-dev build-essential ca-certificates tzdata
cd "<repo>"
python3.12 -m venv .venv
.venv/bin/python -m pip install -c scripts/autonomous_fund/constraints-py312-linux.txt \
    -e . -r scripts/autonomous_fund/requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m scripts.autonomous_fund.deploy prepare --user "<service-user>"
```

`constraints-py312-linux.txt` 锁定了经过测试的全部依赖版本（Python 3.12、Linux x86_64），任何镜像源都会装出同一组版本，
避免 `ResolutionImpossible`。国内镜像缺少新版本时，换官方源 `-i https://pypi.org/simple` 或清华源
`-i https://pypi.tuna.tsinghua.edu.cn/simple`；不要去掉 `-c`。

装好后先确认服务器能连上所需的每个服务（只读探测，不调用模型推理、不下单、不发消息，输出不含任何 Key）：

```bash
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh check
```

`connectivity` 一节逐项给出 Yahoo、Telegram、模型 API、Alpaca、SEC、FRED、Finnhub 和标普 500 成分列表（raw.githubusercontent.com）的 HTTP 状态和耗时，
`connectivity_summary.blocking` 列出当前 `FUND_MODE` 的必需服务和配置失败。local 只探测可选的 Alpaca 行情，
paper 会检查必需的 Paper 账户凭证。local 只填一项 Alpaca 凭证时，`check` 会提示补齐。
**服务器在中国大陆地域时 Telegram 和 Yahoo 通常无法直连**，
请改用香港／新加坡等境外地域，或在 `.env` 设置 `HTTPS_PROXY`。设置了 Alpaca Key 时，local 模式在 Yahoo 拿不到日线时
会自动改用 Alpaca 的历史日线（只用于风控和因子，记录 `risk_history_fallback`），并且每天用 Alpaca 日线逐只核对 Yahoo 日线：
有分歧的交易日剔除，严重不一致的证券当天停用，收盘后发一条“日线数据交叉核对”通知（见 `docs/AUTONOMOUS_FUND_DATA_SOURCES.md`）。
备用及对照日线统一请求拆股与分拆复权，排除现金分红复权；旧判定自动重算，不需清缓存。
公司行动核对覆盖请求及缓存历史，分拆派股比例单列为证据，不自动改账。通知中的历史价差不是当日跌幅，待核对也不等于错账。
**建议 local 模式也配上免费的 Alpaca Paper Key**，这是目前唯一的免费数据核对手段。

`prepare` 只生成 `artifacts/fund-systemd/` 中的交易、监控、数据员三个服务 unit 文件和每日备份的 service／timer，创建模拟账户目录和 STOP 标记，
并在 `.env` 不存在时独占创建空值模板。生成的服务不写死模式：每次启动／重启都读取 `.env` 的 `FUND_MODE`
（`local` 本地模拟盘或 `paper` Alpaca 模拟账户，没有实盘选项）和 `FUND_RUN_DIR`（留空为 `artifacts/fund-<模式>`，
服务要求放在仓库 `artifacts/` 下），`prepare` 会打印服务将使用的模式和账户目录。它不覆盖已有 `.env`，也不安装或启动任何服务。
请在服务器本地编辑 `.env`，不要把密钥粘到聊天中。模板源是
`scripts/autonomous_fund/.env.example`，真实 `.env` 已被 Git 忽略。模板按“模型 Key 与模型选择、用户约束、审批额度、
Telegram、外部数据与运行参数、券商”分节，所有可调项都有中文说明和默认值；优先级为进程环境 > `.env` > `config.json`，
留空表示沿用 `config.json`，`.env` 中拼错的 `FUND_*` 名称会拒绝启动。完整变量表见 `docs/AUTONOMOUS_PAPER_FUND.md` 的 “`.env`：运营者配置入口”。
修改后用 `sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh show-config` 核对最终生效值和来源，再重启服务
（`.env` 归服务用户所有且权限为 600，所以基金命令都以服务用户身份运行）。

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | Flash 研究及 V4 Pro 决策／复盘，共用一个 Key |
| `FUND_TELEGRAM_BOT_TOKEN` | 自己创建的 Telegram Bot token |
| `FUND_TELEGRAM_CHAT_ID` | 已与 Bot 建立会话的接收者或群组；先在 Telegram 中启动 Bot |
| `FUND_TELEGRAM_APPROVER_IDS` | 可选；群组中可回复审批的用户 ID（逗号分隔）。私聊默认由聊天本人审批 |
| `FUND_TELEGRAM_APPROVALS` | 可选；设为 `off` 时 Telegram 只通知、不接受回复审批 |
| `FUND_AUTOMATION_MODE` | `full`（默认）：独立 AI 风控审批例外买入；`semi`：人工审批；修改后重启并在 `status` 核对加载模式 |
| `FUND_APPROVAL_MODEL` | 独立审批模型，默认 `deepseek/deepseek-flash`；可自行更换支持的 `提供方/模型ID`，不影响经理模型；重启后生效 |
| `FUND_SEC_USER_AGENT` | 可选；资讯监控读取 SEC 8-K 公告所需的联系人声明（姓名＋邮箱） |
| `ALPACA_API_KEY`、`ALPACA_API_SECRET` | paper 必填专用模拟账户凭证；local 选填一整对，用于日线核对及备用行情 |
| `ALPACA_IS_PAPER=true` | 仅 paper 读取，默认 true；设为 false 会拒绝启动 |
| `FUND_CAPITAL_USD` 等约束 | 授权资金、仓位、回撤、单日亏损、禁买名单、文字约束、审批额度与外部参数，见模板各节 |

本地虚拟账户不需要券商注册或 Key。其他模型与可选数据服务的变量见模板及
`docsrc/environment_variables.rst`。不要设置 `AI_MODEL`，除非有意同时覆盖两个角色的默认模型。
API 美元成本依赖配置中明确填写的模型费率；未知 usage 或缺少费率不会被记成零成本。

`.env` 由 python-dotenv 解析，绝不 shell `source`；已有进程环境优先。
普通 Python CLI 仅在传入 `--env-file` 时读取；Linux 启动脚本明确选择本 checkout 的 `.env`，
也允许用 `--env-file` 指定另一份文件。Linux 文件权限必须为 0600 或更严格，否则拒绝加载。
服务用户必须能读代码和 `.env`、写所选运行目录；服务运行时通过 systemd 将代码和秘密文件设为只读。

```bash
mkdir -p artifacts/autonomous-fund-account-locks
sudo chown -R "<service-user>" artifacts .env
sudo chmod 600 .env
sudo -u "<service-user>" .venv/bin/python -m scripts.autonomous_fund.run check \
  --run-dir artifacts/fund-local --env-file .env
```

`prepare` 以当前用户创建的账户目录、STOP 和锁文件必须整体交给服务用户（上面的 `chown -R`），否则服务首次写入 STOP
时会因权限不足退出，日志会给出同样的修复命令。之后修改 `.env` 用 `sudo -u "<service-user>" nano .env`。

`check` 只验证公开行情、源时间及 Key 是否存在，不调用付费模型，也不证明提供方鉴权成功。
它还会报告 `research_network`：若研究站点解析为 Fake-IP（198.18.0.0/15）或私有地址，网页研究会被安全检查拒绝，
按提示让服务器不走 Fake-IP，或在 `config.json` 设置 `"research_network": {"resolver": "doh"}`。
如将来选择 Alpaca Paper，先在本地 `.env` 填写专用 Paper Key，把 `FUND_MODE` 改为 `paper`，为服务用户准备新账户目录，
再一起重启三个服务；账本自动换到 `artifacts/fund-paper`，不会用 `fund-local` 的账本连接券商账户：

```bash
sudo install -d -o "<service-user>" -m 700 artifacts/fund-paper
sudo systemctl restart lumibot-fund-runner.service lumibot-fund-monitor.service lumibot-fund-desk.service
```

重启后同样保持 STOP，核对后再 `resume`。也可用 `deploy prepare --mode paper --run-dir artifacts/fund-paper`
生成写死模式的服务；此时若 `.env` 的 `FUND_MODE` 与之不同，服务会拒绝启动并提示。首次 Paper 启动时账户不能持有投资范围内的证券或未完成订单，
现金不少于 `mandate.capital_usd`；超出部分自动记为 AI 不能动用的保留现金。要让 AI 管理的规模变化，用
`bash scripts/fund-capital.sh subscribe <金额>`／`redeem <金额>`（不用停服务，钱在保留现金和 AI 之间划转；模拟盘没有真实转账）。
以后实盘的钱真的进出券商时，先停止交易服务再用 `deposit <金额>`／`withdraw <金额>` 登记。
如果父目录不允许服务用户遍历，需由管理员调整目录权限；不要用放开所有文件读写的方式解决。
可选浏览器的安装和外部数据订阅仍按 LumiBot 原有说明配置；缺少这些工具会影响研究覆盖面。

## 审阅并启动服务

```bash
systemd-analyze verify artifacts/fund-systemd/lumibot-fund-*.service
sudo install -m 644 artifacts/fund-systemd/lumibot-fund-runner.service /etc/systemd/system/
sudo install -m 644 artifacts/fund-systemd/lumibot-fund-monitor.service /etc/systemd/system/
sudo install -m 644 artifacts/fund-systemd/lumibot-fund-desk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lumibot-fund-runner.service lumibot-fund-monitor.service lumibot-fund-desk.service
systemctl status lumibot-fund-runner.service lumibot-fund-monitor.service lumibot-fund-desk.service
journalctl -u lumibot-fund-runner.service -u lumibot-fund-monitor.service -u lumibot-fund-desk.service -n 60 --no-pager
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh notify-test --run-dir artifacts/fund-local
```

首次交易进程建立 SQLite 后，监控即可连接；首次启动竞态可能导致监控退出一次，systemd 会重试。
先确认 Telegram 手机实际收到测试通知，并检查 `artifacts/fund-local/monitor.json` 的
`health` 是 `stopped` 或 `healthy`、心跳新鲜、`telegram_configured=true`。
模型或行情报错时先修复，不能通过反复 resume 绕过故障。

```bash
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh resume --run-dir artifacts/fund-local
```

之后普通模拟交易无需审批；开盘后的首次可用迭代直接唤起基金经理评估组合，
以后按 AI 的下次复核时间（不早于 15 分钟）及盯盘触发条件复核。
盘中不再先同步调用研究员：经理收到已有预研及其原始时间；预研缺失、无效或属于更早的交易日时，
明确标注，由经理使用自己的只读工具核查行情和风险。研究员失败不再推迟经理评估。
只有经理调用或决策验证失败才设置盘中 5／15／60 分钟退避；每日 Agent 预算仍然有效。
升级后的 runner 会根据账本归属解除旧版“仅研究员失败”留下的共享退避，并记录
`research_backoff_detached`；经理失败和无法确认归属的退避仍保留。更新后需要重启 runner。
所有服务启动／重启都保留或设置 STOP。`prepare` 写入的首次安装 STOP 和 `stop` 命令写入的 STOP 必须核对后显式 resume；
手动关闭服务（`systemctl stop`／`restart`）同样记为人工 STOP，不会自动恢复。
异常关闭——进程崩溃、被系统杀死、断电后重启、操作系统关机或重启——造成的 STOP，由独立监控在交易进程连续健康 3 分钟、没有状态不明的订单预留后自动解除
（24 小时内最多 3 次，`FUND_AUTO_RESUME=false` 可关闭），并通过 Telegram 通知；磁盘不足、版本不符等故障仍需人工处理。
服务显示 `active`、runner 心跳新鲜，并不表示已解除 STOP：恢复次数用完后，
进程仍会监控和记账，但不会下新单。相同 STOP 期间保留最先发生的存活故障及时间，
不会被监督进程随后引起的 `runner_exited` 覆盖；磁盘不足等更强故障和人工停止仍优先。
先用 `bash scripts/fund-logs.sh errors` 查看完整堆栈，修复后再重启和确认恢复。
若需检查某个时间段的原始日志，分别读取监控服务与交易服务；不要对包含大型账户报告的
合并日志只取最后几百行，否则真正的异常可能被报告挤出。

AI 研究超过调度间隔时，`MaxInstancesReachedError` 表示上一轮仍在执行，本应跳过重叠任务。
已修复的一条实际停机路径是：记录这条警告时，LiteLLM 日志过滤器发生 `KeyError: 'litellm'`，
异常逃出并终止调度线程，随后监控判定迭代停止。应用现在保留正常日志过滤；过滤器异常时只输出
`scheduler_logging_error` 和异常类型、隐藏原始记录，并继续调度；不增加并发迭代，不吞掉模型或交易异常。
该提示同时保留安全的异常位置、原因链和 OS 错误码，便于定位依赖过滤器故障；不会打印原始日志内容、输入值或局部变量。
`fund-logs.sh errors` 会将结构化回答校验的字段位置及缩进详情保留在同一条异常记录中，避免只看到
`1 validation error` 而丢失原因。自定义异常名不以 `Error`／`Exception` 结尾或没有消息时，也保留最后的原因行及异常链；
仅在同一进程的 traceback 内识别这类终行，普通成功日志不因此进入异常摘要。
AI 调用原生 `notify_user` 使用本账户的持久化队列，队列接受不等于 Telegram 已送达。
监控收到正常停止／重启的 SIGINT 时也会正常退出，不再把 `KeyboardInterrupt` 当成崩溃堆栈输出。

宏观摘要的 `macro.enabled` 只控制应用预计算的 FRED 市场摘要，
不会关闭或替换 LumiBot 原生 FRED 研究工具；直接调用这些工具仍需显式配置 `FRED_API_KEY`。
详见 `docs/AUTONOMOUS_PAPER_FUND.md` 的“停止、恢复与异常通知”。
休市时不撮合；恢复后按原生市场调度运行，盘前研究和盘后复盘在对应钩子触发。

服务以非 root 用户运行，退出后延迟 30 秒重启，启动次数不设永久锁死上限。
子进程卡死也由独立进程检测；关闭先发 SIGINT，30 秒未退出再强制结束。
交易子进程带 `--require-monitor`：独立监控心跳超过 45 秒、状态异常或未配置 Telegram 时，
每次提交前及每次本地模拟成交前都会拒绝执行。监督进程还会回收失去监控或卡住的子进程。
监控每轮的 Telegram 通知和回复共用 20 秒处理预算，两个队列轮流优先；已开始的单次请求
仍受 10 秒网络超时约束。预算用完就留到下一轮，审批和状态消息的游标只推进到已处理的位置。
监控重启、观测间隔超过 45 秒、时钟回退或交易进程重启，都会重新累计连续健康时间；
不会把无人监控的空档算进恢复所需的 3 分钟。
盘中等待下一轮时，卡住阈值取原 180 秒与已记录迭代间隔加 10 秒中的较大值，
因此合法的 300 秒迭代间隔不会在下一轮到期前被误停；已经开始的迭代仍按原 180 秒检查，
模型调用仍按原调用超时检查。缺失旧版间隔记录时保留原阈值。
状态中的“交易：运行中”要求交易进程处于 `running` 且心跳在 180 秒内；没有心跳、
已经退出、心跳过期或时间异常会明确显示未确认、未运行或状态未知，不能仅凭没有 STOP 判断运行正常。
STOP、回撤锁、审批状态、模糊订单、每日订单计数、单日亏损闸门、经理失败退避和每日模型预算都存在原账本中，重启不会清空。
券商端已挂出的 Paper 订单可能在断线／撤单确认前成交；恢复时必须核对，不能假定停进程等于已撤单。

## 审批、日报和日常检查

**Telegram 每天会收到的消息**（时间均同时显示美东时间和服务器本地时间）：

| 时间 | 消息 |
|---|---|
| 开盘前约 1 小时（休市期间重启后也会补一次） | 📊 盘前研究：AI 的研究摘要和证据来源；内容太长时作为文本附件 |
| 开盘后第一轮 | 🔔 美股开盘：今天是否交易（暂停时写明原因和恢复方法）、账户净值／现金／持仓、盘前研究要点、收盘时间 |
| 盘中，AI 调整组合时 | 🧭 AI 调整目标组合：计划买入／加仓、计划卖出／减仓、不变的证券和权重（含入场价、止损、止盈）、目标现金比例；随后单列当前实际持仓（股数、买入均价、账户净值和现金、快照时间），再显示理由和下次复核时间。增删证券、权重变动至少 1 个百分点或交易价位变化时发送，每天第一次复核也发一次；权重不变但入场价、止损、止盈或止盈后保留权重变化时显示「AI 更新交易价位」，撤销价位也会说明 |
| 确认成交后 | ✅ 成交后账户更新：新的实际持仓、现金、净值及最近 6 笔确认成交；相邻成交可合并通知，重启通过持久化游标接续，不重复入队。60 秒仍无成交后账户快照时先发送确认成交并注明快照待更新，取得新快照后再补账户更新 |
| 盘中按需 | 审批请求（回复「同意／拒绝」）、止损触发、停止／自动恢复、异常告警；组合构建方式自动切换时（🔁，附配对检验结果） |
| 盘中，长时间没有有效决策时（每天最多一次） | ⚠️ 进程健康但没有做出决策：开盘 90 分钟后今天还没有有效决策，或距最近一次决策自己定的下次复核时间已超过 3 小时。附今天的决策失败重试次数 |
| 收盘后复盘完成 | 盘后日报附件：当日表现、成交、当日归因、记分卡、因子检验、复盘结论和下一交易日策略备忘 |
| 复盘后，对照实验退化或换手异常时 | 🔁 学习对照或组合构建的退化回滚（最近 20 个交易日转为不利）；⚠️ 换手偏高、复核间隔加倍，以及恢复正常的通知 |
| 每周最后一个交易日复盘完成后（北京时间一般周六 04:00–05:30；那天没有复盘时，下周第一个交易日开盘后用本周最近一次复盘补发） | 📅 周报附件：本周收益与风险、相对基准、交易成本和模型费用、归因合计、预测与研究想法打分（按把握分组）、因子实验进展、对照实验状态、异常和缺失数据、AI 复盘要点、下周关注的假设；分别标明事实、统计推断、模型判断和待验证假设，没有数据的部分写“无数据” |

盘前时间按交易所日历计算，与服务器时区无关。北京时间在美东冬令时约 21:30 开始研究，
夏令时约 20:30 开始；通知在生成完成后发送。旧版公共 Broker 时钟会把本地时间误当成 UTC，
在北京时间服务器上可能误等到下一交易日；更新修复后需要重启 runner，让已进入的长时间等待重新计算。
保留原账户目录及 STOP／订单状态，不要通过清空账本恢复。详见
`docs/investigations/2026-11-02_BROKER_MARKET_CLOCK_TIMEZONES.md`。

停止、恢复、报错等通知都是中文说明，末尾带事件编号和类型，便于对照 `bash scripts/fund-logs.sh` 的记录。
`healthy` 和新鲜心跳只表示进程健康；业务轮次仍可能失败。`status` 会显示最近业务异常的阶段、
系统错误码和模块／函数／行号，以及已记录的 AI 失败退避时间；完整的安全诊断位于日志和账本中的
`operation_error`。同一种告警会去重 30 分钟，不应把没有新告警当成业务已经恢复。
价位提醒的 `price_alert_unavailable` 会区分报价过期、源时间超前和价格无效，并说明是否只取得完成分钟线的回退报价。
实际失败检查次数及首次／最近失败时间另存为 `price_alert_health`，不受通知去重影响；概况显示仍在活动且尚未记录恢复的提醒。
取得有效报价后发出 `price_alert_recovered`，即使尚未越过提醒价位也会报告恢复；重启保留该状态。
移除提醒不算报价恢复，恢复也不补认遗漏的价格触发。记录间隔不能当作持续故障时长，旧版本未记录的检查不追补。
Telegram 的状态查询同样显示这些活动提醒；周报状态查询失败不会隐藏提醒故障信息。

逐标的排查可运行以下只读命令，将 `SYMBOL` 换成需要检查的代码：

```bash
bash scripts/fund-logs.sh quotes
bash scripts/fund-logs.sh quotes SYMBOL
```

该命令按 `FUND_MODE`／`FUND_RUN_DIR` 选择账户，只读取已保存的进程快照、活动提醒和盯盘检查记录，
展示所选报价、推送和图表成交的源时间、当次检查时的年龄、最近请求失败及尚无恢复证据的提醒。
它不下载行情、不调用模型或唤醒复核。快照过期／时间超前、模式不一致、损坏或缺失会明确提示；
缺少逐标的记录不等于行情正常，也不会借用另一账户或 local 模式的快照回答 paper 模式问题。
源时间年龄相对记录中的 `checked_at` 计算，不能把新鲜心跳或查询时间当成新鲜报价。
另列美东当日持仓／目标盯盘与价位提醒各自的检查次数、通过率、拒绝原因、失败后首次通过次数和源报价年龄分布。
这些是已保存检查的抽样统计，包括复用报价；流动性等交易限制也会造成拒绝，不能当作行情源可用率。
旧检查不追补，无记录或统计不可读均不等于正常；崩溃或统计保存失败可能缺样。
日报和周报按所选复盘截止时间读取对应日期范围，后续检查不会混入；SQLite 备份保留这些记录。
`quote_quality_error` 表示统计未完整保存，盯盘仍继续；使用 `errors` 核对，不要据此推断已经恢复。
概况的“决策”一行显示今天有几次有效决策、最近一次的纽约时间和累计次数。
这里的“有效决策”指通过结构与权限校验的 AI 复核，不代表下单、成交或投资判断已经被证明有效。
概况另外列出首次记录、调整组合、调整价位、小幅权重变化和维持组合的次数，以及实际订单、成交和通知送达状态；
累计和当日次数按完整账本统计，不再只看最近 200 条。
例如一天复核多次但一直维持同一组合，可能只收到当天首次的一条通知；不能仅凭通知数量认定其余复核无效。
需要核对具体记录时，运行 `bash scripts/fund-logs.sh decisions` 查看纽约当天，或
`bash scripts/fund-logs.sh decisions YYYY-MM-DD` 指定日期（把日期替换成实际纽约日期）。
这个查询只读取 `FUND_MODE`／`FUND_RUN_DIR` 选定的账户，不调用模型、不下单，也不会创建新账本。
每次复核下面列出：是什么叫醒了基金经理（资讯标题、价格异动、价格提醒、市场压力、止损、申购赎回），快速模型的事件判断及理由（被抽查的“不需要”会标出是否算漏判），以及基金经理理由的开头；同一天买入又卖出时，用它看清每次改变的原因。
新日志用计划 ID 关联每次复核、订单和通知；旧日志仅在完整原决策精确匹配时关联，不能确认的记录明确说明。
投递状态是查询时的状态，能区分未发调仓通知、已送达、等待发送和等待重试；无成交但无原因记录时不推测模型失效。
通知入队与去重基线现在在同一个事务内提交：写入失败会全部回滚，后续相同计划仍能重试通知。
Telegram 开盘／调仓通知与 `fund-logs.sh` 概况使用同一个“📌 当前实际持仓”区块，每只证券单独一行，
开市时净值金额后简写快照距今时间，如“（30秒前）”“（2分钟前）”；休市统一显示“（休市）”，查询和通知延迟不会重置快照时间。
金额保留两位小数。买入均价来自本地账本或券商返回的实际持仓，外部持仓不混入 AI 授权资金组合；
旧账户快照尚未记录均价时显示“暂无”，更新后下一次账户观测会补齐，不用目标价或现价倒推。
本地／Paper 在 AI 返回后、调仓通知入队前重新读取账户；读取失败明确显示快照不可用，不回填决策前的旧持仓。
目标权重及入场限价只代表计划；已发出的计划通知保留其历史含义，成交后另发账户更新，不提前把持仓改成目标值。
短债 ETF 仍单列为现金等价物，其市值已经包含在现金内，不重复计入证券持仓。
持仓区块下还显示快照时间之前最近 6 笔已确认的买入／卖出股数、成交价及观测时间；
券商恢复的整单累计成交均价会替代该订单的分笔回调，避免重复显示，其他分笔成交照实保留。
没有已确认成交时明确说明，不拿委托限价、目标价或当前市价当作成交价。
原有调仓理由、止损止盈、下次复核和审批说明全部保留，调仓理由不再截到 1,500 字。
超过 Telegram 文本长度时发送完整文本附件，附件标题仍展示调仓与实际持仓摘要；完整内容不删减。
心跳健康不代表在做决策：模型每次重试都失败、订单一直未完成或逻辑故障时进程仍然健康，所以监控另外检查有效决策（`decision_drought`）。STOP 期间、当天模型次数或费用预算用完、只观察运行和休市时不算；STOP 解除后重新计 90 分钟。

原生 AI 记忆默认位于 `<工作目录>/.lumibot/memory`，不在服务允许写入的 `artifacts` 中。
旧入口漏配记忆目录，会在模型调用前加载 `MemoryStore` 时触发 `EROFS`（只读文件系统），
同时影响研究员和经理。启动入口现在默认设置 `LUMIBOT_MEMORY_DIR=<账户目录>/memory`，
与行情缓存分别管理；显式配置的非空 `LUMIBOT_MEMORY_DIR` 仍优先，服务使用的路径须在其允许写入的目录中。
更新并重启 runner 才会采用新默认值，无需放宽 `ProtectSystem` 或删除账本。
2026-11-03 的盘前彩排又发现同类问题：AI 的 SEC 财报工具（`get_company_facts`）、FRED 工具和交易日历缓存默认写在
`~/.lumibot`、`~/.cache` 下，而服务把家目录设为只读，于是工具报 `EROFS`。启动入口现在把未设置的
`LUMIBOT_SEC_CACHE_DIR`、`LUMIBOT_FRED_CACHE_DIR`、`LUMIBOT_TRADING_DAYS_CACHE_DIR` 也指向 `<账户目录>/cache/`，
并把 `FUND_SEC_USER_AGENT` 传给 AI 的 SEC 工具；彩排的“研究工具缓存目录可写”一项专门检查这些目录。
`status` 优先显示实际抛出的系统错误码及非标准库调用位置，递归建目录时先前处理过的 `ENOENT`
不再覆盖最后的 `EROFS`。故障证据和验证边界见
`docs/investigations/2026-11-03_FUND_SERVICE_MEMORY_STORAGE.md`。

卖出和减仓始终自动执行。买入在每日自动额度内（总敞口较开盘增加不超过 25%、买入现金流不超过净值 40%）
直接执行；只有超出额度的买入，或市场压力（SPY 大跌／高波动）下的买入，才生成一份覆盖整个调仓计划的审批请求。
默认 `FUND_AUTOMATION_MODE=full` 由独立风控 AI 复核例外；设为 `semi` 才等待人工回复审批。
`FUND_APPROVAL_MODEL` 单独选择审批模型，默认 `deepseek/deepseek-flash`，可自行更换；不随经理模型切换。
自动额度内的部分仍可先执行，所以待审批不代表所有买入都停止。AI 超时、格式错误或拒绝不会变成批准，
并继续受模型预算和持久化重试限制；批准后下一轮再独立刷新账户／报价并重算风控。
通知包括触发原因、拟买入证券及权重上限和限价上界（±2% 领口）、批准后最高敞口、当日买入现金流上限、
组合波动和 ES、压力详情及 AI 的理由，长理由用文本附件保留全文。一份批准覆盖该计划的多笔买入，
有效期最长 60 分钟。新证券、更高权重、新触发原因、价格超出领口或目标变更时重新审批。
审批不放宽量化约束、单股 10%、总敞口 70%、每日 20 次提交、单日 50% 敞口增幅和 8% 回撤锁。
详细原则见 `docs/AUTONOMOUS_PAPER_FUND.md` 的“量化风控原则”和“例外审批”。

```bash
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh approvals --run-dir artifacts/fund-local
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh approve --run-dir artifacts/fund-local \
  --approval-id "<reviewed-id>" --approval-digest "<reviewed-digest>"
```

拒绝时将 `approve` 改为 `reject`。更方便的方式是在 Telegram 里**直接回复审批提醒「同意」或「拒绝」**，
或发送「同意 代码」／「拒绝 代码」（代码是提醒里的 6 位字符）；只有一个待审请求时也可以直接发「同意」。
只接受配置的会话和审批人；早于请求的消息、STOP 期间、过期或已被替换的请求都会被拒绝，机器人会回复处理结果，
每条指令都记入账本。发送「状态」可查看监控状态、待审批数、分仓净值和周报状态。请给 Telegram 账号开启两步验证。

每次成功完成盘后复盘，独立监控自动发送对应日期的 Markdown 日报附件：账户与现金／持仓、
观测盈亏与收益率、对齐窗口的 SPY／QQQ、运行至截止的回撤和费用、模型 usage、订单／成交／审批记录、
选股与择时调整、验证和失效条件、次日策略备忘及数据缺项。
不是交易日不虚构日报；停机、STOP 或模型失败可能导致复盘尚未完成，应查看异常通知和日志。
运行一段时间后执行 `bash scripts/run_autonomous_fund.sh acceptance --run-dir artifacts/fund-local` 生成验收报告
（`acceptance.md`）：连续交易日数、迭代完成率、监控 STOP、未结束委托、成交明细、模型失败率、复盘完成率和平均滑点都达标后，
再考虑进入下一阶段。同时生成投资能力验收报告（`investment_acceptance.md`），回答“扣费后有没有超额收益”；
两份报告互相独立，运行验收通过不代表投资能力验收通过。
本地文件仍在 `reviews/YYYY-MM-DD.md`、`postmarket_review.md`、`report.json`、`equity.csv`、`fills.csv`。

日报从固定账本生成，不读取 AI 指定的文件路径；原始研究和估值时间保留，缺少收盘估值会明确披露。
日报保留冻结输入的盈亏，另列报告截止前最新账户观测；现金等价物包含在现金中，展示自由现金时只扣一次。
新闻复核汇总数量并列最近 20 条，保护性触发全部展示，原记录没有删除。成交假设仅取自已保存的成交事件。
周报纳入所选的最新复盘，即使写入比完成时刻略晚；其他未来事件仍排除。基准旁增加扣已知模型费后超额，
未定价调用标为上界；交易费用已经进入账户净值，数据费用看完整系统净成本口径。
新复盘随完成记录保存实际输入记分卡，避免监控抢先读取时显示缺失；旧记录缺失的证据不会补造。
周报和投资验收还显示调用归因覆盖率：无编号旧记录的支出仍列入整体已知费用，不猜测其用途或采用结果。
按用途分别列已采用、失败、明确未采用及采用未知的已知费用；未定价次数另列，目标变化不等于收益贡献。
`economics` 及周报的经济对照分别显示同一组样本扣费前、调用链费用和扣费后，再列费用完整且区间不重叠的结果。
不能把全部方案的均值减去费用完整子集的均值，解释为费用造成的损失；两组样本可能不同。
更新代码后可用 `bash scripts/fund-logs.sh weekly` 从现有账本重建周报并核对最新备忘，只打印、不重发通知。
监控离线后重启会发现已保存的复盘并补发，发送失败进入持久化退避队列。
Telegram 接收确认丢失时重试可能重复，不保证恰好一次；同一天文件名可以用于核对。

```bash
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh report --run-dir artifacts/fund-local
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh stop --run-dir artifacts/fund-local
sudo systemctl stop lumibot-fund-runner.service lumibot-fund-monitor.service lumibot-fund-desk.service
```

**盘中事件判断**：`bash scripts/fund-logs.sh` 概况里的“盘中事件判断”一行显示它在影子观察还是已生效、最近漏判次数和今天的判断次数。
默认直接生效，每 5 次“不需要”仍抽查一次基金经理；漏判变多会自动退回影子运行并发 Telegram 通知，达标后再自动生效。
不想用可在 `.env` 设 `FUND_TRIAGE=off` 后重启交易进程。

**持仓被收购或退市**（本地账本）：收到“持仓 X 已连续 N 个交易日没有新的日线”的通知后，先查明实际结算金额
（现金收购价；破产退市一般为 0），停止服务，结算，再启动。结算命令只用于本地账本，运行中会被运行锁拒绝；
它会用严格对账复核整个账本，不一致就整笔回滚。Alpaca 模拟账户由券商自己结算，不用这个命令。

```bash
sudo systemctl stop lumibot-fund-runner.service
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh settle-position --run-dir artifacts/fund-local --symbol <代码> --cash-per-share <每股实收现金> --reason "<发生了什么>"
sudo systemctl start lumibot-fund-runner.service
```

`runtime.log` 按 20 MB 轮转，保留 5 个历史文件；journald 的磁盘额度由服务器管理员设置。
SQLite 审计账本不会自动删除；监控发现运行分区剩余不足 200 MB 时会写 STOP 并通知。
定期检查磁盘、内存、模型账单、通知队列、时间同步和服务重启次数；长期内存稳定性需实际观察。

**CPU 一直满载**：2026-11-03 的满载来自一次彩排：研究员 AI 用 `duckdb_query` 工具执行了一条自己写的 SQL（计算整个投资范围的近期收益和波动），这条查询一直没有结束，DuckDB 的原生线程占满了两个核心 12 小时；DuckDB 没有语句超时，Python 层的超时也停不下原生代码。现在 AI 的 DuckDB 查询最多只用 1 个线程、1 GB 内存，超过 60 秒由 DuckDB 自己中断并告诉 AI 简化查询；交易进程里的研究员和基金经理同样适用。彩排的临时任务最多运行 30 分钟、CPU 优先级低于交易进程，`fund-logs.sh` 的概况在彩排仍在运行时会提示。旧版本脚本启动的彩排没有固定任务名，用 `ps -ef | grep "run rehearse"` 找到进程号后 `kill` 结束，只影响彩排副本。另外，2026-11-03 之前的版本里，交易进程每秒查 3 次开盘／收盘时间，每次都重新计算一遍交易所日历（含节假日规则），空闲时也会占满一个 CPU 核心。现在每天只算一次（`CachedMarketHours`），同样条件下从约 32% 降到 1% 以下。更新后仍然很高时，用下面的命令看是哪个进程：

```bash
ps -eo pid,user,pcpu,pmem,etime,args --sort=-pcpu | head -12
```

```bash
systemctl status lumibot-fund-runner lumibot-fund-desk lumibot-fund-monitor --no-pager | grep -E "●|CPU:|Memory:"
```

如果占用最高的不是 `scripts.autonomous_fund` 的进程，而是不认识的程序，应按服务器安全事件处理（检查登录记录、计划任务和对外连接），不要只重启了事。

## 行情轮询与可靠性的边界

本地撮合轮询每 5 秒，默认策略调度每 30 秒（可配置）；AI 按自己的复核时间运行，并非每次轮询都收费推理。
本地轮询在开市时约每 30 秒独立计算授权资金净值，账本／成交变化时立即尝试刷新，不等待正在运行的 AI。
净值为实际现金加持仓市值和应收款，排除保留资金与账户外持仓；现金等价物仍归入现金。
开市报价过时或读取失败时保留上次成功快照及源行情时间，`fund-logs.sh` 和 Telegram「状态」显示估值异常。
开盘首分钟若某只持仓只能取得上一交易日的报价，会发出「开盘估值等待」通知。后台估值会在下一次 5 秒轮询尝试更新，组合复核在策略迭代里继续检查。
`fund-logs.sh errors` 可查看被拒报价的标的、来源、源时间和年龄。首分钟之后若仍缺合格报价，会按普通估值失败报告。
收盘时用最后一根完整常规时段分钟线保存一次收盘估值；纯现金和持股均冻结至下次开市，重启、复盘不会重复刷新。
缺最终分钟线时保留旧值并说明收盘估值缺失；数据员拉取资讯本身不更新账户净值。
本地买入、卖出各扣成交额 0.1% 模拟费用，现金和净值已扣费，价差／滑点另体现在成交价里；
Alpaca Paper 使用券商回报的费用，未提供时保持未知。
价格提醒可只读跟踪交易名单外的指数等符号，需有新鲜、带源时间的价格；缺失时披露并重试，目标和订单仍严格遵守交易名单。

用 `bash scripts/fund-logs.sh audit` 对当前配置选定的本地账户做只读账务核对（不需要停止服务）。
输出包含现金／持仓重放、订单与成交累计、费用、公司行动、净值加总和逐笔执行／滑点证据。
`verified`（退出码 0）表示已记录账目内部一致；`partial`（退出码 3）列明旧记录缺失的凭据或报价；
`failed`／`unavailable`（退出码 2）表示存在差异或账本不存在。不会自动修账或改写旧成交。
完整审计仍核对所有历史净值，输出 `scope: ledger_and_observations`。
账户读取、下单和成交前后的实时核对输出 `scope: ledger`，每次重验权威账目，避免重复扫描累计净值历史；
监控通知和指标逐个验算最近最多 200 个候选快照，没有合格快照就保持待更新／不输出净值指标。
此优化不放宽资金、股数、成交、费用或行情新鲜度检查；委托和成交记录很多时，完整账目重放仍有开销。
重放的开销与成交数成正比（5,000 笔成交约 0.25 秒；以前分红越多越慢，约 2 秒），
且只在估值到期、价格可能成交、出现新成交或下单时运行，不再每 5 秒轮询都重放；
历史净值快照逐条检查，不再整体载入内存；每 5 分钟的报告、决策上下文、盘后复盘和周报也只读取需要的快照
（每天最后一条、前一交易日基准等），彩排只读本次运行的记录。
长期运行后若 CPU 或内存仍持续偏高，
先用 `fund-logs.sh audit` 看 `orders_checked`／`fills_checked` 的规模，再按上文“CPU 一直满载”一条排查其他进程。
资金、股数、成交或费用不一致时，账户工具和报告停用争议信息，撮合阻止执行并由监控停交易；
研究可读取明确标为未核实的证据继续调查。历史净值快照加总错误单列为 `partial` 警告，
报告、通知和复盘跳过该快照，当前账本一致的买卖可以继续。旧记录缺滑点凭据也不阻止正常交易。
短暂断网、缺价或过时行情只让订单等待合格行情自动重试，不触发永久停交易；
不同证券的合格卖单继续撮合，限价、有效期、费用和新鲜度校验均保留。新增买入仍需可靠的全组合估值，
公司行动尚无法核实时也会等待，避免错用拆股前价格。
旧委托限价不再计作到达价，缺实际提交报价时滑点与择时分解显示未知，而不是 0。
本地报价按 Yahoo 新鲜推送、同次响应的最新成交价／源时间元数据、已完成分钟线的顺序回退，保留披露的点差代理。
当前分钟内的合格推送会直接返回，避免先等历史下载；撮合仍独立要求实际新分钟线，报价有效期没有放宽。
`status.json.market_data.quotes_by_kind.chart_quote` 统计最新成交元数据回退；仅在源时间合格的常规时段采用，
不将正在形成的分钟线或下载时间当作实时价格证据。模拟成交仍按完整新分钟线加滑点撮合，不是交易所逐笔执行。
开市分钟数据缓存 15 秒，休市 300 秒，日线 900 秒；相同系列并发请求只下载一次，
不同证券最多 2 个网络下载并发，失败从 15 秒退避至最多 300 秒。
缓存不改写源时间，不用旧数据冒充新报价；执行仍要求源时间新鲜。缓存指标可看 `status.json.market_data`。
全市场持续逐笔行情需要另选有授权的专业数据源和相应 Key，再通过独立阶段验收，不能只缩短轮询解决。

systemd、监督和持久化降低中断风险，无法保证永不故障。整机断电、磁盘损坏、网络断开时，
同机 Telegram 监控也可能无法通知；如需覆盖整机故障，另配服务器外部可用性告警和异地加密备份。
不要同时在旧电脑和新服务器运行同一个账户；本地文件锁不能跨机器防止双实例。

## 迁移、备份与验收

先停止旧进程和监控，核对挂单，再生成一致 SQLite 快照；不要直接复制正在写入的数据库文件。

```bash
.venv/bin/python -m scripts.autonomous_fund.deploy backup \
  --run-dir artifacts/fund-local --output-dir "artifacts/<new-backup-folder>"
```

快照保留账户绑定、风控／审批／订单状态，始终带 STOP，不含 Key。

**每日自动备份**：`fund-start.sh` 同时安装并启用 `lumibot-fund-backup.timer`，每天美东 17:45（收盘和复盘之后，随机延后
最多 5 分钟；服务器关机错过时开机补做）对当前账户做一次一致快照，存到 `artifacts/fund-backups/<UTC 时间>/`，保留最近 14 份，
只删除它自己创建的快照目录。手动执行：

```bash
sudo -u "<service-user>" .venv/bin/python -m scripts.autonomous_fund.deploy rotate-backup --keep 14
```

删除旧快照前先校验源账本的 SQLite 完整性，并确认目标目录不存在。可用空间要容纳一份完整账本，
并在备份后至少留 512 MB（高于监控 200 MB 的低磁盘 STOP 线）；不足时仅删除本次轮换本来就要删的旧快照，
且新快照完成前至少保留最新一份已完成快照。删了也不够时跳过当天备份、不删除任何已有快照，并通过监控发送
“备份已跳过：磁盘空间不足”提醒。`--keep 1` 无法同时容纳新旧两份时也跳过。复制失败后仍保留至少一份旧快照，
关闭 SQLite 连接并清理本次不完整目录；硬退出留下的 `.incomplete` 目录不计入快照。这样备份不会因写满磁盘而触发交易停止。

每次备份之后还会按“数据保留”规则清理没有程序再读取的数据：30 天前的普通资讯、180 天前的重要资讯、
30 天前的模型调用追踪文件和 12 个月前的事件导出归档；账本、决策、复盘、净值和新调用的应用输入／返回结果归档不删。手动预览：
`bash scripts/fund-logs.sh prune`（加 `--apply` 执行）。

**单机调用证据**：新模型调用在同一 `state.sqlite3` 内保存任务／系统提示、上下文、Schema、参数、应用源码及依赖版本，
返回后保存回答与已报告用量。组件以 SHA-256 标识，gzip 压缩并去重，随现有 SQLite 备份保留；不增加服务、数据订阅或模型推理。
`bash scripts/fund-logs.sh evidence` 只读检查覆盖率、哈希与调用事件引用，默认不打印私人输入；
`bash scripts/fund-logs.sh evidence <call-id>` 展开所选账户中该调用的详细输入与返回结果，应按私人账本保护。
旧输入不会补造，归档失败和未完成调用明确列出；每个未压缩组件上限 16 MiB，失败只记录异常类型，正常模型调度和交易校验不变。
`verified` 仅表示已记录应用证据内部一致；`partial`／`unavailable` 显示缺项（退出码 0），完整性错误为 `failed`（退出码 2）。
不包含原生记忆、SDK 最终提示、完整后续工具响应或提供方状态；进程首次归档时的磁盘源码也不认证所有已加载依赖一致，不能据此宣称精确重放或盈利。
恢复验证覆盖合成模型输入和结果、哈希及 STOP；仍需在目标机器进行现有恢复验收。详见
[调用证据归档与只读校验](AUTONOMOUS_PAPER_FUND.md#调用证据归档与只读校验)。

快照仍在同一台机器上。异地保存请自行把 `artifacts/fund-backups/` 同步到对象存储或另一台主机（例如每天
`rsync -a artifacts/fund-backups/ <backup-host>:<path>/`，或用云厂商的对象存储同步工具）。

**监控指标**：独立监控每轮写 `<账户目录>/metrics.prom`（Prometheus 文本格式）：`fund_healthy`、`fund_health_state`、
`fund_stop_active`、`fund_equity_usd`、`fund_cash_usd`、`fund_cash_equivalent_usd`、`fund_gross_exposure`、`fund_drawdown`、
`fund_pending_approvals`、`fund_notifications`、`fund_disk_free_bytes` 和最后检查时间。已有 Prometheus／Grafana 的话，
让 node_exporter 的 textfile 收集器读取这个文件（复制或软链到它的目录），就能画曲线、设告警；没有也不影响运行。
秘密和准确版本的配置应另行安全保存；恢复必须使用同一配置和正确账户，先核对再 resume。
SQLite 不是异地备份，不能抵御整机磁盘丢失。

目标主机上线前至少验收：依赖安装和 unit 校验、只读行情预检、Telegram 测试、
模拟账户单实例锁、断网停单、杀进程自动重启且 STOP 保留、整机重启后的核对恢复、
重大审批、真实模型决策到虚拟成交以及一次收盘复盘附件。
目前 Windows 回归采用合成行情、模型替身和 HTTP 替身；不能将这些结果当作 Ubuntu 或券商联网已验收。

本次综合选择运行 409 项测试通过，1 项 Linux 文件权限检查因 Windows 平台跳过；
随后服务专项 16 项通过（同一 Linux 项仍跳过），覆盖监控子进程卡死回收的新断言。
Linux shell 语法检查通过。实际 CLI Mock 在原生引擎产生 6 次结构化测试决策、1 次模拟成交和 4 次复盘，
另经 CLI 备份验证 STOP 保留且不复制秘密。这些是机械流程证据，不是投资效果证据。
Sphinx HTML 构建成功，仍有未修改页面中的 41 条诊断；本次环境变量页未新增构建诊断。

官方参考：[systemd 服务与重启行为](https://www.freedesktop.org/software/systemd/man/255/systemd.service.html)、
[Telegram 文档上传](https://core.telegram.org/bots/api#senddocument)。
