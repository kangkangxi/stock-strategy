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
    *(Job("exit_" + at.replace(":", ""), at, SHORT, "exit") for at in
      ("09:35", "10:00", "10:30", "11:00", "11:31", "13:05", "13:30", "14:00", "14:30")),
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
    if now.weekday() >= 5:
        return []
    first = now.date().replace(day=1)
    while first.weekday() >= 5:
        first += timedelta(days=1)
    result = []
    for job in JOBS:
        if job.period == "week" and now.weekday() != 4:
            continue
        if job.period == "month" and now.date() != first:
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

    def initialize(self):
        if self.runtime.exists():
            raise RuntimeError("隔离目录已存在，不覆盖已有运行数据。")
        config = self.strategy_config()
        self.runtime.mkdir(parents=True)
        shutil.copytree(self.source, self.runtime / SHORT,
                        ignore=shutil.ignore_patterns(self.home.name, ".git", "__pycache__", "*.bak", "*.bak-*"))
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
                self.home.name, ".git", "__pycache__", "*.bak", "*.bak-*",
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
        if job.mode in ("paper_settle", "week", "month"):
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
        return module

    def market_env(self):
        return self.market_module().em_market_env()

    def quotes(self, codes):
        return self.market_module().tencent_quotes(codes) if codes else {}

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
        if job.mode in ("week", "month"):
            return
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
        if job.mode in ("week", "month"):
            result = read(self.state_file, {"jobs": {}})["jobs"].get(f"{now:%Y-%m-%d}/{job.name}", {})
            if result.get("research_report"):
                return Path(result["research_report"])
        names = {"scan": f"尾盘选股_{now:%Y-%m-%d}.md", "pm": f"买入清单_{now:%Y-%m-%d}.md",
                 "week": "最新周总结.md", "month": "最新月总结.md"}
        name = names.get(job.mode)
        return self.runtime / SHORT / "报告" / name if name else None

    def notify(self, job, now, started_ns):
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

    def execute(self, job, now, state):
        key = now.strftime("%Y-%m-%d") + "/" + job.name
        if key in state["jobs"]:
            return
        state["jobs"][key] = {"status": "started", "at": now.isoformat()}
        dump(self.state_file, state)
        started_ns = time.time_ns()
        version = None
        try:
            self.archive_runtime(job, now)
            self.guard(job, now, state)
            log_file = self.home / "logs" / f"{now:%Y-%m-%d}_{job.name}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            result = {"status": "done", "at": now.isoformat(), "log": str(log_file)}
            if job.system == SHORT and job.mode != "status":
                with self.research() as store:
                    version = store.version(now)
                result["research_version"] = version
            if job.mode == "paper_settle":
                with self.research() as store:
                    quotes = self.quotes(store.open_codes())
                    result["paper"] = store.settle(quotes, self.clock())
                log_file.write_text(json.dumps(result["paper"], ensure_ascii=False, indent=2), encoding="utf-8")
            elif job.mode in ("week", "month"):
                with self.research() as store:
                    report = store.report(job.mode, self.clock())
                result["research_report"] = report["text"]
                log_file.write_text(Path(report["text"]).read_text(encoding="utf-8"), encoding="utf-8")
            else:
                env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                           SHORT_TERM_BASE=str(self.runtime / SHORT))
                with log_file.open("wb") as output:
                    completed = subprocess.run(self.command(job, now), cwd=str(self.home), env=env,
                                               stdout=output, stderr=subprocess.STDOUT, timeout=600, check=False)
                if completed.returncode:
                    raise RuntimeError(f"脚本退出码 {completed.returncode}，详见 {log_file}")
            if job.mode == "scan":
                path = self.runtime / SHORT / "报告" / f"候选_{now:%Y-%m-%d}.json"
                data = read(path, {})
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
                with self.research() as store:
                    if version != store.version(self.clock()):
                        raise RuntimeError("执行期间版本变化，不登记模拟样本。")
                    result["paper"] = store.enter(version, signal, quotes, self.clock())
        except Exception as error:
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
