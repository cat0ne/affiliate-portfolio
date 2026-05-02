#!/usr/bin/env bash
#
# run-cro-tester.sh
#
# Convenience wrapper around scripts/cro-link-tester.ts.
# - Ensures we're in the scripts/ directory.
# - Installs npm deps if node_modules is missing.
# - Installs the Chromium browser for Playwright once (cached afterwards).
# - Runs the tester, forwarding any flags.
#
# Usage:
#   ./scripts/run-cro-tester.sh                 # headless, default 5 articles
#   ./scripts/run-cro-tester.sh --headed        # show browser
#   ./scripts/run-cro-tester.sh --site bureau   # run for a single site
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d node_modules ]; then
  echo "[run-cro-tester] node_modules missing, running npm install…"
  npm install
fi

# Install Chromium (idempotent — Playwright caches the download).
# Suppress noisy output unless something fails.
if ! npx playwright --version >/dev/null 2>&1; then
  echo "[run-cro-tester] playwright CLI not available — re-running npm install"
  npm install
fi

# `playwright install chromium` is fast on subsequent runs (just verifies
# the cached browser is present).
npx playwright install chromium >/dev/null 2>&1 || npx playwright install chromium

exec npx tsx cro-link-tester.ts "$@"
