from bisect import bisect_right
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import heapq
import re


CST = timezone(timedelta(hours=8))
CENT = Decimal("0.01")
SLOTS = (
    ("09:25", "auction"), ("09:30", "0930"), ("09:35", "0935"),
    ("10:00", "1000"), ("10:30", "1030"), ("11:00", "1100"),
    ("11:30", "1130"), ("13:00", "1300"), ("13:05", "1305"),
    ("13:30", "1330"), ("14:00", "1400"), ("14:30", "1430"),
    ("14:51", "1451"),
)
LIMITATIONS = (
    "Confirmed-signal execution replay only; no universe selection or baseline.",
    "Entries assume the archival confirmed signal price is executable at generated "
    "14:51-14:55, with policy slippage and fees; no contemporaneous order-book guarantee.",
    "Intraday quotes and highs use completed bars only; ten-minute data cannot "
    "reconstruct auction quotes or sub-bar stop paths. Stale quotes defer.",
    "Sells use the strictly next completed bar's close, subject to volume and "
    "price-limit checks; positive volume is not an order-book or fill-capacity guarantee.",
    "VWAP is unavailable unless every completed current-session bar has actual amount; "
    "volume is lots of 100 shares. MA5 can include earlier exchange sessions.",
    "Daily NAV uses raw daily closes without corporate-action adjustment; missing "
    "closes are explicitly stale. Only close-to-close drawdown is measured.",
    "Open positions are not force-liquidated after the observation horizon; "
    "closed-only statistics are small-sample observations, not annualized projections.",
)


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid number") from exc
    if not result.is_finite():
        raise ValueError("non-finite number")
    return result


def money(value):
    return _decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _positive(value):
    try:
        return _decimal(value) > 0
    except ValueError:
        return False


def _actual_amount(value):
    try:
        return _decimal(value) >= 0
    except ValueError:
        return False


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("expected ISO trading date")
    return value


def _time(day, clock):
    return datetime.strptime(day + " " + clock, "%Y-%m-%d %H:%M").replace(tzinfo=CST)


def _stamp(value):
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=CST)
    if parsed.strftime("%Y-%m-%d %H:%M") != value:
        raise ValueError("expected YYYY-MM-DD HH:MM")
    return parsed


def _pct(numerator, denominator):
    return float(numerator / denominator * 100) if denominator else None


