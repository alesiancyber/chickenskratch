---
name: weekly-review
description: Run a weekly review - summarize tasks completed this week, list overdue and upcoming tasks, and write a review note. Use when the user asks for a weekly review, week summary, or week wrap-up.
---

# Weekly review

Produce a review of the past week and set up the next one. Use exactly the
two tool calls below — the partitioning happens on their result fields.

## Procedure

1. **Done this week**: list_tasks(status="done"), then keep only tasks whose
   `completed_on` field is within the last 7 days. (There is no
   completed-date filter parameter — filter the results yourself.)
2. **Overdue + coming up** in one call: list_tasks(status="open",
   due_before=<today+8 days>), then partition the results by their `due`
   field: due before today = **Overdue** (note days late); today through
   +7 = **Coming up** (soonest first).
3. **Board**: list_notes(folder="Boards") — for each board found, one line
   of cards-per-column via read_board. No boards: omit the section.
4. Create `Weekly/{today} Review.md` with sections:
   - **Done this week** — grouped by type
   - **Overdue** — each with days late
   - **Coming up** — soonest first
   - **Board** — one line per board
   If today's review note already exists, replace its sections instead of
   creating a duplicate.
5. **Offer** (do not do automatically) to roll overdue tasks forward: for
   each, propose a new due date and apply only what the user approves.

## Rules

- Counts come from the tool's `total` and `counts_by_type` fields and your
  partition of the returned list — never recount from memory.
- The review note is a snapshot — keep it scannable: bullets, no prose
  paragraphs.
