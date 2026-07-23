# Design: the processor engine — registry, triggers, inline commands

*Status: **APPROVED 2026-07-22**, not yet built (DEVPLAN Phases 7–9).
Engineering companion to DESIGN-philosophy.md; generalizes the watcher
(DESIGN-review-dashboard.md's pipeline, which stands unchanged in
mechanics) from three hardcoded work-item kinds into a registry, adds
the trigger layer that makes the system headless, and specs inline
`//commands`. Nothing here weakens an invariant: detect stays
deterministic, draft stays one bounded worker call, every mutation stays
queue-gated except the command-ack line defined in §4.*

## 1. The abstraction

A **processor** is one transformation with two stages, matching what
`watcher.py` already does implicitly:

```python
@dataclass(frozen=True)
class ProcessorSpec:
    name: str                 # "schema_fix", "capture", "tasks_cmd", ...
    scan: Callable            # (vault, state) -> list[WorkItem]; deterministic, no model
    draft: Callable           # async (item) -> proposal dict | None; ≤1 run_worker call
    # write-back is NOT configurable: drafts land in the proposal queue.
    # The single exception (command ack lines) is engine machinery, §4.
```

`PROCESSORS: list[ProcessorSpec]` lives in `src/harness/processors.py`.
The engine (today's `Watcher`, rewritten around the list) runs one pass:
act on Review.md decisions → if the vault fingerprint moved, collect
`scan()` items across the registry (capture-class processors first, so a
schema backlog never starves active input; then commands, then the
rest) → draft up to `n_max` → render Review.md → memoize. Per-processor
disable flags replace `grouping_enabled` (`harness-watch
--disable <name>`, the generalized rollback switch).

**Migration is a rewrite, not a wrap** (greenfield rule): `schema_fix`,
`grouping`, and `capture` become the first three registry entries with
byte-identical behavior; `test_watcher.py`/`test_capture_surface.py`
must pass with only import/constructor updates. *(As built — and the
registry earned its keep immediately: `spec_drift`, the fourth
processor, landed as ~60 lines with a deterministic draft and zero
engine changes. A `draft` need not call a worker; it must call at most
one.)*

## 2. Triggers — many clients, one engine

`run_once` is the only entry point. Clients:

- **`harness-watch`** — one pass by hand or cron (exists).
- **`harness-watch --daemon`** — the headless default: whole-vault
  change detection loop. Implementation: a cheap poll every ~5s of an
  mtime walk over vault top-level dirs (excluding `.git`, `.obsidian`),
  with a **quiescence debounce** — run a pass only after the newest
  mtime is ≥3s old, so we never extract from a note mid-keystroke. No
  watchdog/FSEvents dependency until the poll measurably costs
  something. Ships with a launchd plist template
  (`config/launchd/com.tezcat.watch.plist`) for login-start.
- **web server loop** — becomes a thin delegate to the same trigger code
  (today it stat-polls only the two surfaces; that special case is
  deleted with the daemon in place).

**Pass lock (new requirement):** daemon + web server may both be
running. `run_once` takes an exclusive `flock` on
`data/watcher_state.lock`; a losing trigger skips its tick (the winner's
pass covers it). The self-write guard stays: the engine ignores its own
writes to `Harness/` when fingerprinting.

## 3. Inline commands — pseudo-chat as document transformation

Syntax: a line matching `^//([a-z]+)(?:\s+(.*))?$` in any note outside
`Harness/` and `Private/`. Detection is a command-scanner processor:
regex over notes in the changed set, memoized by content hash exactly
like the capture surface, so a rejected or completed command never
re-fires.

Lifecycle (the pseudo-chat feel comes from the ack):

1. **Seen** — the engine rewrites the line in place to
   `//tasks … ⏳` and snapshots. This is the "message received."
2. **Drafted** — the processor's one worker call runs; effects become a
   proposal in Review.md (v1: always — see consent classes).
3. **Answered** — the ack line is replaced with a result line:
   `//tasks — ✅ 3 task(s) proposed in [[Harness/Review]]` or
   `//tasks — ⚠ worker unavailable (edit this line to retry)`.
   No retry loops; editing the line changes the hash and re-arms it.
4. **Cancelled** — the user edited or deleted the line before drafting:
   the anchor is gone, the command is dropped. Free undo.

**Anchoring:** the rewrite targets the exact line text, re-located at
write time (never a line number). If the user moved the line, it is
found; if it changed, the command is cancelled. Ack and result writes go
through the gated vault ops like everything else (I1) — they are the
single sanctioned direct write, justified by I3: the user hand-typed a
command in this note seconds ago.

**Consent classes** (I3 made mechanical):

| Effect | Route | Examples |
|---|---|---|
| The command's own line (ack/result) | direct, snapshotted | all commands |
| Result block below the line, same note | direct — **v2, gated** | `//research`, `//summarize` |
| Anything touching another note | proposal queue, always | `//tasks`, `//group`, `//tidy` |

**v1 command set** — chosen because each reuses an existing worker and
needs zero new model surface:

- `//tasks` — extract action items from *this note's* text
  (`extract_tasks`), propose into `capture_inbox`.
- `//group` — run the grouping candidate rule on the section containing
  the command; propose the partition (same integrity enforcement).
- `//tidy` — schema-fix this note now (skip the queue-coverage wait),
  proposal as usual.

`//research` is **v2**: it needs a search backend (a `SearchSpec`:
query → ranked URLs; SearXNG or Brave API, decided when built) and it is
the first direct in-note result writer. Its output contract is fixed
now, per the corpus of trust this project runs on: **ranked source
links, never model paraphrase** — a Sources block, matching the
research-entity schema. Gated on v1 commands proving out in real use
(DEVPLAN Phase 11 gate).

**Latency budget, honest:** Obsidian autosave (~2s) + debounce (3s) +
poll tick (≤5s) + local worker generation. Target: ack visible ≤15s,
result ≤60s on the M5. That is a fine "go do this while I keep writing";
it is not chat, and nothing here pretends to stream.

## 4. What the engine owns vs what processors own

Engine machinery (built once): decision parsing/acting, staleness,
rendering, fingerprint memo, pass lock, the command scanner + ack/result
line rewriting, corpus recording. Processors own only `scan` and
`draft`. A processor cannot write — the applier and the ack rewriter are
the only two code paths that touch the vault, both through gated ops.

## 5. Testing & evals

- Engine: existing end-to-end pattern (tmp vault, scripted fake
  drafters) extended with: registry dispatch, pass-lock contention,
  debounce (mtime settle), command round-trip (type → ack → proposal →
  approve → result line), cancellation (edit the line mid-flight),
  worker-down result line.
- Workers: nothing changes — each processor's worker keeps its own
  `evals/cases/workers/*.yaml` file; one transformation, one eval. The
  no-chaining rule is what keeps this true.

## 6. Deferred, explicitly

- **Chat as an engine client** — plausible endgame, deferred; chat stays
  frozen (philosophy §3). Revisit only after Phases 7–9 have real
  mileage.
- **`//jira`, webhooks, any external-service command** — out (philosophy
  "explicitly out"); requires its own design for credentials and egress.
- **Direct-write result blocks (consent class 2)** — v2, gated on the
  D0–D2 usage gates *and* v1 command mileage.
