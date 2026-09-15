#!/usr/bin/env bash
# chickenskratch first-run setup: build, configure, connect a vault.
#
#   ./setup.sh                                   # interactive — asks, then does it
#   ./setup.sh --vault ~/Vaults/personal --yes   # scripted, no prompts
#   ./setup.sh --vault ... --worker qwen35-moe --posture import
#   ./setup.sh --clean                           # + wipe data/ (queues, events, caches)
#   ./setup.sh --vault ~/Documents/x --allow-icloud   # past the iCloud refusal
#   ./setup.sh --uninstall                       # unload + remove the launch agents
#
# What it does NOT do: put anything in your vault. No starter notes, no
# folder scaffold, no examples. The only files that appear are
# Harness/Spec.md (your settings, seeded from the vault's own shape) and
# Harness/Capture.md (the canvas's write-ahead log). Both are the
# harness's own machinery and both are revertible.
#
# That restraint is new. This script used to mkdir five folders and write
# Tasks/Inbox.md and Tasks/Waiting.md into every vault it touched — an
# inbox the engine stopped having in 7c2e66a and a waiting list the 👤
# task field replaced. Notes for concepts that no longer exist are worse
# than clutter: the engine still walks them, still drafts against them,
# and still can't ever make them conform.
#
# Idempotent: safe to re-run. Existing notes are never touched.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
WATCH_LABEL="com.chickenskratch.watch"
HEART_LABEL="com.chickenskratch.heartbeat"

VAULT="" WORKER="" POSTURE="" FOAM=0 REMOTE="" LAUNCHD=1 UNINSTALL=0 CLEAN=0 ASSUME_YES=0
ALLOW_ICLOUD=0
while [ $# -gt 0 ]; do
  case "$1" in
    --vault) VAULT="$2"; shift 2 ;;
    --worker) WORKER="$2"; shift 2 ;;
    --posture) POSTURE="$2"; shift 2 ;;
    --foam) FOAM=1; shift ;;
    --remote) REMOTE="$2"; shift 2 ;;
    --no-launchd) LAUNCHD=0; shift ;;
    --clean) CLEAN=1; shift ;;
    --allow-icloud) ALLOW_ICLOUD=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    # The header block and nothing else: bounded by `set -e` rather than
    # a line count, which drifts the moment the header grows and starts
    # printing the body's comments as usage.
    -h|--help) sed -n '2,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    ✓ %s\n' "$*"; }
note() { printf '    · %s\n' "$*"; }
warn() { printf '    ! %s\n' "$*"; }
die()  { printf '    ✗ %s\n' "$*" >&2; exit 1; }
# Prompt only when there is a human to answer. --yes and a non-tty both
# take the default, so the same script drives CI and a first install.
ask()  { # ask <prompt> <default>
  local reply=""
  if [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ]; then printf '%s' "$2"; return; fi
  read -r -p "    $1 [$2]: " reply </dev/tty || true
  printf '%s' "${reply:-$2}"
}

if [ "$UNINSTALL" = 1 ]; then
  say "Uninstalling launch agents"
  for label in "$WATCH_LABEL" "$HEART_LABEL"; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$AGENTS_DIR/$label.plist" && ok "removed $label"
  done
  echo "    Repo, vault, and data/ are untouched."
  exit 0
fi

# ---- 1. prerequisites ----
#
# A fresh Mac has Homebrew and nothing else, so "check and die" is the
# wrong shape: it tells an admin with a package manager to go and run the
# command we could have run. Offer instead, and be honest about which
# ones are load-bearing.
say "Prerequisites"
BREW="$(command -v brew || true)"

