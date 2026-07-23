# Dev plan: processor engine build-out (Tezcat)

*Status 2026-07-22: **ADOPTED**, supersedes the vault-as-UI dev plan.
That plan's Phases 0–5 are **shipped** (193 tests green: foundations,
training corpus, entity schemas, worker registry, dashboard, capture
surface + grouping G1 — as-built detail lives in README and git
history). This plan builds the direction ratified in
DESIGN-philosophy.md: the watcher generalizes into a processor engine,
the runtime goes headless, and inline `//commands` become the pseudo-chat.
Spec: DESIGN-processors.md. The dashboard pipeline
(DESIGN-review-dashboard.md) and vault spec (DESIGN-vault.md) stand.*

*Carried-over gate, unchanged and still open: the **D0/D1/D2 usage
gates** (~10 real schema proposals + ~5 grouping proposals reviewed live
in Obsidian, one real stale occurrence, live `harness-worker-eval -k
grouping` run). Machinery phases (7–8) may proceed; anything that
widens write scope (Phase 11) may not start until these pass.*

*Greenfield rules, unchanged: no backward-compat obligations; rewrite,
don't wrap; each primitive built exactly once.*

---

## The building blocks (build once, consume everywhere)

Blocks B1–B8 from the previous plan exist and are consumed as-is. New:

| Block | Lives in | Consumed by |
|---|---|---|
| B9 `ProcessorSpec` + `PROCESSORS` registry (scan/draft pairs, per-processor disable flags) | `src/harness/processors.py` | engine passes; every current and future processor (schema_fix, grouping, capture, commands, research) |
| B10 engine: `run_once` rewritten around B9 — decisions → registry scan → bounded draft → render → memo, plus the **pass lock** (`flock` on `data/watcher_state.lock`) | `src/harness/watcher.py` (rewritten) | `harness-watch`, daemon, web loop |
| B11 trigger layer: whole-vault mtime walk + 3s quiescence debounce, one shared loop | `src/harness/watcher.py` daemon mode | `harness-watch --daemon`, web server loop (delegates, deletes its surface-only stat poll) |
| B12 command scanner + anchored line-rewrite primitive (ack → result → cancel semantics, content-hash memo) | engine machinery in `watcher.py`/`processors.py` | `//tasks`, `//group`, `//tidy`; later `//research` |
| B13 Changes renderer: `git log` + B4 trailer → `Harness/Changes.md` | `src/harness/changes.py` | Changes surface; nothing else |
| B14 `SearchSpec`: query → ranked URLs (backend chosen at build time) | `src/harness/search.py` | `//research` only — **not built until Phase 11** |

Anti-repetition decisions, fixed now:

- **One engine entry point** — every trigger calls `run_once`; no client
  grows its own scan/draft/apply logic. The web loop becomes a delegate.
- **One write-back rule** — processors return proposals; the applier and
  the B12 ack rewriter are the only two vault-writing code paths.
- **One disable idiom** — per-processor flags via B9; `--no-grouping`
  is deleted in favor of `--disable grouping`.
- **One memo mechanism** — the content-hash memo generalizes from the
  capture surface to command lines; no per-feature seen-state schemes.

---

## Phase 6 — Ratify the direction (docs) ✅ THIS COMMIT

- **6.1** `DESIGN-philosophy.md` (tightened: principles, invariants I1–I5,
  corrected web-as-admin-console table, explicit exclusions).
- **6.2** `DESIGN-processors.md` (registry, triggers, commands, consent
  classes, v1/v2 split).
- **6.3** Supersession notes: DESIGN-vault.md §1 table → philosophy doc;
  README roadmap → this plan; this DEVPLAN rewritten.

**Done when:** doc set is coherent — no doc claims approvals live in the
web UI; README points here.

## Phase 7 — Processor registry (rewrite the watcher, change nothing) ✅ BUILT

- **7.1** `processors.py` (B9): `ProcessorSpec`; migrate `schema_fix`,
  `grouping`, `capture` as the first three entries. Drafter injection
  points (used by every existing test) move to registry construction.
- **7.2** Engine rewrite (B10): `run_once` iterates the registry
  (capture-class first — the starvation rule survives the refactor);
  pass lock added; `--disable <name>` replaces `--no-grouping`.
- **7.3** Tests: `test_watcher.py`, `test_capture_surface.py`,
  `test_grouping.py` green with only constructor/import updates —
  behavior byte-identical. New: registry dispatch, pass-lock contention
  (second `run_once` skips), disable flag per processor.

**Done when:** full suite green; `watcher.py` contains no processor
names in its control flow; a fourth processor is addable without
touching the engine.

**As built (2026-07-23):** scan/draft pairs moved to `processors.py`;
per-processor behavior rides on ProcessorSpec hooks (`ensure` creates
surfaces, `on_applied` clears capture, `render_diff` suppresses the
capture diff) so the engine's control flow is name-free. The default
worker drafters stayed in watcher.py as the injection points tests
script (`w.drafter`/`w.grouping_drafter`/`w.capture_extractor`).
`Watcher(registry=...)` overrides the whole set — the fourth-processor
test runs an unknown kind end to end (scan → Review.md → checkbox →
apply → hook). proposal.yaml's `kind` enum relaxed to a pattern: the
registry, not the schema, is the authority on which processors exist.
Pass lock is an `flock` beside the state file; a contended pass returns
`locked: True` and does nothing. Pre-existing failures fixed en route:
test_capture's stale `Captures/` dir (2026-07-22 Meetings cleanup) and
grouping tests' n_max=10 (fixture restructure grew the schema backlog
past it — starvation by design, tests now use 20).

