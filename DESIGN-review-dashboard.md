# Design: review dashboard — proposal queue, watcher, and scoped grouping

*Status: **BUILT through D2 machinery** (DEVPLAN Phases 4–5, 2026-07-22)
— queue (`src/harness/proposals.py`), applier (`applier.py`), watcher
(`watcher.py`, `uv run harness-watch`, web-server poll), `schema_fix`
drafter, the `Harness/Review.md` checkbox flow, the `Harness/Capture.md`
intake surface, and grouping G1 (`grouping.py`: structural candidates,
partition integrity, byte-identical rewrites, eval cases incl. the
customer-boundary regression) are implemented and tested end-to-end with
scripted drafters. The D0/D1/D2 **usage gates** (real proposals reviewed
live, a real stale occurrence, a live grouping eval run) still need
mileage; D3 (G2 cross-note consolidation) is not built. Depends on DESIGN-vault-schema.md
as its substrate. Complements DESIGN-training-corpus.md: every dashboard
decision is a corpus label (`source: "dashboard"`, wired). **§4 (web
pane) is superseded by DESIGN-vault.md §5:** the dashboard renders as
`Harness/Review.md` with checkbox-approve.*

## Context — a pane where potential gains live

The goal is **not** to notify the user when the AI thinks something. It is
a pane the user opens at convenience, holding drafted improvements they
can approve, reject, or ignore:

- "These 4 meeting notes don't conform to the meeting schema — here are
  the normalizations." *(schema conformance — mechanical to detect)*
- "You added 5 different versions of 'review X detection' over the past
  two weeks — here's a consolidated review-detection task list."
  *(grouping/consolidation — fuzzy to detect)*

Approving one applies it through the same gated write tools every chat
uses. This gets consistent structure across the vault **without the user
doing cleanup by hand and without unsupervised rewriting** — the two
failure modes this project has explicitly rejected (manual toil;
plausible-but-wrong silent mutation, DESIGN-vault-schema.md).

Two design problems dominate: **what generates proposals in the
background** (the watcher), and **when a stored proposal is still safe to
apply** (staleness). Both are solved below with machinery the harness
already has — the vault's git history and the worker framework.

## 1. The watcher is a pipeline, not a process

The obvious mental model — a resident AI daemon watching the vault — is
rejected. This codebase already refused to manage remote server processes
because half-dead background processes the harness "thinks it owns" are a
failure-mode factory (DESIGN-mixed-models.md §10). A background LLM
daemon on a 16 GB machine that also serves the chat model is that,
squared. Instead:

**The watcher is a two-stage pipeline invoked at defined moments, with a
git-SHA memo so it is usually free.**

### Stage 1 — scan (deterministic, code, no LLM, fast)

Calls the same implementation behind `validate_schema`
(DESIGN-vault-schema.md) plus the grouping candidate builder (§3):

- unmarked/nonconforming entities → one `schema_fix` work item per note
- grouping candidate sets (built structurally, §3) → one `grouping` work
  item per scope

Output: a list of work items. Pure functions of vault content — cheap
enough to run on every trigger.

### Stage 2 — draft (LLM, bounded, read-only)

Each work item gets one bounded LLM call producing a proposal into the
queue. Drafting follows the worker contract of DESIGN-mixed-models.md
§7.1 verbatim: read-only, jsonschema-validated output, per-call timeout,
one retry, temperature ≤ 0.2. **Drafters write only to the proposal queue
(harness data dir), never to the vault** — "workers interpret, they never
mutate" holds; the mutation happens at approval, through gated tools.

