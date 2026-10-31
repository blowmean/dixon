#!/usr/bin/env bash
# Overnight US Top-50 strategy board (grid s1-v1, US v1.2 costs).
#
# Run after the daily EOD ingest. This does not download bars.
#
#   ./scripts/strategy_board.sh
#   ./scripts/strategy_board.sh --as-of 2026-10-30
#   ./scripts/strategy_board.sh --symbols AAPL,MSFT
#
# Exit codes (from python -m dixon.strategy_board):
#   0 = board written, or nothing to score (no US session)
#   1 = fatal
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "ERROR: .venv missing — run: python3 -m venv .venv && pip install -r requirements.txt && pip install -e ." >&2
  exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] dixon strategy_board starting"
set +e
python -m dixon.strategy_board "$@"
rc=$?
set -e
echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] dixon strategy_board finished rc=${rc}"
exit "${rc}"
