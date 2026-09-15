# chickenskratch

A model-agnostic maintenance layer for a folder of markdown notes, with
its own surfaces: dashboard, canvas, editor, file explorer, chat pane.
The vault is plain markdown, so nothing traps your notes — but chickenskratch
is the front end, not a companion to someone else's. You write chicken
scratch; the harness files it as structured records with formatted
tasks (your verbatim text always kept), fixes structure, answers
typed `//commands` in place, and queues anything needing judgment for a
one-key verdict on the dashboard. Every mutation is a git commit;
`chickenskratch-revert` undoes any of them. Principles and invariants:
[PHILOSOPHY.md](PHILOSOPHY.md).

Two classes of change, two contracts:

| | **Commands** (`//azure query` + Enter) | **Proposals** (capture, schema fixes, grouping) |
|---|---|---|
| origin | you typed it | the harness inferred it |
| trust | intrinsic | your verdict, or verifier + a `verified`/`full` policy |
| result | replaces its own line (callout) in ~settle-time | queued with a diff, decided on the dashboard |
| staleness | the exact-line anchor; your edit wins | whole-note hash |

## Layout

```
src/chickenskratch_vault/   vault core: path safety, git snapshots, markdown/task/
                    kanban parsing, entity schemas, MCP server, LLM workers
src/chickenskratch/         the engine: watcher pass, commands, processors, applier,
                    proposals, evals, CLI chat, backends, dashboard server
daemon/             chickenskratchd (Rust): event-driven trigger (FSEvents/inotify)
config/             models.yaml (profiles/backends/worker), mcp.yaml
                    (MCP servers), vault.yaml (spec defaults), schemas/
data/               proposals, corpus (training labels), stats, logs
evals/              agent suite + worker suite (the model-bakeoff gate)
web/                the owned dashboard (React; built assets served by chickenskratch-ui)
research/           plans and what-ifs; owned-surfaces-plan.md (UI) and
                    dumps-become-records.md (capture lane) are the
                    operative specs, the rest is not canon
testvault/          scratch vault the default config points at
```

## Setup

Prereqs: `brew install uv`, a rust toolchain, git.

**The short way** — `setup.sh` builds, asks what it needs, and connects a
vault. Run it with no arguments and it walks you through:

```sh
./setup.sh
# or scripted:
./setup.sh --vault ~/Vaults/personal --worker qwen35-moe --yes
# --posture fresh|import  override the auto-detect   --foam: VSCode/Foam settings
# --remote <git-url>: vault backup   --no-launchd: skip the agents
# --clean: wipe data/    --uninstall: remove the agents      (idempotent)
```

**It puts nothing in your vault.** Two files appear —
`Harness/Spec.md` (your settings) and `Harness/Capture.md` (the canvas's
write-ahead log) — and that is all. No starter notes, no folder
scaffold, no examples.

**Pointing it at a vault you already have** is the case it takes most
care over. Any vault with notes in it is detected as an *import*, and
the two per-note lanes — `schema_fix` and `grouping` — are seeded `off`.
Left on, those cost one model call per note until the whole vault has
been through, and `ask` would not help: it changes where the result
lands, not whether the call happens. Everything else is seeded `ask`, so
nothing is rewritten without your verdict. Turn the sweeps on from the
console once you've looked around.

The folder→type map is read off the folders that actually hold notes,
not asserted from a template; anything it can't type is mapped `other`
and `spec_drift` proposes the rest as they appear.

**The manual steps** (what the script automates):

1. **Build.** `uv sync`, then `cd daemon && cargo build --release`.
2. **Point at a vault.** Precedence: `--vault` flag > `CHICKENSKRATCH_VAULT_PATH`
   (put it in `.env` to pin it for every CLI) > `vault:` in
   `config/models.yaml`.
3. **Models.** Any OpenAI-compatible endpoint: profiles in
   `config/models.yaml`, cloud keys via `.env` (`api_key_env` — keys
   never live in config or the vault), local servers under `backends:`
   (start/stop with `chickenskratch-backends`).
