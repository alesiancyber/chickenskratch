import { useCallback, useEffect, useState } from "react";
import { getJSON, postJSON } from "./api.js";

/* The management console (⌘,). Everything the harness treats as
   configuration, in one place, with the split that decides how a change
   behaves stated on the surface rather than left to be discovered:

   - Vault  — Harness/Spec.md. How THIS vault behaves. A gated note write,
              so every change is a revertible snapshot, and the engine
              re-reads it next pass. No restart.
   - Models — config/models.yaml. What the harness IS. Outside the vault,
              so writes are validated and backed up instead (restore is
              right here), and some fields only take effect on restart.

   Secrets never appear. A profile names the env var holding its key; the
   console reports the NAME and whether it is set. */

/* An (i) that opens the explanation in place. Collapsed by default: the
   descriptions are long because these settings genuinely need explaining,
   and a form where every control shouts a paragraph is one nobody reads.
   Blank lines in the text become paragraphs. */
function Info({ text, label }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  return (
    <>
      <button type="button" className={"cfg-info" + (open ? " on" : "")}
              aria-expanded={open}
              aria-label={`What ${label} does`}
              onClick={(e) => { e.preventDefault(); setOpen((v) => !v); }}>
        i
      </button>
      {open && (
        <div className="cfg-help small">
          {String(text).split("\n\n").map((p, i) => <p key={i}>{p}</p>)}
        </div>
      )}
    </>
  );
}

function Field({ label, help, declared, children }) {
  return (
    <div className="cfg-field">
      <div className="cfg-label">
        {label}
        {/* Inherited vs. yours: a value shown without saying which invites
            you to "change" something that was never written down. */}
        {declared === false && <span className="chip ghost">default</span>}
        <Info text={help} label={label} />
      </div>
      {children}
    </div>
  );
}

/* One checkbox per processor, each saying what it is and — behind the
   (i) — what ticking it actually changes. `noVerifier` marks the ones a
   deterministic check cannot vouch for: under gated autonomy, trusting
   those does nothing, and a control that silently means nothing is worse
   than one that isn't offered. */
