# AI 模拟基金：Ubuntu 服务部署

本应用的显式秘密配置、无人值守进程管理、审批和日报运维说明。

**Last Updated:** 2026-11-01

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

## 安装及秘密配置

先把已审阅的当前版本代码放到服务器。使用既有专用用户；安装系统组件需要管理员权限：

```bash
sudo apt-get update
sudo apt-get install python3.12-venv python3-dev build-essential ca-certificates tzdata
cd "<repo>"
python3.12 -m venv .venv
.venv/bin/python -m pip install -e . -r scripts/autonomous_fund/requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m scripts.autonomous_fund.deploy prepare --user "<service-user>"
```

`prepare` 只生成 `artifacts/fund-systemd/` 中的两个 unit 文件，创建模拟账户目录和 STOP 标记，
并在 `.env` 不存在时独占创建空值模板。它不覆盖已有 `.env`，也不安装或启动任何服务。
请在服务器本地编辑 `.env`，不要把密钥粘到聊天中。模板源是
`scripts/autonomous_fund/.env.example`，真实 `.env` 已被 Git 忽略。

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | Flash 研究及 V4 Pro 决策／复盘，共用一个 Key |
| `FUND_TELEGRAM_BOT_TOKEN` | 自己创建的 Telegram Bot token |
| `FUND_TELEGRAM_CHAT_ID` | 已与 Bot 建立会话的接收者或群组；先在 Telegram 中启动 Bot |
| `ALPACA_API_KEY`、`ALPACA_API_SECRET` | 仅选择 `paper` 时需要，使用专用模拟账户的 Paper Key |
| `ALPACA_IS_PAPER=true` | 可选显式声明；设为 false 会拒绝启动，不会启用实盘 |

本地虚拟账户不需要券商注册或 Key。其他模型与可选数据服务的变量见模板及
`docsrc/environment_variables.rst`。不要设置 `AI_MODEL`，除非有意同时覆盖两个角色的默认模型。
API 美元成本依赖配置中明确填写的模型费率；未知 usage 或缺少费率不会被记成零成本。

`.env` 由 python-dotenv 解析，绝不 shell `source`；已有进程环境优先。
普通 Python CLI 仅在传入 `--env-file` 时读取；Linux 启动脚本明确选择本 checkout 的 `.env`，
也允许用 `--env-file` 指定另一份文件。Linux 文件权限必须为 0600 或更严格，否则拒绝加载。
服务用户必须能读代码和 `.env`、写所选运行目录；服务运行时通过 systemd 将代码和秘密文件设为只读。

```bash
chmod 600 .env
sudo chown "<service-user>" .env artifacts/fund-local artifacts/fund-local/STOP
mkdir -p artifacts/autonomous-fund-account-locks
sudo chown "<service-user>" artifacts/autonomous-fund-account-locks
sudo -u "<service-user>" .venv/bin/python -m scripts.autonomous_fund.run check \
  --run-dir artifacts/fund-local --env-file .env
```

`check` 只验证公开行情、源时间及 Key 是否存在，不调用付费模型，也不证明提供方鉴权成功。
如将来选择 Alpaca Paper，先在本地 `.env` 填写专用 Paper Key，再用
`deploy prepare --user "<service-user>" --mode paper --run-dir artifacts/fund-paper`
生成另一套配置；不要用 `fund-local` 的账本连接券商账户。首次 Paper 账户还须满足应用要求的
10,000 美元现金及空证券持仓，并完成原有 Paper 验收。
如果父目录不允许服务用户遍历，需由管理员调整目录权限；不要用放开所有文件读写的方式解决。
可选浏览器的安装和外部数据订阅仍按 LumiBot 原有说明配置；缺少这些工具会影响研究覆盖面。

## 审阅并启动两个服务

```bash
systemd-analyze verify artifacts/fund-systemd/lumibot-fund-*.service
sudo install -m 644 artifacts/fund-systemd/lumibot-fund-runner.service /etc/systemd/system/
sudo install -m 644 artifacts/fund-systemd/lumibot-fund-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lumibot-fund-runner.service lumibot-fund-monitor.service
systemctl status lumibot-fund-runner.service lumibot-fund-monitor.service
journalctl -u lumibot-fund-runner.service -u lumibot-fund-monitor.service -n 60 --no-pager
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh notify-test --run-dir artifacts/fund-local
```

