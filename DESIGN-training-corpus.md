# Design: labeled training corpus (approve/deny + bug reports + revert)

*Status: **BUILT** (DEVPLAN Phase 1, 2026-07-22) — `src/harness/corpus.py`
plus the web.py/agent.py/mcp_client.py/vault.py wiring below, all
implemented as specced and tested (`tests/test_corpus.py`, web-layer
tests for confirm/timeout/report/revert). One addition beyond this doc:
`record_dashboard(proposal, decision)` — review-dashboard decisions
(DESIGN-review-dashboard.md §5) land in the same corpus with the
proposal itself as payload, no trajectory needed. Remaining: the manual
live-chat smoke at the bottom has not been run yet.*

## Context

We audited whether current data retention would leave anything usable for a
future fine-tune. Finding: `data/chats/*.json` already captures full,
real-usage trajectories (system prompt, tool calls, tool results) — but
`RETENTION_DAYS = 3` (chats.py:18) deletes them within days. In six months,
when a strong new open-weight model is worth fine-tuning on the b70, there
would be nothing to train on.

The user wants to start capturing now, using three signals: **explicit
approve/deny on gated write tools**, **bug reports**, and **git-revert of a
harness commit**. All three are human judgments already flowing through the
harness today — this just needs to persist them permanently instead of
letting them evaporate with the 3-day chat prune. No redaction: this is
private, single-user data, fine-tuned locally on the user's own b70.

Revert is structurally different from the other two: approve/deny and
bug-report labels are known *synchronously*, at the moment the trajectory
is still in memory. A revert can happen anytime later, against a commit
made by a chat that may already be gone from `data/chats/` (3-day
retention, chats.py:18). The user has decided explicitly: **accept that
gap** — keep the 3-day retention as-is; a revert within that window
produces a full labeled corpus row, a revert after it is silently skipped
(nothing to train on anyway once the trajectory is gone). This is simpler
than bumping retention and matches the same "don't build the harder thing
until it's shown to matter" instinct as the rest of this harness.

No fine-tuning pipeline or export/format-conversion step is in scope here —
just capture, so the corpus exists when it's needed.

## Schema

One JSON object per line in `data/corpus/YYYY-MM.jsonl` (monthly rotation):

```json
{
  "schema_version": 1,
  "ts": "2026-07-21T14:32:01",
  "chat_id": "20260721-143005-a1b2c3",
  "model": "gemma4-12b-mlx",
  "messages": [ /* full agent.messages trajectory, verbatim */ ],
  "label": {
    "source": "confirm",
    "polarity": "positive",
    "tool_name": "write_note",
    "tool_args": {"...": "..."},
    "approved": true,
    "timed_out": false
  }
}
```

Bug-report rows use the same envelope with:
```json
"label": {
  "source": "bug_report",
  "polarity": "negative",
  "description": "the note counts were wrong",
  "report_path": "data/bug_reports/20260721-143312-note-counts-were-wrong.md"
}
```

Revert rows:
```json
"label": {
  "source": "revert",
  "polarity": "negative",
  "sha": "a1b2c3d",
  "commit_message": "harness: append to Tasks/Inbox.md"
}
```

- `polarity`: `"positive"` for an approved write; `"negative"` for a denied
  write, a timed-out confirm, or any bug report. One top-level field a later
  export script can filter on regardless of source.
- `timed_out` (confirm only): distinguishes an explicit deny from a 600s
  silent timeout (`CONFIRM_TIMEOUT`) — both currently collapse to
  `approved=False` in `_confirm`. Recording the distinction now costs nothing
  and lets a future export step decide whether timeouts count as real
  negatives.
- No `tool_call_id`: threading it through would change the shared `confirm`
  callback signature used by web.py, cli.py, evals.py, and
  test_agent_guarantees.py. `tool_name`+`tool_args`, plus the full `messages`
  array (which already contains the real tool-call entry), are enough.
