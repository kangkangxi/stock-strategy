from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import tempfile


LIMITATIONS = [
    "这是历史已确认信号的条件执行回放，不是全市场选股回测；无法验证当前筛选器的无偏选股收益。",
    "历史信号沿用当日归档确认价及止损，同一时刻按代码顺序处理资金分配；假设归档确认时刻（14:51—14:55）可按参考价加不利滑点买入，没有同期独立盘口证明可成交，也未重算入场条件。",
    "旧资料统一归档时间不能证明当时留存；未使用10月14日盘后修正信号，仍存在信号选择与存续样本偏差。",
    "快照仅10分钟OHLCV，没有09:25竞价、09:30/09:35精确报价及真实成交额；缺失报价不成交，VWAP组合条件禁用，不估算VWAP。",
    "决策只使用已完成K线；卖出采用决策后下一根K线收盘价，再加不利滑点。14:51决定在15:00模拟成交，不代表真实14:51可成交收益。",
    "止损是离散时点判断而非连续触发，K线内路径、排队、流动性、冲击成本无法还原。首个可验证盘中判断为10:00，午休旧报价延期。",
    "涨跌停幅度按普通主板10%、创业板/科创板20%建模；历史ST、除权和特殊限制元数据不完整，价格冲突及明显异常样本剔除，边界不保证真实可成交。",
    "至少要求真实交易日历T+1、T+2完整快照，按数据质量剔除而非收益剔除；这一覆盖要求本身可能导致样本偏差。",
    "净值按日收盘估值、已扣实际模拟成交费用；最大回撤仅日收盘净值回撤，不代表盘中最大回撤。缺估值明确标记陈旧。",
    "样本短且有限，不做年化、参数寻优、显著性或收益保证；尚无独立样本外、随机买入和完整市场基准对照，不据此认定正期望或改实盘策略。",
]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")


def metric(value, suffix=""):
    return "—" if value is None else f"{value:,.2f}{suffix}"


