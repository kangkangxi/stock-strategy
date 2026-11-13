#!/usr/bin/env bash
# 月复盘：上一个自然月统计与改进建议
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/tail_momentum.py --mode month "$@"
