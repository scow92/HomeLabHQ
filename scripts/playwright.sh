#!/usr/bin/env bash
set -uo pipefail

cd "$(git rev-parse --show-toplevel)"

if [[ ! -f package.json ]]; then
    echo "SKIP: package.json does not exist."
    exit 0
fi

if ! command -v npx >/dev/null 2>&1; then
    echo "SKIP: npx is not installed."
    exit 0
fi

if [[ ! -f playwright.config.ts &&
      ! -f playwright.config.js &&
      ! -f playwright.config.mjs &&
      ! -f playwright.config.cjs ]]; then
    echo "SKIP: no Playwright configuration was found."
    exit 0
fi

echo "==> Playwright"
npx playwright test