`schema_fix` drafting fits the 4B worker (single-shot, schema-shaped —
exactly the single-turn-AST-shaped work DESIGN-mixed-models.md §11.2
says small models don't lose at). `grouping` drafting starts on the
default chat profile; may move down to the worker if its eval cases pass
there.

Budget per run: at most `N_max = 5` new proposals drafted (config), so a
legacy vault's backlog drains over days instead of hammering the machine
in one pass. Stage 2 never runs while a chat generation is in flight —
single user, one model at a time on the M5.

### Triggers (pull-based, in priority order)

1. **Dashboard pane opened** — the primary trigger. Review-at-convenience
   implies scan-at-convenience.
2. **`uv run harness-watch`** — manual/cron CLI entry point, same
   pipeline, for warming the queue overnight once the b70 exists.
3. *(Not in v1:)* post-chat-session hook.

### The git memo — why this is usually free

The vault is a git repo and every harness mutation commits. The watcher
records `(vault HEAD sha, dirty-file list hash)` after each scan
(`data/watcher_state.json`). On trigger, if neither changed since the
last scan, **nothing runs** — open the pane twice in a row and the second
open is a file read. `git status --porcelain` catches hand-edits Obsidian
made that no harness write has snapshotted yet.

## 2. Proposal queue

`data/proposals/<id>.json`, one file per proposal (human-skimmable
singles, like `data/bug_reports/` — these are meant to be opened and
read; high-volume append logs stay JSONL).

```json
{
  "schema_version": 1,
  "id": "p-20260722-a1b2c3",
  "kind": "schema_fix",            // or "grouping"
  "status": "pending",             // pending | approved | rejected | stale
  "created": "2026-07-22T09:14:02",
  "drafted_against": {
    "vault_sha": "9fc2e11",        // vault HEAD when drafted
    "note": "Meetings/acme.md",    // the one note this plan touches
    "content_hash": "sha1...",     // that note's bytes at draft time
    "entity_ids": ["t-a91f..."]    // optional; reserved for multi-note grouping
  },
  "title": "Normalize 'Acme sync 2026-06-30' to the meeting schema",
  "summary": "Missing id and attendees list; date in prose not frontmatter.",
  "issues": ["no id", "attendees absent", "date not in frontmatter"],
  "apply": [                        // ordered gated-tool calls
    {"tool": "update_frontmatter", "args": {"path": "Meetings/acme.md",
      "key": "id", "value": "m-4be2c91a77d0"}},
    {"tool": "update_frontmatter", "args": {"path": "Meetings/acme.md",
      "key": "attendees", "value": "rob, dana"}}
  ],
  "resolved": null                  // {ts, decision} once acted on
}
```

The `apply` plan is expressed **entirely in existing gated tool calls** —
no new mutation surface. A proposal is data describing tool calls, and
approving it executes them through the exact path a chat turn would:
loop guard, git snapshot, write gating all apply unchanged.

### Staleness — when is a stored proposal still safe to apply?

Drafted Sunday, approved Tuesday, note edited Monday: blind-patching here
is the silent-corruption pathway this design must not open. Two guards,
both cheap:

1. **Vault-SHA fast path.** Vault HEAD still equals `drafted_against
   .vault_sha` and the working tree is clean ⇒ nothing changed ⇒ apply.
2. **Content-hash slow path** *(as built — simpler and strictly stronger
   than the originally-specced entity-id re-resolution for single-note
   plans)*: HEAD moved ⇒ re-hash `drafted_against.note`; unchanged ⇒
   only other notes moved, apply. Missing or changed ⇒ `stale`, never
   applied. There is no explicit re-draft button: a stale or rejected
   proposal leaves its note uncovered, so the **next scan re-drafts it
   naturally** against current content. `entity_ids` stays in the schema
   for future multi-note grouping plans, where per-entity re-resolution
   becomes necessary again.

Queue hygiene: `pending` proposals whose scan no longer reports the
underlying issue (user fixed it by hand, or in chat) are auto-resolved as
`stale` on the next scan. Resolved proposals prune after N days like
chats.

## 3. Grouping — scope-limited v1, boundaries from entity types

### Why schemas gate grouping

Semantic similarity alone cannot know that two projects on one kanban
board must never have their tasks mixed — they might be different
customers. Entity structure knows it deterministically. So candidate sets
are built **in code, from entity boundaries, before any model runs**:

- v1 candidate sets contain **tasks only** — never kanban cards, never a
  mix of entity types.
- A candidate set never spans a kanban board or column (v1: boards are
  excluded entirely).
- **Grouping is only offered on fully stamped scopes.** Every task in the
  set must carry a `^t-` id (proposals address members by id; staleness
  detection needs them). This forces the pipeline order — conformance
  first, grouping unlocked per-scope as conformance lands — and it is
  also the debuggability decision: troubleshooting why grouping fails
  across entity types while working within one type is a rabbit hole v1
  refuses to enter. One entity type, one scope, dialed, **then**
  broadened.

### G1 — organize one list (within a single note/section)

Input: one train-of-thought task list (a brain-dump note, a daily-note
carryover). Candidate set: `iter_tasks` over that note/section. Drafter
output (jsonschema-validated):

```json
{"groups": [{"name": "Review X detection",
             "member_ids": ["t-a91f", "t-b22c"]},
            {"name": "Ungrouped", "member_ids": ["t-9d01"]}]}
```

Hard validation in code before it becomes a proposal: `member_ids` must
be exactly a partition of the candidate set — every input task appears
exactly once, no invented ids, no dropped tasks (the same
silently-missing-items-are-failures policy as the extraction worker,
DESIGN-mixed-models.md §7.3). Apply plan: rewrite the section with
grouped headings via `replace_section` — reordering and heading
insertion only, task lines byte-identical (ids included).

### G2 — consolidate near-duplicates (across notes, tasks only)

The "5 versions of review X detection" case — inherently cross-note
(they landed in different daily notes over weeks), still single
entity-type. Two-stage to keep the LLM honest:

1. **Tier-0 candidate pre-filter, no LLM:** normalized-token-overlap /
   `difflib` similarity across open tasks; only clusters above a
   threshold become work items. Deterministic, testable, and bounds what
   the model ever sees.
2. **LLM confirm + draft:** the drafter sees one cluster and answers:
   same intent or coincidence? If same: a consolidated task list (which
   wording survives, which duplicates close). Apply plan: `add_task` for
   the consolidated entries (or a new note via `create_note`),
   `set_task_status`-close the duplicates by id. Every original task is
   either kept, merged (named in the summary), or left alone — never
   silently dropped.

G2 ships after G1's eval cases are dialed, behind its own gate.

### Grouping evals

Fixture cases under `evals/cases/workers/grouping*.yaml` (the §7.4
worker-eval machinery from DESIGN-mixed-models.md, already built as
`harness-worker-eval`): input task lists + expected partitions, scored
structurally. Regression case #1: two customers' similarly-worded tasks
in one fixture must **not** be grouped when their notes' entity metadata
differs — the boundary rule as an executable test.

## 4. Dashboard surface — SUPERSEDED by DESIGN-vault.md §5, built that way

No web pane was built and none will be. The dashboard is
`Harness/Review.md`: per pending proposal — title, summary, issues, a
` ```diff ` preview simulated against the *current* note text
(`applier.preview`), and `- [ ] approve p-…` / `- [ ] reject p-…`
checkbox lines. Renders are deterministic (no timestamps), written only
on change, and committed as their own snapshot (`snapshot_before` first,
so user edits are never swept into a harness commit). While
`harness-web` runs, it stats Review.md every 5s and runs a watcher pass
when the file changed; `uv run harness-watch` is the one-shot/cron
entry. Approved plans execute in-process through the gated vault
operations (applier, DEVPLAN B8); their commits land in ↺ Changes like
any other mutation.

## 5. Corpus integration — the dashboard is a label factory

Every approve/reject calls the corpus module (DESIGN-training-corpus.md)
with `source: "dashboard"`, the proposal JSON, and the decision. These
are *cleaner* labels than chat-confirm rows — each endorses one
self-contained proposal rather than a whole trajectory — which directly
answers the label-coarseness concern in that design's review. Same
never-fail-the-feature-over-telemetry convention (`try/except OSError`).

## 6. Rollout phases — each gated, each with a one-line rollback

**D0 — queue + schema_fix + surface, manual scan. ✅ machinery built;
usage gate open (10 real proposals reviewed, zero stale-applies).**
Build: proposal store, watcher Stages 1–2 for `schema_fix` only,
`harness-watch` CLI, pane with approve/reject.
Gate: 10 real schema_fix proposals reviewed; zero stale-applies (audit:
every applied plan matched the live note); user actually uses the pane.
Rollback: don't open the pane; delete `data/proposals/`.

**D1 — poll trigger + staleness slow path + auto-stale hygiene.
✅ machinery built and unit-tested; needs one real stale occurrence.**
Build: pane-open trigger + git memo; entity-id staleness re-resolution;
auto-stale hygiene.
Gate: repeated pane opens with unchanged vault run zero scans (memo
works); a hand-edited note between draft and approve reliably flips its
proposal to `stale` (test + one real occurrence).
Rollback: revert to manual `harness-watch`.

**D2 — grouping G1 (within-note, tasks only, stamped scopes only).
✅ machinery built and unit-tested (DEVPLAN 5.2); usage gate open.**
Gate: grouping eval cases pass including the customer-boundary
regression case; 5 real G1 proposals with ≥3 approved (if the user
rejects most groupings, the drafter isn't dialed — stay in D2).
Rollback: disable the `grouping` work-item builder (config flag).

**D3 — grouping G2 (cross-note consolidation). ⏳ not built.**
Gate: tier-0 pre-filter precision checked by hand on the real vault
(clusters shown are mostly real duplicates); same partition-integrity
evals; corpus rows show approvals dominating.
Rollback: same flag.

**Later / out of scope:** cross-entity-type grouping; kanban-card
grouping; post-chat triggers; any auto-apply tier (even for "trivial"
fixes — the whole point is the human gate); embedding-based candidate
generation (revisit only if tier-0 similarity provably misses real
duplicate clusters).

## 7. Failure modes → designed response

| Failure                                   | Response                       |
|-------------------------------------------|--------------------------------|
| Vault changed between draft and approve   | SHA + entity-id staleness ⇒ `stale`, re-draft offered, never blind-apply |
| Drafter emits invalid ids / drops a task  | Partition check in code rejects before it ever becomes a proposal |
| Grouping mixes semantic boundaries        | Candidate sets built structurally in code; boards excluded; eval regression case |
| Watcher hammers the machine               | Git memo (no-op on unchanged vault) + `N_max` drafts/run + never during chat generation |
| Apply fails mid-plan                      | Gated tool path ⇒ each step git-snapshotted; partial application visible in ↺ Changes and revertible per-commit |
| Queue rot (fixed-by-hand issues linger)   | Auto-stale on rescan; resolved proposals pruned after N days |
| User ignores the pane                     | Fine — that is the design. Proposals expire stale; nothing ever lands unapproved |
