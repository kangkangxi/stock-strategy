import contextlib
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

from bot import Bot, CST, JOBS, SHORT, STATUS, due, dump, fresh, lock, read


class ScheduleTests(unittest.TestCase):
    def test_scan_window(self):
        at = datetime(2026, 11, 16, 14, 31, tzinfo=CST)
        self.assertIn("scan", [j.name for j in due(at, {})])
        self.assertIn("scan", [j.name for j in due(at + timedelta(seconds=119), {})])
        self.assertNotIn("scan", [j.name for j in due(at + timedelta(seconds=120), {})])
        self.assertNotIn("scan", [j.name for j in due(at, {"2026-11-16/scan": {"status": "done"}})])

    def test_weekends(self):
        self.assertEqual(due(datetime(2026, 11, 14, 14, 31, tzinfo=CST), {}), [])

    def test_week_and_month(self):
        monday = datetime(2026, 11, 2, 15, 5, tzinfo=CST)
        self.assertEqual([j.name for j in due(monday, {})], ["month"])
        self.assertNotIn("month", [j.name for j in due(monday + timedelta(days=1), {})])
        self.assertIn("week", [j.name for j in due(datetime(2026, 11, 13, 15, 5, tzinfo=CST), {})])

    def test_market_freshness(self):
        at = datetime(2026, 11, 16, 14, 31, tzinfo=CST)
        env = {"ts": at.timestamp(), "indices": [{"code": "000001", "price": 3600}]}
        self.assertTrue(fresh(env, at))
        self.assertFalse(fresh({}, at))
        for price in (None, 0, float("nan"), float("inf")):
            self.assertFalse(fresh({**env, "indices": [{"code": "000001", "price": price}]}, at))
        for ts in ((at - timedelta(days=1)).timestamp(), (at - timedelta(seconds=301)).timestamp(),
                   (at + timedelta(seconds=31)).timestamp(), float("inf")):
            self.assertFalse(fresh({**env, "ts": ts}, at))
        after_close = at.replace(hour=15, minute=10)
        close_quote = {**env, "ts": after_close.replace(minute=0).timestamp()}
        self.assertFalse(fresh(close_quote, after_close))
        self.assertTrue(fresh(close_quote, after_close, max_age=900))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "source"
        self.home = root / "robot"
        scripts = self.source / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "tail_momentum.py").write_text("import sys\nprint('local test', sys.argv)\n", encoding="utf-8")
        dump(self.source / "config.json", {"push": True, "paper_track": True})
        dump(self.source / "positions.json", {"positions": [{"code": "123456"}], "paper": {"trades": []}})
        self.bot = Bot(self.home, self.source)
        with contextlib.redirect_stdout(io.StringIO()):
            self.bot.initialize()
        self.now = datetime(2026, 11, 16, 14, 31, tzinfo=CST)
        self.state = {"jobs": {}}

    def test_isolated_initialization(self):
        cfg = read(self.bot.runtime / SHORT / "config.json", {})
        self.assertFalse(cfg["push"])
        self.assertFalse(cfg["paper_track"])
        self.assertEqual(read(self.bot.runtime / SHORT / "positions.json", {})["positions"], [])
        self.assertEqual(read(self.source / "positions.json", {})["positions"], [{"code": "123456"}])
        self.assertTrue(read(self.source / "config.json", {})["push"])
        with self.assertRaises(RuntimeError):
            self.bot.initialize()

    def test_initialization_from_fresh_clone_layout(self):
        repo = Path(self.temp.name) / "clone"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts" / "tail_momentum.py").write_text("print('engine')\n", encoding="utf-8")
        dump(repo / "config.example.json", {"push": True, "paper_track": True, "example": True})
        (repo / ".git").mkdir()
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        home = repo / "GO_BOT"
        home.mkdir()
        (home / "bot.py").write_bytes((Path(__file__).with_name("bot.py")).read_bytes())
        bot = Bot(home, repo)
        with contextlib.redirect_stdout(io.StringIO()):
            bot.initialize()
        copied = bot.runtime / SHORT
        cfg = read(copied / "config.json", {})
        self.assertEqual((cfg["push"], cfg["paper_track"], cfg["example"]), (False, False, True))
        self.assertFalse((copied / "positions.json").exists())
        self.assertFalse((copied / "GO_BOT").exists())
        self.assertFalse((copied / ".git").exists())
        self.assertFalse((repo / "config.json").exists())
        with bot.research() as store:
            paths = {row[0] for row in store.db.execute("SELECT source || '/' || path FROM artifacts")}
            self.assertEqual(store.legacy_trades(), [])
        self.assertIn("original/短线策略/scripts/tail_momentum.py", paths)
        self.assertIn("runtime/短线策略/config.json", paths)
        self.assertFalse(any("GO_BOT" in path or ".git" in path for path in paths))

    def test_safe_command(self):
        for job in JOBS:
            command = self.bot.command(job, self.now)
            if job.mode in ("paper_settle", "week", "month"):
                self.assertEqual(command[3], str(self.home / "bot.py"))
                self.assertEqual(command[-2:], ["--run", job.name])
                continue
            self.assertIn(str(self.bot.runtime), command[3])
            if job.system == SHORT:
                self.assertIn("--no-push", command)
                self.assertIn("--no-hold", command)
                self.assertIn("--force-now", command)
                self.assertNotIn("--hold", command)
                self.assertNotIn("--close", command)

    def test_lock(self):
        path = self.home / "test.lock"
        with lock(path) as acquired:
            self.assertTrue(acquired)
            with lock(path) as second:
                self.assertFalse(second)
        with lock(path) as released:
            self.assertTrue(released)

    def test_status_integration_and_deduplication(self):
        self.assertEqual(self.bot.run(self.now, STATUS), 0)
        result = read(self.bot.state_file, {})["jobs"]["2026-11-16/status"]
        self.assertEqual(result["status"], "done")
        self.assertIn("local test", Path(result["log"]).read_text(encoding="utf-8"))
        with patch("bot.subprocess.run") as execute:
            self.bot.run(self.now, STATUS)
            execute.assert_not_called()

    def test_failed_execution_record(self):
        with patch.object(self.bot, "guard"), patch("bot.subprocess.run", return_value=subprocess.CompletedProcess([], 9)):
            self.assertEqual(self.bot.run(self.now, STATUS), 1)
        self.assertEqual(read(self.bot.state_file, {})["jobs"]["2026-11-16/status"]["status"], "failed")
        self.assertTrue((self.home / "logs" / "bot.jsonl").is_file())

    def test_guard_failure_never_launches_script(self):
        with patch.object(self.bot, "guard", side_effect=RuntimeError("missing market data")), patch("bot.subprocess.run") as execute:
            self.assertEqual(self.bot.run(self.now, STATUS), 1)
            execute.assert_not_called()

    def test_pm_requires_successful_scan_and_clean_validation(self):
        pm = next(j for j in JOBS if j.name == "pm")
        market = {"ts": self.now.timestamp(), "indices": [{"code": "000001", "price": 3600}]}
        with patch.object(self.bot, "market_env", return_value=market):
            with self.assertRaisesRegex(RuntimeError, "初筛未成功"):
                self.bot.guard(pm, self.now, self.state)
            with self.bot.research() as store:
                version = store.version(self.now)
            self.state["jobs"]["2026-11-16/scan"] = {"status": "done", "research_version": version}
            path = self.bot.runtime / SHORT / "报告" / "候选_2026-11-16.json"
            dump(path, {"date": "2026-11-16", "validations": ["\u2705 日K历史：\u274c3", "\u274c 日K全源缺失"]})
            with self.assertRaisesRegex(RuntimeError, "校验异常"):
                self.bot.guard(pm, self.now, self.state)
            dump(path, {"date": "2026-11-16", "validations": ["\u2705 行情自洽性：\u270553 \u26a0\ufe0f0 \u274c0"]})
            self.bot.guard(pm, self.now, self.state)

    def test_nonempty_runtime_holdings_are_blocked(self):
        dump(self.bot.runtime / SHORT / "positions.json", {"positions": [{"code": "123456"}]})
        with self.assertRaisesRegex(RuntimeError, "非空持仓"):
            self.bot.guard(next(j for j in JOBS if j.name == "scan"), self.now, self.state)

    def test_incomplete_initialization(self):
        self.bot.state_file.unlink()
        with self.assertRaisesRegex(RuntimeError, "完成初始化"):
            self.bot.run(self.now, STATUS)

    def prepare_push(self, content="research report"):
        webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000"
        dump(self.home / "push_config.json", {"enabled": True, "webhook": webhook})
        job = next(j for j in JOBS if j.name == "week")
        report = self.bot.report_path(job, self.now)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(content, encoding="utf-8")
        return job, webhook

    def test_notification_disabled_and_not_applicable(self):
        job = next(j for j in JOBS if j.name == "week")
        with patch("bot.urllib.request.urlopen") as request:
            self.assertEqual(self.bot.notify(job, self.now, 0)["status"], "disabled")
            self.assertEqual(self.bot.notify(STATUS, self.now, 0)["status"], "not_applicable")
            request.assert_not_called()

    def test_feishu_payload_and_utf8_limit(self):
        for content in ("报告数据" * 10000, '"\\' * 10000):
            with self.subTest(content=content[:8]):
                job, webhook = self.prepare_push(content)
                with patch("bot.urllib.request.urlopen") as request:
                    request.return_value.__enter__.return_value.read.return_value = b'{"code": 0}'
                    self.assertEqual(self.bot.notify(job, self.now, 0)["status"], "sent")
                outgoing = request.call_args.args[0]
                self.assertEqual(outgoing.full_url, webhook)
                self.assertEqual(outgoing.get_method(), "POST")
                self.assertLessEqual(len(outgoing.data), 20000)
                payload = json.loads(outgoing.data.decode("utf-8"))
                self.assertEqual(payload["msg_type"], "text")
                text = payload["content"]["text"]
                self.assertIn("非实盘", text)
                self.assertIn("已截断", text)
                self.assertLessEqual(len(text.encode("utf-8")), 18000)
                self.assertEqual(request.call_count, 1)

    def test_feishu_business_error_redacts_secrets(self):
        job, webhook = self.prepare_push()
        with patch("bot.urllib.request.urlopen") as request:
            response = request.return_value.__enter__.return_value
            response.read.return_value = json.dumps({"code": 19024, "msg": webhook}).encode("utf-8")
            result = self.bot.notify(job, self.now, 0)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(webhook, json.dumps(result))

    def test_feishu_http_error_no_retry(self):
        job, webhook = self.prepare_push()
        error = urllib.error.HTTPError(webhook, 429, webhook, {}, None)
        with patch("bot.urllib.request.urlopen", side_effect=error) as request:
            result = self.bot.notify(job, self.now, 0)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(webhook, json.dumps(result))
        self.assertEqual(request.call_count, 1)

    def test_feishu_timeout_unknown_no_retry(self):
        job, webhook = self.prepare_push()
        with patch("bot.urllib.request.urlopen", side_effect=urllib.error.URLError(webhook)) as request:
            result = self.bot.notify(job, self.now, 0)
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn(webhook, json.dumps(result))
        self.assertEqual(request.call_count, 1)

    def test_feishu_response_requires_success_code(self):
        job, _ = self.prepare_push()
        for body in (b'{}', b'[]', b'invalid', b'{"code": "0"}', b'{"code": false}'):
            with self.subTest(body=body), patch("bot.urllib.request.urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = body
                self.assertIn(self.bot.notify(job, self.now, 0)["status"], ("failed", "unknown"))

    def test_invalid_webhook_no_request(self):
        job, webhook = self.prepare_push()
        dump(self.home / "push_config.json", {"enabled": True, "webhook": webhook + "?invalid=1"})
        with patch("bot.urllib.request.urlopen") as request:
            self.assertEqual(self.bot.notify(job, self.now, 0)["status"], "failed")
            request.assert_not_called()

    def test_empty_or_stale_report_no_request(self):
        job, _ = self.prepare_push("")
        with patch("bot.urllib.request.urlopen") as request:
            self.assertEqual(self.bot.notify(job, self.now, 0)["status"], "failed")
            self.bot.report_path(job, self.now).write_text("report", encoding="utf-8")
            self.assertEqual(self.bot.notify(job, self.now, time.time_ns() + 1)["status"], "failed")
            self.bot.report_path(job, self.now).unlink()
            self.assertEqual(self.bot.notify(job, self.now, 0)["status"], "failed")
            request.assert_not_called()

    def test_push_failure_preserves_report_and_never_reexecutes(self):
        job = next(j for j in JOBS if j.name == "week")
        with patch.object(self.bot, "guard"), patch.object(self.bot, "notify", return_value={"status": "unknown"}):
            self.assertEqual(self.bot.run(self.now, job), 1)
        result = read(self.bot.state_file, {})["jobs"]["2026-11-16/week"]
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["notification"]["status"], "unknown")
        with patch("bot.subprocess.run") as execute, patch.object(self.bot, "notify") as notify:
            self.assertEqual(self.bot.run(self.now, job), 1)
            execute.assert_not_called()
            notify.assert_not_called()

    def test_failed_guard_never_notifies(self):
        job = next(j for j in JOBS if j.name == "week")
        with patch.object(self.bot, "guard", side_effect=RuntimeError("blocked")), patch.object(self.bot, "notify") as notify:
            self.assertEqual(self.bot.run(self.now, job), 1)
            notify.assert_not_called()

    def test_notification_state_persisted_before_send(self):
        job = next(j for j in JOBS if j.name == "week")

        def notify(*args):
            result = read(self.bot.state_file, {})["jobs"]["2026-11-16/week"]
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["notification"]["status"], "started")
            return {"status": "sent"}

        with patch.object(self.bot, "guard"), patch.object(self.bot, "notify", side_effect=notify):
            self.assertEqual(self.bot.run(self.now, job), 0)

    def test_update_syncs_strategy_and_keeps_accumulated_data(self):
        copied = self.bot.runtime / SHORT
        dump(copied / "positions.json", {"positions": [], "paper": {"trades": [{"code": "600001"}]}})
        dump(copied / "报告" / "候选_2026-11-16.json", {"date": "2026-11-16"})
        with self.bot.research() as store:
            old = store.version(self.now)
        (self.source / "scripts" / "tail_momentum.py").write_text("print('optimized')\n", encoding="utf-8")
        dump(self.source / "config.json", {"push": True, "paper_track": True, "pct_low": 4})
        dump(self.source / "报告" / "候选_2026-11-16.json", {"date": "source must not overwrite"})
        result = self.bot.update(self.now)
        self.assertEqual(result["previous_version"], old)
        self.assertNotEqual(result["version"], old)
        self.assertEqual((copied / "scripts" / "tail_momentum.py").read_text(encoding="utf-8"), "print('optimized')\n")
        self.assertEqual(read(copied / "config.json", {}), {"push": False, "paper_track": False, "pct_low": 4})
        self.assertEqual(read(copied / "positions.json", {})["paper"]["trades"], [{"code": "600001"}])
        self.assertEqual(read(copied / "报告" / "候选_2026-11-16.json", {}), {"date": "2026-11-16"})
        with self.bot.research() as store:
            self.assertEqual(store.version(self.now), result["version"])

    def test_update_refused_between_scan_and_confirmation(self):
        dump(self.bot.state_file, {"jobs": {"2026-11-16/scan": {"status": "done"}}})
        engine = self.bot.runtime / SHORT / "scripts" / "tail_momentum.py"
        before = engine.read_bytes()
        (self.source / "scripts" / "tail_momentum.py").write_text("print('new')\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "尾盘确认尚未执行"):
            self.bot.update(self.now)
        self.assertEqual(engine.read_bytes(), before)
        dump(self.bot.state_file, {"jobs": {"2026-11-16/scan": {"status": "done"}, "2026-11-16/pm": {"status": "done"}}})
        self.bot.update(self.now)
        self.assertEqual(engine.read_text(encoding="utf-8"), "print('new')\n")

    def test_paper_settlement_precedes_confirmation(self):
        at = self.now.replace(minute=51)
        self.assertEqual([j.name for j in due(at, {})], ["paper_settle", "pm"])

    def test_settlement_independent_of_scan_failure(self):
        at = self.now.replace(minute=51)
        state = {"jobs": {"2026-11-16/scan": {"status": "failed"}}}
        dump(self.bot.state_file, state)
        job = next(j for j in JOBS if j.mode == "paper_settle")
        market = {"ts": at.timestamp(), "indices": [{"code": "000001", "price": 3600}]}
        with patch.object(self.bot, "market_env", return_value=market), patch.object(self.bot, "quotes", return_value={}), \
                patch.object(self.bot, "clock", return_value=at), patch("bot.subprocess.run") as execute:
            self.assertEqual(self.bot.run(at, job), 0)
            execute.assert_not_called()
        result = read(self.bot.state_file, {})["jobs"]["2026-11-16/paper_settle"]
        self.assertEqual(result["paper"]["closed"], 0)
        self.assertEqual(result["status"], "done")

    def test_pm_records_independent_paper_position(self):
        at = self.now.replace(minute=51)
        job = next(j for j in JOBS if j.mode == "pm")
        with self.bot.research() as store:
            version = store.version(at)
        dump(self.bot.state_file, {"jobs": {
            "2026-11-16/scan": {"status": "done", "research_version": version},
            "2026-11-16/paper_settle": {"status": "done"},
        }})
        report_dir = self.bot.runtime / SHORT / "报告"
        dump(report_dir / "候选_2026-11-16.json", {"date": "2026-11-16", "validations": ["passed"]})
        quote = {"code": "600000", "price": 10, "open": 10, "vol": 1000, "limit_up": 11,
                 "limit_down": 9, "time": "20261116145100"}

        def child(*args, **kwargs):
            dump(report_dir / "买入清单_2026-11-16.json", {
                "date": "2026-11-16", "generated": "2026-11-16 14:51",
                "confirmed": [{"code": "600000", "name": "fixture", "data_ok": True, "price": 10}],
            })
            return subprocess.CompletedProcess([], 0)

        market = {"ts": at.timestamp(), "indices": [{"code": "000001", "price": 3600}]}
        with patch.object(self.bot, "market_env", return_value=market), patch.object(self.bot, "quotes", return_value={"600000": quote}), \
                patch.object(self.bot, "clock", return_value=at), patch("bot.subprocess.run", side_effect=child):
            self.assertEqual(self.bot.run(at, job), 0)
        with self.bot.research() as store:
            self.assertEqual(len(store.account(version)["positions"]), 1)
            self.assertEqual(len(store.account(version)["closed"]), 0)
        self.assertEqual(read(self.bot.runtime / SHORT / "positions.json", {})["positions"], [])

    def test_config_change_after_scan_blocks_confirmation(self):
        job = next(j for j in JOBS if j.mode == "pm")
        with self.bot.research() as store:
            version = store.version(self.now)
        state = {"jobs": {"2026-11-16/scan": {"status": "done", "research_version": version}}}
        cfg_path = self.home / "research_config.json"
        cfg = read(cfg_path, {})
        cfg["commission_min"] += 1
        dump(cfg_path, cfg)
        market = {"ts": self.now.timestamp(), "indices": [{"code": "000001", "price": 3600}]}
        with patch.object(self.bot, "market_env", return_value=market):
            with self.assertRaisesRegex(RuntimeError, "版本发生变化"):
                self.bot.guard(job, self.now, state)

    def test_week_uses_new_report_not_original_diagnostics(self):
        job = next(j for j in JOBS if j.mode == "week")
        with patch.object(self.bot, "clock", return_value=self.now), patch("bot.subprocess.run") as execute:
            self.assertEqual(self.bot.run(self.now, job), 0)
            execute.assert_not_called()
        path = self.bot.report_path(job, self.now)
        self.assertIn(str(self.home / "research" / "reports"), str(path))
        self.assertIn("独立虚拟账户", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
