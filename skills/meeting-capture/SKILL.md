---
name: meeting-capture
description: Structure a raw meeting capture note (stream-of-consciousness fragments) into a simple meeting note - day, time, topics covered, action items. Use when the user finishes a capture session, says "/done", or asks to format meeting notes from raw text.
---

# Meeting capture → structured meeting note

You are given the path of a raw capture note (usually under `Captures/`).
It contains unedited fragments typed during a live meeting: half sentences,
typos, shorthand. Turn it into a simple, consistent meeting note.

## Procedure

1. Read the capture note in full before writing anything.
2. Determine the meeting title: from the capture filename or an obvious
   topic in its first lines. If neither works, ask the user — do not invent one.
3. Create the note with create_meeting_note (this fills day, date, and time
   from the template automatically), then fill the two sections with
   replace_section:
   - **Topics covered** — short bullets, one per distinct topic discussed.
   - **Action items** — checkbox tasks: `- [ ] Owner: task text 📅 date`,
     the emoji date only when the capture states or clearly implies one
     ("by Friday" → that date). No invented owners or deadlines.

## Rules

- **Organize, don't embellish.** Every bullet must trace to something in the
  capture. Cleaning up grammar is fine; adding topics, decisions, or tasks
  that aren't there is not.
- Fragments you can't confidently place go at the end of Topics covered as
  `- (unsorted) <fragment>` rather than being dropped or guessed at.
- End the note with `Raw notes: [[<capture path>]]`. Never edit or delete
  the capture note itself.

## After structuring

Reply with the new note's path, the action-item count, and any unsorted
fragments (so the user can clarify while memory is fresh).
