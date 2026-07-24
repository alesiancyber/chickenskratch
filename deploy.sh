#!/usr/bin/env bash
# tezcat end-to-end deploy: build everything, scaffold the vault, run the
# first engine pass (which seeds Spec.md, inits the vault's git repo, and
# renders every surface), install the daemon + heartbeat, verify.
#
#   ./deploy.sh --vault ~/Vaults/personal            # the whole thing
#   ./deploy.sh --vault ~/Vaults/personal --foam     # + VSCode/Foam settings
#   ./deploy.sh --vault ... --remote git@github.com:me/vault-backup.git
#   ./deploy.sh --vault ... --no-launchd             # everything but the agents
#   ./deploy.sh --uninstall                          # unload + remove the agents
#
# Idempotent: safe to re-run; existing notes are never touched, agents are
# reloaded, binaries rebuilt.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
WATCH_LABEL="com.tezcat.watch"
HEART_LABEL="com.tezcat.heartbeat"

VAULT="" FOAM=0 REMOTE="" LAUNCHD=1 UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --vault) VAULT="$2"; shift 2 ;;
    --foam) FOAM=1; shift ;;
    --remote) REMOTE="$2"; shift 2 ;;
    --no-launchd) LAUNCHD=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) grep '^#' "$0" | head -14 | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)"; exit 2 ;;
  esac
done

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    ✓ %s\n' "$*"; }
die()  { printf '    ✗ %s\n' "$*" >&2; exit 1; }

if [ "$UNINSTALL" = 1 ]; then
  say "Uninstalling launch agents"
  for label in "$WATCH_LABEL" "$HEART_LABEL"; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$AGENTS_DIR/$label.plist" && ok "removed $label"
  done
  echo "Repo, vault, and data are untouched."
  exit 0
fi

# ---- resolve the vault ----
if [ -z "$VAULT" ]; then
  VAULT="${OBSIDIAN_VAULT_PATH:-}"
fi
if [ -z "$VAULT" ]; then
  VAULT="$(grep -E '^vault:' "$REPO/config/models.yaml" | sed 's/vault:[[:space:]]*//;s/[[:space:]]*#.*//' || true)"
  [ -n "$VAULT" ] && VAULT="$REPO/config/$VAULT"
fi
[ -n "$VAULT" ] || die "no vault: pass --vault <path> (or set OBSIDIAN_VAULT_PATH)"
mkdir -p "$VAULT"
VAULT="$(cd "$VAULT" && pwd)"
say "Deploying tezcat  repo=$REPO  vault=$VAULT"

# ---- 1. prerequisites ----
say "Checking prerequisites"
command -v git >/dev/null || die "git not found"
command -v uv  >/dev/null || die "uv not found (brew install uv)"
command -v cargo >/dev/null || die "cargo not found (brew install rustup && rustup-init)"
ok "git, uv, cargo"

# ---- 2. build ----
say "Syncing python environment"
(cd "$REPO" && uv sync -q) && ok "uv sync"
say "Building tezcatd (release)"
(cd "$REPO/daemon" && cargo build --release -q) && ok "daemon/target/release/tezcatd"

# ---- 3. vault scaffold (only what doesn't exist; never touches notes) ----
say "Scaffolding vault folders"
for d in Tasks Meetings Daily Projects Private; do mkdir -p "$VAULT/$d"; done
[ -f "$VAULT/Tasks/Inbox.md" ]   || printf '# Inbox\n\n## Tasks\n' > "$VAULT/Tasks/Inbox.md"
[ -f "$VAULT/Tasks/Waiting.md" ] || printf '# Waiting\n' > "$VAULT/Tasks/Waiting.md"
ok "folders + inboxes (existing files untouched)"

# ---- 4. pin the vault for every CLI (.env; exported shell vars still win) ----
say "Pinning OBSIDIAN_VAULT_PATH in .env"
touch "$REPO/.env"
grep -v '^OBSIDIAN_VAULT_PATH=' "$REPO/.env" > "$REPO/.env.tmp" || true
printf 'OBSIDIAN_VAULT_PATH=%s\n' "$VAULT" >> "$REPO/.env.tmp"
mv "$REPO/.env.tmp" "$REPO/.env"
ok ".env"

