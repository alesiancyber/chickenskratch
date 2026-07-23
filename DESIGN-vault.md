# Design: the vault spec — layout, entity types, and vault-as-UI

*Status: **largely BUILT** (DEVPLAN Phases 0–4, 2026-07-22): the
`config/vault.yaml` spec, folder→type mapping, entity schemas +
stamping, `Private/` tool-layer exclusion, the vault `.gitignore` venv
rule, and the `Harness/Review.md` dashboard surface are all implemented
and tested. Not yet built: `Harness/Capture.md`, `Harness/Changes.md`,
the §4 opt-in entity menu, and grouping (see §9 progress). This doc defines
what the vault IS; the top-level principles now live in
DESIGN-philosophy.md (adopted 2026-07-22), which supersedes §1's
"stays in web" table — the web app is an admin console, and approvals
are vault-side. DESIGN-processors.md carries the engine/commands
direction forward.
DESIGN-vault-schema.md supplies the schema/id mechanics for the entity
types declared here; DESIGN-review-dashboard.md's pipeline stands, with
its §4 web-pane UI superseded by the vault surfaces in §5 below.*

## 1. The decision: vault-first UI

Every feature the harness grows must answer one question first: **can
Obsidian render this?** If yes, it ships as a note the harness writes, not
as web-UI development. The trade accepted here, explicitly: we spec the
vault to a default config — folders, entity types, schemas — so the
harness can *guarantee* behavior against a known structure, and in exchange
the UI surface gets drastically cheaper because Obsidian is the renderer.
The user is in Obsidian while taking notes; changes should appear where
the eyes already are (rule 1 already delivers live pickup of every harness
write — this design just aims more of the product at it).

What stays in the web UI — *table superseded by DESIGN-philosophy.md
principle 1 (web = admin console: chat escape hatch, mid-chat confirm,
servers, evals/stats, logs; approvals are vault-side via Review.md)*:

| Stays in web/CLI                  | Why                                    |
|-----------------------------------|----------------------------------------|
| Chat (streaming, tool activity)   | Interactive generation isn't a note    |
| Approve/deny prompt (confirm_writes)| Blocks a live generation mid-turn    |
| ⚡ Servers panel (process mgmt)    | Process state is not vault content     |
| Evals / stats pages               | Developer tooling, not knowledge       |

Everything else — capture, review queue, proposal diffs, recent-changes
visibility — moves vault-side (§5). No new web panels get built.

### What lives in the vault vs `data/`

**Vault = knowledge plus review surfaces** (things a human reads/edits in
Obsidian). **`data/` = harness internals** (chats, proposal JSON, stats,
bug reports, corpus, watcher state). Where the human needs to *see*
harness internals, the harness renders a markdown **view** of them into
`Harness/` (§5); the JSON stays authoritative in `data/`. Views are
regenerated, never hand-merged.

## 2. Default layout (formalizing what testvault already is)

```
vault/
  Home.md          # user's own map-of-content — harness never writes it
  Daily/           # daily notes (open_daily_note)
  Meetings/        # meetings AND their raw captures — one broad folder
  Tasks/           # the task lists: Inbox.md, Waiting.md, Owed.md
                   #   (type: deliverable), Reading List.md (type: research)
  Projects/        # project notes + kanban boards (kanban-plugin typed)
  Scripts/         # scripts + their notes; venvs GITIGNORED (§6)
  Harness/         # machine surfaces: Review.md, Capture.md, Templates/ (§5)
  Private/         # never enters model context (§7)
  Archive/         # graduation target for done/stale content [when needed]
```

**Folders are broad workspaces, not one-per-type** (the 2026-07-22
consolidation). A folder-per-entity-type layout makes the sidebar a
taxonomy instead of a workspace; instead the folder map supplies only
the *default* type for untyped notes, and a note's own signals always
win — `kanban-plugin` frontmatter ⇒ kanban, then frontmatter `type:`,
then the map. So `Tasks/Owed.md` is a deliverable and `Tasks/Reading
List.md` is research while sitting next to the inbox, and consolidation
costs nothing mechanically. New entity types get a schema file and a
`type:` value, not a folder.

