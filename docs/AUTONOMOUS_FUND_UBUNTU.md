# AI 模拟基金：Ubuntu 服务部署

本应用的显式秘密配置、无人值守进程管理、审批和日报运维说明。

**Last Updated:** 2026-11-03

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

装好 Python 依赖（见下一节）之后，日常只需要五个脚本，都在仓库目录里**以普通用户运行**（不要加 sudo，需要时脚本会自己调用）。
服务以运行脚本的用户身份运行，例如 `ubuntu`：

| 脚本 | 作用 |
|---|---|
| `bash scripts/fund-start.sh` | 启动：检查 `.env` → 安装或更新三个 systemd 服务（交易、独立监控、数据员）和每日备份定时器 → 启动 → 等独立监控就绪（最多 3 分钟）→ 处于人工停止时询问是否恢复交易（`-y` 直接恢复）。旧版本装过的 `lumibot-fund-prefetch.timer` 会自动停用并删除（已被数据员取代） |
| `bash scripts/fund-restart.sh` | **更新代码后用这个**：重新安装有变化的服务文件 → 真正重启三个服务（加载新代码）→ 等到重启后的独立监控写出新状态 → 询问是否恢复交易（`-y` 直接恢复）。`--update` 先 `git pull --ff-only`（只快进，不覆盖本地修改）并按锁定版本重装依赖。`fund-start.sh` 对已经在运行的服务不会重启，所以更新后单独运行它看起来“没反应” |
| `bash scripts/fund-rehearse.sh` | **盘前彩排**：在交易服务完全相同的文件沙箱里，用 `FUND_MODE` 账户的副本（含数据员准备的数据）、真实行情和真实模型完整走一遍决策（数据员状态 → 盘前研究 → 风控数据 → 因子研究 → 财报日历 → FRED → 反方审查 → 基金经理 → 决策校验 → 量化风控），在下单前停下，逐段报告耗时、本次彩排中每次模型调用的耗时和 token，并单列“模型回答是否可用”（格式不符、反方审查失败、复盘失败及位置）。在后台临时服务里运行，断开 SSH 不会中断，重连后用 `--show` 看结果。不碰真实账户、STOP、审批和 Telegram；`--no-models` 只检查数据环节、不花模型费用 |
| `bash scripts/fund-logs.sh` | 维护：默认显示运行概况（服务、交易／停止原因、监控、数据员各类数据的更新时间、账户、待审批、最近通知）；`logs` 三个服务的实时日志，`errors` 最近 24 小时的警告和错误，`report` 账户报告，`approvals` 待审批 |
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
   一次彩排大约调用 2–3 次模型（盘前研究若当天已做过则不再调用）。断线后重新连上，运行 `bash scripts/fund-rehearse.sh --show` 查看结果。

**模型调用的时间上限**：LumiBot 的 `run_timeout` 是每次尝试的上限，超时还会被当作临时错误重试，实盘默认最多 10 次，
一次调用可能拖到将近一小时，而独立监控在单次调用超过 900 秒时就会停止交易。启动入口因此默认 `LUMIBOT_AGENT_MAX_RUN_ATTEMPTS=2`，
研究员每次尝试 420 秒、反方审查和基金经理各 300 秒，最坏情况分别约 842 秒和 602 秒；研究员被要求控制在约 20 次工具调用内。
如果你手动把重试次数调大到会超过 900 秒，启动时会记录 `agent_time_budget_exceeds_monitor` 并提示。

## 数据员：常驻的数据整理服务

研究员和基金经理不再在模型调用里现拉数据，新消息也不用等到某个固定时间。第三个服务 `lumibot-fund-desk`（数据员）
一直在后台运行，只拉数据、整理数据、算好指标；它不调用模型、不下单，也不改 STOP、审批和决策：

