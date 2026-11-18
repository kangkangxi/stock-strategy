import argparse
import contextlib
import importlib.util
import json
import math
import msvcrt
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from research import Research

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from VALIDATION.evidence import Recorder, selection_chain, start_campaign, verify_campaign

HOME = Path(__file__).resolve().parent
STRATEGY = HOME.parent
CST = timezone(timedelta(hours=8))
SHORT = "短线策略"
TASK_NAME = "Qoder.StockTrading.GO_BOT"


@dataclass(frozen=True)
class Job:
    name: str
    at: str
    system: str
    mode: str
    period: str = "daily"


JOBS = (
    Job("auction", "09:25", SHORT, "auction"),
    *(Job("exit_" + at.replace(":", ""), at, SHORT, "exit") for at in
      ("09:30", "09:35", "10:00", "10:30", "11:00", "11:30", "13:00", "13:05", "13:30", "14:00", "14:30")),
    Job("scan", "14:31", SHORT, "scan"),
    Job("paper_settle", "14:51", SHORT, "paper_settle"),
    Job("pm", "14:51", SHORT, "pm"),
    Job("week", "15:05", SHORT, "week", "week"),
    Job("month", "15:05", SHORT, "month", "month"),
    Job("ksnap", "15:10", SHORT, "ksnap"),
)
STATUS = Job("status", "00:00", SHORT, "status")


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def due(now, completed):
    result = []
    for job in JOBS:
        if job.period == "month":
            if now.day != 1:
                continue
        elif now.weekday() >= 5:
            continue
        if job.period == "week" and now.weekday() != 4:
            continue
        hour, minute = map(int, job.at.split(":"))
        lateness = (now - now.replace(hour=hour, minute=minute, second=0, microsecond=0)).total_seconds()
        key = now.strftime("%Y-%m-%d") + "/" + job.name
        if 0 <= lateness < 120 and key not in completed:
            result.append(job)
    return result


# The engine marks each item's status with its leading symbol; "❌0" inside a summary is only a count.
def failed(validations):
    return any(not isinstance(item, str) or item.lstrip().startswith("\u274c") for item in validations)


def fresh(env, now, max_age=300):
    ts = env.get("ts")
    sh = next((item for item in env.get("indices", []) if item.get("code") == "000001"), {})
    price = sh.get("price")
    if (not isinstance(ts, (int, float)) or not ts or not math.isfinite(ts)
            or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0):
        return False
    try:
        quoted = datetime.fromtimestamp(ts, CST)
    except (ValueError, OverflowError, OSError):
        return False
    age = (now - quoted).total_seconds()
    return quoted.date() == now.date() and -30 <= age <= max_age