class ReplayEngine:
    def __init__(self, dataset, policy):
        self.dataset = deepcopy(dataset)
        self.policy = policy
        self.config = deepcopy(policy.config)
        self._validate()
        self._bar_rows = {}
        self._bar_times = {}
        self._by_day = {}
        self._source_warnings = []
        self._prepare_bars()

    def _validate(self):
        data = self.dataset
        if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
            raise ValueError("dataset schema must be 1")
        if data.get("scope") != "confirmed_signal_replay":
            raise ValueError("dataset scope must be confirmed_signal_replay")
        self.start, self.end, self.through = (_day(data[key]) for key in ("start", "end", "through"))
        if not self.start <= self.end <= self.through:
            raise ValueError("require start <= end <= through")
        calendar = data.get("calendar")
        if not isinstance(calendar, list):
            raise ValueError("calendar must contain actual exchange trading dates")
        self.calendar = sorted({_day(item) for item in calendar})
        self.days = [day for day in self.calendar if self.start <= day <= self.through]
        self._calendar_set = set(self.calendar)
        if not isinstance(data.get("signals"), list):
            raise ValueError("signals must be a list")
        if not isinstance(data.get("bars"), dict) or not isinstance(data.get("daily"), dict):
            raise ValueError("bars and daily must be mappings")
        self.initial_cash = money(self.config["initial_cash"])
        self.allocation = money(self.config["allocation"])
        maximum = self.config["max_positions"]
        if self.initial_cash <= 0 or not 0 < self.allocation <= self.initial_cash:
            raise ValueError("invalid initial cash or allocation")
        if type(maximum) is not int or maximum < 1:
            raise ValueError("max_positions must be a positive integer")
        self.max_positions = maximum

    def _prepare_bars(self):
        for code, dates in self.dataset["bars"].items():
            rows = []
            if not isinstance(dates, dict):
                self._source_warnings.append(f"{code}: invalid bar mapping")
                continue
            for day in sorted(dates):
                if day not in self._calendar_set or day > self.through:
                    continue
                seen = set()
                for bar in dates[day]:
                    try:
                        at = _stamp(bar["end"])
                        clock = at.strftime("%H:%M")
                        if at.date().isoformat() != day or not (
                            "09:30" < clock <= "11:30" or "13:00" < clock <= "15:00"
                        ):
                            raise ValueError("bar end outside its exchange session")
                        if at in seen:
                            raise ValueError("duplicate bar end")
                        if any(not _positive(bar.get(key)) for key in ("o", "h", "l", "c")):
                            raise ValueError("invalid OHLC")
                        if not (_decimal(bar["l"]) <= min(_decimal(bar["o"]), _decimal(bar["c"]))
                                <= max(_decimal(bar["o"]), _decimal(bar["c"])) <= _decimal(bar["h"])):
                            raise ValueError("inconsistent OHLC")
                        if _decimal(bar["v"]) < 0:
                            raise ValueError("negative volume")
                        seen.add(at)
                        rows.append((at, deepcopy(bar)))
                    except (KeyError, TypeError, ValueError) as exc:
                        self._source_warnings.append(f"{code}/{day}: excluded invalid bar ({exc})")
            rows.sort(key=lambda row: row[0])
            self._bar_rows[code] = rows
            self._bar_times[code] = [row[0] for row in rows]
            for at, bar in rows:
                self._by_day.setdefault((code, at.date().isoformat()), []).append((at, bar))

    def _known_daily(self, code, day):
        # Only pre-session metadata is observable before today's close.
        row = self.dataset["daily"].get(code, {}).get(day, {})
        prev = row.get("prev_close")
        if not _positive(prev):
            prior = self._last_daily_close(code, day, strictly_before=True)
            index = bisect_right(self.calendar, day) - 1
            previous_day = self.calendar[index - 1] if index > 0 else None
            prev = prior[1] if prior and prior[0] == previous_day else None
        rate = row.get("limit_pct")
        try:
            valid_rate = _decimal(rate) in (Decimal("0.1"), Decimal("0.2"))
        except ValueError:
            valid_rate = False
        limits = {}
        if _positive(prev) and valid_rate:
            limits = {
                "limit_up": float(money(_decimal(prev) * (1 + _decimal(rate)))),
                "limit_down": float(money(_decimal(prev) * (1 - _decimal(rate)))),
            }
        return {"prev_close": float(_decimal(prev)) if _positive(prev) else None, **limits}

    def _last_daily_close(self, code, day, strictly_before=False):
        rows = self.dataset["daily"].get(code, {})
        for candidate in sorted(rows, reverse=True):
            if candidate not in self._calendar_set:
                continue
            if candidate < day or (candidate == day and not strictly_before):
                close = rows[candidate].get("c")
                if _positive(close):
                    return candidate, close
        return None

    def _snapshot(self, code, now):
        today = self._by_day.get((code, now.date().isoformat()), [])
        times = [row[0] for row in today]
        completed = today[:bisect_right(times, now)]
        history_index = bisect_right(self._bar_times.get(code, []), now)
        history = self._bar_rows.get(code, [])[:history_index][-5:]
        indicator = {
            "ma5_last": float(sum(_decimal(row[1]["c"]) for row in history) / 5) if len(history) == 5 else None,
            "vwap": None, "data_missing": False, "err": None,
            "source": "completed_archival_bars", "ma5_bar_ends": [row[1]["end"] for row in history],
            "last_bar": completed[-1][1]["end"] if completed else None,
        }
        if completed and all(_actual_amount(bar.get("amount")) for _, bar in completed):
            volume = sum(_decimal(bar["v"]) for _, bar in completed)
            if volume > 0:
                indicator["vwap"] = float(sum(_decimal(bar["amount"]) for _, bar in completed) / (volume * 100))
        if indicator["vwap"] is None:
            indicator["notes"] = ["Actual cumulative amount unavailable; VWAP not estimated; MA remains usable."]
        if not completed:
            return {}, indicator
        last_at, last = completed[-1]
        quote = {
            "code": code, "time": last_at.strftime("%Y%m%d%H%M%S"),
            "price": float(_decimal(last["c"])),
            "open": float(_decimal(completed[0][1]["o"])) if completed[0][0].strftime("%H:%M") == "09:40" else None,
            "high": float(max(_decimal(bar["h"]) for _, bar in completed)),
            "vol": float(sum(_decimal(bar["v"]) for _, bar in completed)),
            "bar_volume": float(_decimal(last["v"])), "source": "latest_completed_bar_close",
            **self._known_daily(code, now.date().isoformat()),
        }
        return quote, indicator

    def _push(self, at, priority, key, kind, payload):
        self._sequence += 1
        heapq.heappush(self._events, (at, priority, key, self._sequence, kind, payload))

    def _execution(self, price, shares, side):
        raw = self.policy.execution(price, shares, side)
        result = {"price": float(_decimal(raw["price"]))}
        for key in ("amount", "fee", "cash"):
            result[key] = float(money(raw[key]))
        if not _positive(result["price"]) or not _positive(result["amount"]) or result["fee"] < 0:
            raise ValueError("invalid policy execution")
        expected = money(result["amount"]) + money(result["fee"]) * (1 if side == "buy" else -1)
        if money(result["cash"]) != expected or money(result["amount"]) != money(_decimal(result["price"]) * shares):
            raise ValueError("inconsistent policy execution accounting")
        return result

    @staticmethod
    def _inside_limits(quote, execution):
        return (all(_positive(quote.get(key)) for key in ("limit_down", "limit_up", "price"))
                and _decimal(quote["limit_down"]) < _decimal(quote["price"]) < _decimal(quote["limit_up"])
                and _decimal(quote["limit_down"]) < _decimal(execution["price"]) < _decimal(quote["limit_up"]))

    def _skip(self, signal, reason, at=None):
        self.skipped.append({"id": signal.get("id"), "code": signal.get("code"),
                             "entry_date": signal.get("entry_date"), "at": at.isoformat() if at else None,
                             "reason": reason})

    def _prepare_signals(self):
        seen = set()
        for signal in self.dataset["signals"]:
            if not isinstance(signal, dict):
                self.skipped.append({"reason": "invalid_signal"})
                continue
            identifier = signal.get("id")
            if isinstance(identifier, str) and identifier in seen:
                self._skip(signal, "duplicate_signal_id")
                continue
            if isinstance(identifier, str):
                seen.add(identifier)
            try:
                day = _day(signal["entry_date"])
                code = signal["code"]
                at = _stamp(signal["generated"])
                if not isinstance(code, str) or not re.fullmatch(r"(?:60\d{4}|688\d{3}|00\d{4}|30\d{4})", code):
                    raise ValueError("unsupported_code")
                if identifier != f"{code}/{day}":
                    raise ValueError("invalid_signal_id")
                if not self.start <= day <= self.end:
                    raise ValueError("outside_entry_range")
                if day not in self._calendar_set:
                    raise ValueError("entry_not_exchange_day")
                if at.date().isoformat() != day or not "14:51" <= at.strftime("%H:%M") < "14:56":
                    raise ValueError("entry_must_be_generated_1451_1455")
                if signal.get("data_ok") is not True or not _positive(signal.get("price")):
                    raise ValueError("unconfirmed_or_invalid_price")
                self._push(at, 2, code, "entry", signal)
            except (KeyError, TypeError, ValueError) as exc:
                self._skip(signal, str(exc))

    def _enter(self, signal, now):
        code = signal["code"]
        if any(position["code"] == code for position in self.positions.values()):
            self._skip(signal, "already_held", now)
            return
        if len(self.positions) >= self.max_positions:
            self._skip(signal, "position_capacity", now)
            return
        budget = min(self.cash, self.allocation)
        minimum = 200 if code.startswith("688") else 100
        try:
            unit = self._execution(signal["price"], 100, "buy")
            shares = int(budget / (_decimal(unit["price"]) * 100)) * 100
            buy = None
            while shares >= minimum:
                buy = self._execution(signal["price"], shares, "buy")
                if money(buy["cash"]) <= budget:
                    break
                shares -= 100
            if shares < minimum:
                self._skip(signal, "insufficient_cash_including_fees", now)
                return
            known = self._known_daily(code, signal["entry_date"])
            if "limit_up" not in known or "limit_down" not in known:
                self._skip(signal, "archival_entry_missing_price_limits", now)
                return
            if not self._inside_limits({**known, "price": signal["price"]}, buy):
                self._skip(signal, "archival_entry_limit_boundary", now)
                return
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            self._skip(signal, f"entry_execution_error: {exc}", now)
            return
        position_id = f"{self.policy.version}/{signal['id']}"
        position = {
            "position_id": position_id, "signal_id": signal["id"], "code": code,
            "name": signal.get("name", code), "entry_date": signal["entry_date"],
            "entry_time": now.isoformat(), "entry_quote": float(_decimal(signal["price"])),
            "shares": shares, "initial_shares": shares, "remaining_shares": shares, "buy": buy,
            "plan": deepcopy(signal.get("plan")) if isinstance(signal.get("plan"), dict) else {},
            "prev_close": known["prev_close"], "version": self.policy.version,
            "fill_assumption": "archival_confirmed_price_at_generated_1451_1455",
            "cumulative_sell": None, "exit_quote_amount": 0.0, "sell_fills": [], "reasons": [],
        }
        self.cash = money(self.cash - money(buy["cash"]))
        self.fees = money(self.fees + money(buy["fee"]))
        self.positions[position_id] = position
        self.fills.append({"status": "filled", "kind": "buy", "side": "buy", "at": now.isoformat(),
                           "position_id": position_id, "code": code, "shares": shares,
                           "reference_price": position["entry_quote"], "execution": deepcopy(buy),
                           "fill_assumption": position["fill_assumption"], "cash_after": float(self.cash)})

    def _decide(self, now, slot):
        for identifier in sorted(list(self.positions), key=lambda key: (self.positions[key]["code"], key)):
            if identifier in self.pending:
                continue
            position = self.positions[identifier]
            quote, indicator = self._snapshot(position["code"], now)
            decision = deepcopy(self.policy.decide(deepcopy(position), deepcopy(quote), deepcopy(indicator),
                                                   now, slot, deepcopy(self.partials), force=slot == "1451"))
            decision.update({"position_id": identifier, "code": position["code"], "at": now.isoformat(),
                             "slot": slot, "quote": quote, "indicator": indicator,
                             "decision_id": f"{identifier}/{now.isoformat()}/{slot}", "fill_status": "not_requested"})
            self.decisions.append(decision)
            if decision.get("action") not in ("sell", "reduce"):
                continue
            error = None
            if slot == "auction":
                error = "auction_observation_only"
            elif position["entry_date"] >= now.date().isoformat():
                error = "T+1"
            else:
                error = self._quote_gate(position["code"], quote, now)
            shares = decision.get("sell_shares")
            before = position["remaining_shares"]
            minimum = 200 if position["code"].startswith("688") else 100
            if error is None and (type(shares) is not int or shares % 100 != 0 or not minimum <= shares <= before
                                  or (before - shares != 0 and before - shares < minimum)):
                error = "illegal_sell_lot"
            if error is None and decision["action"] == "sell" and shares != before:
                error = "full_exit_must_sell_remaining"
            if error is None and decision["action"] == "reduce":
                if shares == before:
                    error = "reduction_must_leave_legal_remainder"
                elif any(event["position_id"] == identifier and event["at"][:10] == now.date().isoformat()
                         and event.get("reason_id") == decision.get("reason_id") for event in self.partials):
                    error = "partial_reason_already_filled_today"
            if error:
                decision.update({"action": "defer", "fill_status": "blocked", "execution_error": error})
                continue
            times = self._bar_times.get(position["code"], [])
            index = bisect_right(times, now)
            if index == len(times):
                decision.update({"fill_status": "blocked", "execution_error": "no_next_bar_through_observation"})
                self._blocked(position, decision, now, "no_next_bar_through_observation")
                continue
            fill_at, bar = self._bar_rows[position["code"]][index]
            decision.update({"fill_status": "pending", "scheduled_at": fill_at.isoformat()})
            self.pending[identifier] = decision
            self._push(fill_at, 0, position["code"], "fill", (identifier, bar))

    def _quote_gate(self, code, quote, now):
        if not quote or quote.get("code") != code:
            return "missing_quote"
        try:
            stamp = datetime.strptime(quote["time"], "%Y%m%d%H%M%S").replace(tzinfo=CST)
        except (KeyError, TypeError, ValueError):
            return "invalid_quote_time"
        if stamp.date() != now.date() or not 0 <= (now - stamp).total_seconds() <= 120:
            return "stale_or_future_quote"
        if not _positive(quote.get("vol")):
            return "zero_volume"
        return self.policy.quote_error(code, deepcopy(quote), now, "sell")

    def _blocked(self, position, decision, now, reason):
        decision.update({"fill_status": "blocked", "execution_error": reason, "resolved_at": now.isoformat()})
        self.fills.append({"status": "blocked", "kind": "sell", "side": "sell", "at": now.isoformat(),
                           "position_id": position["position_id"], "code": position["code"],
                           "decision_id": decision["decision_id"], "decision_at": decision["at"],
                           "reason_id": decision.get("reason_id"), "reason": reason,
                           "shares": decision["sell_shares"]})

    def _fill(self, identifier, bar, now):
        decision = self.pending.pop(identifier, None)
        position = self.positions.get(identifier)
        if decision is None or position is None:
            return
        quote, _ = self._snapshot(position["code"], now)
        error = "T+1" if position["entry_date"] >= now.date().isoformat() else None
        if error is None and not _positive(bar.get("v")):
            error = "zero_fill_bar_volume"
        if error is None:
            error = self._quote_gate(position["code"], quote, now)
        sell = None
        if error is None:
            try:
                # The next bar's OPEN predates this decision and is never executable here.
                sell = self._execution(bar["c"], decision["sell_shares"], "sell")
                if not self._inside_limits(quote, sell):
                    error = "fill_limit_boundary_or_slippage"
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                error = f"sell_execution_error: {exc}"
        if error:
            self._blocked(position, decision, now, error)
            return
        shares = decision["sell_shares"]
        before = position["remaining_shares"]
        sold_before = position["shares"] - before
        original_cost = money(position["buy"]["cash"])
        cost_before = money(original_cost * sold_before / position["shares"])
        cost_after = money(original_cost * (sold_before + shares) / position["shares"])
        allocated = money(cost_after - cost_before)
        sale_pnl = money(money(sell["cash"]) - allocated)
        self.cash = money(self.cash + money(sell["cash"]))
        self.fees = money(self.fees + money(sell["fee"]))
        self.realized_pnl = money(self.realized_pnl + sale_pnl)
        position["remaining_shares"] -= shares
        cumulative = position["cumulative_sell"] or {"amount": 0, "fee": 0, "cash": 0}
        total = {key: float(money(money(cumulative[key]) + money(sell[key]))) for key in ("amount", "fee", "cash")}
        total["price"] = float(_decimal(total["amount"]) / (sold_before + shares))
        position["cumulative_sell"] = total
        reference_amount = _decimal(position["exit_quote_amount"]) + _decimal(bar["c"]) * shares
        position["exit_quote_amount"] = float(reference_amount)
        kind = "partial_sell" if position["remaining_shares"] else "close"
        fill = {
            "status": "filled", "kind": kind, "side": "sell", "at": now.isoformat(),
            "position_id": identifier, "code": position["code"], "decision_id": decision["decision_id"],
            "decision_at": decision["at"], "reason_id": decision.get("reason_id"),
            "reasons": deepcopy(decision.get("reasons", [])), "shares": shares, "sell_shares": shares,
            "shares_before": before, "remaining_shares": position["remaining_shares"],
            "reference_price": float(_decimal(bar["c"])), "execution": sell,
            "allocated_buy_cash": float(allocated), "sale_pnl": float(sale_pnl),
            "cash_after": float(self.cash), "fill_assumption": "strictly_next_bar_end_close",
        }
        self.fills.append(fill)
        position["sell_fills"].append(deepcopy(fill))
        position["reasons"].append({"at": now.isoformat(), "reason_id": decision.get("reason_id"),
                                     "reasons": deepcopy(decision.get("reasons", []))})
        decision.update({"fill_status": "filled", "resolved_at": now.isoformat()})
        if kind == "partial_sell":
            self.partials.append({"kind": kind, "position_id": identifier, "at": now.isoformat(),
                                  "reason_id": decision.get("reason_id")})
        else:
            pnl = money(money(total["cash"]) - original_cost)
            trade = deepcopy(position)
            trade.update({"exit_date": now.date().isoformat(), "exit_time": now.isoformat(), "sell": total,
                          "execution_sell": sell, "pnl": float(pnl), "net_pct": _pct(pnl, original_cost),
                          "gross_pct": float((reference_amount / position["shares"] /
                                              _decimal(position["entry_quote"]) - 1) * 100),
                          "fees": float(money(money(position["buy"]["fee"]) + money(total["fee"])))})
            self.trades.append(trade)
            del self.positions[identifier]

    def _mark(self, position, day):
        latest = self._last_daily_close(position["code"], day)
        if latest:
            mark_date, price = latest
            stale = mark_date != day
            source = "stale_daily_close" if stale else "raw_daily_close"
        else:
            index = bisect_right(self._bar_times.get(position["code"], []), _time(day, "15:00"))
            if index:
                at, bar = self._bar_rows[position["code"]][index - 1]
                mark_date, price, source = at.date().isoformat(), bar["c"], "bar_close_fallback"
            else:
                mark_date, price, source = position["entry_date"], position["entry_quote"], "archival_entry_fallback"
            stale = True
        value = money(_decimal(price) * position["remaining_shares"])
        sold = position["shares"] - position["remaining_shares"]
        remaining_cost = money(money(position["buy"]["cash"]) -
                               money(money(position["buy"]["cash"]) * sold / position["shares"]))
        return {"position_id": position["position_id"], "code": position["code"],
                "remaining_shares": position["remaining_shares"], "price": float(_decimal(price)),
                "mark_date": mark_date, "source": source, "stale": stale,
                "market_value": float(value), "remaining_buy_cash": float(remaining_cost),
                "unrealized_pnl": float(money(value - remaining_cost))}

    def _nav(self, now, initial=False):
        marks = [] if initial else [self._mark(self.positions[key], now.date().isoformat())
                                   for key in sorted(self.positions)]
        value = money(sum((money(mark["market_value"]) for mark in marks), Decimal(0)))
        nav = money(self.cash + value)
        self.peak = max(self.peak, nav)
        drawdown = _pct(self.peak - nav, self.peak)
        self.max_drawdown = max(self.max_drawdown, drawdown)
        row = {"date": now.date().isoformat(), "at": now.isoformat(),
               "kind": "initial" if initial else "daily_close", "cash": float(self.cash),
               "market_value": float(value), "equity": float(nav), "nav": float(nav),
               "realized_pnl": float(self.realized_pnl),
               "unrealized_pnl": float(money(sum((money(mark["unrealized_pnl"]) for mark in marks), Decimal(0)))),
               "fees": float(self.fees), "drawdown_pct": drawdown,
               "stale": any(mark["stale"] for mark in marks), "marks": marks}
        self.equity.append(row)

    def run(self):
        self.cash = self.initial_cash
        self.fees = Decimal("0.00")
        self.realized_pnl = Decimal("0.00")
        self.peak = self.initial_cash
        self.max_drawdown = 0.0
        self.positions, self.pending = {}, {}
        self.trades, self.decisions, self.fills, self.skipped, self.equity, self.partials = [], [], [], [], [], []
        self._events, self._sequence = [], 0
        self._nav(_time(self.start, "09:00"), initial=True)
        self._prepare_signals()
        for day in self.days:
            for clock, slot in SLOTS:
                self._push(_time(day, clock), 1, "", "decision", slot)
            self._push(_time(day, "15:00"), 3, "", "nav", None)
        while self._events:
            now, _, _, _, kind, payload = heapq.heappop(self._events)
            if kind == "entry":
                self._enter(payload, now)
            elif kind == "decision":
                self._decide(now, payload)
            elif kind == "fill":
                identifier, bar = payload
                self._fill(identifier, bar, now)
            else:
                self._nav(now)
        closed_pnl = money(sum((money(trade["pnl"]) for trade in self.trades), Decimal(0)))
        profit = sum((money(trade["pnl"]) for trade in self.trades if trade["pnl"] > 0), Decimal(0))
        loss = -sum((money(trade["pnl"]) for trade in self.trades if trade["pnl"] < 0), Decimal(0))
        final_nav = money(self.equity[-1]["nav"])
        summary = {
            "n": len(self.trades), "open_count": len(self.positions),
            "win_rate": sum(trade["pnl"] > 0 for trade in self.trades) / len(self.trades) * 100 if self.trades else None,
            "pnl": float(closed_pnl), "realized_pnl": float(self.realized_pnl), "fees": float(self.fees),
            "total_return_pct": _pct(final_nav - self.initial_cash, self.initial_cash),
            "max_drawdown_pct": self.max_drawdown,
            "mean_net_pct": sum(trade["net_pct"] for trade in self.trades) / len(self.trades) if self.trades else None,
            "amount_profit_factor": float(profit / loss) if loss else None,
            "final_cash": float(self.cash), "final_equity": float(final_nav),
            "unrealized_pnl": self.equity[-1]["unrealized_pnl"],
        }
        open_positions = []
        for identifier in sorted(self.positions):
            position = deepcopy(self.positions[identifier])
            position["mark"] = self._mark(position, self.days[-1] if self.days else self.through)
            open_positions.append(position)
        return deepcopy({
            "config": {**self.config, "effective_max_positions": self.max_positions,
                       "policy_version": self.policy.version, "policy_metadata": deepcopy(self.policy.metadata),
                       "schema": 1, "scope": self.dataset["scope"], "start": self.start, "end": self.end,
                       "through": self.through, "timezone": "+08:00", "source_limitations": list(LIMITATIONS)},
            "trades": self.trades, "decisions": self.decisions, "fills": self.fills,
            "skipped": self.skipped, "equity": self.equity, "open_positions": open_positions, "summary": summary,
            "policy": {**deepcopy(self.policy.metadata), "version": self.policy.version},
            "warnings": deepcopy(self.dataset.get("warnings", [])) + self._source_warnings,
            "exclusions": deepcopy(self.dataset.get("exclusions", [])),
            "provenance": deepcopy(self.dataset.get("provenance", {})), "source_limitations": list(LIMITATIONS),
        })
