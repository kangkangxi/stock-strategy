import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from urllib import request

DEFAULT_START = "2026-10-16"
DEFAULT_END = "2026-11-12"
DEFAULT_THROUGH = "2026-11-16"
INDEX = "1.000001"
HTTP_TIMEOUT = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SESSION_ENDS = tuple(
    f"{minute // 60:02d}:{minute % 60:02d}"
    for first, last in ((580, 690), (790, 900))
    for minute in range(first, last + 1, 10)
)
EM_URL = (
    "http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
    "&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    "&klt=101&fqt=0&end={end}&lmt=320"
)
SINA_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=320"
)
ASSUMPTIONS = [
    "仅历史确认信号执行回放，不是全市场选股回测。",
    "涨跌停幅度按普通主板10%、300/301/688按20%假设；历史特殊限制资料不足，明显ST、退市及N/C名称风险输入剔除。",
    "快照及日线成交量单位为手；快照没有成交额，不估算VWAP。日/分钟总量按最多24手或日量0.5%的较大值容许来源取整/修订差异。",
    "日线不复权；参考价由东财当日收盘价减当日涨跌额取得，再核验前一实际交易日原始收盘。前日数据缺失或参考价冲突（含疑似除权）剔除，不补造股息及收益；新浪日线缺参考价，仅可补交易日历，不用于股票成交回放。",
    "快照取得时间可能晚于入场；仅使用入场日14:51—14:55分钟生成的确认信号，时间戳不能证明当时留存。",
]


class DatasetError(ValueError):
    """Malformed or unsafe replay data."""


def _require(condition, message):
    if not condition:
        raise DatasetError(message)


