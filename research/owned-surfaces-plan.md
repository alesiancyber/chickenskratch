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

### 0-A. Dead now (remove before any UI code exists) — EXECUTED 2026-07-25

1. **`config/mcp.yaml` `presets:` block** — served the deleted ⚡
   Servers panel; nothing in src/ or tests/ reads it. Delete.
2. **`config/models.yaml` orphaned comment** (the "# 8192). Only used
   for the chat UI's ctx% meter" fragment — mid-sentence leftover from a
   deleted field) and the "(⚡ Servers panel)" reference. Delete/reword.
3. **`corpus.record_bug_report`** (+ its tests) — chat-era writer,
   zero production callers since the UI deletion; git history is the
   schema of record. **Keep `record_revert`** (Phase-2 revert button
   calls it) and — amended 2026-07-25 — **keep `record_confirm`**: the
   Phase-4 pane's inline write gate is its returning caller. (It was
   on this list; the pane decision reprieved it. Subtract-before-add
   working as intended: deletions wait for the phase that proves them.)
4. **Doc rot that lies to the user**: `changes.py` HEADER text renders
   "revert from the web UI's ↺ Changes" into every vault — the UI it
   names doesn't exist (should say `tezcat-revert`). Same stale "web
   UI" references in `changes.py`/`revert.py`/`backends.py` docstrings.
   Fix wording now; Phase 2 re-points Changes.md at the dashboard.

### 0-B. Dies when the phase that supersedes it lands (not before)

5. **`diff_style` knob** (vaultconfig + Spec seeding + `_diff_block`
   split branch): the split-HTML-inside-markdown path exists only
   because Obsidian was the diff renderer. Degraded Review.md
   standardizes on unified ```diff (renders everywhere, zero knobs).
   **Amended 2026-07-25 — promote the computation, not the HTML.**
   diffhtml's diff logic (opcodes, word-level marks, context
   collapsing) is promoted as a JSON diff view-model the server ships
   and the dashboard renders under its own design tokens. The
   inline-styled HTML emitter is NOT promoted: it is Obsidian-shaped
   (styled via Obsidian CSS variables, contorted to survive the
   markdown HTML-block parser), and injecting server-generated inline
   styles into the React UI would fight the design language (tokens
   can't restyle inline styles; hover affordances can't reach inside
   static HTML). It dies with the split path it served. General rule,
   applied build-wide: **recycle contracts, state, and logic — never
   renderings**; renderings were shaped by the editor constraint this
   plan removes. Lands with Phase 2 (the view-model itself lands in
   Phase 1, which needs rich diffs).
6. **`callout_type` knob**: hardcode `NOTE` (per its own comment it
   renders styled in Obsidian AND VSCode/GitHub). Commands keep their
   callout rendering — they live in notes, which stay editor-rendered.
   Lands with Phase 3 (or 0-A if convenient; it's independent).
7. **`link_scheme` knob**: moves from Spec.md to UI config — it becomes
   "which editor do dashboard deep-links open" — and the chat-citation
   use follows. Lands with Phase 2.
8. Spec.md seeding, `_spec_overrides`, and tests (`test_spec_note`,
   `test_diffhtml` relocation) shrink in the same commits as 5–7.

### 0-C. Refactor before adding (the anti-bolt-on core) — EXECUTED 2026-07-25

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
  structured diff payloads (the promoted diff computation, not HTML),
  recent_commits, worker/backend health.
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
   **BUILT 2026-07-25** (`tezcat-ui` + `web/`): Starlette JSON
   view-models + SSE over the pass's event log (data/events-*.jsonl,
   engine-authored, UI-tailed); React+Radix+cmdk frontend built to
   static assets; POST rejected everywhere, enforced by test. The
   design-budget verdict — does "alive" land — is now a matter of
   using it.
2. **Decisions.** Approve/reject/revert buttons → `decide`/`revert`.
   Retires dependence on the checkbox without deleting it. 0-B items
   5+7 land here; Changes.md wording re-points here (closing item 4).
   **BUILT 2026-07-25** (items 5, 6, and 7 all landed): POST
   decide/revert/pass through service.py, consent-hardened
   (JSON-only + local-origin on the localhost bind); optimistic +
   undoable UI (undo IS revert(sha), including multi-step applies —
   the decide response carries the snapshot shas); j/k·a/r·e·u
   keyboard review; revert affordance on Changes rows. diffhtml died
   as amended: `diffs.diff_rows` is the promoted computation, Review.md
   is always unified ```diff, callouts always NOTE, `link_scheme` is
   repo config. Every UI revert lands a corpus row (record_revert
   returned to production, as the 0-A reprieve predicted).
