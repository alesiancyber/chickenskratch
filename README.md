# tezcat

A model-agnostic maintenance layer for a folder of markdown notes, with
its own surfaces: dashboard, canvas, editor, file explorer, chat pane.
The vault is plain markdown, so nothing traps your notes — but tezcat
is the front end, not a companion to someone else's. You write; the
harness extracts tasks, fixes structure, answers
typed `//commands` in place, and queues anything needing judgment for a
one-key verdict on the dashboard. Every mutation is a git commit;
`tezcat-revert` undoes any of them. Principles and invariants:
[PHILOSOPHY.md](PHILOSOPHY.md).

Two classes of change, two contracts:

| | **Commands** (`//azure query` + Enter) | **Proposals** (capture, schema fixes, grouping) |
|---|---|---|
| origin | you typed it | the harness inferred it |
| trust | intrinsic | your verdict, or verifier + Spec.md `auto_apply` |
| result | replaces its own line (callout) in ~settle-time | queued with a diff, decided on the dashboard |
| staleness | the exact-line anchor; your edit wins | whole-note hash |

## Layout

```
src/tezcat_vault/   vault core: path safety, git snapshots, markdown/task/
                    kanban parsing, entity schemas, MCP server, LLM workers
src/tezcat/         the engine: watcher pass, commands, processors, applier,
                    proposals, evals, CLI chat, backends, dashboard server
daemon/             tezcatd (Rust): event-driven trigger (FSEvents/inotify)
config/             models.yaml (profiles/backends/worker), mcp.yaml
                    (MCP servers), vault.yaml (spec defaults), schemas/
data/               proposals, corpus (training labels), stats, logs
evals/              agent suite + worker suite (the model-bakeoff gate)
web/                the owned dashboard (React; built assets served by tezcat-ui)
research/           plans and what-ifs; owned-surfaces-plan.md is the
                    operative UI spec, the rest is not canon
testvault/          scratch vault the default config points at
```

## Setup

Prereqs: `brew install uv`, a rust toolchain, git.

**The short way** — `deploy.sh` scripts the manual steps below; it's a
convenience, not a replacement (everything it does, you can do by hand):

```sh
./deploy.sh --vault ~/Vaults/personal --foam
# --foam: VSCode/Foam settings   --remote <git-url>: vault backup remote
# --no-launchd: skip the agents  --uninstall: remove them   (idempotent)
```

**The manual steps** (what the script automates):

1. **Build.** `uv sync`, then `cd daemon && cargo build --release`.
2. **Point at a vault.** Precedence: `--vault` flag > `TEZCAT_VAULT_PATH`
   (put it in `.env` to pin it for every CLI) > `vault:` in
   `config/models.yaml`.
3. **Models.** Any OpenAI-compatible endpoint: profiles in
   `config/models.yaml`, cloud keys via `.env` (`api_key_env` — keys
   never live in config or the vault), local servers under `backends:`
   (start/stop with `tezcat-backends`).
4. **First engine pass.** `uv run tezcat-watch` — the engine is
   self-bootstrapping: seeds `Harness/Spec.md` from `config/vault.yaml`
   defaults and inits the vault's own git repo.
5. **The daemon.** Manual run:

   ```sh
   daemon/target/release/tezcatd --vault <vault> --repo <this repo>
   # --poll for network mounts where filesystem events are unreliable
   ```

   Login-start: fill `__REPO__`/`__VAULT__` in
   `config/launchd/com.tezcat.watch.plist` (the event daemon,
   KeepAlive) and `com.tezcat.heartbeat.plist` (one 6am pass for the
   date rollover), copy both to `~/Library/LaunchAgents/`, `launchctl
   load` them. Logs land in `data/logs/`.
6. **Vault backup** (recommended): the vault is its own git repo —
   `git -C <vault> remote add origin <url> && git push -u origin HEAD`.

## Using it

Your notes are text in the vault. The views over them are tezcat's —
rendered live, never written back as files: a view that is also a file
costs a write, a commit, and a churn guard every pass and buys nothing
the renderer alone doesn't.

- **`//azure <query>`** on its own line in any note, press Enter →
  replaced in place by ranked Microsoft Learn links a few seconds later.
  Model-free. Commands live in `src/tezcat/commands.py`; adding one is
  an async function plus a registry entry (`//research` is reserved).
