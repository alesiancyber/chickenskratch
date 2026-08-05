# The Chickenskratch Guide

*How it works, what it does, and how to drive it. This is the
usability guide — setup lives in [README.md](README.md), and the
principles behind every design call live in
[PHILOSOPHY.md](PHILOSOPHY.md). This document assumes you can use a
terminal and know what a git commit is, and nothing else.*

---

## 1. The big idea

You have a folder of markdown notes (the **vault**). You write messy,
fast, unstructured text into it — chicken scratch. Chickenskratch is a
program that watches that folder and cleans up after you: it turns
brain-dumps into organized records, formats tasks it finds, fixes
notes that drift out of shape, spots duplicate tasks, and answers
questions from your own notes.

Three things make it different from "notes app with AI button":

1. **Your files stay plain markdown.** There is no database. Every
   note is a text file you can open in anything, forever. The AI layer
   sits *beside* your notes, never inside a format that traps them.

2. **The AI never gets free rein.** Every AI output is either checked
   by deterministic code before it lands, or queued as a *proposal*
   you approve with one keypress. And every change — yours or the
   machine's — is a git commit you can revert.

3. **You control the models.** Every AI call goes to an endpoint you
   configure — a cloud API today, a model running on your own GPU
   tomorrow — and every prompt it uses is a config file you can read,
   edit, and test. The whole point of the project is that the "turn
   notes into structure" loop runs on hardware and prompts you own.

The one-line mental model, from PHILOSOPHY.md:

> The vault is the source of truth. The AI is the maintenance layer.
> Every surface is just a renderer.

---

## 2. The pieces, on one screen

```
 you type ──► the vault (a folder of .md files, its own git repo)
                  │
                  ▼
            the engine (chickenskratch-watch)
            runs a "pass" when files change:
            walks the vault, notices work to do
                  │
     ┌────────────┼────────────────┐
     ▼            ▼                ▼
  processors   workers          proposals
  (code that   (small bounded   (queued changes
  decides what  AI calls, one   with diffs, waiting
  needs doing)  job each)       for your verdict)
                  │
                  ▼
            the applier (writes the change, snapshots it in git)
                  │
                  ▼
            the surfaces (what you actually look at):
            Today dashboard · canvas/editor · chat pane · console
```

Vocabulary you'll see everywhere:

| word | meaning |
|---|---|
| **vault** | your folder of markdown notes; also its own git repo |
| **pass** | one run of the engine over the vault (triggered by file changes, or the 6am heartbeat) |
| **processor** | one maintenance behavior (capture, schema fix, overlap, grouping, plan-today) |
| **worker** | one small AI call with one job, defined entirely by a YAML file in `config/workers/` |
| **proposal** | a change the engine *wants* to make, queued with a diff for your one-key verdict |
| **record** | what a brain-dump becomes: one note holding the rewrite, the tasks, and your verbatim text |
| **snapshot** | the git commit wrapped around every mutation — the undo unit |
| **Spec.md** | `Harness/Spec.md` inside the vault: the vault's own settings, editable like any note |

---

## 3. The core loop: a dump becomes a record

This is the main event, so here it is step by step. You open the
dashboard's canvas (or `Harness/Capture.md`) and dump:

```
met with priya and marcus about the azure baseline. legacy auth still
on for two tenants, marcus owns the remediation plan, wants a draft by
friday. we moved the review to biweekly. i need to send priya the log
volume numbers today
```

What happens, in order:

1. **The dump is committed first.** Before any AI touches it, your
   text is saved and snapshotted. If everything downstream fails, your
   words are safe. This ordering is deliberate and non-negotiable.

2. **The watcher notices** the file changed and starts a pass.

3. **One worker call.** The `extract_record` worker gets your dump and
   returns structured JSON: what *kind* of record this is (a meeting?
   a plain capture?), a title, attendees, a readable summary, and the
   tasks it found — each with optional due date, scheduled week,
   priority, and who owes it.

