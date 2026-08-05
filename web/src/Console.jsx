import { useCallback, useEffect, useState } from "react";
import { getJSON, postJSON } from "./api.js";

/* The management console (⌘;). Everything the harness treats as
   configuration, in one place, with the split that decides how a change
   behaves stated on the surface rather than left to be discovered:

   - Vault     — Harness/Spec.md. How THIS vault behaves. A gated note
                 write, so every change is a revertible snapshot, and the
                 engine re-reads it next pass. No restart.
   - Models    — config/models.yaml. What the harness talks to. Outside
                 the vault, so writes are validated and backed up instead
                 (restore is right here); some fields need a restart.
   - Behaviors — config/workers/*.yaml. What the AI does: each worker's
                 prompt, small-model hedges, reply schema and bounds.
                 Read fresh every pass — an edit is live next pass.
   - Schemas   — config/schemas/*.yaml. What entities look like, and
                 (via record blocks) which capture kinds exist. Also
                 read fresh every pass.

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

/* Per-processor consent levels. The level IS the gate — there is no
   second setting for a level to conflict with, which retired the whole
   "ticked but inert" class this component's predecessor had to explain.
   `verified` is offered only where a deterministic check exists; the
   preset select above just fills these in. */
const LEVEL_LABEL = {
  off: "off — don't run",
  ask: "ask — queue everything",
  verified: "verified — apply what the check vouches for",
  full: "full — apply everything, objections on the record",
};

const PRESETS = {
  cautious: () => "ask",
  standard: (it) => (it.verified ? "verified" : "ask"),
  autonomous: () => "full",
};

function presetOf(items, levels) {
  for (const [name, fn] of Object.entries(PRESETS)) {
    if (items.every((it) => (levels?.[it.name] ?? "ask") === fn(it))) return name;
  }
  return "custom";
}

function PolicyList({ items, value, onChange }) {
  const levels = value || {};
  return (
    <div className="cfg-checklist">
      {items.map((it) => {
        const opts = ["off", "ask", ...(it.verified ? ["verified"] : []), "full"];
        const cur = levels[it.name] ?? "ask";
        return (
          <div className="cfg-check" key={it.name}>
            <span className="cfg-item-name">{it.title}</span>
            <span className="mono ghost small">{it.name}</span>
            <select value={opts.includes(cur) ? cur : "ask"}
                    onChange={(e) => onChange({ ...levels, [it.name]: e.target.value })}>
              {opts.map((o) => <option key={o} value={o}>{LEVEL_LABEL[o]}</option>)}
            </select>
            <Info text={it.blurb} label={it.title} />
          </div>
        );
      })}
    </div>
  );
}

