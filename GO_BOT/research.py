import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

CST = timezone(timedelta(hours=8))
SHORT = "短线策略"
DEFAULT_CONFIG = {
    "initial_cash": 100000, "allocation": 20000, "max_positions": 5,
    "commission_rate": 0.00025, "commission_min": 5,
    "stamp_tax_rate": 0.0005, "transfer_fee_rate": 0.00001,
    "slippage_pct": 0.1,
}
RULES = {
    "schema": 1, "entry": "confirmed_then_fresh_quote_1451_1456",
    "exit": "first_observed_later_weekday_1451_1456",
    "quote_max_age_seconds": 120, "signal_max_age_seconds": 300,
    "fill": "last_price_with_adverse_slippage_no_orderbook_guarantee",
    "lot": "688_minimum_200_other_mainland_minimum_100_multiples_100",
    "net_return_denominator": "buy_amount_plus_buy_fees",
    "calendar": "observed_sessions_only_missing_weekdays_flagged_not_assumed_holidays",
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def audit_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: audit_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [audit_value(item) for item in value]
    return value


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def validate_config(cfg):
    for key in DEFAULT_CONFIG:
        value = cfg.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"研究配置字段无效：{key}")
    if not (0 < cfg["allocation"] <= cfg["initial_cash"]):
        raise ValueError("研究单笔资金必须大于零且不超过初始资金")
    if type(cfg["max_positions"]) is not int or not 1 <= cfg["max_positions"] <= 100:
        raise ValueError("研究最大持仓数必须是 1 至 100 的整数")
    if cfg["slippage_pct"] >= 10 or any(cfg[key] >= 1 for key in
            ("commission_rate", "stamp_tax_rate", "transfer_fee_rate")):
        raise ValueError("研究费用或滑点配置超出允许范围")
    return cfg


def fee(amount, side, cfg):
    value = max(Decimal(str(cfg["commission_min"])), amount * Decimal(str(cfg["commission_rate"])))
    value += amount * Decimal(str(cfg["transfer_fee_rate"]))
    if side == "sell":
        value += amount * Decimal(str(cfg["stamp_tax_rate"]))
    return money(value)


def execution(price, shares, side, cfg):
    direction = 1 if side == "buy" else -1
    px = money(Decimal(str(price)) * (1 + direction * Decimal(str(cfg["slippage_pct"])) / 100))
    amount = money(px * shares)
    cost = fee(amount, side, cfg)
    cash = amount + cost if side == "buy" else amount - cost
    return {"price": float(px), "amount": float(amount), "fee": float(cost), "cash": float(cash)}


def quote_error(code, quote, now, side, cfg):
    if not isinstance(quote, dict) or quote.get("code") != code:
        return "缺少匹配代码的报价"
    try:
        stamp = datetime.strptime(quote.get("time", ""), "%Y%m%d%H%M%S").replace(tzinfo=CST)
    except (ValueError, TypeError):
        return "报价时间无效"
    if stamp.date() != now.date() or not 0 <= (now - stamp).total_seconds() <= 120:
        return "报价过期或来自未来"
    if any(not positive(quote.get(key)) for key in ("price", "open", "vol", "limit_up", "limit_down")):
        return "停牌、零成交量或关键价格缺失"
    low, px, high = quote["limit_down"], quote["price"], quote["limit_up"]
    slipped = execution(px, 100, side, cfg)["price"]
    if not low < px < high or not low < slipped < high:
        return "涨跌停边界或滑点价超出价格限制"
    return None


def trading_window(now):
    return now.utcoffset() == timedelta(hours=8) and now.weekday() < 5 and "1451" <= now.strftime("%H%M") < "1456"


def missed_weekdays(entry, exit_day):
    day = datetime.fromisoformat(entry).date() + timedelta(days=1)
    end = datetime.fromisoformat(exit_day).date()
    count = 0
    while day < end:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


