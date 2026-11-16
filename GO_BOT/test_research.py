import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import patch

import research


CST = timezone(timedelta(hours=8))


def at(day, clock="14:52:00"):
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=CST)


def cents(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def expected_execution(price, shares, side, cfg):
    """Independent Decimal oracle, not a call to the implementation's fee helpers."""
    slip = Decimal(str(cfg["slippage_pct"])) / Decimal(100)
    multiplier = Decimal(1) + slip if side == "buy" else Decimal(1) - slip
    fill = cents(Decimal(str(price)) * multiplier)
    amount = cents(fill * shares)
    commission = max(Decimal(str(cfg["commission_min"])),
                     amount * Decimal(str(cfg["commission_rate"])))
    transfer = amount * Decimal(str(cfg["transfer_fee_rate"]))
    stamp = amount * Decimal(str(cfg["stamp_tax_rate"])) if side == "sell" else Decimal(0)
    fees = cents(commission + transfer + stamp)
    cash = amount + fees if side == "buy" else amount - fees
    return {"price": float(fill), "amount": float(amount),
            "fee": float(fees), "cash": float(cash)}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="go_bot_research_tests_")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.now = at("2026-11-02")
        self.home = self.base / "robot home"
        self.original = self.base / "source originals"
        self.history = self.base / "history archive"
        self.legacy = [
            {"code": "600090", "date": "2025-12-29", "shares": 0,
             "pct": 1.75, "version": "legacy-unknown", "note": "保留原始历史"},
            {"code": "000090", "date": "2026-01-05", "shares": 100,
             "pct": -0.25, "version": "legacy-unknown"},
        ]
        self.make_runtime(self.home)
        self.write_json(self.original / "positions.json",
                        {"paper": {"cash": 54321, "positions": [], "trades": self.legacy}})
        self.write_json(self.original / "config.json", {"original": True})
        self.write_bytes(self.original / "scripts" / "tail_momentum.py",
                         b"# historical strategy revision\nTHRESHOLD = 1\n")
        self.write_json(self.history / "history.json",
                        {"history": [{"date": "2025-12-29", "code": "600090"}]})
        self.r = research.Research(self.home)
        self.addCleanup(self.r.close)
        self.v = self.r.version(self.now)

    def write_bytes(self, path, data):
        path = Path(path)
        # Every fixture helper must remain within this test's disposable directory.
        self.assertTrue(path.resolve().is_relative_to(self.base))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def write_json(self, path, data):
        self.write_bytes(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def make_runtime(self, home):
        runtime = home / "runtime"
        self.write_bytes(runtime / research.SHORT / "scripts" / "tail_momentum.py",
                         b"# test-only strategy snapshot; never executed\nTHRESHOLD = 2\n")
        self.write_json(runtime / research.SHORT / "config.json",
                        {"threshold": 2, "filters": {"volume": 1.5, "enabled": True}})
        self.write_json(runtime / research.SHORT / "positions.json",
                        {"paper": {"trades": [{"code": "600099", "pct": 99}], "positions": []}})

    def book(self, name, cfg=None, now=None):
        home = self.base / name
        self.make_runtime(home)
        r = research.Research(home)
        self.addCleanup(r.close)
        if cfg is not None:
            self.write_json(r.config_path, {**research.DEFAULT_CONFIG, **cfg})
        return r, r.version(now or self.now)

    def configure(self, **changes):
        cfg = {**research.DEFAULT_CONFIG, **changes}
        self.write_json(self.r.config_path, cfg)
        return self.r.version(self.now), cfg

    @staticmethod
    def signal(now, codes=("600001",), price=10):
        return {"date": now.date().isoformat(), "generated": now.strftime("%Y-%m-%d %H:%M"),
                "confirmed": [{"code": code, "name": "测试股票", "data_ok": True,
                               "price": price, "form": "tail", "score": 87} for code in codes]}

    @staticmethod
    def quote(code, now, price=10, **changes):
        result = {"code": code, "price": price, "open": price, "vol": 12000,
                  "time": now.strftime("%Y%m%d%H%M%S"),
                  "limit_up": round(price * 1.2, 2), "limit_down": round(price * 0.8, 2)}
        result.update(changes)
        return result

    def quotes(self, now, codes=("600001",), price=10):
        return {code: self.quote(code, now, price) for code in codes}

    def open_one(self, r=None, version=None, now=None, code="600001", price=10):
        r = r or self.r
        version = version or self.v
        now = now or self.now
        result = r.enter(version, self.signal(now, (code,), price), self.quotes(now, (code,), price), now)
        self.assertEqual(result["opened"], 1)
        return next(p for p in r.account(version)["positions"].values() if p["code"] == code)

    def counts(self, r=None):
        r = r or self.r
        return {table: r.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("artifacts", "versions", "events", "runs", "baselines")}

    @staticmethod
    def snapshot(root):
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*") if p.is_file()}

    @staticmethod
    def report_row(result, version):
        return next(row for row in result["data"]["versions"] if row["version"] == version)

    def populate_all_tables(self):
        self.r.bootstrap(self.original, self.now)
        self.open_one()
        self.assertTrue(all(self.counts().values()))

    def test_module_has_no_direct_network_imports(self):
        tree = ast.parse(Path(research.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {"requests", "httpx", "aiohttp", "urllib", "http",
                                     "socket", "websocket", "websockets", "ftplib"})

    def test_default_config_and_initial_account(self):
        self.assertEqual(json.loads(self.r.config_path.read_text(encoding="utf-8")), research.DEFAULT_CONFIG)
        self.assertEqual(self.r.account(self.v),
                         {"cash": float(cents(research.DEFAULT_CONFIG["initial_cash"])),
                          "positions": {}, "closed": []})
        self.assertEqual(self.r.root, self.home / "research")

    def test_bootstrap_archives_original_and_runtime_without_changing_them(self):
        original_before = self.snapshot(self.original)
        runtime_before = self.snapshot(self.home / "runtime" / research.SHORT)
        self.write_bytes(self.original / ".git" / "HEAD", b"ref: refs/heads/main\n")
        result = self.r.bootstrap(self.original, self.now)
        self.assertEqual(result["added_artifacts"], len(original_before) + len(runtime_before))
        self.assertEqual(result["legacy_trades"], len(self.legacy))
        self.assertEqual(result["version"], self.v)
        expected = set()
        for location, files in (("original", original_before), ("runtime", runtime_before)):
            for path, raw in files.items():
                expected.add((f"{location}/{research.SHORT}", path, hashlib.sha256(raw).hexdigest()))
        actual = set(self.r.db.execute("SELECT source,path,digest FROM artifacts"))
        self.assertEqual(actual, expected)
        self.assertEqual(self.snapshot(self.home / "runtime" / research.SHORT), runtime_before)
        again = self.r.bootstrap(self.original, self.now + timedelta(days=1))
        self.assertEqual(again["added_artifacts"], 0)
        self.assertEqual(self.counts()["baselines"], 2)
        self.assertEqual(again["version"], self.v)

    def test_archive_preserves_revisions_and_deduplicates_reverted_content(self):
        directory = self.history
        path = directory / "history.json"
        before = path.read_bytes()
        self.assertEqual(self.r.archive("history", directory, self.now), 1)
        changed = b'{"history": [], "revision": 2}'
        self.write_bytes(path, changed)
        self.assertEqual(self.r.archive("history", directory, self.now + timedelta(days=1)), 1)
        self.write_bytes(path, before)
        self.assertEqual(self.r.archive("history", directory, self.now + timedelta(days=2)), 0)
        rows = self.r.db.execute("SELECT path,digest,captured_at FROM artifacts ORDER BY captured_at").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([self.r.object(row[1]) for row in rows], [before, changed])
        self.assertEqual(rows[0][2], self.now.isoformat())
        self.assertEqual({row[0] for row in rows}, {"history.json"})

    def test_archive_tracks_source_and_path_even_when_content_is_identical(self):
        directory = self.history
        raw = (directory / "history.json").read_bytes()
        self.write_bytes(directory / "copy.json", raw)
        self.assertEqual(self.r.archive("one", directory, self.now), 2)
        self.assertEqual(self.r.archive("two", directory, self.now), 2)
        rows = self.r.db.execute("SELECT id,source,path,digest FROM artifacts").fetchall()
        self.assertEqual(len({row[0] for row in rows}), 4)
        self.assertEqual(len({row[3] for row in rows}), 1)
        for identity, source, path, digest in rows:
            payload = json.dumps([source, path, digest], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.assertEqual(identity, hashlib.sha256(payload).hexdigest())

    def test_archive_ignores_pycache_and_accepts_a_missing_directory(self):
        directory = self.history
        self.write_bytes(directory / "__pycache__" / "ignored.pyc", b"cache")
        self.assertEqual(self.r.archive("history", directory, self.now), 1)
        self.assertEqual(self.r.archive("missing", self.base / "not present", self.now), 0)

    def test_blob_is_content_addressed_and_reuses_identical_bytes(self):
        raw = "独立历史快照\n".encode("utf-8")
        digest = self.r.blob(raw)
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        path = self.r.root / "objects" / digest[:2] / digest
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.r.blob(raw), digest)
        self.assertEqual(self.r.object(digest), raw)
        self.assertNotEqual(self.r.blob(raw + b"revision"), digest)

    def test_object_read_and_blob_reuse_detect_tampering_without_overwriting(self):
        raw = b"immutable historical bytes"
        digest = self.r.blob(raw)
        path = self.r.root / "objects" / digest[:2] / digest
        self.write_bytes(path, b"tampered")
        with self.assertRaisesRegex(RuntimeError, "校验失败"):
            self.r.object(digest)
        with self.assertRaisesRegex(RuntimeError, "校验失败"):
            self.r.blob(raw)
        self.assertEqual(path.read_bytes(), b"tampered")

    def test_update_and_delete_triggers_reject_changes_to_every_table(self):
        self.populate_all_tables()
        for table in self.counts():
            key = "source" if table == "baselines" else "id"
            before = self.r.db.execute(f"SELECT * FROM {table}").fetchall()
            for operation in (f"UPDATE {table} SET {key}={key}", f"DELETE FROM {table}"):
                with self.subTest(table=table, operation=operation):
                    with self.r.db:
                        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                            self.r.db.execute(operation)
                    self.assertEqual(self.r.db.execute(f"SELECT * FROM {table}").fetchall(), before)

    def test_conflicting_replace_cannot_overwrite_append_only_history(self):
        # REPLACE performs an implicit delete: the append-only guarantee must cover it too.
        self.populate_all_tables()
        fields = {"artifacts": ("digest", "0" * 64), "versions": ("spec", "{}"),
                  "events": ("data", "{}"), "runs": ("data", "{}"),
                  "baselines": ("digest", "0" * 64)}
        for table, (field, replacement) in fields.items():
            with self.subTest(table=table):
                cursor = self.r.db.execute(f"SELECT * FROM {table} LIMIT 1")
                columns = [description[0] for description in cursor.description]
                row = list(cursor.fetchone())
                row[columns.index(field)] = replacement
                placeholders = ",".join("?" for _ in row)
                # An assertion failure also rolls back the attempted overwrite.
                with self.r.db:
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.r.db.execute(f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})", row)

    def test_account_and_triggers_persist_after_reopening(self):
        self.open_one()
        expected = self.r.account(self.v)
        reopened = research.Research(self.home)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.account(self.v), expected)
        self.assertEqual(reopened.version(self.now + timedelta(days=1)), self.v)
        with reopened.db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                reopened.db.execute("DELETE FROM events")

    def test_version_is_stable_across_time_and_json_formatting(self):
        spec_before = self.r.spec(self.v)
        cfg = json.loads(self.r.config_path.read_text(encoding="utf-8"))
        self.write_bytes(self.r.config_path, json.dumps(dict(reversed(list(cfg.items()))), indent=4).encode())
        strategy_path = self.r.runtime / research.SHORT / "config.json"
        strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
        self.write_bytes(strategy_path, json.dumps(dict(reversed(list(strategy.items()))), indent=2).encode())
        self.assertEqual(self.r.version(self.now + timedelta(days=1)), self.v)
        self.assertEqual(self.r.spec(self.v), spec_before)
        self.assertEqual(self.counts()["versions"], 1)
        self.assertEqual(self.r.db.execute("SELECT created_at FROM versions").fetchone()[0], self.now.isoformat())
        self.assertEqual(json.loads(self.r.object(self.v)), spec_before)

    def test_parameter_change_creates_an_independent_frozen_version(self):
        old_spec = self.r.spec(self.v)
        self.open_one()
        old_account = self.r.account(self.v)
        newer, cfg = self.configure(allocation=research.DEFAULT_CONFIG["allocation"] / 2)
        self.assertNotEqual(newer, self.v)
        self.assertEqual(self.r.spec(self.v), old_spec)
        self.assertEqual(self.r.account(self.v), old_account)
        self.assertEqual(self.r.account(newer), {"cash": float(cents(cfg["initial_cash"])), "positions": {}, "closed": []})
        self.write_json(self.r.config_path, research.DEFAULT_CONFIG)
        self.assertEqual(self.r.version(self.now + timedelta(days=1)), self.v)
        self.assertEqual(self.counts()["versions"], 2)

    def test_strategy_parameter_change_is_versioned_separately_from_script(self):
        old = self.r.spec(self.v)
        strategy = {**old["strategy_config"], "threshold": 3}
        self.write_json(self.r.runtime / research.SHORT / "config.json", strategy)
        newer = self.r.version(self.now)
        self.assertNotEqual(newer, self.v)
        self.assertEqual(self.r.spec(newer)["strategy_config"], strategy)
        self.assertEqual(self.r.spec(newer)["engine_object"], old["engine_object"])
        self.assertEqual(self.r.spec(self.v), old)

    def test_script_bytes_create_new_version_and_preserve_old_script(self):
        path = self.r.runtime / research.SHORT / "scripts" / "tail_momentum.py"
        original = path.read_bytes()
        old = self.r.spec(self.v)
        self.write_bytes(path, original + b"# revision two\n")
        newer = self.r.version(self.now)
        self.assertNotEqual(newer, self.v)
        self.assertNotEqual(self.r.spec(newer)["engine_object"], old["engine_object"])
        self.assertEqual(self.r.object(old["engine_object"]), original)
        self.assertEqual(self.r.spec(newer)["strategy_config"], old["strategy_config"])
        self.assertEqual(hashlib.sha256(self.r.object(old["research_object"])).hexdigest(), old["research_object"])
        self.write_bytes(path, original)
        self.assertEqual(self.r.version(self.now), self.v)

    def test_invalid_config_does_not_create_a_version(self):
        invalid = [("allocation", 0), ("allocation", research.DEFAULT_CONFIG["initial_cash"] + 1),
                   ("initial_cash", -1), ("commission_min", True), ("commission_min", float("nan")),
                   ("max_positions", 1.0), ("max_positions", 0), ("max_positions", 101),
                   ("slippage_pct", 10), ("commission_rate", 1), ("stamp_tax_rate", 1),
                   ("transfer_fee_rate", 1)]
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                self.write_json(self.r.config_path, {**research.DEFAULT_CONFIG, key: value})
                with self.assertRaises(ValueError):
                    self.r.version(self.now)
                self.assertEqual(self.counts()["versions"], 1)
        cfg = dict(research.DEFAULT_CONFIG)
        del cfg["allocation"]
        self.write_json(self.r.config_path, cfg)
        with self.assertRaises(ValueError):
            self.r.version(self.now)

    def test_old_positions_keep_original_fees_and_slippage_after_new_version(self):
        old_cfg = self.r.spec(self.v)["paper_config"]
        position = self.open_one()
        newer, new_cfg = self.configure(initial_cash=200000, allocation=30000,
                                        commission_rate=0.01, commission_min=100,
                                        stamp_tax_rate=0.02, transfer_fee_rate=0.001, slippage_pct=5)
        later = self.now + timedelta(days=1)
        self.assertEqual(self.r.settle(self.quotes(later, price=12), later)["closed"], 1)
        trade = self.r.account(self.v)["closed"][0]
        expected_sell = expected_execution(12, position["shares"], "sell", old_cfg)
        self.assertEqual(trade["sell"], expected_sell)
        self.assertNotEqual(trade["sell"], expected_execution(12, position["shares"], "sell", new_cfg))
        self.assertEqual(trade["version"], self.v)
        self.assertEqual(self.r.account(self.v)["cash"],
                         float(cents(old_cfg["initial_cash"]) - cents(position["buy"]["cash"]) + cents(expected_sell["cash"])))
        opened = self.open_one(version=newer, now=later, code="000001")
        self.assertEqual(opened["buy"], expected_execution(10, opened["shares"], "buy", new_cfg))
        self.assertEqual(self.r.spec(self.v)["paper_config"], old_cfg)
        report = self.r.report("all", later)
        self.assertEqual(self.report_row(report, self.v)["cumulative"]["n"], 1)
        self.assertEqual(self.report_row(report, newer)["cumulative"]["n"], 0)
        self.assertEqual(self.report_row(report, newer)["open_positions"], 1)

    def test_default_buy_uses_fresh_quote_plus_adverse_slippage_not_signal_price(self):
        signal = self.signal(self.now, price=8)
        self.r.enter(self.v, signal, self.quotes(self.now, price=10), self.now)
        position = next(iter(self.r.account(self.v)["positions"].values()))
        cfg = self.r.spec(self.v)["paper_config"]
        self.assertEqual(position["signal_price"], 8)
        self.assertEqual(position["entry_quote"], 10)
        self.assertEqual(position["buy"], expected_execution(10, position["shares"], "buy", cfg))
        self.assertGreater(position["buy"]["price"], 10)
        self.assertEqual(position["shares"] % 100, 0)
        self.assertLessEqual(position["buy"]["cash"], cfg["allocation"])
        self.assertGreater(expected_execution(10, position["shares"] + 100, "buy", cfg)["cash"], cfg["allocation"])
        self.assertEqual(position["fill_assumption"], research.RULES["fill"])

    def test_allocation_includes_minimum_commission_and_mainland_lot_size(self):
        cases = [(1000, "600001", 0), (1005, "600001", 100),
                 (2000, "600001", 100), (2005, "600001", 200),
                 (2505, "000001", 200), (2505, "300001", 200)]
        for index, (allocation, code, shares) in enumerate(cases):
            with self.subTest(allocation=allocation, code=code):
                r, version = self.book(f"allocation_{index}", {
                    "initial_cash": 10000, "allocation": allocation, "slippage_pct": 0,
                    "commission_rate": 0, "commission_min": 5,
                    "transfer_fee_rate": 0, "stamp_tax_rate": 0})
                result = r.enter(version, self.signal(self.now, (code,)), self.quotes(self.now, (code,)), self.now)
                account = r.account(version)
                self.assertEqual(result["opened"], int(shares > 0))
                self.assertEqual(result["skipped"], int(shares == 0))
                if shares:
                    position = next(iter(account["positions"].values()))
                    self.assertEqual(position["shares"], shares)
                    self.assertEqual(position["buy"]["fee"], 5)
                    self.assertEqual(position["buy"]["cash"], shares * 10 + 5)
                    self.assertEqual(account["cash"], 10000 - shares * 10 - 5)
                else:
                    self.assertEqual(account, {"cash": 10000.0, "positions": {}, "closed": []})
                    row = self.report_row(r.report("all", self.now), version)
                    self.assertEqual(row["skipped_signals"], 1)
                    self.assertEqual(row["period"]["n"], 0)
                    self.assertEqual(row["period"]["fees"], 0)

    def test_688_requires_at_least_200_shares_and_still_uses_hundred_share_steps(self):
        for index, (allocation, expected_shares) in enumerate(((1505, 0), (2000, 0), (2005, 200), (3505, 300))):
            with self.subTest(allocation=allocation):
                r, version = self.book(f"star_{index}", {
                    "initial_cash": 10000, "allocation": allocation, "slippage_pct": 0,
                    "commission_rate": 0, "commission_min": 5, "transfer_fee_rate": 0})
                result = r.enter(version, self.signal(self.now, ("688001",)), self.quotes(self.now, ("688001",)), self.now)
                self.assertEqual(result["opened"], int(expected_shares > 0))
                account = r.account(version)
                if expected_shares:
                    self.assertEqual(next(iter(account["positions"].values()))["shares"], expected_shares)
                else:
                    self.assertEqual(account["cash"], 10000)
                    self.assertFalse(account["positions"])

    def test_available_cash_is_reserved_between_rows_and_never_overdrawn(self):
        version, _ = self.configure(initial_cash=2010, allocation=2000, slippage_pct=0,
                                    commission_rate=0, commission_min=5, transfer_fee_rate=0)
        codes = ("600001", "600002", "600003")
        result = self.r.enter(version, self.signal(self.now, codes), self.quotes(self.now, codes), self.now)
        self.assertEqual((result["opened"], result["skipped"]), (2, 1))
        account = self.r.account(version)
        self.assertEqual(account["cash"], 0)
        self.assertEqual([p["shares"] for p in account["positions"].values()], [100, 100])
        self.assertFalse(account["closed"])

    def test_max_positions_and_existing_codes_skip_without_charging_cash(self):
        version, _ = self.configure(max_positions=2)
        codes = ("600001", "600002", "600003")
        first = self.r.enter(version, self.signal(self.now, codes), self.quotes(self.now, codes), self.now)
        self.assertEqual((first["opened"], first["skipped"]), (2, 1))
        before = self.r.account(version)
        later = self.now + timedelta(days=1)
        result = self.r.enter(version, self.signal(later, ("600001", "600004")), self.quotes(later, ("600001", "600004")), later)
        self.assertEqual((result["opened"], result["skipped"]), (0, 2))
        self.assertEqual(self.r.account(version), before)

    def test_invalid_confirmed_rows_are_skips_not_trades(self):
        signal = self.signal(self.now, ("600001", "600002", "900001"))
        signal["confirmed"][0]["data_ok"] = False
        signal["confirmed"][1]["price"] = 0
        quotes = self.quotes(self.now, ("600001", "600002", "900001"))
        before = self.r.account(self.v)
        result = self.r.enter(self.v, signal, quotes, self.now)
        self.assertEqual((result["opened"], result["skipped"]), (0, 3))
        self.assertEqual(self.r.account(self.v), before)
        row = self.report_row(self.r.report("all", self.now), self.v)
        self.assertEqual(row["period"]["n"], 0)
        self.assertEqual(row["skipped_signals"], 3)

    def test_missing_confirmed_signals_raise_without_creating_trades_or_runs(self):
        for value in (None, {}, "missing"):
            with self.subTest(confirmed=value):
                signal = self.signal(self.now)
                if value is None:
                    del signal["confirmed"]
                else:
                    signal["confirmed"] = value
                before = self.counts()
                with self.assertRaises(ValueError):
                    self.r.enter(self.v, signal, self.quotes(self.now), self.now)
                self.assertEqual(self.counts(), before)
        self.assertFalse(self.r.account(self.v)["positions"])

    def test_empty_confirmed_list_is_a_zero_trade_run_not_zero_return_sample(self):
        result = self.r.enter(self.v, self.signal(self.now, ()), {}, self.now)
        self.assertEqual((result["opened"], result["skipped"]), (0, 0))
        row = self.report_row(self.r.report("all", self.now), self.v)
        self.assertEqual(row["period"]["n"], 0)
        self.assertEqual(row["period"]["fees"], 0)
        self.assertIsNone(row["period"]["mean_net_pct"])
        self.assertIsNone(row["period"]["win_rate"])
        self.assertEqual(self.counts()["events"], 0)
        self.assertEqual(self.counts()["runs"], 1)

    def test_entry_rejects_weekends_wrong_timezone_and_outside_window(self):
        times = [at("2026-11-07"), at("2026-11-08"), at("2026-11-02", "14:50:59"),
                 at("2026-11-02", "14:56:00"), at("2026-11-02", "15:00:00"),
                 self.now.replace(tzinfo=timezone.utc), self.now.replace(tzinfo=None)]
        for now in times:
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    self.r.enter(self.v, self.signal(now), self.quotes(now), now)
                self.assertEqual(self.counts()["events"], 0)
                self.assertEqual(self.counts()["runs"], 0)

    def test_entry_window_includes_1451_and_1455_but_not_1456(self):
        for index, clock in enumerate(("14:51:00", "14:55:59")):
            with self.subTest(clock=clock):
                now = at("2026-11-02", clock)
                r, version = self.book(f"boundary_{index}")
                self.open_one(r, version, now)

    def test_old_future_and_malformed_signal_timestamps_are_rejected(self):
        for changes in ({"date": "2026-11-01"}, {"generated": "2026-11-02 14:46"},
                        {"generated": "2026-11-02 14:53"}, {"generated": "invalid"},
                        {"generated": ""}):
            with self.subTest(changes=changes):
                signal = {**self.signal(self.now), **changes}
                with self.assertRaises(ValueError):
                    self.r.enter(self.v, signal, self.quotes(self.now), self.now)
                self.assertEqual(self.counts()["events"], 0)
                self.assertEqual(self.counts()["runs"], 0)

    def test_signal_exactly_five_minutes_old_is_allowed(self):
        now = at("2026-11-02", "14:55:00")
        signal = {**self.signal(now), "generated": "2026-11-02 14:50"}
        self.assertEqual(self.r.enter(self.v, signal, self.quotes(now), now)["opened"], 1)

    def invalid_quote_cases(self, now, side):
        base = self.quote("600001", now)
        cases = [("missing", {}), ("wrong_code", {"600001": {**base, "code": "600002"}}),
                 ("not_a_mapping", {"600001": []})]
        changes = [("stale", {"time": (now - timedelta(seconds=121)).strftime("%Y%m%d%H%M%S")}),
                   ("previous_day", {"time": (now - timedelta(days=1)).strftime("%Y%m%d%H%M%S")}),
                   ("future", {"time": (now + timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")}),
                   ("bad_time", {"time": "bad"}), ("zero_volume", {"vol": 0}),
                   ("negative_price", {"price": -1}), ("zero_open", {"open": 0}),
                   ("string_price", {"price": "10"}), ("boolean_volume", {"vol": True}),
                   ("limit_up", {"price": base["limit_up"]}),
                   ("limit_down", {"price": base["limit_down"]}),
                   ("inverted_limits", {"limit_down": 12, "limit_up": 8}),
                   ("slipped_limit", {"limit_up": 10.01} if side == "buy" else {"limit_down": 9.99})]
        cases.extend((name, {"600001": {**base, **change}}) for name, change in changes)
        for key in ("time", "price", "open", "vol", "limit_up", "limit_down"):
            quote = dict(base)
            del quote[key]
            cases.append((f"missing_{key}", {"600001": quote}))
        return cases

    def test_invalid_entry_quotes_skip_without_changing_cash_or_holdings(self):
        for name, quotes in self.invalid_quote_cases(self.now, "buy"):
            with self.subTest(case=name):
                r, version = self.book(f"bad_buy_{name}", {"slippage_pct": 0.1})
                before = r.account(version)
                result = r.enter(version, self.signal(self.now), quotes, self.now)
                self.assertEqual((result["opened"], result["skipped"]), (0, 1))
                self.assertEqual(r.account(version), before)
                self.assertEqual([event["kind"] for event in r.events(version)], ["skip"])
                self.assertTrue(r.events(version)[0]["reason"])

    def test_invalid_exit_quotes_defer_without_changing_cash_or_holdings(self):
        later = self.now + timedelta(days=1)
        for name, quotes in self.invalid_quote_cases(later, "sell"):
            with self.subTest(case=name):
                r, version = self.book(f"bad_sell_{name}", {"slippage_pct": 0.1})
                self.open_one(r, version)
                before = r.account(version)
                result = r.settle(quotes, later)
                self.assertEqual((result["closed"], result["deferred"]), (0, 1))
                self.assertEqual(r.account(version), before)
                self.assertEqual([event["kind"] for event in r.events(version)], ["open", "defer"])
                self.assertTrue(r.events(version)[-1]["reason"])
                row = self.report_row(r.report("all", later), version)
                self.assertEqual(row["cumulative"]["n"], 0)
                self.assertEqual(row["open_positions"], 1)
                self.assertEqual(row["deferred_observations"], 1)

    def test_nonfinite_quotes_skip_or_defer_without_changing_cash_or_holdings(self):
        for side in ("buy", "sell"):
            for field in ("price", "open", "vol", "limit_up", "limit_down"):
                for index, value in enumerate((float("nan"), float("inf"), float("-inf"))):
                    with self.subTest(side=side, field=field, value=value):
                        r, version = self.book(f"nonfinite_{side}_{field}_{index}")
                        now = self.now
                        if side == "sell":
                            self.open_one(r, version)
                            now += timedelta(days=1)
                        before = r.account(version)
                        counts = self.counts(r)
                        quotes = self.quotes(now)
                        quotes["600001"][field] = value
                        if side == "buy":
                            result = r.enter(version, self.signal(now), quotes, now)
                            self.assertEqual((result["opened"], result["skipped"]), (0, 1))
                            self.assertEqual(r.events(version)[-1]["kind"], "skip")
                        else:
                            result = r.settle(quotes, now)
                            self.assertEqual((result["closed"], result["deferred"]), (0, 1))
                            self.assertEqual(r.events(version)[-1]["kind"], "defer")
                        self.assertEqual(r.account(version), before)
                        self.assertEqual(self.counts(r)["events"], counts["events"] + 1)
                        self.assertEqual(self.counts(r)["runs"], counts["runs"] + 1)
                        raw = r.object(result["input_object"])
                        recorded = json.loads(raw)
                        self.assertEqual(recorded["quotes"]["600001"][field], str(value))
                        self.assertNotIn(b"NaN", raw)
                        self.assertNotIn(b"Infinity", raw)

    def test_quote_exactly_120_seconds_old_is_accepted_on_both_sides(self):
        quote = self.quote("600001", self.now - timedelta(seconds=120))
        self.assertEqual(self.r.enter(self.v, self.signal(self.now), {"600001": quote}, self.now)["opened"], 1)
        later = self.now + timedelta(days=1)
        quote = self.quote("600001", later - timedelta(seconds=120), price=11)
        self.assertEqual(self.r.settle({"600001": quote}, later)["closed"], 1)

    def test_duplicate_codes_in_one_input_are_counted_only_once(self):
        codes = ("600001", "600001", "600002", "600002")
        signal = self.signal(self.now, codes)
        signal["confirmed"][2]["data_ok"] = False
        result = self.r.enter(self.v, signal, self.quotes(self.now, codes), self.now)
        self.assertEqual((result["opened"], result["skipped"]), (1, 1))
        self.assertEqual([event["kind"] for event in self.r.events(self.v)], ["open", "skip"])
        self.assertEqual(len(self.r.account(self.v)["positions"]), 1)

    def test_entry_replay_deduplicates_the_entire_day_even_across_version_changes(self):
        signal = self.signal(self.now)
        first = self.r.enter(self.v, signal, self.quotes(self.now), self.now)
        before = self.r.account(self.v)
        self.assertEqual(self.r.enter(self.v, signal, self.quotes(self.now), self.now), first)
        newer, _ = self.configure(allocation=research.DEFAULT_CONFIG["allocation"] / 2)
        changed_time = self.now.replace(hour=15)
        # Current contract keys entry runs by day, not version, and returns before revalidation.
        replay = self.r.enter(newer, {}, {}, changed_time)
        self.assertEqual(replay, first)
        self.assertEqual(self.r.account(self.v), before)
        self.assertFalse(self.r.account(newer)["positions"])
        self.assertEqual(self.counts()["runs"], 1)
        reopened = research.Research(self.home)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.enter(newer, {}, {}, changed_time), first)

    def test_next_day_entry_is_not_deduplicated_with_previous_day(self):
        self.open_one()
        later = self.now + timedelta(days=1)
        self.open_one(now=later, code="600002")
        self.assertEqual(len(self.r.account(self.v)["positions"]), 2)
        self.assertEqual(self.counts()["runs"], 2)

    def test_same_day_settlement_never_exits_or_defers_new_positions(self):
        self.open_one()
        before = self.r.account(self.v)
        result = self.r.settle({}, self.now + timedelta(minutes=1))
        self.assertEqual((result["closed"], result["deferred"]), (0, 0))
        self.assertEqual(self.r.account(self.v), before)
        later = self.now + timedelta(days=1)
        self.assertEqual(self.r.settle(self.quotes(later), later)["closed"], 1)

    def test_settlement_rejects_weekend_and_out_of_window_without_mutation(self):
        self.open_one()
        before = self.r.account(self.v)
        counts = self.counts()
        for now in (at("2026-11-07"), at("2026-11-08"), at("2026-11-03", "14:50:59"),
                    at("2026-11-03", "14:56:00"), at("2026-11-03").replace(tzinfo=timezone.utc)):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    self.r.settle(self.quotes(now, price=12), now)
                self.assertEqual(self.r.account(self.v), before)
                self.assertEqual(self.counts(), counts)

    def test_settlement_uses_first_snapshot_and_is_idempotent_after_closing(self):
        position = self.open_one()
        later = self.now + timedelta(days=1)
        first = self.r.settle(self.quotes(later, price=11), later)
        before = self.r.account(self.v)
        replay = self.r.settle(self.quotes(later, price=15), later + timedelta(minutes=1))
        self.assertEqual(replay, first)
        self.assertEqual(self.r.account(self.v), before)
        self.assertEqual(before["closed"][0]["exit_quote"], 11)
        self.assertEqual(before["closed"][0]["sell"],
                         expected_execution(11, position["shares"], "sell", self.r.spec(self.v)["paper_config"]))
        self.assertEqual(self.r.settle(self.quotes(later + timedelta(days=1)), later + timedelta(days=1))["closed"], 0)

    def test_deferred_run_also_consumes_day_and_later_weekday_can_close(self):
        self.open_one()
        before = self.r.account(self.v)
        later = self.now + timedelta(days=1)
        first = self.r.settle({}, later)
        self.assertEqual((first["closed"], first["deferred"]), (0, 1))
        self.assertEqual(self.r.settle(self.quotes(later), later + timedelta(minutes=1)), first)
        self.assertEqual(self.r.account(self.v), before)
        next_day = later + timedelta(days=1)
        self.assertEqual(self.r.settle(self.quotes(next_day), next_day)["closed"], 1)
        self.assertEqual(self.r.account(self.v)["closed"][0]["unobserved_weekdays"], 1)

    def test_closed_pnl_includes_buy_and_sell_fees_with_buy_cash_denominator(self):
        position = self.open_one()
        later = self.now + timedelta(days=1)
        self.r.settle(self.quotes(later, price=12), later)
        account = self.r.account(self.v)
        trade = account["closed"][0]
        cfg = self.r.spec(self.v)["paper_config"]
        buy = expected_execution(10, position["shares"], "buy", cfg)
        sell = expected_execution(12, position["shares"], "sell", cfg)
        pnl = cents(sell["amount"]) - cents(buy["amount"]) - cents(buy["fee"]) - cents(sell["fee"])
        self.assertEqual(trade["buy"], buy)
        self.assertEqual(trade["sell"], sell)
        self.assertEqual(trade["pnl"], float(pnl))
        self.assertAlmostEqual(trade["net_pct"], float(pnl / cents(buy["cash"]) * 100), places=10)
        self.assertNotAlmostEqual(trade["net_pct"], float(pnl / cents(buy["amount"]) * 100), places=6)
        self.assertAlmostEqual(trade["gross_pct"], 20)
        self.assertEqual(account["cash"], float(cents(cfg["initial_cash"]) + pnl))
        self.assertFalse(account["positions"])
        stat = self.report_row(self.r.report("all", later), self.v)["period"]
        self.assertEqual(stat["fees"], float(cents(buy["fee"]) + cents(sell["fee"])))
        self.assertEqual(stat["realized_pnl"], float(pnl))

    def test_flat_market_price_is_a_net_loss_after_fees_and_slippage(self):
        self.open_one()
        later = self.now + timedelta(days=1)
        self.r.settle(self.quotes(later), later)
        trade = self.r.account(self.v)["closed"][0]
        self.assertEqual(trade["gross_pct"], 0)
        self.assertLess(trade["pnl"], 0)
        self.assertLess(trade["net_pct"], 0)
        stat = self.report_row(self.r.report("all", later), self.v)["period"]
        self.assertEqual(stat["win_rate"], 0)
        self.assertEqual(stat["return_profit_factor"], 0)
        self.assertEqual(stat["amount_profit_factor"], 0)

    def test_friday_to_monday_has_zero_unobserved_weekdays(self):
        friday, monday = at("2026-11-06"), at("2026-11-09")
        self.open_one(now=friday)
        self.r.settle(self.quotes(monday, price=11), monday)
        self.assertEqual(self.r.account(self.v)["closed"][0]["unobserved_weekdays"], 0)
        self.assertEqual(self.report_row(self.r.report("all", monday), self.v)["gap_trades"], 0)

    def test_missing_weekday_is_flagged_without_assuming_a_holiday(self):
        friday, tuesday = at("2026-11-06"), at("2026-11-10")
        self.open_one(now=friday)
        self.r.settle(self.quotes(tuesday, price=11), tuesday)
        trade = self.r.account(self.v)["closed"][0]
        self.assertEqual(trade["unobserved_weekdays"], 1)
        self.assertEqual(trade["exit_date"], "2026-11-10")
        report = self.r.report("all", tuesday)
        self.assertEqual(self.report_row(report, self.v)["gap_trades"], 1)
        self.assertIn("节假日", report["data"]["gap_note"])

    def test_legacy_import_is_frozen_never_relabelled_or_mixed_with_new_trades(self):
        original_before = self.snapshot(self.original)
        self.r.bootstrap(self.original, self.now)
        self.assertEqual(self.r.legacy_trades(), self.legacy)
        self.assertEqual(self.snapshot(self.original), original_before)
        baseline_before = self.r.db.execute("SELECT * FROM baselines ORDER BY source").fetchall()
        self.assertEqual(self.r.events(), [])
        changed = [{"code": "600080", "pct": 987, "version": self.v}]
        self.write_json(self.original / "positions.json", {"paper": {"trades": changed}})
        self.write_json(self.r.runtime / research.SHORT / "positions.json", {"paper": {"trades": changed}})
        newer, _ = self.configure(allocation=research.DEFAULT_CONFIG["allocation"] / 2)
        result = self.r.bootstrap(self.original, self.now + timedelta(days=1))
        self.assertGreaterEqual(result["added_artifacts"], 2)
        self.assertEqual(result["legacy_trades"], 2)
        self.assertEqual(self.r.legacy_trades(), self.legacy)
        self.assertEqual(self.r.db.execute("SELECT * FROM baselines ORDER BY source").fetchall(), baseline_before)
        self.open_one(version=newer)
        later = self.now + timedelta(days=1)
        self.r.settle(self.quotes(later, price=11), later)
        report = self.r.report("all", later)
        self.assertEqual(report["data"]["legacy"]["n"], 2)
        self.assertEqual(report["data"]["legacy"]["mean_stored_net_pct"], 0.75)
        self.assertEqual(self.report_row(report, self.v)["cumulative"]["n"], 0)
        self.assertEqual(self.report_row(report, newer)["cumulative"]["n"], 1)
        self.assertEqual(self.r.legacy_trades()[0]["shares"], 0)
        self.assertEqual(self.r.legacy_trades()[0]["version"], "legacy-unknown")
        self.assertEqual(json.loads((self.original / "positions.json").read_text(encoding="utf-8"))["paper"]["trades"], changed)

    def test_legacy_baseline_object_tampering_is_detected(self):
        self.r.bootstrap(self.original, self.now)
        digest = self.r.db.execute("SELECT digest FROM baselines WHERE source='original'").fetchone()[0]
        self.write_bytes(self.r.root / "objects" / digest[:2] / digest, b'{"paper":{"trades":[]}}')
        with self.assertRaisesRegex(RuntimeError, "校验失败"):
            self.r.legacy_trades()
        with self.assertRaisesRegex(RuntimeError, "校验失败"):
            self.r.report("all", self.now)

    def build_cohort(self, name, friday):
        r, version = self.book(name, now=friday)
        monday, tuesday = friday + timedelta(days=3), friday + timedelta(days=4)
        for day, codes, expensive in ((friday, ("600001", "600002", "600005"), "600005"),
                                      (monday, ("600003", "600004", "600006"), "600006")):
            if day == monday:
                r.settle(self.quotes(day, ("600001",), price=11), day)
            quotes = self.quotes(day, codes)
            quotes[expensive] = self.quote(expensive, day, price=10000)
            result = r.enter(version, self.signal(day, codes), quotes, day)
            self.assertEqual((result["opened"], result["skipped"]), (2, 1))
        r.settle(self.quotes(tuesday, ("600003",), price=11), tuesday)
        return r, version, tuesday

    def test_weekly_report_uses_entry_date_cohorts_and_counts_open_positions_separately(self):
        r, version, now = self.build_cohort("weekly", at("2026-11-06"))
        report = r.report("week", now)
        row = self.report_row(report, version)
        self.assertEqual(report["data"]["entry_date_start"], "2026-11-09")
        self.assertEqual(report["data"]["entry_date_end"], "2026-11-10")
        self.assertEqual(row["period"]["n"], 1)
        self.assertEqual(row["period"]["entry_days"], 1)
        self.assertEqual(row["cumulative"]["n"], 2)
        self.assertEqual(row["cumulative"]["entry_days"], 2)
        self.assertEqual(row["open_positions"], 2)
        self.assertEqual(row["period_open_positions"], 1)
        self.assertEqual(row["skipped_signals"], 1)
        self.assertEqual(row["deferred_observations"], 3)
        current = next(t for t in r.account(version)["closed"] if t["code"] == "600003")
        self.assertEqual(row["period"]["mean_net_pct"], current["net_pct"])
        self.assertEqual(row["period"]["realized_pnl"], current["pnl"])

    def test_monthly_report_means_previous_calendar_month_and_includes_later_closures(self):
        r, version, now = self.build_cohort("monthly", at("2026-10-30"))
        report = r.report("month", now)
        row = self.report_row(report, version)
        self.assertEqual(report["data"]["entry_date_start"], "2026-10-01")
        self.assertEqual(report["data"]["entry_date_end"], "2026-10-31")
        self.assertEqual(row["period"]["n"], 1)
        self.assertEqual(row["period"]["entry_days"], 1)
        self.assertEqual(row["cumulative"]["n"], 2)
        self.assertEqual(row["open_positions"], 2)
        self.assertEqual(row["period_open_positions"], 1)
        self.assertEqual(row["skipped_signals"], 1)
        self.assertEqual(row["deferred_observations"], 0)
        october = next(t for t in r.account(version)["closed"] if t["code"] == "600001")
        self.assertEqual(october["exit_date"], "2026-11-02")
        self.assertEqual(row["period"]["mean_net_pct"], october["net_pct"])

    def test_no_loss_profit_factors_are_json_null_not_infinity(self):
        empty = self.r.report("all", self.now)
        self.open_one()
        later = self.now + timedelta(days=1)
        self.r.settle(self.quotes(later, price=11), later)
        profitable = self.r.report("all", later)
        for result, expected_n in ((empty, 0), (profitable, 1)):
            with self.subTest(n=expected_n):
                stat = self.report_row(result, self.v)["period"]
                self.assertEqual(stat["n"], expected_n)
                self.assertIsNone(stat["return_profit_factor"])
                self.assertIsNone(stat["amount_profit_factor"])
                raw = Path(result["json"]).read_text(encoding="utf-8")
                self.assertNotIn("Infinity", raw)
                self.assertNotIn("NaN", raw)
                self.assertIsNone(json.loads(raw)["versions"][0]["period"]["amount_profit_factor"])

    def test_mixed_winners_and_losers_have_distinct_return_and_amount_profit_factors(self):
        codes = ("600001", "600002")
        self.r.enter(self.v, self.signal(self.now, codes),
                     {"600001": self.quote("600001", self.now, price=10),
                      "600002": self.quote("600002", self.now, price=30)}, self.now)
        later = self.now + timedelta(days=1)
        self.r.settle({"600001": self.quote("600001", later, price=11),
                       "600002": self.quote("600002", later, price=28)}, later)
        closed = self.r.account(self.v)["closed"]
        self.assertEqual(len(closed), 2)
        win = next(t for t in closed if t["pnl"] > 0)
        loss = next(t for t in closed if t["pnl"] < 0)
        stat = self.report_row(self.r.report("all", later), self.v)["period"]
        self.assertEqual(stat["n"], 2)
        self.assertEqual(stat["entry_days"], 1)
        self.assertEqual(stat["win_rate"], 50)
        self.assertAlmostEqual(stat["mean_net_pct"], (win["net_pct"] + loss["net_pct"]) / 2)
        self.assertAlmostEqual(stat["amount_profit_factor"], win["pnl"] / -loss["pnl"])
        self.assertAlmostEqual(stat["return_profit_factor"], win["net_pct"] / -loss["net_pct"])
        self.assertNotAlmostEqual(stat["amount_profit_factor"], stat["return_profit_factor"], places=4)

    def test_input_snapshots_preserve_tencent_quotes_and_observation_times(self):
        signal = self.signal(self.now)
        quotes = self.quotes(self.now)
        result = self.r.enter(self.v, signal, quotes, self.now)
        recorded = json.loads(self.r.object(result["input_object"]))
        self.assertEqual(recorded, {"signal": signal, "quotes": quotes, "observed_at": self.now.isoformat()})
        signal["confirmed"][0]["price"] = 999
        quotes["600001"]["price"] = 999
        self.assertEqual(json.loads(self.r.object(result["input_object"]))["quotes"]["600001"]["price"], 10)
        later = self.now + timedelta(days=1)
        closing_quotes = self.quotes(later, price=11)
        close = self.r.settle(closing_quotes, later)
        trade = self.r.account(self.v)["closed"][0]
        self.assertEqual(trade["input_object"], result["input_object"])
        self.assertEqual(trade["exit_input_object"], close["input_object"])
        self.assertEqual(json.loads(self.r.object(close["input_object"])),
                         {"quotes": closing_quotes, "observed_at": later.isoformat()})

    def test_reports_are_content_archived_idempotent_and_keep_older_snapshots(self):
        first = self.r.report("all", self.now)
        original_json = Path(first["json"]).read_bytes()
        original_text = Path(first["text"]).read_bytes()
        self.assertEqual(self.r.object(hashlib.sha256(original_json).hexdigest()), original_json)
        self.assertEqual(json.loads(original_json), first["data"])
        self.assertEqual(self.r.report("all", self.now), first)
        self.open_one()
        updated = self.r.report("all", self.now)
        self.assertNotEqual(updated["json"], first["json"])
        self.assertEqual(Path(first["json"]).read_bytes(), original_json)
        self.assertEqual(Path(first["text"]).read_bytes(), original_text)
        self.assertTrue(Path(updated["json"]).resolve().is_relative_to(self.base))

    def test_report_refuses_to_overwrite_tampered_existing_report(self):
        result = self.r.report("all", self.now)
        self.write_bytes(Path(result["json"]), b"tampered report")
        with self.assertRaisesRegex(RuntimeError, "拒绝覆盖"):
            self.r.report("all", self.now)
        self.assertEqual(Path(result["json"]).read_bytes(), b"tampered report")

    @staticmethod
    def fail_after_second_event(r):
        real_append = r.append
        calls = []

        def append_then_fail(*args, **kwargs):
            real_append(*args, **kwargs)
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("injected event failure")

        return append_then_fail

    def test_entry_transaction_rolls_back_all_events_and_run_if_second_event_fails(self):
        codes = ("600001", "600002")
        before, counts = self.r.account(self.v), self.counts()
        with patch.object(self.r, "append", side_effect=self.fail_after_second_event(self.r)):
            with self.assertRaisesRegex(RuntimeError, "injected event failure"):
                self.r.enter(self.v, self.signal(self.now, codes), self.quotes(self.now, codes), self.now)
        self.assertEqual(self.r.account(self.v), before)
        self.assertEqual(self.counts(), counts)
        retry = self.r.enter(self.v, self.signal(self.now, codes), self.quotes(self.now, codes), self.now)
        self.assertEqual(retry["opened"], 2)
        self.assertEqual(self.counts()["runs"], 1)

    def test_settlement_transaction_rolls_back_cash_positions_and_run_if_event_fails(self):
        codes = ("600001", "600002")
        self.r.enter(self.v, self.signal(self.now, codes), self.quotes(self.now, codes), self.now)
        before, counts = self.r.account(self.v), self.counts()
        later = self.now + timedelta(days=1)
        with patch.object(self.r, "append", side_effect=self.fail_after_second_event(self.r)):
            with self.assertRaisesRegex(RuntimeError, "injected event failure"):
                self.r.settle(self.quotes(later, codes, price=11), later)
        self.assertEqual(self.r.account(self.v), before)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.r.settle(self.quotes(later, codes, price=11), later)["closed"], 2)

    def test_settlement_failure_rolls_back_other_versions_in_same_transaction(self):
        self.open_one()
        newer, _ = self.configure(allocation=research.DEFAULT_CONFIG["allocation"] / 2)
        self.open_one(version=newer, now=self.now + timedelta(days=1), code="600002")
        before = {version: self.r.account(version) for version in (self.v, newer)}
        counts = self.counts()
        later = self.now + timedelta(days=2)
        quotes = self.quotes(later, ("600001", "600002"), price=11)
        with patch.object(self.r, "append", side_effect=self.fail_after_second_event(self.r)):
            with self.assertRaisesRegex(RuntimeError, "injected event failure"):
                self.r.settle(quotes, later)
        self.assertEqual({version: self.r.account(version) for version in before}, before)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.r.settle(quotes, later)["closed"], 2)

    def test_finish_run_failure_also_rolls_back_entry_or_settlement_events(self):
        for side in ("entry", "settle"):
            with self.subTest(side=side):
                r, version = self.book(f"finish_failure_{side}")
                now = self.now
                if side == "settle":
                    self.open_one(r, version)
                    now += timedelta(days=1)
                before, counts = r.account(version), self.counts(r)
                with patch.object(r, "finish_run", side_effect=RuntimeError("injected run failure")):
                    with self.assertRaisesRegex(RuntimeError, "injected run failure"):
                        if side == "entry":
                            r.enter(version, self.signal(now), self.quotes(now), now)
                        else:
                            r.settle(self.quotes(now), now)
                self.assertEqual(r.account(version), before)
                self.assertEqual(self.counts(r), counts)
                if side == "entry":
                    self.assertEqual(r.enter(version, self.signal(now), self.quotes(now), now)["opened"], 1)
                else:
                    self.assertEqual(r.settle(self.quotes(now), now)["closed"], 1)


if __name__ == "__main__":
    unittest.main()