3. **Canvas.** Ingest box with inline extraction preview (proposal
   renders beside what you typed; approve without leaving), instant
   command execution. "Only two surfaces" becomes literally true.
   **BUILT 2026-07-25**: `#canvas` view — auto-growing input (no
   editor engine, per the stack decision), ⌘↩ submits. The server
   splits the dump with the command grammar (`commands.parse` — one
   grammar, two entry points): //command lines execute instantly via
   `service.run_command` (I3 trust, same Spec kill switch, result
   returns to the canvas, vault untouched), the rest rides
   `service.ingest` → the capture pipeline. Dumps settle into history
   blocks with results attached: command callouts (mirroring the vault
   rendering), queued proposals with inline approve/reject, or the
   quiet "filed automatically" line when the verifier auto-applied.
   History is scratch; the dump itself is snapshot-committed on
   Capture.md, so the vault stays the archive.
4. **The escape-hatch pane** (decided 2026-07-25). **BUILT 2026-07-25**
   (`pane.py` + the ⌘J pane; the Agent loop verbatim with `log`/
   `log_result`/`confirm` hooks pointed at an SSE event queue; every
   Allow/Deny lands in the corpus via record_confirm WITH the full
   trajectory; sessions set HARNESS_CHAT_ID so in-pane vault commits
   carry the Chat-Id trailer; timeout = deny, marked timed_out. The
   prototype-parity amendments shipped in the same build: task
   check-off rows, partial approval, ticker/toast/palette polish.)
   A collapsible side pane hosting chat + visible MCP interactions. This is NOT a third
   surface and NOT chat growing features — it is the existing frozen
   escape hatch (the `harness` CLI's Agent loop, verbatim: same
   profiles, same server allowlists, same write gate) re-homed where
   its tool activity is visible. Contract, enforced in design:
   - **Collapsed by default**; summoned (⌘J / palette), closed when
     done. If it's open all day, the processors are failing — that
     signal goes to the processor backlog, not into pane features.
   - **Tool calls render as visible rows** (the Agent's existing `log`
     hook); expandable to the raw result. MCP made legible.
   - **Write gate inline**: `confirm_writes` prompts become Allow/Deny
     chips (the Agent's existing `confirm` hook) — and every decision
     is recorded via `corpus.record_confirm`, which returns to
     production use (0-A ledger amended below).
   - **Transcripts stay scratch** (the vault is the archive); no chat
     history surface, no pinned conversations, no chat-first features.
     A prompt issued twice is still a processor/command candidate —
     principle 7 keeps its teeth.

### Amendments, 2026-07-25 (prototype parity — user-approved)

Measured against the surfaces prototype artifact (the design brief the
user signed off on), three classes of gap, all approved to build:

- **Polish ledger** (design-budget items, no new trust surface):
  "pass Ns ago" ticker + pulse dot off SSE pass_complete; the floating
  "Approved — snapshot kept · Undo" toast (ONE toast, auto-dismissing,
  replacing the inline action bar — the no-toast-spam rule bans spam,
  not the single undo affordance the prototype demonstrated); palette
  entries "//azure …" (canvas prefill) and "Revert last snapshot";
  timestamps + applied-status line on canvas dump history.
- **Task check-off from the dashboard** (new write verb — a deliberate
  widening of the closed write list): `complete_task(note, text)`
  through the same gated `set_task_status` tool the MCP server exposes,
  same consent channel (human click, JSON-only local-origin POST),
  snapshot per mutation. Due/Waiting rows get the prototype's
  checkboxes. Degraded mode unchanged: checking the box in the editor
  was always possible — this only gives the dashboard the same hand.
- **Partial approval of capture extractions** (dashboard/canvas-only
  affordance): approve a SUBSET of a capture proposal's apply steps
  (per-task ✓/✗ + "approve selected"). Implemented as a queue-file
  trim + normal decide under the same pass: the trimmed proposal is
  what applies and what the corpus records. Review.md keeps
  all-or-nothing — the checkbox grammar stays two lines, and degraded
  mode loses nothing it ever had.

### Amendment, 2026-07-26 (the file explorer — user-requested)

The plan had no manual-navigation surface: notes were reachable only
from the rows the dashboard happened to be showing and from ⌘K. That's
the right *default* and the wrong *only* — knowing where a note lives
and being unable to walk there is a hole no processor closes.

- **A collapsible left rail** (⌘B, collapsed by default, state in
  localStorage) rendering the vault's folder shape from a new read-only
  view-model, `/api/tree`. It sits OUTSIDE the two-column split, so
  opening it never re-proportions the dashboard/canvas fraction the
  user dragged.
- **It adds no state and no trust class.** The tree is
  `Vault.iter_notes` — the same walk, the same exclusions (I4), the
  same dot-dir blindness (I5) — served flat and bounded
  (`MAX_TREE_NOTES`, I6). Clicking a note opens it in the editor that
  already exists; the rail owns no verb of its own.
- **Not built, deliberately:** create / rename / delete / move from the
  rail. Those are vault mutations, and every mutation belongs to a
  gated tool with a snapshot and a consent story (I1/I2) — a new write
  class is its own decision, not a side effect of wanting to browse.
  The editor and the CLI keep those hands.
- **Why the rail doesn't violate the anti-machinery tripwire:** it's a
  view of the user's own writing, not a screen for harness internals.
  The tripwire is aimed at servers/settings/status panels; the vault
  itself is the one thing that IS touched daily.

### Amendment, 2026-07-26 (Obsidian is not a surface — user-decided)

Clicking "filed → Inbox" in the canvas prompted the browser to launch
Obsidian. That was the plan working as written — deep-link-out was an
explicit affordance — and the plan was wrong. **Obsidian is where this
started, not an alternative front end.** Compatibility is a byproduct
of the vault being markdown; it is not a goal, and it buys no other app
an affordance inside our UI. Executed:

- `noteHref` (deleted), the dashboard's ↗ `OpenLink` on every row
  (deleted), and the canvas's "filed → Inbox" link (now opens our
  editor).