| 数据 | 内容 | 多久更新一次 |
|---|---|---|
| 资讯 | Yahoo 个股新闻、SEC 8-K 公告、美联储发布、自定义 RSS，以及 AI 设定的 `news_watch` 搜索词，写进账本的资讯表；跟踪的标的包括持仓、最新目标和价格提醒 | 开盘时每 `FUND_NEWS_POLL_SECONDS`（默认 5 分钟），休市时每 20 分钟 |
| 盘中行情 | 每个可交易标的和 SPY/QQQ/IWM/DIA：最新已完成一分钟的价格、相对昨收的涨跌、开盘跳空、日内高低和成交量，领涨领跌和涨跌家数 | 开盘时每 `FUND_DESK_INTRADAY_SECONDS`（默认 5 分钟） |
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
请求之间停 0.3 秒，一轮里连续 3 次失败就停下这一轮；交易进程自己的 Yahoo 请求一出错，就写下冷却时间
（`cache/desk/yahoo_cooldown.json`），冷却期间数据员不再请求 Yahoo（资讯里只暂停 Yahoo 新闻，美联储、SEC、Google 新闻照常）。
数据员服务的 CPU、磁盘优先级更低，内存紧张时系统先停它。

**过期和可疑的数据会标出来**：超过 15 分钟没更新的盘中行情、超过 1 小时的盘前行情、超过 90 分钟的全球市场，在 AI 的输入里只显示
“已过期”和更新时间，不当作当前数据。相对昨收跳空 25% 以上的标的标为“可疑（可能是拆股、特别分红或数据错误，需核实）”，
不计入领涨领跌。资讯会把不同来源的同一条新闻合并成一条（注明几家报道），并按重要程度排序：重大事件、持仓、关注词、宏观、目标、公告，
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

`connectivity` 一节逐项给出 Yahoo、Telegram、模型 API、Alpaca、SEC、FRED、Finnhub 的 HTTP 状态和耗时，
`connectivity_summary.blocking` 列出会影响运行的失败项并附中文原因。**服务器在中国大陆地域时 Telegram 和 Yahoo 通常无法直连**，
请改用香港／新加坡等境外地域，或在 `.env` 设置 `HTTPS_PROXY`。设置了 Alpaca Key 时，local 模式在 Yahoo 拿不到日线时
会自动改用 Alpaca 的历史日线（只用于风控和因子，记录 `risk_history_fallback`）。

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
| `FUND_SEC_USER_AGENT` | 可选；资讯监控读取 SEC 8-K 公告所需的联系人声明（姓名＋邮箱） |
| `ALPACA_API_KEY`、`ALPACA_API_SECRET` | 仅选择 `paper` 时需要，使用专用模拟账户的 Paper Key |
| `ALPACA_IS_PAPER=true` | 可选显式声明；设为 false 会拒绝启动，不会启用实盘 |
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
现金不少于 `mandate.capital_usd`；超出部分自动记为 AI 不能动用的保留现金。之后账户出入金须先停止交易服务，再执行
`bash scripts/run_autonomous_fund.sh adjust-reserve --run-dir artifacts/fund-paper --amount <金额>` 记入保留现金。
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
详见 `docs/AUTONOMOUS_PAPER_FUND.md` 的“停止、恢复与异常通知”。
休市时不撮合；恢复后按原生市场调度运行，盘前研究和盘后复盘在对应钩子触发。

服务以非 root 用户运行，退出后延迟 30 秒重启，启动次数不设永久锁死上限。
子进程卡死也由独立进程检测；关闭先发 SIGINT，30 秒未退出再强制结束。
交易子进程带 `--require-monitor`：独立监控心跳超过 45 秒、状态异常或未配置 Telegram 时，
每次提交前及每次本地模拟成交前都会拒绝执行。监督进程还会回收失去监控或卡住的子进程。
STOP、回撤锁、审批状态、模糊订单、每日订单计数、单日亏损闸门、经理失败退避和每日模型预算都存在原账本中，重启不会清空。
券商端已挂出的 Paper 订单可能在断线／撤单确认前成交；恢复时必须核对，不能假定停进程等于已撤单。

## 审批、日报和日常检查

**Telegram 每天会收到的消息**（时间均同时显示美东时间和服务器本地时间）：

