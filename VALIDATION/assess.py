import hashlib
import json
import os
import uuid
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

try:
    from .evidence import Recorder, selection_chain
except ImportError:
    from evidence import Recorder, selection_chain

CST = timezone(timedelta(hours=8))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def trace(directory):
    if not (directory / "seal.json").is_file():
        raise ValueError("TraceNotSealed")
    reader = Recorder.reader(directory)
    if reader.health()["status"] != "ok":
        raise ValueError("TraceIncomplete")
    return reader.summary()


def research_input(home, digest):
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("ResearchInputMissing")
    path = home / "research" / "objects" / digest[:2] / digest
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("ResearchInputMismatch")
    return json.loads(raw)


def auditable_trade(home, trade, opened, sales, input_links):
    try:
        checks = [(opened, opened["input_object"], "entry_quote")]
        checks += [(event, event["exit_input_object"], "exit_quote") for event in sales]
        if not sales or sum(event["sell_shares"] for event in sales) != opened["shares"]:
            return False
        for event, digest, price_key in checks:
            observed = research_input(home, digest)
            if digest not in input_links or observed["observed_at"] != event["at"]:
                return False
            quote = observed["quotes"][event["code"]]
            if quote["price"] != event[price_key]:
                return False
            quote_time = datetime.strptime(quote["time"], "%Y%m%d%H%M%S").replace(tzinfo=CST)
            if not 0 <= (datetime.fromisoformat(event["at"]) - quote_time).total_seconds() <= 120:
                return False
        return True
    except (KeyError, TypeError, OSError, ValueError):
        return False


def trade_stats(trades):
    values = [Decimal(str(t["pnl"])) for t in trades]
    wins = sum((v for v in values if v > 0), Decimal(0))
    losses = -sum((v for v in values if v < 0), Decimal(0))
    total = sum(values, Decimal(0))
    best = max(values) if values else None
    return {"closed": len(values), "pnl": float(total),
            "win_rate_pct": sum(v > 0 for v in values) / len(values) * 100 if values else None,
            "amount_profit_factor": float(wins / losses) if losses else None,
            "mean_net_pct": sum(t["net_pct"] for t in trades) / len(trades) if trades else None,
            "fees": float(sum((Decimal(str(t["buy"]["fee"])) + Decimal(str(t["sell"]["fee"])) for t in trades), Decimal(0))),
            "best_trade_pnl": float(best) if best is not None else None,
            "pnl_without_best_trade": float(total - best) if best is not None else None}


