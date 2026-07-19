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

if command -v ruff >/dev/null 2>&1; then
    run_check "Ruff" ruff check .
else
    echo "SKIP: ruff is not installed."
fi

if command -v black >/dev/null 2>&1; then
    run_check "Black" black --check .
else
    echo "SKIP: black is not installed."
fi

if command -v mypy >/dev/null 2>&1; then
    if grep -qs '\[tool\.mypy\]' pyproject.toml mypy.ini setup.cfg 2>/dev/null; then
        run_check "MyPy" mypy .
    else
        echo "SKIP: mypy is installed but no configuration was found."
    fi
else
    echo "SKIP: mypy is not installed."
fi

echo
echo "Lint checks run: $ran"
echo "Lint failures:   $failures"
exit "$failures"