- **`Harness/Capture.md`** — brain-dump anything; tasks get extracted
  and proposed. Free text in the task inbox gets the same treatment.
  This is the one surface left, and it is not a view: the canvas commits
  your dump here *before* extraction runs, so it survives extraction
  failing.
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
  uv run tezcat today                      # the attention view
  uv run tezcat changes                    # the audit log
  uv run tezcat decide p-8f3c21a09b4e approve
  ```

  The 6am heartbeat (`config/launchd/com.tezcat.heartbeat.plist`) covers
  the date rollover on a quiet vault.
- **`Harness/Spec.md`** — the vault's own config, read fresh every
  pass: `folders` (type map), `capture_inbox`/`waiting_inbox`,
  `exclude` (e.g. `Private/` — never enters model context),
  `auto_apply`, `autonomy` (gated|full — `full` is the standing grant
  that lets verifier-less judgment processors auto-apply; everything
  stays snapshot-revertible), `fidelity` (strict|close), `worker`
  (models.yaml profile name for the drafting workers), `n_max`,
  `disabled_processors` (incl. `inline_command`), and `daily_dir`.
  Renderer choices are repo config, not Spec keys: diffs are always
  unified ```diff and callouts always `NOTE`.
- **The dashboard** — `uv run tezcat-ui` (127.0.0.1:8399): the primary
  attention/decision surface.
  Pending proposals with rich side-by-side diffs, approve/reject/revert
  buttons (the same gated paths every other channel uses), j/k·a/r
  keyboard review, ⌘K palette, SSE-live off the engine's passes.
  Frontend builds once with `cd web && npm install && npm run build`.
- **The canvas** — the dashboard's `#canvas` view: dump anything, ⌘↩.
  `//commands` in the dump execute instantly; the rest is extracted
  and either auto-filed (verifier passed) or offered as an inline
  approve/reject — per-task, if you only want some of it — right where
  you typed it. Same pipeline as Capture.md — the editor path keeps
  working identically.
- **The pane** (⌘J) — the chat escape hatch, re-homed: the same Agent
  loop as `tezcat -i`, with tool calls visible as expandable rows and
  write-gated calls as Allow/Deny chips (every verdict recorded to the
  training corpus). Transcripts are scratch; the vault is the archive.
- **The file explorer** (⌘B) — a collapsible rail for the times you
  know where a note lives: the vault's folder shape, filterable, notes
  opening in the editor above. Collapsed by default; ⌘K reaches any
  note by name without it.

Editor notes: tezcat's own surfaces are the front end, and note
references anywhere in the UI (rows, citations, "filed → Inbox") open
tezcat's editor — no app-scheme deep links are emitted. The vault is
still an ordinary folder of markdown, so any editor can open it —
that's the format being portable, a byproduct we keep, not a
compatibility target we design toward. If you do keep an editor open
alongside, turn on its autosave (`files.autoSave: afterDelay` in
VSCode): the disk file is what the harness sees.

## CLI

```sh
uv run tezcat "add a task to renew the domain by friday"   # chat escape hatch
uv run tezcat -i                                           # REPL
uv run tezcat today             # the attention view
uv run tezcat changes           # the audit log
uv run tezcat decide <pid> approve|reject    # consent without a UI process
uv run tezcat-revert            # list snapshots; tezcat-revert <sha> undoes one
uv run tezcat-ui                # the dashboard server (127.0.0.1:8399)
uv run tezcat-backends          # list/start/stop local model servers
uv run tezcat-eval -m <profile>         # agent suite (fixture vault, scored on files)
uv run tezcat-worker-eval -m <model>    # worker suite: 69 cases × 3 repeats;
                                         # schema_fix scored by the production verifier
```

Model swaps are eval-gated — reports land in `evals/reports/`.

## The MCP servers standalone

The vault server plugs into any MCP client (Claude Desktop/Code):

```sh
TEZCAT_VAULT_PATH=~/Vaults/personal uv run tezcat-vault-mcp
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

Read-only filesystem search: `FILES_ROOTS="~" uv run tezcat-files-mcp`.

## Tests

```sh
uv run pytest          # ~330 tests, no model, no network
cd daemon && cargo test
```
