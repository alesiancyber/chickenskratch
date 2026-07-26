# Owned surfaces — canvas + dashboard plan

*2026-07-25. Supersedes the "editor is the only interface" half of the
pages-only decision; everything else in PHILOSOPHY.md stands. Rule for
this build: SUBTRACT BEFORE ADDING — Phase 0 removes and refactors so
the UI is a second renderer over existing state, never a bolt-on.*

**Thesis.** The vault stays pure, portable markdown — Obsidian/Foam
remain a complete degraded mode forever. But the two surfaces touched
daily get owned: **canvas** (dump anything in) and **dashboard** (a
live, intelligent attention/decision surface). The user should only
ever touch these two. This is NOT the deleted 2026-07-23 UI: that one
wrapped the machinery (chat/servers/revert panels) and competed with
the editor for the work. This one owns the workflow's endpoints; the
machinery stays invisible.

**Why it holds architecturally.** The surfaces are already views:
Review.md renders `data/proposals/` + vault state, Today.md is a pure
function of vault state, Changes.md renders the vault's git log. The UI
is a second renderer of the same state through the same gated write
paths. No new source of truth, no forked data.

**Consent gets stronger, not weaker.** Review.md checkboxes were locked
against tool writes because any writer can forge text. A localhost
button click feeding the same `_act` path is a channel models cannot
reach at all. The two-class model maps to affordances directly:
commands = inline execute; proposals = rich diff + approve/reject.

---

## Phase 0 — subtraction ledger

### 0-A. Dead now (remove before any UI code exists)

1. **`config/mcp.yaml` `presets:` block** — served the deleted ⚡
   Servers panel; nothing in src/ or tests/ reads it. Delete.
2. **`config/models.yaml` orphaned comment** (the "# 8192). Only used
   for the chat UI's ctx% meter" fragment — mid-sentence leftover from a
   deleted field) and the "(⚡ Servers panel)" reference. Delete/reword.
3. **`corpus.record_confirm` + `record_bug_report`** (+ their tests) —
   chat-era writers, zero production callers since the UI deletion.
   Git history is the schema of record; if the new UI grows those
   affordances they return UI-shaped. **Keep `record_revert`** — the
   Phase-2 revert button calls it unchanged.
4. **Doc rot that lies to the user**: `changes.py` HEADER text renders
   "revert from the web UI's ↺ Changes" into every vault — the UI it
   names doesn't exist (should say `harness-revert`). Same stale "web
   UI" references in `changes.py`/`revert.py`/`backends.py` docstrings.
   Fix wording now; Phase 2 re-points Changes.md at the dashboard.

### 0-B. Dies when the phase that supersedes it lands (not before)

