import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

try:
    from . import evidence, run
except ImportError:
    import evidence
    import run


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()


class RecorderTests(TemporaryTest):
    def test_concurrent_raw_hash_nonfinite_and_summary(self):
        directory = self.root / "evidence"
        recorders = [evidence.Recorder(directory), evidence.Recorder(directory)]
        raw = bytes(range(256)) * 64
        digest = hashlib.sha256(raw).hexdigest()
        before = datetime.now(evidence.CST)

        def write(index):
            recorders[index % 2].event("http", {
                "index": index, "url": "https://push2.eastmoney.com/api/qt/stock/get?secid=0.000001",
                "encoding": "gbk", "started_at": before,
                "received_at": datetime.now(evidence.CST),
                "nested": [raw, {"raw": bytearray(raw), "view": memoryview(raw)}],
                "missing": [float("nan"), float("inf"), -float("inf")],
            })

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(write, range(60)))
        after = datetime.now(evidence.CST)
        events = recorders[0].summary()
        self.assertEqual(len(events), 60)
        self.assertEqual(len(list(directory.glob("stream-*.jsonl"))), 2)
        self.assertEqual([path.name for path in recorders[0].objects.iterdir()], [digest])
        self.assertEqual((recorders[0].objects / digest).read_bytes(), raw)
        self.assertEqual(sorted(event["data"]["index"] for event in events), list(range(60)))
        for event in events:
            timestamp = datetime.fromisoformat(event["observed_at"])
            self.assertEqual(timestamp.utcoffset(), timedelta(hours=8))
            self.assertLessEqual(before, timestamp)
            self.assertLessEqual(timestamp, after)
            self.assertEqual(event["data"]["missing"], [None, None, None])
            reference = {"blob_sha256": digest, "bytes": len(raw)}
            self.assertEqual(event["data"]["nested"], [reference, {"raw": reference, "view": reference}])
        self.assertEqual(recorders[0].health(), {"status": "ok", "errors": []})
        self.assertEqual(recorders[1].health(), {"status": "ok", "errors": []})

    def test_url_privacy_and_sensitive_keys(self):
        recorder = evidence.Recorder(self.root)
        recorder.event("http", {
            "url": "https://alice:PRIVATE@push2.eastmoney.com/api?secid=0.1&token=PRIVATE&ut=PRIVATE#PRIVATE",
            "headers": {"Authorization": "PRIVATE"},
            "request_headers": b"PRIVATE",
            "identity": "PRIVATE", "api_key": "PRIVATE", "webhook": "PRIVATE",
            "nested": {"password": "PRIVATE", "cookie": "PRIVATE", "user_id": "PRIVATE"},
            "urls": ["https://qt.gtimg.cn/q=s_sh000001", "https://hq.sinajs.cn/list=sh000001",
                     "https://eastmoney.com.evil.invalid/?token=PRIVATE",
                     "https://open.feishu.cn/open-apis/bot/v2/hook/PRIVATE",
                     "https://qt.gtimg.cn:invalid/path"],
            "message": "failed https://evil.invalid/PRIVATE",
            "raw": b"complete market payload",
        })
        event = recorder.summary()[0]
        data = event["data"]
        serialized = recorder.stream_path.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn("alice", serialized)
        self.assertEqual(data["url"], "https://push2.eastmoney.com/api?secid=0.1")
        self.assertEqual(data["nested"], {})
        self.assertEqual(data["urls"], ["https://qt.gtimg.cn/q=s_sh000001",
                         "https://hq.sinajs.cn/list=sh000001", "[REDACTED]", "[REDACTED]", "[REDACTED]"])
        self.assertEqual(len(list(recorder.objects.iterdir())), 1)
        self.assertEqual(recorder.health()["status"], "ok")

    def test_write_failure_latched_without_raise_or_retry(self):
        recorder = evidence.Recorder(self.root)
        with patch.object(recorder, "_append", side_effect=PermissionError("PRIVATE")) as append:
            recorder.event("http", {"raw": b"response"})
            self.assertEqual(append.call_count, 1)
        self.assertEqual(recorder.errors, ["PermissionError"])
        recorder.event("config", {"retry": False})
        self.assertEqual(len(recorder.summary()), 1)
        self.assertEqual(recorder.health(), {"status": "incomplete", "errors": ["PermissionError"]})
        self.assertNotIn("PRIVATE", recorder.stream_path.read_text(encoding="utf-8"))

    def test_blob_write_failure_and_unsupported_data(self):
        recorder = evidence.Recorder(self.root)
        with patch.object(evidence, "_write_once", side_effect=OSError("PRIVATE")) as write:
            recorder.event("http", {"raw": b"response"})
            self.assertEqual(write.call_count, 1)
        recorder.event("config", {"unsupported": object()})
        self.assertEqual(recorder.errors, ["OSError", "TypeError"])
        self.assertEqual(recorder.health()["status"], "incomplete")

    def test_truncated_invalid_and_duplicate_json(self):
        for contents in (b'{"kind":', b'{}\n', b'\n',
                         b'{"observed_at":"2026-11-18T12:00:00+08:00","kind":"a","kind":"b","data":{}}\n',
                         b'{"observed_at":"2026-11-18T12:00:00+08:00","kind":"a","data":NaN}\n'):
            with self.subTest(contents=contents):
                recorder = evidence.Recorder(self.root / hashlib.sha256(contents).hexdigest())
                recorder.stream_path.write_bytes(contents)
                self.assertEqual(recorder.health()["status"], "incomplete")

    def test_missing_blob_wrong_hash_and_wrong_size(self):
        for problem in ("missing", "hash", "size"):
            with self.subTest(problem=problem):
                recorder = evidence.Recorder(self.root / problem)
                recorder.event("http", {"raw": b"original"})
                event = recorder.summary()[0]
                path = recorder.objects / event["data"]["raw"]["blob_sha256"]
                if problem == "missing":
                    path.unlink()
                elif problem == "hash":
                    path.write_bytes(b"modified")
                else:
                    path.write_bytes(b"wrong length")
                self.assertEqual(recorder.health()["status"], "incomplete")

    def test_all_streams_worker_errors_and_unfinished_workers(self):
        parent = evidence.Recorder(self.root)
        child = evidence.Recorder(self.root)
        child.event("worker_start", {"returncode": None, "errors": []})
        self.assertEqual(parent.health()["status"], "incomplete")
        child.event("worker_end", {"returncode": 1, "errors": ["TimeoutError"]})
        self.assertEqual(parent.health()["status"], "incomplete")
        self.assertIn("TimeoutError", parent.health()["errors"])
        good = evidence.Recorder(self.root / "good")
        good.event("worker_start", {"returncode": None, "errors": []})
        good.event("worker_end", {"returncode": 0, "errors": []})
        self.assertEqual(good.health(), {"status": "ok", "errors": []})

    def test_child_evidence_errors_even_with_zero_returncode(self):
        parent = evidence.Recorder(self.root)
        child = evidence.Recorder(self.root)
        child.event("worker_end", {"returncode": 0, "errors": ["OSError"]})
        self.assertEqual(parent.health(), {"status": "incomplete", "errors": ["OSError"]})

    def test_health_does_not_follow_external_stream_or_blob(self):
        recorder = evidence.Recorder(self.root / "evidence")
        external = self.root / "outside"
        external.write_bytes(b"PRIVATE")
        link = recorder.directory / "stream-external.jsonl"
        link.write_bytes(b"PRIVATE")
        redirected = {link}
        original_read = Path.read_bytes
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            return external if path in redirected else original_resolve(path, *args, **kwargs)

        def read(path):
            if path == external or path in redirected:
                self.fail("external source was read")
            return original_read(path)

        with patch.object(Path, "resolve", resolve), patch.object(Path, "read_bytes", read):
            self.assertEqual(recorder.health()["status"], "incomplete")
        link.unlink()
        digest = hashlib.sha256(b"PRIVATE").hexdigest()
        blob = recorder.objects / digest
        blob.write_bytes(b"PRIVATE")
        redirected.add(blob)
        recorder.event("http", {"raw": {"blob_sha256": digest, "bytes": 7}})
        with patch.object(Path, "resolve", resolve), patch.object(Path, "read_bytes", read):
            self.assertEqual(recorder.health()["status"], "incomplete")

    def test_missing_stream_is_not_silently_recreated(self):
        recorder = evidence.Recorder(self.root)
        recorder.event("config", {"value": 1})
        recorder.stream_path.unlink()
        recorder.event("config", {"value": 2})
        self.assertFalse(recorder.stream_path.exists())
        self.assertEqual(recorder.errors, ["FileNotFoundError"])
        self.assertEqual(recorder.health()["status"], "incomplete")


class CampaignTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.code = self.root / "engine.py"
        self.code.write_text("VERSION = 'v1'\n", encoding="utf-8")
        self.directory = self.root / "campaign"
        self.paths = {"engine": self.code}
        self.spec = {"filters": {"threshold": 0.8, "reject_reasons": ["liquidity", "risk"]},
                     "scan": [1, 2, 3], "pm": {"enabled": True}}
        self.now = datetime(2026, 11, 18, 4, 30, tzinfo=timezone.utc)

    def start(self, **changes):
        values = dict(root=self.directory, version="v1", spec=self.spec,
                      now=self.now, code_paths=self.paths)
        values.update(changes)
        return evidence.start_campaign(**values)

    def test_manifest_complete_idempotent_no_overwrite(self):
        self.assertIsNone(evidence.verify_campaign(self.directory, "v1", self.paths))
        manifest = self.start()
        path = self.directory / "campaign.json"
        frozen = path.read_bytes()
        self.assertEqual(manifest["spec"], self.spec)
        self.assertEqual(manifest["code_hashes"], {"engine": hashlib.sha256(self.code.read_bytes()).hexdigest()})
        self.assertEqual(manifest["started_at"], "2026-11-18T12:30:00.000000+08:00")
        self.assertEqual(manifest["evaluation_targets"]["complete_scan_pm_trading_days"], 60)
        self.assertEqual(manifest["evaluation_targets"]["fully_closed_trades"], 100)
        self.assertIn("not_profit_proof", manifest["evaluation_targets"]["purpose"])
        content = {key: value for key, value in manifest.items() if key != "campaign_id"}
        encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        self.assertEqual(manifest["campaign_id"], hashlib.sha256(encoded).hexdigest())
        self.assertEqual(self.start(spec={"different": True}, now=self.now + timedelta(days=1)), manifest)
        self.assertEqual(evidence.verify_campaign(self.directory, "v1", self.paths), manifest)
        self.assertEqual(path.read_bytes(), frozen)
        self.assertEqual(list(self.directory.iterdir()), [path])

    def test_version_code_and_code_name_drift_refused_without_overwrite(self):
        self.start()
        path = self.directory / "campaign.json"
        frozen = path.read_bytes()
        for function in (lambda: self.start(version="v2"),
                         lambda: evidence.verify_campaign(self.directory, "v2", self.paths),
                         lambda: self.start(code_paths={"renamed": self.code})):
            with self.assertRaisesRegex(RuntimeError, "冻结验证版本发生变化，禁止新增样本"):
                function()
            self.assertEqual(path.read_bytes(), frozen)
        self.code.write_text("VERSION = 'v2'\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "冻结验证版本发生变化，禁止新增样本"):
            self.start()
        self.assertEqual(path.read_bytes(), frozen)
        self.code.unlink()
        with self.assertRaises(RuntimeError):
            evidence.verify_campaign(self.directory, "v1", self.paths)

    def test_self_hash_and_corrupt_manifest_refused(self):
        manifest = self.start()
        path = self.directory / "campaign.json"
        manifest["spec"]["scan"].append(4)
        tampered = json.dumps(manifest).encode("utf-8")
        path.write_bytes(tampered)
        with self.assertRaises(RuntimeError):
            self.start()
        self.assertEqual(path.read_bytes(), tampered)
        path.write_bytes(b'{"campaign_id":')
        with self.assertRaises(RuntimeError):
            evidence.verify_campaign(self.directory, "v1", self.paths)

    def test_sensitive_spec_and_nonfiles_rejected(self):
        for spec in ({"nested": {"webhook": "PRIVATE"}}, {"api_key": "PRIVATE"},
                     {"source": "https://open.feishu.cn/hook/PRIVATE"}, {"nan": float("nan")}):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.start(spec=spec)
            self.assertFalse((self.directory / "campaign.json").exists())
        for paths in ([self.root], {"push_config": self.code}, [], [self.code, self.code]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.start(code_paths=paths)
        with self.assertRaises(TypeError):
            self.start(code_paths=self.code)
        self.assertFalse((self.directory / "campaign.json").exists())

    def test_concurrent_campaign_creation_is_exclusive(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            manifests = list(pool.map(lambda index: self.start(now=self.now + timedelta(seconds=index)), range(16)))
        self.assertTrue(all(manifest == manifests[0] for manifest in manifests))
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_code_path_list(self):
        manifest = self.start(code_paths=[self.code])
        self.assertEqual(set(manifest["code_hashes"]), {"engine.py"})
        self.assertEqual(evidence.verify_campaign(self.directory, "v1", [self.code]), manifest)


class CaptureTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.engine = self.root / "engine.py"
        self.directory = self.root / "evidence"
        self.count = self.root / "calls.txt"
        self.make_engine("return 0")

    def make_engine(self, outcome):
        source = (
            "import sys\nfrom pathlib import Path\nfrom dataclasses import dataclass\n"
            "@dataclass\nclass Local:\n    value: int = 1\n"
            "if __name__ == '__main__':\n    raise AssertionError('main guard executed')\n"
            "def audit_event(kind, data):\n    pass\n"
            "def main():\n"
            "    with Path(__file__).with_name('calls.txt').open('a') as stream:\n"
            "        stream.write('once\\n')\n"
            "    audit_event('config', {'argv': sys.argv, 'value': Local().value})\n"
            "    audit_event('http', {'url': 'https://qt.gtimg.cn/q=sh000001', 'raw': b'full response'})\n"
            "    " + outcome + "\n"
        )
        self.engine.write_text(source, encoding="utf-8")

    def args(self, remainder=None):
        return ["capture", "--engine", str(self.engine), "--evidence", str(self.directory), "--",
                *(remainder if remainder is not None else ["--mode", "scan", "--no-push", "--no-hold"])]

    def records(self):
        events = []
        for path in self.directory.glob("stream-*.jsonl"):
            events.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
        return events

    def test_capture_once_argv_hook_no_main_guard_or_bytecode(self):
        original_argv = sys.argv
        original_path = list(sys.path)
        original_bytecode = sys.dont_write_bytecode
        frozen_engine = self.engine.read_bytes()
        self.assertEqual(run.main(self.args()), 0)
        self.assertEqual(self.count.read_text(), "once\n")
        events = self.records()
        self.assertEqual([event["kind"] for event in events], ["worker_start", "config", "http", "worker_end"])
        self.assertEqual(events[1]["data"]["argv"], [str(self.engine), "--mode", "scan", "--no-push", "--no-hold"])
        self.assertEqual(events[-1]["data"], {"returncode": 0, "errors": []})
        self.assertEqual(evidence.Recorder(self.directory).health()["status"], "ok")
        self.assertIs(sys.argv, original_argv)
        self.assertEqual(sys.path, original_path)
        self.assertEqual(sys.dont_write_bytecode, original_bytecode)
        self.assertFalse((self.root / "__pycache__").exists())
        self.assertEqual(self.engine.read_bytes(), frozen_engine)

    def test_allowed_modes_and_equals_form(self):
        for mode in ("scan", "pm", "ksnap"):
            with self.subTest(mode=mode):
                self.assertEqual(run.main(self.args(["--mode=" + mode, "--no-push", "--no-hold"])), 0)
        self.assertEqual(self.count.read_text(), "once\n" * 3)

    def test_gates_before_engine_or_evidence_creation(self):
        base = ["--mode", "scan", "--no-push", "--no-hold"]
        remainders = [[], ["--mode", "scan", "--no-push"], ["--mode", "scan", "--no-hold"],
                      ["--mode", "exit", "--no-push", "--no-hold"],
                      ["--mode", "auto", "--no-push", "--no-hold"],
                      ["--no-push", "--no-hold"], base + ["--mode=pm"],
                      base + ["--", "--hold", "1", "2"],
                      ["--mo", "scan", "--no-push", "--no-hold"]]
        remainders.extend(base + [option, "000001", "10"] for option in (
            "--hold", "--close", "--hold=000001", "--close=000001", "--ho", "--cl", "--h", "--c"
        ))
        for remainder in remainders:
            with self.subTest(remainder=remainder), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    run.main(self.args(remainder))
                self.assertEqual(caught.exception.code, 2)
                self.assertFalse(self.count.exists())
                self.assertFalse(self.directory.exists())
        for args in (["capture", "--engine", "relative.py", "--evidence", str(self.directory), "--", *base],
                     ["capture", "--engine", str(self.engine), "--evidence", "relative", "--", *base],
                     ["replay"], self.args()[:5] + base):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run.main(args)
        self.assertFalse(self.count.exists())
        self.assertFalse(self.directory.exists())

    def test_nonzero_exception_systemexit_and_interrupt_keep_evidence(self):
        for index, (outcome, expected, error) in enumerate((
            ("return 7", 7, None), ("raise RuntimeError('PRIVATE')", 1, "RuntimeError"),
            ("raise SystemExit(5)", 5, "SystemExit"), ("raise SystemExit()", 0, None),
            ("raise SystemExit('PRIVATE')", 1, "SystemExit"),
            ("raise KeyboardInterrupt()", 130, "KeyboardInterrupt"),
        )):
            with self.subTest(outcome=outcome):
                self.directory = self.root / ("evidence-" + str(index))
                self.make_engine(outcome)
                self.assertEqual(run.main(self.args()), expected)
                events = self.records()
                self.assertEqual(events[-1]["kind"], "worker_end")
                self.assertEqual(events[-1]["data"]["returncode"], expected)
                self.assertEqual(events[-1]["data"]["errors"], [error] if error else [])
                self.assertNotIn("PRIVATE", json.dumps(events))
                health = evidence.Recorder(self.directory).health()
                self.assertEqual(health["status"], "incomplete" if expected else "ok")
        self.assertEqual(self.count.read_text(), "once\n" * 6)

    def test_import_failure_is_recorded(self):
        self.engine.write_text("raise ValueError('PRIVATE')\n", encoding="utf-8")
        self.assertEqual(run.main(self.args()), 1)
        self.assertEqual([event["kind"] for event in self.records()], ["worker_start", "worker_end"])
        self.assertEqual(self.records()[-1]["data"], {"returncode": 1, "errors": ["ValueError"]})
        self.assertFalse(self.count.exists())
        self.assertNotIn("PRIVATE", json.dumps(self.records()))

    def test_capture_evidence_failure_no_engine_retry_preserves_returncode(self):
        self.make_engine("return 7")
        original = evidence.Recorder._append
        attempts = []

        def append(recorder, payload):
            if json.loads(payload)["kind"] == "config":
                attempts.append(1)
                raise OSError("PRIVATE")
            return original(recorder, payload)

        with patch.object(evidence.Recorder, "_append", append):
            self.assertEqual(run.main(self.args()), 7)
        self.assertEqual(attempts, [1])
        self.assertEqual(self.count.read_text(), "once\n")
        self.assertEqual(self.records()[-1]["data"], {"returncode": 7, "errors": ["OSError"]})
        self.assertIn("OSError", evidence.Recorder(self.directory).health()["errors"])

    def test_worker_end_write_failure_is_incomplete(self):
        original = evidence.Recorder._append
        attempts = []

        def append(recorder, payload):
            if json.loads(payload)["kind"] == "worker_end":
                attempts.append(1)
                raise OSError("PRIVATE")
            return original(recorder, payload)

        with patch.object(evidence.Recorder, "_append", append):
            self.assertEqual(run.main(self.args()), 0)
        self.assertEqual(attempts, [1])
        self.assertEqual(self.count.read_text(), "once\n")
        self.assertEqual(self.records()[-1]["kind"], "http")
        self.assertEqual(evidence.Recorder(self.directory).health()["status"], "incomplete")

    def test_cli_subprocess(self):
        result = subprocess.run([sys.executable, "-B", str(Path(run.__file__).resolve()), *self.args()],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.count.read_text(), "once\n")
        self.assertEqual(self.records()[-1]["kind"], "worker_end")


if __name__ == "__main__":
    unittest.main()
