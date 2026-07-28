# Tezcat philosophy — the vault is the truth

*The constitution of this repo: principles and non-negotiable
invariants, current as of 2026-07-26 (tezcat is the surface; the
markdown surfaces retired; one consent channel with two front doors).
History lives in git, not here.*

*Canon maintenance rule: any commit that changes a behavior this
document asserts updates this document IN THE SAME COMMIT. A stale
constitution is a bug of the blocking-release class — it wedges every
future decision that trusts it.*

> The vault is the source of truth. The AI is the maintenance layer.
> Every surface is just a renderer.

Success is measured by how rarely the user must think about anything
besides writing and deciding. The user writes; commands execute;
processors organize; workers transform; git remembers. The vault
remains a plain folder of markdown — no surface ever owns state the
vault and `data/` don't already hold, so the data is never trapped in
anything, this project included.

**Tezcat is the surface (decided 2026-07-26).** Obsidian is where this
started and what several conventions were taken from; it is not an
alternative front end we owe parity or handoffs to. Our surfaces open
notes in *our* editor — no `obsidian://` or `vscode://` deep link is
emitted anywhere, and the `link_scheme` knob that chose between them is
deleted. Other editors can still open the vault, because it is a folder
of markdown; that portability is a **byproduct of the format, not a
goal we design toward**, and it never buys another app an affordance
inside our UI.

## Principles

1. **The vault is the truth; the harness owns its surfaces.** Every
   feature ships as text in a note or a record under `data/` — never as
   state living only in a UI. What renders that state is ours: the
   dashboard, canvas, editor, file explorer, and escape-hatch pane
   (2026-07-25, spec in `research/owned-surfaces-plan.md`).

   **The markdown surfaces were retired 2026-07-26**
   (`research/surface-subtraction-plan.md`). Review.md, Today.md, and
   Changes.md are gone as notes; the same views are `tezcat today`,
   `tezcat changes`, and the dashboard, and the same consent is a
   dashboard button or `tezcat decide`. What survives is the rule that
   mattered: **no surface owns state the vault and `data/` don't already
   hold**, so the data is never trapped — not the weaker claim that
   every view must also be a file. Anything that can't ride a surface
   rides the CLI. `Harness/Capture.md` remains, because it is not a view:
   it is the canvas's write-ahead log.
2. **Markdown is the protocol.** Processors consume Markdown and
   produce Markdown; the model never owns the data. Harness internals
   stay as JSON under `data/` (authoritative). Since the surfaces were
   retired, the engine parses no note back except `//command` lines
   (I3) and the capture surface it wrote itself — the checkbox grammar
   that was the one other exception went with Review.md. The owned UI
   obeys the same rule from the other side: it renders view-models,
   never parses markdown.
3. **Two classes of change, two contracts.** A **proposal** is a change
   the harness *inferred*: it earns trust (a human verdict — or a
   deterministic verifier plus Spec.md `auto_apply`, unless its own
   drafter flagged `needs_review`), waits in the queue under `data/`,
   and whole-note staleness guards the diff a human approved. A **command** (`//azure …` + Enter, or the
   canvas equivalent) is a change the user *ordered*: trust is
   intrinsic, nothing queues, no artifact but the git commit, and
   staleness is the exact-line anchor — the user's edit to the command
   line always wins. Both ride the same substrate (gated vault tools,
   snapshot per mutation, revert).
4. **Processors are two-stage: detect is code, draft is one bounded
   worker call.** No worker chaining — multi-hop LLM pipelines multiply
   small-model error rates and make evals unattributable. One
   transformation, one worker, one eval.
5. **Review is part of the pipeline — and the reviewer can be code.**
   Human verdicts are for judgment; mechanical transformations whose
   safety is provable (fidelity verifiers: the user's words, nothing
   invented, nothing touched but the target — the target pinned in
   code, not in a prompt) may earn auto-apply, per-processor, opt-out
   in Spec.md. A plan a verifier can't vouch for waits for a human; a
   plan that could only be a malfunction (a cross-note schema fix) is
   never a proposal at all. Review isn't the product; *control* is.