4. **The reply is validated against a schema.** The worker's reply
   must match a declared JSON shape exactly. A reply that drops a
   required field or invents a date format fails loudly and is
   retried once — it is never "cleaned up" silently.

5. **Code shapes the record.** Because this dump reads as a meeting,
   it becomes its own note under `Meetings/` (a shapeless dump would
   instead append to today's note under `Captures/`). The note gets:
   the summary, an **Action items** section with your tasks, a
   **Waiting on** section for things other people owe you (Marcus's
   remediation plan, marked `👤 Marcus`), and your *verbatim dump*,
   quoted, in a collapsed block at the bottom.

6. **The gate: `verify_record`.** Deterministic code — not a model —
   now checks the draft: exactly one file change; every task's words
   trace back to your dump (no invented content); every line of your
   verbatim rides in complete; every attendee is actually named in
   the dump. The summary is the one thing code can't vouch for, so:

   - a **bare task list** (no summary, everything checkable) can file
     itself automatically,
   - a dump that **earned a summary** queues as a proposal for your
     one-key approve — with the objection recorded if your autonomy
     policy is set to `full` and it files anyway.

   The collapsed verbatim underneath is the permanent human check:
   one unfold and you're comparing the rewrite to your own words.

7. **Snapshot.** The apply is one git commit. `chickenskratch-revert`
   (or the revert button on the dashboard) takes it back.

Why it's built this way: **the AI proposes, code verifies, you
decide.** The model is only ever trusted with judgment (what's a
task? is this a meeting?); everything checkable is checked by code;
everything unverifiable waits for you or is flagged behind you.

Tasks stay *in* the record — they are not moved to some central list.
**Location is provenance**: the dashboard finds every task by walking
every note, and clicking one opens the record it was born in.

---

## 4. Dates, the way the system thinks about them

Two different marks, deliberately not interchangeable:

- **📅 due** — a hard deadline. Rare on purpose. The extractor only
  sets one when your text states a real commitment ("by Friday",
  "urgent — today").
- **⏳ scheduled** — a plan, at *week* granularity. "This week" stamps
  this week's **Sunday**; "next week" stamps the next one. Every
  surface agrees Sunday is the anchor, so week-level plans sort and
  group cleanly without pretending you know the exact day.
- **👤 owner** — work someone else owes *you*. "Nadia will send the
  contract" becomes a task with `👤 Nadia`, and the waiting-on view is
  just a filter over those.

The **plan-today** processor is the one place the system volunteers an
opinion: from your undated backlog it may suggest up to 3 tasks for
today, each with a one-line reason — and it is prompted to suggest
*nothing* when nothing stands out, and to hold back when your day is
already full. Suggestions, never commitments; you promote them.

---

## 5. The surfaces

Everything runs behind `uv run chickenskratch-ui` at
`http://127.0.0.1:8399`.

**Today** — the attention view. Today's tasks, the week's, the
waiting-on list, and the decision region where pending proposals sit
with side-by-side diffs. `j`/`k` to move, `a`/`r` to approve/reject.
Every button routes through the same gated paths the CLI uses — the UI
has no special powers.

**The canvas / editor** — chickenskratch's own markdown editor. The
capture box commits your dump before extraction (see above). Notes
render live: task lines get their chips, callouts fold, machine-made
records open with the verbatim collapsed. Callouts are *locked* by
default — a stray click won't drop your caret into raw markdown; the
`edit` button on the callout head (or deliberately arrowing in with
the keyboard) is the way in.

**Typed commands** — type `//command args` on its own line in any
note, press Enter, and the line is replaced in place by its result a
few seconds later, rendered as a callout:

- `//azure <query>` → ranked Microsoft Learn links (model-free).
- `//ask <question>` → an answer *strictly from your own notes*, with
  source links. Two guarantees here: the answer worker only ever sees
  **pre-AI text** — your verbatim dumps and hand-typed notes, never an
  earlier model's summary (so model errors can't compound invisibly) —
  and every quote it cites is verified character-for-character against
  the note before it may become a jump link. A paraphrased "quote"
  silently degrades to a plain note link rather than lying to you.

