# A股尾盘短线策略系统

> 14:30 全市场强势股筛选 → 14:51 尾盘确认买入 → 次日冲高卖出。纯 Python 标准库实现，零第三方依赖。

[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white)](#)
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](#)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

> **风险提示**：本项目仅用于量化策略研究与工程实践，**不构成任何投资建议**。策略参数是待验证的初始假设，尚未完成严格的历史回测与实盘验证。股市有风险，据此操作，盈亏自负。

---

## 项目简介

这是一个可独立运行的 A 股短线策略引擎，围绕「**尾盘强势股筛选 → 次日冲高卖出**」这条交易假设构建，覆盖：

- 全市场涨幅榜初筛与多维评分；
- 日 K 位置、10 分钟均线结构、尾盘强度、量能、板块环境等硬性条件校验；
- 14:51 二次确认与买入清单生成；
- 次日开盘情景判定与持仓监控；
- 周/月复盘、选股漏斗统计、影子跟踪与 K 线快照。

引擎只输出分析与交易计划，**不自动下单**。所有报告严格区分「事实数据 / 计算结果 / 主观判断」。

## 核心特性

| 特性 | 说明 |
|---|---|
| **零依赖** | 仅使用 Python 标准库，`python3` 即可运行，无 pip 安装步骤 |
| **无未来函数** | 10 分钟 K 线只使用已收盘 bar，日 K 当日 bar 明确标记「进行中」 |
| **三层数据校验** | 行情自洽 → 东财 × 腾讯双源交叉 → K 线历史合法性检查 |
| **结构化报告** | Markdown + JSON 双输出，便于人读、机器复盘与推送 |
| **影子跟踪** | 对初筛池做「假设买入」前向跟踪，扩大统计样本 |
| **K 线快照** | 自动补记建仓后 T+1/T+2/T+3 的 10 分钟 K 线，用于卖点复盘 |
| **可选推送** | 默认关闭；可接入企业微信 / 飞书等 webhook，只通知不下单 |
| **定时任务友好** | 每个模式均可独立调用，可直接交给 cron / systemd timer |

## 策略流程

```
14:31  全市场初筛          涨幅 3%~8% → 成交额/换手/量比过滤 → 日K位置 → 10分钟MA5结构
                           → 尾盘强度 → 百分制评分 → 候选报告（Markdown + JSON）

14:51  尾盘确认            持仓退出处理 → 按最新行情复核候选 → 硬性条件二次校验
                           → 生成买入清单（最多 5 只，含止损价与盈亏比）

次日   开盘决断            9:35 按开盘情景 A–E 判定 → 冲高卖出 / 继续观察
                           → 盘中每半小时巡检 → 14:51 最迟退出

收盘   复盘沉淀            周/月总结 → 胜率、盈亏比、最大回撤、选股漏斗、分档统计
```

更细的规则与参数说明见 [策略文档](策略文档.md) 与 [运维手册](docs/OPERATIONS.md)。

## 快速开始

**环境要求**：Python 3.8+，可访问东方财富 / 腾讯 / 新浪行情接口的网络环境。

```bash
git clone git@github.com:kangkangxi/stock-strategy.git
cd stock-strategy

# 1. 生成本地配置（config.json 已被 .gitignore 忽略）
cp config.example.json config.json

# 2. 查看当前持仓与统计（首次运行会自动创建空数据）
python3 scripts/tail_momentum.py --mode status

# 3. 试跑一次尾盘初筛：只出报告，不推送、不写入持仓
python3 scripts/tail_momentum.py --mode scan --dry-run
```

运行后会在项目根目录生成 `报告/`、`日志/` 与 `positions.json`，这些均属于本地数据，不会进入 Git。

### 常用命令

```bash
python3 scripts/tail_momentum.py --mode scan       # 14:31 全市场初筛
python3 scripts/tail_momentum.py --mode pm         # 14:51 持仓退出 + 尾盘确认
python3 scripts/tail_momentum.py --mode exit       # 次日持仓监控 / 卖出信号
python3 scripts/tail_momentum.py --mode week       # 本周复盘
python3 scripts/tail_momentum.py --mode month      # 上月复盘
python3 scripts/tail_momentum.py --mode ksnap      # 补记 K 线快照

python3 scripts/tail_momentum.py --hold 600000 12.34 500   # 手工登记成交
python3 scripts/tail_momentum.py --close 600000 12.90       # 手工平仓
```

调试时可用 `--dry-run` 只出报告、不推送；用 `--force-now "YYYY-MM-DD HH:MM"` 复算指定时点（引擎会按该时钟过滤行情，不会读取未来数据）。

## 配置

将 `config.example.json` 复制为 `config.json` 后按需修改，主要参数：

| 分组 | 参数 | 说明 |
|---|---|---|
| 初筛 | `pct_low` / `pct_high` | 当日涨幅区间（默认 3% ~ 8%） |
| 初筛 | `min_amount_yi` / `min_turnover` | 最低成交额（亿元）/ 最低换手率 |
| 技术 | `max_dev_from_ma` / `max_5d_gain` | 距 10 分钟均线最大偏离 / 5 日最大涨幅 |
| 风险 | `min_rr` / `min_stop_pct` / `max_stop_pct` | 最小盈亏比 / 止损区间 |
| 资金 | `account_equity` / `risk_per_trade_pct` | 账户权益（0 = 只给止损价与股数比例） |
| 执行 | `max_buy_list` / `force_exit_time` | 单日最多买入数 / 最迟退出时间 |
| 推送 | `push` / `push_max_stocks` | 是否推送 / 单次推送标的数 |

配置采用「缺省合并」：`config.json` 中未写的键会使用代码内默认值。

## 输出与报告

```
报告/
├── 尾盘选股_YYYY-MM-DD.md     14:31 完整候选报告（字段 + 评分明细）
├── 候选_YYYY-MM-DD.json       候选结构化数据
├── 买入清单_YYYY-MM-DD.md     14:51 最终买入清单
├── 持仓监控_YYYY-MM-DD_HHMM.md
├── 周总结_YYYY-MM-DD.md / 月总结_YYYY-MM.md
├── K线快照_YYYY-MM-DD.json    建仓后 T+1/T+2/T+3 的 10 分钟 K 线
└── 最新推送.md                最近一次推送原文

日志/
└── 运行记录.jsonl             每轮运行的模式、结果与统计
```

> 报告与日志包含真实交易数据，默认全部被 `.gitignore` 排除。**请勿将 `positions.json`、`报告/`、`日志/` 提交到公开仓库。**

## 项目结构

```
stock-strategy/
├── README.md
├── LICENSE
├── .gitignore
├── config.example.json          参数模板
├── 策略文档.md                  策略规则原文
├── scripts/
│   ├── tail_momentum.py         策略引擎（单文件，纯标准库）
│   ├── tail_momentum_scan.sh    14:31 初筛包装脚本
│   ├── tail_momentum_pm.sh      14:51 确认包装脚本
│   ├── tail_momentum_exit.sh    持仓监控包装脚本
│   ├── tail_momentum_week.sh    周复盘包装脚本
│   ├── tail_momentum_month.sh   月复盘包装脚本
│   └── tail_momentum_ksnap.sh   K 线快照包装脚本
├── docs/
│   ├── OPERATIONS.md            运维与实现细节
│   └── index.html               项目介绍页（可发布到 GitHub Pages）
├── 报告/                        运行时生成，已忽略
└── 日志/                        运行时生成，已忽略
```

## 项目介绍页

`docs/index.html` 是一个零依赖的静态介绍页（响应式，支持移动端）。推送到 GitHub 后：

1. 打开仓库 **Settings → Pages**；
2. Source 选择 **Deploy from a branch**；
3. 分支选择 `main`，目录选择 `/docs`，保存。

稍等片刻即可通过 `https://kangkangxi.github.io/stock-strategy/` 访问。

## 定时任务（可选）

以下为 Linux cron 示例，按需调整时间与路径：

```cron
31 14 * * 1-5  cd /path/to/stock-strategy && ./scripts/tail_momentum_scan.sh
51 14 * * 1-5  cd /path/to/stock-strategy && ./scripts/tail_momentum_pm.sh
35 9  * * 1-5  cd /path/to/stock-strategy && ./scripts/tail_momentum_exit.sh
5  15 * * 5    cd /path/to/stock-strategy && ./scripts/tail_momentum_week.sh
5  15 1 * *    cd /path/to/stock-strategy && ./scripts/tail_momentum_month.sh
10 15 * * 1-5  cd /path/to/stock-strategy && ./scripts/tail_momentum_ksnap.sh
```

非交易日（行情时间戳非当日）任务会直接退出、不推送。完整时点见 [运维手册](docs/OPERATIONS.md)。

### 推送（默认关闭）

引擎通过调用外部可执行文件发送通知，接口为：`wecom_push.py <markdown文件路径>`。

- 默认查找 `scripts/wecom_push.py`；
- 也可用环境变量指定：`export WECOM_PUSH=/path/to/your_push.py`；
- 开启方式：`config.json → "push": true`。

推送器不属于本仓库（避免误提交 webhook 密钥），请自行实现或使用现有通知服务。

## 数据源与校验

| 用途 | 来源 |
|---|---|
| 全市场涨幅榜 / 行业板块 | 东方财富 `push2.eastmoney.com` |
| 5 分钟 K 线 | 东方财富（主）/ 新浪（备，交叉校验） |
| 日 K 线 | 东方财富（主）/ 新浪（备） |
| 实时行情 | 腾讯 `qt.gtimg.cn` |

关键校验失败时不出交易名单；数据缺失处标记 `—`，不编造。

## 免责声明

1. 本项目是**技术研究与教学演示**，不是投资顾问服务，不构成任何证券买卖建议。
2. 策略逻辑与参数均为**待验证假设**，作者不对其有效性、收益或回撤作任何承诺。
3. 行情数据来自第三方公开接口，可能存在延迟、缺失或错误。
4. 使用者应自行评估风险并遵守所在地区的法律法规；因使用本项目产生的任何损失，作者不承担责任。

## License

[MIT](LICENSE) © 2026 kangkangxi
