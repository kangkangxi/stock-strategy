"""Offline unittest coverage; discovery works with -s BACKTEST or package imports."""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import importlib
import json
from pathlib import Path
import unittest

try:
    from .engine import CST, ReplayEngine, money
except ImportError:
    from engine import CST, ReplayEngine, money


FIRST = "2026-11-16"
SECOND = "2026-11-17"
THIRD = "2026-11-18"
CODE = "600001"
CONFIG = {
    "initial_cash": 100000, "allocation": 20000, "max_positions": 5,
    "commission_rate": 0.00025, "commission_min": 5,
    "stamp_tax_rate": 0.0005, "transfer_fee_rate": 0.00001, "slippage_pct": 0.1,
}


def signal(code=CODE, day=FIRST, price=10, **changes):
    return {"id": f"{code}/{day}", "code": code, "name": "archived",
            "entry_date": day, "generated": f"{day} 14:51", "price": price,
            "plan": {"stop": 8}, "data_ok": True, **changes}


def bar(day, clock, close=10, opening=None, high=None, volume=100, amount=None):
    opening = close if opening is None else opening
    return {"end": f"{day} {clock}", "o": opening,
            "h": max(close, opening) if high is None else high,
            "l": min(close, opening), "c": close, "v": volume, "amount": amount}


def daily(close=10, prev=10, rate=0.1, **changes):
    return {"o": close, "h": close, "l": close, "c": close, "v": 1000,
            "prev_close": prev, "limit_pct": rate, **changes}


def dataset(signals=None, through=SECOND):
    days = [FIRST, SECOND] + ([THIRD] if through >= THIRD else [])
    return {"schema": 1, "scope": "confirmed_signal_replay", "start": FIRST,
            "end": FIRST, "through": through, "calendar": days,
            "signals": [signal()] if signals is None else signals,
            "bars": {CODE: {FIRST: [bar(FIRST, "14:50"), bar(FIRST, "15:00")],
                            SECOND: [bar(SECOND, "09:40"), bar(SECOND, "10:00"),
                                     bar(SECOND, "10:10"), bar(SECOND, "14:50"), bar(SECOND, "15:00")]}},
            "daily": {CODE: {FIRST: daily(), SECOND: daily()}},
            "exclusions": [{"code": "000999", "reason": "not confirmed"}],
            "warnings": ["archival source"], "provenance": {"fixture": True}}


class MockPolicy:
    """Pure local fee math and current schema-2 decision behavior for fixtures."""

    version = "fixture-frozen-schema2-sha"
    metadata = {"schema": 2, "source": "offline mock"}

    def __init__(self, config=None, script=None):
        self.config = {**CONFIG, **(config or {})}
        self.script = script
        self.calls = []

    def execution(self, price, shares, side):
        cfg = self.config
        direction = 1 if side == "buy" else -1
        px = money(Decimal(str(price)) * (1 + direction * Decimal(str(cfg["slippage_pct"])) / 100))
        amount = money(px * shares)
        fees = max(Decimal(str(cfg["commission_min"])), amount * Decimal(str(cfg["commission_rate"])))
        fees += amount * Decimal(str(cfg["transfer_fee_rate"]))
        if side == "sell":
            fees += amount * Decimal(str(cfg["stamp_tax_rate"]))
        fees = money(fees)
        cash = amount + fees if side == "buy" else amount - fees
        return {"price": float(px), "amount": float(amount), "fee": float(fees), "cash": float(cash)}

    def quote_error(self, code, quote, now, side):
        if not quote or quote.get("code") != code:
            return "missing quote"
        at = datetime.strptime(quote["time"], "%Y%m%d%H%M%S").replace(tzinfo=CST)
        if at.date() != now.date() or not 0 <= (now - at).total_seconds() <= 120:
            return "stale quote"
        if any(not quote.get(key) or quote[key] <= 0 for key in ("price", "open", "vol", "limit_up", "limit_down")):
            return "zero volume or missing limits"
        slipped = self.execution(quote["price"], 100, side)["price"]
        if not quote["limit_down"] < quote["price"] < quote["limit_up"] or not quote["limit_down"] < slipped < quote["limit_up"]:
            return "limit boundary"
        return None

    def decide(self, position, quote, indicator, now, slot, partials, force=False):
        self.calls.append(deepcopy({"position": position, "quote": quote, "indicator": indicator,
                                    "now": now, "slot": slot, "partials": partials, "force": force}))
        result = {"action": "hold", "sell_shares": 0, "reason_id": None, "reasons": []}
        error = self.quote_error(position["code"], quote, now, "sell")
        if error:
            return {**result, "action": "defer", "reasons": [error]}
        if slot == "auction":
            return {**result, "action": "observe"}
        if position["entry_date"] >= now.date().isoformat():
            return {**result, "reasons": ["T+1"]}
        if self.script:
            scripted = self.script(position, quote, indicator, now, slot, partials, force)
            if scripted is not None:
                return {**result, **scripted}
        before = position["remaining_shares"]
        px = quote["price"]
        stop = position["plan"].get("stop")
        reason = None
        if stop and px <= stop:
            result["action"], reason = "sell", "plan_stop"
        elif force:
            result["action"], reason = "sell", "force_1451"
        elif not stop:
            return result
        elif quote["open"] > quote["prev_close"] > px:
            result["action"], reason = "sell", "gap_B"
        elif px < quote["open"] < quote["prev_close"]:
            result["action"], reason = "sell", "gap_D"
        elif (indicator.get("ma5_last") and indicator.get("vwap") and
              px <= indicator["ma5_last"] * 0.995 and px < indicator["vwap"]):
            result["action"], reason = "reduce", "ma_vwap"
        elif px <= quote["high"] * 0.97 and px > position["entry_quote"]:
            result["action"], reason = "reduce", "high_pullback"
        elif slot in ("1400", "1430") and (px < quote["prev_close"] or
                                             indicator.get("ma5_last") and px < indicator["ma5_last"]):
            result["action"], reason = "reduce", "afternoon_weak"
        shares = before if result["action"] == "sell" else 0
        if result["action"] == "reduce":
            shares = before // 200 * 100
            minimum = 200 if position["code"].startswith("688") else 100
            repeated = any(event["position_id"] == position["position_id"] and
                           event["at"][:10] == now.date().isoformat() and event["reason_id"] == reason for event in partials)
            if repeated or shares < minimum or before - shares < minimum:
                result["action"], shares = "hold", 0
        return {**result, "reason_id": reason, "sell_shares": shares}