def render(result, dataset):
    s = result["summary"]
    config = result["config"]
    actions = Counter(d.get("action", "unknown") for d in result["decisions"])
    fills = [f for f in result["fills"] if f.get("status") == "filled"]
    blocked_fills = sum(f.get("status") == "blocked" for f in result["fills"])
    outside_count = sum("entry_outside_range" in e["reason"] for e in dataset.get("exclusions", []))
    quality_count = len(dataset.get("exclusions", [])) - outside_count
    lines = ["A股尾盘策略｜历史确认信号回放报告", "仅模拟研究，不下单，不构成投资建议。", "",
             f"报告编号：{result['report_id']}",
             f"入场范围：{dataset['start']} — {dataset['end']}；观察截至：{dataset['through']}",
             f"范围内有效信号：{len(dataset['signals'])}；范围内质量剔除：{quality_count}；范围外不纳入：{outside_count}；资金/执行跳过：{len(result['skipped'])}",
             f"策略规则版本：{result['policy']['version']}", "", "一、净结果（费用后）",
             f"初始资金：{metric(config['initial_cash'])} 元；期末净值：{metric(s.get('final_equity'))} 元",
             f"账户收益率：{metric(s.get('total_return_pct'), '%')}；日收盘最大回撤：{metric(s.get('max_drawdown_pct'), '%')}",
             f"完整平仓：{s.get('n', len(result['trades']))} 笔；期末未平仓：{len(result['open_positions'])} 笔",
             f"已实现净盈亏：{metric(s.get('realized_pnl', s.get('pnl')))} 元；累计费用：{metric(s.get('fees'))} 元",
             f"完整平仓胜率：{metric(s.get('win_rate'), '%')}；单笔平均净收益：{metric(s.get('mean_net_pct'), '%')}",
             f"金额利润因子：{metric(s.get('amount_profit_factor'))}（无亏损或无交易时不计算）", "",
             "二、资金与成交模型",
             f"每笔预算含费用：{metric(config['allocation'])} 元；最多{config['max_positions']}个持仓；独立现金账户，不借用原BOT资金。",
             f"不利滑点：{config['slippage_pct']}%；佣金{config['commission_rate']*100:.3f}%、最低{config['commission_min']}元；卖出印花税{config['stamp_tax_rate']*100:.3f}%；过户费{config['transfer_fee_rate']*100:.3f}%。",
             "卖出资金仅在未来实际模拟成交时到账，不提前资助当日14:51买入；T+1、整手、重复减仓与涨跌停门禁生效。",
             "判断计数：" + "；".join(f"{key}={count}" for key, count in sorted(actions.items())),
             f"成功模拟成交：{len(fills)} 笔（含买入和分次卖出）；受阻成交：{blocked_fills} 笔；所有判断、股数、原因与输入时刻保存在配套JSON。", "",
             "三、完整平仓明细",
             "入场日 | 代码 名称 | 初始股数 | 买入参考 | 最后退出日 | 净盈亏(元) | 净收益(%)"]
    for t in result["trades"]:
        lines.append(f"{t['entry_date']} | {t['code']} {t.get('name', '')} | {t.get('shares', t.get('initial_shares', '—'))} | "
                     f"{metric(t.get('entry_quote'))} | {t.get('exit_date', '—')} | {metric(t['pnl'])} | {metric(t['net_pct'])}")
    if result["open_positions"]:
        lines += ["", "期末未平仓（不计完整平仓胜率）"]
        for p in result["open_positions"]:
            lines.append(f"{p['code']} {p.get('name', '')}：剩余{p['remaining_shares']}股；入场{p['entry_date']}")
    lines += ["", "四、每日资金曲线", "日期 | 现金(元) | 持仓市值(元) | 净值(元) | 陈旧估值"]
    for e in result["equity"]:
        label = e['date'] + ("（初始）" if e.get('kind') == 'initial' else "（收盘）")
        stale = "是" if e.get('stale', e.get('stale_codes', [])) else "否"
        lines.append(f"{label} | {metric(e.get('cash'))} | {metric(e.get('market_value'))} | "
                     f"{metric(e.get('equity', e.get('nav')))} | {stale}")
    lines += ["", "五、剔除与跳过"]
    for item in dataset.get("exclusions", []) + result["skipped"]:
        lines.append(f"{item.get('id', item.get('position_id', item.get('code', '—')))}：{item.get('reason', item.get('reasons', '—'))}")
    lines += ["", "六、解释边界（必须与收益一同阅读）"] + [f"{i+1}. {text}" for i, text in enumerate(LIMITATIONS)]
    if dataset.get("warnings"):
        lines += ["", "数据警告"] + [str(w) for w in dataset["warnings"]]
    lines += ["", "结论：本次数字仅描述上述存档信号及成交假设下的历史结果；不能推出完整策略有效，更不能保证未来盈利。",
              "完整输入哈希、策略代码哈希、每日净值、逐次判断和成交见配套JSON；原持仓、账本与调度均不参与回放。"]
    return "\n".join(lines) + "\n"


def save_report(result, dataset, output_dir):
    files = sorted(Path(__file__).resolve().parent.glob("*.py"))
    result["policy"] = result.get("policy", {})
    result["dataset_sha256"] = hashlib.sha256(encoded(dataset)).hexdigest()
    result["implementation_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files if not p.name.startswith("test_")}
    result["limitations"] = LIMITATIONS
    result["data_provenance"] = dataset.get("provenance", [])
    result["data_exclusions"] = dataset.get("exclusions", [])
    result["data_warnings"] = dataset.get("warnings", [])
    result["range"] = {k: dataset[k] for k in ("start", "end", "through")}
    result["report_id"] = hashlib.sha256(encoded({k: v for k, v in result.items() if k != "report_id"})).hexdigest()[:16]
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    stem = f"回测_{dataset['start']}_{dataset['end']}_{result['report_id']}"
    text_path, json_path = root / (stem + ".txt"), root / (stem + ".json")
    contents = ((text_path, render(result, dataset).encode("utf-8")), (json_path, encoded(result)))
    staged = []
    try:
        for path, raw in contents:
            if path.exists():
                if path.read_bytes() != raw:
                    raise ValueError("同编号报告内容冲突，禁止覆盖")
                continue
            with tempfile.NamedTemporaryFile(dir=root, suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                staged.append((temporary, path, raw))
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        for temporary, path, raw in staged:
            if path.exists():
                if path.read_bytes() != raw:
                    raise ValueError("同编号报告内容冲突，禁止覆盖")
            else:
                temporary.replace(path)
    finally:
        for temporary, _, _ in staged:
            temporary.unlink(missing_ok=True)
    return text_path, json_path
