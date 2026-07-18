# Phase 3 — Job-ready

Context: the harness becomes a daily tool in a few weeks with a new job and
a smaller AI budget. Local models handle the routine work; a cloud profile
(Gemini Flash class) is the escape hatch for hard problems. Everything below
is ordered, and every step has an explicit evaluation gate.

## Step 1 — Hardening & maintainability pass (do first, everything rides on it)

Known debts from an honest self-review:

- **Boilerplate triplication**: cli.py, web.py, and evals.py each re-implement
  config loading + vault resolution + server-spec building. Extract one
  `bootstrap()` helper in a shared module.
- **Untested guarantees**: the loop guard, empty-reply nudge, and write gate
  are the harness's core promises, but they're only exercised end-to-end via
  slow model-dependent evals. Add fast unit tests with a fake OpenAI client
  and fake MCP client so `pytest` proves them in milliseconds.
- **Duplicated theme CSS** between index.html and evals.html → one shared
  stylesheet.
- **No linter**: add ruff (format + lint) to keep future sessions consistent.
- **Friendly failures**: detect "Ollama/MLX server not running" at startup
  and say so plainly instead of a stack trace.
- Small tool debts: `due_on` param on list_tasks (Gemma's eval miss),
  `create_board` tool, 2 error-recovery eval cases.

**Evaluate:** full pytest green including new guarantee tests; ruff clean;
eval suite re-run on gemma — score ≥ 20/21 with due-tomorrow now passing.

## Step 2 — Approvals toggle in the UI

A switch in the chat header: Approvals on/off (default from the profile,
override per session, remembered in localStorage). Off = writes execute
directly (git snapshots still apply — the floor never moves).

**Evaluate:** flip mid-conversation; verify gated → ungated behavior on the
same request; verify the qwen profiles still default on, gemma default can
be relaxed once trusted.

## Step 3 — Token streaming

Stream model deltas over the existing SSE channel; render text as it
generates. Tool chips already stream — this closes the gap on prose.

**Evaluate:** measure time-to-first-visible-token before/after on a long
answer (expect ~15s → ~2-3s); confirm tool-call turns still render chips
correctly mid-stream.

## Step 4 — Undo strip (git without the git)

`/api/history`: last ~15 vault commits (harness + capture + manual).
UI: a collapsible "Recent changes" strip; each row = message + files +
one-click **Revert**. Revert = `git revert` server-side; conflicts surface
as a readable error, never a half-state.

**Evaluate:** make a change via chat → revert from UI → file verified
restored; revert the revert; attempt reverting an old commit that conflicts
and confirm the error is comprehensible.

## Step 5 — Chat persistence, 3-day retention

Conversations saved server-side (JSON under `data/chats/`); a simple list in
the UI; auto-prune anything older than 3 days (user decision: no long-term
chat archive — vault notes are the archive). Context trimming when a
conversation approaches the profile's window.

**Evaluate:** restart harness-web mid-conversation → resume it; verify a
>3-day-old chat is pruned on startup; verify a long conversation trims
oldest turns without losing the system prompt or recent context.

## Step 6 — Cloud escape-hatch profile

Add a `gemini-flash` profile via Gemini's OpenAI-compatible endpoint
(API key from env var, never in the repo). This is the budget model:
local for routine, cloud only when local fails.

**Evaluate:** run the full eval suite against it — doubles as the first
proof the harness is genuinely backend-agnostic beyond local serving, and
gives a cloud-vs-local scorecard so "when is cloud worth it" is data.

## Step 7 — Files MCP server — ✅ shipped 2026-07-18

Revised 2026-07-18: file-finding is an *agent* capability — "where's that
file?" is a chat message, not a terminal command. A new small, strictly
read-only `files` MCP server:

- `find_file(name, root?)` — fuzzy/partial name match → paths.
- `search_text(text, root?, limit/offset)` — content search → `path:line`.
- `list_dir(path)` — shallow listing.

Harness owns the ergonomics (skip .git/node_modules/.venv/caches,
case-insensitive, capped + paginated results); roots are an allowlist in
mcp.yaml with vault-style path-escape checks; all tools readOnlyHint so
they're never approval-gated. **No raw bash tool** — it would bypass every
harness guarantee and flood small-model context; revisit only with a
dedicated gated design if a real need appears.

Deliberately deferred: `enable_toolset` dynamic tool loading (the model
requests a toolset's schemas mid-conversation — skills-style progressive
disclosure for tools). The files server is only ~3 small schemas; the
mechanism pays for itself when a big tool surface (Jira, ~25 tools)
arrives. Design note kept here so it's ready then.

**Evaluate:** via chat: find a note by half its name, find a function in
this repo, find a config key — correct paths, no gating prompts; eval
cases added for find_file/search_text; confirm schema cost in context is
negligible (<600 tokens).

## Step 8 — Real-vault onboarding (when the job starts)

- Point config at the real vault; verify snapshots auto-init its git repo.
- Vault .gitignore for `.obsidian/workspace*` churn so snapshots stay
  meaningful.
- Decide the single auto-committer (harness snapshots vs obsidian-git
  plugin — recommend harness owns commits, plugin used for push/backup
  only, or disabled).
- launchd agents so mlx server + harness-web start on login.

**Evaluate:** one week of real use; then re-read the eval suite against
reality — every friction felt becomes either a fixture case or a skill.

---

Explicitly deferred: Jira/Atlassian MCP (until the job demands it),
Obsidian plugin (web viewer verdict stands), multi-user anything.
