# tezcat

A model-agnostic maintenance layer for a folder of markdown notes
(Obsidian, Foam/VSCode, or any editor — none of them execute harness
code). You write; the harness extracts tasks, fixes structure, answers
typed `//commands` in place, and queues anything needing judgment as a
checkbox in `Harness/Review.md`. Every mutation is a git commit;
`harness-revert` undoes any of them. Principles and invariants:
[PHILOSOPHY.md](PHILOSOPHY.md).

Two classes of change, two contracts:

| | **Commands** (`//azure query` + Enter) | **Proposals** (capture, schema fixes, grouping) |
|---|---|---|
| origin | you typed it | the harness inferred it |
| trust | intrinsic | checkbox, or verifier + Spec.md `auto_apply` |
| result | replaces its own line (callout) in ~settle-time | queued in Review.md with a diff |
| staleness | the exact-line anchor; your edit wins | whole-note hash |

## Layout

```
src/obsidian_mcp/   vault core: path safety, git snapshots, markdown/task/
                    kanban parsing, entity schemas, MCP server, LLM workers
src/harness/        the engine: watcher pass, commands, processors, applier,
                    proposals, evals, CLI chat, backends
daemon/             tezcatd (Rust): event-driven trigger (FSEvents/inotify)
config/             models.yaml (profiles/backends/worker), mcp.yaml
                    (MCP servers), vault.yaml (spec defaults), schemas/
data/               proposals, corpus (training labels), stats, logs
evals/              agent suite + worker suite (the model-bakeoff gate)
research/           non-operational what-ifs (model orchestration) — not canon
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
2. **Point at a vault.** Precedence: `--vault` flag > `OBSIDIAN_VAULT_PATH`
   (put it in `.env` to pin it for every CLI) > `vault:` in
   `config/models.yaml`.
3. **Models.** Any OpenAI-compatible endpoint: profiles in
   `config/models.yaml`, cloud keys via `.env` (`api_key_env` — keys
   never live in config or the vault), local servers under `backends:`
   (start/stop with `harness-backends`).
4. **First engine pass.** `uv run harness-watch` — the engine is
   self-bootstrapping: seeds `Harness/Spec.md` from `config/vault.yaml`
   defaults, inits the vault's own git repo, renders every surface.
5. **The daemon.** Manual run:

   ```sh
   daemon/target/release/tezcatd --vault <vault> --repo <this repo> \
     --fast-file <vault>/Harness/Review.md      # --poll for network mounts
   ```

   Login-start: fill `__REPO__`/`__VAULT__` in
   `config/launchd/com.tezcat.watch.plist` (the event daemon,
   KeepAlive) and `com.tezcat.heartbeat.plist` (one 6am pass for the
   date rollover), copy both to `~/Library/LaunchAgents/`, `launchctl
   load` them. Logs land in `data/logs/`.
6. **Vault backup** (recommended): the vault is its own git repo —
   `git -C <vault> remote add origin <url> && git push -u origin HEAD`.

## Using it

Everything is text in the vault:

- **`//azure <query>`** on its own line, press Enter → replaced in place
  by ranked Microsoft Learn links a few seconds later. Model-free.
  Commands live in `src/harness/commands.py`; adding one is an async
  function plus a registry entry (`//research` is reserved).
- **`Harness/Capture.md`** — brain-dump anything; tasks get extracted
  and proposed. Free text in the task inbox gets the same treatment.
- **`Harness/Review.md`** — pending proposals with diffs; check
  `approve`/`reject`, acted on within seconds. Mechanical fixes that
  pass their fidelity verifier auto-apply (opt-out per processor).
- **`Harness/Changes.md`** — the audit log: every snapshot commit plus
  a worker health line.
- **`Harness/Today.md`** — the attention surface: overdue/due-soon
  tasks, waiting-on items, today's meetings, in-flight kanban cards,
  pending-decision count. Deterministic, read-only, one screen. The 6am
  heartbeat (`config/launchd/com.tezcat.heartbeat.plist`) covers the
  date rollover on a quiet vault.
- **`Harness/Spec.md`** — the vault's own config, read fresh every
  pass: `folders` (type map), `capture_inbox`/`waiting_inbox`,
  `exclude` (e.g. `Private/` — never enters model context),
  `auto_apply`, `fidelity` (strict|close), `worker` (models.yaml
  profile name for the drafting workers), `n_max`,
  `disabled_processors` (incl. `inline_command`), and editor rendering:
  `diff_style` (split|unified), `callout_type`, `link_scheme`
  (obsidian|vscode), `daily_dir`.

Editor notes: Obsidian works out of the box. For Foam/VSCode set
`diff_style: unified` + `link_scheme: vscode` and turn on
`files.autoSave: afterDelay` (the disk file is what the harness sees).

## CLI

```sh
uv run harness "add a task to renew the domain by friday"   # chat escape hatch
uv run harness -i                                           # REPL
uv run harness-revert            # list snapshots; harness-revert <sha> undoes one
uv run harness-backends          # list/start/stop local model servers
uv run harness-eval -m <profile>         # agent suite (fixture vault, scored on files)
uv run harness-worker-eval -m <model>    # worker suite: 69 cases × 3 repeats;
                                         # schema_fix scored by the production verifier
```

Model swaps are eval-gated — reports land in `evals/reports/`.

## The MCP servers standalone

The vault server plugs into any MCP client (Claude Desktop/Code):

```sh
OBSIDIAN_VAULT_PATH=~/Vaults/personal uv run obsidian-mcp
```

19 tools: notes (`list_notes`, `read_note`, `search_vault`,
`create_note`, `append_to_note`, `replace_section`, `replace_line`,
`update_frontmatter`), tasks (`list_tasks`, `add_task`,
`set_task_status`, `extract_tasks`), boards (`create_board`,
`read_board`, `add_card`, `move_card`), plus `validate_schema`,
`open_daily_note`, `create_meeting_note`. Semantic tools own format
correctness (a model never hand-edits kanban markdown); edits are
anchored to headings/tasks/lines — whole-file rewrites don't exist.

Read-only filesystem search: `FILES_ROOTS="~" uv run files-mcp`.

## Tests

```sh
uv run pytest          # ~225 tests, no model, no network
cd daemon && cargo test
```
