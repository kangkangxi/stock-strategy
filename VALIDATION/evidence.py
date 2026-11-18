import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CST = timezone(timedelta(hours=8))
_DRIFT = "冻结验证版本发生变化，禁止新增样本"
_REDACTED = "[REDACTED]"
_HOSTS = ("eastmoney.com", "gtimg.cn", "gtimg.com", "sina.com.cn", "sina.cn", "sinajs.cn")
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_TYPE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_TARGETS = {
    "complete_scan_pm_trading_days": 60,
    "fully_closed_trades": 100,
    "purpose": "review_trigger_only_not_profit_proof",
}


def _sensitive(key):
    key = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return (any(word in key for word in (
        "secret", "password", "passwd", "token", "webhook", "apikey",
        "accesskey", "authorization", "credential", "cookie", "header", "session",
        "userid", "username", "openid", "unionid", "identity", "email", "phone",
    )) or key in {
        "key", "auth", "ut", "user", "uid", "mobile", "appid", "appkey",
        "clientid", "account", "accountid", "pushconfig", "signature", "sign",
        "sid", "jwt", "ticket", "bearer", "csrf", "login",
    })


def _public_url(value):
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() not in ("http", "https") or not any(
            host == domain or host.endswith("." + domain) for domain in _HOSTS
        ):
            return _REDACTED
        port = parts.port
        netloc = host + (":" + str(port) if port is not None else "")
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                           if not _sensitive(k)])
        return urlunsplit((parts.scheme.lower(), netloc, parts.path, query, ""))
    except (ValueError, UnicodeError):
        return _REDACTED


def _text(value, key=""):
    if "url" in str(key).lower():
        return _public_url(value)
    return _URL.sub(lambda match: _public_url(match.group()), value)


def _stamp(value=None):
    if value is None:
        value = datetime.now(CST)
    elif isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime or ISO timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=CST)
    return value.astimezone(CST).isoformat(timespec="microseconds")


def _check_stamp(value):
    if not isinstance(value, str) or datetime.fromisoformat(value).utcoffset() != timedelta(hours=8):
        raise ValueError("invalid CST timestamp")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


def _load(value):
    return json.loads(value, object_pairs_hook=_pairs, parse_constant=_invalid_constant)


def _write_once(path, payload):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _local(path):
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("external evidence path")


def _add_error(errors, name):
    if name not in errors:
        errors.append(name)


