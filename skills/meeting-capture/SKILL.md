---
name: meeting-capture
description: Structure a raw meeting capture note (stream-of-consciousness fragments) into a simple meeting note - day, time, topics covered, action items. Use when the user finishes a capture session, says "/done", or asks to format meeting notes from raw text.
---

# Meeting capture → structured meeting note

You are given the path of a raw capture note (usually under `Meetings/`).
It contains unedited fragments typed during a live meeting: half sentences,
typos, shorthand. Turn it into a simple, consistent meeting note.

## Procedure

1. Read the capture note in full before writing anything.
2. Determine the meeting title from the capture filename — unless the
   filename is just a time (e.g. "12.20", from an untitled capture), in
   which case infer a short title from the content. If nothing in the
   content suggests one, ask the user — do not invent a title.
3. Create the note with create_meeting_note (this fills day, date, and time
   from the template automatically). **If it already exists** (e.g. a
   retry), skip creation and fill the existing note's sections instead.
4. Fill the two sections with replace_section:
   - **Topics covered** — short bullets, one per distinct topic discussed.
   - **Action items** — checkbox tasks. Owner prefix (`Owner: `) only when
     the capture names one; the emoji due date (`📅 YYYY-MM-DD`) only when
     the capture states a clear date. A task with no stated owner or date
     is written plain: `- [ ] task text`.

## Rules

- **Organize, don't embellish.** Every bullet must trace to something in the
  capture. Cleaning up grammar is fine; adding topics, owners, dates, or
  tasks that aren't there is not.
- Relative deadlines ("by Friday", "next week") resolve against the capture
  date — and if the resolution is ambiguous (e.g. "by Friday" captured on a
  Friday), leave the task undated and flag it in your reply instead of
  guessing.
- Fragments you can't confidently place go at the end of Topics covered as
  `- (unsorted) <fragment>` rather than being dropped or guessed at.
- End the note with `Raw notes: [[<capture path>]]`. Never edit or delete
  the capture note itself.

## After structuring

Reply with the new note's path, the action-item count, any unsorted
fragments, and any deadlines you left undated as ambiguous — so the user
can clarify while memory is fresh.