need() {   # need <cmd> <formula> <why> <required|optional>
  local cmd="$1" formula="$2" why="$3" level="$4"
  if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd"; return 0; fi
  note "$cmd is missing — $why"
  if [ -n "$BREW" ]; then
    local a
    a="$(ask "Install it with: brew install $formula ? (y/n)" "y")"
    if [ "$a" = "y" ]; then
      brew install "$formula" && command -v "$cmd" >/dev/null 2>&1 && { ok "$cmd installed"; return 0; }
      warn "brew install $formula did not put $cmd on PATH"
    fi
  else
    warn "Homebrew not found — install $cmd yourself, then re-run"
  fi
  [ "$level" = "required" ] && die "$cmd is required"
  return 1
}

# git first, and RUN it rather than just finding it: a fresh Mac ships a
# /usr/bin/git shim that exists on PATH and then pops the Xcode Command
# Line Tools installer the moment anything invokes it. `command -v`
# passes; the first real snapshot hangs on a GUI dialog.
if ! git --version >/dev/null 2>&1; then
  warn "git is a stub until the Xcode Command Line Tools are installed"
  note "a window may open — finish it, then re-run this script"
  xcode-select --install 2>/dev/null || true
  die "waiting on the Command Line Tools"
fi
ok "git ($(git --version | awk '{print $3}'))"

need uv uv "it runs the engine and every CLI" required
# Node only builds the dashboard's bundle — but without it the dashboard
# is an API with a apology page where the UI should be, which on day one
# is indistinguishable from broken.
HAVE_NPM=1; need npm node "it builds the dashboard you actually look at" optional || HAVE_NPM=0
# Rust builds the file-watching daemon. Genuinely optional: without it
# the 6am heartbeat still runs passes, and you can trigger one from the
# dashboard whenever you like — you just don't get the on-change trigger.
HAVE_CARGO=1; need cargo rustup "it builds the on-change daemon (optional)" optional || HAVE_CARGO=0
[ "$HAVE_CARGO" = 1 ] || note "no daemon: passes run on the 6am heartbeat and the dashboard's button"

# ---- 2. build ----
say "Building"
(cd "$REPO" && uv sync -q) && ok "python environment"
if [ "$HAVE_CARGO" = 1 ]; then
  (cd "$REPO/daemon" && cargo build --release -q) && ok "chickenskratchd (release)" \
    || { warn "daemon build failed — continuing without it"; HAVE_CARGO=0; }
fi
if [ "$HAVE_NPM" = 1 ]; then
  (cd "$REPO/web" && npm install --silent >/dev/null 2>&1 && npm run build --silent >/dev/null 2>&1) \
    && ok "dashboard bundle (web/dist)" \
    || warn "web build failed — the dashboard will serve its API but no page"
else
  warn "no dashboard page will be built — the API works, the UI won't be there"
fi

# ---- 3. which vault ----
say "Vault"
if [ -z "$VAULT" ]; then
  VAULT="${CHICKENSKRATCH_VAULT_PATH:-}"
  [ -n "$VAULT" ] && note "from CHICKENSKRATCH_VAULT_PATH"
fi
[ -z "$VAULT" ] && VAULT="$(ask "Path to your vault (created if missing)" "$HOME/Vaults/personal")"
VAULT="${VAULT/#\~/$HOME}"
[ -n "$VAULT" ] || die "no vault given"
mkdir -p "$VAULT"
VAULT="$(cd "$VAULT" && pwd)"
case "$VAULT" in
  "$REPO"|"$REPO"/*) die "the vault cannot live inside the repo ($VAULT) — its git history is its own" ;;
esac
# YOUR notes: Harness/ is excluded because it is ours. Counting it made a
# second run of this script reclassify the vault it had just set up as an
# import — the two files setup created reading back as two notes it found.
NOTE_COUNT="$(find "$VAULT" -name '*.md' -not -path '*/.*' -not -path "$VAULT/Harness/*" \
  2>/dev/null | wc -l | tr -d ' ')"
ok "$VAULT  ($NOTE_COUNT existing note(s))"

