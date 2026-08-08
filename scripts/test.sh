#!/usr/bin/env bash
set -uo pipefail

cd "$(git rev-parse --show-toplevel)"

failures=0
ran=0

run_check() {
    local label="$1"
    shift
    echo
    echo "==> $label"
    ran=$((ran + 1))
    "$@" || failures=$((failures + 1))
}

if command -v pytest >/dev/null 2>&1; then
    run_check "Pytest" pytest
else
    echo "SKIP: pytest is not installed."
fi

if [[ -f package.json ]] && command -v npm >/dev/null 2>&1; then
    if node -e 'const p=require("./package.json"); process.exit(p.scripts?.test ? 0 : 1)'; then
        run_check "npm test" npm test
    else
        echo "SKIP: package.json has no test script."
    fi
fi

echo
echo "Test checks run: $ran"
echo "Test failures:   $failures"
exit "$failures"