4. **First engine pass.** `uv run chickenskratch-watch` — the engine is
   self-bootstrapping: seeds `Harness/Spec.md` from `config/vault.yaml`
   defaults and inits the vault's own git repo. By hand you get the
   template folder map and the shipped policy levels; `setup.sh` seeds
   the same note from the vault's own folders and picks the posture
   first, which matters on an import (see above).
5. **The daemon.** Manual run:

   ```sh
   daemon/target/release/chickenskratchd --vault <vault> --repo <this repo>
   # --poll for network mounts where filesystem events are unreliable
   ```

   Login-start: fill `__REPO__`/`__VAULT__` in
   `config/launchd/com.chickenskratch.watch.plist` (the event daemon,
   KeepAlive) and `com.chickenskratch.heartbeat.plist` (one 6am pass for the
   date rollover), copy both to `~/Library/LaunchAgents/`, `launchctl
   load` them. Logs land in `data/logs/`.
6. **Vault backup** (recommended): the vault is its own git repo —
   `git -C <vault> remote add origin <url> && git push -u origin HEAD`.

## Using it

Your notes are text in the vault. The views over them are chickenskratch's —
rendered live, never written back as files: a view that is also a file
costs a write, a commit, and a churn guard every pass and buys nothing
the renderer alone doesn't.

- **`//azure <query>`** on its own line in any note, press Enter →
  replaced in place by ranked Microsoft Learn links a few seconds later.
  Model-free. Commands live in `src/chickenskratch/commands.py`; adding one is
  an async function plus a registry entry (`//research` is reserved).
- **`Harness/Capture.md`** — brain-dump anything; the dump becomes a
  **record**: a meeting note (or a dated capture note) holding a
  readable rewrite, the tasks it contains — owed items marked `👤` —
  and your verbatim dump quoted, collapsed, beneath. Tasks live in the
  record (location is provenance; the dashboard walks every note).
  Autonomy is graded by what `verify_record` can check: a bare task
  list (faithful tasks, complete verbatim, no rewrite) files itself;
  a dump that earned a summary queues under the default posture, and
  at a `full` policy it files with the prose objection on the audit
  trail — the folded verbatim is the check, one unfold away.
  This is the one surface left, and it is not a view: the canvas
  commits your dump here *before* extraction runs, so it survives
  extraction failing.
- **Duplicates & links** — the overlap processor compares tasks across
  notes: exact matches pair in code, near-matches through a cached
  embedding index (`EMBED_BASE_URL`/`EMBED_MODEL`; defaults to Ollama's
  `nomic-embed-text`, degrades to exact-only without an endpoint), and
  one batched judge call decides duplicate / related / distinct. Fixes
  queue per pair: a duplicate removes the redundant copy, a related
  pair gains a `⇄ [[note#^id]]` block link — links, not prose. Rejected
  pairs are remembered and never re-asked.
- **Headless**, when no UI is running:

  ```sh
  uv run chickenskratch today                      # the attention view
  uv run chickenskratch changes                    # the audit log
  uv run chickenskratch decide p-8f3c21a09b4e approve
  ```

  The 6am heartbeat (`config/launchd/com.chickenskratch.heartbeat.plist`) covers
  the date rollover on a quiet vault.
- **`Harness/Spec.md`** — the vault's own config, read fresh every
  pass: `folders` (type map), `meetings_dir`/`capture_archive` (where
  records live), `exclude` (e.g. `Private/` — never enters model
  context), `policies` (the consent map: one `off|ask|verified|full`
  level per processor — the level IS the gate, and the console's
  autonomy presets just pre-select them; everything stays
  snapshot-revertible), `fidelity` (strict|close), `worker`
  (models.yaml profile name for the drafting workers), `n_max`,
  `disabled_processors` (the `inline_command` kill switch and the
  per-lane emergency off), and `daily_dir`.
  Renderer choices are repo config, not Spec keys: diffs are always
  unified ```diff and callouts always `NOTE`.
- **The dashboard** — `uv run chickenskratch-ui` (127.0.0.1:8399): the primary
  attention/decision surface.
  Pending proposals with rich side-by-side diffs, approve/reject/revert
  buttons (the same gated paths every other channel uses), j/k·a/r
  keyboard review, ⌘K palette, SSE-live off the engine's passes.
  Frontend builds once with `cd web && npm install && npm run build`.
- **The canvas** — the dashboard's `#canvas` view: dump anything, ⌘↩.
  `//commands` in the dump execute instantly; the rest becomes a
  record — auto-filed with a revert on the receipt (verifier passed)
  or offered as an inline approve/reject, right where you typed it. A
  record is one atomic step: approve files everything, reject files
  nothing. Same pipeline as Capture.md — the editor path keeps working
  identically.
