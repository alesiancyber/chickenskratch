# tezcat

*Named for Tezcatlipoca's smoking mirror — a scrying mirror of polished
obsidian. You ask the black mirror questions; it answers.*

A model-agnostic agent harness that drives an Obsidian vault: notes, tasks,
kanban boards, daily notes, meeting notes. Local models (MLX / Ollama) today;
any OpenAI-compatible endpoint tomorrow.

## Architecture

```
┌─────────────────────────────────────────────┐
│ Harness (src/harness) — thin agent loop     │
├─────────────────────────────────────────────┤
│ Model adapter — config/models.yaml profiles │  ← swap models here
├─────────────────────────────────────────────┤
│ MCP servers — config/mcp.yaml               │  ← usable by ANY MCP client
│  obsidian (vault ops) · files (find/grep)   │
│  ms-learn (remote docs) · atlassian (off)   │
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
   one click in the chat UI's **↺ Changes** panel (or `git revert` in the
   vault, if you prefer the long way). Snapshots fail loud: if the
   before-snapshot can't be committed, the mutation is blocked rather than
   run without a safety net.

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

A third guarantee covers documentation servers: marking a server with
`docs_domain:` in mcp.yaml (ms-learn ships with
`docs_domain: learn.microsoft.com`) makes its search results arrive as
ranked link digests, and any answer built on its tools always carries the
source URLs — appended deterministically if the model omits them. Adding
another docs source (Python docs, MDN, …) is one config entry, no code.

The same guarantee turns inward for the vault: an answer built on vault
reads carries a **From your vault** block listing the notes it drew on, as
clickable `obsidian://` links (with line numbers where known). Notes the
model already named inline are skipped, so citing never duplicates. Active
whenever the harness knows the vault path (web + CLI); off in evals so
answer checks aren't perturbed.

## Setup

```sh
brew install uv
uv sync
mkdir testvault        # scratch vault the default config points at
                       # (or set `vault:` in config/models.yaml to your own)
```

You also need a model server running. The default profile expects Gemma 4
via mlx-vlm on port 8080:

```sh
uv tool install mlx-vlm
mlx_vlm.server --model mlx-community/gemma-4-12B-it-4bit --port 8080
```

The qwen profiles expect Ollama (`ollama serve`) with the models pulled.
Any OpenAI-compatible endpoint works — see "Adding a model."

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
marked.js/DOMPurify are vendored). In the header:

- **model picker** — switch profiles (starts a fresh conversation)
- **🛡 Approvals** — toggle the write-confirmation gate per session
  (profile default until overridden; remembered in localStorage)
- **↺ Changes** — recent vault commits with one-click revert
- **💬 Chats** — recent conversations, auto-saved after every reply,
  reopenable, pruned after 3 days (the vault is the archive, chats are scratch)
- **● Capture** — model-free meeting capture (see Workflows below)
- **usage readout** — per-chat context %, input and output tokens, live in
  the header (`~` marks chars÷4 estimates when the backend doesn't report
  usage; set `context_tokens` in the profile to your server's real window
  so the percentage is honest). **Stats** links a per-model totals page
  (runs, tokens, tool calls across web + CLI; evals excluded by design).
- **🐞 Report issue** (in the menu) — saves the whole current conversation
  (your note + every user/assistant turn + tool calls *with results*) to a
  markdown file under `data/bug_reports/`. Each file is a ready-made example
  to hand to Claude Code — build a fix list without reconstructing context.
- **⚡ Servers** — start/stop local model backends (mlx, Ollama) declared
  under `backends:` in models.yaml, so a 12B model isn't quietly holding
  7GB while you're not using it. Start only runs the config-declared
  command; Stop only touches processes the harness itself started —
  anything launched elsewhere shows as "external" and is left alone.
  Started backends survive harness restarts (pids are tracked and
  re-adopted); logs land in `data/logs/<name>.log`. The same panel has the
  **tool working-set**: per-tool checkboxes grouped by server, one-click
  presets (`presets:` in mcp.yaml — ships with Checklist and Notes), and
  select-all/none. Pinning a small set shrinks the tool schemas riding in
  every request *and* sharpens small-model tool selection (fewer options,
  fewer wrong picks) — the two together are the real win at 8k context. A
  server only connects if one of its tools is selected, so deselecting a
  whole server saves its connection cost too. Changes apply from the next
  message — the conversation survives via the same rebuild-and-resume path
  used for crash recovery.

Replies stream token-by-token; tool calls appear as chips as they happen;
gated writes pause on approve/deny buttons. One active conversation at a
time by design. Long conversations trim their oldest turns rather than
silently overflowing the model's context. If a backend dies mid-chat
(model server restart, MCP transport drop), the next message rebuilds the
sessions and resumes the conversation from its last saved reply.