- The agent's `_editor_link` (deleted). Chat citations are a `vault:`
  link in the pane and a plain `Note (line N)` path in the CLI — a
  terminal has no editor to open, and launching someone else's isn't
  our job.
- The `link_scheme` config knob, its Spec/vault.yaml plumbing, and its
  `/api/health` field: gone. Nothing was left for it to choose. A stale
  `link_scheme:` in a config file is inert, not an error.
- **Enforced at the choke point, not just the producers:**
  `web/src/text.jsx` — the one place model-written links become
  clickable — now renders anything that isn't `http(s)` or `vault:` as
  plain text. A future producer emitting `obsidian://` cannot put a
  launcher back on screen.

Not touched: the markdown surfaces (`Harness/*.md`) still render every
pass. That is the degraded-mode contract below and principle 1's
no-state-outside-the-vault rule — it is about the *format* not trapping
the data, not about Obsidian being a peer surface.

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

## Stack (decided 2026-07-25)

**React + Radix primitives + cmdk**, styled by a small design-token
file (Tailwind pinned to it, or plain CSS) — Radix supplies unstyled
interaction correctness (focus, ARIA, keyboard), we own every pixel;
cmdk is the cmd+K palette. Built to static assets the FastAPI + SSE
server ships; no CDN. Backend stays the same ~4 endpoints + SSE
regardless of frontend choices.

**Canvas V1 uses NO editor engine**: a beautifully typographed
auto-growing input; the intelligence (extraction previews, command
results, dump history) renders beside/below it as ordinary React
blocks — fully Notion-styleable because nothing fights us inside a
text buffer. The canvas is one-way input (`ingest(text)`), so markdown
round-trip fidelity is a non-constraint. **TipTap** (ProseMirror) is
the earned upgrade if the canvas ever wants true block editing —
a swap-one-component job, not a rebuild. **CodeMirror 6 is shelved**:
it only wins for in-app vault-note *editing*, a screen this plan
deliberately never builds (the editor already does it better; UI
deep-links out).

Tauri wrap (tray, global hotkey to summon canvas, decision
notifications) is a later cosmetic — thin shell over the same
localhost server; don't start there.

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

Mobile, multi-user, cloud sync, any new processor capability — this
build changes renderers and input channels, never the trust model or
the engine's contracts. Chat exists ONLY as the Phase-4 escape-hatch
pane under its contract above (the CLI remains for terminals); chat
history surfaces, pinned conversations, and chat-first features stay
out — that's the line the deleted 2026-07-23 UI crossed.
