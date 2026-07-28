# Dumps become records — the capture lane, phase 2

*2026-07-28. Extends the capture lane past task extraction. Nothing in
PHILOSOPHY.md is superseded; one thing in it finally gets used properly
(principle 7: a prompt issued twice is a processor candidate).*

*Revised same day, after the workflow conversation: the inbox is gone
entirely rather than demoted, the waiting list stops being a note, and
the attention model is specified. See "Revision" at the end — it
resolves the open question this doc originally flagged.*

**Thesis.** A dump is not a source of tasks. It is a **record** that
happens to contain work. The harness currently keeps the work and throws
the record away — which is why everything reads as a task, why notes go
missing, and why an extracted task arrives with no idea where it came
from. Write the record, let the work live inside it, and most of what we
were about to build stops being necessary.

---

## What the user actually does

Stated plainly, unprompted:

> *Research, tasks, meetings. I deliver on tasks, the research informs
> how I handle certain tasks, and meetings are for greater awareness.
> The meetings act as a record and context for certain tasks.*

Three kinds, but not three peers. **Tasks are the work. Meetings and
research are context that earns its keep by attaching to work.** They
attach differently, and the difference turns out to be the whole design:

- A meeting **contains** its action items. The task was born in that
  room, on that date, among those people.
- Research **informs** tasks it has no relationship to at all. Nobody
  wrote "this research is about that task"; they're just about the same
  thing.

Containment and similarity. Two relationships, two mechanisms — and
conflating them is what makes "link the notes together" feel unbounded.

---

## Three findings that change the plan

### 1. Today and Week already read every note

`today._walk()` iterates `iter_knowledge_notes()` and collects **every
open task in the vault**. The inboxes have no privileged status in the
due/planned model — only the waiting list reads one specific note.

So a task does not need to be in `Tasks/Inbox.md` to be seen. It never
did; the inbox predates Today and Week.

### 2. "Select the task and the context pops up" already works

`TaskRow` renders a `NoteRow note={r.note} target={r.text}`, which opens
that note in the editor and marks the line. The exact behaviour asked
for — *"the meeting pops up with the task highlighted so you can quickly
get the context around what the action item is"* — is shipped, and has
been for a while.

It only fails for extracted tasks. Because extraction **moved them**.

### 3. Extraction is what severs the context

`_add_task_step` writes the task into `capture_inbox` / `waiting_inbox`.
`add_task()` takes text, dates, priority — no source, and there is no
backlink machinery anywhere in the repo. The moment a task is extracted
out of its dump, its origin exists only in git history.

**The inbox is a vestige of not having Today.** It was the only place a
task could go when nothing surfaced tasks in place. Keeping it as the
default destination now costs us the exact context we want.

---

## The design

### Stop moving tasks. Write the record; the work lives in it.

One dump produces **one record note**, and the tasks it contains stay
inside that note. Provenance stops being a feature: it is not a link, an
index, or a new field in the task grammar. It is *location*.

```
dump:  "meeting with team about azure. alex says we need a baseline
        push. action items for me: research azure ci/cd, update jiras.
        nadia will send the pipeline costs."

  →  Meetings/2026-07-28 team weekly.md
       ---
       type: meeting     date: 2026-07-28     attendees: [Alex, Nadia]
       ---
       ## Topics covered
       - Azure deployments · baseline push (Alex)
       ## Action items
       - [ ] research azure ci/cd  ➕ 2026-07-28  ^t-…
       - [ ] update jiras          ➕ 2026-07-28  ^t-…
       ## Waiting on
       - [ ] Nadia: pipeline costs ➕ 2026-07-28  ^t-…
```

Nothing else changes. Today surfaces those three tasks tomorrow because
`_walk` already reads them. Clicking one opens the meeting on that line
because `NoteRow` already does that. `archive_done` already
*deliberately* leaves tasks inside knowledge notes alone — "they're
historical record" — which is the same instinct, already written down.

### No `//meeting`. One decomposition, not a classification.

The dump is not routed to a kind. The worker returns what the dump *is
and contains*:

```json
{
  "record": {"kind": "meeting", "title": "team weekly",
             "date": "2026-07-28", "attendees": ["Alex", "Nadia"],
             "sections": {"Topics covered": ["…"]}},
  "tasks":  [{"text": "research azure ci/cd", "owed_by": null}, …]
}
```