**The chat pane** — a chat session with your configured chat model,
with vault tools. Stop kills and replaces the session (no zombie
half-turns). It's a side channel; the vault is still the truth.

**Changes** — the audit log. Every snapshot with its diff and a revert
button. This is where "wait, what did it just do?" gets answered.

Headless equivalents when no UI is running:

```sh
uv run chickenskratch today       # the attention view, in the terminal
uv run chickenskratch changes     # the audit log
uv run chickenskratch decide p-8f3c21a09b4e approve
```

---

## 6. The console: every knob, one panel

The gear icon opens the console. Four tabs, split by *where the config
lives* and *when a change takes effect*:

| tab | edits | takes effect |
|---|---|---|
| **Vault** | `Harness/Spec.md` (this vault's behavior: folder types, policies, fidelity, worker profile…) | next pass — it's a note edit, snapshotted like any other |
| **Models** | `config/models.yaml` (model profiles, backends) | some fields live, some need a restart — the console tells you which |
| **Behaviors** | `config/workers/*.yaml` (every worker's prompt, hedges, reply schema, bounds) | next pass — the engine reads these files fresh every time |
| **Schemas** | `config/schemas/*.yaml` (what each entity type looks like, and which capture kinds exist) | next pass |

Things worth knowing about how the console behaves:

- **Validation is the real loader.** A save is checked by the exact
  code the engine will use to load the file. If it saves, it runs;
  if it wouldn't run, the save is refused with the reason.
- **Every save is backed up.** Repo-side config isn't in the vault's
  git, so the console keeps its own history (`data/config-history/`).
  Every card has a *Previous versions* list; restore is one click, and
  a restore is itself backed up. Deleting a worker or schema keeps its
  backups reachable — a "deleted" card stays in the list offering
  restore.
- **Two tabs open at once can't drift.** Any save fires a
  `config_changed` event; every open surface refetches.
- **A broken file still renders.** If you hand-edit a YAML file into a
  syntax error, the console shows the file raw with the parse error —
  because the console is where you go to fix it.

---

## 7. Workers: the AI layer as config files

A **worker** is one bounded AI call with one job. There are seven the
engine invokes by name (they can be edited, never deleted):

| worker | job |
|---|---|
| `extract_record` | the capture lane: dump → record + tasks |
| `extract_tasks` | free text → structured tasks (the MCP tool / lighter lane) |
| `answer` | //ask: answer from packed notes with verifiable quotes |
| `schema_fix` | draft the minimal fix for a note failing its schema |
| `plan_today` | pick 0–3 backlog tasks worth doing today |
| `overlap` | judge candidate task pairs: duplicate / related / distinct |
| `grouping` | partition a flat task list into a few themed groups |

Each is one YAML file in `config/workers/`. Anatomy, abridged:

```yaml
description: what this worker is for, in a sentence
inputs: [text, today, this_sunday, next_sunday]   # the placeholders a call fills
timeout_s: 180        # bounds: a slow model is a failed call, not a hang
temperature: 0.1
max_tokens: 4000
prompt: |
  Decompose the dump below into a RECORD and its TASKS...
  ...rules written for a CAPABLE model...
  {hedges}
  Dump:
  {text}
hedges: |
  - When unsure whether it is a meeting, use kind "capture".
  - When unsure about due, scheduled, or priority, use null.
schema:               # the reply's required JSON shape — validated after every call
  type: object
  required: [record, tasks]
  ...
```

The parts that matter:

- **`inputs` is a contract.** Every `{placeholder}` in the prompt must
  be declared, and every declared input must appear. The engine checks
  it fills exactly these before calling. A typo fails at *save* time,
  not at 2am mid-pass.
- **`hedges` is the small-model adapter.** The base prompt is written
  for a capable model. Hedges are extra conservative rules spliced in
  only when the active profile says `worker_adapt: small` — "when
  unsure, say null" style guardrails that a strong model doesn't need
  and a 4B model absolutely does. This is how one prompt serves both a
  frontier API and a local model without forking.
- **The `schema` is never tiered.** Hedging changes how cautious the
  model is, never the shape of the reply — the schema is the API.
- **Edits are live next pass.** No restart, ever, for prompt work. The
  loader re-reads a file the moment its timestamp changes.
- **Generated parts stay generated.** `extract_record`'s list of
  record kinds (and its reply's `kind` enum) is composed from the
  schema files' `record:` blocks at load time — add a capture kind in
  the Schemas tab and the extract prompt already knows about it.
  You'll see the composed prompt under "Show the composed prompt" on
  the worker's card.

**The test-run panel** ("Try it", on every worker card) is the tuning
loop: paste sample input, hit Run, and see the *exact* rendered prompt
that was sent, the model's reply, and — when the reply breaks the
schema — the precise refusal. It runs your **unsaved draft**, so the
loop is: tweak prompt → Run → read → tweak → save when it behaves.

---

## 8. Models: profiles, backends, and going local

`config/models.yaml` (Models tab) holds:

- **Profiles** — named model configurations: a model id, an
  OpenAI-compatible `base_url`, optionally `api_key_env` (the *name*
  of an environment variable holding the key — keys never live in
  config files), and per-profile knobs like `worker_adapt` and
  `worker_max_tokens`.
- **`default:`** — the profile the chat pane talks to.
- **`worker:`** — the profile all seven workers run on. A vault can
  override this via Spec.md's `worker:` key (Vault tab), so switching
  the entire drafting layer to a different model is a note edit.
- **Backends** — local model servers (`chickenskratch-backends` starts
  and stops them): a URL and a launch command per entry.

Anything that speaks the OpenAI API works: a cloud endpoint, Ollama,
an mlx server on the Mac, or a box on your LAN serving a model from
your GPU. Going local is: create a backend, create a profile pointing
at its URL, set `worker:` to that profile. Nothing else changes —
same prompts, same schemas, same gates.

---

## 9. The bakeoff: proving a local model is actually usable

This is why the eval harness exists. "Scores well on someone's
benchmark" and "can run *my* capture lane" are different claims, so
the harness tests the second one directly:

```sh
uv run chickenskratch-worker-eval                    # current worker profile
uv run chickenskratch-worker-eval -m <model> --base-url <url>   # a challenger
uv run chickenskratch-worker-eval -k record          # just the capture cases
```

What a run does: **113 fixture cases across all seven workers**, each
case run **3 times** (small models are stochastic — right two runs out
of three is a different animal from right every time). Scoring is
entirely deterministic — regexes, field equality, set membership —
never another model grading the first one. And wherever a production
gate exists, the eval scores *through it*:

- `extract_record` replies are shaped into a real record by the real
  code and judged by the real `verify_record` — a pass means "this
  record would have survived review," and the strictest cases demand
  it would have **auto-filed**.
- `schema_fix` replies run through the real verifier and a simulated
  apply.
- `answer` quotes run through the real quote-verification gate, and
  citing a note that wasn't in the pack is an instant failure —
  fabricated citations are precisely what you're screening for.

The cases are deliberately mean where local models are weak: inputs up
to ~10KB (a four-speaker meeting transcript, a two-month ops journal
with the answer buried in week 2 of month 2), fabrication traps,
quoted-email tails whose stale asks must *not* be extracted, and
restraint tests (an empty pick is the right answer; a full day means
at most one suggestion).

Reading a report (`evals/reports/workers/*.md`):

```
**41/113 cases passed all 3 runs** — 205/339 runs clean — 38 retries — 24.1 min total
Slowest (mean s/run): record-long-transcript-full-pipeline (94.2s), ...
| case | clean runs | retries | mean s/run | first failure |
```

- **cases passed** — the headline: right *every* time.
- **runs clean** — partial credit visibility: a model at 2/3 on many
  cases is unreliable, not unlucky.
- **retries** — the hidden tax. Every worker call quietly retries once
  on malformed JSON; a model that leans on that retry passes cases
  while being twice as slow and one bad sample from failing. High
  retries = marginal schema discipline. This column is arguably the
  single best "will this local model hold up" signal.
- **mean s/run + budgets** — every case has a time budget (generous —
  they catch *unusable*, not *not-snappy*). A model can now fail on
  speed alone, because a pass that fans out a dozen 90-second calls is
  a failed product no matter how accurate.

**The workflow for the B70:**

1. **Baseline first.** Run the full set once against the current cloud
   profile. If the frontier model fails a judgment pin, that's a case
   to recalibrate, not a signal. This snapshot is what every local
   number is compared against.
2. **Run each candidate** with `-m`/`--base-url`. Reports are stamped
   with model *and* adapt tier, so hedged and loose runs never blur.
3. **Read the deltas**: clean rate, retries, mean seconds — and *which
   workers* break first. A model that aces extract_tasks but falls
   apart on the long-transcript record case tells you exactly what
   your day would feel like.
4. **Tune before rejecting.** A near-miss model often becomes usable
   with a sharper hedge. That loop is the console's test panel
   (paste the failing case's input, iterate on hedges, watch the
   reply), then re-run `-k <case-name>` — an edit is live immediately.
5. When a local model matches the baseline where it matters, switching
   to it is Spec.md's `worker:` key. One note edit. That's the whole
   migration.

(There's also `chickenskratch-eval` — a separate, heavier suite that
scores whole *chat-agent conversations* against a fixture vault. The
worker suite is the one that gates the drafting-model choice.)

---

## 10. When something looks wrong

- **"It didn't do anything" / "my change didn't show up"** — before
  bug-hunting, check whether the UI server predates the change:
  `./restart.sh`. A stale process is the most common ghost.
- **A worker is failing** — the dashboard's health line names the
  worker error; failures render as *errors*, never as empty views (an
  unreachable model must never look like "nothing found").
- **The engine did something you don't like** — Changes feed → revert.
  Every mutation is one commit; reverting is safe and itself
  revertible.
- **You broke a config file by hand** — open the console; the broken
  file renders raw with the parse error, and its backups are one
  click away. One broken schema file pauses kind-composition (you'll
  see the error on the affected worker card) but never takes the
  console down.
- **A proposal is stuck** — proposals go stale when the note under
  them changes (whole-note hash); a stale one is dropped rather than
  applied over your edit. Re-trigger by touching the source.

---

## 11. Cheat sheet

```sh
uv run chickenskratch-ui             # the dashboard (127.0.0.1:8399)
uv run chickenskratch-watch          # one engine pass, by hand
uv run chickenskratch today          # attention view, headless
uv run chickenskratch changes        # audit log, headless
uv run chickenskratch decide <id> approve|reject
uv run chickenskratch-revert         # undo a snapshot
uv run chickenskratch-backends       # start/stop local model servers
uv run chickenskratch-worker-eval    # the model bakeoff (see §9)
uv run chickenskratch-eval           # the chat-agent suite
./restart.sh                         # the "is it just stale?" button
```

In the vault:

| where | what |
|---|---|
| `Harness/Spec.md` | this vault's settings (folders, policies, fidelity, worker profile) |
| `Harness/Capture.md` | the dump surface (the canvas writes here) |
| `Meetings/`, `Captures/` | where records file (configurable per kind) |

In the repo:

| where | what |
|---|---|
| `config/models.yaml` | profiles, backends, chat default, worker profile |
| `config/workers/*.yaml` | one file per worker: prompt, hedges, schema, bounds |
| `config/schemas/*.yaml` | entity shapes + capture kinds |
| `data/config-history/` | every console save's backups |
| `evals/reports/workers/` | bakeoff reports |