def summarize(trades):
    returns = [t["net_pct"] for t in trades]
    wins = sum(x > 0 for x in returns)
    profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    positive_returns = sum(x for x in returns if x > 0)
    negative_returns = -sum(x for x in returns if x < 0)
    daily = {}
    for trade in trades:
        daily.setdefault(trade["entry_date"], []).append(trade["net_pct"])
    daily_means = [sum(values) / len(values) for values in daily.values()]
    return {
        "n": len(trades), "entry_days": len({t["entry_date"] for t in trades}),
        "win_rate": wins / len(trades) * 100 if trades else None,
        "mean_net_pct": sum(returns) / len(returns) if returns else None,
        "equal_day_mean_net_pct": sum(daily_means) / len(daily_means) if daily_means else None,
        "first_entry_date": min(daily) if daily else None, "last_entry_date": max(daily) if daily else None,
        "realized_pnl": round(sum(t["pnl"] for t in trades), 2),
        "fees": round(sum(t["buy"]["fee"] + t["sell"]["fee"] for t in trades), 2),
        "return_profit_factor": positive_returns / negative_returns if negative_returns else None,
        "amount_profit_factor": profit / loss if loss else None,
    }


class Research:
    def __init__(self, home):
        self.home = Path(home)
        self.runtime = self.home / "runtime"
        self.root = self.home / "research"
        self.config_path = self.home / "research_config.json"
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            with self.config_path.open("xb") as handle:
                handle.write(encoded(DEFAULT_CONFIG))
        self.db = sqlite3.connect(self.root / "research.sqlite3", timeout=30)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA recursive_triggers=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, path TEXT NOT NULL,
                digest TEXT NOT NULL, captured_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS versions (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, spec TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, version TEXT NOT NULL REFERENCES versions(id),
                kind TEXT NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS baselines (
                source TEXT PRIMARY KEY, digest TEXT NOT NULL, captured_at TEXT NOT NULL);
        """)
        for table in ("artifacts", "versions", "events", "runs", "baselines"):
            for operation in ("UPDATE", "DELETE"):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} "
                                "BEGIN SELECT RAISE(ABORT, 'append-only research data'); END")
        self.db.commit()

    def close(self):
        self.db.close()

    def blob(self, data):
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / "objects" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError("历史对象校验失败，拒绝覆盖")
        return digest

    def object(self, digest):
        raw = (self.root / "objects" / digest[:2] / digest).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RuntimeError("历史对象校验失败")
        return raw

    def archive(self, source, directory, now):
        count = 0
        root = Path(directory).resolve()
        home = self.home.resolve()
        with self.db:
            for path in sorted(Path(directory).rglob("*")):
                parts = path.relative_to(directory).parts
                if (not path.is_file() or path.is_symlink() or {"__pycache__", ".git"} & set(parts)
                        or (home in path.resolve().parents and home not in (root, *root.parents))):
                    continue
                relative = path.relative_to(directory).as_posix()
                digest = self.blob(path.read_bytes())
                identity = hashlib.sha256(encoded([source, relative, digest])).hexdigest()
                cursor = self.db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?)",
                                         (identity, source, relative, digest, now.isoformat()))
                count += cursor.rowcount
        return count

    def bootstrap(self, source, now):
        added = 0
        for location, root in (("original", Path(source)), ("runtime", self.runtime / SHORT)):
            added += self.archive(f"{location}/{SHORT}", root, now)
            positions = root / "positions.json"
            if positions.is_file():
                digest = self.blob(positions.read_bytes())
                with self.db:
                    self.db.execute("INSERT OR IGNORE INTO baselines VALUES (?,?,?)", (location, digest, now.isoformat()))
        version = self.version(now)
        return {"added_artifacts": added, "version": version, "legacy_trades": len(self.legacy_trades())}

    def version(self, now):
        cfg = validate_config(json.loads(self.config_path.read_text(encoding="utf-8")))
        engine = self.runtime / SHORT / "scripts" / "tail_momentum.py"
        strategy_path = self.runtime / SHORT / "config.json"
        strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
        spec = {"rules": RULES, "paper_config": cfg, "strategy_config": strategy,
                "engine_object": self.blob(engine.read_bytes()),
                "research_object": self.blob(Path(__file__).read_bytes()),
                "orchestrator_object": self.blob(Path(__file__).with_name("bot.py").read_bytes())}
        digest = self.blob(encoded(spec))
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO versions VALUES (?,?,?)", (digest, now.isoformat(), encoded(spec).decode("utf-8")))
        return digest

    def spec(self, version):
        row = self.db.execute("SELECT spec FROM versions WHERE id=?", (version,)).fetchone()
        if row is None:
            raise ValueError("未知研究版本")
        return json.loads(row[0])

    def events(self, version=None):
        rows = self.db.execute("SELECT version,kind,at,data FROM events ORDER BY rowid").fetchall()
        return [{"version": v, "kind": kind, "at": at, **json.loads(data)} for v, kind, at, data in rows if version is None or v == version]

    def append(self, key, version, kind, now, data):
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (key, version, kind, now.isoformat(), encoded(data).decode("utf-8")))

    def account(self, version):
        cash = money(self.spec(version)["paper_config"]["initial_cash"])
        positions = {}
        closed = []
        for event in self.events(version):
            if event["kind"] == "open":
                cash -= money(event["buy"]["cash"])
                positions[event["position_id"]] = event
            elif event["kind"] == "close":
                cash += money(event["sell"]["cash"])
                positions.pop(event["position_id"])
                closed.append(event)
        return {"cash": float(cash), "positions": positions, "closed": closed}

    def open_codes(self):
        codes = set()
        for (version,) in self.db.execute("SELECT id FROM versions"):
            codes.update(p["code"] for p in self.account(version)["positions"].values())
        return sorted(codes)

    def legacy_trades(self):
        row = self.db.execute("SELECT digest FROM baselines WHERE source='original'").fetchone()
        if row is None:
            return []
        return json.loads(self.object(row[0]))["paper"].get("trades", [])

    def finish_run(self, key, now, result):
        self.db.execute("INSERT INTO runs VALUES (?,?,?)", (key, now.isoformat(), encoded(result).decode("utf-8")))

    def prior_run(self, key):
        row = self.db.execute("SELECT data FROM runs WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def enter(self, version, signal, quotes, now):
        key = f"entry/{now:%Y-%m-%d}"
        prior = self.prior_run(key)
        if prior is not None:
            return prior
        if not trading_window(now):
            raise ValueError("模拟登记仅允许实际交易日 14:51 至 14:56")
        generated = datetime.strptime(signal.get("generated", ""), "%Y-%m-%d %H:%M").replace(tzinfo=CST)
        if signal.get("date") != now.date().isoformat() or not 0 <= (now - generated).total_seconds() <= 300:
            raise ValueError("模拟登记禁止旧日或过期信号")
        rows = signal.get("confirmed")
        if not isinstance(rows, list):
            raise ValueError("确认记录缺失")
        input_object = self.blob(encoded(audit_value({"signal": signal, "quotes": quotes, "observed_at": now.isoformat()})))
        cfg = self.spec(version)["paper_config"]
        result = {"opened": 0, "skipped": 0, "version": version, "input_object": input_object}
        account = self.account(version)
        cash = money(account["cash"])
        held = {p["code"] for p in account["positions"].values()}
        seen = set()
        with self.db:
            for row in rows:
                code = row.get("code", "")
                if code in seen:
                    continue
                seen.add(code)
                quote = quotes.get(code)
                reason = None
                if not re.fullmatch(r"(?:60[0-9]{4}|688[0-9]{3}|00[0-9]{4}|30[0-9]{4})", code):
                    reason = "不支持的市场代码"
                elif row.get("data_ok") is not True or not positive(row.get("price")):
                    reason = "确认数据校验未通过"
                elif code in held or len(held) >= cfg["max_positions"]:
                    reason = "已有同代码持仓或达到持仓上限"
                else:
                    reason = quote_error(code, quote, now, "buy", cfg)
                shares, buy = 0, None
                if reason is None:
                    budget = min(cash, money(cfg["allocation"]))
                    px = execution(quote["price"], 100, "buy", cfg)["price"]
                    shares = int(budget / (money(px) * 100)) * 100
                    minimum = 200 if code.startswith("688") else 100
                    while shares >= minimum and money(execution(quote["price"], shares, "buy", cfg)["cash"]) > budget:
                        shares -= 100
                    if shares < minimum:
                        reason = "可用资金不足最小模拟股数及费用"
                    else:
                        buy = execution(quote["price"], shares, "buy", cfg)
                position_id = f"{version}/{now:%Y-%m-%d}/{code}"
                common = {"position_id": position_id, "code": code, "entry_date": now.date().isoformat(), "input_object": input_object}
                if reason:
                    self.append("skip/" + position_id, version, "skip", now, {**common, "reason": reason})
                    result["skipped"] += 1
                else:
                    data = {**common, "name": row.get("name", code), "shares": shares, "buy": buy,
                            "signal_price": row["price"], "entry_quote": quote["price"], "entry_time": now.isoformat(),
                            "quote_time": quote["time"], "form": row.get("form"), "score": row.get("score"),
                            "fill_assumption": RULES["fill"]}
                    self.append("open/" + position_id, version, "open", now, data)
                    cash -= money(buy["cash"])
                    held.add(code)
                    result["opened"] += 1
            self.finish_run(key, now, result)
        return result

    def settle(self, quotes, now):
        key = f"settle/{now:%Y-%m-%d}"
        prior = self.prior_run(key)
        if prior is not None:
            return prior
        if not trading_window(now):
            raise ValueError("模拟结算仅允许实际交易日 14:51 至 14:56")
        input_object = self.blob(encoded(audit_value({"quotes": quotes, "observed_at": now.isoformat()})))
        calculator_object = self.blob(Path(__file__).read_bytes())
        result = {"closed": 0, "deferred": 0, "input_object": input_object}
        versions = [row[0] for row in self.db.execute("SELECT id FROM versions")]
        with self.db:
            for version in versions:
                cfg = self.spec(version)["paper_config"]
                for position in self.account(version)["positions"].values():
                    if position["entry_date"] >= now.date().isoformat():
                        continue
                    code = position["code"]
                    quote = quotes.get(code)
                    reason = quote_error(code, quote, now, "sell", cfg)
                    if self.spec(version)["rules"] != RULES:
                        reason = "历史模拟规则与当前规则不同，需显式迁移，不用新规则改算旧持仓"
                    if reason:
                        self.append(f"defer/{now:%Y-%m-%d}/{position['position_id']}", version, "defer", now,
                                    {"position_id": position["position_id"], "code": code, "reason": reason, "input_object": input_object})
                        result["deferred"] += 1
                        continue
                    sell = execution(quote["price"], position["shares"], "sell", cfg)
                    pnl = money(sell["cash"]) - money(position["buy"]["cash"])
                    data = {k: v for k, v in position.items() if k not in ("kind", "at", "version")}
                    data.update({"sell": sell, "pnl": float(pnl), "net_pct": float(pnl / money(position["buy"]["cash"]) * 100),
                                 "gross_pct": (quote["price"] / position["entry_quote"] - 1) * 100,
                                 "exit_date": now.date().isoformat(), "exit_time": now.isoformat(), "exit_quote": quote["price"],
                                 "exit_quote_time": quote["time"], "exit_input_object": input_object,
                                 "exit_calculator_object": calculator_object,
                                 "unobserved_weekdays": missed_weekdays(position["entry_date"], now.date().isoformat())})
                    self.append("close/" + position["position_id"], version, "close", now, data)
                    result["closed"] += 1
            self.finish_run(key, now, result)
        return result

    def report(self, kind, now):
        end = now.date()
        if kind == "month":
            end = end.replace(day=1) - timedelta(days=1)
            start = end.replace(day=1)
        elif kind == "week":
            start = end - timedelta(days=end.weekday())
        else:
            start = datetime(1900, 1, 1).date()
        rows = []
        for version, created, _ in self.db.execute("SELECT * FROM versions ORDER BY created_at,id"):
            account = self.account(version)
            events = self.events(version)
            trades = [t for t in account["closed"] if start.isoformat() <= t["entry_date"] <= end.isoformat()]
            rows.append({"version": version, "created_at": created, "cash": account["cash"],
                         "open_positions": len(account["positions"]),
                         "period_open_positions": sum(start.isoformat() <= p["entry_date"] <= end.isoformat() for p in account["positions"].values()),
                         "skipped_signals": sum(e["kind"] == "skip" and start.isoformat() <= e["entry_date"] <= end.isoformat() for e in events),
                         "deferred_observations": sum(e["kind"] == "defer" and start.isoformat() <= e["at"][:10] <= end.isoformat() for e in events),
                         "gap_trades": sum(t["unobserved_weekdays"] > 0 for t in trades),
                         "period": summarize(trades), "cumulative": summarize(account["closed"])})
        legacy = self.legacy_trades()
        legacy_stats = {"n": len(legacy), "mean_stored_net_pct": sum(t["pct"] for t in legacy) / len(legacy) if legacy else None,
                        "note": "历史版本未知、股数为零时原引擎按100股替代；保持原值，不归入新版本或重算成实盘"}
        data = {"kind": kind, "generated": now.isoformat(), "entry_date_start": start.isoformat(), "entry_date_end": end.isoformat(),
                "versions": rows, "legacy": legacy_stats,
                "comparison_note": "各版本为独立虚拟账户；不同入场日样本不可作因果比较，不自动排名或调参。未平仓不填零收益。",
                "fill_note": "报价加不利滑点的模拟假设，无盘口成交保证；不连接券商。资金余额不是净值，不生成伪账户回撤。",
                "gap_note": "退出为建仓后首次成功观测的工作日尾盘；中间工作日可能缺行情或为节假日，单独标注，不宣称必为T+1。"}
        lines = [f"股票持续研究 · {kind}", f"入场日范围：{start} 至 {end}；统计截至 {now:%Y-%m-%d %H:%M:%S}",
                 data["comparison_note"], data["fill_note"], data["gap_note"], "",
                 f"历史账本：{legacy_stats['n']} 笔；历史均净收益 {legacy_stats['mean_stored_net_pct']}%；独立保留、不混算。", ""]
        def fmt(value):
            return "未定义/无样本" if value is None else f"{value:.4f}"
        for row in rows:
            stat = row["period"]
            lines += [f"版本 {row['version'][:12]} · 启用 {row['created_at']}",
                      f"区间已平仓 {stat['n']} 笔 / {stat['entry_days']} 个入场日；区间未平仓 {row['period_open_positions']} 笔；跳过信号 {row['skipped_signals']} 个",
                      f"逐笔净均收益 {fmt(stat['mean_net_pct'])}%；等权入场日净均收益 {fmt(stat['equal_day_mean_net_pct'])}%；胜率 {fmt(stat['win_rate'])}%",
                      f"收益率PF {fmt(stat['return_profit_factor'])} / 金额PF {fmt(stat['amount_profit_factor'])}；已平仓样本入场日 {stat['first_entry_date']} 至 {stat['last_entry_date']}",
                      f"区间已实现盈亏 {stat['realized_pnl']:.2f} 元；费用 {stat['fees']:.2f} 元；观测缺口平仓 {row['gap_trades']} 笔；本期延迟退出记录 {row['deferred_observations']} 条",
                      f"累计已平仓 {row['cumulative']['n']} 笔；累计已实现盈亏 {row['cumulative']['realized_pnl']:.2f} 元；当前现金 {row['cash']:.2f} 元；当前未平仓 {row['open_positions']} 笔", ""]
        payload = encoded(data)
        digest = self.blob(payload)
        reports = self.root / "reports"
        reports.mkdir(exist_ok=True)
        stem = f"{kind}_{now:%Y-%m-%d_%H%M%S_%f}_{digest[:12]}"
        json_path = reports / (stem + ".json")
        text_path = reports / (stem + ".txt")
        for path, content in ((json_path, payload), (text_path, "\n".join(lines).encode("utf-8"))):
            if path.exists():
                if path.read_bytes() != content:
                    raise RuntimeError("拒绝覆盖已有研究报告")
            else:
                with path.open("xb") as handle:
                    handle.write(content)
        return {"json": str(json_path), "text": str(text_path), "data": data}
