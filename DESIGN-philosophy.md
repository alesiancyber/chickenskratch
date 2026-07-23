# Design: Tezcat philosophy — the vault is the interface

*Status: **ADOPTED 2026-07-22**. This is the top-level doc of the design
set: it states the principles and the non-negotiable invariants, and it
supersedes DESIGN-vault.md §1's "stays in web" table (corrected below —
approvals live in the vault, not the web). Engineering counterpart:
DESIGN-processors.md. Everything else in the design set stands unless
named here.*

> The vault is the interface. The AI is the maintenance layer.

Success is measured by how rarely you need to open a chat window. The
user writes; processors organize; workers transform; git remembers. The
vault remains the source of truth.

## Principles

1. **The vault is the interface.** Every feature answers "can Obsidian
   render this?" first; if yes, it ships as a note the harness writes.
   The web app is an **administration console**, not product surface:
   chat (the escape hatch), the mid-chat `confirm_writes` dialog, server
   management, evals/stats, logs. **Approvals are vault-side** — the
   `Harness/Review.md` checkbox is the approval mechanism; no web
   approvals panel exists or will.
2. **Markdown is the protocol.** Processors consume Markdown and produce
   Markdown; the model never owns the data. Harness internals stay as
   JSON under `data/` (authoritative); where a human needs to see them,
   `Harness/*.md` is a rendered view — regenerated, never parsed back,
   except for the checkbox/command lines that carry ids precisely so
   parsing them is trivial.
3. **Chat is an escape hatch, and frozen.** Chat handles what no
   processor covers yet: questions, research, one-offs. A prompt you
   issue twice is a processor candidate. Chat gains no features; it is
   not deleted, it is just not the product.
4. **Processors are the primary abstraction, in two stages.**
   *Detect* is deterministic code (schema validation, structural rules,
   content hashes). *Draft* is **one bounded, schema-validated worker
   call** whose only output is a proposal. Not a principle we hold:
   worker chaining — multi-hop LLM pipelines multiply small-model error
   rates and make evals unattributable. One transformation, one worker,
   one eval. Revisit only when single-hop eval suites are boringly green.
5. **Review is part of the pipeline — and the reviewer can be code.**
   Detect → propose → verify-or-review → apply. Human checkboxes are for
   judgment; mechanical transformations whose safety is provable (a
   fidelity verifier: the user's words, nothing invented, nothing
   touched but the target) may earn auto-apply, per-processor, opt-out
   in the vault's own Spec.md. Review isn't the product; *control* is.
6. **The AI should disappear.** The best processor is one you forget
   exists — until you read the note it quietly fixed.

## Invariants (enforced in code, not by convention)

- **I1 — Every vault mutation goes through the gated vault operations,
  git-snapshotted before and after.** No processor, worker, or command
  handler writes files directly. Revert is always one commit away.
- **I2 — Harness-initiated changes apply only through the proposal
  queue and its staleness guard, gated by consent — human or verified.**
  The default gate is the Review.md checkbox. A processor named in
  Spec.md's `auto_apply` list may skip the checkbox **only** when its
  deterministic verifier passes (2026-07-23: extraction's fidelity check
  — the user's own words with, in `close` fidelity, a bounded
  grammar/punctuation allowance: filler/morphology additions only,
  meaning-flipping words never, sequence-coverage against the source;
  pre-existing lines byte-identical). Verification failure degrades to the queue, never
  blocks; judgment calls (grouping, schema rewrites, spec changes) have
  no verifier and always queue. The trust list is the user's, edited in
  the vault.
- **I3 — A hand-typed inline command is consent, scoped to the note it
  was typed in.** `//command` may be acknowledged and answered in place,
  below its own line; any effect on *other* notes routes through the
  queue like everything else. The harness never directly writes anywhere
  the user didn't just type.
- **I4 — `Private/` never enters model context** (enforced at the vault
  layer, one choke point) **and no secrets live in the vault, ever** —
  the vault is git-history'd and one profile switch from the cloud;
  notes hold pointers to a real secret manager.
- **I5 — Surfaces are views.** `Harness/` is machine-rendered, excluded
  from schema scans, grouping candidates, and command scanning — but
  never from git snapshots.

## Architecture direction

```
            vault (user writes)
                   ↓
   triggers: daemon / web loop / CLI / cron
                   ↓
        processor engine (one pass)
     detect (code) → draft (one worker)
                   ↓
       proposal queue → Review.md
                   ↓  (checkbox)
        applier → gated vault ops → git
```

Every trigger invokes the same engine; the web server's poll loop is
just one client of it, and the long-term default is headless (daemon +
launchd), with Obsidian as the only thing the user has open. Making chat
itself a client of the engine is a plausible endgame but is **deferred**
— chat stays frozen as-is until the engine has earned that refactor.

## Explicitly out (until a design doc says otherwise)

- **Worker chaining** (principle 4).
- **External-service processors** (`//jira`, webhooks): a new capability
  class — credentials, egress, a second source of truth — that collides
  with I4 and gets its own design or doesn't happen.
- **Autonomous edits** of any kind, including checkbox-revert from a
  text file.
- **Script execution** (future DESIGN-runtime.md, unchanged).