/* Spec.md. Edits are staged and saved together — saving each keystroke
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

      <Field label="How much it may do without asking" help={help.policies}
             declared={declared.has("policies")}>
        {/* The preset is not stored — it pre-selects the per-processor
            levels below, which are the only truth. Mixed levels read
            back as "custom". */}
        <select value={presetOf(data.trustable, draft.policies)}
                onChange={(e) => {
                  const fn = PRESETS[e.target.value];
                  if (fn) set("policies", Object.fromEntries(
                    data.trustable.map((it) => [it.name, fn(it)])));
                }}>
          <option value="cautious">cautious — everything asks</option>
          <option value="standard">standard — apply what a check can vouch for</option>
          <option value="autonomous">autonomous — apply everything, on the record</option>
          <option value="custom" disabled>custom — tuned below</option>
        </select>
        <PolicyList items={data.trustable} value={draft.policies}
                    onChange={(v) => set("policies", v)} />
      </Field>

      <Field label="Typed //commands" help={help.disabled_processors}
             declared={declared.has("disabled_processors")}>
        <label className="cfg-check">
          <input type="checkbox"
                 checked={!(draft.disabled_processors || []).includes("inline_command")}
                 onChange={(e) => set("disabled_processors",
                   e.target.checked
                     ? (draft.disabled_processors || []).filter((x) => x !== "inline_command")
                     : [...(draft.disabled_processors || []), "inline_command"])} />
          <span className="cfg-item-name">Execute //commands typed into notes</span>
          <Info text={(data.disablable.find((d) => d.name === "inline_command") || {}).blurb || ""}
                label="//commands" />
        </label>
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

      {[["done_archive", "Completed tasks are archived to"],
        ["capture_archive", "Capture records folder"],
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

/* The undo the vault's git snapshots can't provide out here. `path` +
   `payload` name the restore endpoint, since worker/schema files restore
   through their own routes; the response's `restart` says whether the
   engine picks it up on its own. */
function Backups({ file, path = "/api/console/models/restore", payload = {},
                   onSaved, onError }) {
  const restore = async (stamp) => {
    try {
      const r = await postJSON(path, { ...payload, stamp });
      onSaved(`Restored ${file.name} from ${stamp}` +
              (r.restart === false ? " — live next pass"
                                   : " — restart chickenskratch-ui to load it"));
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

function ProfileCard({ p, fields, onField, onDelete }) {
  return (
    <div className="cfg-profile">
      <div className="cfg-profile-head">
        <span className="grow"><strong>{p.name}</strong> <span className="mono ghost small">{p.model}</span></span>
        {p.is_default && <span className="chip verdict-approve">chat default</span>}
        {p.is_worker && <span className="chip">worker</span>}
        {!p.is_default && !p.is_worker && onDelete && (
          <button className="linklike small" onClick={() => onDelete(p.name)}>delete</button>
        )}
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

/* Creating a profile is a form, not a YAML exercise: the server renders
   the block, validates the whole file, and refuses rather than break it.
   api_key_env takes the NAME of an env var — the form says so because
   pasting the key itself here would write it into a file in a repo. */
function NewProfileForm({ onSaved, onError }) {
  const empty = { name: "", model: "", base_url: "", api_key_env: "" };
  const [open, setOpen] = useState(false);
  const [f, setF] = useState(empty);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF((d) => ({ ...d, [k]: e.target.value }));
  const create = async () => {
    setBusy(true);
    try {
      const { name, ...fields } = f;
      await postJSON("/api/console/models/profile", { name, fields });
      onSaved(`Profile ${name} created — restart chickenskratch-ui to chat on it`);
      setF(empty); setOpen(false);
    } catch (e) { onError(String(e.message || e)); }
    setBusy(false);
  };
  if (!open) {
    return <button className="linklike" onClick={() => setOpen(true)}>+ New profile</button>;
  }
  return (
    <div className="cfg-profile">
      <div className="cfg-profile-head"><strong>New profile</strong></div>
      <div className="cfg-grid">
        <label className="cfg-inline"><span className="ghost small">name</span>
          <input value={f.name} onChange={set("name")} placeholder="homelab-70b" /></label>
        <label className="cfg-inline"><span className="ghost small">model id</span>
          <input value={f.model} onChange={set("model")} placeholder="qwen3.5:72b" /></label>
        <label className="cfg-inline"><span className="ghost small">base_url</span>
          <input value={f.base_url} onChange={set("base_url")}
                 placeholder="http://192.168.1.50:8000/v1" /></label>
        <label className="cfg-inline"><span className="ghost small">api_key_env (env var NAME, optional)</span>
          <input value={f.api_key_env} onChange={set("api_key_env")}
                 placeholder="MY_API_KEY" /></label>
      </div>
      <div className="cfg-actions">
        <button className="decide approve" disabled={busy || !f.name || !f.model || !f.base_url}
                onClick={create}>Create</button>
        <button className="linklike" onClick={() => { setF(empty); setOpen(false); }}>Cancel</button>
      </div>
    </div>
  );
}

function ModelsTab({ data, onSaved, onError, refresh }) {
  const [raw, setRaw] = useState(null);       // null = form view
  const [text, setText] = useState(data.text);
  const [busy, setBusy] = useState(false);
  const [backendForm, setBackendForm] = useState(null); // {name,url,command} | null
  useEffect(() => { setText(data.text); }, [data.text]);

  const send = async (path, body, note) => {
    setBusy(true);
    try {
      const r = await postJSON(path, body);
      onSaved(note + (r.restart ? " — restart chickenskratch-ui to load it" : " — live next pass"));
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
                       `${name}.${field} → ${value}`)}
                     onDelete={(name) => send(
                       "/api/console/models/profile/delete", { name },
                       `Profile ${name} deleted`)} />
      ))}
      <NewProfileForm onSaved={(m) => { onSaved(m); }} onError={onError} />

      <div className="cfg-label" style={{ marginTop: "var(--s4)" }}>Local backends</div>
      <p className="ghost small">
        Model servers startable with <span className="mono">chickenskratch-backends</span>.
      </p>
      {data.backends.map((b) => (
        <div className="row" key={b}>
          <span className="grow mono small">{b}</span>
          <button className="linklike small"
                  onClick={() => send("/api/console/models/backend/delete", { name: b },
                                      `Backend ${b} deleted`)}>delete</button>
        </div>
      ))}
      {backendForm ? (
        <div className="cfg-profile">
          <div className="cfg-grid">
            {[["name", "mlx-worker"], ["url", "http://127.0.0.1:8081/v1"],
              ["command", "uv tool run --from mlx-lm mlx_lm.server --port 8081"]].map(([k, ph]) => (
              <label key={k} className="cfg-inline"><span className="ghost small">{k}</span>
                <input value={backendForm[k]} placeholder={ph}
                       onChange={(e) => setBackendForm((d) => ({ ...d, [k]: e.target.value }))} />
              </label>
            ))}
          </div>
          <div className="cfg-actions">
            <button className="decide approve"
                    disabled={busy || !backendForm.name || !backendForm.url || !backendForm.command}
                    onClick={async () => {
                      const { name, ...fields } = backendForm;
                      await send("/api/console/models/backend", { name, fields },
                                 `Backend ${name} created`);
                      setBackendForm(null);
                    }}>Create</button>
            <button className="linklike" onClick={() => setBackendForm(null)}>Cancel</button>
          </div>
        </div>
      ) : (
        <button className="linklike"
                onClick={() => setBackendForm({ name: "", url: "", command: "" })}>
          + New backend
        </button>
      )}

      <div className="cfg-actions">
        <button className="linklike" onClick={() => setRaw((v) => !v)}>
          {raw ? "Hide" : "Edit"} models.yaml directly
        </button>
        <span className="ghost small">
          server allowlists, comments, anything the forms don't cover
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

/* ---- Behaviors: config/workers/*.yaml ---- */

/* Local-time ISO date. toISOString is UTC — in any western timezone an
   evening session would prefill TOMORROW as {today} and hand the worker
   anchors the engine (which uses local dates throughout) disagrees with. */
const localISO = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-` +
  String(d.getDate()).padStart(2, "0");

/* Mirrors tasks.week_end server-side: the Sunday ending d's week. */
const sundayFrom = (d, weeksAhead = 0) => {
  const out = new Date(d);
  out.setDate(d.getDate() + ((7 - d.getDay()) % 7) + weeksAhead * 7);
  return localISO(out);
};

/* Try a worker before committing it: paste sample input, run it on the
   current worker profile, read the reply (or the refusal — a
   schema-invalid reply is exactly what you came here to see). */
function TestRunPanel({ name, inputs, draft, onError }) {
  const today = new Date();
  const prefill = { today: localISO(today),
                    this_sunday: sundayFrom(today), next_sunday: sundayFrom(today, 1) };
  const [vals, setVals] = useState({});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [showPrompt, setShowPrompt] = useState(false);

  const run = async () => {
    setBusy(true); setResult(null);
    try {
      const body = { name, inputs: {} };
      for (const k of inputs) body.inputs[k] = vals[k] ?? prefill[k] ?? "";
      if (draft?.text != null) body.text = draft.text;
      else if (draft?.fields != null) body.fields = draft.fields;
      setResult(await postJSON("/api/console/workers/test", body));
    } catch (e) { onError(String(e.message || e)); }
    setBusy(false);
  };

  return (
    <div className="cfg-test">
      <div className="cfg-label">Try it</div>
      {inputs.map((k) => (
        <label key={k} className="cfg-inline">
          <span className="ghost small mono">{k}</span>
          {k === "text" || k === "notes" || k === "content" ? (
            <textarea rows={4} value={vals[k] ?? ""} placeholder="paste sample input"
                      onChange={(e) => setVals((v) => ({ ...v, [k]: e.target.value }))} />
          ) : (
            <input value={vals[k] ?? prefill[k] ?? ""}
                   onChange={(e) => setVals((v) => ({ ...v, [k]: e.target.value }))} />
          )}
        </label>
      ))}
      <div className="cfg-actions">
        <button className="decide approve" disabled={busy} onClick={run}>
          {busy ? "Running…" : "Run" + (draft ? " (unsaved draft)" : "")}
        </button>
        {result && <span className="ghost small mono">{result.model} · adapt {result.adapt}</span>}
      </div>
      {result && (
        <>
          <button className="linklike small" onClick={() => setShowPrompt((v) => !v)}>
            {showPrompt ? "Hide" : "Show"} the exact prompt sent
          </button>
          {showPrompt && <pre className="cfg-pre">{result.prompt}</pre>}
          {result.error
            ? <div className="cfg-note small attention">{result.error}</div>
            : <pre className="cfg-pre">{JSON.stringify(result.reply, null, 2)}</pre>}
        </>
      )}
    </div>
  );
}

/* A file deleted since its last backup: name plus the restore list.
   Without this card the delete toast's "its backups remain restorable"
   would promise a path the surface doesn't offer — rows normally render
   from live files only. Shared by Behaviors and Schemas. */
function DeletedCard({ item, restorePath, onSaved, onError }) {
  return (
    <div className="cfg-profile">
      <div className="cfg-profile-head">
        <span className="grow"><strong>{item.name}</strong></span>
        <span className="chip">deleted</span>
      </div>
      <Backups file={item.file} path={restorePath}
               payload={{ name: item.name }} onSaved={onSaved} onError={onError} />
    </div>
  );
}

function WorkerCard({ item, onSaved, onError }) {
  const toDraft = (it) => ({
    description: it.description || "",
    inputs: (it.inputs || []).join(", "),
    timeout_s: it.timeout_s, temperature: it.temperature, max_tokens: it.max_tokens,
    prompt: it.prompt || "", hedges: it.hedges || "", schema: it.schema_text || "",
  });
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(() => toDraft(item));
  const [rawMode, setRawMode] = useState(Boolean(item.invalid));
  const [text, setText] = useState(item.text);
  const [busy, setBusy] = useState(false);
  const [showGenerated, setShowGenerated] = useState(false);
  useEffect(() => { setDraft(toDraft(item)); setText(item.text); }, [item]);

  const set = (k) => (e) => setDraft((d) => ({ ...d, [k]: e.target.value }));
  const fields = () => ({ ...draft,
    inputs: draft.inputs.split(",").map((s) => s.trim()).filter(Boolean) });
  const dirty = rawMode ? text !== item.text
    : JSON.stringify(draft) !== JSON.stringify(toDraft(item));

  const save = async () => {
    setBusy(true);
    try {
      const body = rawMode ? { name: item.name, text }
                           : { name: item.name, fields: fields() };
      await postJSON("/api/console/workers", body);
      onSaved(`${item.name} saved — live next pass`);
    } catch (e) { onError(String(e.message || e)); }
    setBusy(false);
  };

  const remove = async () => {
    setBusy(true);
    try {
      await postJSON("/api/console/workers/delete", { name: item.name });
      onSaved(`${item.name} deleted — its backups remain restorable`);
    } catch (e) { onError(String(e.message || e)); }
    setBusy(false);
  };

  return (
    <div className="cfg-profile">
      <div className="cfg-profile-head">
        <button className="linklike grow cfg-cardtoggle" onClick={() => setOpen((v) => !v)}>
          <strong>{item.name}</strong>
          {item.description && <span className="ghost small"> — {item.description}</span>}
        </button>
        {item.reserved && <span className="chip">engine</span>}
        {item.invalid && <span className="chip attention">broken</span>}
        {!item.reserved && (
          <button className="linklike small" disabled={busy} onClick={remove}>delete</button>
        )}
      </div>
      {item.invalid && <div className="cfg-note small attention">{item.invalid}</div>}
      {open && (
        <>
          {!item.invalid && (
            <div className="cfg-actions">
              <button className="linklike small" onClick={() => setRawMode((v) => !v)}>
                {rawMode ? "Form view" : "Edit file directly"}
              </button>
            </div>
          )}
          {rawMode ? (
            <textarea className="cfg-raw" rows={20} value={text}
                      onChange={(e) => setText(e.target.value)} />
          ) : (
            <>
              <Field label="Description">
                <input value={draft.description} onChange={set("description")} />
              </Field>
              <Field label="Inputs"
                     help={item.reserved
                       ? "The engine fills exactly these placeholders when it invokes\nthis worker — they are its calling contract, so they are fixed."
                       : "Comma-separated placeholder names. Each must appear in the\nprompt as {name}, and the test panel offers a field per input."}>
                <input value={draft.inputs} onChange={set("inputs")}
                       disabled={item.reserved} className="mono" />
              </Field>
              <Field label="Prompt"
                     help={"The capable-model reading. {hedges} marks where the\nsmall-model rules splice in; {input} slots fill per call.\n\nDescribe the JSON shape in prose here — the reply schema below\nis what actually enforces it."}>
                <textarea className="cfg-raw" rows={14} value={draft.prompt}
                          onChange={set("prompt")} />
              </Field>
              <Field label="Hedges (small-model rules)"
                     help={"The adapt layer: extra conservative rules spliced into the\nprompt's {hedges} slot only when the worker profile says\nworker_adapt: small. The base prompt stays the capable-model\nreading; the reply schema is never tiered."}>
                <textarea className="cfg-raw" rows={5} value={draft.hedges}
                          onChange={set("hedges")} />
              </Field>
              <div className="cfg-grid">
                {[["timeout_s", "1"], ["temperature", "0.1"], ["max_tokens", "1"]].map(([k, step]) => (
                  <label key={k} className="cfg-inline">
                    <span className="ghost small">{k}</span>
                    <input type="number" step={step} value={draft[k]} onChange={set(k)} />
                  </label>
                ))}
              </div>
              <Field label="Reply schema (YAML)"
                     help={"The jsonschema every reply must satisfy — the API, validated\nafter each call. A reply that drops required fields fails loud\ninstead of being silently cleaned."}>
                <textarea className="cfg-raw" rows={10} value={draft.schema}
                          onChange={set("schema")} />
              </Field>
            </>
          )}
          {item.generated && (
            <>
              <button className="linklike small" onClick={() => setShowGenerated((v) => !v)}>
                {showGenerated ? "Hide" : "Show"} the composed prompt (record kinds:{" "}
                {item.generated.kind_enum.join(", ")})
              </button>
              {showGenerated && <pre className="cfg-pre">{item.generated.prompt}</pre>}
            </>
          )}
          <div className="cfg-actions">
            <button className="decide approve" disabled={!dirty || busy} onClick={save}>
              {busy ? "Saving…" : "Save"}
            </button>
            {dirty && (
              <button className="linklike"
                      onClick={() => { setDraft(toDraft(item)); setText(item.text); }}>
                Discard
              </button>
            )}
          </div>
          {!item.invalid && (
            <TestRunPanel name={item.name} inputs={fields().inputs}
                          draft={dirty ? (rawMode ? { text } : { fields: fields() }) : null}
                          onError={onError} />
          )}
          <Backups file={item.file} path="/api/console/workers/restore"
                   payload={{ name: item.name }} onSaved={onSaved} onError={onError} />
        </>
      )}
    </div>
  );
}

function BehaviorsTab({ data, onSaved, onError }) {
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);

  const create = async () => {
    setBusy(true);
    try {
      const seeded = await postJSON("/api/console/workers/seed", { name: newName.trim() });
      await postJSON("/api/console/workers", { name: seeded.name, text: seeded.text });
      onSaved(`${seeded.name} created — fill in its prompt and schema below`);
      setNewName("");
    } catch (e) { onError(String(e.message || e)); }
    setBusy(false);
  };

  return (
    <div className="cfg-body">
      <p className="ghost small">
        {data.dir}/ — each worker is one file: prompt, small-model hedges,
        reply schema, bounds. Backed up on every save, and the engine
        reads them fresh each pass — no restart. Workers marked{" "}
        <span className="chip">engine</span> are invoked by the pipeline
        and can be edited but not deleted.
      </p>
      {data.items.map((it) => (
        it.deleted
          ? <DeletedCard key={it.name} item={it} restorePath="/api/console/workers/restore"
                         onSaved={onSaved} onError={onError} />
          : <WorkerCard key={it.name} item={it} onSaved={onSaved} onError={onError} />
      ))}
      <div className="cfg-actions">
        <input value={newName} placeholder="new_behavior_name" className="mono"
               onChange={(e) => setNewName(e.target.value)} />
        <button className="decide approve" disabled={busy || !newName.trim()} onClick={create}>
          + New behavior
        </button>
      </div>
    </div>
  );
}

/* ---- Schemas: config/schemas/*.yaml ---- */

const FIELD_KINDS = ["string", "date", "number", "boolean", "array", "enum", "const"];

function FieldRows({ rows, onChange }) {
  const set = (i, k, v) => onChange(rows.map((r, j) => (j === i ? { ...r, [k]: v } : r)));
  return (
    <div className="cfg-checklist">
      {rows.map((r, i) => (
        <div className="cfg-fieldrow" key={i}>
          <input className="mono" value={r.name} placeholder="field"
                 onChange={(e) => set(i, "name", e.target.value)} />
          <select value={r.kind} onChange={(e) => set(i, "kind", e.target.value)}>
            {FIELD_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
          <label className="small"><input type="checkbox" checked={r.required}
                 onChange={(e) => set(i, "required", e.target.checked)} /> required</label>
          {/* array and const have no null spelling in the compiler —
              showing the box would silently drop the choice on save */}
          {!["array", "const"].includes(r.kind) && (
            <label className="small"><input type="checkbox" checked={r.nullable}
                   onChange={(e) => set(i, "nullable", e.target.checked)} /> nullable</label>
          )}
          {r.kind === "string" && (
            <input className="mono grow" value={r.pattern} placeholder="pattern (regex, optional)"
                   onChange={(e) => set(i, "pattern", e.target.value)} />
          )}
          {r.kind === "enum" && (
            <input className="mono grow" value={(r.enum || []).join(", ")}
                   placeholder="values, comma separated"
                   onChange={(e) => set(i, "enum",
                     e.target.value.split(",").map((s) => s.trim()).filter(Boolean))} />
          )}
          {r.kind === "const" && (
            <input className="mono grow" value={r.const ?? ""} placeholder="constant value"
                   onChange={(e) => set(i, "const", e.target.value)} />
          )}
          <button className="linklike small"
                  onClick={() => onChange(rows.filter((_, j) => j !== i))}>✕</button>
        </div>
      ))}
      <button className="linklike small"
              onClick={() => onChange([...rows, { name: "", kind: "string", required: false,
                nullable: false, pattern: "", min_length: 0, enum: [], const: null }])}>
        + field
      </button>
    </div>
  );
}

/* A record block makes this schema a CAPTURE KIND: extract_record's kind
   list and reply enum are generated from these, so the cues here are
   literally part of the prompt. */
function RecordEditor({ record, dirKeys, onChange }) {
  if (!record) {
    return (
      <button className="linklike small"
              onClick={() => onChange({ kind: "", default: false, files_as: "daily-append",
                                        dir_key: "", cues: "", guidance: "" })}>
        + Make this a capture kind (recognized and filed by extract_record)
      </button>
    );
  }
  const set = (k) => (e) => onChange({ ...record,
    [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  return (
    <div className="cfg-inert">
      <div className="cfg-label">Capture kind
        <button className="linklike small" onClick={() => onChange(null)}>remove</button>
      </div>
      <div className="cfg-grid">
        <label className="cfg-inline"><span className="ghost small">kind name</span>
          <input className="mono" value={record.kind || ""} onChange={set("kind")} /></label>
        <label className="cfg-inline"><span className="ghost small">files as</span>
          <select value={record.files_as || "daily-append"} onChange={set("files_as")}>
            <option value="daily-append">daily-append — a block in the dated capture note</option>
            <option value="note">note — its own note per record</option>
          </select></label>
        {record.files_as === "note" && (
          <label className="cfg-inline"><span className="ghost small">files into (vault setting)</span>
            <select value={record.dir_key || ""} onChange={set("dir_key")}>
              <option value="">choose…</option>
              {dirKeys.map((k) => <option key={k} value={k}>{k}</option>)}
            </select></label>
        )}
        <label className="cfg-check"><input type="checkbox" checked={Boolean(record.default)}
               onChange={set("default")} />
          <span className="small">default kind (every unmatched dump files through it)</span></label>
      </div>
      <Field label="Detection cues"
             help={"Prompt bullets, verbatim: when is a dump THIS kind? These are\nspliced into extract_record's generated kind list, so write them\nas '- kind \"x\" when …' bullets."}>
        <textarea className="cfg-raw" rows={3} value={record.cues || ""} onChange={set("cues")} />
      </Field>
      <Field label="Extra guidance (optional)"
             help={"More bullets appended after every kind's cues — exceptions,\nformatting rules (the meeting transcript EXCEPTION lives here)."}>
        <textarea className="cfg-raw" rows={3} value={record.guidance || ""} onChange={set("guidance")} />
      </Field>
    </div>
  );
}

function SchemaCard({ item, dirKeys, onSaved, onError }) {
  const toDraft = (it) => ({
    description: it.description || "", id_prefix: it.id_prefix || "",
    record: it.record ? {
      kind: it.record.kind || it.name, default: Boolean(it.record.default),
      files_as: it.record.files_as || "daily-append", dir_key: it.record.dir_key || "",
      cues: it.record.cues || "", guidance: it.record.guidance || "",
    } : null,
    fields: it.form || [],
  });
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(() => toDraft(item));
  const [rawMode, setRawMode] = useState(Boolean(item.invalid) || !item.formable);
  const [text, setText] = useState(item.text);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setDraft(toDraft(item)); setText(item.text);
    setRawMode(Boolean(item.invalid) || !item.formable);
  }, [item]);

  const dirty = rawMode ? text !== item.text
    : JSON.stringify(draft) !== JSON.stringify(toDraft(item));

  const save = async () => {
    setBusy(true);
    try {
      const body = rawMode ? { name: item.name, text } : { name: item.name, fields: draft };
      await postJSON("/api/console/schemas", body);
      onSaved(`${item.name} saved — live next pass`);
    } catch (e) { onError(String(e.message || e)); }
    setBusy(false);
  };

  const remove = async () => {
    setBusy(true);
    try {
      await postJSON("/api/console/schemas/delete", { name: item.name });
      onSaved(`${item.name} deleted — its backups remain restorable`);
    } catch (e) { onError(String(e.message || e)); }
    setBusy(false);
  };

  return (
    <div className="cfg-profile">
      <div className="cfg-profile-head">
        <button className="linklike grow cfg-cardtoggle" onClick={() => setOpen((v) => !v)}>
          <strong>{item.name}</strong>
          {item.record && <span className="ghost small"> · capture kind
            {item.record.default ? " (default)" : ""}</span>}
          {item.description && <span className="ghost small"> — {item.description}</span>}
        </button>
        {item.engine && <span className="chip">engine</span>}
        {item.invalid && <span className="chip attention">broken</span>}
        {!item.engine && (
          <button className="linklike small" disabled={busy} onClick={remove}>delete</button>
        )}
      </div>
      {item.used_by?.length > 0 && (
        <div className="ghost small">folder map: {item.used_by.join(", ")}</div>
      )}
      {item.invalid && <div className="cfg-note small attention">{item.invalid}</div>}
      {open && (
        <>
          {!item.invalid && item.formable && (
            <div className="cfg-actions">
              <button className="linklike small" onClick={() => setRawMode((v) => !v)}>
                {rawMode ? "Form view" : "Edit file directly"}
              </button>
            </div>
          )}
          {!item.invalid && !item.formable && (
            <p className="ghost small">
              This schema's shape is beyond the form (nested structures) —
              it edits as raw YAML.
            </p>
          )}
          {rawMode ? (
            <textarea className="cfg-raw" rows={16} value={text}
                      onChange={(e) => setText(e.target.value)} />
          ) : (
            <>
              <div className="cfg-grid">
                <label className="cfg-inline"><span className="ghost small">description</span>
                  <input value={draft.description}
                         onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))} /></label>
                <label className="cfg-inline"><span className="ghost small">id prefix (stamped ids: x-…)</span>
                  <input className="mono" value={draft.id_prefix}
                         onChange={(e) => setDraft((d) => ({ ...d, id_prefix: e.target.value }))} /></label>
              </div>
              <div className="cfg-label">Frontmatter fields</div>
              <FieldRows rows={draft.fields}
                         onChange={(rows) => setDraft((d) => ({ ...d, fields: rows }))} />
              <div style={{ marginTop: "var(--s3)" }}>
                <RecordEditor record={draft.record} dirKeys={dirKeys}
                              onChange={(r) => setDraft((d) => ({ ...d, record: r }))} />
              </div>
            </>
          )}
          <div className="cfg-actions">
            <button className="decide approve" disabled={!dirty || busy} onClick={save}>
              {busy ? "Saving…" : "Save"}
            </button>
            {dirty && (
              <button className="linklike"
                      onClick={() => { setDraft(toDraft(item)); setText(item.text); }}>
                Discard
              </button>
            )}
          </div>
          <Backups file={item.file} path="/api/console/schemas/restore"
                   payload={{ name: item.name }} onSaved={onSaved} onError={onError} />
        </>
      )}
    </div>
  );
}

function SchemasTab({ data, onSaved, onError }) {
  const [newName, setNewName] = useState("");
  const [newKind, setNewKind] = useState(false);
  const [busy, setBusy] = useState(false);

  const create = async () => {
    setBusy(true);
    try {
      const name = newName.trim();
      const fields = { description: "", id_prefix: "", fields: [] };
      if (newKind) {
        fields.record = { kind: name, default: false, files_as: "daily-append", dir_key: "",
                          cues: `- kind "${name}" when the dump is … (describe the shape)` };
      }
      await postJSON("/api/console/schemas", { name, fields });
      onSaved(`${name} created — add its fields below`);
      setNewName(""); setNewKind(false);
    } catch (e) { onError(String(e.message || e)); }
    setBusy(false);
  };

  return (
    <div className="cfg-body">
      <p className="ghost small">
        {data.dir}/ — one file per entity type: its frontmatter schema,
        id prefix, and (optionally) a record block that makes it a
        capture kind extract_record can recognize and file. Backed up on
        every save, read fresh each pass. <span className="chip">engine</span>{" "}
        schemas validate the machinery's own files and can't be deleted.
      </p>
      {data.items.map((it) => (
        it.deleted
          ? <DeletedCard key={it.name} item={it} restorePath="/api/console/schemas/restore"
                         onSaved={onSaved} onError={onError} />
          : <SchemaCard key={it.name} item={it} dirKeys={data.dir_keys}
                        onSaved={onSaved} onError={onError} />
      ))}
      <div className="cfg-actions">
        <input value={newName} placeholder="new_type_name" className="mono"
               onChange={(e) => setNewName(e.target.value)} />
        <label className="small"><input type="checkbox" checked={newKind}
               onChange={(e) => setNewKind(e.target.checked)} /> also a capture kind</label>
        <button className="decide approve" disabled={busy || !newName.trim()} onClick={create}>
          + New schema
        </button>
      </div>
    </div>
  );
}

export default function Console({ open, onClose, configVersion }) {
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

  // configVersion bumps on every config_changed event — a save from this
  // console, another tab, or a restore — so an open console never shows
  // a config the disk has moved past.
  useEffect(() => { if (open) load(); }, [open, load, configVersion]);

  // Esc closes — but not while a field has focus: there esc is the
  // control's own cancel, and closing would unmount the tabs and silently
  // discard a staged Spec.md draft or a half-edited models.yaml.
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      if (/^(input|textarea|select)$/i.test(e.target.tagName)) {
        e.target.blur();   // first esc leaves the field…
        return;
      }
      onClose();           // …second esc closes the console
    };
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
            {[["vault", "Vault"], ["models", "Models"],
              ["workers", "Behaviors"], ["schemas", "Schemas"]].map(([id, label]) => (
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
          : tab === "models"
            ? <ModelsTab data={data.models} onSaved={saved} onError={failed}
                         refresh={load} />
          : tab === "workers"
            ? <BehaviorsTab data={data.workers} onSaved={saved} onError={failed} />
            : <SchemasTab data={data.schemas} onSaved={saved} onError={failed} />}
      </div>
    </div>
  );
}
