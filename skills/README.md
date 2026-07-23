# Skills — the workflow layer

Workflows for this harness follow the [Agent Skills](https://agentskills.io)
open standard: each workflow is a folder with a `SKILL.md` (YAML frontmatter
`name` + `description`, markdown body of instructions). Because it's the
same format Claude Code, Copilot, Codex, and ~40 other tools consume, a
skill written here is portable to any of them, and public catalog skills
can be dropped in unchanged.

## How the harness uses skills (design)

**Progressive disclosure, two levels** — the reason this format exists:

1. **Level 1 (always in context):** the system prompt carries only each
   skill's `name` + `description` (one line per skill). Cheap for small
   models; enough for the model to know a workflow exists.
2. **Level 2 (loaded on demand):** the full SKILL.md body enters context
   only when invoked, via either path:
   - **Slash command** (user-initiated): typing `/meeting-capture` in the
     chat UI loads the skill body and runs it with whatever follows.
   - **`use_skill` tool** (model-initiated): a synthetic read-only harness
     tool; the model calls it when a request matches a skill's description.

**Rules for skill bodies in this harness:**

- Keep bodies short (~500 words). They share context with 15+ tool schemas
  on local models.
- Instructions reference the vault's own templates (`Harness/Templates/*.md`) rather
  than embedding formats — users change formats by editing their vault, not
  the skill.
- Skills describe *procedure and judgment*, not tool mechanics — the tools
  already document themselves.
- A skill must never instruct the model to bypass harness guarantees
  (write confirmation, loop guard, git snapshots).

## Capture mode (companion feature, ships with meeting-capture)

Stream-of-consciousness input needs zero latency and zero failure modes, so
capture is model-free by design:

1. Toggle **Capture** in the chat UI (or `harness capture` in the CLI).
   Every line typed is appended verbatim to `Meetings/{date}-{slug}.md` —
   no model round-trip, nothing to go wrong mid-meeting.
2. Toggle off (or `/done`) → invokes the `meeting-capture` skill pointed at
   the capture note. The model structures it into a real meeting note.
3. The raw capture is never deleted and stays linked from the meeting note:
   the model organizes your data but is never its custodian. A bad
   formatting pass is a free retry, not a loss.

## Current skills

| skill | purpose |
|---|---|
| `meeting-capture` | structure a raw capture note into a formatted meeting note with extracted action items |
| `weekly-review` | summarize the week: done tasks, overdue rollover, review note |