# ~/Documents and ~/Desktop are the two folders macOS hands to iCloud
# when "Desktop & Documents Folders" is on, and that setting is a default
# for anyone who signs into iCloud during setup. It is the one vault
# location that can quietly break things:
#
#   - the vault is a git repo, and iCloud syncing .git between machines
#     corrupts it — two machines writing the object store is not a merge
#     iCloud knows how to do;
#   - synced files get EVICTED to dataless placeholders when space runs
#     low. A placeholder reads as a stall or an error, and the engine
#     walks every note on every pass.
#
# Detected by IDENTITY, not by the existence of the CloudDocs folder.
# With the sync on, ~/Documents and the CloudDocs copy are one directory
# reached two ways, so they share a device and inode. The folder alone
# proves nothing — it survives the sync being turned back off, and
# testing for it called a perfectly ordinary ~/Documents "synced" on a
# machine where it plainly was not.
CLOUD_DOCS="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
same_dir() {
  local a b
  a="$(stat -f '%d:%i' "$1" 2>/dev/null)" || return 1
  b="$(stat -f '%d:%i' "$2" 2>/dev/null)" || return 1
  [ -n "$a" ] && [ "$a" = "$b" ]
}
icloud_docs=0
case "$VAULT" in
  "$HOME/Documents"|"$HOME/Documents"/*)
    same_dir "$HOME/Documents" "$CLOUD_DOCS/Documents" && icloud_docs=1 ;;
  "$HOME/Desktop"|"$HOME/Desktop"/*)
    same_dir "$HOME/Desktop" "$CLOUD_DOCS/Desktop" && icloud_docs=1 ;;
esac
# Anything literally inside the container is synced by definition.
case "$(cd "$VAULT" && pwd -P)" in *"/Library/Mobile Documents/"*) icloud_docs=1 ;; esac
if [ "$icloud_docs" = 1 ]; then
  warn "this path is inside iCloud's Desktop & Documents sync"
  note "the vault is a git repo; iCloud syncing .git across machines corrupts it,"
  note "and evicted files read as stalls to a pass that walks every note."
  note "Safer: ~/Vaults/personal, or turn that sync off for Documents."
  if [ "$ALLOW_ICLOUD" = 1 ]; then
    warn "continuing (--allow-icloud) — watch data/logs/ if passes start stalling"
  else
    C="$(ask "Use it anyway? (y/n)" "n")"
    [ "$C" = "y" ] || die "pick another path, or pass --allow-icloud (nothing was changed)"
    warn "continuing — keep an eye on data/logs/ if passes start stalling"
  fi
fi

# ---- 4. posture: a vault with notes is an IMPORT, and imports must ask ----
#
# The single most consequential default in the whole install. schema_fix
# ships at `verified`, which means it AUTO-APPLIES: one bounded model
# call and one git commit per nonconforming note, and every apply moves
# the tree, which wakes the daemon, which drafts the next batch. On an
# empty vault that is the point. Pointed at 500 notes somebody else
# wrote, it is hours of unattended rewriting nobody asked for.
#
# The fixes it makes are additive and verified (verify_schema_fix
# requires every original line byte-identical) — so this is about
# consent and noise, not safety.
say "First-run posture"
if [ -z "$POSTURE" ]; then
  if [ "$NOTE_COUNT" -gt 0 ]; then POSTURE="import"; else POSTURE="fresh"; fi
  note "chosen for you: $POSTURE ($NOTE_COUNT note(s) already here)"
fi
case "$POSTURE" in
  fresh)  ok "fresh — schema fixes apply themselves once their check passes" ;;
  import) ok "import — everything queues for your verdict until you say otherwise"
          note "flip it later in the console (⌘;) or Harness/Spec.md" ;;
  *) die "--posture must be 'fresh' or 'import', got '$POSTURE'" ;;
esac

# ---- 5. which model drafts ----
say "Worker model"
PROFILES="$(cd "$REPO" && uv run python - <<'PY'
from chickenskratch.bootstrap import DEFAULT_MODELS
from chickenskratch.profiles import load_config
try:
    c = load_config(DEFAULT_MODELS)
    print(" ".join(c.models))
except Exception:
    print("")
PY
)"
[ -n "$PROFILES" ] || die "config/models.yaml does not parse — fix it before continuing"
note "declared: $PROFILES"
if [ -z "$WORKER" ]; then
  DEFAULT_WORKER="$(cd "$REPO" && uv run python -c "
from chickenskratch.bootstrap import DEFAULT_MODELS
from chickenskratch.profiles import load_config
print(load_config(DEFAULT_MODELS).worker or '')")"
  WORKER="$(ask "Profile the background workers run on" "$DEFAULT_WORKER")"
fi
case " $PROFILES " in
  *" $WORKER "*) ok "worker: $WORKER" ;;
  *) die "no profile named '$WORKER' — declared: $PROFILES" ;;
esac
# Two ways this choice silently fails to take, both invisible until a
# draft goes wrong hours later: a cloud profile whose key is missing, and
# a WORKER_* already pinned in the environment. Env outranks Spec.md on
# purpose (an explicit override should win), but a setup that asks you to
# pick a worker and then quietly runs a different one is exactly the
# "did that take?" problem this whole install is trying not to have.
(cd "$REPO" && uv run python - "$WORKER" <<'PY'
import os, sys
from chickenskratch.bootstrap import DEFAULT_MODELS, load_env_file
from chickenskratch.profiles import load_config
load_env_file()
name = sys.argv[1]
p = load_config(DEFAULT_MODELS).profile(name)
if p.api_key_env and not os.environ.get(p.api_key_env):
    print(f"    ! {p.api_key_env} is not set — add it to .env, or this profile "
          f"fails on its first draft")
pinned = {k: os.environ[k] for k in ("WORKER_MODEL", "WORKER_BASE_URL")
          if os.environ.get(k)}
if pinned and pinned.get("WORKER_MODEL", p.model) != p.model:
    print(f"    ! .env pins WORKER_MODEL={pinned['WORKER_MODEL']}, which OUTRANKS "
          f"the Spec.md worker you just chose ({name} -> {p.model}).")
    print( "      The env var wins by design. Drop it from .env to let the vault "
           "choose, or leave it and ignore the Spec.md setting.")
PY
) || true

# ---- 6. pin the vault for every entry point ----
say "Writing .env"
touch "$REPO/.env"
grep -v '^CHICKENSKRATCH_VAULT_PATH=' "$REPO/.env" > "$REPO/.env.tmp" || true
printf 'CHICKENSKRATCH_VAULT_PATH=%s\n' "$VAULT" >> "$REPO/.env.tmp"
mv "$REPO/.env.tmp" "$REPO/.env"
ok "CHICKENSKRATCH_VAULT_PATH pinned (outranks the vault: line in models.yaml)"

# ---- 7. optional: clear derived state ----
if [ "$CLEAN" = 1 ]; then
  say "Clearing data/"
  if [ -d "$REPO/data" ]; then
    C="$(ask "Delete proposal queues, event logs, caches and config backups? (y/n)" "n")"
    if [ "$C" = "y" ]; then rm -rf "$REPO/data" && ok "data/ removed"
    else note "kept"; fi
  else ok "nothing to clear"; fi
fi

# ---- 8. seed Spec.md from the vault's OWN shape ----
#
# Before the first pass, not by it. The engine seeds a Spec.md from
# config/vault.yaml's defaults, which assert a layout — Tasks, Meetings,
# Research, Scripts — that a vault we have never seen has no reason to
# share. Worse, the seed decides the posture, and by the time the pass
# has written it the pass has already acted on it.
#
# So the map is derived from the folders that actually hold notes, and
# the posture is written in first. Folders the harness cannot type are
# mapped `other`; spec_drift proposes the rest as they appear.
say "Seeding Harness/Spec.md"
(cd "$REPO" && uv run python - "$VAULT" "$WORKER" "$POSTURE" <<'PY'
import pathlib, sys
from chickenskratch_vault import markdown as md
from chickenskratch_vault import schemas as sc
from chickenskratch_vault.vaultconfig import SPEC_REL, get_config
from chickenskratch.watcher import SPEC_BODY
from chickenskratch import processors

vault, worker, posture = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
spec = vault / SPEC_REL
if spec.exists():
    print(f"    · {SPEC_REL} already exists — left alone (yours, never regenerated)")
    raise SystemExit(0)

cfg = get_config(str(vault))
known = set(sc.load_schemas())
folders = {}
for child in sorted(p for p in vault.iterdir() if p.is_dir()):
    name = child.name
    if name.startswith(".") or name in cfg.exclude or name == "Harness":
        continue
    if not any(child.rglob("*.md")):
        continue                      # empty scaffolding is not a mapping
    guess = name.lower().rstrip("s")  # Meetings -> meeting, Notes -> note
    folders[name] = guess if guess in known else "other"

# Every processor named at its resolved level: a new vault's whole
# posture is legible in one key rather than inherited from code.
policies = {p.name: cfg.policy_for(p.name) for p in processors.REGISTRY}

# The sweep lanes: the ones whose scan yields one work item PER NOTE, so
# their cost scales with the vault rather than with what you did today.
# On an import they go OFF, not to `ask` — `ask` only moves where the
# result lands. The model call still happens, once per nonconforming
# note, n_max per pass, every pass, until the whole vault has been
# through; you would just be reviewing 500 queued proposals instead of
# 500 applied ones. Off costs nothing and is one click to undo.
SWEEPS = ("schema_fix", "grouping")
if posture == "import":
    policies = {k: ("off" if k in SWEEPS else "ask") for k in policies}

fm = {
    "folders": folders,
    "done_archive": cfg.done_archive,
    "exclude": list(cfg.exclude),
    "capture_archive": cfg.capture_archive,
    "policies": policies,
    "fidelity": cfg.fidelity,
    "worker": worker,
    "disabled_processors": list(cfg.disabled_processors),
    "n_max": cfg.n_max,
    "daily_dir": cfg.daily_dir,
    "meetings_dir": cfg.meetings_dir,
}
spec.parent.mkdir(parents=True, exist_ok=True)
spec.write_text(md.join_frontmatter(fm, SPEC_BODY), encoding="utf-8")
print(f"    ✓ {SPEC_REL} — {len(folders)} folder(s) mapped, worker={worker}, posture={posture}")
for name, kind in folders.items():
    print(f"        {name}/ -> {kind}")
PY
) || die "could not seed Spec.md"

# ---- 9. first engine pass: inits the vault's git repo, renders nothing ----
say "First engine pass"
(cd "$REPO" && uv run chickenskratch-watch --vault "$VAULT") || die "first pass failed"
[ -f "$VAULT/Harness/Capture.md" ] && ok "Harness/Capture.md ready"
git -C "$VAULT" rev-parse HEAD >/dev/null 2>&1 && ok "vault git repo initialised (every change revertible)"

# ---- 10. foam / vscode niceties ----
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
fi

# ---- 11. optional off-machine backup for the vault repo ----
if [ -n "$REMOTE" ]; then
  say "Wiring vault backup remote"
  git -C "$VAULT" remote remove origin 2>/dev/null || true
  git -C "$VAULT" remote add origin "$REMOTE"
  git -C "$VAULT" push -u origin HEAD && ok "pushed to $REMOTE"
fi

# ---- 12. launch agents: the event daemon + the 6am heartbeat ----
if [ "$LAUNCHD" = 1 ]; then
  say "Launch agents"
  I="$(ask "Install the background agents? (y/n)" "y")"
  if [ "$I" = "y" ]; then
    mkdir -p "$AGENTS_DIR" "$REPO/data/logs"
    # Resolved here, never hardcoded: Homebrew lives at /opt/homebrew on
    # Apple Silicon and /usr/local on Intel, and launchd gives a plist
    # naming the wrong one no diagnosis beyond an exit code at login.
    UV_BIN="$(command -v uv)"
    AGENT_PATH="$(printf '%s:%s%s' "$(dirname "$UV_BIN")" \
      "${BREW:+$(brew --prefix)/bin:}" "/usr/local/bin:/usr/bin:/bin" \
      | awk -v RS=: '!seen[$0]++ {printf "%s%s", sep, $0; sep=":"}')"
    # The watch agent is the daemon; without a daemon binary there is
    # nothing for it to launch, and installing it anyway would give you a
    # KeepAlive agent that respawns a missing file forever.
    labels="$HEART_LABEL"
    [ "$HAVE_CARGO" = 1 ] && labels="$WATCH_LABEL $labels"
    for label in $labels; do
      sed -e "s|__REPO__|$REPO|g" -e "s|__VAULT__|$VAULT|g" \
          -e "s|__UV__|$UV_BIN|g" -e "s|__PATH__|$AGENT_PATH|g" \
        "$REPO/config/launchd/$label.plist" > "$AGENTS_DIR/$label.plist"
      launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$AGENTS_DIR/$label.plist" 2>/dev/null \
        || launchctl load "$AGENTS_DIR/$label.plist"
      ok "$label"
    done
    [ "$HAVE_CARGO" = 1 ] || note "$WATCH_LABEL skipped — no daemon binary was built"
    if [ "$HAVE_CARGO" = 1 ]; then
      sleep 2
      pgrep -f "chickenskratchd.*$VAULT" >/dev/null && ok "chickenskratchd running" \
        || warn "chickenskratchd not visible yet — see data/logs/chickenskratchd.log"
    fi
    # macOS gates ~/Documents, ~/Desktop and ~/Downloads behind TCC. A
    # Terminal run prompts and you click Allow; a launchd agent often
    # gets refused with no prompt at all, and the only symptom is passes
    # that never touch anything.
    case "$VAULT" in
      "$HOME/Documents"/*|"$HOME/Desktop"/*|"$HOME/Downloads"/*)
        note "macOS protects this folder: if background passes never run, grant"
        note "Full Disk Access to $UV_BIN in System Settings › Privacy & Security" ;;
    esac
  else
    note "skipped — trigger passes from the dashboard, or run:"
    note "  uv run chickenskratch-watch --vault $VAULT"
  fi
fi

# ---- 13. what landed ----
#
# Only what SETUP created, never `git ls-files` — on an import the first
# pass commits the notes that were already there, and listing those under
# "what this put in your vault" would take credit for your own writing.
say "Done"
PLANTED="$(cd "$VAULT" && git ls-files 'Harness/*' | tr '\n' ' ')"
cat <<EOF

  Vault:      $VAULT
  Setup put:  ${PLANTED:-（nothing）}
              — and nothing else. No starter notes, no folder scaffold,
                no examples.
  Your notes: $NOTE_COUNT already there, untouched$([ "$NOTE_COUNT" -gt 0 ] && echo " (captured in the vault's first git snapshot)")
  Posture:    $POSTURE      (worker: $WORKER)
  Dashboard:  uv run chickenskratch-ui        → 127.0.0.1:8399
  Headless:   uv run chickenskratch today | chickenskratch changes
  Settings:   the console (⌘; in the dashboard), or $VAULT/Harness/Spec.md
  Undo:       uv run chickenskratch-revert    (every change is a git snapshot)

EOF
if [ "$POSTURE" = "import" ]; then cat <<EOF
  You imported $NOTE_COUNT note(s), so nothing sweeps them:

    schema_fix, grouping   off   — the per-note lanes. Left on, these
                                   cost one model call per note until
                                   the whole vault has been through.
    everything else        ask   — drafts land in the queue for a verdict.

  Look around first. When you want the vault tidied, turn schema_fix on
  in the console (⌘;) and watch one pass before leaving it running.

EOF
fi
