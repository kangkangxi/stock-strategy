import copy
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest import mock

# Import only the sibling standalone module; never import the live bot.
_SPEC = importlib.util.spec_from_file_location("confirmed_replay_data", Path(__file__).with_name("data.py"))
data = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = data
_SPEC.loader.exec_module(data)


class ReplayDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.cache = self.root / "cache"
        self.output = self.root / "dataset.json"
        self.days = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21",
                     "2026-11-12", "2026-11-13", "2026-11-16"]
        self.payloads = {}
        self.fail_em = set()
        self.fail_sina = set()
        self.sina_payloads = {}
        self.http = mock.patch.object(data.request, "urlopen", side_effect=self.respond).start()
        self.addCleanup(mock.patch.stopall)
        self.report("2026-10-16", [self.signal()])
        self.snapshot("2026-10-16", {"600001": self.sessions("2026-10-19", "2026-10-20", "2026-10-21")})

    def write(self, path, payload):
        path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")

    def signal(self, code="600001", name="普通股票", **changes):
        row = {"code": code, "name": name, "price": 10.0, "data_ok": True,
               "plan": {"stop": 9.5, "target": 11.5, "entry_zone": [9.9, 10.1],
                        "qty": 0, "invalidate": ["Stop breach"], "notes": []}}
        row.update(changes)
        return row

    def report(self, day, confirmed, generated=None):
        self.write(self.reports / ("买入清单_" + day + ".json"),
                   {"date": day, "generated": generated or day + " 14:51", "confirmed": confirmed,
                    "rejected": [self.signal("600999")], "overflow": [self.signal("600998")]})

    def sessions(self, *days):
        return {day: [[end, 10.0, 11.0, 9.0, 10.0, 100.0] for end in data.SESSION_ENDS] for day in days}

    def snapshot(self, day, codes):
        self.write(self.reports / ("K线快照_" + day + ".json"),
                   {"date": day, "generated": "2026-11-18 15:10", "complete": True,
                    "target_days": ["2099-01-01"],  # deliberately untrusted; actual index is authoritative
                    "rows": {code: {"name": "普通股票", "kind": "trade", "bars": bars}
                             for code, bars in codes.items()}})

    def em_payload(self, code):
        symbol = "000001" if code == data.INDEX else code
        return {"data": {"code": symbol, "name": "Index" if code == data.INDEX else "普通股票",
                         "klines": [day + ",10,10,11,9,2400,240000,0,0,0,0" for day in self.days]}}

    def sina_payload(self):
        return [{"day": day + " 15:00:00", "open": "10", "high": "11", "low": "9",
                 "close": "10", "volume": "240000"} for day in self.days]

    def respond(self, req, timeout):
        self.assertEqual(req.get_method(), "GET")
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 15)
        self.assertNotIn("Authorization", req.headers)
        url = urlsplit(req.full_url)
        query = parse_qs(url.query)
        if url.hostname == "push2his.eastmoney.com":
            self.assertEqual(query["klt"], ["101"])
            self.assertEqual(query["fqt"], ["0"])
            self.assertEqual(query["lmt"], ["320"])
            secid = query["secid"][0]
            code = data.INDEX if secid == data.INDEX else secid.split(".")[1]
            if code in self.fail_em:
                raise OSError("do not persist https://secret.example/webhook?token=secret")
            body = self.payloads.get(code, self.em_payload(code))
        elif url.hostname == "money.finance.sina.com.cn":
            self.assertEqual(url.path, "/quotes_service/api/json_v2.php/CN_MarketData.getKLineData")
            self.assertEqual(query["scale"], ["240"])
            self.assertEqual(query["datalen"], ["320"])
            symbol = query["symbol"][0]
            code = data.INDEX if symbol == "sh000001" else symbol[2:]
            if code in self.fail_sina:
                raise OSError("no network fixture")
            body = self.sina_payloads.get(code, self.sina_payload())
        else:
            self.fail("unexpected URL " + req.full_url)
        raw = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        return io.BytesIO(raw)

    def prepare(self, **kwargs):
        return data.prepare(self.reports, self.output, cache_dir=self.cache, **kwargs)

    def reason(self, result, identifier="600001/2026-10-16"):
        return next(item["reason"] for item in result["exclusions"] if item["id"] == identifier)

    def assert_excluded(self, result, reason, identifier="600001/2026-10-16"):
        self.assertNotIn(identifier, [signal["id"] for signal in result["signals"]])
        self.assertIn(reason, self.reason(result, identifier))
        self.assertEqual(data.load_dataset(self.output), result)

    def tamper(self, valid, edit, reason=None):
        changed = copy.deepcopy(valid)
        edit(changed)
        self.write(self.output, changed)
        with self.assertRaises(data.DatasetError) as raised:
            data.load_dataset(self.output)
        if reason:
            self.assertIn(reason, str(raised.exception))

    def test_valid_interface_raw_metadata_provenance_and_offline_load(self):
        before = {path: path.read_bytes() for path in self.reports.iterdir()}
        result = self.prepare()
        self.assertEqual(result["scope"], "confirmed_signal_replay")
        self.assertEqual((result["start"], result["end"], result["through"]),
                         ("2026-10-16", "2026-11-12", "2026-11-16"))
        self.assertEqual(result["signals"][0]["required_dates"], ["2026-10-19", "2026-10-20"])
        daily = result["daily"]["600001"]
        self.assertEqual(daily["2026-10-16"]["prev_close"], 10)
        self.assertEqual(daily["2026-10-15"]["prev_close"], 10)
        self.assertEqual(daily["2026-10-16"]["limit_pct"], 0.1)
        self.assertTrue(all(row["amount"] is None for row in result["bars"]["600001"]["2026-10-19"]))
        self.assertEqual(len(result["bars"]["600001"]["2026-10-19"]), 24)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(before, {path: path.read_bytes() for path in self.reports.iterdir()})
        for item in result["provenance"]:
            self.assertEqual(item["sha256"], hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest())
            if item["kind"] in ("daily_cache", "calendar"):
                envelope = json.loads(Path(item["path"]).read_text(encoding="utf-8"))
                self.assertEqual(item["source_sha256"], hashlib.sha256(envelope["body"].encode()).hexdigest())
                self.assertIn("acquired_at", item)
        with mock.patch.object(data.request, "urlopen", side_effect=AssertionError("loader went online")):
            self.assertEqual(data.load_dataset(self.output), result)

    def test_import_has_no_network(self):
        with mock.patch.object(data.request, "urlopen", side_effect=AssertionError("import went online")):
            spec = importlib.util.spec_from_file_location("replay_import_test", Path(data.__file__))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertTrue(callable(module.prepare))

    def test_identical_signals_and_cross_file_bars_deduplicated(self):
        first = self.signal()
        second = self.signal(score=999, form="A")
        self.report("2026-10-16", [first, second])
        self.snapshot("2026-10-19", {"600001": self.sessions("2026-10-19", "2026-10-20")})
        result = self.prepare()
        self.assertEqual(len(result["signals"]), 1)
        self.assertTrue(any("已去重" in warning for warning in result["warnings"]))
        self.assertEqual(len(result["bars"]["600001"]["2026-10-19"]), 24)

    def test_conflicting_confirmed_signals_reject_not_first_or_last_wins(self):
        self.report("2026-10-16", [self.signal(), self.signal(price=10.1)])
        result = self.prepare()
        self.assert_excluded(result, "conflicting_confirmed_signal")
        self.assertEqual(self.http.call_count, 1)  # only index, not an ambiguous symbol

    def test_conflicting_bar_rejects_entire_code_date_and_affected_signals(self):
        self.report("2026-10-19", [self.signal()])
        self.snapshot("2026-10-19", {"600001": self.sessions("2026-10-20", "2026-10-21")})
        conflicting = self.sessions("2026-10-20")
        conflicting["2026-10-20"][5][-1] += 1  # even volume-only disagreement is a conflict
        self.snapshot("2026-10-20", {"600001": conflicting})
        result = self.prepare()
        self.assert_excluded(result, "conflicting_snapshot_bars")
        self.assert_excluded(result, "conflicting_snapshot_bars", "600001/2026-10-19")
        self.assertEqual(result["bars"], {})

    def test_conflict_does_not_exclude_unrelated_code_or_later_entry(self):
        self.report("2026-10-16", [self.signal(), self.signal("600002")])
        self.report("2026-11-12", [self.signal()])
        self.snapshot("2026-10-16", {"600001": self.sessions("2026-10-19", "2026-10-20"),
                                     "600002": self.sessions("2026-10-19", "2026-10-20")})
        conflict = self.sessions("2026-10-19")
        conflict["2026-10-19"][0][1] = 10.1
        self.snapshot("2026-10-19", {"600001": conflict})
        self.snapshot("2026-11-12", {"600001": self.sessions("2026-11-13", "2026-11-16")})
        result = self.prepare()
        self.assert_excluded(result, "conflicting_snapshot_bars")
        self.assertEqual([signal["id"] for signal in result["signals"]],
                         ["600002/2026-10-16", "600001/2026-11-12"])

    def test_incomplete_first_session_not_repaired_by_complete_second_session(self):
        sessions = self.sessions("2026-10-19", "2026-10-20")
        sessions["2026-10-19"].pop(0)
        self.snapshot("2026-10-16", {"600001": sessions})
        result = self.prepare()
        self.assert_excluded(result, "incomplete_24bar_session:2026-10-19")

    def test_complementary_identical_sources_can_complete_a_session(self):
        first = self.sessions("2026-10-19", "2026-10-20")
        other = self.sessions("2026-10-19")
        first["2026-10-19"] = first["2026-10-19"][:12]
        other["2026-10-19"] = other["2026-10-19"][12:]
        self.snapshot("2026-10-16", {"600001": first})
        self.snapshot("2026-10-19", {"600001": other})
        self.assertEqual(len(self.prepare()["signals"]), 1)

    def test_invalid_snapshot_ohlc_volume_time_and_duplicate_time(self):
        cases = [("ordering", lambda rows: rows[0].__setitem__(2, 8), "OHLC ordering"),
                 ("zero", lambda rows: rows[0].__setitem__(1, 0), "price/volume"),
                 ("negative_volume", lambda rows: rows[0].__setitem__(5, -1), "price/volume"),
                 ("boolean", lambda rows: rows[0].__setitem__(1, True), "non-numeric"),
                 ("numeric_string", lambda rows: rows[0].__setitem__(1, "10"), "non-numeric"),
                 ("lunch", lambda rows: rows[0].__setitem__(0, "12:00"), "session_time"),
                 ("opening", lambda rows: rows[0].__setitem__(0, "09:30"), "session_time"),
                 ("duplicate", lambda rows: rows.append(rows[0][:]), "duplicate_snapshot_session_time")]
        for name, edit, reason in cases:
            with self.subTest(name=name):
                sessions = self.sessions("2026-10-19", "2026-10-20")
                edit(sessions["2026-10-19"])
                self.snapshot("2026-10-16", {"600001": sessions})
                self.assert_excluded(self.prepare(), reason)

    def test_range_filter_prevents_post_hoc_and_future_inputs_and_fetches(self):
        self.report("2026-10-14", [self.signal("600014")], generated="2026-10-16 14:51")
        self.report("2026-11-13", [self.signal("600013")])
        self.report("2026-11-18", [self.signal("600018")])
        self.report("2026-11-12", [self.signal("600012")], generated="2026-11-16 14:51")
        sessions = self.sessions("2026-10-19", "2026-10-20", "2026-11-17")
        sessions["2026-11-17"][0][2] = 1  # future invalid data must not contaminate eligible signal
        self.snapshot("2026-10-16", {"600001": sessions, "600018": self.sessions("2026-11-17")})
        self.days.append("2026-11-17")
        result = self.prepare()
        self.assertEqual([signal["id"] for signal in result["signals"]], ["600001/2026-10-16"])
        self.assert_excluded(result, "entry_outside_range", "600014/2026-10-14")
        self.assert_excluded(result, "entry_outside_range", "600013/2026-11-13")
        self.assert_excluded(result, "post_hoc_generated", "600012/2026-11-12")
        self.assertLessEqual(max(result["calendar"]), result["through"])
        self.assertLessEqual(max(result["daily"]["600001"]), result["through"])
        self.assertNotIn("2026-11-17", result["bars"]["600001"])
        self.assertEqual(self.http.call_count, 2)
        urls = [call.args[0].full_url for call in self.http.call_args_list]
        self.assertTrue(all("end=20261116" in url for url in urls))
        self.assertTrue(all("600013" not in url and "600018" not in url and "600999" not in url for url in urls))

    def test_actual_index_calendar_not_weekdays_or_snapshot_target_days(self):
        self.days.remove("2026-10-19")  # simulate an exchange holiday (not a weekday calculation)
        self.snapshot("2026-10-16", {"600001": self.sessions("2026-10-20", "2026-10-21")})
        result = self.prepare()
        self.assertEqual(result["signals"][0]["required_dates"], ["2026-10-20", "2026-10-21"])
        self.assertNotIn("2026-10-19", result["calendar"])
        self.assertEqual(data.load_dataset(self.output), result)

    def test_observation_requires_T2_even_at_right_entry_boundary(self):
        self.report("2026-11-12", [self.signal("600012")])
        self.snapshot("2026-11-12", {"600012": self.sessions("2026-11-13", "2026-11-16")})
        result = self.prepare(start="2026-11-12", end="2026-11-12", through="2026-11-13")
        self.assert_excluded(result, "insufficient_T1_T2_calendar", "600012/2026-11-12")
        self.assertEqual(max(result["calendar"]), "2026-11-13")

    def test_raw_daily_snapshot_mismatch_each_ohlc_field(self):
        for field, value in (("o", 10.1), ("h", 11.1), ("l", 8.9), ("c", 10.1)):
            with self.subTest(field=field):
                payload = self.em_payload("600001")
                index = self.days.index("2026-10-19")
                values = payload["data"]["klines"][index].split(",")
                values[{"o": 1, "c": 2, "h": 3, "l": 4}[field]] = str(value)
                payload["data"]["klines"][index] = ",".join(values)
                self.payloads["600001"] = payload
                cached = self.cache / "600001-2026-11-16.json"
                if cached.exists():
                    cached.unlink()  # fixture only; explicit new acquisition per subtest
                self.assert_excluded(self.prepare(), "daily_snapshot_mismatch:2026-10-19:" + field)

    def test_cent_tolerance_and_raw_entry_high_low(self):
        self.report("2026-10-16", [self.signal(price=11.02)])
        payload = self.em_payload("600001")
        index = self.days.index("2026-10-19")
        payload["data"]["klines"][index] = "2026-10-19,10.02,10.02,11.02,8.98,2400,240000,0,0,0.02,0"
        self.payloads["600001"] = payload
        self.assertEqual(len(self.prepare()["signals"]), 1)
        self.report("2026-10-16", [self.signal(price=11.03)])
        self.assert_excluded(self.prepare(), "signal_price_outside_raw_entry_range")

    def test_reference_close_mismatch_rejects_corporate_action_or_bad_reference(self):
        payload = self.em_payload("600001")
        index = self.days.index("2026-10-19")
        fields = payload["data"]["klines"][index].split(",")
        fields[9] = "1"
        payload["data"]["klines"][index] = ",".join(fields)
        self.payloads["600001"] = payload
        self.assert_excluded(self.prepare(), "reference_close_or_corporate_action_mismatch")

    def test_bad_optional_T3_session_does_not_remove_valid_T1_T2_signal(self):
        sessions = self.sessions("2026-10-19", "2026-10-20", "2026-10-21")
        for bar in sessions["2026-10-21"]:
            bar[-1] = 0
        self.snapshot("2026-10-16", {"600001": sessions})
        result = self.prepare()
        self.assertEqual(len(result["signals"]), 1)
        self.assertNotIn("2026-10-21", result["bars"]["600001"])
        self.assertTrue(any("已移除" in warning for warning in result["warnings"]))

    def test_snapshot_zero_volume_cannot_contradict_traded_daily(self):
        sessions = self.sessions("2026-10-19", "2026-10-20")
        for bar in sessions["2026-10-19"]:
            bar[-1] = 0
        self.snapshot("2026-10-16", {"600001": sessions})
        self.assert_excluded(self.prepare(), "daily_snapshot_volume_mismatch")

    def test_snapshot_volume_tolerance_not_unit_mismatch(self):
        sessions = self.sessions("2026-10-19", "2026-10-20")
        for bar in sessions["2026-10-19"]:
            bar[-1] = 100.5
        self.snapshot("2026-10-16", {"600001": sessions})
        self.assertEqual(len(self.prepare()["signals"]), 1)
        for bar in sessions["2026-10-19"]:
            bar[-1] = 10000
        self.snapshot("2026-10-16", {"600001": sessions})
        self.assert_excluded(self.prepare(), "daily_snapshot_volume_mismatch")

    def test_missing_raw_daily_and_missing_actual_previous_close(self):
        for mode in ("entry", "T1", "previous"):
            with self.subTest(mode=mode):
                payload = self.em_payload("600001")
                remove = {"entry": "2026-10-16", "T1": "2026-10-19", "previous": "2026-10-15"}[mode]
                payload["data"]["klines"] = [row for row in payload["data"]["klines"] if not row.startswith(remove)]
                self.payloads["600001"] = payload
                cached = self.cache / "600001-2026-11-16.json"
                if cached.exists():
                    cached.unlink()
                result = self.prepare()
                self.assert_excluded(result, "missing_daily" if mode != "previous" else "missing_previous_exchange_daily")

    def test_missing_previous_session_is_not_assumed_to_be_suspension(self):
        self.days.insert(0, "2026-10-14")
        payload = self.em_payload("600001")
        payload["data"]["klines"][0] = "2026-10-14,9.5,9.5,10,9,2400,240000,0,0,0,0"
        payload["data"]["klines"] = [row for row in payload["data"]["klines"]
                                      if not row.startswith("2026-10-15")]
        self.payloads["600001"] = payload
        result = self.prepare()
        self.assertIn("2026-10-15", result["calendar"])
        self.assert_excluded(result, "missing_previous_exchange_daily:2026-10-16")
        self.assertEqual(result["signals"], [])

    def test_ST_name_risks_and_boards(self):
        rows = [self.signal("600001", "*ST risk"), self.signal("600002", "退市股票"),
                self.signal("600003", "N新股"), self.signal("600004", "C新股"),
                self.signal("830001", "Beijing"), self.signal("600005", data_ok=False),
                self.signal("300001", "创业板"), self.signal("301001", "创业板二"),
                self.signal("688001", "科创-U"), self.signal("002001", "主板")]
        self.report("2026-10-16", rows)
        self.snapshot("2026-10-16", {code: self.sessions("2026-10-19", "2026-10-20")
                                     for code in ("300001", "301001", "688001", "002001")})
        result = self.prepare()
        for code in ("600001", "600002", "600003", "600004"):
            self.assert_excluded(result, "st_or_name_risk", code + "/2026-10-16")
        self.assert_excluded(result, "unsupported board", "830001/2026-10-16")
        self.assertEqual(len(result["signals"]), 4)
        self.assertEqual(self.http.call_count, 5)
        for code in ("300001", "301001", "688001"):
            self.assertEqual(result["daily"][code]["2026-10-16"]["limit_pct"], .2)
        self.assertEqual(result["daily"]["002001"]["2026-10-16"]["limit_pct"], .1)

    def test_risk_name_from_daily_source_excludes(self):
        payload = self.em_payload("600001")
        payload["data"]["name"] = "ST source risk"
        self.payloads["600001"] = payload
        self.assert_excluded(self.prepare(), "daily_source_st_or_name_risk")

    def test_stable_order_and_no_hardcoded_symbol_exclusions(self):
        self.report("2026-10-16", [self.signal("688343", "云天励飞-U"), self.signal("600637", "东方明珠")])
        self.snapshot("2026-10-16", {code: self.sessions("2026-10-19", "2026-10-20")
                                     for code in ("688343", "600637")})
        result = self.prepare()
        self.assertEqual([signal["code"] for signal in result["signals"]], ["600637", "688343"])

    def test_cache_reuse_is_offline_and_hash_verified(self):
        first = self.prepare()
        self.http.reset_mock()
        self.http.side_effect = AssertionError("cached preparation attempted HTTP")
        self.assertEqual(self.prepare(), first)
        self.http.assert_not_called()
        path = self.cache / "600001-2026-11-16.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["body"] += " "
        self.write(path, envelope)
        broken = path.read_bytes()
        result = self.prepare()
        self.assert_excluded(result, "daily_unavailable")
        self.assertEqual(path.read_bytes(), broken)
        self.assertTrue(any(item.get("status") == "invalid" for item in result["provenance"]))
        self.http.assert_not_called()

    def test_bounded_sina_fallback_no_estimated_amount_or_future_dates(self):
        self.fail_em.add("600001")
        self.days.append("2026-11-17")
        result = self.prepare()
        self.assert_excluded(result, "daily_reference_unavailable")
        source = next(item for item in result["provenance"] if item.get("code") == "600001")
        self.assertEqual(source["source"], "sina")
        parsed, _ = data._parse_daily(json.dumps(self.sina_payload()), "sina", "600001", "2026-11-16")
        self.assertEqual(parsed["2026-10-19"]["v"], 2400)
        self.assertNotIn("2026-11-17", parsed)
        self.assertEqual(self.http.call_count, 3)
        self.assertNotIn("secret.example", json.dumps(result))
        self.assertNotIn("token=secret", json.dumps(result))

    def test_sina_unquoted_keys_are_parsed_without_eval(self):
        self.fail_em.add("600001")
        body = json.dumps(self.sina_payload())
        for field in ("day", "open", "high", "low", "close", "volume"):
            body = body.replace('"' + field + '":', field + ":")
        self.sina_payloads["600001"] = body
        self.assert_excluded(self.prepare(), "daily_reference_unavailable")
        parsed, _ = data._parse_daily(body, "sina", "600001", "2026-11-16")
        self.assertEqual(parsed["2026-10-19"]["v"], 2400)

    def test_all_daily_sources_fail_preserves_failure_manifest_and_cache(self):
        self.fail_em.add("600001")
        self.fail_sina.add("600001")
        result = self.prepare()
        self.assert_excluded(result, "daily_unavailable")
        source = next(item for item in result["provenance"] if item.get("code") == "600001")
        self.assertEqual(source["status"], "failed")
        self.assertEqual(len(source["failures"]), 2)
        self.assertEqual(result["daily"], {})
        self.assertEqual(self.http.call_count, 3)
        self.http.reset_mock()
        self.assertEqual(self.prepare(), result)
        self.http.assert_not_called()
        self.assertNotIn("secret.example", json.dumps(result))

    def test_calendar_failure_does_not_fabricate_weekdays_or_fetch_universe(self):
        self.fail_em.add(data.INDEX)
        self.fail_sina.add(data.INDEX)
        result = self.prepare()
        self.assert_excluded(result, "calendar_unavailable")
        self.assertEqual(result["calendar"], [])
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["bars"], {})
        self.assertEqual(self.http.call_count, 2)

    def test_invalid_daily_response_falls_back_or_fails_without_fabrication(self):
        payload = self.em_payload("600001")
        payload["data"]["klines"][0] = "2026-10-15,NaN,10,11,9,2400,240000"
        self.payloads["600001"] = payload
        self.fail_sina.add("600001")
        self.assert_excluded(self.prepare(), "daily_unavailable")

    def test_duplicate_daily_dates_are_rejected(self):
        payload = self.em_payload("600001")
        payload["data"]["klines"].append(payload["data"]["klines"][1])
        self.payloads["600001"] = payload
        self.fail_sina.add("600001")
        self.assert_excluded(self.prepare(), "daily_unavailable")

    def test_loader_rejects_tampering_structural_and_semantic(self):
        valid = self.prepare()
        edits = [("schema", lambda d: d.update(schema=True)),
                 ("scope", lambda d: d.update(scope="universe_backtest")),
                 ("duplicate_signal", lambda d: d["signals"].append(copy.deepcopy(d["signals"][0]))),
                 ("duplicate_calendar", lambda d: d["calendar"].append(d["calendar"][-1])),
                 ("unsorted_calendar", lambda d: d["calendar"].reverse()),
                 ("future_calendar", lambda d: d["calendar"].append("2026-11-17")),
                 ("required_dates", lambda d: d["signals"][0].update(required_dates=["2026-10-18", "2026-10-19"])),
                 ("entry_range", lambda d: d.update(start="2026-10-19")),
                 ("post_hoc", lambda d: d["signals"][0].update(generated="2026-11-18 14:51")),
                 ("id", lambda d: d["signals"][0].update(id="other")),
                 ("data_ok", lambda d: d["signals"][0].update(data_ok=1)),
                 ("plan_secret", lambda d: d["signals"][0]["plan"].update(webhook="secret")),
                 ("price", lambda d: d["signals"][0].update(price=12)),
                 ("prev_close", lambda d: d["daily"]["600001"]["2026-10-19"].update(prev_close=9)),
                 ("prev_missing", lambda d: d["daily"]["600001"]["2026-10-19"].update(prev_close=None)),
                 ("limit", lambda d: d["daily"]["600001"]["2026-10-19"].update(limit_pct=.2)),
                 ("daily_missing", lambda d: d["daily"]["600001"].pop("2026-10-19")),
                 ("daily_high", lambda d: d["daily"]["600001"]["2026-10-19"].update(h=11.1)),
                 ("session_missing", lambda d: d["bars"]["600001"]["2026-10-19"].pop(0)),
                 ("bar_duplicate", lambda d: d["bars"]["600001"]["2026-10-19"][1].update(end="2026-10-19 09:40")),
                 ("estimated_amount", lambda d: d["bars"]["600001"]["2026-10-19"][0].update(amount=100)),
                 ("bar_ohlc", lambda d: d["bars"]["600001"]["2026-10-19"][0].update(l=12)),
                 ("negative_volume", lambda d: d["bars"]["600001"]["2026-10-19"][0].update(v=-1)),
                 ("hash", lambda d: d["provenance"][0].update(sha256="bad")),
                 ("source_adjustment", lambda d: next(p for p in d["provenance"] if p["kind"] == "daily_cache").update(adjustment="adjusted")),
                 ("missing_assumption", lambda d: d["warnings"].clear()),
                 ("overlap_exclusion", lambda d: d["exclusions"].append({"id": d["signals"][0]["id"], "reason": "bad"})),
                 ("unknown_root", lambda d: d.update(credentials="secret")),
                 ("orphan", lambda d: d["bars"].update({"600002": {}}))]
        for name, edit in edits:
            with self.subTest(name=name):
                self.tamper(valid, edit)

    def test_loader_rejects_nonfinite_json_and_duplicate_keys(self):
        valid = self.prepare()
        text = json.dumps(valid)
        self.output.write_text(text.replace('"price": 10.0', '"price": NaN'), encoding="utf-8")
        with self.assertRaises(data.DatasetError):
            data.load_dataset(self.output)
        self.output.write_text(text.replace('"schema": 1', '"schema": 1, "schema": 1'), encoding="utf-8")
        with self.assertRaises(data.DatasetError):
            data.load_dataset(self.output)

    def test_input_duplicate_json_keys_fail_closed_before_network(self):
        path = self.reports / "K线快照_2026-10-16.json"
        path.write_text('{"date":"2026-10-16","rows":{},"rows":{}}', encoding="utf-8")
        with self.assertRaises(data.DatasetError):
            self.prepare()
        self.http.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_sensitive_extra_input_fields_are_not_copied(self):
        signal = self.signal(webhook="https://secret.example", credentials={"token": "secret"})
        signal["plan"]["webhook"] = "https://secret.example"
        self.report("2026-10-16", [signal])
        result = self.prepare()
        self.assertNotIn("secret.example", json.dumps(result))
        self.assertNotIn("credentials", json.dumps(result))

    def test_snapshot_name_risk_excludes_affected_signal(self):
        path = self.reports / "K线快照_2026-10-16.json"
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot["rows"]["600001"]["name"] = "*ST snapshot risk"
        self.write(path, snapshot)
        self.assert_excluded(self.prepare(), "snapshot_st_or_name_risk")

    def test_daily_date_disagreement_with_index_is_not_silently_dropped(self):
        payload = self.em_payload(data.INDEX)
        payload["data"]["klines"] = [row for row in payload["data"]["klines"]
                                      if not row.startswith("2026-10-21")]
        self.payloads[data.INDEX] = payload
        # No snapshot on disputed date: daily/calendar inconsistency alone must fail.
        self.snapshot("2026-10-16", {"600001": self.sessions("2026-10-19", "2026-10-20")})
        self.assert_excluded(self.prepare(), "daily_date_not_in_exchange_calendar:2026-10-21")

    def test_failure_cache_cannot_leak_arbitrary_fields_into_dataset(self):
        self.fail_em.add("600001")
        self.fail_sina.add("600001")
        self.prepare()
        path = self.cache / "600001-2026-11-16.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["attempts"][0]["error"] = "https://secret.example/token"
        self.write(path, envelope)
        self.http.reset_mock()
        result = self.prepare()
        self.assert_excluded(result, "daily_unavailable")
        self.assertNotIn("secret.example", json.dumps(result))
        self.http.assert_not_called()

    def test_loader_provenance_is_strict(self):
        valid = self.prepare()
        for edit in (lambda d: d["provenance"][0].update(webhook="https://secret.example"),
                     lambda d: next(p for p in d["provenance"] if p["kind"] == "calendar").pop("acquired_at"),
                     lambda d: next(p for p in d["provenance"] if p["kind"] == "daily_cache").update(source_sha256="bad")):
            self.tamper(valid, edit)

    def test_entry_generation_after_close_is_not_a_historical_entry(self):
        self.report("2026-10-16", [self.signal()], generated="2026-10-16 15:00:59")
        self.assert_excluded(self.prepare(), "generated_outside_entry_session")
        self.assertEqual(self.http.call_count, 1)

    def test_entry_generation_matches_replay_1451_minute(self):
        for generated in ("2026-10-16 09:30", "2026-10-16 14:56", "2026-10-16 14:51:07"):
            with self.subTest(generated=generated):
                self.report("2026-10-16", [self.signal()], generated=generated)
                self.assert_excluded(self.prepare(), "generated_outside_entry_session")

    def test_prepare_refuses_to_write_into_read_only_reports(self):
        with self.assertRaises(data.DatasetError):
            data.prepare(self.reports, self.reports / "out.json", cache_dir=self.cache)
        with self.assertRaises(data.DatasetError):
            data.prepare(self.reports, self.output, cache_dir=self.reports / "cache")
        self.http.assert_not_called()

    def test_invalid_range_is_rejected_before_http(self):
        for kwargs in ({"start": "2026-11-13"}, {"through": "2026-11-01"}, {"start": "2026-10-1"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(data.DatasetError):
                self.prepare(**kwargs)
        self.http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