| 时间 | 消息 |
|---|---|
| 开盘前约 1 小时（休市期间重启后也会补一次） | 📊 盘前研究：AI 的研究摘要和证据来源；内容太长时作为文本附件 |
| 开盘后第一轮 | 🔔 美股开盘：今天是否交易（暂停时写明原因和恢复方法）、账户净值／现金／持仓、盘前研究要点、收盘时间 |
| 盘中，AI 调整组合时 | 🧭 AI 调整目标组合：买入／加仓、卖出／减仓、不变的证券和权重（含入场价、止损、止盈）、现金比例、理由、下次复核时间。只在组合真的变化时发送（增删证券或权重变动至少 1 个百分点），每天第一次复核也发一次 |
| 盘中按需 | 审批请求（回复「同意／拒绝」）、止损触发、停止／自动恢复、异常告警；组合构建方式自动切换时（🔁，附配对检验结果） |
| 收盘后复盘完成 | 盘后日报附件：当日表现、成交、记分卡、因子检验、复盘结论和下一交易日策略备忘 |

盘前时间按交易所日历计算，与服务器时区无关。北京时间在美东冬令时约 21:30 开始研究，
夏令时约 20:30 开始；通知在生成完成后发送。旧版公共 Broker 时钟会把本地时间误当成 UTC，
在北京时间服务器上可能误等到下一交易日；更新修复后需要重启 runner，让已进入的长时间等待重新计算。
保留原账户目录及 STOP／订单状态，不要通过清空账本恢复。详见
`docs/investigations/2026-11-02_BROKER_MARKET_CLOCK_TIMEZONES.md`。

停止、恢复、报错等通知都是中文说明，末尾带事件编号和类型，便于对照 `bash scripts/fund-logs.sh` 的记录。
`healthy` 和新鲜心跳只表示进程健康；业务轮次仍可能失败。`status` 会显示最近业务异常的阶段、
系统错误码和模块／函数／行号，以及已记录的 AI 失败退避时间；完整的安全诊断位于日志和账本中的
`operation_error`。同一种告警会去重 30 分钟，不应把没有新告警当成业务已经恢复。

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
每条指令都记入账本。发送「状态」可查看监控状态、待审批数和分仓净值。请给 Telegram 账号开启两步验证。

每次成功完成盘后复盘，独立监控自动发送对应日期的 Markdown 日报附件：账户与现金／持仓、
观测盈亏与收益率、对齐窗口的 SPY／QQQ、运行至截止的回撤和费用、模型 usage、订单／成交／审批记录、
选股与择时调整、验证和失效条件、次日策略备忘及数据缺项。
不是交易日不虚构日报；停机、STOP 或模型失败可能导致复盘尚未完成，应查看异常通知和日志。
运行一段时间后执行 `bash scripts/run_autonomous_fund.sh acceptance --run-dir artifacts/fund-local` 生成验收报告
（`acceptance.md`）：连续交易日数、迭代完成率、监控 STOP、未结束委托、成交明细、模型失败率、复盘完成率和平均滑点都达标后，
再考虑进入下一阶段。
本地文件仍在 `reviews/YYYY-MM-DD.md`、`postmarket_review.md`、`report.json`、`equity.csv`、`fills.csv`。

日报从固定账本生成，不读取 AI 指定的文件路径；原始研究和估值时间保留，缺少收盘估值会明确披露。
监控离线后重启会发现已保存的复盘并补发，发送失败进入持久化退避队列。
Telegram 接收确认丢失时重试可能重复，不保证恰好一次；同一天文件名可以用于核对。

```bash
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh report --run-dir artifacts/fund-local
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh stop --run-dir artifacts/fund-local
sudo systemctl stop lumibot-fund-runner.service lumibot-fund-monitor.service lumibot-fund-desk.service
```

`runtime.log` 按 20 MB 轮转，保留 5 个历史文件；journald 的磁盘额度由服务器管理员设置。
SQLite 审计账本不会自动删除；监控发现运行分区剩余不足 200 MB 时会写 STOP 并通知。
定期检查磁盘、内存、模型账单、通知队列、时间同步和服务重启次数；长期内存稳定性需实际观察。

## 行情轮询与可靠性的边界

本地撮合轮询每 5 秒，默认策略调度每 30 秒（可配置）；AI 按自己的复核时间运行，并非每次轮询都收费推理。
行情源是 Yahoo 已完成分钟线，加披露的点差／滑点代理；不是逐笔成交、交易所买卖盘或低延迟 tick。
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
