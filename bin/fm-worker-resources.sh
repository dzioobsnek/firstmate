#!/usr/bin/env bash
# fm-worker-resources.sh - on-demand Linux worker resource report.
#
# Usage: fm-worker-resources.sh --home <FM_HOME> [--json] [--sort cpu|ram|tokens|disk]
#        [--interval <seconds>] [--publish] [--ttl <seconds>] [--wait]
#        fm-worker-resources.sh --home <FM_HOME> --herdr-config [--sidebar-width <cols>]
#
# FM_HOME can replace --home. No home is inferred from the current directory.
# Python's standard library supplies the bounded sampler, read-only usage readers,
# formatting and optional Herdr metadata publication. --help owns all options.
# Default: stdout only; no history file, daemon, configuration edit or model call.
# --publish explicitly opts in to display-only metadata for this home's exact
# current Herdr direct reports, with expiry. It never drives pane lifecycle.
# --herdr-config prints a suggested manual configuration fragment without applying
# it. Existing custom identity rows must be preserved when adopting the fragment.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'python3 is required' >&2; exit 1; }
exec python3 "$ROOT/fm-worker-resources.py" "$@"
