# Design: entity schemas and review watermarks (task / card / meeting / kanban)

*Status: **BUILT** (DEVPLAN Phase 2, 2026-07-22) — schemas, ids,
stamping, and `validate_schema` are implemented and tested; as-built
notes inline below. This is the **substrate** for
DESIGN-review-dashboard.md. The entity-type roster and folder→type
mapping live in DESIGN-vault.md (`config/vault.yaml`); this doc owns the
mechanics — schemas, ids, stamping, validation.*

## Context — what the id actually is

Earlier framing of this design led with "ids let tools address entities
instead of matching by text." That is a side benefit, not the point. The
point:

**An id is a review watermark.** The user never writes an id by hand.
An id is stamped on an entity in exactly two ways: the harness creates the
entity (stamped at birth, conformant by construction), or the user
approves a normalization of an existing entity from the review dashboard
(stamped when the approved fix lands). Therefore:

- **No id ⇒ definitely never reviewed.** The set of unmarked entities is
  the exact work surface for the schema-conformance scan — a cheap,
  deterministic filter, no LLM judgment needed to find the frontier.
- The frontier is **monotonic**: it only ever shrinks. The vault converges
  on conformance incrementally, one approved fix at a time, without the
  user ever doing cleanup by hand.

The goal is enforced consistent structure across tools and entity types
**without the user doing the cleanup and without unsupervised rewriting**:
detect (code) → draft a fix (LLM, read-only) → queue it → the user
approves at convenience → the write lands through the normal gated tools.
The queue/approval half lives in DESIGN-review-dashboard.md; this doc
defines what "conformant" means and how the watermark works.

### The drift caveat (load-bearing, do not lose it)

**An id proves past review, not current conformance.** The vault is
hand-edited in Obsidian; a stamped note edited by hand next month can
drift back out of schema. So:

- `no id` → unreviewed, top priority for the scan.
- `has id` → reviewed once; still re-validated by the (cheap, mechanical)
  jsonschema pass on scan, just at lower priority and with the id kept.
- Nothing anywhere may treat `has id` as `is currently valid`.

### Second role: ids are how proposals address entities

The review dashboard stores drafted proposals that get applied *later*,
against a vault that may have changed. Proposals reference entities by id,
never by text — apply-time lookup survives wording edits, and a vanished
id is an unambiguous "this proposal is stale" signal (see the staleness
design in DESIGN-review-dashboard.md). Text matching cannot provide either
property. This is also why **grouping proposals are only offered on fully
stamped scopes** (dashboard doc §grouping): the ordering
conformance-then-grouping is forced by the addressing mechanism itself.

### Third role: entity types are semantic boundaries

Text embeddings and LLM similarity judgments cannot know that two projects
on a kanban board must never have their tasks mixed — they might be
different customers. The entity type system *can* know it, structurally:
a card belongs to a column belongs to a board; a task belongs to a section
belongs to a note. Grouping candidate sets are built from these boundaries
in code, before any model sees anything (dashboard doc). The schema layer
is what makes fuzzy grouping safe to attempt at all.

## Decisions locked in

