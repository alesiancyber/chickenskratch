---
name: weekly-review
description: Run a weekly review - summarize tasks completed this week, list overdue and upcoming tasks, and write a review note. Use when the user asks for a weekly review, week summary, or week wrap-up.
---

# Weekly review

Produce a review of the past week and set up the next one.

## Procedure

1. Gather, using list_tasks:
   - tasks completed this week (status "done", completed_on within the last 7 days)
   - open tasks that are overdue (due_before today+1)
   - open tasks due in the next 7 days
2. Read the kanban board (read_board on the main project board) for column
   counts — mention movement only if the user has asked before.
3. Create `Weekly/{today} Review.md` with sections:
   - **Done this week** — completed tasks, grouped by type
   - **Overdue** — each with how many days late
   - **Coming up** — due in the next 7 days, soonest first
   - **Board** — cards per column, one line
4. Offer (do not do automatically) to roll overdue tasks forward: for each,
   propose a new due date and apply only what the user approves.

## Rules

- Counts come from the tool's `total` and `counts_by_type` fields — never
  recount by eye.
- The review note is a snapshot: create it fresh; if one already exists for
  today, replace its sections rather than duplicating.
- Keep the whole note scannable — bullets, no prose paragraphs.