## Phase 8 — Headless runtime ✅ BUILT (Rust daemon)

- **8.1** Daemon mode (B11): `harness-watch --daemon` — mtime walk,
  quiescence debounce, tick ≤5s, pass on settle. Excludes `.git`,
  `.obsidian`, and honors the self-write guard.
- **8.2** Web loop delegates to B11's shared code; the surface-only
  stat poll is deleted.
- **8.3** launchd template `config/launchd/com.tezcat.watch.plist` +
  README setup section (login-start, log path). This also closes the
  "real-vault onboarding: launchd auto-start" roadmap item.
- **8.4** Tests: debounce (fresh mtime defers, settled mtime fires),
  daemon+web contention via the pass lock, edit-a-note→proposal
  end-to-end with no web server constructed.

**Done when:** with only the daemon running and Obsidian open, editing a
nonconforming note produces a Review.md proposal within ~15s, and
running the web server alongside causes zero duplicate passes.

**As built (2026-07-23):** the daemon is `daemon/` — **tezcatd**, a
~250-line zero-dependency Rust binary (a deliberate indulgence that
costs nothing: its whole contract is "watch a tree, wait for quiet,
spawn `uv run harness-watch`", so it never touches the Python iteration
loop; if the binary vanishes, cron does the same job). Mtime-walk
fingerprint every 5s over the whole vault (hidden dirs, `.venv`,
`__pycache__` invisible), 3s quiescence settle so a pass never fires
mid-keystroke, re-baseline after each pass so the engine's own
writes/commits never re-trigger, one startup pass to absorb
daemon-was-down edits. Concurrency with the web server needs no
coordination: the engine's pass lock (Phase 7) already serializes
triggers. Deviation from 8.2: the web loop's surface poll was **kept**,
not deleted — it now derives its watch set from the engine
(`Watcher.trigger_paths()`) and gives snappier checkbox pickup while
the server runs; the daemon owns whole-vault coverage. `cargo test`
covers fingerprint stability/motion, skip-dir invisibility, and the
settle gate. launchd template: `config/launchd/com.tezcat.watch.plist`.

## Phase 8.5 — The vault describes itself ✅ BUILT (2026-07-23)

