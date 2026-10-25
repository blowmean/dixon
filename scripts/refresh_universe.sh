#!/usr/bin/env bash
# Regenerate universe/us_all.txt, hk_all.txt, all.txt
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate
python -m dixon.refresh_universe "$@"