@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class Bot:
    def __init__(self, home=HOME, source=STRATEGY):
        self.home = Path(home)
        self.source = Path(source)
        self.runtime = self.home / "runtime"
        self.state_file = self.home / "state.json"
        self.validation_root = self.home / "validation"
        self._evidence = None

    def record_evidence(self, kind, data):
        if self._evidence is not None:
            try:
                self._evidence.event(kind, data)
            except Exception as error:
                name = type(error).__name__
                if name not in self._evidence.errors:
                    self._evidence.errors.append(name)

    def validation_paths(self):
        module = HOME.parent / "VALIDATION"
        return {"engine": self.runtime / SHORT / "scripts" / "tail_momentum.py",
                "bot": Path(__file__), "research": Path(__file__).with_name("research.py"),
                **{name: module / (name + ".py") for name in ("evidence", "run", "assess")}}

    def validation_start(self, now):
        with self.research() as store:
            version = store.version(now)
            return start_campaign(self.validation_root, version, store.spec(version), now, self.validation_paths())

    def initialize(self):
        if self.runtime.exists():
            raise RuntimeError("隔离目录已存在，不覆盖已有运行数据。")
        config = self.strategy_config()
        self.runtime.mkdir(parents=True)
        shutil.copytree(self.source, self.runtime / SHORT,
                        ignore=shutil.ignore_patterns(self.home.name, "VALIDATION", ".git", "__pycache__", "*.bak", "*.bak-*"))
        self.write_runtime_config(config)
        positions_file = self.runtime / SHORT / "positions.json"
        if positions_file.is_file():
            positions = read(positions_file, {})
            positions["positions"] = []
            positions["advice_state"] = {}
            dump(positions_file, positions)
        with self.research() as store:
            store.bootstrap(self.source, datetime.now(CST))
        dump(self.state_file, {"jobs": {}})
        print(f"隔离副本已建立：{self.runtime}；独立研究账本，不登记原引擎持仓，不下单。")

    def strategy_config(self):
        config = next((path for path in (self.source / "config.json", self.source / "config.example.json")
                       if path.is_file()), None)
        if not (self.source / "scripts" / "tail_momentum.py").is_file() or config is None:
            raise RuntimeError(f"缺少策略脚本或配置：{self.source}")
        return config

    def write_runtime_config(self, config):
        cfg = read(config, {})
        cfg["push"] = False
        cfg["paper_track"] = False
        dump(self.runtime / SHORT / "config.json", cfg)

    def update(self, now):
        if not self.runtime.is_dir() or not self.state_file.is_file():
            raise RuntimeError("尚未完成初始化；先运行 --init。")
        jobs = read(self.state_file, {"jobs": {}})["jobs"]
        if jobs.get(f"{now:%Y-%m-%d}/scan", {}).get("status") == "done" and f"{now:%Y-%m-%d}/pm" not in jobs:
            raise RuntimeError("今日初筛已完成、尾盘确认尚未执行；确认后再更新，避免混用策略版本。")
        config = self.strategy_config()
        with self.research() as store:
            before = store.version(now)
            store.archive("runtime/" + SHORT, self.runtime / SHORT, now)
            shutil.copytree(self.source, self.runtime / SHORT, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
                self.home.name, "VALIDATION", ".git", "__pycache__", "*.bak", "*.bak-*",
                "positions.json*", "报告", "日志", "config.json", "最新推送.md", "*.log"))
            self.write_runtime_config(config)
            store.archive("runtime/" + SHORT, self.runtime / SHORT, now)
            return {"previous_version": before, "version": store.version(now)}

    @contextlib.contextmanager
    def research(self):
        store = Research(self.home)
        try:
            yield store
        finally:
            store.close()

    def clock(self):
        return datetime.now(CST)

    def command(self, job, now):
        if job.mode in ("auction", "exit", "paper_settle", "week", "month"):
            return [sys.executable, "-X", "utf8", str(self.home / "bot.py"), "--run", job.name]
        return [sys.executable, "-X", "utf8", str(self.runtime / SHORT / "scripts" / "tail_momentum.py"),
                "--mode", job.mode, "--no-push", "--no-hold", "--force-now", now.strftime("%Y-%m-%d %H:%M")]

    def event(self, **fields):
        path = self.home / "logs" / "bot.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"time": datetime.now(CST).isoformat(), **fields}, ensure_ascii=False) + "\n")

    def market_module(self):
        spec = importlib.util.spec_from_file_location("go_bot_market", self.runtime / SHORT / "scripts" / "tail_momentum.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if self._evidence is not None:
            module.audit_event = self.record_evidence
        return module

    def market_env(self):
        result = self.market_module().em_market_env()
        if self._evidence is not None:
            self.record_evidence("market_environment", result)
        return result

    def quotes(self, codes):
        result = self.market_module().tencent_quotes(codes) if codes else {}
        if self._evidence is not None:
            self.record_evidence("execution_quotes", {"requested": codes, "quotes": result})
        return result

    def exit_indicators(self, codes, now):
        if not codes or now.strftime("%H%M") < "0940":
            return {}
        worker = (
            "import contextlib,json,sys; from datetime import datetime; from bot import Bot,Recorder; "
            "request=json.load(sys.stdin); "
            "\nwith contextlib.redirect_stdout(sys.stderr):\n"
            " bot=Bot(request['home']); errors=[]\n"
            " if request.get('evidence'):\n"
            "  try:\n"
            "   bot._evidence=Recorder(request['evidence'])\n"
            "   bot.record_evidence('worker_start',{'job':'indicators'})\n"
            "  except Exception as error: errors.append(type(error).__name__)\n"
            " result=bot.collect_exit_indicators(request['codes'],datetime.fromisoformat(request['now']))\n"
            " if bot._evidence is not None:\n"
            "  bot.record_evidence('worker_end',{'returncode':0,'errors':bot._evidence.errors})\n"
            "  errors+=bot._evidence.errors\n"
            "print(json.dumps({'indicators':result,'evidence_errors':errors},ensure_ascii=False,allow_nan=False))"
        )
        try:
            completed = subprocess.run([sys.executable, "-X", "utf8", "-c", worker],
                                       input=json.dumps({"home": str(self.home), "codes": codes, "now": now.isoformat(),
                                                         "evidence": str(self._evidence.directory) if self._evidence is not None else None}),
                                       cwd=str(Path(__file__).resolve().parent), capture_output=True, text=True,
                                       encoding="utf-8", timeout=35, check=True)
            response = json.loads(completed.stdout)
            if self._evidence is not None:
                for name in response["evidence_errors"]:
                    if name not in self._evidence.errors:
                        self._evidence.errors.append(name)
            return response["indicators"]
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError, OSError) as error:
            if self._evidence is not None:
                self.record_evidence("indicator_unavailable", {"codes": codes, "error_type": type(error).__name__})
            return {code: {"data_missing": True, "err": "技术指标超时或不可用，仅按有效价格规则判断"} for code in codes}

    def collect_exit_indicators(self, codes, now):
        engine = self.market_module()
        result = {}
        for code in codes:
            try:
                bars, source, status, _, notes = engine.fetch_5m(code, now, now.date().isoformat(), lmt=200)
                if isinstance(status, str) and status.lstrip().startswith("\u274c"):
                    result[code] = {"data_missing": True, "err": "5分钟K线校验失败，禁用技术指标"}
                    continue
                closed = engine.build_10m(bars, now)
                today = [bar for bar in closed if bar["t"][:10] == now.date().isoformat()]
                if not today or now.replace(tzinfo=None) - datetime.strptime(today[-1]["t"], "%Y-%m-%d %H:%M") > timedelta(minutes=15):
                    result[code] = {"data_missing": True, "err": "当日已完成10分钟K线缺失或过期"}
                    continue
                amount = sum(bar.get("amt", 0) for bar in today)
                volume = sum(bar["v"] for bar in today)
                cfg = read(self.runtime / SHORT / "config.json", {})
                estimated = any(bar.get("amt_est") for bar in bars)
                result[code] = {"ma5_last": engine.ma([bar["c"] for bar in closed], int(cfg.get("ma10m_period", 5))),
                                "vwap": amount / (volume * 100) if amount and volume and not estimated else None,
                                "source": source, "notes": notes, "last_bar": today[-1]["t"], "data_missing": False}
                if estimated:
                    result[code]["err"] = "成交额为估算值，禁用VWAP规则"
            except (ValueError, TypeError, KeyError, OSError):
                result[code] = {"data_missing": True, "err": "技术指标读取失败，仅评估有效价格规则"}
        if self._evidence is not None:
            for name in getattr(engine, "AUDIT_ERRORS", []):
                if name not in self._evidence.errors:
                    self._evidence.errors.append(name)
        return result

    def paper_report(self, job, now, result):
        actions = {"hold": "持有", "sell": "全部卖出", "reduce": "减仓", "defer": "无法模拟成交", "observe": "仅观察"}
        lines = [f"独立模拟持仓判断 · {now:%Y-%m-%d %H:%M} · {job.name}",
                 "非实盘，不向券商下单；模拟成交使用新鲜报价加不利滑点，不保证真实成交。", ""]
        for item in result.get("decisions", []):
            lines += [f"{item['code']} {item.get('name', '')} | {actions.get(item['action'], item['action'])} | 现价 {item.get('price', '缺失')}",
                      f"模拟卖出 {item.get('sell_shares', 0)} 股；剩余 {item.get('remaining_shares', item.get('shares_before', 0))} 股；情景 {item.get('scenario', '不可判定')}",
                      "；".join(item.get("reasons", [])), f"入场版本 {item.get('version', '')[:12]}", ""]
        lines += [f"本次模拟平仓 {result.get('closed', 0)} 笔，减仓 {result.get('reduced', 0)} 笔，延期 {result.get('deferred', 0)} 笔。",
                  "当前独立模拟持仓（按入场版本保留，不与空的原引擎账本混淆）："]
        held = []
        with self.research() as store:
            for (version,) in store.db.execute("SELECT id FROM versions"):
                for position in store.account(version)["positions"].values():
                    held.append(f"{position['code']} {position['name']} | {position['remaining_shares']} 股 | 建仓 {position['entry_date']} | 版本 {version[:12]}")
        lines.extend(held or ["无模拟持仓"])
        path = self.home / "research" / "reports" / f"持仓判断_{now:%Y-%m-%d}_{job.name}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    def archive_runtime(self, job, now):
        if job.mode == "status":
            return
        with self.research() as store:
            store.archive("runtime/" + job.system, self.runtime / job.system, now)

    def guard(self, job, now, state):
        if job.mode == "status":
            return
        if datetime.now().astimezone().utcoffset() != timedelta(hours=8):
            raise RuntimeError("原引擎使用本机时间；Windows 时区需为 UTC+8，机器人不会修改系统时区。")
        if (job.system == SHORT and job.mode not in ("paper_settle", "week", "month")
                and read(self.runtime / SHORT / "positions.json", {}).get("positions")):
            raise RuntimeError("仅报告模式禁止处理非空持仓；请人工核验隔离账本。")
        if job.mode in ("week", "month", "auction", "exit"):
            return
        if job.mode in ("scan", "pm"):
            with self.research() as store:
                campaign = verify_campaign(self.validation_root, store.version(now), self.validation_paths())
            if campaign is not None and now < datetime.fromisoformat(campaign["started_at"]):
                raise RuntimeError("不能在冻结验证开始之前登记样本。")
        if not fresh(self.market_env(), now, max_age=900 if job.mode == "ksnap" else 300):
            raise RuntimeError("无当日完整、新鲜指数行情；跳过任务，不以缺失时间戳放行。")
        if job.mode == "pm":
            scan = state["jobs"].get(now.strftime("%Y-%m-%d") + "/scan", {})
            if scan.get("status") != "done":
                raise RuntimeError("当日机器人初筛未成功，禁止使用历史候选进行尾盘确认。")
            with self.research() as store:
                if scan.get("research_version") != store.version(now):
                    raise RuntimeError("初筛后策略或费用版本发生变化，禁止混用候选。")
            candidates = read(self.runtime / SHORT / "报告" / f"候选_{now:%Y-%m-%d}.json", {})
            if candidates.get("date") != now.strftime("%Y-%m-%d") or not candidates.get("validations"):
                raise RuntimeError("缺少当日候选或校验结果。")
            if failed(candidates["validations"]):
                raise RuntimeError("候选包含校验异常，禁止尾盘确认。")

    def report_path(self, job, now):
        result = read(self.state_file, {"jobs": {}})["jobs"].get(f"{now:%Y-%m-%d}/{job.name}", {})
        if result.get("research_report"):
            return Path(result["research_report"])
        if job.mode in ("auction", "exit", "paper_settle"):
            return None
        names = {"scan": f"尾盘选股_{now:%Y-%m-%d}.md", "pm": f"买入清单_{now:%Y-%m-%d}.md",
                 "week": "最新周总结.md", "month": "最新月总结.md"}
        name = names.get(job.mode)
        return self.runtime / SHORT / "报告" / name if name else None

    def notify(self, job, now, started_ns):
        result = read(self.state_file, {"jobs": {}})["jobs"].get(f"{now:%Y-%m-%d}/{job.name}", {})
        if result.get("report_error"):
            return {"status": "failed", "error": "模拟判断已记录，但本次报告生成失败；不发送旧报告"}
        report = self.report_path(job, now)
        if report is None:
            return {"status": "not_applicable"}
        cfg = read(self.home / "push_config.json", {})
        if not cfg.get("enabled", False):
            return {"status": "disabled"}
        webhook = cfg.get("webhook", "")
        if not isinstance(webhook, str) or not re.fullmatch(
                r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", webhook):
            return {"status": "failed", "error": "飞书 webhook 配置无效"}
        if not report.is_file() or report.stat().st_mtime_ns < started_ns:
            return {"status": "failed", "error": "未生成本次报告，禁止推送旧文件"}
        content = report.read_text(encoding="utf-8").strip()
        if not content:
            return {"status": "failed", "error": "报告为空，未发送"}
        title = f"股票研究 | {job.name} | {now:%Y-%m-%d %H:%M}\n"
        title += ("独立模拟统计，非实盘；不同版本和日期不直接推断优化效果。\n\n" if job.mode in ("week", "month")
                  else "隔离研究报告，非实盘；原策略仍有已知缺陷，不据此自动调参。\n\n")
        text = (title + content).encode("utf-8")
        suffix = "\n\n[内容已截断，完整报告保存在本机隔离目录]"
        if len(text) > 18000:
            text = text[:18000 - len(suffix.encode("utf-8"))].decode("utf-8", errors="ignore") + suffix
        else:
            text = text.decode("utf-8")
        body = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
        while len(body) > 20000:
            text = text.removesuffix(suffix)[:-1000] + suffix
            body = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read(65536).decode("utf-8"))
            code = result.get("code", result.get("StatusCode"))
            if type(code) is not int or code != 0:
                return {"status": "failed", "error": "飞书返回非成功业务码"}
        except urllib.error.HTTPError as error:
            return {"status": "failed", "error": f"飞书 HTTP {error.code}；不自动重发"}
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"status": "unknown", "error": "推送响应未确认；不自动重发"}
        except (ValueError, AttributeError):
            return {"status": "unknown", "error": "推送响应无法解析；不自动重发"}
        return {"status": "sent", "at": datetime.now(CST).isoformat()}

    def validation_health(self, job):
        if self._evidence is None:
            health = {"status": "incomplete", "errors": ["RecorderUnavailable"]}
        else:
            health = self._evidence.health()
            if job.mode in ("scan", "pm"):
                events = self._evidence.summary() if health["status"] == "ok" else []
                required = {job.mode + "_final", "config", "http", "worker_end"}
                if not required.issubset({event["kind"] for event in events}):
                    health = {"status": "incomplete", "errors": ["SelectionTraceMissing"]}
            health["path"] = str(self._evidence.directory)
        if job.mode in ("scan", "pm") and (self.validation_root / "campaign.json").exists() and health["status"] != "ok":
            raise RuntimeError("前向验证输入留存不完整，禁止新增模拟样本。")
        return health

    def execute(self, job, now, state):
        key = now.strftime("%Y-%m-%d") + "/" + job.name
        if key in state["jobs"]:
            return
        state["jobs"][key] = {"status": "started", "at": now.isoformat()}
        dump(self.state_file, state)
        started_ns = time.time_ns()
        version = None
        result = {"status": "failed", "at": now.isoformat()}
        try:
            if job.mode not in ("status", "week", "month"):
                try:
                    directory = self.validation_root / "runs" / f"{now:%Y-%m-%d}" / f"{job.name}_{started_ns}"
                    self._evidence = Recorder(directory)
                    self.record_evidence("run_start", {"job": job.name, "scheduled_at": now.isoformat()})
                except Exception:
                    self._evidence = None
                    if job.mode in ("scan", "pm") and (self.validation_root / "campaign.json").exists():
                        raise RuntimeError("验证证据目录不可写，禁止新增模拟样本。")
            self.archive_runtime(job, now)
            self.guard(job, now, state)
            log_file = self.home / "logs" / f"{now:%Y-%m-%d}_{job.name}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            result = {"status": "done", "at": now.isoformat(), "log": str(log_file)}
            if job.system == SHORT and job.mode != "status":
                with self.research() as store:
                    version = store.version(now)
                result["research_version"] = version
            if job.mode in ("auction", "exit", "paper_settle"):
                with self.research() as store:
                    codes = store.open_codes()
                indicators = self.exit_indicators(codes, now) if job.mode == "exit" else {}
                quotes = self.quotes(codes)
                observed = self.clock()
                for code, indicator in indicators.items():
                    if indicator.get("last_bar"):
                        stamp = datetime.strptime(indicator["last_bar"], "%Y-%m-%d %H:%M").replace(tzinfo=CST)
                        if not 0 <= (observed - stamp).total_seconds() <= 900:
                            indicators[code] = {"data_missing": True, "err": "实际观察时指标已过期，禁用技术规则"}
                if self._evidence is not None:
                    self.record_evidence("decision_input", {"quotes": quotes, "indicators": indicators,
                                                            "observed_at": observed.isoformat()})
                with self.research() as store:
                    if job.mode == "paper_settle":
                        result["paper"] = store.settle(quotes, observed)
                    else:
                        result["paper"] = store.monitor(quotes, indicators, observed, job.name)
                state["jobs"][key] = result
                dump(self.state_file, state)
                result["research_report"] = self.paper_report(job, now, result["paper"])
                log_file.write_text(json.dumps(result["paper"], ensure_ascii=False, indent=2), encoding="utf-8")
            elif job.mode in ("week", "month"):
                with self.research() as store:
                    report = store.report(job.mode, self.clock())
                result["research_report"] = report["text"]
                log_file.write_text(Path(report["text"]).read_text(encoding="utf-8"), encoding="utf-8")
            else:
                env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                           SHORT_TERM_BASE=str(self.runtime / SHORT))
                command = self.command(job, now)
                if self._evidence is not None and job.mode in ("scan", "pm", "ksnap"):
                    command = command[:3] + [str(HOME.parent / "VALIDATION" / "run.py"), "capture",
                                             "--engine", command[3], "--evidence", str(self._evidence.directory),
                                             "--", *command[4:]]
                with log_file.open("wb") as output:
                    completed = subprocess.run(command, cwd=str(self.home), env=env,
                                               stdout=output, stderr=subprocess.STDOUT, timeout=600, check=False)
                if completed.returncode:
                    raise RuntimeError(f"脚本退出码 {completed.returncode}，详见 {log_file}")
            if job.mode == "scan":
                path = self.runtime / SHORT / "报告" / f"候选_{now:%Y-%m-%d}.json"
                data = read(path, {})
                if self._evidence is not None:
                    self.record_evidence("candidates_file", {"raw": path.read_bytes()})
                result["validation"] = self.validation_health(job)
                if (not data.get("validations") or data.get("date") != now.date().isoformat()
                        or path.stat().st_mtime_ns < started_ns
                        or failed(data["validations"])):
                    raise RuntimeError("初筛校验异常、文件过期或无结果，禁止后续确认。")
            if job.mode == "pm":
                settlement = state["jobs"].get(f"{now:%Y-%m-%d}/paper_settle", {})
                if settlement.get("status") != "done":
                    raise RuntimeError("当日独立模拟结算未成功，不登记新的模拟样本。")
                path = self.runtime / SHORT / "报告" / f"买入清单_{now:%Y-%m-%d}.json"
                if not path.is_file() or path.stat().st_mtime_ns < started_ns:
                    raise RuntimeError("未生成本次确认文件，不登记模拟样本。")
                signal = read(path, {})
                quotes = self.quotes([r["code"] for r in signal.get("confirmed", [])])
                if self._evidence is not None:
                    self.record_evidence("signals_file", {"raw": path.read_bytes()})
                result["validation"] = self.validation_health(job)
                with self.research() as store:
                    observed = self.clock()
                    current_version = store.version(observed)
                    if version != current_version:
                        raise RuntimeError("执行期间版本变化，不登记模拟样本。")
                    campaign = verify_campaign(self.validation_root, current_version, self.validation_paths())
                    if campaign is not None:
                        scan_trace = state["jobs"][f"{now:%Y-%m-%d}/scan"].get("validation", {}).get("path")
                        if not scan_trace:
                            raise RuntimeError("缺少冻结初筛证据，禁止新增模拟样本。")
                        selection_chain(scan_trace, self._evidence.directory, signal)
                    result["paper"] = store.enter(version, signal, quotes, observed)
                state["jobs"][key] = result
                dump(self.state_file, state)
                result["research_report"] = self.paper_report(job, now, result["paper"])
                summary = Path(result["research_report"])
                with summary.open("a", encoding="utf-8") as output:
                    output.write(f"\n\n本次模拟建仓 {result['paper']['opened']} 笔，跳过 {result['paper']['skipped']} 个信号。\n")
                    with self.research() as store:
                        for event in store.events(version):
                            if event["kind"] == "skip" and event["entry_date"] == now.date().isoformat():
                                output.write(f"{event['code']} 未模拟建仓：{event['reason']}\n")
                    source_report = self.runtime / SHORT / "报告" / f"买入清单_{now:%Y-%m-%d}.md"
                    if source_report.is_file() and source_report.stat().st_mtime_ns >= started_ns:
                        output.write("\n候选分析原文（原引擎已购买清单不作为模拟账本，以本报告上方持仓为准）：\n")
                        output.write(source_report.read_text(encoding="utf-8"))
        except Exception as error:
            if "paper" in result:
                result["report_error"] = str(error)
            else:
                result = {"status": "failed", "at": now.isoformat(), "error": str(error), "research_version": version}
        try:
            self.archive_runtime(job, self.clock())
        except Exception as error:
            result["status"] = "failed"
            result["archive_error"] = str(error)
        if result["status"] == "done":
            result["notification"] = {"status": "started"}
            state["jobs"][key] = result
            dump(self.state_file, state)
            try:
                result["notification"] = self.notify(job, now, started_ns)
            except (ValueError, OSError):
                result["notification"] = {"status": "failed", "error": "推送配置或报告读取失败；不自动重发"}
        if self._evidence is not None:
            self.record_evidence("run_end", {"job": job.name, "status": result["status"],
                                              "version": version, "paper": result.get("paper"),
                                              "evidence_errors": self._evidence.errors})
            try:
                self._evidence.seal()
                result["validation"] = {**self._evidence.health(), "path": str(self._evidence.directory)}
            except Exception as error:
                result["validation"] = {"status": "incomplete", "errors": [type(error).__name__]}
        self._evidence = None
        state["jobs"][key] = result
        dump(self.state_file, state)
        self.event(job=job.name, **result)

    def run(self, now, job=None):
        if not self.runtime.is_dir() or not self.state_file.is_file():
            raise RuntimeError("尚未完成初始化；先运行 --init。")
        with lock(self.home / "bot.lock") as acquired:
            if not acquired:
                return 0
            state = read(self.state_file, {"jobs": {}})
            pending = [job] if job else due(now, state["jobs"])
            for item in pending:
                self.execute(item, now, state)
            results = [state["jobs"].get(f"{now:%Y-%m-%d}/{item.name}", {}) for item in pending]
            return int(any(result.get("status") == "failed" or
                           result.get("notification", {}).get("status") in ("failed", "unknown", "started")
                           for result in results))


