import { useCallback, useEffect, useMemo, useState } from "react";

/* The manual-navigation surface.

   The dashboard is what you look at and ⌘K is how you jump; this is for
   the times you already know where a note lives and want to walk to it.
   So it's collapsed by default and its state persists: a rail you open
   on purpose, never a column that silently costs you a third of the
   screen (I5 — a dashboard fits one screen or it isn't one).

   It adds no state to the system. The shape it draws is the vault's own
   folder shape, served flat by /api/tree with exclusions already applied
   at the vault layer (I4), and the only things it remembers — which
   folders are open, how wide the filter is — are view state in
   localStorage, never anything the vault would have to hold. */

const OPEN_KEY = "tezcat.explorer.open";

function loadOpen() {
  try {
    const v = JSON.parse(localStorage.getItem(OPEN_KEY) || "[]");
    return new Set(Array.isArray(v) ? v : []);
  } catch {
    return new Set();
  }
}

/* Flat rels in, nested folders out. Maps (not objects) so a folder
   called "constructor" or "__proto__" is a folder, not a bug. */
function buildTree(notes) {
  const root = { dirs: new Map(), files: [] };
  for (const rel of notes) {
    const parts = rel.split("/");
    let node = root;
    for (const part of parts.slice(0, -1)) {
      if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [] });
      node = node.dirs.get(part);
    }
    node.files.push({ name: parts[parts.length - 1].replace(/\.md$/, ""), rel });
  }
  return root;
}

/* Every folder above a note — what has to be open for it to be visible. */
function ancestors(rel) {
  const parts = rel.split("/").slice(0, -1);
  return parts.map((_, i) => parts.slice(0, i + 1).join("/"));
}

/* Files sit one caret-width in from their folder, so a note name and a
   folder name at the same depth line up on their text, not their box. */
const indent = (depth, isFile) => ({ paddingLeft: 8 + depth * 14 + (isFile ? 16 : 0) });

function Nodes({ node, path, depth, isOpen, toggle, active, onOpen, isGhost }) {
  const dirs = useMemo(
    () => [...node.dirs.entries()].sort((a, b) => a[0].localeCompare(b[0])),
    [node]);
  return (
    <>
      {dirs.map(([name, child]) => {
        const p = path ? `${path}/${name}` : name;
        const open = isOpen(p);
        return (
          <div key={p}>
            <button className="tree-row tree-dir" style={indent(depth)}
                    aria-expanded={open} onClick={() => toggle(p)}>
              <span className={"tree-caret" + (open ? " open" : "")}>▸</span>
              <span className="grow">{name}</span>
            </button>
            {open && (
              <Nodes node={child} path={p} depth={depth + 1} isOpen={isOpen}
                     toggle={toggle} active={active} onOpen={onOpen} isGhost={isGhost} />
            )}
          </div>
        );
      })}
      {node.files.map((f) => (
        <button key={f.rel} title={f.rel} style={indent(depth, true)}
                className={"tree-row tree-file"
                  + (f.rel === active ? " current" : "")
                  + (isGhost(f.rel) ? " machine" : "")}
                onClick={() => onOpen(f.rel)}>
          {f.name}
        </button>
      ))}
    </>
  );
}

export default function Explorer({ tree, open, onToggle, active, onOpen }) {
  const [openDirs, setOpenDirs] = useState(loadOpen);
  const [query, setQuery] = useState("");

  useEffect(() => {
    localStorage.setItem(OPEN_KEY, JSON.stringify([...openDirs]));
  }, [openDirs]);

  /* Opening a note from anywhere — a decision row, the palette, the
     chat pane — reveals it here, so the rail is never showing a
     different place than the editor is. */
  useEffect(() => {
    if (!active) return;
    const need = ancestors(active);
    if (need.length === 0) return;
    setOpenDirs((prev) => {
      if (need.every((d) => prev.has(d))) return prev;   // no needless rerender
      return new Set([...prev, ...need]);
    });
  }, [active]);

  const toggle = useCallback((p) => setOpenDirs((prev) => {
    const next = new Set(prev);
    next.has(p) ? next.delete(p) : next.add(p);
    return next;
  }), []);

  const notes = tree?.notes ?? [];
  const q = query.trim().toLowerCase();
  const matches = useMemo(
    () => (q ? notes.filter((rel) => rel.toLowerCase().includes(q)) : notes),
    [notes, q]);
  const model = useMemo(() => buildTree(matches), [matches]);
  const surfaces = useMemo(() => new Set(tree?.surface_dirs ?? []), [tree]);

  // While filtering, folding is noise: show every branch that survived.
  const isOpen = useCallback((p) => (q ? true : openDirs.has(p)), [q, openDirs]);
  const isGhost = useCallback((rel) => surfaces.has(rel.split("/")[0]), [surfaces]);

  if (!open) {
    return (
      <div className="explorer collapsed">
        <button className="rail-toggle" onClick={onToggle}
                title="Show files ⌘B" aria-label="Show files">▸</button>
      </div>
    );
  }

  return (
    <div className="explorer">
      <div className="tree-head">
        <input className="tree-filter" value={query} placeholder="Filter notes…"
               aria-label="Filter notes"
               onChange={(e) => setQuery(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Escape") setQuery(""); }} />
        <button className="rail-toggle" onClick={onToggle}
                title="Hide files ⌘B" aria-label="Hide files">◂</button>
      </div>
      <nav className="tree small">
        {!tree ? (
          <div className="ghost small tree-note">loading…</div>
        ) : matches.length === 0 ? (
          <div className="ghost small tree-note">
            {notes.length === 0 ? "No notes yet." : "Nothing matches."}
          </div>
        ) : (
          <Nodes node={model} path="" depth={0} isOpen={isOpen} toggle={toggle}
                 active={active} onOpen={onOpen} isGhost={isGhost} />
        )}
        {tree?.truncated && (
          <div className="ghost small tree-note">
            first {notes.length} notes — filter to reach the rest
          </div>
        )}
      </nav>
    </div>
  );
}
