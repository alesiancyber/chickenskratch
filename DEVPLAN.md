# Dev plan: vault-as-UI build-out

*Status 2026-07-22: **Phases 0–4 BUILT** — 157 tests green, ruff clean.
Phase 5 waits on its gate (D0/D1 dialed with real usage). Per-phase
deviations from spec are noted inline as **As built:** lines.*

*Scope: the three approved features — **vault schema**, **review
dashboard**, **training corpus** — plus every shared
building block they need. Specs: DESIGN-vault.md (foundation),
DESIGN-vault-schema.md (mechanics), DESIGN-review-dashboard.md (pipeline),
DESIGN-training-corpus.md (capture). Mixed-model routing is NOT in scope;
only the worker-framework subset the dashboard needs is.*

*Greenfield rules: no backward-compat obligations. Refactor at will —
rewrite, don't wrap. Each primitive is built exactly once, in one place,
and everything consumes it from there.*

---

## The building blocks (build once, consume everywhere)

| Block | Lives in | Consumed by |
|---|---|---|
| B1 `jsonschema` validation (dep + one `validate(instance, schema) -> list[str]` helper with human-readable errors) | `src/obsidian_mcp/jsonval.py` | entity schemas (F-schema), worker outputs (F-dashboard drafters + extract_tasks), proposal-file self-validation (F-dashboard) |
| B2 `config/vault.yaml` + loader: folder→entity-type map, schema paths, `Harness/` surface paths, `Private/` exclusions | `src/obsidian_mcp/vaultconfig.py` | `_note_type` replacement, validate_schema, watcher scan scoping, read exclusions |
| B3 id primitives: `stamp_id(kind)`, block-ref regex, strip/attach | `src/obsidian_mcp/ids.py` | stamping (schema), proposal addressing + staleness (dashboard), grouping members |
| B4 Chat-Id plumbing: env var → commit trailer → `commit_message(sha)` | agent.py / mcp_client.py / vault.py | corpus revert attribution; later Changes.md |
| B5 read exclusions at the vault layer (one choke point, not per-tool) | vault.py `iter_notes`/`read` | `Private/` guarantee; `Harness/` scan exclusion |
| B6 worker registry: `WorkerSpec` (prompt, schema, timeout, temperature) + one bounded runner with B1 validation, 1 retry | `src/obsidian_mcp/workers.py` (rewritten) | extract_tasks (migrated), schema_fix drafter, grouping drafter |
| B7 corpus envelope: one generic `_append(rec)` + per-source constructors incl. `source: "dashboard"` from day one | `src/harness/corpus.py` | confirm/bug/revert labels; dashboard approve/reject labels |
| B8 in-process applier: proposals' apply steps call the vault operations directly (same venv — the git-snapshot guarantees live in vault.py, so they hold without an MCP round-trip; the checkbox IS the confirm) | `src/harness/applier.py` | dashboard apply; nothing else calls it |

Anti-repetition decisions, fixed now:
- **One validation idiom** — jsonschema via B1 for *everything* structured
  (entities, worker replies, proposal files). No ad-hoc `json.loads` +
  manual checks anywhere after Phase 3.
- **One typing source** — B2's folder map. `_note_type()`'s hardcoded logic
  is deleted, not shadowed.
- **One id format** — B3. No inline `uuid` calls in tool functions.
- **One attribution mechanism** — B4's trailer. Anything that ever asks
  "which chat did this commit" reads the trailer.
- **Vault surfaces are rendered views** — `data/` JSON stays authoritative;
  `Harness/*.md` is regenerated, never parsed back except for checkbox
  lines (which carry proposal ids precisely so parsing them is trivial).

---

## Phase 0 — Foundations (everything else stands on this) ✅ BUILT

Order within phase is the dependency order; 0.4 is independent and can be
done anytime in the phase.

- **0.1** Add `jsonschema` to pyproject; `jsonval.py` (B1). Tests: error
  strings are human-readable, nested paths named.
- **0.2** `config/vault.yaml` + `vaultconfig.py` (B2). Rewrite server.py's
  `_note_type` to read it; delete the hardcoded fallback chain (unmapped
  folder → `"other"` stays). Tests: mapping, unmapped folder, missing file
  → sane defaults.
- **0.3** `ids.py` (B3). `tasks.py`: `Task` gains `block_id`; `TASK_RE`
  path strips a trailing `^ref` so display/match text never contains it;
  `format_task` gains optional id append. This is a **parser change, not a
  wrapper** — every consumer (list_tasks, set_task_status, kanban card
  matching) sees clean text. Tests: round-trip, match-with-id, no-id lines
  byte-identical to today.
