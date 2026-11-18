from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from adapter import CurrentSellPolicy
from notify import send_report
from report import render, save_report

CST = timezone(timedelta(hours=8))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.policy = CurrentSellPolicy()
        self.position = {"position_id": "600000/2026-10-16", "code": "600000", "name": "样本",
                         "entry_date": "2026-10-16", "entry_quote": 10, "shares": 1000,
                         "remaining_shares": 1000, "plan": {"stop": 9}, "prev_close": 10}
        self.now = datetime(2026, 10, 19, 10, 0, tzinfo=CST)
        self.quote = {"code": "600000", "time": "20261019100000", "price": 10.1, "open": 10,
                      "prev_close": 10, "vol": 1000, "high": 10.6, "limit_down": 9, "limit_up": 11}

    def test_pure_adapter_does_not_construct_or_connect(self):
        with patch.object(self.policy._module.Research, "__init__", side_effect=AssertionError("live constructor")), \
                patch("sqlite3.connect", side_effect=AssertionError("live database")):
            result = self.policy.decide(self.position, self.quote, {}, self.now, "1000", [])
        self.assertEqual(result["reason_id"], "high_pullback")
        self.assertEqual(result["sell_shares"], 500)

    def test_same_reason_deduplicates_only_actual_partial(self):
        events = [{"kind": "partial_sell", "position_id": self.position["position_id"],
                   "at": self.now.isoformat(), "reason_id": "high_pullback"}]
        result = self.policy.decide(self.position, self.quote, {}, self.now, "1000", events)
        self.assertEqual(result["action"], "hold")

    def test_vwap_missing_still_allows_valid_ma_afternoon(self):
        now = self.now.replace(hour=14)
        quote = {**self.quote, "time": "20261019140000", "high": 10.2}
        result = self.policy.decide(self.position, quote, {"ma5_last": 10.2, "vwap": None}, now, "1400", [])
        self.assertEqual(result["reason_id"], "afternoon_weak")

    def test_future_or_stale_quote_not_used(self):
        for time in ("20261019100100", "20261019095000"):
            result = self.policy.decide(self.position, {**self.quote, "time": time}, {}, self.now, "1000", [])
            self.assertEqual(result["action"], "defer")

    def test_config_and_source_frozen_in_version(self):
        self.assertEqual(self.policy.version, CurrentSellPolicy().version)
        self.assertNotEqual(self.policy.version, CurrentSellPolicy({"slippage_pct": 0.2}).version)
        self.assertEqual(self.policy.metadata["rules"]["schema"], 2)
        self.assertEqual(self.policy.execution(10, 100, "buy"), {"price": 10.01, "amount": 1001.0, "fee": 5.01, "cash": 1006.01})


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = self.root / "report.txt"
        self.report.write_text("报告\n一、结果\n收益-1%\n三、完整平仓明细\n明细\n六、解释边界（必须与收益一同阅读）\n仅信号回放，非完整选股回测\n", encoding="utf-8")
        self.config = self.root / "push.json"
        self.config.write_text(json.dumps({"enabled": True, "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000"}), encoding="utf-8")

    def send(self):
        return send_report(self.report, self.config, self.root / "notifications")

    def test_success_and_no_second_send(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"code":0}')) as post:
            self.assertEqual(self.send()["status"], "sent")
            self.assertEqual(self.send()["status"], "not_resent")
            post.assert_called_once()
            self.assertIn("解释边界", post.call_args.args[0].data.decode("utf-8"))
            self.assertNotIn("webhook", next((self.root / "notifications").glob("*.json")).read_text())

    def test_timeout_unknown_never_retried(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError) as post:
            self.assertEqual(self.send()["status"], "unknown")
            self.assertEqual(self.send()["previous"], "unknown")
            post.assert_called_once()

    def test_wrong_business_code_or_boolean_not_success(self):
        for code in (False, "0", 1):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as state:
                with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps({"code": code}).encode())):
                    self.assertEqual(send_report(self.report, self.config, state)["status"], "failed")

    def test_started_state_not_resent_after_interruption(self):
        with patch("urllib.request.urlopen", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.send()
        with patch("urllib.request.urlopen", side_effect=AssertionError("retry")):
            self.assertEqual(self.send()["previous"], "started")

    def test_missing_boundaries_not_sent(self):
        self.report.write_text("only good returns", encoding="utf-8")
        with patch("urllib.request.urlopen", side_effect=AssertionError("send")):
            with self.assertRaises(ValueError):
                self.send()

    def test_overlong_risk_text_not_silently_cut(self):
        with self.report.open("a", encoding="utf-8") as handle:
            handle.write("风险" * 10000)
        with patch("urllib.request.urlopen", side_effect=AssertionError("send")):
            with self.assertRaises(ValueError):
                self.send()

    def test_long_detail_sends_summary_without_cutting_limits(self):
        self.report.write_text("报告\n三、完整平仓明细\n" + "明细" * 10000 +
                               "\n六、解释边界（必须与收益一同阅读）\n非完整选股回测\n", encoding="utf-8")
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"code":0}')) as post:
            self.assertEqual(self.send()["content_mode"], "summary_with_limitations")
            self.assertLessEqual(len(post.call_args.args[0].data), 20000)
            self.assertIn("非完整选股回测", post.call_args.args[0].data.decode("utf-8"))

    def test_disabled_no_request(self):
        self.config.write_text('{"enabled":false}', encoding="utf-8")
        with patch("urllib.request.urlopen", side_effect=AssertionError("send")):
            self.assertEqual(self.send()["status"], "disabled")


class ReportTests(unittest.TestCase):
    def test_reproducible_negative_result_and_boundaries(self):
        dataset = {"start": "2026-10-16", "end": "2026-11-12", "through": "2026-11-16",
                   "signals": [{"id": "sample"}], "warnings": ["缺失VWAP"], "exclusions": [], "provenance": []}
        result = {"policy": CurrentSellPolicy().metadata, "config": CurrentSellPolicy().config,
                  "summary": {"n": 0, "final_equity": 99900, "total_return_pct": -0.1, "max_drawdown_pct": 0.1,
                              "fees": 10, "pnl": 0, "win_rate": None, "mean_net_pct": None},
                  "trades": [], "decisions": [], "fills": [], "skipped": [], "equity": [], "open_positions": []}
        with tempfile.TemporaryDirectory() as root:
            paths = save_report(result, dataset, root)
            again = save_report(result, dataset, root)
            self.assertEqual(paths, again)
            content = paths[0].read_text(encoding="utf-8")
            self.assertIn("-0.10%", content)
            self.assertIn("不是全市场选股回测", content)
            self.assertIn("缺失VWAP", content)
            self.assertIn("不做年化", content)
            self.assertNotIn("webhook", paths[1].read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as root:
            with patch.object(Path, "replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    save_report(result, dataset, root)
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertTrue(save_report(result, dataset, root)[1].is_file())


if __name__ == "__main__":
    unittest.main()