def main():
    parser = argparse.ArgumentParser(description="Windows 股票研究机器人；独立模拟账本、按配置推送飞书，不下单。")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--init", action="store_true")
    action.add_argument("--plan", action="store_true")
    action.add_argument("--tick", action="store_true")
    action.add_argument("--research-init", action="store_true", help="只读归档原资料并初始化独立研究，不联网、不推送")
    action.add_argument("--research-report", choices=["all", "week", "month"], help="本地生成版本对比报告，不联网、不推送")
    action.add_argument("--update", action="store_true", help="把仓库中的策略代码与参数同步到运行副本，保留累计数据")
    action.add_argument("--run", choices=[j.name for j in JOBS] + ["status"])
    action.add_argument("--validation-start", action="store_true", help="冻结当前版本并开始前向模拟验证，不联网、不推送")
    action.add_argument("--validation-report", action="store_true", help="只读证据和模拟账本，输出前向验证进度，不联网、不推送")
    parser.add_argument("--date", help="仅 --plan 可用，YYYY-MM-DD")
    args = parser.parse_args()
    if args.date and not args.plan:
        parser.error("--date 只能用于不执行任务的 --plan。")
    bot = Bot()
    now = datetime.now(CST)
    if args.init:
        bot.initialize()
    elif args.update:
        with lock(bot.home / "bot.lock") as acquired:
            if not acquired:
                raise RuntimeError("机器人正在执行，稍后再更新。")
            print(json.dumps(bot.update(now), ensure_ascii=False, indent=2))
    elif args.validation_start or args.validation_report:
        if not bot.runtime.is_dir() or not bot.state_file.is_file():
            raise RuntimeError("先完成机器人隔离初始化。")
        with lock(bot.home / "bot.lock") as acquired:
            if not acquired:
                raise RuntimeError("机器人正在执行，未变更验证数据。")
            if args.validation_start:
                result = bot.validation_start(now)
            else:
                from VALIDATION.assess import assess, save_report
                result = save_report(assess(bot.home, now), bot.validation_root / "reports")
            print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.research_init or args.research_report:
        if not bot.runtime.is_dir() or not bot.state_file.is_file():
            raise RuntimeError("先完成机器人隔离初始化。")
        with lock(bot.home / "bot.lock") as acquired:
            if not acquired:
                raise RuntimeError("机器人正在执行，未变更研究数据。")
            with bot.research() as store:
                if args.research_init:
                    result = store.bootstrap(bot.source, now)
                else:
                    store.version(now)
                    report = store.report(args.research_report, now)
                    result = {"text": report["text"], "json": report["json"]}
                print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.plan:
        day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=CST) if args.date else now
        for job in JOBS:
            hour, minute = map(int, job.at.split(":"))
            at = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if job in due(at, {}):
                print(f"{job.at} {job.name}: {subprocess.list2cmdline(bot.command(job, at))}")
    else:
        job = next((item for item in (*JOBS, STATUS) if item.name == args.run), None)
        result_code = bot.run(now, job)
        if job:
            result = read(bot.state_file, {"jobs": {}})["jobs"].get(f"{now:%Y-%m-%d}/{job.name}", {})
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result_code if result.get("status") == "done" else 1
        return result_code
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        Bot().event(status="fatal", error=str(error))
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