Tip: dock it inside Obsidian with the core Web Viewer plugin (drag the tab
into the right sidebar) for a Copilot-style panel with zero extra code.
The layout is panel-aware: under ~700px the header collapses to icons
(server status becomes a health dot — hover it for details), and under
~480px the context meter keeps just the bar. Nothing is lost, only shrunk;
every control keeps a hover tooltip.

Point it at a real vault with `-v`, `OBSIDIAN_VAULT_PATH`, or the `vault:`
key in `config/models.yaml`.

## Adding a model

Add an entry to `config/models.yaml` — that's the whole procedure:

```yaml
homelab:
  model: qwen3.5:72b
  base_url: http://192.168.1.50:8000/v1   # vLLM, llama.cpp, Ollama, mlx-vlm — anything OpenAI-compatible
  max_iterations: 25
  max_tool_result_chars: 12000
  confirm_writes: true        # write-approval gate default for this model
  # servers: [obsidian]       # optionally narrow the tool surface
```

Ollama note: its OpenAI endpoint uses the model's default context length.
Raise it with `OLLAMA_CONTEXT_LENGTH=16384 ollama serve` or a Modelfile.
Gemma 4 note: the `gemma4_unified` checkpoints load in **mlx-vlm** (not
mlx-lm), whose server provides full OpenAI-style tool calling.

## The MCP server standalone

The tool server is independent of the harness — plug it into Claude Desktop,
Claude Code, or any MCP client:

```sh
OBSIDIAN_VAULT_PATH=~/Vaults/personal uv run obsidian-mcp
```

Tools (16): `list_notes`, `read_note`, `search_vault`, `create_note`,
`append_to_note`, `replace_section`, `update_frontmatter`, `list_tasks`,
`add_task`, `set_task_status`, `create_board`, `read_board`, `add_card`,
`move_card`, `open_daily_note`, `create_meeting_note`.

The read-only files server runs standalone the same way:
`FILES_ROOTS="~" uv run files-mcp` (`find_file`, `search_text`, `list_dir`).

Design intent: semantic tools own format correctness (a model never hand-edits
kanban markdown), general tools (`search_vault`, `read_note`, `replace_section`)
keep capable models uncaged. Results are paginated/truncated by the harness, and
whole-file rewrites don't exist — edits are anchored to headings, tasks, cards.
Board rewrites are round-trip safe: card descriptions, sub-items, and any
other content the harness doesn't model are preserved verbatim (and move
with their card).

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
uv run pytest      # 91 tests, ~4s
uv run ruff check src tests
```

Tool smoke tests run every vault tool against a temp fixture; guarantee
tests prove the harness promises (loop guard, write gate, empty-reply
recovery, streaming assembly, context trimming, docs source URLs, kanban
round-trip safety, snapshot fail-loud) against a scripted fake model — no
model or network required.

## Evals — measuring model swaps

```sh
uv run harness-eval                    # full suite vs the default profile
uv run harness-eval -m qwen3.5-9b      # compare another profile
uv run harness-eval -k kanban          # subset by name
```

23 fixture cases in `evals/cases/*.yaml`, each run against a pristine copy of
`evals/vault` (19 open tasks with known type counts; date placeholders like
`{tomorrow}` resolved at run time so cases never go stale). Scoring checks
the resulting files and board/task structure — never the model's prose —
plus regression cases for every observed failure class: duplicate writes,
spurious mutations on read-only questions, denied-write honesty, multi-turn
reference resolution. Reports (md + json) land in `evals/reports/`, and a
comparison dashboard lives at `/evals` in the web UI (model × case matrix,
click a ✗ for failure details).

Current baselines: gemma4-12B (mlx) 22/23 · qwen3.5-9B (ollama) 16/21.

Run it when you: swap models, edit the system prompt, add or change a tool,
or change toolset scoping. New model = new profile entry + one eval run =
a scored answer instead of vibes.

## Status & roadmap

Built and verified: everything above. Remaining, in
[PHASE2.md](PHASE2.md) / [PHASE3.md](PHASE3.md):

- **Cloud escape-hatch profile** (Gemini Flash class) — wiring is one
  profile entry + an API key; then an eval run to score cloud vs local.
- **Real-vault onboarding** — `.obsidian/` snapshot hygiene, single
  auto-committer decision, launchd auto-start.
- **Jira via Atlassian's remote MCP** (OAuth 2.1) — endpoint already
  configured but disabled; includes `enable_toolset` dynamic tool loading
  to keep its ~25 tools out of context until needed.
