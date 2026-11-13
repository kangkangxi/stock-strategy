#!/usr/bin/env bash
# 14:31 尾盘初筛：全市场筛选 → 评分 → 候选报告
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/tail_momentum.py --mode scan "$@"