def assess(home, now):
    home = Path(home)
    root = home / "validation"
    campaign = json.loads((root / "campaign.json").read_text(encoding="utf-8"))
    identity = json.dumps({k: v for k, v in campaign.items() if k != "campaign_id"},
                          sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(identity).hexdigest() != campaign["campaign_id"]:
        raise ValueError("冻结验证清单哈希不匹配")
    started = datetime.fromisoformat(campaign["started_at"])
    if now < started:
        raise ValueError("观察时间早于验证开始")
    version = campaign["version"]
    database = home / "research" / "research.sqlite3"
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        spec_row = connection.execute("SELECT spec FROM versions WHERE id=?", (version,)).fetchone()
        if spec_row is None or json.loads(spec_row[0]) != campaign["spec"]:
            raise ValueError("冻结版本或参数与模拟账本不匹配")
        rows = connection.execute("SELECT kind,at,data FROM events WHERE version=? ORDER BY rowid", (version,)).fetchall()
    finally:
        connection.close()
    ledger = [{"kind": kind, "at": at, **json.loads(raw)} for kind, at, raw in rows
              if started <= datetime.fromisoformat(at) <= now]
    paired = {}
    day_runs = {}
    input_links = set()
    coverage = []
    environments = []
    for directory in sorted((root / "runs").glob("*/*")):
        if not directory.is_dir():
            continue
        row = {"path": str(directory), "status": "incomplete"}
        try:
            events = trace(directory)
            ends = [event for event in events if event["kind"] == "run_end"]
            if len(ends) != 1:
                raise ValueError("RunEndMissing")
            end = ends[0]
            if not started <= datetime.fromisoformat(end["observed_at"]) <= now:
                continue
            data = end["data"]
            if data.get("version") != version:
                continue
            row.update(job=data["job"], day=directory.parent.name)
            if data["status"] != "done":
                raise ValueError("RunFailed")
            if data["job"] in ("scan", "pm"):
                required = {data["job"] + "_final", "config", "http", "worker_end"}
                if not required.issubset({event["kind"] for event in events}):
                    raise ValueError("SelectionInputMissing")
            paper = data.get("paper") or {}
            if data["job"] in ("pm", "paper_settle") or data["job"].startswith("exit_"):
                if not any(event["kind"] == "http" and "raw" in event["data"] for event in events):
                    raise ValueError("ExecutionSourceMissing")
                if paper.get("input_object"):
                    input_links.add(paper["input_object"])
            row["status"] = "ok"
            if data["job"] in ("scan", "pm"):
                paired.setdefault(directory.parent.name, set()).add(data["job"])
                day_runs.setdefault(directory.parent.name, {})[data["job"]] = directory
            for event in events:
                if event["kind"] == "scan_market":
                    market = event["data"]
                    environments.append({"day": directory.parent.name, "source": market["source"],
                                         "observed_rows": len(market["rows"]), "market_total": market.get("market_total"),
                                         "coverage": market.get("coverage"), "environment": market.get("environment")})
        except (OSError, ValueError, KeyError, TypeError) as error:
            row["error_type"] = type(error).__name__
        coverage.append(row)
    complete_days = []
    for day, jobs in sorted(paired.items()):
        if jobs == {"scan", "pm"}:
            try:
                selection_chain(day_runs[day]["scan"], day_runs[day]["pm"])
                complete_days.append(day)
            except (OSError, ValueError, TypeError, KeyError) as error:
                coverage.append({"day": day, "job": "selection_chain", "status": "incomplete", "error_type": type(error).__name__})
    opens = {e["position_id"]: e for e in ledger if e["kind"] == "open"}
    closed = [e for e in ledger if e["kind"] == "close" and e["position_id"] in opens]
    auditable = [e for e in closed if e["entry_date"] in complete_days and auditable_trade(
        home, e, opens[e["position_id"]],
        [sale for sale in ledger if sale["kind"] in ("partial_sell", "close") and sale["position_id"] == e["position_id"]],
        input_links)]
    remaining = set(opens) - {e["position_id"] for e in closed}
    qualified = len(complete_days) >= 60 and len(auditable) >= 100
    limitations = [
        "只记录冻结版本启用后的真实前向观测；历史回放和旧版本不混入。",
        "涨幅榜只留存策略实际抓取的页面，不是全市场横截面；未抓取股票为未观察，不能视为被淘汰。",
        "完整日仅指初筛及确认的输入链齐全；可审计平仓另核验入场、每次卖出输入哈希及时间关联，不代表所有盘中时点都有行情或真实可成交。",
        "60个完整观察日且100笔可审计平仓仅触发复查，不是统计显著性或策略可行性的证明。",
        "观察涨跌/震荡/下跌阶段需真实积累并另做市场状态划分；这里保留原始市场环境，不伪造行情阶段覆盖。",
        "同批信号次日收盘卖出、随机买入及市场基准对照尚待独立评估，不把原影子池直接视为合格基准。",
        "当前统计是完整平仓样本净盈亏，不是账户净值收益；没有完整每日估值时不输出账户回撤或年化。",
        "缺失留存样本同时列入全部前向平仓统计与覆盖缺口，不按盈利或亏损选择剔除。",
        "报价加不利滑点仅为模拟成交，无盘口保证；不下单，不自动调参，不据此启用实盘。",
    ]
    return {"schema": 1, "generated": now.isoformat(), "campaign_id": campaign["campaign_id"],
            "version": version, "started_at": campaign["started_at"],
            "review_status": "达到复查门槛，仍须样本外与基准验证" if qualified else "积累样本中，未验证可行性",
            "goals": {"complete_days": 60, "auditable_closed": 100}, "complete_days": complete_days,
            "all_forward_closed": trade_stats(closed), "auditable_closed": trade_stats(auditable),
            "closed_missing_audit_trace": len(closed) - len(auditable), "open_positions": len(remaining),
            "skip_count": sum(e["kind"] == "skip" for e in ledger),
            "decision_actions": {action: sum(e["kind"] == "decision" and e.get("action") == action for e in ledger)
                                 for action in ("hold", "sell", "reduce", "defer", "observe")},
            "run_coverage": coverage, "market_environments": environments, "limitations": limitations}


def save_report(result, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = encoded(result)
    digest = hashlib.sha256(payload).hexdigest()
    stem = "前向验证_" + digest[:16]
    lines = ["A股尾盘策略｜冻结版本前向验证进度", "仅模拟研究，不下单，不构成投资建议。",
             f"验证开始：{result['started_at']}；统计截至：{result['generated']}",
             f"冻结版本：{result['version']}", f"状态：{result['review_status']}",
             f"完整初筛/确认观察日：{len(result['complete_days'])}/60；可审计完整平仓：{result['auditable_closed']['closed']}/100",
             f"全部前向完整平仓：{result['all_forward_closed']['closed']}；其中缺完整审计留存：{result['closed_missing_audit_trace']}；未平仓：{result['open_positions']}",
             f"全部平仓净盈亏：{result['all_forward_closed']['pnl']:.2f} 元；累计完整平仓费用：{result['all_forward_closed']['fees']:.2f} 元",
             f"完整平仓胜率：{result['all_forward_closed']['win_rate_pct']}%；金额利润因子：{result['all_forward_closed']['amount_profit_factor']}",
             f"去掉最大盈利/最佳交易后的净盈亏：{result['all_forward_closed']['pnl_without_best_trade']} 元", "",
             "解释边界（必须与结果一同阅读）", *[f"{i}. {text}" for i, text in enumerate(result["limitations"], 1)]]
    output = {}
    for extension, raw in (("json", payload), ("txt", "\n".join(lines).encode("utf-8"))):
        path = directory / (stem + "." + extension)
        if path.exists():
            if path.read_bytes() != raw:
                raise RuntimeError("拒绝覆盖不同内容的验证报告")
        else:
            temporary = directory / (stem + "." + extension + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        output[extension] = str(path)
    return output