首次交易进程建立 SQLite 后，监控即可连接；首次启动竞态可能导致监控退出一次，systemd 会重试。
先确认 Telegram 手机实际收到测试通知，并检查 `artifacts/fund-local/monitor.json` 的
`health` 是 `stopped` 或 `healthy`、心跳新鲜、`telegram_configured=true`。
模型或行情报错时先修复，不能通过反复 resume 绕过故障。

```bash
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh resume --run-dir artifacts/fund-local
```

之后普通模拟交易无需逐笔审批；系统按 AI 的下次复核时间重新研究和决策。
所有服务启动／重启都保留或设置 STOP，必须核对账户、订单、心跳后显式 resume。
因此服务器重启会恢复进程，交易恢复需要人工核对；不会把“进程已恢复”当作“订单状态已确认”。
休市时不撮合；恢复后按原生市场调度运行，盘前研究和盘后复盘在对应钩子触发。

服务以非 root 用户运行，退出后延迟 30 秒重启，启动次数不设永久锁死上限。
子进程卡死也由独立进程检测；关闭先发 SIGINT，30 秒未退出再强制结束。
交易子进程带 `--require-monitor`：独立监控心跳超过 45 秒、状态异常或未配置 Telegram 时，
每次提交前及每次本地模拟成交前都会拒绝执行。监督进程还会回收失去监控或卡住的子进程。
STOP、回撤锁、审批状态、模糊订单和每日订单计数都存在原账本中，重启不会清空。
券商端已挂出的 Paper 订单可能在断线／撤单确认前成交；恢复时必须核对，不能假定停进程等于已撤单。

## 审批、日报和日常检查

单笔达到净值 8%、当日累计预留／提交买卖额达到 20%，或清空全部证券时，等待人工审批。
通知包括买卖方向、标的、当前持股、目标比例、数量／限价范围、金额、触发条件、有效期、
AI 的买卖依据和风险。长理由用文本附件保留全文。卖出限价不能跌破审批范围后继续沿用旧批准。
超时、价格或数量超范围、目标变更时重新审批。审批不放宽单股 10%、总敞口 70%、每日 5 单和 8% 回撤锁。

```bash
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh approvals --run-dir artifacts/fund-local
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh approve --run-dir artifacts/fund-local \
  --approval-id "<reviewed-id>" --approval-digest "<reviewed-digest>"
```

拒绝时将 `approve` 改为 `reject`。Telegram 仅通知，聊天回复不会执行审批；命令中的 ID／摘要必须来自完整请求。

每次成功完成盘后复盘，独立监控自动发送对应日期的 Markdown 日报附件：账户与现金／持仓、
观测盈亏与收益率、对齐窗口的 SPY／QQQ、运行至截止的回撤和费用、模型 usage、订单／成交／审批记录、
选股与择时调整、验证和失效条件、次日策略备忘及数据缺项。
不是交易日不虚构日报；停机、STOP 或模型失败可能导致复盘尚未完成，应查看异常通知和日志。
本地文件仍在 `reviews/YYYY-MM-DD.md`、`postmarket_review.md`、`report.json`、`equity.csv`、`fills.csv`。

日报从固定账本生成，不读取 AI 指定的文件路径；原始研究和估值时间保留，缺少收盘估值会明确披露。
监控离线后重启会发现已保存的复盘并补发，发送失败进入持久化退避队列。
Telegram 接收确认丢失时重试可能重复，不保证恰好一次；同一天文件名可以用于核对。

```bash
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh report --run-dir artifacts/fund-local
sudo -u "<service-user>" bash scripts/run_autonomous_fund.sh stop --run-dir artifacts/fund-local
sudo systemctl stop lumibot-fund-runner.service lumibot-fund-monitor.service
```

`runtime.log` 按 20 MB 轮转，保留 5 个历史文件；journald 的磁盘额度由服务器管理员设置。
SQLite 审计账本不会自动删除；监控发现运行分区剩余不足 200 MB 时会写 STOP 并通知。
定期检查磁盘、内存、模型账单、通知队列、时间同步和服务重启次数；长期内存稳定性需实际观察。

## 行情轮询与可靠性的边界

本地撮合轮询每 5 秒，策略调度每分钟；AI 按自己的复核时间运行，并非每次轮询都收费推理。
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