- **Scope: per-entity, not vault-wide.** A vault-wide schema fights
  vault-as-API (the vault is hand-edited; the harness can't force
  conformance on content it didn't write). Per-entity schemas generalize
  what `kanban.py`'s `Board`/`Column` dataclasses already do for one type.
- **Schema definition: declarative YAML** under `config/schemas/`,
  validated with `jsonschema` (now an explicit dependency; the shared
  helper is `src/obsidian_mcp/jsonval.py` — the one validation idiom for
  entities, worker replies, and proposal files alike).
- **Reconciliation is read-only detection, never autonomous rewriting.**
  An unsupervised background worker that LLM-guesses fixes into place is
  rejected: it would be a new unsupervised-mutation pathway, violating
  "workers interpret, they never mutate" (DESIGN-mixed-models.md §7.1),
  and "best guess, written back silently" is the invisible
  plausible-but-wrong failure mode — a bad silent rewrite of a months-old
  note might not be noticed for a long time. All fixes land as dashboard
  proposals applied through `confirm_writes`-gated tools.

## Where ids live

- **Note-level entities** (meeting notes, kanban boards): one more
  frontmatter field, `id: <12-hex>`, alongside the existing `date`/`type`/
  `attendees` (meetings) or `kanban-plugin` (boards).
- **Line-level entities** (tasks *and* kanban cards): Obsidian's own
  **block-reference syntax** — `^t-<shortid>` (tasks) / `^c-<shortid>`
  (cards) appended to the line. A first-class Obsidian primitive, not
  custom noise; free side benefit: linkable from anywhere via
  `[[Note#^t-abc123]]`.

Interaction with existing parsing (must be handled, has tests):
`tasks.py`'s `TASK_RE`/`parse_task_line` and the text matchers in
`set_task_status` / `Board.find_card` will now see lines carrying a
trailing block ref. `parse_task_line` strips and returns it as
`Task.block_id`; the display text and match text exclude it, so today's
substring matching behaves exactly as before on stamped lines.

## `config/schemas/*.yaml`

`task.yaml`, `deliverable.yaml`, `card.yaml`, `meeting.yaml`,
`research.yaml`, `kanban.yaml` — each a jsonschema declaration of
required/optional fields and shapes (plus `proposal.yaml`, which is not
a vault entity: it validates the dashboard's own queue files).
Kanban's `Board`/`Column` dataclasses stay as the *parser*; the YAML
schema is an independent check layered on top (e.g. requiring `id` in
board frontmatter), not a replacement.

## Module layout (as built)

- `src/obsidian_mcp/ids.py` — `stamp_id(kind)`, block-ref regex,
  `split_block_ref`/`attach_block_ref`: the one id format.
- `src/obsidian_mcp/schemas.py`:
  - `validate_frontmatter(entity_type, fm) -> list[str]` — jsonschema via
    jsonval, human-readable issues; YAML `date:` objects normalized to
    ISO strings first. Line types (`LINE_TYPES = {task, deliverable,
    card}`) have no frontmatter schema and return `[]`.
  - `validate_line(task, entity_type) -> list[str]` — one entry point for
    all line entities; `line_kind()` picks card on boards, deliverable in
    Deliverables/, task elsewhere. Deliverable `for` is parsed from a
    Dataview inline field `[for:: Name]` on the line.
  - `validate_research_body(body)` — the required `## Sources` section.
- Entity classification reuses `_note_type()` — kanban presence →
  frontmatter `type` → the vault.yaml folder map. No new classifier.

## New read-only tool in server.py: `validate_schema`

```python
@mcp.tool(annotations=READ_ONLY)
def validate_schema(folder: str = "", limit: int = 50, offset: int = 0) -> dict:
    ...
```

Walks `v.iter_notes(folder)`, classifies via `_note_type`, runs the
frontmatter check (plus the research Sources check), iterates
`tk.iter_tasks(body)` for line-level checks. Returns
`{"total_checked": N, "nonconforming_count": M, "shown": K,
"nonconforming": [{"note", "entity", "unmarked": bool, "issues": [...],
"tasks": [{"line", "text", "issues"}]}]}`.
**Paginated** (`limit`/`offset`, same shape as `search_vault`) — a legacy
vault's full nonconforming list would blow past `max_tool_result_chars`.
Entities with ids are still checked (drift caveat) but sort after the
unmarked frontier. Harness surface folders (vault.yaml `surfaces`) are
skipped — machine-rendered views are not entities.

This tool is also the scan primitive the dashboard's watcher calls
directly as a Python function — the MCP tool surface and the watcher share
one implementation.

## Existing write tools get id-stamping (new entities never start unmarked)

- `create_meeting_note` / `create_board` (server.py:421/326): add
  `id: stamp_id("m"|"b")` to the frontmatter at creation.
- `add_task` → `tk.format_task` (tasks.py:70): append ` ^t-<shortid>`.
- `add_card` (server.py:366): append ` ^c-<shortid>` — cards are
  first-class line entities, same treatment as tasks.
- Existing entities without an id are exactly what `validate_schema`
  flags; they get ids only via approved dashboard proposals.

## Demoted to side benefit: id-addressed tool calls

`set_task_status(path, task_text)` and `move_card(path, card)` keep text
matching — hand-created entities will never all have ids, so the text path
must live forever. Once stamping exists, these matchers gain one cheap
extension: an argument that looks like `^t-abc123` matches by block id
first. Nice, small, not the motivation, can ship any time after stamping.

## Tests (as built)

- `tests/test_schemas.py`: per-entity jsonschema fixtures valid/invalid;
  `validate_line` for tasks/cards/deliverables (incl. the
  same-line-passes-as-task check); date-object normalization; research
  body.
- `tests/test_vault_spec.py`: block-ref round-trips (`parse_task_line` →
  `Task.block_id`, `format_task`, `set_task_status` keeps the ref last),
  `stamp_id` format, plus the Phase-0 foundations (config map,
  exclusions, Chat-Id trailer).
- `tests/test_tools.py`: `validate_schema` flags the fixture's
  nonconforming notes/deliverables, unmarked-first ordering, pagination,
  born-stamped assertions for notes/boards/tasks/cards, Private/
  invisibility.

## Deferred / out of scope

- The proposal queue, watcher, dashboard pane, grouping — all in
  DESIGN-review-dashboard.md.
- Bulk one-shot migration of the existing vault — rejected; conformance
  arrives incrementally via approved proposals only.
- The inline diff viewer — separately planned UI; proposals pair with it
  but don't depend on it.