class Recorder:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.objects = self.directory / "objects"
        _local(self.objects)
        self.objects.mkdir(exist_ok=True)
        self.stream_path = self.directory / ("stream-" + str(uuid.uuid4()) + ".jsonl")
        with self.stream_path.open("xb"):
            pass
        self.errors = []
        self._lock = threading.RLock()

    @classmethod
    def reader(cls, directory):
        result = cls.__new__(cls)
        result.directory = Path(directory).resolve()
        result.objects = result.directory / "objects"
        streams = sorted(result.directory.glob("stream-*.jsonl"))
        if not streams:
            raise FileNotFoundError("missing evidence streams")
        result.stream_path = streams[0]
        result.errors = []
        result._lock = threading.RLock()
        return result

    def _blob_check(self, digest, size):
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or type(size) is not int or size < 0):
            raise ValueError("invalid blob reference")
        _local(self.objects)
        path = self.objects / digest
        _local(path)
        raw = path.read_bytes()
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("blob hash mismatch")

    def _encode(self, value, key=""):
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            digest = hashlib.sha256(raw).hexdigest()
            _local(self.objects)
            path = self.objects / digest
            _local(path)
            _write_once(path, raw)
            self._blob_check(digest, len(raw))
            return {"blob_sha256": digest, "bytes": len(raw)}
        if isinstance(value, Mapping):
            result = {}
            for name, item in value.items():
                if not isinstance(name, str):
                    raise TypeError("event keys must be strings")
                if not _sensitive(name):
                    result[name] = self._encode(item, name)
            return result
        if isinstance(value, (list, tuple)):
            return [self._encode(item, key) for item in value]
        if isinstance(value, str):
            return _text(value, key)
        if isinstance(value, datetime):
            return _stamp(value)
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if value is None or isinstance(value, (bool, int)):
            return value
        raise TypeError("unsupported evidence value")

    def _append(self, payload):
        if (self.directory / "seal.json").exists():
            raise RuntimeError("sealed evidence cannot be appended")
        _local(self.stream_path)
        descriptor = os.open(self.stream_path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "ab") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    def event(self, kind, data):
        with self._lock:
            try:
                if not isinstance(kind, str):
                    raise TypeError("event kind must be a string")
                observed_at = _stamp()
                event = {"observed_at": observed_at, "kind": kind, "data": self._encode(data)}
                self._append(_json(event) + b"\n")
            except Exception as exc:
                _add_error(self.errors, type(exc).__name__)

    def _references(self, value):
        if isinstance(value, dict):
            if "blob_sha256" in value:
                if set(value) != {"blob_sha256", "bytes"}:
                    raise ValueError("invalid blob reference")
                self._blob_check(value["blob_sha256"], value["bytes"])
            else:
                for item in value.values():
                    self._references(item)
        elif isinstance(value, list):
            for item in value:
                self._references(item)

    def _read_stream(self, path):
        _local(path)
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise ValueError("truncated evidence stream")
        events = []
        for line in raw.splitlines():
            event = _load(line)
            if not isinstance(event, dict) or set(event) != {"observed_at", "kind", "data"}:
                raise ValueError("invalid evidence event")
            _check_stamp(event["observed_at"])
            if not isinstance(event["kind"], str):
                raise ValueError("invalid event kind")
            self._references(event["data"])
            events.append(event)
        return events

    def seal(self):
        with self._lock:
            try:
                manifest = {"streams": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                         for path in sorted(self.directory.glob("stream-*.jsonl"))},
                            "errors": self.health()["errors"]}
                payload = _json(manifest) + b"\n"
                path = self.directory / "seal.json"
                if not _write_once(path, payload) and path.read_bytes() != payload:
                    raise ValueError("evidence seal mismatch")
            except Exception as exc:
                _add_error(self.errors, type(exc).__name__)

    def health(self):
        with self._lock:
            errors = list(self.errors)
            try:
                _local(self.directory)
                _local(self.objects)
                if not self.directory.is_dir() or not self.objects.is_dir() or not self.stream_path.is_file():
                    raise FileNotFoundError("missing evidence directory or stream")
                streams = sorted(self.directory.glob("stream-*.jsonl"))
                seal = self.directory / "seal.json"
                if seal.exists():
                    _local(seal)
                    manifest = _load(seal.read_bytes())
                    actual = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in streams}
                    if actual != manifest["streams"]:
                        raise ValueError("sealed stream mismatch")
                    for name in manifest["errors"]:
                        _add_error(errors, name)
            except Exception as exc:
                _add_error(errors, type(exc).__name__)
                streams = []
            for path in streams:
                try:
                    pending = 0
                    for event in self._read_stream(path):
                        if event["kind"] == "worker_start":
                            pending += 1
                        elif event["kind"] == "run_end":
                            for name in event["data"].get("evidence_errors", []):
                                if not isinstance(name, str) or not _TYPE.fullmatch(name):
                                    raise ValueError("invalid recorder error type")
                                _add_error(errors, name)
                        elif event["kind"] == "worker_end":
                            data = event["data"]
                            if (not isinstance(data, dict) or not isinstance(data.get("errors"), list)
                                    or type(data.get("returncode")) is not int):
                                raise ValueError("invalid worker result")
                            for name in data["errors"]:
                                if not isinstance(name, str) or not _TYPE.fullmatch(name):
                                    raise ValueError("invalid worker error type")
                                _add_error(errors, name)
                            if data["returncode"] != 0:
                                _add_error(errors, "RuntimeError")
                            pending = max(0, pending - 1)
                    if pending:
                        raise RuntimeError("unfinished worker")
                except Exception as exc:
                    _add_error(errors, type(exc).__name__)
            return {"status": "incomplete" if errors else "ok", "errors": errors}

    def summary(self):
        with self._lock:
            events = [event for path in sorted(self.directory.glob("stream-*.jsonl"))
                      for event in self._read_stream(path)]
            return sorted(events, key=lambda event: datetime.fromisoformat(event["observed_at"]))


