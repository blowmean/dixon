#!/usr/bin/env bash
# One-shot golden-set backfill + rollup + indicators
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate
python -m dixon.db --apply-schema
python -m dixon.ingest --golden --years "${DIXON_HISTORY_YEARS:-5}" --parquet
python -m dixon.rollup --all --parquet
python -m dixon.compute_indicators --all
echo "Backfill complete → ${DIXON_DB:-$ROOT/data/dixon.duckdb}"
