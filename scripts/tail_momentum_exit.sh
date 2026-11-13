#!/usr/bin/env bash
# 持仓监控：开盘决断与盘中巡检
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/tail_momentum.py --mode exit "$@"