Unplanned, pulled forward from live use: the spec lived repo-side while
the vault drifted (the sidebar-vs-fixture divergence made it obvious).
Now `Harness/Spec.md` frontmatter is the live spec (folders, inboxes,
exclusions) — seeded once from `config/vault.yaml`, user-owned, re-read
each pass via an mtime-keyed cache in `vaultconfig.get_config(vault)`;
surfaces/schemas_dir stay repo-side. A fourth processor, **spec_drift**
(deterministic draft, no worker — the registry made it ~60 lines),
proposes mapping new note-bearing top-level folders as `other`; real
types are hand edits to Spec.md. Spec.md joins `trigger_paths()` so
edits take effect promptly while the server runs. Tests:
`tests/test_spec_note.py` (seed-once/user-owned, live overrides,
drift→approve→map-updated→no-redraft, empty folders aren't drift).

## Phase 8.6 — Inbox extraction ✅ BUILT (2026-07-23)

Pulled forward from live use (free text typed into `Tasks/Inbox.md` did
nothing — a reasonable expectation the product promise implies). New
processor **inbox_extract**: loose lines (non-checkbox, non-heading) in
the capture inbox's first prose-bearing section → one `extract_tasks`
call → a proposal that rewrites the section in place (existing task
lines byte-identical, prose replaced by stamped tasks; owed items
`add_task`'d to the waiting list). Rejection memo (`inbox_seen`) mirrors
capture's. Came with an engine fix: the one-proposal-per-note invariant
now dedupes **within** a pass too (`seen_notes` in run_once) — two
processors wanting the same note resolve by registry priority, which was
a latent double-draft bug for schema_fix+grouping before. Free text
anywhere else stays Phase 9's `//tasks`.

## Phase 8.7 — Verified auto-apply ✅ BUILT (2026-07-23)

User decision: mechanical transformations shouldn't cost a checkbox —
"a list turning into a formatted list probably doesn't need my approval
if we can test for making sure it uses my [words] accurately." That test
is exactly what shipped: `ProcessorSpec.verify` (deterministic, per
proposal) + a Spec.md `auto_apply` trust list (default: capture,
inbox_extract). The extraction verifier checks every produced task is
built from the user's own words (`_words` ⊆ source; dates/priorities
live in structured fields) and any rewritten section keeps pre-existing
task lines byte-identical. Pass ⇒ apply immediately (staleness-checked,
snapshotted, status `auto`, no corpus row — not a human label); fail ⇒
the proposal keeps its checkbox. Grouping/schema/spec_drift have no
verifier and always queue. Philosophy I2 amended accordingly. This
raises the stakes on Phase 10 (Changes.md): auto-applies make "what did
it just do" visibility non-optional.

Follow-up same day — **`fidelity: strict | close`** (Spec.md, default
close): pure edit-distance is unsafe ("do X"→"don't X" scores ~0.92),
so `close` is layered — meaning-flippers (not/never/delete/all/…) never
addable, other additions must be grammatical filler or a morphological
variant of the user's words, and the produced text must be ≥70%
sequence-covered by its best source line (catches same-words mashups).
Grammar and punctuation polish auto-applies; semantics changes queue.

## Phase 8.8 — Additive-shape verifier: schema_fix joins auto-apply ✅ BUILT (2026-07-23)

Generalizes 8.7 per the user's "one page with a defined shape" instinct
— WITHOUT per-entity-type builds, because shape is data
(config/schemas/) and the verifier is one function for all types.
`verify_schema_fix`: simulate the whole plan in memory
(`applier.simulate`), then require (a) every step simulable, (b) every
user-written body line byte-identical modulo appended block refs, in
order, (c) added lines are headings only, (d) existing frontmatter
values unchanged (keys may only be added), (e) the result FULLY
conformant per `server.note_text_issues` (extracted from
validate_schema) — a partial fix would re-draft every pass, so it
queues. Net effect: id stamping and skeleton additions self-apply for
every current and future entity type; anything that touches a user's
word, or doesn't finish the job, keeps its checkbox. Grouping stays
queued on principle: its verifier could prove *harmlessness* but not
*desirability* — a structurally perfect bad grouping is still noise,
and semantic judgment is the user's (their 8.7 rule).

## Phase 9 — Inline commands v1 (queue-routed only)

- **9.1** Command scanner + ack/result/cancel line machinery (B12):
  regex per spec §3, hash memo, anchored rewrite, worker-down result
  line. Engine-owned; snapshotted writes only.
- **9.2** `//tasks` and `//tidy` processors — both reuse existing
  workers (`extract_tasks`, `schema_fix`) with zero new prompts.
- **9.3** `//group` — scoped to the section containing the command line;
  same partition-integrity enforcement as G1.
- **9.4** Tests: full round-trip in a tmp vault (type → ack → proposal →
  approve → result line), cancellation by edit and by delete, memo
  (result line never re-fires), `Harness/`+`Private/` blindness.
  Eval impact: none — no new workers, existing case files stand.

**Done when:** in live Obsidian: type `//tasks` under meeting scraps in
a daily note, ack ≤15s, approve in Review.md, tasks land in the inbox,
result line links there. `//research` typed anywhere yields
`⚠ unknown command` — the scanner fails loud, not silent.

## Phase 10 — `Harness/Changes.md` (visibility for a headless system)

- **10.1** B13 renderer: last ~20 vault commits — subject, relative
  time, files touched, chat/proposal attribution via the B4 trailer.
  Rendered at the end of every pass that committed anything; excluded
  from scans/candidates like all surfaces; **no revert checkbox**
  (explicitly-cut list stands).
- **10.2** Tests: render determinism, trailer attribution, self-render
  doesn't loop the fingerprint.

**Done when:** "what did it just do" is answerable in Obsidian alone.

## Phase 11 — `//research` + direct result blocks (v2) — GATED

*Gate: D0/D1/D2 usage gates passed AND v1 commands used in real notes
for a couple of weeks without consent surprises. Do not start early.*

- **11.1** B14 `SearchSpec` + backend decision (SearXNG vs Brave —
  decided then, not now).
- **11.2** Research worker: query → ranked-URL Sources block. Output
  contract fixed by spec §3: links, never paraphrase.
- **11.3** First consent-class-2 write: result block below the command
  line, same note, snapshotted; everything else still queues.

---

## Explicitly cut / frozen (so nothing hazardous creeps in)

- **No worker chaining.** One transformation, one worker, one eval
  (philosophy principle 4).
- **No external-service processors** — `//jira`, webhooks, anything with
  credentials or egress. Needs its own design doc first. (The old
  roadmap's "Jira via Atlassian remote MCP" item moves behind this
  gate.)
- **Chat is frozen** — no features, no deletion, no engine-client
  refactor of agent.py yet. Web UI = admin console; no new panels.
- **No checkbox-revert** in Changes.md.
- **No cross-note grouping (G2)** until G1's D2 gate passes (unchanged).
- **No script execution** (future DESIGN-runtime.md, unchanged).
- **No mixed-model routing** (separate unapproved track, unchanged).

## Suggested checkpoints (commit/PR granularity)

Each phase is 1–2 PRs; every PR leaves the full suite green. Natural
seams: 6 (docs) / 7.1–7.3 / 8.1–8.4 / 9.1 / 9.2–9.4 / 10 / 11. Phase 7
is the load-bearing refactor and merges alone; 8 and 9 both depend on 7
but not on each other (9 can ride the existing web-loop trigger if 8
slips); 10 is independent after 7; 11 is gated, not scheduled.