- Full `messages` duplicated per event, not diffed against the chat store:
  `data/chats/*.json` is pruned after 3 days, so anything that reconstructs
  via `chat_id` would break by the time it's needed. Self-contained rows are
  the only way to guarantee permanence independent of the chat store's
  lifecycle — same reasoning `bugreports.save_report` already uses to inline
  full transcripts rather than pointing back at `data/chats/`. Disk cost is
  negligible at this volume (a handful of events per session).

## File layout

`data/corpus/YYYY-MM.jsonl`, computed from `dt.datetime.now()` at write time
(same idiom as `ChatStore.new_id()`). Monthly rotation over one giant file
(keeps any single file boundable and lets old months gzip/sample
independently) or one-file-per-example (this repo's convention for
growing, script-consumed logs is JSONL append, e.g. the `routing_log.jsonl`
shape in DESIGN-mixed-models.md §9 — not one-file-per-record like
bug-report markdown, which is meant for human skimming of a single event).

## New module: `src/harness/corpus.py`

Same shape as `bugreports.py` — module constant, no classes:

- `record_confirm(chat_id, model, messages, tool_name, tool_args, approved, timed_out=False, corpus_dir=None) -> Path | None`
- `record_bug_report(chat_id, model, messages, description, report_path, corpus_dir=None) -> Path | None`
- `record_revert(chat_id, model, messages, sha, commit_message, corpus_dir=None) -> Path | None`
- Shared private `_append(rec, corpus_dir)`: no-ops (`None`) if `messages` is
  empty (nothing to label); otherwise `mkdir(parents=True, exist_ok=True)`,
  appends one `json.dumps(rec) + "\n"` to the current month's file.