# ---- 5. first engine pass: seeds Spec.md, inits vault git, renders surfaces ----
say "Running the first engine pass"
(cd "$REPO" && uv run harness-watch --vault "$VAULT") || die "first pass failed"
[ -f "$VAULT/Harness/Spec.md" ] && ok "Harness/Spec.md seeded"
[ -f "$VAULT/Harness/Today.md" ] && ok "surfaces rendered (Today, Review, Changes, Capture)"

# ---- 6. foam / vscode niceties ----
if [ "$FOAM" = 1 ]; then
  say "Configuring for Foam/VSCode"
  if [ ! -f "$VAULT/.vscode/settings.json" ]; then
    mkdir -p "$VAULT/.vscode"
    cat > "$VAULT/.vscode/settings.json" <<'JSON'
{
  "files.autoSave": "afterDelay",
  "files.autoSaveDelay": 1000,
  "foam.openDailyNote.directory": "Daily",
  "foam.openDailyNote.filenameFormat": "yyyy-mm-dd"
}
JSON
    ok ".vscode/settings.json (autosave = the harness sees what you type)"
  else
    ok ".vscode/settings.json exists — left untouched (want files.autoSave: afterDelay)"
  fi
  (cd "$REPO" && uv run python - "$VAULT" <<'PY'
import sys
from pathlib import Path
from obsidian_mcp import markdown as md
spec = Path(sys.argv[1]) / "Harness" / "Spec.md"
fm, body = md.split_frontmatter(spec.read_text(encoding="utf-8"))
fm["link_scheme"] = "vscode"
fm["diff_style"] = "unified"
spec.write_text(md.join_frontmatter(fm, body), encoding="utf-8")
PY
  ) && ok "Spec.md: link_scheme=vscode, diff_style=unified"
fi

# ---- 7. optional off-machine backup for the vault repo ----
if [ -n "$REMOTE" ]; then
  say "Wiring vault backup remote"
  git -C "$VAULT" remote remove origin 2>/dev/null || true
  git -C "$VAULT" remote add origin "$REMOTE"
  git -C "$VAULT" push -u origin HEAD && ok "pushed to $REMOTE"
fi

# ---- 8. launch agents: the event daemon + the 6am heartbeat ----
if [ "$LAUNCHD" = 1 ]; then
  say "Installing launch agents"
  mkdir -p "$AGENTS_DIR" "$REPO/data/logs"
  for label in "$WATCH_LABEL" "$HEART_LABEL"; do
    sed -e "s|__REPO__|$REPO|g" -e "s|__VAULT__|$VAULT|g" \
      "$REPO/config/launchd/$label.plist" > "$AGENTS_DIR/$label.plist"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENTS_DIR/$label.plist" 2>/dev/null \
      || launchctl load "$AGENTS_DIR/$label.plist"
    ok "$label"
  done
  sleep 2
  pgrep -f "tezcatd.*$VAULT" >/dev/null && ok "tezcatd running (event watch)" \
    || echo "    ? tezcatd not visible yet — check data/logs/tezcatd.log"
fi

# ---- 9. done ----
say "Deployed"
cat <<EOF

  Vault:     $VAULT   (git-snapshotted; revert via: uv run harness-revert)
  Dashboard: $VAULT/Harness/Today.md
  Config:    $VAULT/Harness/Spec.md   (worker model, auto_apply, knobs — live per pass)
  Daemon:    data/logs/tezcatd.log    ($([ "$LAUNCHD" = 1 ] && echo installed || echo "skipped — run: daemon/target/release/tezcatd --vault $VAULT --repo $REPO --fast-file $VAULT/Harness/Review.md"))

  Try it: open any note, type '//azure obsidian sync' on its own line, press Enter.
  Worker model: set 'worker: <profile>' in Spec.md (profiles/keys in config/models.yaml + .env).
EOF
