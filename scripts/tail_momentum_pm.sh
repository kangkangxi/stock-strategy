#!/usr/bin/env bash
# 14:51 尾盘确认：持仓退出 + 候选复核 → 买入清单
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/tail_momentum.py --mode pm "$@"
