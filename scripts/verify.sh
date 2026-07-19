#!/usr/bin/env bash
set -uo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

passes=0
failures=0
skips=0
declare -a results=()

print_heading() {
    echo
    echo "############################################################"
    echo "# $1"
    echo "############################################################"
}

record_result() {
    local status="$1"
    local label="$2"
    local detail="${3:-}"

    results+=("$status: $label")
    case "$status" in
        PASS) passes=$((passes + 1)) ;;
        FAIL) failures=$((failures + 1)) ;;
        SKIP) skips=$((skips + 1)) ;;
    esac

    echo
    echo "$status: $label"
    if [[ -n "$detail" ]]; then
        echo "      $detail"
    fi
}

run_check() {
    local label="$1"
    shift

    print_heading "$label"
    "$@"
    local status=$?
    if (( status == 0 )); then
        record_result "PASS" "$label"
    else
        record_result "FAIL" "$label" "Command exited with status $status."
    fi
}

skip_check() {
    local label="$1"
    local reason="$2"
    local completion_command="$3"

    print_heading "$label"
    echo "$reason"
    record_result "SKIP" "$label" "To run: $completion_command"
}

python_bin="${PYTHON:-}"
if [[ -z "$python_bin" ]]; then
    if [[ -x "$repo_root/.venv/bin/python" ]]; then
        python_bin="$repo_root/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        python_bin="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        python_bin="$(command -v python)"
    fi
fi

printf -v python_command '%q' "${python_bin:-python}"
python_setup="$python_command -m pip install -r requirements.txt -c constraints.txt -e '.[test]'"
python_reinstall="$python_command -m pip install --force-reinstall -r requirements.txt -c constraints.txt -e '.[test]'"

python_has_module() {
    "$python_bin" -c \
        'import importlib.util, sys; raise SystemExit(importlib.util.find_spec(sys.argv[1]) is None)' \
        "$1" >/dev/null 2>&1
}

run_python_check() {
    local label="$1"
    local module="$2"
    shift 2

    if ! python_has_module "$module"; then
        skip_check "$label" "Python module '$module' is unavailable." "$python_setup"
    elif ! "$python_bin" -m "$module" --version >/dev/null 2>&1; then
        skip_check "$label" "Python tool '$module' is installed but cannot be launched." \
            "$python_reinstall"
    else
        run_check "$label" "$python_bin" "$@"
    fi
}

if [[ -n "$python_bin" ]] && "$python_bin" --version >/dev/null 2>&1; then
    run_check "Python compilation" "$python_bin" -m compileall -q backend _verify tests
    run_python_check "Ruff" ruff -m ruff check backend _verify tests
    run_python_check "MyPy" mypy -m mypy
    run_python_check "Pytest with coverage" pytest -m pytest --cov=backend --cov-report=term-missing
    run_python_check "Dependency audit" pip_audit -m pip_audit \
        --cache-dir "${TMPDIR:-/tmp}/homelabhq-pip-audit-cache-${UID:-user}"
else
    for label in "Python compilation" "Ruff" "MyPy" "Pytest with coverage" "Dependency audit"; do
        skip_check "$label" "A usable Python interpreter is unavailable." "$python_setup"
    done
fi

export PYTHON="$python_bin"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/srv/playwright-browsers}"

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    skip_check "Playwright" "Node.js and npm are unavailable." "npm ci && npx playwright install --with-deps chromium"
elif [[ ! -f package.json ]]; then
    print_heading "Playwright"
    record_result "FAIL" "Playwright" "package.json is missing."
elif ! node -e 'const p=require("./package.json"); process.exit(p.scripts?.["test:e2e"] ? 0 : 1)' \
        >/dev/null 2>&1; then
    print_heading "Playwright"
    record_result "FAIL" "Playwright" "package.json has no test:e2e script."
elif ! node -e 'require.resolve("@playwright/test")' >/dev/null 2>&1; then
    skip_check "Playwright" "Playwright's Node.js dependency is unavailable." "npm ci"
elif ! node -e '
        const fs = require("node:fs");
        const { chromium } = require("@playwright/test");
        process.exit(fs.existsSync(chromium.executablePath()) ? 0 : 1);
    ' >/dev/null 2>&1; then
    skip_check "Playwright" "The Playwright Chromium executable is unavailable." \
        "npx playwright install --with-deps chromium"
else
    run_check "Playwright" npm run test:e2e
fi

echo
echo "============================================================"
for result in "${results[@]}"; do
    echo "$result"
done
echo "------------------------------------------------------------"
echo "Verification completed: $passes PASS, $failures FAIL, $skips SKIP."

if (( failures > 0 )); then
    exit 1
fi
