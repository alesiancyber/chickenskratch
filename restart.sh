#!/usr/bin/env bash
# Restart every running tezcat process on the current code.
#
#   ./restart.sh            # restart the daemon and the dashboard
#   ./restart.sh --build    # uv sync + web build + cargo build first
#   ./restart.sh --no-ui    # daemon only (leave the dashboard alone)
#
# The daemon (tezcatd) is launchd-supervised with KeepAlive, so killing
# it by hand would just have launchd respawn it — it is KICKSTARTED
# through launchd instead, which is the supervised way to restart it.
# The dashboard (tezcat-ui) runs unsupervised, so it is killed and
# started fresh; it logs to data/logs/ui.log and its pid lands in
# data/logs/ui.pid. Killing mid-pass is safe by design: the pass lock
# is a flock (released on process death) and every mutation is its own
# git snapshot.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
WATCH_LABEL="com.tezcat.watch"
UI_PORT="${TEZCAT_UI_PORT:-8399}"

BUILD=0 UI=1
while [ $# -gt 0 ]; do
  case "$1" in
    --build) BUILD=1; shift ;;
    --no-ui) UI=0; shift ;;
    -h|--help) grep '^#' "$0" | head -16 | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)"; exit 2 ;;
  esac
done

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    ✓ %s\n' "$*"; }
die()  { printf '    ✗ %s\n' "$*" >&2; exit 1; }

if [ "$BUILD" = 1 ]; then
  say "Building"
  (cd "$REPO" && uv sync -q) && ok "uv sync"
  # lint BEFORE the bundle: vite happily builds a reference to deleted
  # state (the blank-page bug, 2026-07-28); eslint's no-undef refuses it
  (cd "$REPO/web" && npm run lint --silent) && ok "web lint"
  (cd "$REPO/web" && npx vite build --logLevel error) && ok "web/dist"
  (cd "$REPO/daemon" && cargo build --release -q) && ok "tezcatd"
fi

# ---- the daemon ----
say "Restarting tezcatd"
if launchctl print "gui/$(id -u)/$WATCH_LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/$WATCH_LABEL"
  ok "kickstarted $WATCH_LABEL (launchd-supervised)"
else
  # not installed as an agent: kill any hand-started one; nothing to start
  # (a daemon nobody supervises is deploy.sh's business, not a restart's)
  if pkill -f 'tezcatd --vault' 2>/dev/null; then
    ok "killed unsupervised tezcatd — run deploy.sh (or start it by hand) to bring it back"
  else
    ok "no tezcatd running and no launch agent installed"
  fi
fi

# a wedged or mid-flight engine pass holds the flock until it dies;
# killing it costs nothing (the next trigger re-scans everything)
pkill -f 'tezcat-watch' 2>/dev/null && ok "killed a running engine pass" || true

# ---- the dashboard ----
if [ "$UI" = 1 ]; then
  say "Restarting tezcat-ui"
  pkill -f 'tezcat-ui' 2>/dev/null && ok "stopped the old dashboard" \
    || ok "no dashboard was running"
  # TERM first, then verify it actually died — a `uv run` wrapper has
  # been seen outliving its python child — and escalate if not.
  for _ in $(seq 1 20); do
    pgrep -f 'tezcat-ui' >/dev/null 2>&1 || break
    sleep 0.25
  done
  if pgrep -f 'tezcat-ui' >/dev/null 2>&1; then
    pkill -9 -f 'tezcat-ui' 2>/dev/null || true
    ok "force-killed a lingering dashboard process"
  fi
  for _ in $(seq 1 40); do   # wait for the port to actually free
    lsof -ti "tcp:$UI_PORT" >/dev/null 2>&1 || break
    sleep 0.25
  done
  mkdir -p "$REPO/data/logs"
  (cd "$REPO" && nohup uv run tezcat-ui --port "$UI_PORT" \
      >> data/logs/ui.log 2>&1 & echo $! > data/logs/ui.pid)
  for _ in $(seq 1 40); do   # up when it answers, not when it forked
    if curl -fsS -o /dev/null "http://127.0.0.1:$UI_PORT/api/today" 2>/dev/null; then
      ok "dashboard answering: http://127.0.0.1:$UI_PORT (log: data/logs/ui.log)"
      exit 0
    fi
    sleep 0.5
  done
  die "dashboard did not answer on :$UI_PORT within 20s — see data/logs/ui.log"
fi