- **0.4** Chat-Id plumbing (B4), exactly per DESIGN-training-corpus.md
  steps 1–6: `HARNESS_CHAT_ID` env through `Agent` →
  `MultiServerClient._connect`; `snapshot_after` trailer;
  `Vault.commit_message(sha)`. Tests per that doc (trailer present/absent,
  `snapshot_before` never stamped, env absent = today's behavior).
- **0.5** Read exclusions (B5) in vault.py, config from B2: excluded paths
  are invisible to `iter_notes`, error on `read`. `Private/` ships in the
  default vault.yaml. Tests: search/list/read/list_tasks all blind to it.
- **0.6** Vault scaffold: `.gitignore` (`Scripts/**/.venv/`,
  `__pycache__/`), `Harness/`, `Private/`, `Deliverables/` in testvault;
  testvault gains deliberately-nonconforming fixture notes (used by Phase
  2 and 4 tests).

**Done when:** full existing test suite green, plus new foundation tests.
No behavior change visible in a chat except `Private/` invisibility.

**As built:** foundation tests live in `tests/test_vault_spec.py`. The
folder-derived task type renamed `tasks`→`task` (one assertion updated).
`Private/` exclusion sits in `Vault.resolve`/`iter_notes` — one choke
point covering reads, searches, listings, writes, and fuzzy suggestions.

## Phase 1 — Training corpus (complete feature; parallel with Phase 2) ✅ BUILT

Starts as soon as 0.4 lands. Data evaporates on the 3-day prune — this
ships early on purpose.

- **1.1** `corpus.py` (B7) with the generic envelope; constructors:
  `record_confirm` (with `timed_out`), `record_bug_report`,
  `record_revert`, `record_dashboard` (stub-tested now, wired in Phase 4 —
  the envelope is designed once, so the dashboard needs zero corpus
  rework).
- **1.2** web.py wiring per DESIGN-training-corpus.md: `_confirm`
  timed-out tracking + record; `report_issue` record; revert route reads
  the trailer via `commit_message`, loads the chat, records if present,
  silently skips if pruned (the accepted gap).
- **1.3** Tests exactly as specced in that doc (test_corpus.py, test_web
  confirm/timeout/report/revert paths, test_mcp_client env test).

**Done when:** the doc's manual smoke passes — approve, deny, bug report,
revert → four labeled rows in `data/corpus/<month>.jsonl`, revert row
proving the trailer round-tripped through a real subprocess.

**As built:** `record_dashboard` rows carry the proposal as payload
instead of a trajectory (the append gate accepts either). The manual
smoke via a live chat remains to be run once — unit/route tests cover
every path including the timeout distinction.

## Phase 2 — Entity schemas ✅ BUILT

- **2.1** `config/schemas/`: `task.yaml`, `deliverable.yaml` (task +
  required `for` + required `due`), `meeting.yaml`, `research.yaml`
  (required Sources section), `kanban.yaml` (optional `project`/`client`),
  `card.yaml`.
- **2.2** `schemas.py`: load-once via B2 paths; `validate_frontmatter`,
  `validate_task_line`, `validate_card_line` — all through B1.
- **2.3** Stamping at creation via B3: `create_meeting_note`,
  `create_board`, `add_task`, `add_card`. New entities are born
  conformant.
- **2.4** `validate_schema` read-only tool: paginated, unmarked entities
  first, stamped entities still re-checked (drift caveat). Implementation
  is a plain function the Phase 4 watcher imports directly; the MCP tool
  is a thin wrapper over it.
- **2.5** Tests: test_schemas.py per-type valid/invalid; test_tools.py —
  fixture nonconforming notes flagged, born-stamped assertions,
  set_task_status still matches stamped lines; deliverable missing
  `for`/`due` flagged while identical task line passes.

**Done when:** `validate_schema` over testvault reports exactly the
planted nonconformances and nothing else; all creation tools stamp.

**As built:** deliverable `for` is a Dataview inline field
(`[for:: Name]`) parsed by schemas.py; kanban.py's parser/renderer
learned card block refs so `read_board` shows clean text plus `id`; YAML
date objects are normalized to ISO strings before validation; research
notes additionally require a `## Sources` section
(`validate_research_body`). Result rows carry an `unmarked` flag and
unmarked sorts first.

## Phase 3 — Worker registry (rewrite, not wrap) ✅ BUILT

- **3.1** Rewrite `workers.py` around `WorkerSpec` + one `run_worker`
  (B6): B1-validated output, per-spec timeout/temperature, one retry,
  fail as `VaultError`. **Migrate `extract_tasks` into the registry and
  delete its bespoke plumbing** — including the manual cleanup loop that
  silently drops malformed items (schema failure now fails loud, per the
  silently-missing-items-are-failures policy). Extraction schema returns
  someday items with `due: null` (the policy fix), with an eval case.
- **3.2** `harness-worker-eval` keeps working against registry workers;
  existing extract_tasks eval cases must pass unchanged (plus the new
  someday case).

**Done when:** worker eval suite green through the registry; no non-registry
LLM call exists in obsidian_mcp.

**As built:** silently-dropped malformed entries now fail loud
(`schema-invalid` VaultError) — two unit tests updated to assert the new
policy. `WORKER_TIMEOUT_S` overrides the spec default per call. The
someday-item eval case already existed and passes unchanged. `votes:`
is NOT implemented (deferred with the rest of mixed-models P4).

## Phase 4 — Dashboard (queue → watcher → Review.md → checkbox) ✅ BUILT

- **4.1** Proposal store: `data/proposals/<id>.json` per
  DESIGN-review-dashboard.md §2, **self-validated against
  `config/schemas/proposal.yaml` via B1** (dogfooding); lifecycle
  pending/approved/rejected/stale; prune like chats.
- **4.2** Watcher stage 1 (`src/harness/watcher.py`): git memo
  (`data/watcher_state.json`, HEAD sha + dirty hash), scan = the Phase 2
  validate function + B2 scoping (`Harness/` and `Private/` excluded);
  emits `schema_fix` work items, `N_max` cap.
- **4.3** `schema_fix` drafter as a B6 WorkerSpec: input = note +
  its issues, output = the proposal `apply` plan (jsonschema'd). Plans may
  only reference the whitelisted vault operations.
- **4.4** `Harness/Review.md` renderer: sections per pending proposal —
  title, summary, ` ```diff ` preview, `- [ ] approve <proposal-id>` /
  `- [ ] reject <proposal-id>`. Renderer writes are excluded from
  triggering the watcher (self-write guard by path).
- **4.5** Approve path: checkbox scan (the running web server polls the
  one file; `harness-watch` also checks) → staleness guard (SHA fast
  path; B3 id re-resolution slow path; touched-content check) → applier
  (B8) executes the plan through vault.py's snapshot-guaranteed ops →
  section rewritten as applied/stale → `record_dashboard` (B7). Apply is
  idempotent per proposal id.
- **4.6** `harness-watch` CLI entry point (scan + draft + apply-checked,
  one shot).
- **4.7** Tests: end-to-end in a tmp vault with a scripted fake drafter
  (FakeOpenAI pattern) — nonconforming note → proposal → rendered
  Review.md → check the box → applied + stamped + corpus row; staleness:
  hand-edit between draft and check → `stale`, nothing applied; memo:
  unchanged vault → scan no-ops; self-write guard: rendering doesn't
  retrigger.

**Done when:** DESIGN-review-dashboard.md D0+D1 gates pass with the
checkbox flow live in Obsidian against the real testvault (10 real
proposals reviewed, zero stale-applies, memo verified).

**As built:** machinery complete and tested end-to-end with a fake
drafter (`tests/test_watcher.py`); the D0/D1 *usage* gates (10 real
reviewed proposals, one real stale occurrence) still need live mileage.
Deviations: staleness uses a note **content-hash** slow path instead of
entity-id re-resolution — simpler and strictly stronger for single-note
plans (`entity_ids` stays in the proposal schema for future multi-note
grouping); there is no explicit re-draft checkbox — a stale/rejected
proposal leaves its note uncovered, so the next scan re-drafts it
naturally; the web poll is one `stat()` of Review.md every 5s, full pass
only on change, enabled only in `harness-web`'s real server (never in
tests); drafting halts for the pass when the worker model is down;
resolved proposals prune after 30 days.

## Phase 5 — Capture surface + grouping G1 (after 4 is dialed) ⏳ GATED

- **5.1** `Harness/Capture.md`: watcher treats appended content as capture
  input → extract_tasks (registry) → proposals in Review.md. Web capture
  buttons untouched and frozen.
- **5.2** Grouping G1 per DESIGN-review-dashboard.md §3: candidate builder
  (tasks-only, single note/section, fully-stamped scopes only), grouping
  drafter WorkerSpec, **partition integrity enforced in code** (every id
  exactly once, none invented), apply via `replace_section` with
  byte-identical task lines. Eval cases incl. the customer-boundary
  regression. Gate D2 before G2 is even started.

---

## Explicitly cut / frozen (so nothing half-hazard creeps in)

- **No new web UI.** No proposals panel, no new panels at all. Web =
  chat, confirm, servers, evals/stats, existing capture buttons.
- **No routing/cascade/classifier** (mixed-models P0–P2) — separate,
  unapproved track.
- **No checkbox-revert in Changes.md** and no Changes.md yet — additive
  checkbox actions must earn trust first.
- **No cross-note grouping (G2), no cross-entity grouping, no
  kanban-card grouping** until G1's gate passes.
- **No script execution.** Scripts/ is content; runtime is a future
  DESIGN-runtime.md.
- **No fine-tune pipeline/export** — corpus is capture-only.

## Suggested checkpoints (commit/PR granularity)

Each phase is 1–3 PRs; every PR leaves the full suite green. Natural
seams: 0.1–0.3 / 0.4–0.6 / Phase 1 / 2.1–2.3 / 2.4–2.5 / Phase 3 /
4.1–4.4 / 4.5–4.7 / 5.1 / 5.2. Phases 1 and 2 may interleave after 0.4;
everything in 4 depends on 2.4 and 3.1.