def filled_sells(result):
    return [fill for fill in result["fills"] if fill["side"] == "sell" and fill["status"] == "filled"]


def slot_decision(result, slot, day=SECOND, code=CODE):
    return next(item for item in result["decisions"] if item["slot"] == slot and item["at"][:10] == day and item["code"] == code)


class ReplayEngineTests(unittest.TestCase):
    def test_interface_deterministic_and_inputs_untouched(self):
        data, policy = dataset(), MockPolicy()
        original, config = deepcopy(data), deepcopy(policy.config)
        engine = ReplayEngine(data, policy)
        result = engine.run()
        self.assertEqual(result, engine.run())
        self.assertEqual(data, original)
        self.assertEqual(policy.config, config)
        self.assertTrue({"config", "trades", "decisions", "fills", "skipped", "equity", "open_positions", "summary"} <= result.keys())
        self.assertEqual(result["config"]["policy_version"], policy.version)
        self.assertEqual(result["provenance"], original["provenance"])
        self.assertEqual(result["exclusions"], original["exclusions"])
        self.assertTrue(result["source_limitations"])
        json.dumps(result, allow_nan=False)

    def test_future_high_daily_ohlc_and_prior_session_high_do_not_leak(self):
        data = dataset()
        data["bars"][CODE][FIRST][-1]["h"] = 90
        data["bars"][CODE][SECOND] = [
            bar(SECOND, "09:40", close=10.2, opening=10, high=10.3),
            bar(SECOND, "10:00", close=10.2, high=10.4),
            bar(SECOND, "10:10", close=10.2, high=30),
            bar(SECOND, "14:50", close=10.2), bar(SECOND, "15:00", close=10.2),
        ]
        data["daily"][CODE][SECOND].update({"o": 77, "h": 99, "l": 1, "c": 88, "v": 999999})
        result = ReplayEngine(data, MockPolicy()).run()
        decision = slot_decision(result, "1000")
        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["quote"]["high"], 10.4)
        self.assertEqual(decision["quote"]["open"], 10)
        self.assertEqual(decision["quote"]["vol"], 200)
        self.assertEqual(decision["quote"]["price"], 10.2)
        self.assertEqual(decision["quote"]["prev_close"], 10)
        self.assertEqual(decision["quote"]["limit_up"], 11)
        self.assertEqual(decision["quote"]["limit_down"], 9)
        changed = deepcopy(data)
        changed["bars"][CODE][SECOND][2]["h"] = 80
        changed["daily"][CODE][SECOND]["o"] = 123
        other = ReplayEngine(changed, MockPolicy()).run()
        self.assertEqual(decision, slot_decision(other, "1000"))

    def test_missing_auction_early_session_and_lunch_quotes_defer(self):
        result = ReplayEngine(dataset(), MockPolicy()).run()
        for slot in ("auction", "0930", "0935"):
            decision = slot_decision(result, slot)
            self.assertEqual(decision["quote"], {})
            self.assertEqual(decision["action"], "defer")
        for slot in ("1300", "1305"):
            self.assertEqual(slot_decision(result, slot)["action"], "defer")
        self.assertEqual(len(filled_sells(result)), 1)
        self.assertEqual(filled_sells(result)[0]["at"], f"{SECOND}T15:00:00+08:00")

    def test_ma5_uses_five_completed_bars_across_sessions_no_fake_vwap(self):
        data = dataset()
        data["bars"][CODE][FIRST] = [bar(FIRST, "14:30", 9.6), bar(FIRST, "14:40", 9.8),
                                    bar(FIRST, "14:50", 10), bar(FIRST, "15:00", 10.2)]
        data["bars"][CODE][SECOND] = [bar(SECOND, "09:40", 10.4), bar(SECOND, "10:00", 10.6),
                                     bar(SECOND, "10:10", 70)]
        result = ReplayEngine(data, MockPolicy()).run()
        indicator = slot_decision(result, "1000")["indicator"]
        self.assertAlmostEqual(indicator["ma5_last"], 10.2)
        self.assertEqual(len(indicator["ma5_bar_ends"]), 5)
        self.assertEqual(indicator["ma5_bar_ends"][-1], f"{SECOND} 10:00")
        self.assertIsNone(indicator["vwap"])
        self.assertFalse(indicator["data_missing"])
        self.assertIsNone(indicator["err"])

    def test_vwap_requires_actual_amount_for_all_current_completed_bars(self):
        data = dataset()
        data["bars"][CODE][FIRST][0]["amount"] = 999999
        data["bars"][CODE][SECOND][0].update({"amount": 100000, "v": 100})
        data["bars"][CODE][SECOND][1].update({"amount": 220000, "v": 200})
        result = ReplayEngine(data, MockPolicy()).run()
        self.assertAlmostEqual(slot_decision(result, "1000")["indicator"]["vwap"], 320000 / 30000)
        data["bars"][CODE][SECOND][0]["amount"] = None
        result = ReplayEngine(data, MockPolicy()).run()
        self.assertIsNone(slot_decision(result, "1000")["indicator"]["vwap"])
        self.assertFalse(slot_decision(result, "1000")["indicator"]["data_missing"])

    def test_entry_uses_archival_1452_timestamp_not_earlier_force_slot(self):
        data = dataset(signals=[signal(generated=f"{FIRST} 14:52")])
        result = ReplayEngine(data, MockPolicy()).run()
        self.assertEqual(result["fills"][0]["at"], f"{FIRST}T14:52:00+08:00")
        self.assertEqual(result["skipped"], [])

    def test_force_1451_fills_1500_close_not_the_past_open(self):
        data = dataset()
        data["bars"][CODE][SECOND][-1] = bar(SECOND, "15:00", close=10.8, opening=10.1)
        result = ReplayEngine(data, MockPolicy()).run()
        decision = slot_decision(result, "1451")
        fill = filled_sells(result)[0]
        self.assertEqual(decision["at"], f"{SECOND}T14:51:00+08:00")
        self.assertEqual(decision["quote"]["price"], 10)
        self.assertEqual(decision["scheduled_at"], f"{SECOND}T15:00:00+08:00")
        self.assertEqual(fill["reference_price"], 10.8)
        self.assertEqual(fill["execution"]["price"], 10.79)
        self.assertGreater(fill["at"], fill["decision_at"])
        self.assertEqual(result["summary"]["n"], 1)

    def test_future_sale_proceeds_cannot_finance_1451_entries(self):
        data = dataset(signals=[signal(), signal("600002", SECOND)])
        data["end"] = SECOND
        data["daily"]["600002"] = {SECOND: daily()}
        result = ReplayEngine(data, MockPolicy({"initial_cash": 20000})).run()
        self.assertEqual(len([fill for fill in result["fills"] if fill["side"] == "buy"]), 1)
        self.assertEqual(result["skipped"][0]["reason"], "insufficient_cash_including_fees")
        self.assertEqual(filled_sells(result)[0]["at"], f"{SECOND}T15:00:00+08:00")
        self.assertGreater(result["summary"]["final_cash"], 1000)

    def test_future_close_does_not_release_capacity_before_buy(self):
        data = dataset(signals=[signal(), signal("600002", SECOND)])
        data["end"] = SECOND
        result = ReplayEngine(data, MockPolicy({"max_positions": 1})).run()
        self.assertEqual(result["skipped"][0]["reason"], "position_capacity")
        self.assertEqual(result["summary"]["n"], 1)

    def test_exact_fill_precedes_decision_and_same_time_entry(self):
        data = dataset(signals=[signal(), signal("600002", SECOND)])
        data["end"] = SECOND
        data["daily"]["600002"] = {SECOND: daily()}
        data["bars"][CODE][SECOND] = [bar(SECOND, "09:40", close=10), bar(SECOND, "14:30", close=10),
                                     bar(SECOND, "14:51", close=10.1), bar(SECOND, "15:00", close=10.1)]
        def script(position, quote, indicator, now, slot, partials, force):
            if slot == "1430":
                return {"action": "sell", "sell_shares": position["remaining_shares"], "reason_id": "fixture_exit"}
            return {"action": "hold"}
        result = ReplayEngine(data, MockPolicy({"initial_cash": 20000, "max_positions": 1}, script)).run()
        self.assertEqual(result["skipped"], [])
        at = f"{SECOND}T14:51:00+08:00"
        exact = [fill for fill in result["fills"] if fill["at"] == at]
        self.assertEqual([fill["side"] for fill in exact], ["sell", "buy"])
        self.assertFalse(any(item["code"] == CODE and item["slot"] == "1451" for item in result["decisions"]))

    def test_pending_prevents_decisions_until_resolved_then_allows_new_one(self):
        data = dataset()
        data["bars"][CODE][SECOND] = [bar(SECOND, "09:40"), bar(SECOND, "10:00"), bar(SECOND, "11:00", volume=0),
                                     bar(SECOND, "11:10"), bar(SECOND, "14:50"), bar(SECOND, "15:00")]
        def script(position, quote, indicator, now, slot, partials, force):
            if slot in ("1000", "1100"):
                return {"action": "sell", "sell_shares": position["remaining_shares"], "reason_id": "retry"}
        result = ReplayEngine(data, MockPolicy(script=script)).run()
        self.assertFalse(any(item["slot"] == "1030" for item in result["decisions"]))
        self.assertEqual(slot_decision(result, "1000")["fill_status"], "blocked")
        self.assertEqual(slot_decision(result, "1100")["fill_status"], "filled")
        self.assertEqual(filled_sells(result)[0]["at"], f"{SECOND}T11:10:00+08:00")

    def test_partial_accounting_original_cost_weighted_gross_and_final_pnl_once(self):
        data = dataset()
        data["bars"][CODE][SECOND] = [bar(SECOND, "09:40", close=10.3, opening=10, high=10.8),
                                     bar(SECOND, "10:00", close=10.3), bar(SECOND, "10:10", close=10.4),
                                     bar(SECOND, "10:30", close=10.4), bar(SECOND, "14:50", close=10.4),
                                     bar(SECOND, "15:00", close=10.8)]
        policy = MockPolicy()
        result = ReplayEngine(data, policy).run()
        sells = filled_sells(result)
        self.assertEqual([fill["kind"] for fill in sells], ["partial_sell", "close"])
        trade = result["trades"][0]
        original = trade["buy"]["cash"]
        total_net = money(sum(Decimal(str(fill["execution"]["cash"])) for fill in sells))
        self.assertEqual(trade["pnl"], float(total_net - money(original)))
        self.assertEqual(result["summary"]["pnl"], trade["pnl"])
        self.assertEqual(result["summary"]["realized_pnl"], trade["pnl"])
        self.assertEqual(result["summary"]["n"], 1)
        self.assertEqual(sum(money(fill["allocated_buy_cash"]) for fill in sells), money(original))
        self.assertEqual(sum(money(fill["sale_pnl"]) for fill in sells), money(trade["pnl"]))
        self.assertEqual(sum(fill["shares"] for fill in sells), trade["shares"])
        self.assertEqual(trade["sell"]["cash"], float(total_net))
        self.assertAlmostEqual(trade["net_pct"], trade["pnl"] / original * 100)
        weighted_quote = sum(fill["reference_price"] * fill["shares"] for fill in sells) / trade["shares"]
        self.assertAlmostEqual(trade["gross_pct"], (weighted_quote / 10 - 1) * 100)
        self.assertAlmostEqual(trade["sell"]["price"], trade["sell"]["amount"] / trade["shares"])
        self.assertEqual(len(trade["sell_fills"]), 2)
        self.assertEqual(len(trade["reasons"]), 2)
        self.assertEqual(result["summary"]["fees"], trade["fees"])
        self.assertEqual(result["summary"]["final_cash"], float(money(100000) + money(trade["pnl"])))
        before_fill = next(call for call in policy.calls if call["slot"] == "1000")
        after_fill = next(call for call in policy.calls if call["slot"] == "1030")
        self.assertEqual(before_fill["partials"], [])
        self.assertEqual(after_fill["position"]["remaining_shares"], trade["shares"] - sells[0]["shares"])
        self.assertEqual(after_fill["partials"][0]["at"], sells[0]["at"])
        self.assertEqual(after_fill["partials"][0]["kind"], "partial_sell")

    def test_failed_partial_does_not_consume_reason(self):
        data = dataset()
        data["bars"][CODE][SECOND] = [bar(SECOND, "09:40", close=10.3, opening=10, high=10.8),
                                     bar(SECOND, "10:00", close=10.3), bar(SECOND, "10:10", close=10.3, volume=0),
                                     bar(SECOND, "10:30", close=10.3), bar(SECOND, "10:40", close=10.4),
                                     bar(SECOND, "14:50", close=10.4), bar(SECOND, "15:00", close=10.4)]
        policy = MockPolicy()
        result = ReplayEngine(data, policy).run()
        self.assertEqual(slot_decision(result, "1000")["fill_status"], "blocked")
        self.assertEqual(slot_decision(result, "1030")["fill_status"], "filled")
        self.assertEqual(next(call for call in policy.calls if call["slot"] == "1030")["partials"], [])
        self.assertEqual(len([fill for fill in filled_sells(result) if fill["kind"] == "partial_sell"]), 1)

    def test_fee_inclusive_allocation_and_insufficient_minimum(self):
        result = ReplayEngine(dataset(), MockPolicy()).run()
        buy = next(fill for fill in result["fills"] if fill["side"] == "buy")
        self.assertEqual(buy["shares"], 1900)
        self.assertLessEqual(buy["execution"]["cash"], 20000)
        self.assertGreater(MockPolicy().execution(10, 2000, "buy")["cash"], 20000)
        policy = MockPolicy({"initial_cash": 1001, "allocation": 1001})
        result = ReplayEngine(dataset(), policy).run()
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["skipped"][0]["reason"], "insufficient_cash_including_fees")

    def test_entry_assumption_is_logged_without_fabricated_bar_quote(self):
        data = dataset(through=FIRST)
        data["bars"] = {}
        result = ReplayEngine(data, MockPolicy()).run()
        self.assertEqual(len(result["open_positions"]), 1)
        self.assertEqual(result["fills"][0]["fill_assumption"], "archival_confirmed_price_at_generated_1451_1455")
        self.assertEqual(result["fills"][0]["reference_price"], 10)
        self.assertEqual(result["fills"][0]["at"], f"{FIRST}T14:51:00+08:00")

    def test_688_minimum_200_and_legal_partial_remainder(self):
        code = "688001"
        data = dataset(signals=[signal(code)])
        data["bars"][code] = data["bars"].pop(CODE)
        data["daily"][code] = data["daily"].pop(CODE)
        for allocation, expected in ((1500, 0), (2500, 200), (3500, 300), (4500, 400)):
            with self.subTest(allocation=allocation):
                policy = MockPolicy({"allocation": allocation})
                result = ReplayEngine(data, policy).run()
                buys = [fill for fill in result["fills"] if fill["side"] == "buy"]
                self.assertEqual(buys[0]["shares"] if buys else 0, expected)
        data["bars"][code][SECOND][0] = bar(SECOND, "09:40", 10.3, opening=10, high=10.8)
        data["bars"][code][SECOND][1] = bar(SECOND, "10:00", 10.3)
        for allocation, partial_count in ((3500, 0), (4500, 1)):
            result = ReplayEngine(data, MockPolicy({"allocation": allocation})).run()
            partials = [fill for fill in filled_sells(result) if fill["kind"] == "partial_sell"]
            self.assertEqual(len(partials), partial_count)
            for fill in partials:
                self.assertGreaterEqual(fill["shares"], 200)
                self.assertGreaterEqual(fill["remaining_shares"], 200)
                self.assertEqual(fill["shares"] % 100, 0)

    def test_engine_rejects_illegal_688_lot_from_permissive_policy(self):
        code = "688001"
        data = dataset(signals=[signal(code)])
        data["bars"][code] = data["bars"].pop(CODE)
        data["daily"][code] = data["daily"].pop(CODE)
        def script(position, quote, indicator, now, slot, partials, force):
            if slot == "1000":
                return {"action": "reduce", "sell_shares": 100, "reason_id": "bad_lot"}
        result = ReplayEngine(data, MockPolicy(script=script)).run()
        decision = slot_decision(result, "1000", code=code)
        self.assertEqual(decision["execution_error"], "illegal_sell_lot")
        self.assertEqual(decision["fill_status"], "blocked")
        self.assertEqual(len(filled_sells(result)), 1)

    def test_t_plus_one_and_actual_calendar_not_guessed_weekdays(self):
        data = dataset(through=THIRD)
        data["calendar"] = [FIRST, THIRD]  # Tuesday deliberately absent from the exchange calendar.
        data["bars"][CODE][THIRD] = [bar(THIRD, "09:40"), bar(THIRD, "14:50"), bar(THIRD, "15:00")]
        data["daily"][CODE][THIRD] = daily()
        result = ReplayEngine(data, MockPolicy()).run()
        self.assertFalse(any(fill["side"] == "sell" and fill["at"][:10] == FIRST for fill in result["fills"]))
        self.assertFalse(any(item["at"][:10] == SECOND for item in result["decisions"]))
        self.assertEqual(filled_sells(result)[0]["at"][:10], THIRD)
        self.assertEqual([row["date"] for row in result["equity"] if row["kind"] == "daily_close"], [FIRST, THIRD])
        engine = ReplayEngine(dataset(through=FIRST), MockPolicy())
        engine.run()
        engine.policy.decide = lambda position, *args, **kwargs: {
            "action": "sell", "sell_shares": position["remaining_shares"], "reason_id": "bad_t1"}
        engine._decide(datetime.fromisoformat(f"{FIRST}T14:51:00+08:00"), "1451")
        self.assertEqual(engine.decisions[-1]["execution_error"], "T+1")
        self.assertEqual(engine.pending, {})

    def test_zero_volume_limits_and_slippage_at_future_fill_block(self):
        for changes in ({"v": 0}, {"c": 11, "h": 11}, {"c": 9, "o": 9, "h": 9, "l": 9},
                        {"c": 9.01, "o": 9.01, "h": 9.01, "l": 9.01}):
            with self.subTest(changes=changes):
                data = dataset()
                data["bars"][CODE][SECOND][-1].update(changes)
                result = ReplayEngine(data, MockPolicy()).run()
                self.assertEqual(filled_sells(result), [])
                self.assertEqual(result["summary"]["n"], 0)
                self.assertEqual(result["summary"]["open_count"], 1)
                attempt = next(fill for fill in result["fills"] if fill["side"] == "sell")
                self.assertEqual(attempt["status"], "blocked")
                self.assertEqual(attempt["at"], f"{SECOND}T15:00:00+08:00")
                self.assertEqual(slot_decision(result, "1451")["fill_status"], "blocked")

    def test_missing_prev_close_or_limit_rate_never_borrows_future_daily(self):
        data = dataset()
        data["daily"][CODE] = {FIRST: {"prev_close": 10, "limit_pct": 0.1}, THIRD: daily(close=50)}
        data["through"] = THIRD
        data["calendar"].append(THIRD)
        result = ReplayEngine(data, MockPolicy()).run()
        decision = slot_decision(result, "1000")
        self.assertIsNone(decision["quote"]["prev_close"])
        self.assertNotIn("limit_up", decision["quote"])
        self.assertEqual(decision["action"], "defer")
        self.assertEqual(filled_sells(result), [])

    def test_entry_missing_price_limits_is_not_assumed_tradable(self):
        data = dataset()
        data["daily"] = {}
        result = ReplayEngine(data, MockPolicy()).run()
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["skipped"][0]["reason"], "archival_entry_missing_price_limits")
        self.assertEqual(result["summary"]["final_cash"], 100000)

    def test_limit_prices_round_decimal_cents_and_slipped_entry_blocks(self):
        data = dataset(signals=[signal(price=11)])
        result = ReplayEngine(data, MockPolicy()).run()
        self.assertEqual(result["skipped"][0]["reason"], "archival_entry_limit_boundary")
        data = dataset()
        data["daily"][CODE][SECOND].update({"prev_close": 10.05, "limit_pct": 0.2})
        result = ReplayEngine(data, MockPolicy()).run()
        quote = slot_decision(result, "1000")["quote"]
        self.assertEqual(quote["limit_up"], 12.06)
        self.assertEqual(quote["limit_down"], 8.04)

    def test_no_next_bar_does_not_silently_force_close(self):
        data = dataset()
        data["bars"][CODE][SECOND] = [bar(SECOND, "09:40"), bar(SECOND, "14:50")]
        result = ReplayEngine(data, MockPolicy()).run()
        self.assertEqual(result["summary"]["open_count"], 1)
        self.assertEqual(result["summary"]["n"], 0)
        self.assertEqual(slot_decision(result, "1451")["execution_error"], "no_next_bar_through_observation")
        self.assertEqual(filled_sells(result), [])

    def test_dedup_stable_code_sort_held_and_configured_position_cap(self):
        rows = [signal(f"60000{i}") for i in range(7, 0, -1)]
        rows.append(deepcopy(rows[-1]))
        data = dataset(signals=rows, through=FIRST)
        data["daily"] = {row["code"]: {FIRST: daily()} for row in rows}
        result = ReplayEngine(data, MockPolicy({"initial_cash": 200000, "max_positions": 5})).run()
        self.assertEqual([position["code"] for position in result["open_positions"]],
                         [f"60000{i}" for i in range(1, 6)])
        self.assertEqual(result["summary"]["open_count"], 5)
        self.assertEqual(sum(item["reason"] == "duplicate_signal_id" for item in result["skipped"]), 1)
        self.assertEqual(sum(item["reason"] == "position_capacity" for item in result["skipped"]), 2)
        more = ReplayEngine(data, MockPolicy({"initial_cash": 200000, "max_positions": 10})).run()
        self.assertEqual(more["summary"]["open_count"], 7)
        data = dataset(signals=[signal(), signal(day=SECOND)])
        data["end"] = SECOND
        result = ReplayEngine(data, MockPolicy()).run()
        self.assertEqual(result["skipped"][0]["reason"], "already_held")

    def test_defensive_signal_validation(self):
        cases = [({"data_ok": False}, "unconfirmed_or_invalid_price"),
                 ({"generated": f"{FIRST} 14:50"}, "entry_must_be_generated_1451_1455"),
                 ({"price": float("nan")}, "unconfirmed_or_invalid_price"),
                 ({"id": "wrong"}, "invalid_signal_id"),
                 ({"entry_date": THIRD}, "invalid_signal_id"),
                 ({"code": "not_stock"}, "unsupported_code")]
        for changes, reason in cases:
            with self.subTest(changes=changes):
                result = ReplayEngine(dataset(signals=[signal(**changes)]), MockPolicy()).run()
                self.assertEqual(result["skipped"][0]["reason"], reason)
                self.assertEqual(result["fills"], [])

    def test_open_nav_includes_costs_initial_anchor_close_only_drawdown(self):
        data = dataset(through=THIRD)
        data["bars"][CODE][SECOND] = []
        data["bars"][CODE][THIRD] = []
        data["bars"][CODE][FIRST][0]["l"] = 1  # Intrabar crash is not a daily-close drawdown.
        data["daily"][CODE][FIRST]["c"] = 10.5
        data["daily"][CODE][SECOND]["c"] = 9.5
        data["daily"][CODE][THIRD] = daily(close=10)
        result = ReplayEngine(data, MockPolicy()).run()
        position = result["open_positions"][0]
        cash = money(100000) - money(position["buy"]["cash"])
        navs = [money(100000)] + [cash + money(Decimal(str(close)) * position["remaining_shares"])
                                 for close in (10.5, 9.5, 10)]
        self.assertEqual([row["nav"] for row in result["equity"]], [float(value) for value in navs])
        self.assertAlmostEqual(result["summary"]["max_drawdown_pct"], float((navs[1] - navs[2]) / navs[1] * 100))
        self.assertEqual(result["summary"]["n"], 0)
        self.assertIsNone(result["summary"]["win_rate"])
        self.assertIsNone(result["summary"]["mean_net_pct"])
        self.assertEqual(result["summary"]["pnl"], 0)
        self.assertEqual(result["summary"]["fees"], position["buy"]["fee"])
        self.assertAlmostEqual(result["summary"]["total_return_pct"], float((navs[-1] - 100000) / 100000 * 100))
        self.assertEqual(result["summary"]["unrealized_pnl"], float(navs[-1] - 100000))
        self.assertEqual(position["mark"]["source"], "raw_daily_close")

    def test_missing_daily_close_marks_stale_not_fresh_entry_price(self):
        for historical in (True, False):
            with self.subTest(historical=historical):
                data = dataset()
                data["bars"] = {}
                data["daily"][CODE] = {FIRST: daily(close=10.5)} if historical else {FIRST: {"prev_close": 10, "limit_pct": 0.1}}
                result = ReplayEngine(data, MockPolicy()).run()
                mark = result["open_positions"][0]["mark"]
                self.assertTrue(mark["stale"])
                self.assertTrue(result["equity"][-1]["stale"])
                self.assertEqual(mark["mark_date"], FIRST)
                self.assertEqual(mark["price"], 10.5 if historical else 10)
                self.assertEqual(mark["source"], "stale_daily_close" if historical else "archival_entry_fallback")

    def test_partial_left_open_nav_and_closed_statistics(self):
        data = dataset()
        data["bars"][CODE][SECOND] = [bar(SECOND, "09:40", 10.3, opening=10, high=10.8),
                                     bar(SECOND, "10:00", 10.3), bar(SECOND, "10:10", 10.4)]
        data["daily"][CODE][SECOND]["c"] = 10.6
        result = ReplayEngine(data, MockPolicy()).run()
        self.assertEqual(result["summary"]["n"], 0)
        self.assertEqual(result["summary"]["pnl"], 0)
        self.assertEqual(result["summary"]["open_count"], 1)
        fill = filled_sells(result)[0]
        self.assertEqual(result["summary"]["realized_pnl"], fill["sale_pnl"])
        self.assertEqual(money(result["summary"]["final_equity"]) - money(100000),
                         money(result["summary"]["realized_pnl"]) + money(result["summary"]["unrealized_pnl"]))
        self.assertEqual(result["summary"]["fees"],
                         float(money(result["open_positions"][0]["buy"]["fee"]) + money(fill["execution"]["fee"])))

    def test_amount_profit_factor_is_closed_amount_based_not_return_based(self):
        data = dataset(signals=[signal(), signal("600002", price=20)])
        data["bars"][CODE][SECOND][-1] = bar(SECOND, "15:00", 10.5)
        data["bars"]["600002"] = {FIRST: [bar(FIRST, "14:50", 20), bar(FIRST, "15:00", 20)],
                                  SECOND: [bar(SECOND, "09:40", 20), bar(SECOND, "14:50", 20), bar(SECOND, "15:00", 19)]}
        data["daily"]["600002"] = {FIRST: daily(close=20, prev=20), SECOND: daily(close=19, prev=20)}
        result = ReplayEngine(data, MockPolicy()).run()
        pnls = [trade["pnl"] for trade in result["trades"]]
        self.assertEqual(result["summary"]["n"], 2)
        self.assertEqual(result["summary"]["win_rate"], 50)
        self.assertAlmostEqual(result["summary"]["amount_profit_factor"], max(pnls) / -min(pnls))
        self.assertEqual(result["summary"]["pnl"], float(sum(money(pnl) for pnl in pnls)))

    def test_empty_replay_and_contract_errors(self):
        result = ReplayEngine(dataset(signals=[]), MockPolicy()).run()
        self.assertEqual(result["summary"]["final_equity"], 100000)
        self.assertEqual(result["summary"]["max_drawdown_pct"], 0)
        self.assertIsNone(result["summary"]["amount_profit_factor"])
        for changes in ({"schema": 2}, {"scope": "universe_selection"}, {"through": FIRST, "end": SECOND},
                        {"calendar": None}, {"bars": None}, {"signals": None}):
            with self.subTest(changes=changes):
                data = dataset()
                data.update(changes)
                with self.assertRaises((ValueError, TypeError)):
                    ReplayEngine(data, MockPolicy())