**The vault describes itself** (2026-07-23): the live source for the
user-facing spec — the folder→type map, capture/waiting inboxes, and
`Private/`-style exclusions — is the frontmatter of **`Harness/Spec.md`
inside the vault**, edited in Obsidian like any note and re-read every
engine pass (no restart; the note is seeded once from
`config/vault.yaml`'s defaults and never regenerated). The repo YAML
keeps only the defaults plus machine concerns (surface paths,
`schemas_dir`). Drift closes in both directions: a new top-level folder
with notes in it produces a `spec_drift` proposal in Review.md mapping
it as `other` (a real entity type is a hand edit to Spec.md — that
decision belongs to the user, not a worker). `_note_type` reads the
effective mapping; an unmapped folder falls back to today's behavior,
so a hand-grown vault keeps working — the spec is a guarantee layer,
not a straitjacket.

## 3. Entity types (the 80% set)

Mechanics (ids, jsonschema, stamping, validate_schema) per
DESIGN-vault-schema.md. Declared fields per type:

- **task** — checkbox line; `^t-` id; optional due/priority (Tasks-plugin
  emoji syntax, as today). Todo == task; one type, no alias.
- **deliverable** — *a task with accountability made mechanical*: same
  line format plus **required** `for:` (a person) and **required** `due:`.
  Lives wherever it's useful — `Tasks/Owed.md` in practice — typed by
  frontmatter `type: deliverable`, with `[for:: Name]` inline fields
  marking per-line ownership (as built; the folder-based variant lost). The distinction
  exists **because the schema enforces something different**: a
  deliverable missing an owner or a date is nonconforming and lands in
  the review queue; a task missing them is fine. If that enforcement
  stops mattering in practice, delete `deliverable.yaml` and the type
  folds back into task — the schema config is the experiment's off
  switch.
- **meeting** — frontmatter `id`, `type: meeting`, `date`, `attendees`;
  body sections per the template incl. Action items (a section the
  extraction worker already targets well).
- **research** — frontmatter `id`, `type: research`, `status:
  open|active|parked|done`, `tags`; **required `## Sources` section of
  ranked links**. Sources-as-links is schema-enforced, not stylistic:
  research value here is the URL trail, and a research note whose Sources
  section is empty while its body cites claims is exactly what
  `validate_schema` should flag. (Also the natural landing pad for the
  docs-domain link-digest guarantee the harness already has.)
- **kanban** — frontmatter `id`, `kanban-plugin`, plus optional
  **`project:` / `client:`** — the boundary metadata the grouping design
  needs (DESIGN-review-dashboard.md §3's customer-boundary rule gets its
  signal from here, structurally, not from text similarity).
- **daily** — skeleton as today (`## Tasks`, `## Notes`); no id required
  (dailies are append surfaces, not entities; tasks *inside* them are
  entities).

## 4. Security-engineer additions (opt-in menu, not default bloat)

For the cybersecurity research/dev workflow — each is one schema file plus
a folder mapping; adopt when first needed, in this order of likely value:

- **detection** — `Detections/`; frontmatter `id`, `status:
  idea|draft|testing|deployed|retired`, `platform` (sigma/kql/yara/…),
  `data_source`, `fp_notes`; rule body in a fenced block. This is "review
  X detection" as a first-class entity: the 5-duplicate-versions problem
  from the dashboard design becomes a status-tracked pipeline instead of
  scattered tasks — and detection dedup/grouping gets its scope boundary
  (`platform` + `data_source`) for free.
- **snippet** — `Snippets/`; the one-liners/commands cheatsheet that
  otherwise rots in dailies. `id`, `tags`, fenced block. Cheap, high
  80%-value.
- **person** — `People/<name>.md`, minimal (`id`, name, org). Exists so
  `deliverable.for` can be a `[[link]]` instead of a string — one
  backlink click shows everything owed to someone. Adopt only if/when
  deliverables prove out.
- **engagement/project** — only if `project:` on boards + a folder stops
  being enough. Don't pre-build.

Deliberately absent: IOC databases, finding/report generators, credential
inventories — the 20% tail, and the last one is banned outright (§7).

## 5. Vault surfaces — the UI work that no longer happens in HTML

- **`Harness/Review.md`** — the review dashboard
  (DESIGN-review-dashboard.md, §4 superseded): watcher renders pending
  proposals as sections — title, summary, ` ```diff ` block (Obsidian
  highlights these natively), and a checkbox line
  `- [ ] approve p-20260722-a1b2c3`. Checking it is the approval: the
  watcher's next pass (pane… now note-open is invisible to us, so: the
  running web server polls this one file cheaply, plus every
  `harness-watch` run) sees the check, runs the staleness guard, applies
  through gated tools, rewrites the section as ✅ applied. Guards that
  make checkbox-as-command safe: approval anchored to the proposal id on
  the same line (a stale rendering can't approve the wrong thing);
  **the watcher ignores its own writes to `Harness/`** (no self-trigger
  loop); apply is idempotent per proposal id.
- **`Harness/Capture.md`** — the capture button, retired to a note: type
  or paste into it (already in Obsidian, already typing); the watcher
  treats new content as capture input — extraction worker runs, proposed
  tasks land in Review.md. Extraction distinguishes ownership
  (2026-07-22): the user's own actions go to `capture_inbox`
  (`Tasks/Inbox.md`); commitments *other people* made ("Nadia will send
  X") are follow-ups, rendered `Name: what they owe` in `waiting_inbox`
  (`Tasks/Waiting.md`) — the incoming mirror of Owed.md's outgoing
  deliverables. The web capture buttons stay for
  voice/CLI-side capture but gain no further development.
- **`Harness/Changes.md`** — rendered view of recent harness commits
  (subject, time, files) so "what did it just do" is visible in Obsidian.
  Revert stays a web-UI/CLI action in v1 — a checkbox that reverts
  commits is a destructive-action-from-a-text-file, deliberately not
  built until checkbox-approve has earned trust on the additive case.
- `Harness/` is excluded from schema scans and from grouping candidate
  sets (machine-rendered, not knowledge), but **not** from git snapshots
  (surface history is history).

## 6. Scripts/ and the future Python runtime

`Scripts/` holds scripts and their notes. Two hard rules, one now, one
reserved:

1. **venvs never enter vault git.** Rule 3 snapshots every mutation; a
   `.venv` adds thousands of files to every snapshot and wrecks the
   Changes surface, revert granularity, and repo size. Vault `.gitignore`
   ships with `Scripts/**/.venv/`, `__pycache__/`, `*.egg-info/` from day
   one. (Alternative if this chafes: venvs live outside the vault under
   `~/.venvs/`, and Scripts/ notes point at them. Either is fine;
   in-vault-but-ignored is simpler.)
2. **Execution is not a vault write — it gets its own design.** The
   planned Python runtime is a new capability class: git snapshots
   protect content, not consequences of running code, so none of the
   existing write guarantees apply. Reserved for a future
   DESIGN-runtime.md (sandboxing, per-script approval, what
   confirm_writes even means for execution). Until then the harness
   reads Scripts/ (files-mcp is read-only) and never runs anything.

## 7. Security posture of the vault itself (non-optional for this user)

The vault's text is fed to LLMs — local today, and a cloud escape hatch
exists (DESIGN-mixed-models.md Tier 4). For a security engineer's vault
that makes two rules load-bearing:

- **No secrets in the vault, ever.** No credentials, tokens, keys, client
  data dumps. The vault is git-history'd (deletion isn't removal),
  model-context'd, and one explicit profile switch from leaving the
  machine. Secrets live in a real secret manager; vault notes hold
  pointers ("creds in 1Password: *acme-vpn*").
- **`Private/` never enters model context.** Enforced in code at the tool
  layer (`iter_notes`/`read_note`/`search_vault`/`list_tasks` skip
  configured exclusions from `vault.yaml`), not by prompt convention.
  Client-sensitive material that must be in the vault goes there; the
  harness is structurally unable to read it into a model — which also
  makes the Tier-4 cloud rung safe *by construction* for those notes
  rather than by discipline.

## 8. What this changes in the existing docs

- **DESIGN-vault-schema.md** — unchanged in mechanics; gains `deliverable`
  /`research`/optional-type schema files, and `vault.yaml` becomes where
  folder→type mapping lives.
- **DESIGN-review-dashboard.md** — pipeline, queue, staleness, grouping,
  phases all stand. §4 (web pane + routes) superseded by
  `Harness/Review.md`; the approve endpoint survives as the code path the
  checkbox triggers. D0 gate gains: checkbox-approve round-trip works
  live in Obsidian.
- **DESIGN-training-corpus.md** — untouched; Review.md approvals flow in
  as `source: "dashboard"` rows as designed.
- **Web UI** — feature-frozen except chat, confirm, servers, evals/stats.
  No proposals panel is built.

## 9. Order of work — progress

1. ✅ `config/vault.yaml` + folder→type mapping in `_note_type` + vault
   `.gitignore` (venv rule).
2. ✅ Schema files for the §3 core set + stamping + `validate_schema`
   (DESIGN-vault-schema.md, built).
3. ✅ `Private/` exclusion — enforced in `Vault.resolve`/`iter_notes`,
   one choke point covering reads, searches, listings, writes, and
   fuzzy suggestions.
4. ✅ Watcher + queue + `Harness/Review.md` render + checkbox-approve
   (dashboard doc D0–D1 machinery; usage gates still open).
5. ✅ `Harness/Capture.md` (DEVPLAN 5.1: skeleton surface, extraction →
   capture proposals, approve adds to `capture_inbox` and clears).
   ⏳ Then Changes.md; then the §4 menu as needed — still gated behind
   the dashboard being dialed in real use.
