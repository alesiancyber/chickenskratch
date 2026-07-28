/* This vault's task-line grammar, as pure text functions.

   Deliberately free of every editor import: these regexes and the
   rewrite below MIRROR tezcat_vault/tasks.py, and the engine re-reads
   whatever we write here with the Python originals. A mirror that
   drifts silently is worse than no mirror, so this file stays plain
   enough for tests/test_taskline_mirror.py to run it under node and
   diff it against set_line_scheduled case for case. */

// ⌛ parses, ⏳ is what we write — the pair tasks.py accepts.
export const SCHEDULED_RE = /[⏳⌛]\s*(\d{4}-\d{2}-\d{2})/;
export const CREATED_AT_RE = /➕\s*\d{4}-\d{2}-\d{2}/;
export const DUE_AT_RE = /📅\s*\d{4}-\d{2}-\d{2}/;
export const BLOCK_REF_RE = /\s+\^([A-Za-z0-9][A-Za-z0-9_-]*)\s*$/;

/* Rewrite a task line's ⏳ plan date; null clears it. Mirrors
   tasks.set_line_scheduled: the block ref stays last, the field order
   (priority, ⏳, 📅, ➕) is preserved, and every other token stays
   byte-identical — so planning can never disturb an entity id or the
   lifecycle metadata. Never touches 📅: a plan is not a deadline. */
export function setLineScheduled(line, when) {
  const ref = BLOCK_REF_RE.exec(line);
  const tail = ref ? line.slice(ref.index) : "";
  let core = ref ? line.slice(0, ref.index) : line;
  if (SCHEDULED_RE.test(core)) {
    core = when
      ? core.replace(SCHEDULED_RE, `⏳ ${when}`)
      : core.replace(new RegExp("\\s*" + SCHEDULED_RE.source), "").trimEnd();
  } else if (when) {
    // slot before the first LATER field so the line keeps format_task's order
    const later = [CREATED_AT_RE, DUE_AT_RE]
      .map((re) => core.search(re)).filter((i) => i >= 0);
    if (later.length) {
      const at = Math.min(...later);
      core = `${core.slice(0, at).trimEnd()} ⏳ ${when} ${core.slice(at)}`;
    } else {
      core = `${core.trimEnd()} ⏳ ${when}`;
    }
  }
  return core + tail;
}
