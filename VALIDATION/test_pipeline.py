import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from assess import assess, auditable_trade, trade_stats
from evidence import Recorder, selection_chain, start_campaign

ROOT = Path(__file__).resolve().parent.parent
CST = timezone(timedelta(hours=8))


def engine_at(directory):
    with patch.dict("os.environ", {"SHORT_TERM_BASE": str(directory)}):
        spec = importlib.util.spec_from_file_location("fixture_engine", ROOT / "scripts" / "tail_momentum.py")
        engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(engine)
    return engine


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = engine_at(self.root / "runtime")
        self.recorder = Recorder(self.root / "evidence")
        self.engine.audit_event = self.recorder.event
        self.clock = datetime(2026, 11, 18, 14, 31)

    def test_observer_exception_does_not_retry_http_or_change_filtering(self):
        with patch.object(self.engine, "audit_event", side_effect=OSError("fixture")), patch.object(self.engine.urllib.request, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = b"ok"
            self.assertEqual(self.engine.http_get("http://push2.eastmoney.com/api/test"), "ok")
            request.assert_called_once()
            cfg = self.engine.DEFAULT_CFG
            row = {"code": "600001", "name": "测试", "price": 10, "pct": 4,
                   "amount": 300000000, "turnover": 3, "vol_ratio": 1, "list_date": ""}
            self.assertEqual(self.engine.stage1_filter([row], cfg, self.clock)[0], [row])
        self.assertEqual(self.engine.AUDIT_ERRORS, ["OSError"])

    def test_http_preserves_single_response_and_retry_count(self):
        with patch.object(self.engine.urllib.request, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = b'{"data": 1}'
            self.assertEqual(self.engine.http_get("http://push2.eastmoney.com/api/test"), '{"data": 1}')
            self.assertEqual(request.call_count, 1)
            request.return_value.__enter__.return_value.read.assert_called_once_with()
        self.assertEqual(self.recorder.health()["status"], "ok")
        with patch.object(self.engine.urllib.request, "urlopen", side_effect=TimeoutError), patch.object(self.engine.time, "sleep"):
            with self.assertRaises(RuntimeError):
                self.engine.http_get("http://push2.eastmoney.com/api/test", retries=2)
        events = [e for e in self.recorder.summary() if e["kind"] == "http"]
        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1]["data"]["error_type"], "TimeoutError")

    def test_stage1_reasons_and_no_observer_preserve_selection(self):
        row = {"code": "600001", "name": "测试", "price": 10, "pct": 4,
               "amount": 300000000, "turnover": 3, "vol_ratio": 1, "list_date": ""}
        changes = [{"price": None}, {"name": "ST测试"}, {"code": "830001"},
                   {"list_date": "20261117"}, {"pct": 2}, {"amount": 1},
                   {"turnover": 1}, {"vol_ratio": 0.1}, {}]
        rows = [{**row, **change} for change in changes]
        cfg = self.engine.DEFAULT_CFG
        result = self.engine.stage1_filter(rows, cfg, self.clock)
        with patch.object(self.engine, "audit_event"):
            self.assertEqual(result, self.engine.stage1_filter(rows, cfg, self.clock))
        events = self.recorder.summary()
        self.assertEqual(len(events), len(rows))
        self.assertEqual([e["data"]["reason"] for e in events],
                         ["停牌无价", "ST/风险警示", "板块排除", "次新", "涨幅区间", "成交额", "换手率", "量比", None])

    def test_confirmation_records_missing_data_and_cap_without_new_quotes(self):
        e = self.engine
        cfg = {**e.DEFAULT_CFG, "shadow_track": False}
        self.clock = self.clock.replace(minute=51)
        candidate = {"code": "600001", "name": "测试", "price": 10, "prev_close": 9.6,
                     "total": 80, "tradable": True, "plan": {"stop": 9}, "m10": {}}
        data = {"candidates": [candidate], "date": "2026-11-18"}
        args = SimpleNamespace(no_push=True, no_hold=True)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(e, "read_json", return_value=data))
            stack.enter_context(patch.object(e, "load_positions", return_value={"positions": []}))
            quotes = stack.enter_context(patch.object(e, "tencent_quotes", return_value={}))
            stack.enter_context(patch.object(e, "em_market_env", return_value={}))
            bars = stack.enter_context(patch.object(e, "fetch_5m", return_value=([], "缺失", "missing", ["timeout"], [])))
            stack.enter_context(patch.object(e, "prune_closed"))
            stack.enter_context(patch.object(e, "write_position_list"))
            stack.enter_context(patch.object(e, "build_position_list", return_value=[]))
            stack.enter_context(patch.object(e, "record_run"))
            stack.enter_context(patch.object(e, "log"))
            Path(e.REPORT_DIR).mkdir(parents=True)
            self.assertEqual(e.mode_pm(self.clock, cfg, args), 0)
        quotes.assert_called_once()
        bars.assert_called_once()
        final = next(event["data"] for event in self.recorder.summary() if event["kind"] == "pm_final")
        self.assertEqual(final["confirmed"], [])
        self.assertEqual(final["nodata"][0]["code"], "600001")
        self.assertFalse(final["nodata"][0]["data_ok"])


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "research").mkdir()
        (self.home / "validation").mkdir()
        self.at = datetime(2026, 11, 18, 14, 0, tzinfo=CST)
        self.campaign = start_campaign(self.home / "validation", "frozen", {}, self.at, {"test": Path(__file__)})
        self.database = self.home / "research" / "research.sqlite3"
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("CREATE TABLE versions(id TEXT,spec TEXT); CREATE TABLE events(version TEXT,kind TEXT,at TEXT,data TEXT);")
            db.execute("INSERT INTO versions VALUES (?,?)", ("frozen", "{}"))

    def add_trade(self, day="2026-11-18", version="frozen", pnl=-10):
        trade = {"position_id": version + day, "entry_date": day, "pnl": pnl, "net_pct": pnl / 100,
                 "buy": {"fee": 5}, "sell": {"fee": 6}}
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            for kind in ("open", "close"):
                db.execute("INSERT INTO events VALUES (?,?,?,?)", (version, kind, day + "T15:00:00+08:00", json.dumps(trade)))

    def test_report_is_read_only_and_missing_trace_not_silently_dropped(self):
        self.add_trade()
        self.add_trade(version="legacy", pnl=999)
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        result = assess(self.home, self.at.replace(hour=16))
        self.assertEqual(result["all_forward_closed"]["pnl"], -10)
        self.assertEqual(result["closed_missing_audit_trace"], 1)
        self.assertEqual(result["auditable_closed"]["closed"], 0)
        self.assertEqual(result["complete_days"], [])
        self.assertIn("未验证", result["review_status"])
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_paired_complete_days_and_corrupt_streams(self):
        candidates = {"date": "2026-11-18", "candidates": []}
        signal = {"date": "2026-11-18", "confirmed": [], "rejected": []}
        for job in ("scan", "pm"):
            directory = self.home / "validation" / "runs" / "2026-11-18" / job
            recorder = Recorder(directory)
            recorder.event("worker_start", {})
            recorder.event("config", {"effective": {}})
            recorder.event("http", {"raw": b"fixture"})
            recorder.event(job + "_final", signal if job == "pm" else {})
            if job == "scan":
                recorder.event("candidates_file", {"raw": json.dumps(candidates).encode()})
            else:
                recorder.event("pm_input", {"candidates": candidates})
                recorder.event("signals_file", {"raw": json.dumps(signal).encode()})
            recorder.event("worker_end", {"returncode": 0, "errors": []})
            recorder.event("run_end", {"job": job, "status": "done", "version": "frozen"})
            recorder.seal()
        now = datetime.now(CST) + timedelta(seconds=1)
        result = assess(self.home, now)
        self.assertEqual(result["complete_days"], ["2026-11-18"])
        paths = list((self.home / "validation" / "runs" / "2026-11-18" / "pm").glob("stream-*"))
        with paths[0].open("ab") as output:
            output.write(b"{}")
        result = assess(self.home, now)
        self.assertEqual(result["complete_days"], [])
        self.assertTrue(any(row["status"] == "incomplete" for row in result["run_coverage"]))

    def test_selection_chain_rejects_same_day_replaced_candidates(self):
        scan = Recorder(self.home / "validation" / "scan")
        pm = Recorder(self.home / "validation" / "pm")
        original = {"date": "2026-11-18", "candidates": [{"code": "600001"}]}
        replaced = {"date": "2026-11-18", "candidates": [{"code": "600002"}]}
        signal = {"date": "2026-11-18", "confirmed": [], "rejected": []}
        scan.event("candidates_file", {"raw": json.dumps(original).encode()})
        scan.seal()
        pm.event("pm_input", {"candidates": replaced})
        pm.event("pm_final", signal)
        pm.event("signals_file", {"raw": json.dumps(signal).encode()})
        with self.assertRaisesRegex(ValueError, "mismatch"):
            selection_chain(scan.directory, pm.directory)

    def test_audit_requires_every_entry_and_sale_input_hash(self):
        opened = {"code": "600001", "at": "2026-11-18T14:51:00+08:00", "entry_quote": 10, "shares": 100}
        sale = {"code": "600001", "at": "2026-11-19T10:00:00+08:00", "exit_quote": 11, "sell_shares": 100}
        links = set()
        for event, field, price in ((opened, "input_object", 10), (sale, "exit_input_object", 11)):
            data = {"observed_at": event["at"], "quotes": {"600001": {"price": price, "time": event["at"].replace("-", "").replace(":", "").replace("T", "")[:14]}}}
            raw = json.dumps(data).encode()
            digest = hashlib.sha256(raw).hexdigest()
            path = self.home / "research" / "objects" / digest[:2] / digest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            event[field] = digest
            links.add(digest)
        self.assertTrue(auditable_trade(self.home, sale, opened, [sale], links))
        links.remove(sale["exit_input_object"])
        self.assertFalse(auditable_trade(self.home, sale, opened, [sale], links))
        links.add(sale["exit_input_object"])
        path.unlink()
        self.assertFalse(auditable_trade(self.home, sale, opened, [sale], links))

    def test_tampered_manifest_rejected_before_reporting(self):
        path = self.home / "validation" / "campaign.json"
        data = json.loads(path.read_text())
        data["started_at"] = "2026-01-01T00:00:00+08:00"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "哈希"):
            assess(self.home, self.at.replace(hour=16))

    def test_reader_does_not_create_streams_and_replays_persisted_errors(self):
        recorder = Recorder(self.home / "validation" / "fixture")
        recorder.event("run_end", {"evidence_errors": ["PermissionError"]})
        before = {p.name: p.read_bytes() for p in recorder.directory.glob("stream-*")}
        reader = Recorder.reader(recorder.directory)
        self.assertEqual(reader.health()["status"], "incomplete")
        self.assertEqual(before, {p.name: p.read_bytes() for p in recorder.directory.glob("stream-*")})

    def test_trade_stats_undefined_and_best_trade_dependency(self):
        self.assertIsNone(trade_stats([])["win_rate_pct"])
        self.assertIsNone(trade_stats([])["amount_profit_factor"])
        trades = [{"pnl": pnl, "net_pct": pnl / 100, "buy": {"fee": 5}, "sell": {"fee": 6}} for pnl in (100, -60, -50)]
        stats = trade_stats(trades)
        self.assertEqual(stats["pnl"], -10)
        self.assertEqual(stats["pnl_without_best_trade"], -110)
        self.assertEqual(stats["fees"], 33)


if __name__ == "__main__":
    unittest.main()
