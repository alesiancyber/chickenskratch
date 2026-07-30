/* The vault's wikilink grammar — ONE definition for every JS surface
   that reads it (the editor's live preview and click handler, text.jsx's
   linkifier), so no two of them can drift on how [[Note#frag|alias]]
   splits. The regex matches the whole form; parseWikilink owns the
   semantics of the inner text. */

export const WIKILINK_RE = /\[\[([^\]]+)\]\]/g;

/* [[Note]] / [[Note#^ref]] / [[Note|alias]] / [[Note#frag|alias]]:
   inner text -> { path, frag, label }. The frag (a ^ref or a heading)
   appears literally in the raw note, so it rides as the editor's
   find-and-mark target as-is; the label is what renders. An empty path
   (`[[|x]]`) is the caller's cue to render text, not a link. */
export function parseWikilink(inner) {
  const pipe = inner.indexOf("|");
  const target = pipe >= 0 ? inner.slice(0, pipe) : inner;
  const label = pipe >= 0 ? inner.slice(pipe + 1) : inner;
  const [path, frag = ""] = target.split("#");
  return { path: path.trim(), frag: frag.trim(), label };
}
