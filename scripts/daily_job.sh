#!/usr/bin/env bash
# Rate-limited daily catch-up with Yahoo restate window, then targeted W/M + indicators.
#
# Exit codes (from python -m dixon.daily_job):
#   0 = ok
#   1 = fatal
#   2 = partial (some tickers failed; others updated)
#
# Env:
#   DIXON_RESTATE_DAYS   calendar days to re-download (default 14)
#   DIXON_YAHOO_SLEEP    seconds between Yahoo calls (default 0.75)
#   DIXON_DB             path to duckdb file
#
# Optional args are forwarded, e.g.:
#   ./scripts/daily_job.sh --restate-days 7 --golden
#   ./scripts/daily_job.sh --parquet --force-rebuild
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

export DIXON_RESTATE_DAYS="${DIXON_RESTATE_DAYS:-14}"
export DIXON_YAHOO_SLEEP="${DIXON_YAHOO_SLEEP:-0.75}"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] dixon daily_job starting (RESTATE_DAYS=${DIXON_RESTATE_DAYS} SLEEP=${DIXON_YAHOO_SLEEP})"
set +e
python -m dixon.daily_job "$@"
rc=$?
set -e
echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] dixon daily_job finished rc=${rc}"
exit "${rc}"
