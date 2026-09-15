/* Diagrams, from the markdown that was already valid markdown.

   A ```mermaid fence is a plain fenced code block: Obsidian renders it,
   GitHub renders it, and a vault full of them is still a folder of text
   files that outlives this harness. So this adds a RENDERER, not a
   syntax — nothing here changes what lands on disk, and a note written
   in our editor opens correctly in anyone else's.

   Two rules the rest of the module exists to keep:

   - Mermaid is ~500KB, three quarters of everything else the dashboard
     ships. It is imported LAZILY, on the first diagram anyone actually
     looks at, so a vault with no diagrams never pays for it and a vault
     with one pays a disk read on localhost. Vite splits it out on the
     strength of the dynamic import below.
   - A diagram that fails to parse shows its error. Mermaid's instinct is
     to throw an SVG with the word "Syntax error" and no location, or —
     worse, when it half-succeeds — to leave an empty box, and an empty
     box on a surface that renders your notes reads as "your note is
     empty" rather than "this diagram is wrong on line 3".
*/

let loading = null;

/* Every mounted diagram, so a theme flip can redraw them. A Set of live
   entries rather than a re-render on scroll: mermaid.initialize is
   global and one-shot per theme, so the switch has to reach diagrams
   that were drawn under the old one. */
const mounted = new Set();

/* The app's own tokens, read off the document, so a diagram is the same
   ink and the same accent as the note around it rather than mermaid's
   purple defaults. Read at init AND at every theme flip — the tokens
   move under prefers-color-scheme. */
function themeVariables() {
  const s = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (s.getPropertyValue(name).trim() || fallback);
  const ink = v("--ink", "#37352f");
  const line = v("--ink-3", "rgba(55,53,47,0.4)");
  return {
    background: v("--bg", "#fbfbfa"),
    primaryColor: v("--hover", "rgba(55,53,47,0.045)"),
    primaryTextColor: ink,
    primaryBorderColor: line,
    secondaryColor: v("--accent-soft", "rgba(199,110,0,0.09)"),
    tertiaryColor: v("--bg", "#fbfbfa"),
    lineColor: line,
    textColor: ink,
    mainBkg: v("--hover", "rgba(55,53,47,0.045)"),
    nodeBorder: line,
    clusterBkg: "transparent",
    clusterBorder: v("--line", "rgba(55,53,47,0.07)"),
    titleColor: ink,
    edgeLabelBackground: v("--bg", "#fbfbfa"),
    fontFamily: v("--font", "sans-serif"),
    fontSize: "14px",
  };
}

function config() {
  return {
    startOnLoad: false,
    // Diagrams come from notes, and a note can arrive from anywhere — an
    // import, a paste, a model. strict keeps click handlers and raw HTML
    // labels out of the renderer.
    securityLevel: "strict",
    theme: "base",
    themeVariables: themeVariables(),
    flowchart: { htmlLabels: false, curve: "basis" },
  };
}

async function load() {
  if (!loading) {
    loading = import("mermaid").then(({ default: mermaid }) => {
      mermaid.initialize(config());
      return mermaid;
    });
  }
  return loading;
}

/* How far a diagram may be scaled down to fit the column before
   scrolling it is the kinder answer. Neither extreme works alone:
   shrink-to-fit (mermaid's default) renders an eight-node flowchart at
   about 5px of text, and never-shrink makes a diagram that missed the
   measure by a hair need 2.7x of horizontal scrolling. So: shrink while
   the result is still readable, scroll once it would not be. */
const MIN_SCALE = 0.6;

/* Mermaid already sizes the svg responsively — `width: 100%` capped at
   its natural width — which is right in every case but one: a column
   narrow enough makes a wide diagram scale down until the labels are
   unreadable (measured: an eight-node flowchart in a 613px note column
   renders its text at about 5px). So the only thing added here is a
   FLOOR. Past it the diagram keeps its size and the container scrolls,
   the rule wide content already follows here — see .diff-wrap.
   Expressed as a CSS bound rather than computed against a measured
   width, because the two columns sit on a draggable splitter: "the
   width" is not a fact you can read once, and layout re-solves a
   min-width on every resize for free. */
function fit(el) {
  const svg = el.querySelector("svg");
  if (!svg) return;
  const natural = svg.viewBox?.baseVal?.width
    || parseFloat(svg.getAttribute("width")) || 0;
  if (natural) svg.style.minWidth = `${Math.round(natural * MIN_SCALE)}px`;
}

let seq = 0;

async function draw(entry) {
  const { el, code } = entry;
  try {
    const mermaid = await load();
    // parse() first: it reports WHERE the syntax broke, which render()
    // flattens into a generic error graphic.
    await mermaid.parse(code);
    const { svg } = await mermaid.render(`cs-mermaid-${++seq}`, code);
    if (!mounted.has(entry)) return;   // unmounted mid-render
    el.innerHTML = svg;
    el.classList.remove("cm-mermaid-error");
    fit(el);
  } catch (e) {
    if (!mounted.has(entry)) return;
    el.classList.add("cm-mermaid-error");
    el.textContent = String(e?.message || e || "diagram failed to render")
      .replace(/^Error:\s*/, "");
  }
}

/* Render `code` into `el`. Returns an unmount function — call it when the
   node goes away, or a theme flip will redraw into a detached element. */
export function mountDiagram(el, code) {
  const entry = { el, code };
  mounted.add(entry);
  el.classList.add("cm-mermaid");
  // Something, immediately: the lazy chunk takes a moment on first use and
  // a silent empty block for that moment is the failure mode above.
  el.textContent = "rendering diagram…";
  draw(entry);
  return () => mounted.delete(entry);
}

/* One listener for the whole app: re-theme and redraw every live diagram
   when the colour scheme flips. */
if (typeof window !== "undefined" && window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (!loading) return;   // nothing drawn yet; init will read the new tokens
    loading = loading.then((mermaid) => {
      mermaid.initialize(config());   // config() re-reads the tokens
      return mermaid;
    });
    for (const entry of mounted) draw(entry);
  });
}

/* The fence grammar, shared by every surface that renders one: the
   editor's live preview and the read-only renderer in text.jsx. Defined
   here so the two cannot disagree about what a diagram block is. */
export const MERMAID_OPEN_RE = /^ {0,3}```\s*mermaid\s*$/;
export const FENCE_CLOSE_RE = /^ {0,3}```\s*$/;

/* Fenced mermaid blocks in `lines` as {start, end} line indexes
   (0-based, inclusive of both fence lines). An unclosed fence is not a
   block — you are still typing it. */
export function findFences(lines) {
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    if (!MERMAID_OPEN_RE.test(lines[i])) continue;
    for (let j = i + 1; j < lines.length; j++) {
      if (FENCE_CLOSE_RE.test(lines[j])) {
        out.push({ start: i, end: j });
        i = j;
        break;
      }
    }
  }
  return out;
}