6. **Model swaps are measured, never vibes.** Any worker or chat model
   change goes through the eval suites (`tezcat-eval`,
   `tezcat-worker-eval`); the worker suite's scorers are the
   production verifiers, so a passing case means "would have
   auto-applied". Paraphrase clusters guard against a model passing on
   phrasing luck. Worker prompts are written for a capable model — the
   loose reading is the baseline; small-model strictness is the adapt
   layer (a profile's `worker_adapt: small` splices each spec's hedge
   rules in), because hedging by capability tier beats deciding, rule
   by rule, which strictness a given model has outgrown. The reply
   *schema* is never tiered: it is the API, not a hedge. Eval reports
   carry the tier — hedged and loose runs of one model are different
   animals.
7. **Chat is a frozen escape hatch.** It handles what no processor or
   command covers yet, and it gains no features — a prompt you issue
   twice is a processor or command candidate. It has two homes with one
   contract: the CLI (`tezcat`, `tezcat -i`) and the collapsible
   pane (⌘J, built 2026-07-25 — the same Agent loop with its hooks
   pointed at the pane: collapsed by default; tool calls visible as
   expandable rows; write gate inline as Allow/Deny chips, every
   verdict corpus-recorded with its trajectory; transcripts stay
   scratch — the vault is the archive). Chat history surfaces, pinned conversations,
   and chat-first features are the line the deleted 2026-07-23 UI
   crossed; they stay out.
8. **The AI should disappear.** The best processor is one you forget
   exists — until you read the note it quietly fixed. The dashboard's
   ideal state is its empty state: "Nothing needs you." If the escape
   hatch is open all day, that's a processor backlog signal, not a
   reason to grow the hatch.

## Invariants (enforced in code, not by convention)