function CheckList({ items, value, onChange, showVerifier = false, gated = false }) {
  const set = new Set(value);
  const order = items.map((it) => it.name);
  return (
    <div className="cfg-checklist">
      {items.map((it) => {
        const inert = showVerifier && gated && !it.verified && set.has(it.name);
        return (
          <div className="cfg-item" key={it.name}>
            <label className="cfg-check">
              <input type="checkbox" checked={set.has(it.name)}
                     onChange={(e) => {
                       const next = new Set(set);
                       if (e.target.checked) next.add(it.name); else next.delete(it.name);
                       onChange(order.filter((x) => next.has(x)));
                     }} />
              <span className="cfg-item-name">{it.title}</span>
              <span className="mono ghost small">{it.name}</span>
              {showVerifier && !it.verified && (
                <span className="chip" title="No deterministic check can vouch for this one">
                  always asks
                </span>
              )}
              <Info text={it.blurb} label={it.title} />
            </label>
            {inert && (
              <div className="ghost small cfg-inert">
                Ticked, but inert under gated autonomy — nothing can vouch for
                this one, so it still queues. Switch autonomy to full to let it apply.
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* Spec.md. Edits are staged and saved together: these keys interact —
   autonomy decides what auto_apply MEANS — and saving each keystroke
   would write a git snapshot per character. */
function VaultTab({ data, onSaved, onError }) {
  const [draft, setDraft] = useState(data.values);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setDraft(data.values); }, [data.values]);

  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }));
  const dirty = JSON.stringify(draft) !== JSON.stringify(data.values);
  const declared = new Set(data.declared);
  const help = data.help;

  const save = async () => {
    setBusy(true);
    try {
      const changed = Object.fromEntries(
        Object.entries(draft).filter(
          ([k, v]) => JSON.stringify(v) !== JSON.stringify(data.values[k])));
      const r = await postJSON("/api/console/vault", { fields: changed });
      onSaved(r.changed.length
        ? `Saved ${r.changed.join(", ")} — snapshot kept, live next pass`
        : "Nothing to save");
    } catch (e) { onError(String(e.message || e)); }
    setBusy(false);
  };

  return (
    <div className="cfg-body">
      <p className="ghost small">
        {data.spec_note} in your vault. Every change is one revertible
        snapshot, and the engine re-reads it on its next pass — nothing
        to restart.
      </p>

      <Field label="How much it may do without asking" help={help.autonomy}
             declared={declared.has("autonomy")}>
        <select value={draft.autonomy} onChange={(e) => set("autonomy", e.target.value)}>
          {data.choices.autonomy.map((c) => (
            <option key={c} value={c}>
              {c === "gated" ? "gated — ask unless a check can vouch for it"
                             : "full — apply everything trusted below"}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Trusted to apply without asking" help={help.auto_apply}
             declared={declared.has("auto_apply")}>
        <CheckList items={data.trustable} value={draft.auto_apply}
                   onChange={(v) => set("auto_apply", v)}
                   showVerifier gated={draft.autonomy === "gated"} />
      </Field>

      <Field label="Switched off entirely" help={help.disabled_processors}
             declared={declared.has("disabled_processors")}>
        <CheckList items={data.disablable} value={draft.disabled_processors}
                   onChange={(v) => set("disabled_processors", v)} />
      </Field>

      <Field label="How closely extractions must match your words"
             help={help.fidelity} declared={declared.has("fidelity")}>
        <select value={draft.fidelity} onChange={(e) => set("fidelity", e.target.value)}>
          {data.choices.fidelity.map((c) => (
            <option key={c} value={c}>
              {c === "strict" ? "strict — word for word"
                              : "close — allow grammar and word forms"}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Proposals drafted per pass" help={help.n_max}
             declared={declared.has("n_max")}>
        <input type="number" min="1" max="50" value={draft.n_max}
               onChange={(e) => set("n_max", e.target.value)} />
      </Field>

      <Field label="Model that drafts proposals" help={help.worker}
             declared={declared.has("worker")}>
        <select value={draft.worker} onChange={(e) => set("worker", e.target.value)}>
          <option value="">(whatever the repo config picks)</option>
          {data.profiles.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      </Field>

      {[["capture_inbox", "Extracted tasks go to"],
        ["waiting_inbox", "Things others owe you go to"],
        ["done_archive", "Completed tasks are archived to"],
        ["daily_dir", "Daily notes folder"],
        ["meetings_dir", "Meeting notes folder"]].map(([k, label]) => (
          <Field key={k} label={label} help={help[k]} declared={declared.has(k)}>
            <input value={draft[k] ?? ""} onChange={(e) => set(k, e.target.value)} />
          </Field>
        ))}

      <Field label="Folders no model may ever see" help={help.exclude}
             declared={declared.has("exclude")}>
        <textarea rows={3} value={(draft.exclude || []).join("\n")}
                  onChange={(e) => set("exclude", e.target.value.split("\n")
                    .map((s) => s.trim()).filter(Boolean))} />
      </Field>

      <Field label="Default note type per folder" help={help.folders}
             declared={declared.has("folders")}>
        <textarea rows={5}
                  value={Object.entries(draft.folders || {})
                    .map(([k, v]) => `${k}: ${v}`).join("\n")}
                  onChange={(e) => set("folders", Object.fromEntries(
                    e.target.value.split("\n").map((l) => l.split(":"))
                      .filter((p) => p.length >= 2 && p[0].trim())
                      .map((p) => [p[0].trim(), p.slice(1).join(":").trim()])))} />
      </Field>

      <div className="cfg-actions">
        <button className="decide approve" onClick={save} disabled={!dirty || busy}>
          {busy ? "Saving…" : "Save to Spec.md"}
        </button>
        {dirty && <button className="linklike" onClick={() => setDraft(data.values)}>
          Discard
        </button>}
      </div>
    </div>
  );
}

const NUMERIC = new Set(["worker_max_tokens", "max_iterations", "max_tool_result_chars"]);

function ProfileCard({ p, fields, onField }) {
  return (
    <div className="cfg-profile">
      <div className="cfg-profile-head">
        <span className="grow"><strong>{p.name}</strong> <span className="mono ghost small">{p.model}</span></span>
        {p.is_default && <span className="chip verdict-approve">chat default</span>}
        {p.is_worker && <span className="chip">worker</span>}
      </div>
      <div className="ghost small mono">{p.base_url}</div>
      <div className="small">
        {/* The key: which env var, and is it there. Never the value. */}
        {p.api_key_env
          ? <>key from <span className="mono">{p.api_key_env}</span>{" "}
              {p.key_set
                ? <span className="applied">✓ set</span>
                : <span className="attention">not set — this profile will fail</span>}</>
          : p.literal_key
            ? <span className="attention">key is written into models.yaml — move it to api_key_env</span>
            : <span className="ghost">no key needed</span>}
      </div>
      {p.servers && <div className="ghost small">servers: {p.servers.join(", ")}</div>}
      <div className="cfg-grid">
        {fields.map((f) => (
          <label key={f} className="cfg-inline">
            <span className="ghost small">{f}</span>
            {f === "worker_adapt" ? (
              <select value={p.fields[f]} onChange={(e) => onField(p.name, f, e.target.value)}>
                <option value="none">none</option>
                <option value="small">small</option>
              </select>
            ) : f === "confirm_writes" ? (
              <input type="checkbox" checked={Boolean(p.fields[f])}
                     onChange={(e) => onField(p.name, f, e.target.checked)} />
            ) : (
              <input type="number" value={p.fields[f]}
                     step={f === "temperature" ? "0.1" : "1"}
                     onChange={(e) => onField(p.name, f, e.target.value)} />
            )}
          </label>
        ))}
      </div>
    </div>
  );
}

function ModelsTab({ data, onSaved, onError, refresh }) {
  const [raw, setRaw] = useState(null);       // null = form view
  const [text, setText] = useState(data.text);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setText(data.text); }, [data.text]);

  const send = async (path, body, note) => {
    setBusy(true);
    try {
      const r = await postJSON(path, body);
      onSaved(note + (r.restart ? " — restart tezcat-ui to load it" : " — live next pass"));
    } catch (e) { onError(String(e.message || e)); }
    setBusy(false);
  };

  if (data.error)
    return (
      <div className="cfg-body">
        <div className="attention">models.yaml could not be read: {data.error}</div>
        <p className="ghost small">
          Fix it here, or restore a backup below. Nothing else in the console
          can be trusted until this parses.
        </p>
        <textarea className="cfg-raw" rows={24} value={text}
                  onChange={(e) => setText(e.target.value)} />
        <div className="cfg-actions">
          <button className="decide approve" disabled={busy}
                  onClick={() => send("/api/console/models/text", { text }, "models.yaml saved")}>
            Save
          </button>
        </div>
        <Backups file={data.file} onError={onError} onSaved={onSaved} refresh={refresh} />
      </div>
    );

  return (
    <div className="cfg-body">
      <p className="ghost small">
        config/models.yaml — repo-side, so these are backed up rather than
        snapshotted into the vault. The worker profile is re-read every
        pass; the chat default and anything a running agent already holds
        need a restart.
      </p>

      <Field label="Chat profile"
             help="The model the chat pane and the CLI talk to. Changing it\nneeds a restart — a running agent already holds its profile.">
        <select value={data.default} disabled={busy}
                onChange={(e) => send("/api/console/models",
                                      { patch: { default: e.target.value } },
                                      `Chat profile → ${e.target.value}`)}>
          {data.profiles.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
        </select>
      </Field>

      <Field label="Worker profile"
             help="The model that drafts proposals in the background, for every\nvault. A vault can override it for itself on the Vault tab.\n\nRe-read every pass, so a change here takes effect without a restart.">
        <select value={data.worker} disabled={busy}
                onChange={(e) => send("/api/console/models",
                                      { patch: { worker: e.target.value } },
                                      `Worker profile → ${e.target.value}`)}>
          {data.profiles.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
        </select>
      </Field>

      <div className="cfg-label">Profiles</div>
      {data.profiles.map((p) => (
        <ProfileCard key={p.name} p={p} fields={data.profile_fields}
                     onField={(name, field, value) => send(
                       "/api/console/models",
                       { patch: { profiles: { [name]: { [field]: value } } } },
                       `${name}.${field} → ${value}`)} />
      ))}

      {data.backends.length > 0 && (
        <p className="ghost small">
          Local backends startable with <span className="mono">tezcat-backends</span>:{" "}
          {data.backends.join(", ")}
        </p>
      )}

      <div className="cfg-actions">
        <button className="linklike" onClick={() => setRaw((v) => !v)}>
          {raw ? "Hide" : "Edit"} models.yaml directly
        </button>
        <span className="ghost small">
          adding or removing a profile, backends, server allowlists
        </span>
      </div>
      {raw && (
        <>
          <textarea className="cfg-raw" rows={20} value={text}
                    onChange={(e) => setText(e.target.value)} />
          <div className="cfg-actions">
            <button className="decide approve" disabled={busy || text === data.text}
                    onClick={() => send("/api/console/models/text", { text },
                                        "models.yaml saved")}>
              Save models.yaml
            </button>
            {text !== data.text && (
              <button className="linklike" onClick={() => setText(data.text)}>Discard</button>
            )}
          </div>
        </>
      )}

      <Backups file={data.file} onError={onError} onSaved={onSaved} refresh={refresh} />
    </div>
  );
}

/* The undo the vault's git snapshots can't provide out here. */
function Backups({ file, onSaved, onError }) {
  const restore = async (stamp) => {
    try {
      await postJSON("/api/console/models/restore", { stamp });
      onSaved(`Restored ${file.name} from ${stamp} — restart tezcat-ui to load it`);
    } catch (e) { onError(String(e.message || e)); }
  };
  return (
    <div className="cfg-backups">
      <div className="cfg-label">Previous versions</div>
      {file.backups.length === 0
        ? <div className="ghost small">None yet — a backup is kept each time this file is saved.</div>
        : file.backups.slice(0, 8).map((b) => (
            <div className="row" key={b.stamp}>
              <span className="grow mono small">{b.when}</span>
              <button className="affordance linklike" onClick={() => restore(b.stamp)}>
                restore
              </button>
            </div>
          ))}
    </div>
  );
}

export default function Console({ open, onClose }) {
  const [tab, setTab] = useState("vault");
  const [data, setData] = useState(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setData(await getJSON("/api/console"));
      setError("");
    } catch (e) { setError(String(e.message || e)); }
  }, []);

  useEffect(() => { if (open) load(); }, [open, load]);

  // Esc closes — but not while a select or textarea has it, where esc is
  // the control's own cancel.
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const saved = (msg) => { setNote(msg); setError(""); load(); };
  const failed = (msg) => { setError(msg); setNote(""); };

  return (
    <div className="cfg-overlay" onMouseDown={(e) => {
      if (e.target === e.currentTarget) onClose();
    }}>
      <div className="cfg-dialog" role="dialog" aria-label="Console">
        <div className="cfg-head">
          <strong>Console</strong>
          <div className="cfg-tabs">
            {[["vault", "Vault"], ["models", "Models"]].map(([id, label]) => (
              <button key={id} className={"navlink" + (tab === id ? " current" : "")}
                      onClick={() => setTab(id)}>{label}</button>
            ))}
          </div>
          <span className="spacer" />
          <span className="ghost small">esc</span>
          <button className="pane-close" onClick={onClose} aria-label="Close console">✕</button>
        </div>

        {note && <div className="cfg-note small applied">{note}</div>}
        {error && <div className="cfg-note small attention">{error}</div>}

        {/* A failed load is not a slow one. Showing "loading…" underneath
            the error it already returned reads as a hang. */}
        {!data ? <div className="cfg-body ghost">
                   {error ? "Nothing to show — see above." : "loading…"}
                 </div>
          : tab === "vault"
            ? <VaultTab data={{ ...data.vault,
                                profiles: data.models.profiles.map((p) => p.name) }}
                        onSaved={saved} onError={failed} />
            : <ModelsTab data={data.models} onSaved={saved} onError={failed}
                         refresh={load} />}
      </div>
    </div>
  );
}
