/* Markdown-lite: links become links and ```mermaid fences become
   diagrams, nothing else — command results and chat replies are plain
   text with links, and a real markdown engine would be scope the
   surfaces don't need.

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

import { useEffect, useRef } from "react";

import { findFences, mountDiagram } from "./mermaid.js";
import { WIKILINK_RE, parseWikilink } from "./wiki.js";

// markdown link first, so a URL already inside one isn't matched twice;
// wikilink before bare URL so [[…]] never half-matches. The wikilink
// branch is composed from the shared grammar (wiki.js), so this file
// can't drift from the editor's idea of what a wikilink is.
// Groups: 1,2 = markdown label/url · 3 = wikilink inner · 4 = bare URL.
const LINK_RE = new RegExp(
  /\[([^\]]+)\]\(([^)\s]+)\)/.source
    + "|" + WIKILINK_RE.source
    + "|" + /(https?:\/\/[^\s<>"'`]+)/.source,
  "g");
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

    if (m[4]) {
      // bare URL: hand back any trailing punctuation we swallowed
      const url = m[4].replace(TRAILING, "");
      last -= m[4].length - url.length;
      out.push(<a key={key++} href={url} target="_blank" rel="noreferrer"
                  className="doc-link">{url}</a>);
      continue;
    }

    if (m[3]) {
      // vault-native — split by the shared grammar, same as the editor
      const { path, frag, label } = parseWikilink(m[3]);
      out.push(onOpenNote && path
        ? <button key={key++} className="linklike doc-link"
                  onClick={() => onOpenNote(path, frag)}>{label}</button>
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

/* One mounted diagram on a read-only surface (a //command result on the
   canvas, a chat reply). The editor renders fences through CodeMirror
   decorations; here there is no editor, so the node mounts directly —
   but both go through mountDiagram, so a diagram looks the same and
   fails the same wherever it is read. */
function Diagram({ code }) {
  const ref = useRef(null);
  useEffect(() => (ref.current ? mountDiagram(ref.current, code) : undefined), [code]);
  return <div className="diagram" ref={ref} />;
}

/* Lines, with ```mermaid fences lifted out and drawn. Everything else
   falls through to linkify unchanged — this is a renderer for the one
   block that has a picture in it, not a markdown engine. */
export function linkifyBlock(text, onOpenNote) {
  const lines = text.split("\n");
  const fences = findFences(lines);
  if (fences.length === 0)
    return lines.map((ln, i) => <div key={i}>{linkify(ln, onOpenNote)}</div>);
  const out = [];
  let at = 0;
  for (const { start, end } of fences) {
    for (let i = at; i < start; i++)
      out.push(<div key={i}>{linkify(lines[i], onOpenNote)}</div>);
    const code = lines.slice(start + 1, end).join("\n");
    // an empty fence is not a diagram — show it as the text it is
    if (code.trim()) out.push(<Diagram key={`d${start}`} code={code} />);
    else for (let i = start; i <= end; i++)
      out.push(<div key={i}>{linkify(lines[i], onOpenNote)}</div>);
    at = end + 1;
  }
  for (let i = at; i < lines.length; i++)
    out.push(<div key={i}>{linkify(lines[i], onOpenNote)}</div>);
  return out;
}