`record.kind: null` means the dump is a bare task list with nothing to
file — and *that* is when the inbox is the right answer. The inbox stops
being the default and becomes the residual: **where work goes when it
has no context to live in.** That is a coherent job for it, and it is
the honest answer to "if I dump in a pure task list, where does it
live?"

### Contextualising research: similarity, live, never stored

Meetings attach by containment. Research cannot — it is written before
anyone knows which task it will inform, and stored links would have to
be guessed at write time and would rot.

So research attaches at **read** time. `embeddings.Index.nearest()`
already exists, is content-hash cached, and is what `overlap` uses. Open
a task, and the surface asks *what do I know about this* — a live query,
not a stored edge. Nothing to maintain, nothing to invalidate, and it
gets better as the vault grows rather than more tangled.

This is also the honest boundary of what the LLM is being asked to do.
It classifies and shapes a dump *once*, under review. It is not asked to
maintain a graph.

---

## What this costs

Three things break or need widening — worth naming before starting:

1. **`plan_today` scans inbox tasks only** ("open, top-level, stamped
   inbox tasks"). If tasks stay in meetings, its pool has to widen to
   the same walk Today uses, or day-planning quietly stops seeing most
   of the work.
2. **`overlap` anchors on the inbox note** for its proposals. Same
   widening, same reason.
3. **The waiting list.** "Nadia owes me X" from a meeting: does it live
   in the meeting (context) or in `Tasks/Waiting.md` (the nudge list
   Today ages)? These pull opposite ways — the meeting is where the
   commitment was made, the waiting list is where you'd go to chase it.
   Today's waiting section reads one note today. **Open question,
   flagged rather than assumed.**

---

## Phases

**Phase 0 — stop losing prose.** Prerequisite, and a live bug:
- a prose-only dump drafts nothing, stamps its seen-hash, and reports
  "nothing extractable" while the text sits on the surface;
- an approved mixed dump calls `reset_capture`, discarding the
  narrative around the tasks — it survives only in git.

Nothing built on top of this is safe while dumps are being discarded.

**Phase 1 — the record.** The decomposition worker, the record drafter
(meeting first: its shape is already pinned by `config/schemas/meeting.yaml`
and `create_meeting_note`), tasks written into the record, the inbox
demoted to residual. Widen `plan_today` and `overlap`.

**Phase 2 — contextualise.** Related-notes-by-similarity on a task and
on a record, via the existing index. Research becomes useful exactly
when it is relevant, without anyone maintaining a link.

**Phase 3 — reorganise around it.** Today/Week already work; what
changes is what the inbox is *for*, and whether a task row should show
the record it came from. Deferred deliberately: the answer will be
obvious after a week of phase 1, and guessing now would design for a
workflow that no longer exists.

**Not in this phase.** Projects as a kind ("a pending project" — real,
but kanban boards already hold that shape and the interaction is
different), and any autonomous filing that is not a reviewable proposal.

---

## Why this stays safe while it gets more autonomous

Every dump still becomes a *proposal*. A record note that files itself
in the wrong place is one reject, or one revert of a single snapshot.
The reason we can lean this hard on the model is not that it is right —
it is that **being wrong is cheap and visible**, which is the property
I1–I6 exist to protect. Growing autonomy here means letting the model
shape more, never letting it commit unreviewably.

---

# Revision — the inbox is gone, and Today is the amalgamation

*Same day, after working through the actual workflow. This resolves the
waiting-list question above and specifies what "intelligently routing
work to me" means concretely.*

## The settled part: dashboards read, files hold

> *the files hold the context and the today/this week dynamic dashboards
> should surface things. but it shouldnt be a write everytime no? cant we
> show tasks on today without actually writing them to today?*

This is already true and is the load-bearing fact under everything
below. `today.collect()` walks the vault and returns a dict — it writes
nothing, holds nothing, and the markdown `Today.md` surface was retired
in `9f9a52c`. The dashboard is a **pure function of vault state**,
recomputed per request. The only writes are the verbs you press: done,
→ today, → this week.

So a task is surfaced because of what it *is*, not because something
filed it. Nothing has to be moved to be seen. That is what makes the
rest of this cheap.

## No inbox. Every dump becomes a record.

The earlier draft demoted `Tasks/Inbox.md` to a residual for shapeless
dumps. That is still one destination too many. **Every dump becomes a
record**; when a dump has no shape worth naming — a bare task list — the
record is a dated capture note:

```
Captures/2026-07-28.md     type: capture
```

which is itself context ("I dumped these on Tuesday, right after the
Azure meeting"). "Where does a pure task list live?" answers itself:
in the day you dumped it. There is no inbox, so there is no pile.

## The waiting list stops being a note

The open question was whether "Nadia owes me the pipeline costs" lives
in the meeting (context) or in `Tasks/Waiting.md` (the nudge list).
Answer: **in the meeting — and the waiting list becomes a view**, the
same move as the inbox.

> *the context lives in whereever what im owed was derived from —
> meeting, info dump, research. but that should be reflected elsewhere
> so i dont have to scrub through meetings*

That is exactly a view over a walk. `_waiting_rows()` reads one note
today; it becomes a filter over the same `_walk()` every other section
already uses.

**This costs one grammar addition.** Owed-ness is currently encoded by
*location* — a task is owed because it sits in `Waiting.md`. Once it
lives in the meeting, the task line itself has to say so. The task
grammar is emoji fields (`📅 due`, `⏳ scheduled`, `➕ created`, `✅ done`,
priority); owner becomes one more in the same shape:

```
- [ ] pipeline costs 👤 Nadia ➕ 2026-07-28 ^t-…
```

One regex, one `format_task` parameter, one `Task` field. It degrades
harmlessly in Obsidian (renders as text). This is the *only* new grammar
in the whole design, and it exists because we deleted a folder's worth
of implicit meaning.

## Snooze is ⏳, not a new mechanism

> *i should be able to hide it, have it populate as a reminder... or i
> could set them to disappear for a preset amount of time, day, week,
> month*

`⏳ scheduled` already means "deal with this then". For something owed,
"chase this then" is the same field. **Snoozing an owed item is setting
its ⏳** — the defer verb that already exists, with day/week/month
presets on the row. The waiting view shows items whose ⏳ has arrived or
that never had one.

Recurring reminders ("reappear weekly") are deliberately NOT built:
each time it resurfaces you snooze it again, which keeps the decision
with you and needs no repeat grammar, no schedule state, and no way for
the vault to disagree with a scheduler. If snoozing the same item four
times in a row gets tedious, *that tedium is the signal* — and the
attention model below is what should act on it.

## The attention model: reasons, not scores

> *you cant know what im in the mood for today. however, since true due
> dates are rare... age + context + other markers*

Right — so it must never pretend to. The design rule: **the dashboard
never ranks by a hidden score; every surfaced row states the reason it
is there.** That is already the house style — "4d overdue", "12d
waiting", "11d old". A score would be unarguable; a reason can be
disagreed with, and disagreeing is one click (→ today, → this week,
done).

The signals actually available:

| signal | where it lives | already there? |
|---|---|---|
| age since capture | `➕ created` | yes |
| explicit plan | `⏳ scheduled` | yes |
| hard commitment | `📅 due` (rare) | yes |
| stated importance | priority marks | yes, rarely set |
| what it came from | the record it lives in | after phase 1 |
| how often you've pushed it | nothing tracks this | **no** |

**Age is the default clock.** A task captured three weeks ago and never
dispositioned is either neglected-and-important or dead, and both want
the same thing: a decision. That is precisely what
`_stranded_meeting_actions` already does — undated, open, older than 7
days, "rotting quietly" — but it only looks in `meetings_dir`.

**Generalise it.** Stranded stops being a meetings feature and becomes
the attention engine: any open task with no 📅 and no ⏳, older than N
days, grouped by the record it came from. Phase 1 makes this powerful
for free, because after it every task lives in a record worth naming —
"three action items from the Azure meeting, 12 days untouched" is a far
better prompt than "3 undated tasks".

Deferral count is the one genuinely new signal and the most honest one
(you keep pushing this — is it real?). It needs somewhere to live;
`corpus.record_confirm` already logs promote/defer verbs, so the data is
being written and just isn't read. **Phase 2, not phase 1** — it is
worth building only once the rest is in place and you can say whether
the tedium is real.

## What this changes in the plan above

- **Phase 1** additionally: `👤 owner` in the task grammar; `_waiting_rows`
  becomes a filter over `_walk`; no inbox destination at all; a `capture`
  record kind for shapeless dumps.
- **Phase 1 also retires** `capture_inbox` / `waiting_inbox` as
  destinations. They stay as Spec.md keys for one release so an existing
  vault keeps rendering, then go.
- **Phase 3** is now specific: generalise stranded into the attention
  engine, snooze presets on owed rows, and (only then) deferral count.

## The risk worth stating

With nothing ever moved out of records, records accumulate open tasks
forever. There is no inbox to go bankrupt on — which is the point — but
it means the attention engine is not a nicety layered on top: it is the
*only* thing standing between you and a vault full of quietly rotting
checkboxes. Phase 3 is not optional, and phase 1 should not ship without
at least the generalised age surfacing.

---

# Revision 2 — the query layer, and why it comes first

*2026-07-28. Natural-language search over the vault. This reorders the
phases above: the query layer subsumes the attention engine, so it
should be built before it, not after.*

## Compile the query; don't retrieve with the model

The two motivating asks are different animals:

- *"show me my oldest tasks"* — a structured sort on `➕ created`.
  Embeddings have no notion of "oldest" and answer it badly.
- *"show me all tasks about detections"* — semantic.

One mechanism serves both if the model's job is **translating the
sentence into a filter**, not finding the answer. The task grammar is
about eight fields, so this is a small, reliable generation job rather
than a retrieval problem:

```json
{"sort": "created_asc", "limit": 10, "where": {"due": null}, "about": null}
{"about": "detections", "where": {"done": false}}
```

Deterministic code executes that against the walk every other section
already uses; only `about` touches embeddings. Three properties fall
out, and each maps to a rule this project already holds:

- **"oldest" is exact**, not approximate.
- **The filter is showable** — "10 oldest open tasks, no deadline". Same
  reasons-not-scores rule as the attention model: a ranking you can read
  is a ranking you can disagree with.
- **It degrades.** No embedding backend, and `about` falls back to
  `search_vault()`, which already exists.

## What the substrate already gives us

Measured, not assumed (2026-07-28): the embedding cache holds **34
vectors, 768-dim, keyed by task block-id** — Ollama is up on 11434 and
`nomic-embed-text` is answering. So tasks are already embedded; notes
are not.

- `today._walk()` — every open task, fully structured. The data layer is
  done.
- `Index` — content-hash cached, JSONL, working.
- `embed()` takes arbitrary text, so embedding a query is one call.
- `run_worker` — schema validation, retry, timeouts, already built.
- The dashboard is a pure function, so **a new view model is the
  cheapest change in this codebase.**

## What is missing

**Small.** `Index.nearest()` requires a key already *in* the index; a
query string isn't one, so it needs a vector-in variant (~10 lines).
Embedding coverage is only what `overlap` happened to consider, so the
full task pool needs one batch — cheap, hash-cached. Then the query
worker, the filter executor, and a results region.

**Genuinely hard, and out of scope.** Semantic search over notes and
records rather than tasks: that needs chunking, which `embeddings.py`
explicitly defers as "a later, separately-evaluated layer". Threshold
tuning is the other fuzzy part. Neither blocks the two motivating asks.

## Why this reorders the phases

**Search and the attention engine are the same machinery.** "Show me my
oldest tasks" *is* the attention model, invoked by hand. Build the query
layer and the attention engine becomes a **default saved query** rather
than separate code — which retires most of what phase 3 was going to be.

So: query layer before attention engine. It is the more general thing,
and building the specific case first would mean writing surfacing rules
twice.

## Read-only is the payoff, not the constraint

> *if today is read only.... we should be able to dynamically return
> info to it?*

Yes — and it is better than "possible". Because Today writes nothing,
a search result is *the same type of row* as every other row, so it
carries the same verbs. Find the four detection tasks, push three to
next week and promote one, without leaving the results. Search stops
being a list you read and becomes somewhere you work.

## The caveat to design for now

The index only refreshes when `overlap` runs. Search over a stale pool
would return confidently wrong results with no indication they are
stale — precisely the class of lie this project keeps removing (see the
memo, the dead worker, "nothing extractable"). Either freshen the pool
on query, or on every pass, and **say so when results are known
partial.** Never present a stale answer as a complete one.
