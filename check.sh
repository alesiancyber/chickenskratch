#!/usr/bin/env bash
# Every gate this repo has, one command.
#
#   ./check.sh          # the static gates: ruff + eslint + htmlhint
#   ./check.sh --all    # + pytest and cargo test (the slow half)
#
# Model-behavior gates (chickenskratch-eval, chickenskratch-worker-eval) stay separate
# on purpose: they need a live model and answer a different question
# ("is this model good enough"), not "is this code broken".
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
ALL=0
[ "${1:-}" = "--all" ] && ALL=1

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    ✓ %s\n' "$*"; }

say "ruff (python lint)"
(cd "$REPO" && uv run ruff check src tests) && ok "src/ tests/"

say "eslint + htmlhint (web lint)"
(cd "$REPO/web" && npm run lint --silent) && ok "web/src + index.html"

if [ "$ALL" = 1 ]; then
  say "pytest"
  (cd "$REPO" && uv run pytest -q tests/)
  say "cargo test (daemon)"
  (cd "$REPO/daemon" && cargo test -q)
fi

say "All gates passed"