def _iso(value):
    _require(isinstance(value, str), "date must be an ISO string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DatasetError("invalid ISO date") from exc
    _require(parsed.isoformat() == value, "noncanonical ISO date")
    return value


def _range(start, end, through):
    for value in (start, end, through):
        _iso(value)
    _require(start <= end <= through, "require start <= end <= through")


def _timestamp(value):
    _require(isinstance(value, str), "generated must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DatasetError("invalid generated timestamp") from exc
    # Live reports use naive exchange-local minutes. Reject timezone ambiguity.
    _require(parsed.tzinfo is None and len(value) in (16, 19), "expected exchange-local timestamp")
    _require(value == parsed.strftime("%Y-%m-%d %H:%M" if len(value) == 16
                                     else "%Y-%m-%d %H:%M:%S"), "noncanonical timestamp")
    return parsed


def _number(value, positive=False):
    _require(type(value) in (int, float) and math.isfinite(value), "nonfinite/non-numeric value")
    _require(value > 0 if positive else value >= 0, "invalid price/volume")
    return value


def _ohlcv(row):
    _require(isinstance(row, dict), "OHLCV must be an object")
    for key in ("o", "h", "l", "c"):
        _number(row.get(key), positive=True)
    _number(row.get("v"))
    _require(row["l"] <= min(row["o"], row["c"]) <= max(row["o"], row["c"]) <= row["h"],
             "invalid OHLC ordering")


def _code(code):
    _require(isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code) is not None,
             "code must be six digits")
    return code


def _limit(code):
    _code(code)
    if code.startswith(("300", "301", "688")):
        return 0.20
    _require(code.startswith(("000", "001", "002", "003", "600", "601", "603", "605")),
             "unsupported board")
    return 0.10


def _name_risk(name):
    if not isinstance(name, str) or not name.strip():
        return True
    compact = re.sub(r"\s+", "", name).upper()
    return "ST" in compact or "退" in compact or compact.startswith(("N", "C"))


def _text(value):
    _require(isinstance(value, str), "expected string")
    _require(not re.search(r"https?://", value, re.I), "URLs are not permitted in signal text")
    return value


def _plan(raw):
    """Whitelist strategy fields; do not copy runtime/config/credential metadata."""
    _require(isinstance(raw, dict), "missing/invalid plan")
    result = {}
    for key in ("stop", "target"):
        result[key] = _number(raw.get(key), positive=True)
    if "entry_zone" in raw:
        zone = raw["entry_zone"]
        _require(isinstance(zone, list) and len(zone) == 2, "invalid entry_zone")
        result["entry_zone"] = [_number(x, positive=True) for x in zone]
        _require(zone[0] <= zone[1], "reversed entry_zone")
    for key in ("stop_pct", "rr", "qty", "cost_pct", "net_rr"):
        if key in raw:
            result[key] = _number(raw[key])
    for key in ("invalidate", "notes"):
        if key in raw:
            _require(isinstance(raw[key], list), "invalid plan text list")
            result[key] = [_text(x) for x in raw[key]]
    if "target_label" in raw:
        result["target_label"] = _text(raw["target_label"])
    return result


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise DatasetError("nonfinite JSON constant: " + value)


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (ValueError, UnicodeError) as exc:
        raise DatasetError("invalid JSON: " + str(exc)) from exc


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def _read_source(path, kind, provenance):
    raw = path.read_bytes()
    provenance.append({"path": str(path.resolve()), "sha256": _hash(raw), "kind": kind})
    return _json(raw.decode("utf-8-sig"))


def _signal(raw, day, generated):
    _require(isinstance(raw, dict), "invalid confirmed row")
    code = _code(raw.get("code"))
    _limit(code)
    _require(not _name_risk(raw.get("name")), "st_or_name_risk")
    _require(raw.get("data_ok") is True, "input_data_not_ok")
    stamp = _timestamp(generated)
    _require(stamp.date().isoformat() == day, "post_hoc_generated")
    _require(len(generated) == 16 and "14:51" <= stamp.strftime("%H:%M") < "14:56", "generated_outside_entry_session")
    return {"id": code + "/" + day, "code": code, "name": _text(raw["name"]),
            "entry_date": day, "generated": generated,
            "price": _number(raw.get("price"), positive=True),
            "plan": _plan(raw.get("plan")), "data_ok": True}


def _signals(report_dir, start, end, provenance, exclusions, warnings):
    candidates, rejected = {}, set()
    files = sorted(report_dir.glob("买入清单_*.json"))
    _require(bool(files), "no confirmed report files")
    for path in files:
        report = _read_source(path, "confirmed", provenance)
        _require(isinstance(report, dict), "invalid confirmed report")
        day = _iso(report.get("date"))
        _require(path.name == "买入清单_" + day + ".json", "report filename/date mismatch")
        _require(isinstance(report.get("confirmed"), list), "confirmed must be an array")
        for index, raw in enumerate(report["confirmed"]):
            code = raw.get("code") if isinstance(raw, dict) else None
            identifier = str(code or "invalid-row-" + str(index)) + "/" + day
            if not start <= day <= end:
                exclusions.setdefault(identifier, set()).add("entry_outside_range")
                continue
            try:
                signal = _signal(raw, day, report.get("generated"))
            except DatasetError as exc:
                exclusions.setdefault(identifier, set()).add(str(exc))
                rejected.add(identifier)
                continue
            identifier = signal["id"]
            if identifier in candidates:
                if candidates[identifier] != signal:
                    rejected.add(identifier)
                    exclusions.setdefault(identifier, set()).add("conflicting_confirmed_signal")
                else:
                    warnings.append("已去重确认信号 " + identifier)
            else:
                candidates[identifier] = signal
    return {key: value for key, value in candidates.items() if key not in rejected}


def _snapshots(report_dir, codes, start, through, provenance):
    merged, issues = {}, {}
    for path in sorted(report_dir.glob("K线快照_*.json")):
        report = _read_source(path, "snapshot", provenance)
        _require(isinstance(report, dict) and isinstance(report.get("rows"), dict),
                 "invalid snapshot report")
        _require(path.name == "K线快照_" + _iso(report.get("date")) + ".json",
                 "snapshot filename/date mismatch")
        for code, row in report["rows"].items():
            if code not in codes:
                continue
            _require(isinstance(row, dict) and isinstance(row.get("bars"), dict),
                     "invalid snapshot code object")
            for day, raw_bars in row["bars"].items():
                _iso(day)
                if not start <= day <= through:
                    continue
                key = (code, day)
                buckets = merged.setdefault(key, {})
                local_ends = set()
                if "name" in row and _name_risk(row["name"]):
                    issues.setdefault(key, set()).add("snapshot_st_or_name_risk")
                if not isinstance(raw_bars, list):
                    issues.setdefault(key, set()).add("invalid_snapshot_array")
                    continue
                for raw in raw_bars:
                    try:
                        _require(isinstance(raw, list) and len(raw) == 6, "invalid_snapshot_bar")
                        end = raw[0]
                        _require(end in SESSION_ENDS, "invalid_snapshot_session_time")
                        _require(end not in local_ends, "duplicate_snapshot_session_time")
                        local_ends.add(end)
                        bar = dict(zip(("o", "h", "l", "c", "v"), raw[1:]))
                        _ohlcv(bar)
                        bar.update(end=day + " " + end, amount=None)
                        if end in buckets and buckets[end] != bar:
                            issues.setdefault(key, set()).add("conflicting_snapshot_bars")
                        else:
                            # An identical duplicate across files is harmless; never overwrite.
                            buckets.setdefault(end, bar)
                    except (DatasetError, TypeError) as exc:
                        reason = str(exc) if isinstance(exc, DatasetError) else "invalid_snapshot_bar"
                        issues.setdefault(key, set()).add(reason)
    return {key: [rows[end] for end in sorted(rows)] for key, rows in merged.items()}, issues


def _get(url):
    req = request.Request(url, headers={"User-Agent": "confirmed-replay/1.0"}, method="GET")
    with request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    _require(len(raw) <= MAX_RESPONSE_BYTES, "daily response too large")
    return raw.decode("utf-8-sig")


def _secid(code):
    return INDEX if code == INDEX else ("1." if code.startswith("6") else "0.") + code


def _parse_daily(body, source, code, through):
    if source == "sina":
        # Sina occasionally returns JS-style unquoted keys. No eval and no estimates.
        body = re.sub(r'([{,])\s*([A-Za-z_]\w*)\s*:', r'\1"\2":', body)
    payload = _json(body)
    name = ""
    if source == "eastmoney":
        _require(isinstance(payload, dict) and isinstance(payload.get("data"), dict),
                 "empty Eastmoney daily data")
        data = payload["data"]
        expected_code = "000001" if code == INDEX else code
        _require(str(data.get("code")) == expected_code, "daily symbol mismatch")
        name = data.get("name", "")
        _require(isinstance(name, str), "invalid daily name")
        lines = data.get("klines")
        _require(isinstance(lines, list) and bool(lines), "empty daily klines")
        records = []
        for line in lines:
            _require(isinstance(line, str), "invalid daily kline")
            fields = line.split(",")
            _require(len(fields) >= 10, "truncated daily kline")
            day = _iso(fields[0].strip())
            if day > through:
                continue
            try:
                records.append((day, {"o": float(fields[1]), "c": float(fields[2]),
                                      "h": float(fields[3]), "l": float(fields[4]),
                                      "v": float(fields[5]), "_reference": round(float(fields[2]) - float(fields[9]), 2)}))
            except (ValueError, OverflowError) as exc:
                raise DatasetError("invalid daily numeric field") from exc
    else:
        _require(source == "sina" and isinstance(payload, list) and bool(payload),
                 "empty Sina daily data")
        records = []
        for row in payload:
            _require(isinstance(row, dict), "invalid Sina daily row")
            stamp = row.get("day")
            _require(isinstance(stamp, str), "missing Sina daily date")
            day = _iso(stamp[:10])
            if len(stamp) > 10:
                _require(stamp[10:] in (" 15:00:00", " 00:00:00"), "invalid Sina daily timestamp")
            if day > through:
                continue
            try:
                records.append((day, {"o": float(row["open"]), "h": float(row["high"]),
                                      "l": float(row["low"]), "c": float(row["close"]),
                                      "v": float(row["volume"]) / 100.0}))
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise DatasetError("invalid Sina daily numeric field") from exc
    _require(bool(records), "no daily data through observation cutoff")
    result = {}
    previous = None
    for day, bar in sorted(records, key=lambda item: item[0]):
        _require(day not in result, "duplicate/conflicting daily date")
        _ohlcv(bar)
        reference = bar.pop("_reference", previous)
        if reference is not None:
            _number(reference, positive=True)
        bar.update(prev_close=reference, limit_pct=None if code == INDEX else _limit(code))
        result[day] = bar
        previous = bar["c"]
    return result, name


def _acquired(value):
    _require(isinstance(value, str), "missing acquisition timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DatasetError("invalid acquisition timestamp") from exc
    _require(parsed.tzinfo is not None, "acquisition timestamp must have timezone")


def _digest(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
             "invalid SHA256")


def _failures(items):
    _require(isinstance(items, list) and len(items) <= 2, "invalid acquisition failures")
    sources = []
    for item in items:
        _require(isinstance(item, dict) and set(item) == {"source", "error"}, "invalid failure fields")
        _require(item["source"] in ("eastmoney", "sina"), "invalid failure source")
        _require(isinstance(item["error"], str)
                 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", item["error"]) is not None,
                 "failure must contain only an exception type, not URLs/secrets")
        sources.append(item["source"])
    _require(sources == ["eastmoney", "sina"][:len(sources)], "invalid/repeated acquisition attempts")


def _daily_source(code, through, cache_dir, provenance, warnings):
    path = cache_dir / (code + "-" + through + ".json")
    if path.exists():
        raw = path.read_bytes()
        try:
            envelope = _json(raw.decode("utf-8-sig"))
            _require(isinstance(envelope, dict) and type(envelope.get("schema")) is int
                     and envelope["schema"] == 1, "invalid daily cache schema")
            _require(envelope.get("code") == code and envelope.get("through") == through,
                     "daily cache identity mismatch")
            _acquired(envelope.get("acquired_at"))
            _require(envelope.get("status") in ("ok", "failed"), "invalid cache status")
            expected = {"schema", "code", "through", "acquired_at", "status", "attempts"}
            _failures(envelope.get("attempts"))
            if envelope["status"] == "ok":
                expected |= {"source", "body", "source_sha256"}
                _require(set(envelope) == expected, "unknown/missing daily cache fields")
                _require(envelope.get("source") in ("eastmoney", "sina"), "invalid cache source")
                _require(len(envelope["attempts"]) == (0 if envelope["source"] == "eastmoney" else 1),
                         "source does not match acquisition attempts")
                body = envelope.get("body")
                _require(isinstance(body, str), "missing cache response body")
                _require(envelope.get("source_sha256") == _hash(body.encode("utf-8")),
                         "daily cache source hash mismatch")
                bars, name = _parse_daily(body, envelope["source"], code, through)
            else:
                _require(set(envelope) == expected and len(envelope["attempts"]) == 2,
                         "invalid failed cache manifest")
                bars, name = {}, ""
        except (DatasetError, UnicodeError):
            provenance.append({"path": str(path.resolve()), "sha256": _hash(raw),
                               "kind": "calendar" if code == INDEX else "daily_cache",
                               "code": code, "through": through, "status": "invalid"})
            warnings.append("Invalid daily cache for " + code + "; not overwritten or retried.")
            return {}, ""
    else:
        envelope = {"schema": 1, "code": code, "through": through,
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                    "status": "failed", "attempts": []}
        bars, name = {}, ""
        symbol = "sh000001" if code == INDEX else ("sh" if code.startswith("6") else "sz") + code
        urls = (("eastmoney", EM_URL.format(secid=_secid(code), end=through.replace("-", ""))),
                ("sina", SINA_URL.format(symbol=symbol)))
        for source, url in urls:
            try:
                body = _get(url)
                bars, name = _parse_daily(body, source, code, through)
            except Exception as exc:  # bounded single call per source, no retries
                # Exception messages can contain URLs/proxy credentials: store type only.
                envelope["attempts"].append({"source": source, "error": type(exc).__name__})
                continue
            envelope.update(status="ok", source=source, body=body,
                            source_sha256=_hash(body.encode("utf-8")))
            break
        _write_json(path, envelope)
        raw = path.read_bytes()
    item = {"path": str(path.resolve()), "sha256": _hash(raw),
            "kind": "calendar" if code == INDEX else "daily_cache", "code": code,
            "through": through, "status": envelope["status"],
            "acquired_at": envelope["acquired_at"]}
    if envelope["status"] == "ok":
        item.update(source=envelope["source"], source_sha256=envelope["source_sha256"],
                    adjustment="raw_unadjusted")
        if envelope["source"] == "sina":
            warnings.append("Sina public day-K fallback for " + code +
                            "; filtered through cutoff; volume converted shares to lots. "
                            "Historical name unavailable from this endpoint.")
    else:
        item["failures"] = envelope.get("attempts", [])
        warnings.append("Daily acquisition failed for " + code + "; no dates/prices inferred.")
    provenance.append(item)
    return bars, name


def _close(a, b):
    return abs(a - b) <= 0.020000001


def _session_check(rows, day, daily):
    _require(isinstance(rows, list) and len(rows) == 24, "incomplete_24bar_session:" + day)
    _require([row.get("end") for row in rows] == [day + " " + end for end in SESSION_ENDS],
             "incomplete_or_unsorted_session:" + day)
    for row in rows:
        _require(set(row) == {"end", "o", "h", "l", "c", "v", "amount"}, "invalid bar fields")
        _ohlcv(row)
        _require(row["amount"] is None, "snapshot amount must be null")
    _require(daily is not None, "missing_daily:" + day)
    volume = sum(row["v"] for row in rows)
    _require((volume > 0) == (daily["v"] > 0), "daily_snapshot_volume_mismatch:" + day)
    _require(abs(volume - daily["v"]) <= max(24, daily["v"] * 0.005), "daily_snapshot_volume_mismatch:" + day)
    aggregate = {"o": rows[0]["o"], "h": max(row["h"] for row in rows),
                 "l": min(row["l"] for row in rows), "c": rows[-1]["c"]}
    for field, value in aggregate.items():
        _require(_close(value, daily[field]), "daily_snapshot_mismatch:" + day + ":" + field)


def _required_dates(signal, calendar, through):
    day = signal["entry_date"]
    _require(day in calendar, "entry_not_in_exchange_calendar")
    index = calendar.index(day)
    following = calendar[index + 1:index + 3]
    _require(len(following) == 2 and following[-1] <= through, "insufficient_T1_T2_calendar")
    return following


def _eligible(signal, calendar, daily, bars, issues, through):
    code, day = signal["code"], signal["entry_date"]
    following = _required_dates(signal, calendar, through)
    for (symbol, bar_day), reasons in sorted(issues.items()):
        if symbol == code and (bar_day in following or "daily_date_not_in_exchange_calendar" in reasons):
            raise DatasetError(";".join(sorted(reasons)) + ":" + bar_day)
    records = daily.get(code, {})
    for required in (day, *following):
        row = records.get(required)
        _require(row is not None, "missing_daily:" + required)
        _number(row.get("prev_close"), positive=True)
    for observed, row in records.items():
        if not day <= observed <= through:
            continue
        index = calendar.index(observed)
        prior = records.get(calendar[index - 1]) if index > 0 else None
        _require(prior is not None, "missing_previous_exchange_daily:" + observed)
        _number(row.get("prev_close"), positive=True)
        _require(_close(row["prev_close"], prior["c"]), "reference_close_or_corporate_action_mismatch:" + observed)
    entry = records[day]
    _require(entry["l"] - 0.020000001 <= signal["price"] <= entry["h"] + 0.020000001,
             "signal_price_outside_raw_entry_range")
    for required in following:
        _session_check(bars.get((code, required), []), required, records.get(required))
    return following


def prepare(report_dir, output_path, start=DEFAULT_START, end=DEFAULT_END,
            through=DEFAULT_THROUGH, cache_dir=None):
    _range(start, end, through)
    report_dir, output_path = Path(report_dir).resolve(), Path(output_path).resolve()
    cache_dir = Path(cache_dir).resolve() if cache_dir is not None else output_path.parent / "daily_cache"
    _require(report_dir.is_dir(), "report_dir must exist")
    _require(output_path != report_dir and report_dir not in output_path.parents,
             "output must not modify source reports")
    _require(cache_dir != report_dir and report_dir not in cache_dir.parents,
             "cache must not modify source reports")
    _require(cache_dir != output_path and cache_dir not in output_path.parents,
             "output must be separate from cache files")
    provenance, warnings, exclusions = [], list(ASSUMPTIONS), {}
    candidates = _signals(report_dir, start, end, provenance, exclusions, warnings)
    snapshots, issues = _snapshots(report_dir, {s["code"] for s in candidates.values()},
                                   start, through, provenance)
    index_daily, _ = _daily_source(INDEX, through, cache_dir, provenance, warnings)
    calendar = sorted(index_daily)
    daily = {}
    if calendar:
        for code in sorted({s["code"] for s in candidates.values()}):
            records, name = _daily_source(code, through, cache_dir, provenance, warnings)
            daily[code] = {day: row for day, row in records.items() if day in index_daily}
            for day in records:
                if start <= day <= through and day not in index_daily:
                    issues.setdefault((code, day), set()).add("daily_date_not_in_exchange_calendar")
            source = next(item for item in provenance if item.get("code") == code and item["kind"] == "daily_cache")
            if records and source.get("source") != "eastmoney":
                for signal in candidates.values():
                    if signal["code"] == code:
                        exclusions.setdefault(signal["id"], set()).add("daily_reference_unavailable")
            if records and name and _name_risk(name):
                for signal in candidates.values():
                    if signal["code"] == code:
                        exclusions.setdefault(signal["id"], set()).add("daily_source_st_or_name_risk")
    accepted_sessions = {}
    for (code, day), rows in sorted(snapshots.items()):
        if (code, day) in issues:
            continue
        if day not in index_daily:
            issues.setdefault((code, day), set()).add("snapshot_not_in_exchange_calendar")
            continue
        if len(rows) != 24:
            warnings.append("Incomplete optional snapshot session " + code + "/" + day)
            continue
        try:
            _session_check(rows, day, daily.get(code, {}).get(day))
        except DatasetError as exc:
            issues.setdefault((code, day), set()).add(str(exc))
        else:
            accepted_sessions[(code, day)] = rows
    signals = []
    for signal in sorted(candidates.values(), key=lambda s: (s["generated"], s["entry_date"], s["code"])):
        identifier = signal["id"]
        try:
            _require(bool(calendar), "calendar_unavailable")
            _require(bool(daily.get(signal["code"])), "daily_unavailable")
            following = _eligible(signal, calendar, daily, accepted_sessions, issues, through)
        except DatasetError as exc:
            exclusions.setdefault(identifier, set()).add(str(exc))
        if identifier not in exclusions:
            signals.append(dict(signal, required_dates=following))
    for (code, day), reasons in sorted(issues.items()):
        warnings.append(f"已移除不可验证快照 {code}/{day}：{';'.join(sorted(reasons))}；仅影响该日数据，不因无关后续快照剔除入场信号。")
    codes = {signal["code"] for signal in signals}
    first_entry = {code: min(s["entry_date"] for s in signals if s["code"] == code) for code in codes}
    bars = {code: {day: rows for (symbol, day), rows in sorted(accepted_sessions.items())
                   if symbol == code and first_entry[code] <= day <= through} for code in sorted(codes)}
    dataset = {"schema": 1, "scope": "confirmed_signal_replay", "start": start, "end": end,
               "through": through, "calendar": calendar, "signals": signals, "bars": bars,
               "daily": {code: daily[code] for code in sorted(codes)}, "provenance": provenance,
               "exclusions": [{"id": key, "reason": ";".join(sorted(value))}
                              for key, value in sorted(exclusions.items())],
               "warnings": sorted(set(warnings))}
    _validate(dataset)
    _write_json(output_path, dataset)
    return dataset


def _validate(dataset):
    _require(isinstance(dataset, dict), "dataset must be an object")
    _require(set(dataset) == {"schema", "scope", "start", "end", "through", "calendar",
                             "signals", "bars", "daily", "provenance", "exclusions", "warnings"},
             "missing/unknown dataset fields")
    _require(type(dataset["schema"]) is int and dataset["schema"] == 1, "unsupported schema")
    _require(dataset["scope"] == "confirmed_signal_replay", "unsupported replay scope")
    start, end, through = dataset["start"], dataset["end"], dataset["through"]
    _range(start, end, through)
    calendar = dataset["calendar"]
    _require(isinstance(calendar, list), "calendar must be an array")
    for day in calendar:
        _iso(day)
        _require(day <= through, "future calendar date")
    _require(calendar == sorted(set(calendar)), "calendar must be sorted and unique")
    _require(isinstance(dataset["warnings"], list)
             and all(isinstance(item, str) for item in dataset["warnings"]), "invalid warnings")
    _require(all(item in dataset["warnings"] for item in ASSUMPTIONS), "missing disclosed assumptions")
    _require(isinstance(dataset["provenance"], list), "invalid provenance")
    calendar_sources, daily_sources = [], set()
    paths, identities = set(), set()
    for item in dataset["provenance"]:
        _require(isinstance(item, dict), "invalid provenance item")
        path = item.get("path")
        _require(isinstance(path, str) and bool(path) and path not in paths, "invalid/duplicate source path")
        _require(Path(path).is_absolute() or re.match(r"^[A-Za-z]:[/\\]", path), "source path must be absolute")
        paths.add(path)
        _digest(item.get("sha256"))
        kind = item.get("kind")
        _require(kind in ("confirmed", "snapshot", "daily_cache", "calendar"), "unknown provenance kind")
        expected = {"path", "sha256", "kind"}
        if kind in ("daily_cache", "calendar"):
            expected |= {"code", "through", "status"}
            _require(item.get("through") == through, "provenance cutoff mismatch")
            _require(item.get("status") in ("ok", "failed", "invalid"), "invalid source status")
            identity = (kind, item.get("code"))
            _require(identity not in identities, "duplicate daily source identity")
            identities.add(identity)
            if kind == "calendar":
                _require(item.get("code") == INDEX, "invalid calendar source")
                calendar_sources.append(item)
            else:
                _code(item.get("code"))
            if item["status"] != "invalid":
                expected.add("acquired_at")
                _acquired(item.get("acquired_at"))
            if item["status"] == "ok":
                expected |= {"source", "source_sha256", "adjustment"}
                _require(item.get("source") in ("eastmoney", "sina")
                         and item.get("adjustment") == "raw_unadjusted", "invalid daily source")
                _digest(item.get("source_sha256"))
                if kind == "calendar" or item["source"] == "eastmoney":
                    daily_sources.add(item["code"])
            elif item["status"] == "failed":
                expected.add("failures")
                _failures(item.get("failures"))
                _require(len(item["failures"]) == 2, "incomplete source failure manifest")
        _require(set(item) == expected, "unknown/missing provenance fields")
    _require(len(calendar_sources) == 1, "require one index-calendar source")
    _require(bool(calendar) == (calendar_sources[0]["status"] == "ok"), "calendar/source status mismatch")
    _require(isinstance(dataset["exclusions"], list), "invalid exclusions")
    excluded = set()
    for item in dataset["exclusions"]:
        _require(isinstance(item, dict) and set(item) == {"id", "reason"}, "invalid exclusion")
        _require(isinstance(item["id"], str) and item["id"] and item["id"] not in excluded,
                 "invalid/duplicate exclusion id")
        _require(isinstance(item["reason"], str) and bool(item["reason"]), "missing exclusion reason")
        excluded.add(item["id"])
    signals, seen = dataset["signals"], set()
    _require(isinstance(signals, list), "signals must be an array")
    for signal in signals:
        _require(isinstance(signal, dict) and set(signal) == {"id", "code", "name", "entry_date",
                 "generated", "price", "plan", "data_ok", "required_dates"}, "invalid signal fields")
        day = _iso(signal["entry_date"])
        _require(start <= day <= end, "entry_outside_range")
        base = _signal(signal, day, signal["generated"])
        _require(base["id"] == signal["id"] and base["plan"] == signal["plan"], "invalid signal/plan")
        _require(signal["id"] not in seen and signal["id"] not in excluded, "duplicate/excluded eligible signal")
        seen.add(signal["id"])
    _require(signals == sorted(signals, key=lambda s: (s["generated"], s["entry_date"], s["code"])),
             "signals must be stably ordered")
    codes = {signal["code"] for signal in signals}
    _require(isinstance(dataset["daily"], dict) and isinstance(dataset["bars"], dict), "invalid bar maps")
    _require(set(dataset["daily"]) == codes == set(dataset["bars"]), "orphan/missing code data")
    _require(codes <= daily_sources, "missing successful raw daily provenance")
    if signals:
        _require(any(item["kind"] == "confirmed" for item in dataset["provenance"])
                 and any(item["kind"] == "snapshot" for item in dataset["provenance"]), "missing input provenance")
    sessions = {}
    for code in sorted(codes):
        records = dataset["daily"][code]
        _require(isinstance(records, dict) and bool(records), "missing daily records")
        previous = None
        for day, row in sorted(records.items()):
            _iso(day)
            _require(day in calendar and day <= through, "daily date outside exchange calendar/cutoff")
            _require(isinstance(row, dict) and set(row) == {"o", "h", "l", "c", "v", "prev_close", "limit_pct"},
                     "invalid daily fields")
            _ohlcv(row)
            _require(type(row["limit_pct"]) in (int, float) and row["limit_pct"] == _limit(code),
                     "invalid limit assumption")
            if row["prev_close"] is not None:
                _number(row["prev_close"], positive=True)
            if previous is not None and day >= start:
                _require(row["prev_close"] is not None and _close(row["prev_close"], previous),
                         "previous close does not match preceding raw daily close")
            previous = row["c"]
        dates = dataset["bars"][code]
        _require(isinstance(dates, dict), "invalid code-date bar map")
        first = min(s["entry_date"] for s in signals if s["code"] == code)
        for day, rows in dates.items():
            _iso(day)
            _require(first <= day <= through and day in calendar, "snapshot date outside replay/calendar")
            _session_check(rows, day, records.get(day))
            sessions[(code, day)] = rows
    for signal in signals:
        following = _eligible(signal, calendar, dataset["daily"], sessions, {}, through)
        _require(signal["required_dates"] == following, "T1/T2 dates differ from actual exchange calendar")
    return dataset


def load_dataset(path):
    try:
        dataset = _json(Path(path).read_text(encoding="utf-8-sig"))
        return _validate(dataset)
    except DatasetError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DatasetError("invalid dataset structure") from exc
