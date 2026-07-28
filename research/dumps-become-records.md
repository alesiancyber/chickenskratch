# Dumps become records — the capture lane, phase 2

*2026-07-28. Extends the capture lane past task extraction. Nothing in
PHILOSOPHY.md is superseded; one thing in it finally gets used properly
(principle 7: a prompt issued twice is a processor candidate).*

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