- Raises `OSError` on failure rather than swallowing it — callers apply
  `try/except OSError: pass`, matching the existing `record_run` convention
  at web.py (~line 180-183: "totals are cosmetic; never fail a chat over
  them"). Corpus capture must never break a live chat.

## Git-revert wiring: linking a commit back to its chat

`obsidian_mcp` is a standalone MCP server, spawned as its own stdio
subprocess (`mcp_client.py`'s `MultiServerClient._connect`, ~line 188-193)
with a fresh process per `Agent` context-manager entry — i.e. one process
per chat conversation (`AgentWorker._run`'s `async with Agent(...)`),
relaunched with the *same* `chat_id` on any mid-conversation rebuild. Vault
write tools (`create_note`, `append_to_note`, etc. in
`src/obsidian_mcp/server.py`) call `v.snapshot_before(...)` /
`v.snapshot_after(summary)` with no notion of "which chat" today — the MCP
protocol boundary and separate-process nature mean chat_id can't just be a
Python attribute passed in-process.

Two ways to cross that boundary were considered: (a) MCP's per-call `meta`
dict (`ClientSession.call_tool(..., meta={...})`, readable server-side via a
tool's injected `Context.request_context.meta`) — verified this exists in
the installed `mcp` SDK, but it requires adding a `ctx: Context` parameter
to every one of the ~10 write-tool functions in server.py; or (b) an
**environment variable**, exactly the pattern this codebase already uses for
`OBSIDIAN_VAULT_PATH` and `WORKER_BASE_URL` — since the subprocess's entire
lifetime already equals one chat's lifetime, setting `HARNESS_CHAT_ID` once
at subprocess launch is sufficient and touches zero tool functions. Going
with (b): same attribution, an order of magnitude smaller diff, and it
composes for free with any other MCP client of this server (the module's
own stated design: "any MCP client can use this server").

**Threading `chat_id` down to the subprocess launch:**
1. `src/harness/mcp_client.py` — `MultiServerClient.__init__` gains
   `chat_id: str = ""`, stored as `self.chat_id`. In `_connect` (~line
   188-193), the stdio env becomes
   `env={**os.environ, **spec.env, **({"HARNESS_CHAT_ID": self.chat_id} if self.chat_id else {})}`.
   Empty by default, so cli.py/evals.py callers (which never set chat_id)
   see no change.
2. `src/harness/agent.py` — `Agent.__init__` gains `chat_id: str = ""`,
   passed through to `MultiServerClient(servers, ..., chat_id=chat_id)`
   (~line 126).
3. `src/harness/web.py` — `AgentWorker._run`'s `Agent(...)` construction
   (~line 153) gains `chat_id=self.chat_id` (already threaded onto
   `AgentWorker` for the confirm/bug-report wiring above, so this is free).

**Reading it back and embedding the trailer, in `src/obsidian_mcp/vault.py`:**
4. `get_vault()` (~line 159) reads `os.environ.get("HARNESS_CHAT_ID", "")`
   once, alongside the existing `OBSIDIAN_VAULT_PATH` read, and passes it
   into `Vault(root, chat_id=...)`; `Vault.__init__` stores `self.chat_id`.
5. `snapshot_after(summary)` (~line 115): when `self.chat_id` is set, the
   commit message becomes `f"harness: {summary}\n\nChat-Id: {self.chat_id}"`
   instead of just `f"harness: {summary}"` — a trailer in the commit body,
   invisible to `history()`'s existing `%s`-subject-only display, present in
   the full body. `snapshot_before` is untouched: those commits preserve the
   *user's* pre-existing manual edits, not a harness action, so they carry
   no chat attribution.
6. New `Vault.commit_message(sha) -> str` (near `history()`, ~line 123):
   `git log -1 --format=%B <sha>` — the one place that needs the full body,
   not just the subject line `history()` already exposes.

**Emitting the label at revert time, in `src/harness/web.py`'s `revert`
route (~line 519-525):**
7. Before calling `_vault().revert(sha)`, call `_vault().commit_message(sha)`
   and regex-extract `Chat-Id: (\S+)`.
8. If a chat_id is found: `store.load(chat_id)` (the same `store: ChatStore`
   already built in `build_app`, ~line 314, and already used by the chat-list
   and report-issue routes). If the chat is still present (within the 3-day
   window), pull `model`/`messages` from it and, once `revert()` reports
   success, call `record_revert(chat_id, model, messages, sha, commit_message)`
   wrapped in `try/except OSError: pass`.
9. If no trailer is found (pre-harness commit, manual-edit commit, or a
   commit made before this feature existed), or `store.load` returns `None`
   (chat already pruned) — skip silently. This is the accepted gap from the
   Context section above; no orphan/placeholder row is written since an
   empty-`messages` row has no training value.

## Touch points in `src/harness/web.py`

1. Import `record_confirm, record_bug_report` from `.corpus` alongside the
   existing `from .stats import record_run, totals`.
2. `AgentWorker.__init__` (~line 77): add `chat_id: str = ""` param, store as
   `self.chat_id`.
3. `AgentWorker._confirm` (~line 126-142): track `timed_out` (set `True` in
   the `except TimeoutError:` branch, currently just sets `approved = False`).
   After resolving, call `record_confirm(...)` wrapped in
   `try/except OSError: pass`. `self.agent` is always set by this point.
4. `Manager.get_worker` (~line 256-283): pass `chat_id=self.chat_id` into the
   `AgentWorker(...)` construction (chat_id is already set the line before).
5. `Manager.open_chat` (~line 285-304): same — pass `chat_id=chat_id` into
   its `AgentWorker(...)` construction.
6. `report_issue` route handler (~line 536-556): after `save_report(...)`
   succeeds, call `record_bug_report(...)` with `manager.chat_id`,
   `worker.model_name`, `messages`, the description, and the report path —
   wrapped in the same `try/except OSError: pass`.

Total: one new module, a constructor param threaded through two call sites,
and two 4-6 line call-site additions, all following the existing
never-fail-the-chat-over-telemetry convention. (The revert route's wiring
is covered separately above, since it also touches agent.py, mcp_client.py,
and vault.py.)

## Tests

**`tests/test_corpus.py`** (new, styled like `test_bugreports.py` — direct
calls, `tmp_path`, assert on file content):
- confirm-approved → valid JSONL line, `polarity=="positive"`, round-trips
- confirm-denied → `polarity=="negative"`
- confirm-timeout → `timed_out is True`
- bug-report → `polarity=="negative"` always, `description`/`report_path` set
- empty `messages` → no-ops, returns `None`, no file created
- two events, same corpus_dir → one month file, two lines, both parseable
- nested nonexistent `corpus_dir` → created (`mkdir(parents=True)`)

**`tests/test_web.py`** additions:
- Update `FakeWorker.__init__` (~line 28-38) to accept/store `chat_id=""` —
  required the moment `AgentWorker(...)` call sites gain `chat_id=...`, or
  every test that swaps in `FakeWorker` breaks with a `TypeError`.
- Exercise `AgentWorker._confirm` directly (construct via
  `object.__new__(AgentWorker)` to skip `__init__`'s task creation, set the
  handful of attributes it touches), monkeypatch `harness.web.record_confirm`
  to capture the call, drive both the approve and deny paths via
  `worker.resolve(confirm_id, ...)`, and assert the captured fields.
- A timeout-path variant: monkeypatch `CONFIRM_TIMEOUT` small, let it expire
  naturally, assert `timed_out=True, approved=False` reaches the call.
- Extend the existing `test_report_issue_saves_conversation`: monkeypatch
  `record_bug_report` and assert it's *not* called for the existing
  no-active-conversation case (reuses that test's empty-trajectory scenario
  to prove the guard holds end-to-end).

**`tests/test_tools.py`** additions (extends the existing `vault` fixture,
~line 15-21, which already does `monkeypatch.setenv(...)` +
`monkeypatch.setattr(vault_mod, "_vault", None)` for `OBSIDIAN_VAULT_PATH` —
same pattern applies to `HARNESS_CHAT_ID`):
- `test_snapshot_after_embeds_chat_id_trailer` — set `HARNESS_CHAT_ID` via
  monkeypatch before the fixture builds the vault, make a write, assert
  `v.commit_message(sha)` contains `"Chat-Id: <the id>"` while
  `v.history()[0]["message"]` (subject only) does not.
- `test_snapshot_before_has_no_chat_id_trailer` — the manual-edits-preserved
  commit never carries the trailer, even with `HARNESS_CHAT_ID` set.
- `test_no_chat_id_env_no_trailer` — unset (today's exact behavior/tests
  keep passing unmodified when the env var isn't present).

**`tests/test_mcp_client.py`** addition:
- A test on `MultiServerClient._connect` (or its `StdioServerParameters`
  construction) asserting `HARNESS_CHAT_ID` appears in the subprocess env
  when `chat_id` is set on the client, and is absent when it's `""` —
  follows this file's existing monkeypatch-based style (~line 20-30).

**`tests/test_web.py`** addition (new tests alongside the existing route
tests, e.g. near `test_report_issue_saves_conversation` ~line 155):
- Revert of a commit with a `Chat-Id:` trailer whose chat is still in the
  store → `record_revert` called with the expected `chat_id`/`model`/
  `messages`/`sha`.
- Same, but the chat has been pruned (`store.load` returns `None`) →
  `record_revert` not called, revert itself still succeeds.
- Revert of a commit with no trailer (simulating a manual-edit or
  pre-feature commit) → `record_revert` not called.

No changes needed in `bugreports.py` (report format is untouched), `evals.py`,
or `cli.py` (neither constructs `AgentWorker`/`Agent` with a chat_id, and the
`confirm` callback signature doesn't change).

## Deferred / out of scope

- Any fine-tuning pipeline, export step, or format conversion — this design
  is capture only. Revisit once a specific fine-tune framework is chosen.
- Extending `RETENTION_DAYS` for `data/chats/` — considered and explicitly
  rejected; the corpus's self-contained rows make it unnecessary.

## Verification

- `uv run pytest tests/test_corpus.py tests/test_web.py tests/test_tools.py tests/test_mcp_client.py -q`
- Manual smoke: `uv run harness-web`, open a chat against a
  `confirm_writes: true` profile (gemma4-12b-mlx), approve one write and
  deny another, file one bug report via the UI's report button, then revert
  the approved write from the ↺ Changes panel. Confirm
  `data/corpus/<current-month>.jsonl` has four lines with the expected
  `label.source`/`polarity` values and non-empty `messages` — including the
  revert row, proving the `Chat-Id:` trailer round-tripped through a real
  subprocess launch, not just the unit tests.
