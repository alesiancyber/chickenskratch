# Tezcat philosophy — the vault is the truth

*The constitution of this repo: principles and non-negotiable
invariants, current as of 2026-07-25 (owned-surfaces direction decided;
consent and failure-boundary invariants hardened in code). History
lives in git, not here.*

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
vault and `data/` don't already hold, which is why Obsidian, Foam/VSCode,
vim, and the harness's own surfaces are interchangeable viewports.

## Principles

1. **The vault is the truth; surfaces are renderers.** Every feature
   ships as text in a note or a record under `data/` — never as state
   living only in a UI. The harness may OWN its renderers (the canvas /
   dashboard / escape-hatch pane direction, decided 2026-07-25 — spec in
   `research/owned-surfaces-plan.md`), but the markdown surfaces keep
   rendering every pass, so a machine with nothing but an editor has a
   complete, degraded-but-whole tezcat vault: capture via Capture.md,
   decisions via Review.md checkboxes, attention via Today.md.
   Anything that can't ride a text file rides the CLI.
2. **Markdown is the protocol.** Processors consume Markdown and
   produce Markdown; the model never owns the data. Harness internals
   stay as JSON under `data/` (authoritative); `Harness/*.md` is a
   rendered view — regenerated, never parsed back, except checkbox and
   command lines, which carry ids/anchors precisely so parsing them is
   trivial. The owned UI obeys the same rule from the other side: it
   renders view-models, never parses markdown.
3. **Two classes of change, two contracts.** A **proposal** is a change
   the harness *inferred*: it earns trust (checkbox — or a deterministic
   verifier plus Spec.md `auto_apply`, unless its own drafter flagged
   `needs_review`), queues in Review.md, and whole-note staleness guards
   the diff a human approved. A **command** (`//azure …` + Enter, or the
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
   Human checkboxes are for judgment; mechanical transformations whose
   safety is provable (fidelity verifiers: the user's words, nothing
   invented, nothing touched but the target — the target pinned in
   code, not in a prompt) may earn auto-apply, per-processor, opt-out
   in Spec.md. A plan a verifier can't vouch for keeps its checkbox; a
   plan that could only be a malfunction (a cross-note schema fix) is
   never a proposal at all. Review isn't the product; *control* is.
6. **Model swaps are measured, never vibes.** Any worker or chat model
   change goes through the eval suites (`harness-eval`,
   `harness-worker-eval`); the worker suite's scorers are the
   production verifiers, so a passing case means "would have
   auto-applied". Paraphrase clusters guard against a model passing on
   phrasing luck.
7. **Chat is a frozen escape hatch.** It handles what no processor or
   command covers yet, and it gains no features — a prompt you issue
   twice is a processor or command candidate. It has two homes with one
   contract: the CLI (`harness`, `harness -i`) and the collapsible
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
  commit away (`harness-revert`; the UI's undo is the same revert).
- **I2 — Proposals apply only through the queue, gated by consent —
  human or verified — and the consent channel is not model-writable.**
  Default gate: the Review.md checkbox or a UI decision. A processor in
  Spec.md's `auto_apply` may skip it **only** when its deterministic
  verifier passes and its drafter raised no `needs_review` flag;
  verification failure degrades to the queue, never blocks. Machine
  surfaces (Review/Changes/Today) and Spec.md reject tool writes (the
  one exception: Spec's `folders` map — typing, not trust), so no model
  can check its own approve boxes or widen its own trust list.
- **I3 — A typed `//command` is consent, scoped to the exact line it
  was typed on.** It executes in place (Enter terminates the line; a
  final line with no newline is still being typed), outranks pending
  maintenance on the note, never grows a checkbox, and lapses silently
  if the user edits the line away. Effects on *other* notes route
  through the queue like everything else.
- **I4 — `Private/` never enters model context, and only the vault
  ever leaves the machine.** Exclusion is enforced at the vault layer,
  one choke point. Cloud-bound profiles see the vault (minus
  exclusions) and docs search only — the filesystem server never rides
  a cloud profile. No secrets live in the vault, ever: endpoints and
  keys stay repo-side (`models.yaml`, `.env`); the vault's `worker:`
  key picks a profile *name* only.
- **I5 — Surfaces are views.** `Harness/` is machine-rendered, excluded
  from schema scans, grouping, capture extraction, and command scanning
  — but never from git snapshots. Dot-directories are editor machinery
  and invisible to every read path. A read-only view (Today.md, the
  dashboard's reads) needs no trust machinery — wrongness costs a
  glance, never data. The dashboard's buttons are not views but I2
  consent, riding the same gated paths as the checkbox. A dashboard
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
   1. act on decisions — Review.md checkboxes or UI      (human judgment)
   2. execute //commands in place                        (user orders)
   3. scan → draft (one worker) → verify                 (suggestions, n_max-budgeted)
      → auto-apply if trusted+verified+unflagged, else queue
   4. render surfaces (markdown always; UI via SSE when running)
                          ↓
            gated vault ops → git snapshot per mutation
```

Every trigger — daemon, `harness-watch`, cron, the UI server — invokes
the same engine; the pass lock makes concurrent triggers safe. Spec.md
is re-read every pass: worker model, n_max, disabled processors are
note edits, no restart. The owned surfaces are a second renderer over
the same state through the same gated paths — they add no state, no
trust class, and no write that bypasses I1–I6. Built so far
(2026-07-25): the live dashboard (`harness-ui`, 127.0.0.1-only — JSON
view-models + SSE over the pass's event log) with Phase-2 decisions:
approve/reject/revert buttons and `run_pass`, every verb through
`service.py`, resolving in the same engine pass as the checkbox scan —
one `_act`, one pass lock, so the two channels can never double-apply.
The UI's write channel is consent-hardened: JSON-only (forces CORS
preflight) from a local origin, on the localhost-only bind. The canvas
(#canvas) is built: a dump splits on the command grammar — //command
lines execute instantly (I3), the rest rides `ingest()` into the
capture pipeline, and results settle back as blocks (callouts, inline
approve/reject — including partial approval of a capture's steps, a
queue-file trim that keeps the trimmed proposal as the consent
record), or the quiet auto-filed line. The escape-hatch pane (⌘J) is
built per principle 7's contract, and the dashboard's task rows carry
the check-off verb (`complete_task`, the one deliberate widening of
the write list — same gated tool the editor's checkbox always was).
Every phase of `research/owned-surfaces-plan.md` is now shipped. Renderer knobs left the vault with
this build: Review.md diffs are always unified ```diff, command
callouts always NOTE, and `link_scheme` is repo config
(config/vault.yaml) — the vault describes its content, the repo
describes its renderers.

## Explicitly out (until a design commit says otherwise)

- **Worker chaining** (principle 4).
- **External-service processors** (`//jira`, webhooks): a new
  capability class — credentials, egress, a second source of truth —
  that collides with I4. `//research` (web crawling) is reserved and
  ungated-by-name only.
- **Editor attention/focus integration**: recency-of-modification plus
  settle time is the editor-agnostic signal.
- **Autonomous edits** outside I2/I3, including checkbox-revert from a
  text file.
- **Script execution.**
- **Chat history surfaces / chat-first features** (principle 7).
- **In-app vault-note editing**: the editor already does it better;
  owned surfaces deep-link out.
