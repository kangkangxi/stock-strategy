#!/usr/bin/env bash
# K 线快照：补记建仓后 T+1/T+2/T+3 的 10 分钟 K 线
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/tail_momentum.py --mode ksnap "$@"