def selection_chain(scan_directory, pm_directory, signal=None):
    scan = Recorder.reader(scan_directory)
    pm = Recorder.reader(pm_directory)
    if not (scan.directory / "seal.json").is_file() or scan.health()["status"] != "ok" or pm.health()["status"] != "ok":
        raise ValueError("selection evidence incomplete")
    scan_events, pm_events = scan.summary(), pm.summary()

    def one(events, kind):
        values = [event["data"] for event in events if event["kind"] == kind]
        if len(values) != 1:
            raise ValueError("selection stage missing or duplicated")
        return values[0]

    candidate_ref = one(scan_events, "candidates_file")["raw"]
    signal_ref = one(pm_events, "signals_file")["raw"]
    candidates = _load((scan.objects / candidate_ref["blob_sha256"]).read_bytes())
    saved_signal = _load((pm.objects / signal_ref["blob_sha256"]).read_bytes())
    confirmed = one(pm_events, "pm_final")
    if (candidates != one(pm_events, "pm_input")["candidates"]
            or saved_signal["confirmed"] != confirmed["confirmed"]
            or saved_signal["rejected"] != confirmed["rejected"]
            or (signal is not None and signal != saved_signal)):
        raise ValueError("selection input chain mismatch")
    return saved_signal


def _freeze(value):
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or _sensitive(key):
                raise ValueError("sensitive or invalid campaign key")
            result[key] = _freeze(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_freeze(item) for item in value]
    if isinstance(value, str):
        if _text(value) != value:
            raise ValueError("sensitive campaign value")
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite campaign value")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError("unsupported campaign value")


def _code_hashes(code_paths):
    if isinstance(code_paths, (str, bytes, os.PathLike)):
        raise TypeError("code_paths must contain individual code files")
    entries = code_paths.items() if isinstance(code_paths, Mapping) else (
        (Path(path).name, path) for path in code_paths
    )
    hashes = {}
    for name, filename in entries:
        path = Path(filename)
        if (not isinstance(name, str) or not name or name in hashes
                or "push_config" in path.name.lower() or "push_config" in name.lower()):
            raise ValueError("invalid code file name")
        if not path.is_file():
            raise ValueError("code_paths requires individual existing files")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not hashes:
        raise ValueError("code_paths must not be empty")
    return hashes


def _campaign_hash(manifest):
    return hashlib.sha256(_json({k: v for k, v in manifest.items() if k != "campaign_id"})).hexdigest()


def verify_campaign(root, version, code_paths):
    path = Path(root).resolve() / "campaign.json"
    if not path.exists() and not path.is_symlink():
        return None
    try:
        _local(path)
        manifest = _load(path.read_bytes())
        if (not isinstance(manifest, dict) or set(manifest) != {
            "campaign_id", "version", "spec", "code_hashes", "started_at", "evaluation_targets"
        } or manifest["campaign_id"] != _campaign_hash(manifest)
                or manifest["version"] != version
                or manifest["code_hashes"] != _code_hashes(code_paths)
                or manifest["evaluation_targets"] != _TARGETS):
            raise ValueError("campaign mismatch")
        _check_stamp(manifest["started_at"])
        _freeze(manifest["spec"])
        return manifest
    except Exception:
        raise RuntimeError(_DRIFT) from None


def start_campaign(root, version, spec, now, code_paths):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if isinstance(code_paths, Mapping):
        code_paths = dict(code_paths)
    elif not isinstance(code_paths, (str, bytes, os.PathLike)):
        code_paths = list(code_paths)
    existing = verify_campaign(root, version, code_paths)
    if existing is not None:
        return existing
    if not isinstance(version, str) or not version:
        raise ValueError("version must be a nonempty string")
    manifest = {
        "version": _freeze(version),
        "spec": _freeze(spec),
        "code_hashes": _code_hashes(code_paths),
        "started_at": _stamp(now),
        "evaluation_targets": dict(_TARGETS),
    }
    manifest["campaign_id"] = _campaign_hash(manifest)
    if not _write_once(root / "campaign.json", _json(manifest) + b"\n"):
        return verify_campaign(root, version, code_paths)
    return manifest
