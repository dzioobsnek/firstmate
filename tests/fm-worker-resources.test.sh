#!/usr/bin/env bash
# Offline worker-resource ownership/usage/publication tests plus a real Linux
# child-process sample. No Herdr lifecycle, model requests or global writes.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
command -v python3 >/dev/null 2>&1 || { echo "skip: python3 not found"; exit 0; }
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$ROOT/tests/fm-worker-resources.test.py"
