import copy
import hashlib
import json
from pathlib import Path
import types


class _DecisionContext:
    def __init__(self, spec, partials):
        self.frozen_spec = spec
        self.partials = partials

    def spec(self, version):
        return self.frozen_spec

    def events(self, version):
        return self.partials


class CurrentSellPolicy:
    def __init__(self, config=None, source=None):
        path = Path(source) if source else Path(__file__).resolve().parents[1] / "GO_BOT" / "research.py"
        raw = path.read_bytes()
        module = types.ModuleType("backtest_frozen_sell_rules")
        module.__file__ = str(path)
        exec(compile(raw, str(path), "exec"), module.__dict__)
        self._module = module
        if config is not None and (not isinstance(config, dict) or set(config) - set(module.DEFAULT_CONFIG)):
            raise ValueError("回测配置只允许独立资金与费用字段")
        self.config = module.validate_config({**module.DEFAULT_CONFIG, **(config or {})})
        self._spec = {"rules": copy.deepcopy(module.RULES), "paper_config": copy.deepcopy(self.config)}
        source_sha = hashlib.sha256(raw).hexdigest()
        frozen = json.dumps({"source_sha256": source_sha, **self._spec}, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self.version = hashlib.sha256(frozen).hexdigest()
        self.metadata = {"adapter": "read_only_pure_decision_no_research_instance", "source": str(path),
                         "source_sha256": source_sha, "version": self.version, **copy.deepcopy(self._spec)}

    def execution(self, price, shares, side):
        if not self._module.positive(price) or type(shares) is not int or shares <= 0 or side not in ("buy", "sell"):
            raise ValueError("成交参数无效")
        return self._module.execution(price, shares, side, self._spec["paper_config"])

    def quote_error(self, code, quote, now, side):
        return self._module.quote_error(code, quote, now, side, self._spec["paper_config"])

    def decide(self, position, quote, indicator, now, slot, partials, force=False):
        context = _DecisionContext(self._spec, partials)
        return self._module.Research.decision(context, self.version, position, quote, indicator, now,
                                             slot, "offline_completed_bars", self.version, force=force)
