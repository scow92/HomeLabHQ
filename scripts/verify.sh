#!/usr/bin/env bash
set -uo pipefail

cd "$(git rev-parse --show-toplevel)"

failures=0

run_stage() {
    local label="$1"
    shift

    echo
    echo "############################################################"
    echo "# $label"
    echo "############################################################"

    "$@" || failures=$((failures + 1))
}

run_stage "Lint" ./scripts/lint.sh
run_stage "Tests" ./scripts/test.sh
run_stage "Playwright" ./scripts/playwright.sh

echo
echo "============================================================"

if (( failures > 0 )); then
    echo "Verification failed in $failures stage(s)."
    exit 1
fi

echo "All available verification stages passed."
