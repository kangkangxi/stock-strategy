#!/usr/bin/env bash
# 周复盘：胜率 / 盈亏比 / 选股漏斗 / 改进建议
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/tail_momentum.py --mode week "$@"
