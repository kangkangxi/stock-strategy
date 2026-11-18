import argparse
import importlib.util
import sys
import uuid
from pathlib import Path


def _gate(parser, engine, evidence, argv):
    if not engine.is_absolute() or not evidence.is_absolute():
        parser.error("--engine 和 --evidence 必须为绝对路径")
    if not engine.is_file() or engine.suffix.lower() != ".py":
        parser.error("--engine 必须为可信本机 Python 文件")
    if not argv or argv[0] != "--":
        parser.error("引擎参数前必须使用 --")
    argv = argv[1:]
    if "--" in argv or "--no-push" not in argv or "--no-hold" not in argv:
        parser.error("capture 必须包含 --no-push 和 --no-hold")
    modes = []
    for index, argument in enumerate(argv):
        option = argument.split("=", 1)[0]
        if option.startswith("--"):
            name = option[2:]
            if name and any(forbidden.startswith(name) for forbidden in ("hold", "close")):
                parser.error("capture 禁止 --hold 和 --close（包括缩写）")
            if name and "mode".startswith(name) and name != "mode":
                parser.error("capture 必须使用完整 --mode")
        if option == "--mode":
            if "=" in argument:
                modes.append(argument.split("=", 1)[1])
            elif index + 1 < len(argv):
                modes.append(argv[index + 1])
            else:
                parser.error("--mode 缺少值")
    if len(modes) != 1 or modes[0] not in ("scan", "pm", "ksnap"):
        parser.error("capture 仅允许明确指定一个 scan/pm/ksnap 模式")
    return argv


def _returncode(value):
    if value is None:
        return 0
    return int(value) if isinstance(value, int) else 1


def _capture(engine, directory, argv):
    old_argv = sys.argv
    old_path = list(sys.path)
    old_bytecode = sys.dont_write_bytecode
    name = "_validation_engine_" + uuid.uuid4().hex
    recorder = None
    module = None
    errors = []
    returncode = 1
    try:
        sys.dont_write_bytecode = True
        if __package__:
            from .evidence import Recorder
        else:
            from evidence import Recorder
        recorder = Recorder(directory)
        recorder.event("worker_start", {"engine": engine.name, "returncode": None, "errors": []})
        spec = importlib.util.spec_from_file_location(name, engine)
        if spec is None or spec.loader is None:
            raise ImportError()
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        sys.path.insert(0, str(engine.parent))
        spec.loader.exec_module(module)
        module.audit_event = recorder.event
        sys.argv = [str(engine), *argv]
        returncode = _returncode(module.main())
    except SystemExit as exc:
        returncode = _returncode(exc.code)
        if returncode:
            errors.append(type(exc).__name__)
    except KeyboardInterrupt as exc:
        returncode = 130
        errors.append(type(exc).__name__)
    except BaseException as exc:
        returncode = 1
        errors.append(type(exc).__name__)
    finally:
        sys.argv = old_argv
        sys.path[:] = old_path
        sys.dont_write_bytecode = old_bytecode
        sys.modules.pop(name, None)
        if recorder is not None:
            errors = list(dict.fromkeys([*errors, *recorder.errors, *getattr(module, "AUDIT_ERRORS", [])]))
            recorder.event("worker_end", {"returncode": returncode, "errors": errors})
        elif errors:
            print(errors[0], file=sys.stderr)
    return returncode


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture", allow_abbrev=False)
    capture.add_argument("--engine", required=True, type=Path)
    capture.add_argument("--evidence", required=True, type=Path)
    capture.add_argument("engine_argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    remainder = _gate(parser, args.engine, args.evidence, args.engine_argv)
    return _capture(args.engine, args.evidence, remainder)


if __name__ == "__main__":
    sys.exit(main())
