# Subtraction ledger — the markdown surfaces, after Obsidian stopped being a surface

*Scoping doc, 2026-07-26. Follows the deep-link removal in the same
day's owned-surfaces amendment. Question asked: now that tezcat is the
front end, do `Harness/Review.md`, `Today.md`, `Changes.md`, and
`Capture.md` still earn their code? Nothing here is executed.*

**The honest headline first.** Total surface machinery is ~300 Python
lines and ~80 Rust lines out of ~8,900 — call it **4%**. This is not a
big code win, and anyone selling it as one is selling the wrong thing.
The real wins are (a) **per-pass work**: Today.md costs a full vault
walk and Changes.md costs a git-log walk *plus a worker-reachability
network probe*, every single pass, purely to write two notes; and (b)
**one consent channel instead of two**, which is a concept the whole
engine currently pays for. Judge the tiers on those, not on line count.

---

## Tier 1 — Free: stop *writing* Today.md and Changes.md

These are write-only mirrors. Nothing parses them back; the dashboard
renders the same state from the same collectors, better.

| Dies | Where | ~Lines |
|---|---|---|
| `to_markdown` + the six `_*_md` row helpers + `_link`/`_cap` | `today.py` | 91 of 295 |
| `HEADER` + `render` | `changes.py` | 27 of 72 |
| `_render_today`, `_render_changes`, `_worker_status` | `watcher.py` | 40 |
| write-on-change guard for two of three surfaces | `watcher.py` | — |

`today.collect`, `changes.recent_news`, `machine_rels`, and
`is_machine_render` all **stay** — the dashboard and `/api/changes` are
their real callers.

**Per-pass savings (the actual prize):** no vault walk for
`today.collect`, no `v.history()` walk, and no `endpoint_alive_sync()`
network probe per pass. Two fewer commits of render churn.

**What you lose:** with no UI process running, there is no daily view
and no audit list. Today that degrades to Obsidian; after this it
degrades to nothing.

**Cheap mitigation, and the reason this tier is "free":** keep
`today.to_markdown` and `changes.render` as *renderers* and hang them
off the CLI (`harness today`, `harness changes` — ~15 lines each,
printing to stdout). Then nothing is lost, ~40 lines of surface-write
machinery still dies, and the per-pass cost goes away regardless. The
expensive part was never the renderer — it was writing into the vault,
committing it, and guarding the churn.

**Canon impact:** small. Principle 1's "attention via Today.md" and
I5's machine-surface list both need editing.

---

## Tier 2 — The real subtraction: retire the Review.md checkbox channel

This is where meaningful complexity lives, because a checkbox is an
*asynchronous consent channel* and the engine is shaped around that.

