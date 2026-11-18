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
LEGACY_RULES = {
    "schema": 1, "entry": "confirmed_then_fresh_quote_1451_1456",
    "exit": "first_observed_later_weekday_1451_1456",
    "quote_max_age_seconds": 120, "signal_max_age_seconds": 300,
    "fill": "last_price_with_adverse_slippage_no_orderbook_guarantee",
    "lot": "688_minimum_200_other_mainland_minimum_100_multiples_100",
    "net_return_denominator": "buy_amount_plus_buy_fees",
    "calendar": "observed_sessions_only_missing_weekdays_flagged_not_assumed_holidays",
}
RULES = {
    **LEGACY_RULES, "schema": 2,
    "exit": "intraday_plan_stop_or_gap_weakness_full_exit_force_from_1451",
    "reduce": "half_on_ma5_0.995_and_vwap_or_high_3pct_pullback_profit_or_1400_1445_weakness",
    "partial_once": "position_date_reason_id_hundred_share_floor_688_sell_and_remainder_minimum_200",
    "missing_stop": "hold_until_1451_force_no_invented_risk_data",
    "auction": "0925_0927_observation_only_no_execution",
    "legacy_monitor": "schema1_observe_only_settle_with_exact_frozen_rules",
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


def monitor_window(now, slot):
    if now.utcoffset() != timedelta(hours=8) or now.weekday() >= 5:
        return False
    clock = now.strftime("%H%M%S")
    if slot == "auction":
        return "092500" <= clock <= "092759"
    return "093000" <= clock <= "113200" or "130000" <= clock <= "150000"


def auction_quote_error(code, quote, now):
    if not isinstance(quote, dict) or quote.get("code") != code:
        return "缺少匹配代码的报价"
    try:
        stamp = datetime.strptime(quote.get("time", ""), "%Y%m%d%H%M%S").replace(tzinfo=CST)
    except (ValueError, TypeError):
        return "报价时间无效"
    if stamp.date() != now.date() or not 0 <= (now - stamp).total_seconds() <= 120:
        return "报价过期或来自未来"
    if not "092500" <= stamp.strftime("%H%M%S") <= "092759":
        return "非当日竞价报价"
    return None if positive(quote.get("price")) else "竞价价格缺失或无效"


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
        with self.db:
            return self._version(now)

    def _version(self, now):
        cfg = validate_config(json.loads(self.config_path.read_text(encoding="utf-8")))
        engine = self.runtime / SHORT / "scripts" / "tail_momentum.py"
        strategy_path = self.runtime / SHORT / "config.json"
        strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
        spec = {"rules": RULES, "paper_config": cfg, "strategy_config": strategy,
                "engine_object": self.blob(engine.read_bytes()),
                "research_object": self.blob(Path(__file__).read_bytes()),
                "orchestrator_object": self.blob(Path(__file__).with_name("bot.py").read_bytes())}
        digest = self.blob(encoded(spec))
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
                positions[event["position_id"]] = {**event, "remaining_shares": event.get("remaining_shares", event["shares"])}
            elif event["kind"] == "partial_sell":
                cash += money(event["sell"]["cash"])
                position = positions[event["position_id"]]
                position["remaining_shares"] -= event["sell_shares"]
                position["cumulative_sell"] = self.total_sell(position.get("cumulative_sell"), event["sell"],
                                                            position["shares"] - position["remaining_shares"])
                position["exit_quote_amount"] = position.get("exit_quote_amount", 0) + event["exit_quote"] * event["sell_shares"]
            elif event["kind"] == "close":
                cash += money(event.get("execution_sell", event["sell"])["cash"])
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
                            "fill_assumption": self.spec(version)["rules"]["fill"],
                            "plan": audit_value(row["plan"]) if isinstance(row.get("plan"), dict) else {},
                            "prev_close": audit_value(quote.get("prev_close", row.get("prev_close"))),
                            "initial_shares": shares, "remaining_shares": shares}
                    self.append("open/" + position_id, version, "open", now, data)
                    cash -= money(buy["cash"])
                    held.add(code)
                    result["opened"] += 1
            self.finish_run(key, now, result)
        return result

    @staticmethod
    def total_sell(previous, current, sold_shares):
        if not previous:
            return dict(current)
        total = {key: float(money(previous[key]) + money(current[key])) for key in ("amount", "fee", "cash")}
        total["price"] = float(Decimal(str(total["amount"])) / sold_shares)
        return total

    def sell_position(self, version, position, quote, now, input_object, calculator_object,
                      sell_shares, reason_id, decision_id):
        cfg = self.spec(version)["paper_config"]
        sell = execution(quote["price"], sell_shares, "sell", cfg)
        remaining = position["remaining_shares"] - sell_shares
        sold_before = position["shares"] - position["remaining_shares"]
        # Allocate the original buy cash by cumulative shares; rounding telescopes across fills.
        cost_before = money(Decimal(str(position["buy"]["cash"])) * sold_before / position["shares"])
        cost_after = money(Decimal(str(position["buy"]["cash"])) * (sold_before + sell_shares) / position["shares"])
        sale_pnl = money(sell["cash"]) - (cost_after - cost_before)
        common = {"position_id": position["position_id"], "code": position["code"], "name": position.get("name", position["code"]),
                  "entry_date": position["entry_date"], "sell_shares": sell_shares, "remaining_shares": remaining,
                  "shares_before": position["remaining_shares"], "reason_id": reason_id, "decision_id": decision_id,
                  "sale_date": now.date().isoformat(), "sale_pnl": float(sale_pnl), "allocated_buy_cash": float(cost_after - cost_before),
                  "exit_time": now.isoformat(), "exit_quote": quote["price"], "exit_quote_time": quote["time"],
                  "exit_input_object": input_object, "exit_calculator_object": calculator_object}
        if remaining:
            self.append("partial/" + decision_id, version, "partial_sell", now, {**common, "sell": sell})
            return
        total = self.total_sell(position.get("cumulative_sell"), sell, position["shares"])
        pnl = money(total["cash"]) - money(position["buy"]["cash"])
        quote_amount = position.get("exit_quote_amount", 0) + quote["price"] * sell_shares
        data = {k: v for k, v in position.items() if k not in ("kind", "at", "version", "cumulative_sell", "exit_quote_amount")}
        data.update({**common, "sell": total, "execution_sell": sell, "pnl": float(pnl),
                     "net_pct": float(pnl / money(position["buy"]["cash"]) * 100),
                     "gross_pct": (quote_amount / position["shares"] / position["entry_quote"] - 1) * 100,
                     "exit_date": now.date().isoformat(),
                     "unobserved_weekdays": missed_weekdays(position["entry_date"], now.date().isoformat())})
        self.append("close/" + position["position_id"], version, "close", now, data)

    def decision(self, version, position, quote, indicator, now, slot, input_object, decision_version, force=False):
        quote = quote if isinstance(quote, dict) else {}
        indicator = indicator if isinstance(indicator, dict) else {}
        before = position["remaining_shares"]
        reasons = []
        data = {"position_id": position["position_id"], "code": position["code"], "name": position.get("name", position["code"]),
                "version": version, "decision_version": decision_version, "slot": slot, "action": "hold",
                "reasons": reasons, "scenario": None, "quote_time": quote.get("time"),
                "shares_before": before, "sell_shares": 0, "remaining_shares": before, "sell_ratio": 0,
                "entry_date": position["entry_date"], "plan": position.get("plan", {}), "input_object": input_object,
                "reason_id": None}
        if positive(quote.get("price")):
            data["price"] = quote["price"]
        rules = self.spec(version)["rules"]
        error = (auction_quote_error(position["code"], quote, now) if slot == "auction" else
                 quote_error(position["code"], quote, now, "sell", self.spec(version)["paper_config"]))
        if error is None and slot != "auction" and not force:
            quote_clock = quote["time"][8:12]
            if not ("0930" <= quote_clock <= "1130" or "1300" <= quote_clock <= "1500"):
                error = "非盘中报价，禁止用竞价或午休报价模拟成交"
        if rules not in (RULES, LEGACY_RULES):
            data["action"] = "defer"
            reasons.append("历史模拟规则未知，需显式迁移，不用新规则改算旧持仓")
            return audit_value(data)
        if rules == LEGACY_RULES and not force:
            data["action"] = "observe"
            reasons.append("schema1冻结规则仅尾盘settle退出；盘中只观察记录，不模拟减仓或平仓")
            if error:
                reasons.append(error)
            return audit_value(data)
        if error:
            data["action"] = "defer"
            reasons.append(error)
            return audit_value(data)
        px = quote["price"]
        prev = quote.get("prev_close", position.get("prev_close"))
        opening = quote.get("open") if slot != "auction" else px
        if positive(prev) and positive(opening):
            data["scenario"] = ("A" if px >= prev else "B") if opening > prev else (
                "C" if opening == prev else ("D" if px < opening else "E"))
        else:
            reasons.append("昨收或开盘价缺失，跳过gap场景条件")
        ma5, vwap = indicator.get("ma5_last"), indicator.get("vwap")
        technical_ok = not indicator.get("err") and not indicator.get("data_missing")
        if not technical_ok or not positive(ma5) or not positive(vwap):
            reasons.append("技术指标缺失或无效，跳过不可验证的MA/VWAP条件")
        ma_ok = technical_ok and positive(ma5)
        vwap_ok = technical_ok and positive(vwap)
        stop = data["plan"].get("stop") if isinstance(data["plan"], dict) else None
        if not positive(stop):
            reasons.append("计划止损风险数据缺失，不编造止损；仅允许14:51强制卖出")
        if not positive(quote.get("high")):
            reasons.append("日内最高价缺失，跳过3%回落条件")
        if slot == "auction":
            data["action"] = "observe"
            reasons.append("次日竞价仅观察，不模拟成交")
            return audit_value(data)
        if position["entry_date"] >= now.date().isoformat():
            reasons.append("T+1限制，入场当日不得卖出")
            return audit_value(data)
        reason_id = None
        action = "hold"
        if positive(stop) and px <= stop:
            action, reason_id = "sell", "plan_stop"
            reasons.append("触及计划止损，全卖")
        elif force or now.strftime("%H%M") >= "1451":
            action, reason_id = "sell", "force_1451"
            reasons.append("14:51强制尾盘全卖" if rules == RULES else "按schema1冻结尾盘规则结算")
        elif not positive(stop):
            reasons.append("缺少有效计划止损，持有观察")
        elif data["scenario"] == "B":
            action, reason_id = "sell", "gap_B"
            reasons.append("B高开跌破昨收，全卖")
        elif data["scenario"] == "D":
            action, reason_id = "sell", "gap_D"
            reasons.append("D低开且价格跌破开盘，全卖")
        elif ma_ok and vwap_ok and px <= ma5 * 0.995 and px < vwap:
            action, reason_id = "reduce", "ma_vwap"
            reasons.append("价格低于MA5*0.995且低于VWAP，减半")
        elif positive(quote.get("high")) and px <= quote["high"] * 0.97 and px > position["entry_quote"]:
            action, reason_id = "reduce", "high_pullback"
            reasons.append("距日内高点回落至少3%且仍高于入场价，减半")
        elif "1400" <= now.strftime("%H%M") <= "1445" and ((positive(prev) and px < prev) or (ma_ok and px < ma5)):
            action, reason_id = "reduce", "afternoon_weak"
            reasons.append("14:00-14:45低于昨收或MA5，减半")
        else:
            reasons.append("未触发可验证的卖出或减仓条件，持有")
        sell_shares = before if action == "sell" else 0
        if action == "reduce":
            repeated = any(e["kind"] == "partial_sell" and e["position_id"] == position["position_id"] and
                           e["at"][:10] == now.date().isoformat() and e.get("reason_id") == reason_id for e in self.events(version))
            sell_shares = before // 200 * 100
            minimum = 200 if position["code"].startswith("688") else 100
            if repeated or sell_shares < minimum or before - sell_shares < minimum:
                action, sell_shares = "hold", 0
                reasons.append("同一持仓当日同一减仓原因已执行，不重复减仓" if repeated else
                               "股数不足合法半仓整手或剩余最小股数，不作零股/不足200股的科创板减仓")
        data.update({"action": action, "reason_id": reason_id, "sell_shares": sell_shares,
                     "remaining_shares": before - sell_shares, "sell_ratio": sell_shares / before})
        return audit_value(data)

    def monitor(self, quotes, indicators, now, slot):
        if not isinstance(slot, str) or not slot or not monitor_window(now, slot):
            raise ValueError("观察仅允许工作日09:25-09:27竞价或09:30-11:32、13:00-15:00盘中；竞价slot必须为auction")
        key = f"monitor/{now:%Y-%m-%d}/{slot}"
        prior = self.prior_run(key)
        if prior is not None:
            return prior
        input_object = self.blob(encoded(audit_value({"quotes": quotes, "indicators": indicators,
                                                      "observed_at": now.isoformat(), "slot": slot})))
        calculator_object = self.blob(Path(__file__).read_bytes())
        result = {"decisions": [], "closed": 0, "reduced": 0, "deferred": 0, "input_object": input_object}
        quotes = quotes if isinstance(quotes, dict) else {}
        indicators = indicators if isinstance(indicators, dict) else {}
        with self.db:
            decision_version = self._version(now)
            versions = [row[0] for row in self.db.execute("SELECT id FROM versions")]
            for version in versions:
                for position in self.account(version)["positions"].values():
                    code = position["code"]
                    data = self.decision(version, position, quotes.get(code), indicators.get(code), now, slot,
                                         input_object, decision_version)
                    decision_id = f"{key}/{position['position_id']}"
                    self.append("decision/" + decision_id, version, "decision", now, data)
                    result["decisions"].append(data)
                    if data["action"] in ("sell", "reduce"):
                        self.sell_position(version, position, quotes[code], now, input_object, calculator_object,
                                           data["sell_shares"], data["reason_id"], decision_id)
                        result["closed" if data["action"] == "sell" else "reduced"] += 1
                    elif data["action"] == "defer":
                        result["deferred"] += 1
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
        result = {"closed": 0, "deferred": 0, "decisions": [], "input_object": input_object}
        quotes = quotes if isinstance(quotes, dict) else {}
        with self.db:
            decision_version = self._version(now)
            versions = [row[0] for row in self.db.execute("SELECT id FROM versions")]
            for version in versions:
                spec = self.spec(version)
                for position in self.account(version)["positions"].values():
                    if position["entry_date"] >= now.date().isoformat():
                        continue
                    code = position["code"]
                    quote = quotes.get(code)
                    reason = quote_error(code, quote, now, "sell", spec["paper_config"])
                    if spec["rules"] not in (RULES, LEGACY_RULES):
                        reason = "历史模拟规则未知，需显式迁移，不用新规则改算旧持仓"
                    decision_id = f"{key}/{position['position_id']}"
                    data = self.decision(version, position, quote, {}, now, "settle", input_object, decision_version, force=True)
                    self.append("decision/" + decision_id, version, "decision", now, data)
                    result["decisions"].append(data)
                    if reason:
                        self.append(f"defer/{now:%Y-%m-%d}/{position['position_id']}", version, "defer", now,
                                    {"position_id": position["position_id"], "code": code, "reason": reason, "input_object": input_object})
                        result["deferred"] += 1
                        continue
                    reason_id = data["reason_id"] if spec["rules"] == RULES else "legacy_settle"
                    self.sell_position(version, position, quote, now, input_object, calculator_object,
                                       position["remaining_shares"], reason_id, decision_id)
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
            decisions = [e for e in events if e["kind"] == "decision" and start.isoformat() <= e["at"][:10] <= end.isoformat()]
            sales = []
            for event in events:
                if event["kind"] not in ("partial_sell", "close") or not start.isoformat() <= event["at"][:10] <= end.isoformat():
                    continue
                # A close stores the lifetime sell aggregate; period sale detail is only its final execution.
                sold = event.get("sell_shares", event["shares"] if event["kind"] == "close" else 0)
                fill = event.get("execution_sell", event["sell"])
                sales.append({"version": version, "kind": event["kind"], "position_id": event["position_id"],
                              "code": event["code"], "name": event.get("name", event["code"]), "at": event["at"],
                              "entry_date": event["entry_date"], "sale_date": event["at"][:10], "sell_shares": sold,
                              "remaining_shares": event.get("remaining_shares", 0), "sell": fill,
                              "sale_pnl": event.get("sale_pnl", event.get("pnl")), "reason_id": event.get("reason_id"),
                              "input_object": event.get("exit_input_object")})
            action_counts = {action: sum(e["action"] == action for e in decisions)
                             for action in ("hold", "sell", "reduce", "defer", "observe")}
            open_partial = [s for s in sales if s["kind"] == "partial_sell" and s["position_id"] in account["positions"]]
            rows.append({"version": version, "created_at": created, "cash": account["cash"],
                         "decision_details": decisions, "sale_details": sales, "decision_count": len(decisions),
                         "decision_action_counts": action_counts, "sale_count": len(sales),
                         "partial_sale_count": sum(s["kind"] == "partial_sell" for s in sales),
                         "sale_realized_pnl": round(sum(s["sale_pnl"] for s in sales), 2),
                         "sale_fees": round(sum(s["sell"]["fee"] for s in sales), 2),
                         "open_partial_realized_pnl": round(sum(s["sale_pnl"] for s in open_partial), 2),
                         "open_positions": len(account["positions"]),
                         "period_open_positions": sum(start.isoformat() <= p["entry_date"] <= end.isoformat() for p in account["positions"].values()),
                         "skipped_signals": sum(e["kind"] == "skip" and start.isoformat() <= e["entry_date"] <= end.isoformat() for e in events),
                         "deferred_observations": sum((e["kind"] == "defer" or (e["kind"] == "decision" and e["action"] == "defer" and e["slot"] != "settle"))
                                                      and start.isoformat() <= e["at"][:10] <= end.isoformat() for e in events),
                         "gap_trades": sum(t["unobserved_weekdays"] > 0 for t in trades),
                         "period": summarize(trades), "cumulative": summarize(account["closed"])})
        legacy = self.legacy_trades()
        legacy_stats = {"n": len(legacy), "mean_stored_net_pct": sum(t["pct"] for t in legacy) / len(legacy) if legacy else None,
                        "note": "历史版本未知、股数为零时原引擎按100股替代；保持原值，不归入新版本或重算成实盘"}
        data = {"kind": kind, "generated": now.isoformat(), "entry_date_start": start.isoformat(), "entry_date_end": end.isoformat(),
                "observation_date_start": start.isoformat(), "observation_date_end": end.isoformat(),
                "decision_details": [d for row in rows for d in row["decision_details"]],
                "sale_details": [s for row in rows for s in row["sale_details"]],
                "decision_count": sum(row["decision_count"] for row in rows), "sale_count": sum(row["sale_count"] for row in rows),
                "decision_action_counts": {action: sum(row["decision_action_counts"][action] for row in rows)
                                           for action in ("hold", "sell", "reduce", "defer", "observe")},
                "versions": rows, "legacy": legacy_stats,
                "comparison_note": "各版本为独立虚拟账户；不同入场日样本不可作因果比较，不自动排名或调参。未平仓不填零收益。",
                "fill_note": "报价加不利滑点的模拟假设，无盘口成交保证；不连接券商。资金余额不是净值，不生成伪账户回撤。",
                "gap_note": "schema1退出为建仓后首次成功观测的工作日尾盘，schema2可盘中分次退出；中间工作日可能缺行情或为节假日，单独标注，不宣称必为T+1。",
                "sale_note": "入场日统计仅含完整平仓；观察/卖出明细按本期发生日筛选，每笔卖出只计本次成交。未全平仓减仓收益按原买入现金比例分摊费用，单列、不计胜率。"}
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
            lines += [f"本期观察 {row['decision_count']} 条：{row['decision_action_counts']}；卖出 {row['sale_count']} 次（减仓 {row['partial_sale_count']} 次）",
                      f"按卖出日已实现盈亏 {row['sale_realized_pnl']:.2f} 元；卖出费用 {row['sale_fees']:.2f} 元；仍持仓减仓已实现 {row['open_partial_realized_pnl']:.2f} 元（不计胜率）"]
            for decision in row["decision_details"]:
                lines.append(f"观察 {decision['at']} {decision['code']} {decision['name']} {decision['action']} 场景{decision['scenario']} "
                             f"{decision['shares_before']}->{decision['remaining_shares']}股；{'；'.join(decision['reasons'])}")
            for sale in row["sale_details"]:
                lines.append(f"卖出 {sale['at']} {sale['code']} {sale['kind']} {sale['sell_shares']}股 "
                             f"价{sale['sell']['price']:.4f} 现金{sale['sell']['cash']:.2f} 费用{sale['sell']['fee']:.2f} 盈亏{sale['sale_pnl']:.2f}")
            lines.append("")
        lines.append(data["sale_note"])
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
