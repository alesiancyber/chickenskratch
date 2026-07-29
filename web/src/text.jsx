/* Markdown-lite: links become links, nothing else — command results and
   chat replies are plain text with links, and a real markdown engine
   would be scope the surfaces don't need.

   Two link forms, because two producers exist and neither is going to
   change shape for us:

   - `[text](url)` — what the agent writes for vault citations.
   - a bare `https://…` — what digest_docs_search emits for docs hits
     ("- Title — https://learn.microsoft.com/…") and what the agent's
     Sources block appends. Ranked links ARE the product here, so a
     docs result that renders as dead text defeats the whole feature.

   `vault:` links are the exception that navigates rather than leaves:
   they open the note in our own editor, on the cited line.

   [[wikilinks]] are the third form — the vault-native one //ask cites
   sources in (and models emit unprompted). With an onOpenNote they
   navigate like vault: links; without one they render as their label,
   never as dead brackets. */

// markdown link first, so a URL already inside one isn't matched twice;
// wikilink before bare URL so [[…]] never half-matches
const LINK_RE =
  /\[([^\]]+)\]\(([^)\s]+)\)|\[\[([^\]|]+)(?:\|([^\]]+))?\]\]|(https?:\/\/[^\s<>"'`]+)/g;
const VAULT = "vault:";
// Prose puts punctuation right after a URL; it is never part of the URL.
const TRAILING = /[.,;:!?)\]}>'"]+$/;

export function linkify(text, onOpenNote) {
  const out = [];
  let last = 0, m, key = 0;
  LINK_RE.lastIndex = 0;
  while ((m = LINK_RE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    last = m.index + m[0].length;

    if (m[5]) {
      // bare URL: hand back any trailing punctuation we swallowed
      const url = m[5].replace(TRAILING, "");
      last -= m[5].length - url.length;
      out.push(<a key={key++} href={url} target="_blank" rel="noreferrer"
                  className="doc-link">{url}</a>);
      continue;
    }

    if (m[3]) {
      // [[Note]] / [[Note#^ref]] / [[Note|alias]] — vault-native
      const [path, frag] = m[3].split("#");
      const label = m[4] || m[3];
      // the fragment (a ^ref or a heading) appears literally in the raw
      // note, so it rides as the editor's find-and-mark target as-is
      out.push(onOpenNote
        ? <button key={key++} className="linklike doc-link"
                  onClick={() => onOpenNote(path.trim(), frag || "")}>{label}</button>
        : <span key={key++}>{label}</span>);
      continue;
    }

    const [label, href] = [m[1], m[2]];
    if (onOpenNote && href.startsWith(VAULT)) {
      // vault:Meetings/Q3.md#14 -> open that note, reveal line 14
      const [path, line] = decodeURIComponent(href.slice(VAULT.length)).split("#");
      out.push(
        <button key={key++} className="linklike doc-link"
                onClick={() => onOpenNote(path, line || "")}>{label}</button>,
      );
    } else if (/^https?:\/\//i.test(href)) {
      out.push(<a key={key++} href={href} target="_blank" rel="noreferrer"
                  className="doc-link">{label}</a>);
    } else {
      /* Anything else — an app scheme like obsidian:// or vscode://, a
         file: path — renders as text, never as a launcher. This is the
         choke point where model-written links become clickable, so the
         "we don't hand notes to another app" rule is enforced HERE and
         can't be re-introduced by a producer we forgot about. */
      out.push(<span key={key++}>{label}</span>);
    }
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function linkifyBlock(text, onOpenNote) {
  return text.split("\n").map((ln, i) => <div key={i}>{linkify(ln, onOpenNote)}</div>);
}
