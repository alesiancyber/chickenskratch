# tool-harness

A model-agnostic agent harness that drives an Obsidian vault: notes, tasks,
kanban boards, daily notes, meeting notes. Local models via Ollama today; any
OpenAI-compatible endpoint tomorrow.

## Architecture

```
┌─────────────────────────────────────────────┐
│ Harness (src/harness) — thin agent loop     │
├─────────────────────────────────────────────┤
│ Model adapter — config/models.yaml profiles │  ← swap models here
├─────────────────────────────────────────────┤
│ Obsidian MCP server (src/obsidian_mcp)      │  ← usable by ANY MCP client
└─────────────────────────────────────────────┘
```

Three rules keep it plug-and-play:

1. **The vault is the API.** Tools read/write markdown directly (Kanban plugin
   format, Tasks plugin emoji syntax). Works with Obsidian closed; Obsidian
   picks changes up live.
2. **Model specifics live only in profiles** (`config/models.yaml`). Tool code
   and the base system prompt never compensate for one model's quirks.
3. **Every mutation is git-snapshotted.** Manual edits are committed before the
   harness touches anything; each harness change is its own commit. Undo is
   `git revert`/`git reset` in the vault.

## MCP servers & write safety

`config/mcp.yaml` lists the MCP servers the harness connects to — the local
Obsidian server (stdio), the read-only `files` server (find_file /
search_text / list_dir over a FILES_ROOTS allowlist — "where's that file?"
as a chat message), plus remote streamable-HTTP servers (Microsoft Learn
docs is on by default; Atlassian is configured but disabled pending OAuth).
Non-Obsidian tools are namespaced `server__tool`. A profile can narrow its
tool surface with `servers: [obsidian]`.

Tools are classified read-only vs write via MCP `readOnlyHint` annotations
(unannotated external tools are conservatively treated as writes;
`assume_read_only: true` in mcp.yaml overrides per server). Two harness
guarantees apply to write tools:

- **`confirm_writes: true`** in a profile gates every write on your approval —
  a y/N prompt in the CLI (`--yes` to skip), approve/deny buttons in the chat
  UI. Unrequested mutations can't land silently.
- **Loop guard**: an identical write repeated within one model turn returns
  the previous result instead of executing again (models occasionally loop —
  without this, one request can add the same task several times).

## Setup

```sh
brew install uv        # already done on this machine
uv sync
```

## Usage

```sh
# one-shot (uses default model + vault from config/models.yaml)
uv run harness "add a task to renew the domain by next friday"

# multi-turn REPL
uv run harness -i

# different model profile / different vault
uv run harness -m qwen3-8b -v ~/Vaults/personal "what's due today?"
```

### Chat UI

```sh
uv run harness-web                     # http://127.0.0.1:8765
uv run harness-web -v ~/Vaults/personal -p 9000
```

A single-page local chat app (no build step, no external requests —
marked.js/DOMPurify are vendored). Tool calls stream into the chat live as
the model works; the model picker switches profiles (starting a fresh
conversation); "New chat" resets. One conversation at a time by design.

Point it at a real vault with `-v`, `OBSIDIAN_VAULT_PATH`, or the `vault:` key
in `config/models.yaml`. The default is the bundled `testvault/`.

## Adding a model

Add an entry to `config/models.yaml` — that's the whole procedure:

```yaml
homelab:
  model: qwen3.5:72b
  base_url: http://192.168.1.50:8000/v1   # vLLM, llama.cpp, Ollama, anything OpenAI-compatible
  max_iterations: 25
  max_tool_result_chars: 12000
```

Ollama note: its OpenAI endpoint uses the model's default context length.
Raise it with `OLLAMA_CONTEXT_LENGTH=16384 ollama serve` or a Modelfile.

## The MCP server standalone

The tool server is independent of the harness — plug it into Claude Desktop,
Claude Code, or any MCP client:

```sh
OBSIDIAN_VAULT_PATH=~/Vaults/personal uv run obsidian-mcp
```

Tools (15): `list_notes`, `read_note`, `search_vault`, `create_note`,
`append_to_note`, `replace_section`, `update_frontmatter`, `list_tasks`,
`add_task`, `set_task_status`, `read_board`, `add_card`, `move_card`,
`open_daily_note`, `create_meeting_note`.

Design intent: semantic tools own format correctness (a model never hand-edits
kanban markdown), general tools (`search_vault`, `read_note`, `replace_section`)
keep capable models uncaged. Results are paginated/truncated by the harness, and
whole-file rewrites don't exist — edits are anchored to headings, tasks, cards.

## Workflows (skills) & capture mode

Workflows follow the [Agent Skills](https://agentskills.io) open standard —
`skills/<name>/SKILL.md` folders, portable to Claude Code/Copilot/Codex.
Only name + description ride in the system prompt; the body loads when
invoked via `/skill-name` in chat or the model's `use_skill` tool.

**Meeting capture:** hit **● Capture** in the chat UI, type freely during
the meeting (lines append straight to a `Captures/` note — no model, no
latency), hit **■ Stop** — the `meeting-capture` skill structures it into
`Meetings/{date} {title}.md` with day/time, topics covered, and action
items as real dated tasks. The raw capture is kept and linked, so a bad
formatting pass is a free retry. Meeting format lives in
`Templates/Meeting.md` (`{{title}} {{date}} {{day}} {{time}}`) — edit the
template to change the format, no code.

## Tests

```sh
uv run pytest
```

Smoke tests run every tool against a temp copy of `testvault/`.

## Evals — measuring model swaps

```sh
uv run harness-eval                    # full suite vs the default profile
uv run harness-eval -m qwen3.5-9b      # compare another profile
uv run harness-eval -k kanban          # subset by name
```

21 fixture cases in `evals/cases/*.yaml`, each run against a pristine copy of
`evals/vault` (19 open tasks with known type counts; date placeholders like
`{tomorrow}` resolved at run time so cases never go stale). Scoring checks
the resulting files and board/task structure — never the model's prose —
plus regression cases for every observed failure class: duplicate writes,
spurious mutations on read-only questions, denied-write honesty, multi-turn
reference resolution. Reports (md + json) land in `evals/reports/`.

Run it when you: swap models, edit the system prompt, add or change a tool,
or change toolset scoping. New model = new profile entry + one eval run =
a scored answer instead of vibes.

## Roadmap

- **Phase 2** — eval suite (fixture tasks scored by checking resulting files,
  so model swaps are measurable); skills-style markdown workflow files
  (meeting pipeline, weekly review).
- **Phase 3** — point a profile at the networked 32GB server; scheduled runs
  (daily-note prep, task rollover).