class RealAdapterTests(unittest.TestCase):
    def _policy(self):
        module = importlib.import_module(__package__ + ".adapter" if __package__ else "adapter")
        return module.CurrentSellPolicy()

    def test_real_adapter_if_available(self):
        policy = self._policy()
        result = ReplayEngine(dataset(), policy).run()
        self.assertEqual(result["summary"]["n"], 1)
        self.assertEqual(filled_sells(result)[0]["at"], f"{SECOND}T15:00:00+08:00")
        self.assertEqual(result["config"]["policy_version"], policy.version)
        self.assertEqual(result["policy"]["version"], policy.version)
        self.assertEqual(result["trades"][0]["buy"], policy.execution(10, 1900, "buy"))
        for slot in ("auction", "0930", "0935", "1300", "1305"):
            self.assertEqual(slot_decision(result, slot)["action"], "defer")
        json.dumps(result, allow_nan=False)

    def test_real_ma_only_afternoon_rule_works_without_vwap(self):
        data = dataset()
        data["bars"][CODE][FIRST] = [bar(FIRST, clock, 10.5) for clock in
                                    ("14:20", "14:30", "14:40", "14:50", "15:00")]
        data["bars"][CODE][SECOND] = [bar(SECOND, "09:40", 10.4, opening=10), bar(SECOND, "13:40", 10.4, opening=10),
                                     bar(SECOND, "13:50", 10.4), bar(SECOND, "14:00", 10.2),
                                     bar(SECOND, "14:10", 10.3), bar(SECOND, "14:50", 10.3),
                                     bar(SECOND, "15:00", 10.4)]
        result = ReplayEngine(data, self._policy()).run()
        decision = slot_decision(result, "1400")
        self.assertIsNone(decision["indicator"]["vwap"])
        self.assertFalse(decision["indicator"]["data_missing"])
        self.assertGreater(decision["indicator"]["ma5_last"], decision["quote"]["price"])
        self.assertEqual(decision["action"], "reduce")
        self.assertEqual(decision["reason_id"], "afternoon_weak")
        self.assertEqual([fill["kind"] for fill in filled_sells(result)], ["partial_sell", "close"])
        self.assertEqual(result["summary"]["realized_pnl"], result["trades"][0]["pnl"])

    def test_missing_opening_bar_does_not_invent_gap_scenario(self):
        data = dataset()
        data["bars"][CODE][SECOND] = [bar(SECOND, "10:00", 9.9, opening=10.2),
                                     bar(SECOND, "10:10", 9.9), bar(SECOND, "14:50", 9.9), bar(SECOND, "15:00", 9.9)]
        result = ReplayEngine(data, self._policy()).run()
        decision = slot_decision(result, "1000")
        self.assertIsNone(decision["quote"]["open"])
        self.assertEqual(decision["action"], "defer")
        self.assertEqual(filled_sells(result), [])
        self.assertEqual(result["summary"]["open_count"], 1)

    def test_real_future_high_not_seen_by_current_policy(self):
        data = dataset()
        data["bars"][CODE][SECOND] = [bar(SECOND, "09:40", 10.2, opening=10, high=10.3),
                                     bar(SECOND, "10:00", 10.2, high=10.3),
                                     bar(SECOND, "10:10", 10.2, high=80),
                                     bar(SECOND, "14:50", 10.2), bar(SECOND, "15:00", 10.2)]
        data["daily"][CODE][SECOND]["h"] = 99
        result = ReplayEngine(data, self._policy()).run()
        decision = slot_decision(result, "1000")
        self.assertEqual(decision["quote"]["high"], 10.3)
        self.assertEqual(decision["action"], "hold")


if __name__ == "__main__":
    unittest.main()