5. **`diff_style` knob** (vaultconfig + Spec seeding + `_diff_block`
   split branch): the split-HTML-inside-markdown path exists only
   because Obsidian was the diff renderer. Degraded Review.md
   standardizes on unified ```diff (renders everywhere, zero knobs);
   **`diffhtml.py` is promoted, not deleted** — it becomes the
   dashboard's diff component. Lands with Phase 2.
6. **`callout_type` knob**: hardcode `NOTE` (per its own comment it
   renders styled in Obsidian AND VSCode/GitHub). Commands keep their
   callout rendering — they live in notes, which stay editor-rendered.
   Lands with Phase 3 (or 0-A if convenient; it's independent).
7. **`link_scheme` knob**: moves from Spec.md to UI config — it becomes
   "which editor do dashboard deep-links open" — and the chat-citation
   use follows. Lands with Phase 2.
8. Spec.md seeding, `_spec_overrides`, and tests (`test_spec_note`,
   `test_diffhtml` relocation) shrink in the same commits as 5–7.

### 0-C. Refactor before adding (the anti-bolt-on core)

9. **Split view-model from markdown everywhere.** `today.py` becomes
   `collect() -> typed sections/rows` + a thin markdown renderer;
   the UI serializes the same collect() as JSON. `changes.py` is
   already data-first (`recent_commits`); Review state is already data
   (`proposals.pending` + simulate/diff payloads). `watcher._render*`
   become one consumer among two. **No UI code may parse markdown.**
10. **Lift decisions into a service entry.** `Watcher._act` already
    takes `(decisions, pending)`; extract `decide(vault, pid, verdict)`
    running under the pass lock, so checkbox parsing and UI buttons
    converge on one path (same for `revert`).
11. **Lift capture into `ingest(text)`.** Canvas posts to it directly;
    the Capture.md scan becomes the editor-side feeder of the same
    entry. Kills nothing — converges two inputs on one pipeline.

## Architecture

- **Server**: FastAPI/Starlette, in-process imports of the engine (the
  applier idiom), bound to 127.0.0.1 only. The flock pass lock already
  serializes it against the daemon and CLI. Launched by launchd next to
  tezcatd; a dead UI process degrades to the editor path, never blocks
  the engine (per-stage failure isolation applies to the UI too).
- **Reads**: JSON view-models — today.collect(), pending proposals with
  diffhtml payloads, recent_commits, worker/backend health.
- **Writes** (all human-initiated, all through existing gated paths):
  `decide(pid, verdict)`, `ingest(text)`, `revert(sha)`, `run_pass()`.
- **Events**: one SSE stream; the daemon/pass emits `pass_complete`
  (summary payload), `vault_changed`, `decisions_pending(n)`. The
  dashboard updates the moment the engine acts — that's the "alive."
- **Trust**: `_writable` guard untouched; MCP tools unchanged; models
  have no route to any UI endpoint.

## Phases

1. **Live read-only dashboard.** Today + Review (rich diffs) + Changes
   + health over SSE. Zero new write surface (I5: views need no trust
   machinery). This phase is where the design budget goes — it decides
   whether "alive" is real before any risk exists.
2. **Decisions.** Approve/reject/revert buttons → `decide`/`revert`.
   Retires dependence on the checkbox without deleting it. 0-B items
   5+7 land here; Changes.md wording re-points here (closing item 4).
3. **Canvas.** Ingest box with inline extraction preview (proposal
   renders beside what you typed; approve without leaving), instant
   command execution. "Only two surfaces" becomes literally true.

**Degraded-mode contract (hard requirement):** after every phase, a
machine with no UI process still has a fully working vault in
Obsidian/Foam — capture via Capture.md, decisions via checkboxes,
attention via Today.md. The existing test suite is the proof and keeps
running UI-less.

## Design language — "feels like Notion"

Clean, minimalist, incredibly functional. Decomposed into decisions,
because "Notion-like" is a set of disciplines, not a stylesheet:

- **Typography IS the interface.** Content on near-white (true dark
  mode too); chrome invisible until needed. System font stack or
  Inter; ~15px body, 1.6 line height; exactly three type sizes (page
  title / section / body-meta). Separation by whitespace, not borders
  (where a line is unavoidable: 1px at ~7% alpha).
- **One accent color**, used only for "this needs you" (pending
  decisions, due-today). Grays carry everything else. Semantic color
  appears only in diffs (the GitHub hues diffhtml already uses) and
  verdict chips. 4/8px spacing rhythm throughout.
- **Hover-revealed affordances.** Approve/reject/revert/open appear on
  hover, invisible at rest — density without clutter. Every block
  reads clean as text until pointed at.
- **Keyboard-first review.** cmd+K palette (jump to note, run a
  command, trigger a pass); j/k walk pending decisions, a/r
  approve/reject, e opens in editor. The daily review of ten proposals
  should take twenty seconds and zero mouse.
- **Optimistic + undoable, never modal.** Actions apply instantly with
  a quiet "Approved — undo" affordance; undo IS `revert(sha)` — the
  git-snapshot guarantee surfaced as UX. No confirmation dialogs (the
  snapshot is the safety net), no toast spam, no spinners (SSE +
  skeletons); motion limited to 150ms ease-out fades.
- **Empty state is the product statement.** A dashboard with nothing
  pending says "Nothing needs you." — the AI-should-disappear
  principle, rendered literally.

Component mapping: a proposal = a toggle block (collapsed one-liner —
title · note · kind chip; expands to the rich diff); Today rows =
to-do blocks with quiet metadata pills (due, age); canvas = one
editable page where submitted dumps settle into history blocks below
with their extraction results attached; command results = callout
blocks (mirroring their vault rendering).

Anti-list: no component library that fights this (Material/Ant are
instant disqualifiers), no settings screens, no badges/counters for
their own sake, no modals where inline works.

## Stack

FastAPI + SSE; Svelte (or React — pick once, in Phase 1) built to
static assets the server ships; no CDN. **Styling: a small design-token
file + handmade components** (or Tailwind pinned to those tokens) — the
surface area is two pages, and the Notion feel comes from typographic
discipline you can't buy off the shelf. Tauri wrap (tray, global hotkey
to summon canvas, decision notifications) is a later cosmetic — the
Rust toolchain is already in the repo, but don't start there.

## Risks / tripwires

- **This is a design project stapled to an engineering project.**
  Phase 1 front-loads the design risk on purpose.
- **The dead-UI failure mode is scope creep into machinery UI.**
  Tripwire: a screen for anything not touched weekly gets cut. No
  servers panel, no settings screens (Spec.md stays the config), no
  chat.
- **SSE/process lifecycle** on sleep/wake — keep the daemon the only
  trigger authority; the UI only listens and asks.
- Single-user localhost assumption is explicit and fine.

## Out of scope

Chat in the UI (CLI stays the frozen escape hatch), mobile, multi-user,
cloud sync, any new processor capability — this build changes renderers
and input channels, never the trust model or the engine's contracts.