- **I1 — Every vault mutation goes through the gated vault operations,
  git-snapshotted before and after.** No processor, worker, command
  handler, or UI endpoint writes files directly. Revert is always one
  commit away (`tezcat-revert`; the UI's undo is the same revert).
- **I2 — Proposals apply only through the queue, gated by consent —
  human or verified — and the consent channel is not model-reachable.**
  Default gate: a human verdict, which since 2026-07-26 means a
  dashboard button or `tezcat decide` — both landing in the same
  `Watcher._act` under the same pass lock, so two front doors can never
  double-apply. The Review.md checkbox was the third door and is gone;
  consent no longer lives in a file at all, which is a stronger
  guarantee than the old "the file rejects model writes" (a model can
  reach a file it is refused; it cannot reach a localhost-only HTTP
  channel or a terminal). A processor in Spec.md's `auto_apply` may skip
  the gate **only** when its deterministic verifier passes and its
  drafter raised no `needs_review` flag; verification failure degrades
  to the queue, never blocks. **Amendment (2026-07-26, `autonomy:
  full`):** a vault may declare `autonomy: full` in its Spec.md — a
  standing, human-written grant under which EVERYTHING in auto_apply
  applies: verifier-less judgment lanes included, and a failing
  verifier no longer queues — it applies with its failures recorded on
  the proposal (`verifier_failures`), so the audit trail names what
  nobody vouched for. Consent moves from per-proposal to per-vault; it
  is still consent, written by the human in a file no model can write.
  `needs_review` still queues, staleness still guards, poison plans
  still fail themselves, every apply remains one revertible snapshot —
  and reverts become the negative labels the autonomous mode is
  measured by. Spec.md still
  rejects tool writes (one exception: its `folders` map — typing, not
  trust), so no model can widen its own trust list or flip its own
  autonomy.
- **I3 — A typed `//command` is consent, scoped to the exact line it
  was typed on.** It executes in place (Enter terminates the line; a
  final line with no newline is still being typed), outranks pending
  maintenance on the note, never queues, and lapses silently
  if the user edits the line away. Effects on *other* notes route
  through the queue like everything else.
- **I4 — `Private/` never enters model context, and only the vault
  ever leaves the machine.** Exclusion is enforced at the vault layer,
  one choke point. Cloud-bound profiles see the vault (minus
  exclusions) and docs search only — the filesystem server never rides
  a cloud profile. No secrets live in the vault, ever: endpoints and
  keys stay repo-side (`models.yaml`, `.env`); the vault's `worker:`
  key picks a profile *name* only.
- **I5 — Views are free; writes are not.** A read-only view (the
  dashboard's reads, `tezcat today`) needs no trust machinery —
  wrongness costs a glance, never data. The dashboard's buttons are not
  views but I2 consent, riding the same gated paths. `Harness/` stays
  excluded from schema scans, grouping, capture extraction, and command
  scanning — but never from git snapshots; dot-directories are editor
  machinery and invisible to every read path. **A view is not obliged to
  be a file.** Rendering one into the vault costs a write, a commit, and
  a churn guard every pass, and buys nothing the renderer alone doesn't
  — which is why three of them were deleted on 2026-07-26. A dashboard
  fits one screen or it isn't one.
- **I6 — Every stage boundary is a failure boundary.** One bad unit of
  input — a mis-encoded note, a malformed queue file, a garbage env
  var, a crashing scan, a poison plan, a hung remote call — fails its
  own unit and never the loop that processes all of them: scans fail
  their processor, applies fail their proposal, walks read tolerantly
  (strict reads guard the mutation paths so corruption can't write
  back), every external wait is bounded, and a dead UI process degrades
  to the editor path. The engine pass must be unkillable by content.

## Architecture

```
                 vault (user writes, any editor)          data/ (queue, corpus)
                          ↓  filesystem events (tezcatd; --poll fallback)
                 engine pass (flock-serialized)
   1. act on decisions — dashboard or `tezcat decide`   (human judgment)
   2. execute //commands in place                        (user orders)
   3. scan → draft (one worker) → verify                 (suggestions, n_max-budgeted)
      → auto-apply if trusted+verified+unflagged, else queue
   4. emit pass_complete → the UI repaints over SSE       (no surface writes)
                          ↓
            gated vault ops → git snapshot per mutation
```

Every trigger — daemon, `tezcat-watch`, cron, the UI server — invokes
the same engine; the pass lock makes concurrent triggers safe. Spec.md
is re-read every pass: worker model, n_max, disabled processors are
note edits, no restart.

The vault memo (the fingerprint that lets an unchanged tree skip the
scan) is an optimization with one invariant: **only a pass that
finished its work may memoize it.** A cancelled pass, or one whose
worker died mid-draft, memoizes nothing — otherwise the memo asserts
"this tree is handled" over text nothing ever drafted, and the input
sits unprocessed until an unrelated edit happens to move the
fingerprint. `tezcat-watch --rescan` forgets the memo (never the
seen-hashes) as the recovery lever. The same rule at the surface: a
worker that never answered is reported as such (`worker_errors`), never
as "nothing extractable" — a failure the user reads as an empty dump is
the one failure they cannot act on. The owned surfaces are a second renderer over
the same state through the same gated paths — they add no state, no
trust class, and no write that bypasses I1–I6. Built so far
(2026-07-25): the live dashboard (`tezcat-ui`, 127.0.0.1-only — JSON
view-models + SSE over the pass's event log) with Phase-2 decisions:
approve/reject/revert buttons and `run_pass`, every verb through
`service.py`, resolving in one `_act` under one pass lock, so no two
front doors can double-apply.
The UI's write channel is consent-hardened: JSON-only (forces CORS
preflight) from a local origin, on the localhost-only bind — and every
route, reads included, requires a local Host header, because DNS
rebinding reads a localhost server through the victim's browser and
the Host header is the one thing a rebound request cannot fake. The canvas
(#canvas) is built: a dump splits on the command grammar — //command
lines execute instantly (I3), the rest rides `ingest()` into the
capture pipeline, and results settle back as blocks (callouts, inline
approve/reject — including partial approval of a capture's steps, a
queue-file trim that keeps the trimmed proposal as the consent
record), or the quiet auto-filed line. The escape-hatch pane (⌘J) is
built per principle 7's contract, and the dashboard's task rows carry
the human task verbs — check off (`set_task_status`), reschedule
(`set_task_due`), and plan (`set_task_scheduled`) — each a deliberate
widening of the write list through a gated semantic tool, each one
revertible snapshot. 📅 due and ⏳ scheduled are distinct on purpose:
due is a hard external fact the harness never fabricates; scheduled is
the judgment field the promote/defer verbs move, and every move is
corpus-recorded as a scheduling label (the training stream for the
plan_today worker, whose picks always queue — judgment has no
verifier, so it can never auto-apply).
Every phase of `research/owned-surfaces-plan.md` is now shipped. The
file explorer (⌘B, 2026-07-26) is the manual-navigation rail beside
them: a read-only view-model (`/api/tree` — `iter_notes`, so the
exclusions and the dot-dir blindness are the vault layer's, not the
UI's; bounded per I6), collapsed by default, outside the split so it
never re-proportions the two surfaces. It opens notes in the owned
editor and owns no verb — creating, renaming, moving, and deleting
notes stay with the CLI for now, because each is a vault mutation and a
new write class is its own decision. Renderer knobs left the vault with
this build: diffs are always unified ```diff (`applier.preview`) and
command callouts always NOTE — the vault describes its content, the
repo describes its renderers. The `link_scheme` knob is gone entirely
(2026-07-26): with no deep links out, there was nothing left for it to
choose. `noteHref`, the dashboard's ↗ affordance, and the agent's
`_editor_link` went with it; chat citations are a `vault:` link in the
pane (opens our editor on the cited line) and a plain path in the CLI.
The one render path for model-written links (`web/src/text.jsx`) now
refuses any scheme but `http(s)` and `vault:`, so a producer we forget
about cannot quietly put a launcher back on screen.

The markdown surfaces were retired the same day
(`research/surface-subtraction-plan.md`): a pass no longer renders
Review.md, Today.md, or Changes.md, and `Harness/Capture.md` is the only
note the engine writes as a surface. Deleted with them: the checkbox
parser and its render guard, the mid-pass late-decision sweep, the
per-pass worker-reachability probe and git-log walk that existed to draw
Changes.md, the full vault walk that existed to draw Today.md, and
tezcatd's `--fast-file` lane (which now *rejects* the flag rather than
ignoring it, so a stale plist fails loudly). `tezcat today`, `tezcat
changes`, and `tezcat decide <id> approve|reject` keep every retired
view and the consent verb reachable with no UI process running.

One bug surfaced by the removal, worth remembering: those per-pass
surface commits had been sweeping untracked notes into git as a side
effect, which masked `vault_state()` being blind to untracked file
*content*. A `//command` typed into a brand-new note never fired. The
fingerprint now hashes untracked files directly.

## Explicitly out (until a design commit says otherwise)

- **Worker chaining** (principle 4).
- **External-service processors** (`//jira`, webhooks): a new
  capability class — credentials, egress, a second source of truth —
  that collides with I4. `//research` (web crawling) is reserved and
  ungated-by-name only.
- **Editor attention/focus integration**: recency-of-modification plus
  settle time is the editor-agnostic signal.
- **Autonomous edits** outside I2/I3, and destructive verbs driven from
  a text file (revert has never been one and never will be).
- **Re-growing a file-based consent channel.** Retired 2026-07-26 with
  Review.md. It cost a checkbox parser, a render guard, a mid-pass
  sweep, and the daemon's whole fast lane — all to let a human edit a
  file the engine was concurrently rewriting. If a synced multi-device
  vault ever makes asynchronous consent worth having again, that is a
  design commit, not a patch.
- **Script execution.**
- **Chat history surfaces / chat-first features** (principle 7).
- **Becoming the editor.** In-app note editing shipped with the owned
  surfaces (one note at a time, `write_note` through the same gated,
  snapshot-bracketed path) — this bullet used to say it was out, and
  that stopped being true on 2026-07-25. What stays out is everything
  past the edit you're already making: no multi-note workspace, no
  plugin surface, and — for now — no file management from the UI
  (create, rename, move, delete), because a mutation verb is never a
  side effect of a navigation feature. Note that the fallback for those
  is the CLI and the vault folder itself, NOT "go do it in Obsidian":
  since 2026-07-26 nothing in tezcat hands a note to another app, so
  anything we decline to build is a gap we own.
