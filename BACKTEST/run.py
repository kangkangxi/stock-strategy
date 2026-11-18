import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from BACKTEST.adapter import CurrentSellPolicy
from BACKTEST.notify import send_report
from BACKTEST.report import save_report


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent


def parser():
    command = argparse.ArgumentParser(description="独立历史确认信号回放，不连接原账本，不真实下单")
    sub = command.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="显式下载历史日线，并校验本机信号/K线快照")
    prepare.add_argument("--reports", type=Path, default=REPO / "GO_BOT" / "runtime" / "短线策略" / "报告")
    prepare.add_argument("--start", default="2026-10-16")
    prepare.add_argument("--end", default="2026-11-12")
    prepare.add_argument("--through", default="2026-11-16")
    prepare.add_argument("--output", type=Path, default=ROOT / "data" / "dataset.json")
    prepare.add_argument("--cache", type=Path, default=ROOT / "cache")
    replay = sub.add_parser("run", help="完全离线回放并输出中文TXT/JSON，默认不推送")
    replay.add_argument("--dataset", type=Path, default=ROOT / "data" / "dataset.json")
    replay.add_argument("--output", type=Path, default=ROOT / "reports")
    replay.add_argument("--config", type=Path, help="独立回测资金费用配置JSON，不读取原研究账本配置")
    send = sub.add_parser("send", help="显式发送报告至飞书，每份内容只尝试一次")
    send.add_argument("--report", type=Path, required=True)
    send.add_argument("--push-config", type=Path, default=REPO / "GO_BOT" / "push_config.json")
    send.add_argument("--state", type=Path, default=ROOT / "notifications")
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "prepare":
        from BACKTEST.data import prepare
        dataset = prepare(args.reports, args.output, args.start, args.end, args.through, args.cache)
        print(json.dumps({"dataset": str(args.output), "signals": len(dataset["signals"]),
                          "exclusions": len(dataset.get("exclusions", []))}, ensure_ascii=False))
    elif args.command == "run":
        from BACKTEST.data import load_dataset
        from BACKTEST.engine import ReplayEngine
        dataset = load_dataset(args.dataset)
        if not dataset["signals"]:
            raise ValueError("没有通过质量校验的信号，不生成空样本收益报告")
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
        policy = CurrentSellPolicy(config)
        result = ReplayEngine(dataset, policy).run()
        result["policy"] = policy.metadata
        text_path, json_path = save_report(result, dataset, args.output)
        print(json.dumps({"report": str(text_path), "details": str(json_path), "summary": result["summary"]}, ensure_ascii=False))
    else:
        outcome = send_report(args.report, args.push_config, args.state)
        print(json.dumps(outcome, ensure_ascii=False))
        return 0 if outcome["status"] == "sent" or (outcome["status"] == "not_resent" and outcome.get("previous") == "sent") else 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f"回测未完成：{error}", file=sys.stderr)
        raise SystemExit(1)
