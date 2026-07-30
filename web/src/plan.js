/* The ONE placement rule behind every "→ today / → this week / → later"
   chip. The dashboard's rows and the editor's inline chips both call
   this, so two surfaces cannot disagree about where a task currently
   is or where a click can take it.

   A task is in exactly one of three places — today, this week, or
   later — and its chips are the OTHER two. A chip only exists when
   clicking it would actually move the row: the chip verb is ⏳ only,
   and a hard 📅 at or inside the horizon pins the placement no matter
   what the plan says, so offering the move would be a lie.

   "later" = not today and not this week. For a task that means
   clearing ⏳ (when: "") — it drops to the long-term queue (or its own
   far 📅), the pool promotion draws from. For an OWED item the same
   click stamps next Sunday instead: an unsnoozed debt surfaces
   immediately, so clearing would mean "now", the opposite of later.

   Dates are ISO strings compared as strings; the anchors come from the
   server's view-model (today.date / this_sunday / next_sunday), never
   from a local Date() — two surfaces deriving their own Sunday is how
   they end up disagreeing about which Sunday it is. */
export function planTargets({ due = "", scheduled = "", owed = false },
                            { today: T, week: S, next: N }) {
  const d = due, s = scheduled;
  // Owed reads ⏳ as the snooze: none (or arrived) = on today's plate.
  const state = owed
    ? (s && s > T ? (s <= S ? "week" : "later") : "today")
    : ((s && s <= T) || (d && d <= T) ? "today"
      : (s && s <= S) || (d && d <= S) ? "week" : "later");
  const targets = [];
  if (state !== "today" && !(d && d <= T))
    targets.push({ when: T, text: "→ today", toast: "Promoted to today" });
  if (state !== "week") {
    if (S !== T && !(d && d <= T))
      targets.push({ when: S, text: "→ this week", toast: "Planned for this week" });
    // On a Sunday the week anchor IS today — deferring out of today
    // has to reach for next week's anchor and say so.
    else if (S === T && state === "today")
      targets.push({ when: N, text: "→ next week", toast: "Planned for next week" });
  }
  if (state !== "later" && (owed || !d || d > S))
    targets.push({ when: owed ? N : "", text: "→ later",
                   toast: owed ? "Snoozed past this week" : "Moved to later" });
  return targets;
}
