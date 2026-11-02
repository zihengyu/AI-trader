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
并在 `.env` 不存在时独占创建空值模板。生成的服务不写死模式：每次启动／重启都读取 `.env` 的 `FUND_MODE`
（`local` 本地模拟盘或 `paper` Alpaca 模拟账户，没有实盘选项）和 `FUND_RUN_DIR`（留空为 `artifacts/fund-<模式>`，
服务要求放在仓库 `artifacts/` 下），`prepare` 会打印服务将使用的模式和账户目录。它不覆盖已有 `.env`，也不安装或启动任何服务。
请在服务器本地编辑 `.env`，不要把密钥粘到聊天中。模板源是
`scripts/autonomous_fund/.env.example`，真实 `.env` 已被 Git 忽略。模板按“模型 Key 与模型选择、用户约束、审批额度、
Telegram、外部数据与运行参数、券商”分节，所有可调项都有中文说明和默认值；优先级为进程环境 > `.env` > `config.json`，
留空表示沿用 `config.json`，`.env` 中拼错的 `FUND_*` 名称会拒绝启动。完整变量表见 `docs/AUTONOMOUS_PAPER_FUND.md` 的 “`.env`：运营者配置入口”。
修改后用 `bash scripts/run_autonomous_fund.sh show-config` 核对最终生效值和来源，再重启服务。

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
chmod 600 .env
sudo chown "<service-user>" .env artifacts/fund-local artifacts/fund-local/STOP
mkdir -p artifacts/autonomous-fund-account-locks
sudo chown "<service-user>" artifacts/autonomous-fund-account-locks
sudo -u "<service-user>" .venv/bin/python -m scripts.autonomous_fund.run check \
  --run-dir artifacts/fund-local --env-file .env
```

`check` 只验证公开行情、源时间及 Key 是否存在，不调用付费模型，也不证明提供方鉴权成功。
它还会报告 `research_network`：若研究站点解析为 Fake-IP（198.18.0.0/15）或私有地址，网页研究会被安全检查拒绝，
按提示让服务器不走 Fake-IP，或在 `config.json` 设置 `"research_network": {"resolver": "doh"}`。
如将来选择 Alpaca Paper，先在本地 `.env` 填写专用 Paper Key，把 `FUND_MODE` 改为 `paper`，为服务用户准备新账户目录，
再一起重启两个服务；账本自动换到 `artifacts/fund-paper`，不会用 `fund-local` 的账本连接券商账户：

```bash
sudo install -d -o "<service-user>" -m 700 artifacts/fund-paper
sudo systemctl restart lumibot-fund-runner.service lumibot-fund-monitor.service
```

重启后同样保持 STOP，核对后再 `resume`。也可用 `deploy prepare --mode paper --run-dir artifacts/fund-paper`
生成写死模式的服务；此时若 `.env` 的 `FUND_MODE` 与之不同，服务会拒绝启动并提示。首次 Paper 启动时账户不能持有投资范围内的证券或未完成订单，
现金不少于 `mandate.capital_usd`；超出部分自动记为 AI 不能动用的保留现金。之后账户出入金须先停止交易服务，再执行
`bash scripts/run_autonomous_fund.sh adjust-reserve --run-dir artifacts/fund-paper --amount <金额>` 记入保留现金。
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

之后普通模拟交易无需审批；系统按 AI 的下次复核时间（不早于 15 分钟）重新研究和决策，
模型失败时按 5／15／60 分钟退避，并受每日 Agent 运行次数预算约束。
所有服务启动／重启都保留或设置 STOP。`prepare` 写入的首次安装 STOP 和 `stop` 命令写入的 STOP 必须核对后显式 resume；
之后服务器重启、服务退出或进程卡死造成的 STOP，由独立监控在交易进程连续健康 3 分钟、没有状态不明的订单预留后自动解除
（24 小时内最多 3 次，`FUND_AUTO_RESUME=false` 可关闭），并通过 Telegram 通知；磁盘不足、版本不符等故障仍需人工处理。
详见 `docs/AUTONOMOUS_PAPER_FUND.md` 的“停止、恢复与异常通知”。
休市时不撮合；恢复后按原生市场调度运行，盘前研究和盘后复盘在对应钩子触发。

服务以非 root 用户运行，退出后延迟 30 秒重启，启动次数不设永久锁死上限。
子进程卡死也由独立进程检测；关闭先发 SIGINT，30 秒未退出再强制结束。
交易子进程带 `--require-monitor`：独立监控心跳超过 45 秒、状态异常或未配置 Telegram 时，
每次提交前及每次本地模拟成交前都会拒绝执行。监督进程还会回收失去监控或卡住的子进程。
STOP、回撤锁、审批状态、模糊订单、每日订单计数、单日亏损闸门、模型失败退避和每日模型预算都存在原账本中，重启不会清空。
券商端已挂出的 Paper 订单可能在断线／撤单确认前成交；恢复时必须核对，不能假定停进程等于已撤单。

## 审批、日报和日常检查

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