| Dies | Where | ~Lines |
|---|---|---|
| `DECISION_RE`, `_read_decisions` | `watcher.py` | 7 |
| `_render` (Review.md, incl. the mid-pass "don't overwrite a checked box" guard) | `watcher.py` | 49 |
| `_diff_block` (the ```diff fence for the surface) | `watcher.py` | 14 |
| `_sweep_late_decisions` (boxes checked *while* a pass runs, ×3 retry) | `watcher.py` | 16 |
| `_render_surfaces(review_only=True)` fast-feedback branch | `watcher.py` | ~8 |
| the entire daemon fast lane: `--fast-file`/`--fast-cmd`/`--fast-interval`, config parse arms, event-loop and poll-loop arms | `daemon/src/main.rs` | ~80 |
| `decisions_only` fast-lane plumbing | `watcher.py`/`service.py` | ~20 |

`Watcher._act`, `applier`, `proposals`, staleness, and the corpus record
are **untouched** — the UI already resolves through `_act` under the
same pass lock. This deletes a *channel*, not the decision machinery.

**What you lose — say it plainly:** approving becomes possible only
while `harness-ui` is running. Right now a checkbox works from any
editor, on any machine that syncs the vault, with the engine catching up
later. That asynchrony is genuinely useful (phone → synced vault →
checkbox → next pass applies) and it is not an Obsidian feature, it's a
*file* feature.

**Mitigation if you want it gone anyway:** `harness decide <pid>
approve|reject` (~20 lines through `service.decide`). Consent then lives
in two places we own — dashboard and CLI — and never in another app.

**Canon impact: large.** I2 is written as "the Review.md checkbox or a
UI decision"; principle 1's degraded-mode promise ("decisions via
Review.md checkboxes") and the plan's degraded-mode contract both go.
This is a constitutional amendment, not a refactor.

**Test blast radius:** `test_watcher.py` (20 tests), `test_service.py`
(23), `test_auto_apply.py` (16), `test_diffs.py` (5) all exercise the
checkbox path somewhere. Expect a day of test rewriting, and expect the
rewrite to be where the actual risk is.

---

## Tier 3 — Do not touch: Capture.md

Looks like a compat surface, isn't one. `service.ingest` writes the
canvas dump into Capture.md **first**, then runs the pass that extracts
from it. That write is snapshot-bracketed, so a dump survives extraction
failing, the engine being busy (`deferred`), or a crash — and it's
revertible. `scan_capture` then reads it back.

Removing it doesn't delete code, it *relocates* it into a new
text-in-memory ingest path, and it costs the durability guarantee the
canvas's "saved to Capture.md — the engine was busy" message depends on.
**Recommendation: keep, and stop calling it a compat surface.** It's the
canvas's write-ahead log that happens to be readable.

Same verdict for `Harness/Spec.md`: it's vault-side *config*, and it's
now edited in our own editor. Nothing Obsidian about it.

---

## Tier 4 — The sweep: Obsidian-shaped conventions still in the data model

These are formats we took, per the 07-26 decision. They live in the
user's notes, so changing them means rewriting the vault for ~zero code
savings:

- `📅 YYYY-MM-DD` / `✅` task syntax — `obsidian_mcp/tasks.py`
- `kanban-plugin: board` frontmatter — `kanban.py`, `grouping.py`,
  `today.py`, `server.py`
- `[[wikilinks]]` and `^t-…` block ids — `today.py:_link`, `ids.py`
- `> [!NOTE]` callouts — `commands.py:render_callout`

**Recommendation: keep all of it, reframe none of it as compatibility.**
These are tezcat's formats now; that they're also Obsidian's is the
byproduct. (`> [!NOTE]` renders fine as a blockquote anywhere, and the
emoji dates are a reasonable format on their own merits.)

The one genuine artifact worth changing is the **package name
`src/obsidian_mcp/`** — 86 references across 38 files. Purely
mechanical, zero behavior, high symbolic value now that the name asserts
something we've explicitly abandoned. Do it alone, in its own commit, or
not at all.

---

## Recommendation

1. **Do Tier 1 now**, with the CLI renderers. Real per-pass savings, no
   functionality lost, canon edits are two sentences.
2. **Defer Tier 2** until you've lived on the dashboard for a week and
   know whether you miss checking a box from a synced editor. If you
   don't, take it — it's the only change here that removes a *concept*
   rather than lines. Pair it with `harness decide` in the same commit.
3. **Never Tier 3.**
4. **Tier 4 is a rename**, not a subtraction. Cosmetic, cheap, separate.


---

## Executed 2026-07-26 — Tier 1 + Tier 2, with the CLI fallbacks

User decision: the vault stays single-machine and local ("if it ever
ends up being multi device im working too much"), which removes the one
scenario the checkbox channel defended. Both tiers taken.

**Deleted:** `Watcher._render`, `_diff_block`, `_read_decisions`,
`DECISION_RE`, `_sweep_late_decisions`, `_render_surfaces`,
`_render_surface`, `_render_changes`, `_render_today`, `_worker_status`
(watcher.py: 824 → 646 lines); tezcatd's `--fast-file`/`--fast-cmd`/
`--fast-interval-ms` lane and both loops' arms for it; the
`--decisions-only` CLI flag; `review`/`today`/`changes` from
`config/vault.yaml` surfaces.

**Kept, re-homed:** `today.to_markdown` and `changes.render` are now
what `harness today` and `harness changes` print. `harness decide <pid>
approve|reject` is consent with no UI process — it goes through
`service.decide`, so it takes the same pass lock and the same `_act` as
the dashboard button.

**Kept, untouched:** `Harness/Capture.md` (Tier 3), and the Obsidian-shaped
content conventions (Tier 4).

**Two things the removal taught us**, both worth more than the line count:

1. **The surface renders were an accidental heartbeat.** Every pass
   committed a re-render, and `commit_if_dirty` swept untracked notes
   into git as a side effect. That masked `applier.vault_state()` being
   blind to untracked file *content*: `git status --porcelain` prints
   `?? Tasks/new.md` no matter what changed inside it, so a `//command`
   typed into a brand-new note never fired. Fixed by hashing untracked
   files into the fingerprint; regression test in `test_resilience.py`.
2. **A stale flag must fail, not be ignored.** tezcatd now *rejects*
   `--fast-file` (unknown flag) rather than accepting and ignoring it —
   a leftover launchd plist that silently dropped the lane would leave
   someone believing checkbox decisions still worked.

**Not done, deliberately:** the three retired notes are still on disk in
existing vaults. Nothing reads or writes them, but a leftover
`Harness/Review.md` still *looks* actionable — ticking a box in it now
does nothing. Deleting user-visible files is the user's call, not a
migration the engine should perform.
