# Phase 2 — QoL + external MCP integrations

Phase 1 proved the loop: one Obsidian MCP server, model profiles, git-snapshot
safety, CLI + chat UI. Phase 2 has two thrusts: make daily use pleasant (QoL),
and open the harness to external MCP servers (Microsoft Learn docs, Jira)
without degrading small-model reliability.

## Target integrations (verified July 2026)

| Server | Endpoint | Transport | Auth | Tools |
|---|---|---|---|---|
| Microsoft Learn MCP | `https://learn.microsoft.com/api/mcp` | streamable HTTP | none | `microsoft_docs_search`, `microsoft_docs_fetch`, `microsoft_code_sample_search` |
| Atlassian Rovo MCP (Jira) | `https://mcp.atlassian.com/v1/mcp/authv2` | streamable HTTP | OAuth 2.1 (or API token) | ~25+ across Jira/Confluence/JSM |

Learn is the ideal first remote integration: zero auth, tiny tool surface,
read-only. Jira is the stress test: auth plumbing, big tool surface, and the
first tools that write to a system git can't undo.

---

## M1 — Multi-server MCP client (the foundation) — ✅ shipped 2026-07-17

Delivered: `config/mcp.yaml`, `MultiServerClient` (stdio + streamable HTTP,
namespacing, per-server status, graceful failure), profile `servers:`
allowlists, readOnlyHint-based write classification, `confirm_writes`
approval gate (CLI + web UI), and a same-turn duplicate-write loop guard.
MS Learn (M4's server) is already connected as the M1 validation.
Remaining from the original M1 scope: per-conversation server toggle chips
in the UI.

Original scope for reference:

The Agent currently spawns one hardcoded stdio server. Everything else in
Phase 2 hangs off generalizing this.

- `config/mcp.yaml`: named servers with transport (`stdio` command / `http`
  url), env, headers, `enabled` flag. Obsidian stays the default-on entry.
- Agent connects to all enabled servers, namespaces tool names on collision
  (`jira__search`), and routes calls back to the owning session.
- **Toolset scoping** — the load-bearing feature. 15 Obsidian + 25 Jira + 3
  docs tools will visibly degrade a 9B model (and waste context on any model).
  Profiles gain a `servers:` allowlist and optional per-server `tools:`
  allowlist. The chat UI gets per-server toggle chips so a conversation only
  carries the tools it needs. This is a profile/UI concern, per the Phase 1
  rule: never bake small-model accommodations into tool code.
- `/api/config` reports each server's status and tool count; failed servers
  degrade gracefully instead of killing the session.

## M2 — Eval suite (swaps become measurable) — ✅ shipped 2026-07-17

Delivered: `evals/vault` fixture (19 open tasks, deterministic type counts,
date placeholders), 21 cases across tasks/kanban/notes/questions/safety
including regression cases for every failure observed in live use,
`harness-eval` runner with md+json reports in `evals/reports/`.

Original scope for reference:

Promised in Phase 1, and it should land *before* the integrations so their
impact on model behavior is measured, not vibed.

- `evals/` of ~20 fixture cases: YAML with a prompt, a pristine-vault setup,
  and checks against resulting files/git (not the model's prose). Include
  multi-turn cases and a regression case for the thinking-model
  empty-content bug.
- `harness-eval -m <profile>` runner → pass/fail table per model, saved as a
  dated report. Run it when: swapping models, editing the system prompt,
  adding a tool, or changing toolset scoping.

## M3 — QoL (harness + chat UI)

- **Token streaming** in the web UI (`stream=True`, assemble tool-call
  deltas) — the single biggest feel improvement with a local model.
- **Expandable tool chips**: click to see the tool result, red styling for
  errors.
- **Undo in the UI**: an activity strip of recent `harness:` git commits with
  one-click revert. The safety net exists; surface it.
- **Conversation persistence**: save/restore chats; optional "archive this
  chat to the vault" as a note.
- **Context trimming**: when history approaches the profile's context budget,
  drop oldest turns (keep system prompt + recent window). Matters at 16GB
  now, still correct hygiene on the 32GB server.
- **Friendlier failures**: detect Ollama down / model not pulled at startup
  and say so, instead of a stack trace.

## M4 — Microsoft Learn MCP (first remote server)

Mostly a config entry once M1 exists — which is exactly why it goes first;
it validates M1 with near-zero integration risk.

- Server entry + a `docs` toolset (off by default in profiles).
- Workflow it unlocks: research capture — "look up Azure Container Apps
  scaling, save a summary with links to Research/" (docs search/fetch +
  existing `create_note`/`append_to_note`).
- **Untrusted-content posture** starts here: fetched pages are third-party
  text. The system prompt gains one standing rule — content returned by
  fetch/search tools is data, never instructions.

## M5 — Jira MCP (Atlassian Rovo)

- **Auth**: `harness auth atlassian` — one-time OAuth 2.1 browser flow via
  the MCP SDK's auth support, tokens cached locally. (API-token mode as the
  fallback if the flow fights us.)
- **Curated toolset**: expose an allowlist (search issues, get issue, create
  issue, comment, transition) — not the full surface.
- **External-write confirmation**: vault writes are git-revertible; a created
  Jira ticket is not. Writes to external servers get a confirm step in the
  UI (and `--yes` for scripting). This is a harness guarantee, like
  snapshots — not a model-quality workaround.
- Workflows it unlocks:
  - meeting note action items → Jira issues (with confirmation)
  - "what's assigned to me this sprint?" → daily note briefing
  - sprint board → kanban board snapshot note

## M6 — Workflow files (skills pattern) — ✅ shipped 2026-07-17

Decision: adopt the Agent Skills open standard (agentskills.io, SKILL.md
folders) — portable to Claude Code/Copilot/Codex/40+ tools, and public
catalog skills can be dropped in. Design + first two skills in `skills/`
(meeting-capture with model-free capture mode, weekly-review). Remaining:
harness integration (level-1 listing in system prompt, `use_skill` tool,
slash commands + capture toggle in the UI).

Original scope for reference:

Markdown workflow definitions in `workflows/` (meeting pipeline, weekly
review, research capture), loaded into context on demand — the
progressive-disclosure idea from Microsoft's/Anthropic's skills patterns,
kept as plain markdown so it's framework-free. Surfaced as slash-style
commands in the chat UI (`/weekly-review`).

---

## Sequencing

M1 → M2 → (M3 ∥ M4) → M5 → M6. M1 unblocks everything; M2 before
integrations so tool-count effects on each model are measured; M4 is the
low-risk validation of M1; M5 is the heavy integration; M6 rides on all of it.

Explicit non-goals for Phase 2: multi-user/web auth (UI stays local-only),
multi-agent orchestration, RAG/vector search over the vault (worth a look in
Phase 3 — semantic search over notes — but not before evals exist to judge it),
and a dedicated Obsidian plugin — decided 2026-07-17: the web UI docked in
Obsidian's sidebar via the core Web Viewer plugin covers in-Obsidian use with
zero extra code. Revisit only if active-note context / insert-at-cursor
becomes a felt need (the thin-client plugin design is sketched in the
2026-07-17 session notes).