- **The pane** (⌘J) — the chat escape hatch, re-homed: the same Agent
  loop as `chickenskratch -i`, with tool calls visible as expandable rows and
  write-gated calls as Allow/Deny chips (every verdict recorded to the
  training corpus). Transcripts are scratch; the vault is the archive.
- **The file explorer** (⌘B) — a collapsible rail for the times you
  know where a note lives: the vault's folder shape, filterable, notes
  opening in the editor above. Collapsed by default; ⌘K reaches any
  note by name without it.

Editor notes: chickenskratch's own surfaces are the front end, and note
references anywhere in the UI (rows, citations, "filed → Captures")
open chickenskratch's editor — no app-scheme deep links are emitted. The vault is
still an ordinary folder of markdown, so any editor can open it —
that's the format being portable, a byproduct we keep, not a
compatibility target we design toward. If you do keep an editor open
alongside, turn on its autosave (`files.autoSave: afterDelay` in
VSCode): the disk file is what the harness sees.

## CLI

```sh
uv run chickenskratch "add a task to renew the domain by friday"   # chat escape hatch
uv run chickenskratch -i                                           # REPL
uv run chickenskratch today             # the attention view
uv run chickenskratch changes           # the audit log
uv run chickenskratch decide <pid> approve|reject    # consent without a UI process
uv run chickenskratch-revert            # list snapshots; chickenskratch-revert <sha> undoes one
uv run chickenskratch-ui                # the dashboard server (127.0.0.1:8399)
uv run chickenskratch-backends          # list/start/stop local model servers
./restart.sh                    # bounce every chickenskratch process onto the current
                                # code (daemon via launchd kickstart, UI killed
                                # + relaunched); --build to rebuild first
uv run chickenskratch-eval -m <profile>         # agent suite (fixture vault, scored on files)
uv run chickenskratch-worker-eval -m <model>    # worker suite: 69 cases × 3 repeats;
                                         # schema_fix scored by the production verifier
```

Model swaps are eval-gated — reports land in `evals/reports/`.

## The MCP servers standalone

The vault server plugs into any MCP client (Claude Desktop/Code):

```sh
CHICKENSKRATCH_VAULT_PATH=~/Vaults/personal uv run chickenskratch-vault-mcp
```

22 tools: notes (`list_notes`, `read_note`, `search_vault`,
`create_note`, `append_to_note`, `replace_section`, `replace_line`,
`update_frontmatter`), tasks (`list_tasks`, `add_task`,
`set_task_status`, `set_task_due`, `set_task_scheduled`,
`archive_done_tasks`, `extract_tasks`), boards (`create_board`,
`read_board`, `add_card`, `move_card`), plus `validate_schema`,
`open_daily_note`, `create_meeting_note`.
Semantic tools own format
correctness (a model never hand-edits kanban markdown); edits are
anchored to headings/tasks/lines — whole-file rewrites don't exist.

Read-only filesystem search: `FILES_ROOTS="~" uv run chickenskratch-files-mcp`.

## Tests & lint

```sh
./check.sh             # every static gate: ruff + eslint + htmlhint
./check.sh --all       # + pytest (~430 tests, no model) + cargo test
uv run pytest          # just the python suite
cd web && npm run lint # just the web lint (also runs in restart.sh --build)
```

The web lint is correctness-only, same posture as ruff: `no-undef`
(a reference to deleted state builds fine and blanks the app at
runtime — eslint refuses it at build time), unused vars, hooks rules,
and surrogate-safe regex classes. Style stays unlinted on both sides.
