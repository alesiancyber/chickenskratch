# Tezcat philosophy — the vault is the interface

*The constitution of this repo: principles and non-negotiable
invariants, current as of 2026-07-23 (pages-only realized, web UI
deleted, commands/proposals split). History lives in git, not here.*

> The vault is the interface. The AI is the maintenance layer.

Success is measured by how rarely you need anything besides your
editor. The user writes; commands execute; processors organize; workers
transform; git remembers. The vault remains the source of truth — and
the vault is just a folder of markdown: no editor ever executes harness
code, which is why Obsidian, Foam/VSCode, or vim are interchangeable
viewports (rendering differences are Spec.md knobs, not code).

## Principles

1. **The vault is the interface.** Every feature ships as text in a
   note the harness reads or writes. There is no app: decisions are
   checkboxes in `Harness/Review.md`, commands are typed lines, config
   is `Harness/Spec.md` frontmatter, the audit trail is
   `Harness/Changes.md`. Anything that can't ride a text file rides the
   CLI (`harness-revert`, `harness-backends`).
2. **Markdown is the protocol.** Processors consume Markdown and
   produce Markdown; the model never owns the data. Harness internals
   stay as JSON under `data/` (authoritative); `Harness/*.md` is a
   rendered view — regenerated, never parsed back, except checkbox and
   command lines, which carry ids/anchors precisely so parsing them is
   trivial.
3. **Two classes of change, two contracts.** A **proposal** is a change
   the harness *inferred*: it earns trust (checkbox, or a deterministic
   verifier plus Spec.md `auto_apply`), queues in Review.md, and
   whole-note staleness guards the diff a human approved. A **command**
   (`//azure …` + Enter) is a change the user *ordered*: trust is
   intrinsic, nothing queues, no artifact but the git commit, and
   staleness is the exact-line anchor — the user's edit to the command
   line always wins. Both ride the same substrate (gated vault tools,
   snapshot per mutation, revert). LLM-backed commands also write
   directly: the user is watching the spot where the output lands.
4. **Processors are two-stage: detect is code, draft is one bounded
   worker call.** No worker chaining — multi-hop LLM pipelines multiply
   small-model error rates and make evals unattributable. One
   transformation, one worker, one eval.
5. **Review is part of the pipeline — and the reviewer can be code.**
   Human checkboxes are for judgment; mechanical transformations whose
   safety is provable (fidelity verifiers: the user's words, nothing
   invented, nothing touched but the target) may earn auto-apply,
   per-processor, opt-out in Spec.md. Review isn't the product;
   *control* is.
6. **Model swaps are measured, never vibes.** Any worker or chat model
   change goes through the eval suites (`harness-eval`,
   `harness-worker-eval`); the worker suite's scorers are the
   production verifiers, so a passing case means "would have
   auto-applied". Paraphrase clusters guard against a model passing on
   phrasing luck.
7. **Chat is a frozen escape hatch.** The CLI (`harness`, `harness -i`)
   handles what no processor or command covers yet. A prompt you issue
   twice is a processor or command candidate. Chat gains no features.
8. **The AI should disappear.** The best processor is one you forget
   exists — until you read the note it quietly fixed.

## Invariants (enforced in code, not by convention)

- **I1 — Every vault mutation goes through the gated vault operations,
  git-snapshotted before and after.** No processor, worker, or command
  handler writes files directly. Revert is always one commit away
  (`harness-revert`).
- **I2 — Proposals apply only through the queue, gated by consent —
  human or verified.** Default gate: the Review.md checkbox. A
  processor in Spec.md's `auto_apply` may skip it **only** when its
  deterministic verifier passes; verification failure degrades to the
  queue, never blocks; judgment calls have no verifier and always
  queue. The trust list is the user's, edited in the vault.
- **I3 — A typed `//command` is consent, scoped to the exact line it
  was typed on.** It executes in place (Enter terminates the line; a
  final line with no newline is still being typed), outranks pending
  maintenance on the note, never grows a checkbox, and lapses silently
  if the user edits the line away. Effects on *other* notes would
  route through the queue like everything else.
- **I4 — `Private/` never enters model context** (enforced at the vault
  layer, one choke point) **and no secrets live in the vault, ever** —
  the vault is git-history'd and one profile switch from the cloud.
  Model endpoints and keys stay repo-side (`models.yaml`, `.env`); the
  vault's `worker:` key picks a profile *name* only.
- **I5 — Surfaces are views.** `Harness/` is machine-rendered, excluded
  from schema scans, grouping, capture extraction, and command scanning
  — but never from git snapshots. Dot-directories are editor machinery
  and invisible to every read path.

## Architecture

```
                 vault (user writes, any editor)
                          ↓  filesystem events (tezcatd; --poll fallback)
                 engine pass (flock-serialized)
   1. act on Review.md checkbox decisions      (human judgment)
   2. execute //commands in place              (user orders, model-free or not)
   3. scan → draft (one worker) → verify       (harness suggestions, n_max-budgeted)
      → auto-apply if trusted+verified, else queue
   4. render Review.md / Changes.md, save memo
                          ↓
            gated vault ops → git snapshot per mutation
```

Every trigger — daemon, `harness-watch`, cron — invokes the same
engine; the pass lock makes concurrent triggers safe. Spec.md is
re-read every pass: worker model, n_max, disabled processors, editor
rendering are note edits, no restart.

## Explicitly out (until a design commit says otherwise)

- **Worker chaining** (principle 4).
- **External-service processors** (`//jira`, webhooks): a new
  capability class — credentials, egress, a second source of truth —
  that collides with I4. `//research` (web crawling) is reserved and
  ungated-by-name only.
- **Editor attention/focus integration**: recency-of-modification plus
  settle time is the editor-agnostic signal; focus tracking would
  re-couple the harness to one editor for marginal gain.
- **Autonomous edits** outside I2/I3, including checkbox-revert from a
  text file.
- **Script execution.**
