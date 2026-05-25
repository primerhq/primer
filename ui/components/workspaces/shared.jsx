/* global React, Icon */

// ---- WS_FieldRow / WS_Section: form-row helpers prefixed to avoid colliding
// with the semantic-search.jsx file-local copies. Same visual contract as
// the SSP modal so the look-and-feel matches across modals.
function WS_FieldRow({ label, hint, err, children }) {
  return (
    <div className="field">
      <label className="field-label">
        {label}
        {hint && <span className="hint">{hint}</span>}
      </label>
      {children}
      {err && <div className="field-help" style={{ color: "var(--red)" }}>
        <Icon name="x-circle" size={11} style={{ verticalAlign: -1, marginRight: 3 }} />
        {err}
      </div>}
    </div>
  );
}

function WS_Section({ label, sub }) {
  return (
    <div style={{ borderBottom: "1px dashed var(--border)", marginBottom: 12, paddingBottom: 4, marginTop: 6 }}>
      <span className="mono" style={{ fontSize: 10.5, fontWeight: 600, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</span>
      {sub && <span className="muted text-sm" style={{ marginLeft: 8, fontSize: 11 }}>· {sub}</span>}
    </div>
  );
}

// ---- PairListEditor: edits a flat dict via key/value rows ----------------
function PairListEditor({ value, onChange, keyPlaceholder = "key", valuePlaceholder = "value" }) {
  const entries = Object.entries(value || {});
  const setAt = (i, k, v) => {
    const next = entries.slice();
    next[i] = [k, v];
    onChange(Object.fromEntries(next.filter(([kk]) => kk !== "")));
  };
  const remove = (i) => {
    const next = entries.slice();
    next.splice(i, 1);
    onChange(Object.fromEntries(next));
  };
  const add = () => {
    onChange({ ...(value || {}), "": "" });
  };
  return (
    <div className="col" style={{ gap: 6 }}>
      {entries.map(([k, v], i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 6 }}>
          <input className="input mono" placeholder={keyPlaceholder} value={k} onChange={(e) => setAt(i, e.target.value, v)} />
          <input className="input mono" placeholder={valuePlaceholder} value={v ?? ""} onChange={(e) => setAt(i, k, e.target.value)} />
          <button className="icon-btn" style={{ width: 26, height: 26 }} onClick={() => remove(i)} title="Remove"><Icon name="x" size={10} /></button>
        </div>
      ))}
      <button className="btn" style={{ alignSelf: "flex-start", padding: "4px 10px" }} onClick={add}>
        <Icon name="plus" size={11} /> Add row
      </button>
    </div>
  );
}

// ---- StringListEditor: edits a list[str] via row inputs -----------------
function StringListEditor({ value, onChange, placeholder = "" }) {
  const arr = Array.isArray(value) ? value : [];
  const setAt = (i, v) => {
    const next = arr.slice();
    next[i] = v;
    onChange(next);
  };
  const remove = (i) => {
    const next = arr.slice();
    next.splice(i, 1);
    onChange(next);
  };
  const add = () => onChange([...arr, ""]);
  return (
    <div className="col" style={{ gap: 6 }}>
      {arr.map((v, i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 6 }}>
          <input className="input mono" placeholder={placeholder} value={v ?? ""} onChange={(e) => setAt(i, e.target.value)} />
          <button className="icon-btn" style={{ width: 26, height: 26 }} onClick={() => remove(i)} title="Remove"><Icon name="x" size={10} /></button>
        </div>
      ))}
      <button className="btn" style={{ alignSelf: "flex-start", padding: "4px 10px" }} onClick={add}>
        <Icon name="plus" size={11} /> Add row
      </button>
    </div>
  );
}

// ---- JsonTextareaField: free-form JSON with client-side parse check -----
function JsonTextareaField({ value, onChange, placeholder, err, rows = 6 }) {
  const initial = React.useMemo(() => {
    if (value == null) return "";
    try { return JSON.stringify(value, null, 2); } catch { return ""; }
  }, []);
  const [text, setText] = React.useState(initial);
  const [parseErr, setParseErr] = React.useState(null);
  const handle = (s) => {
    setText(s);
    if (s.trim() === "") {
      setParseErr(null);
      onChange(null);
      return;
    }
    try {
      const parsed = JSON.parse(s);
      setParseErr(null);
      onChange(parsed);
    } catch (e) {
      setParseErr(e.message);
    }
  };
  return (
    <div>
      <textarea
        className="input mono"
        style={{ width: "100%", fontSize: 12, minHeight: rows * 18 }}
        rows={rows}
        placeholder={placeholder}
        value={text}
        onChange={(e) => handle(e.target.value)}
      />
      {parseErr && <div className="field-help" style={{ color: "var(--red)" }}>JSON parse error: {parseErr}</div>}
      {err && <div className="field-help" style={{ color: "var(--red)" }}>{err}</div>}
    </div>
  );
}

// ---- EnvPairEditor: key + masked-secret value rows ----------------------
function EnvPairEditor({ value, onChange }) {
  const entries = Object.entries(value || {});
  const [reveal, setReveal] = React.useState({});
  const setAt = (i, k, v) => {
    const next = entries.slice();
    next[i] = [k, v];
    onChange(Object.fromEntries(next.filter(([kk]) => kk !== "")));
  };
  const remove = (i) => {
    const next = entries.slice();
    next.splice(i, 1);
    onChange(Object.fromEntries(next));
  };
  const add = () => onChange({ ...(value || {}), "": "" });
  return (
    <div className="col" style={{ gap: 6 }}>
      {entries.map(([k, v], i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto auto", gap: 6 }}>
          <input className="input mono" placeholder="KEY" value={k} onChange={(e) => setAt(i, e.target.value, v)} />
          <input
            className="input mono"
            placeholder="value"
            type={reveal[i] ? "text" : "password"}
            value={v ?? ""}
            onChange={(e) => setAt(i, k, e.target.value)}
          />
          <button className="icon-btn" style={{ width: 26, height: 26 }} onClick={() => setReveal({ ...reveal, [i]: !reveal[i] })} title={reveal[i] ? "Hide" : "Reveal"}>
            <Icon name={reveal[i] ? "eye-off" : "eye"} size={11} />
          </button>
          <button className="icon-btn" style={{ width: 26, height: 26 }} onClick={() => remove(i)} title="Remove"><Icon name="x" size={10} /></button>
        </div>
      ))}
      <button className="btn" style={{ alignSelf: "flex-start", padding: "4px 10px" }} onClick={add}>
        <Icon name="plus" size={11} /> Add variable
      </button>
    </div>
  );
}

// ---- FileRowEditor: list of {path, content} inline-text files ------------
function FileRowEditor({ value, onChange }) {
  const arr = Array.isArray(value) ? value : [];
  const setAt = (i, patch) => {
    const next = arr.slice();
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };
  const remove = (i) => {
    const next = arr.slice();
    next.splice(i, 1);
    onChange(next);
  };
  const add = () => onChange([...arr, { path: "", source: { kind: "inline", content: "" } }]);
  return (
    <div className="col" style={{ gap: 10 }}>
      {arr.map((f, i) => (
        <div key={i} className="panel" style={{ padding: 10 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 6, marginBottom: 6 }}>
            <input className="input mono" placeholder="path (relative to workspace root)" value={f.path || ""} onChange={(e) => setAt(i, { path: e.target.value })} />
            <button className="icon-btn" style={{ width: 26, height: 26 }} onClick={() => remove(i)} title="Remove"><Icon name="x" size={10} /></button>
          </div>
          <textarea
            className="input mono"
            style={{ width: "100%", fontSize: 12, minHeight: 90 }}
            placeholder="file contents (inline-text only; for git/http sources use the API)"
            value={(f.source && f.source.content) || ""}
            onChange={(e) => setAt(i, { source: { kind: "inline", content: e.target.value } })}
          />
        </div>
      ))}
      <button className="btn" style={{ alignSelf: "flex-start", padding: "4px 10px" }} onClick={add}>
        <Icon name="plus" size={11} /> Add file
      </button>
    </div>
  );
}

// ---- BackendBadge: small visual chip identifying a backend kind ----------
function WorkspaceBackendBadge({ kind }) {
  const colors = {
    local: "var(--green)",
    container: "var(--blue)",
    kubernetes: "var(--violet)",
  };
  const color = colors[kind] || "var(--text-3)";
  return (
    <span className="pill" style={{ background: "var(--bg-2)", color, border: "1px solid var(--border)" }}>
      <span className="dot" style={{ background: color }}></span>
      <span className="mono text-sm">{kind}</span>
    </span>
  );
}

window.WorkspacePairListEditor = PairListEditor;
window.WorkspaceStringListEditor = StringListEditor;
window.WorkspaceJsonTextareaField = JsonTextareaField;
window.WorkspaceEnvPairEditor = EnvPairEditor;
window.WorkspaceFileRowEditor = FileRowEditor;
window.WorkspaceBackendBadge = WorkspaceBackendBadge;
// Note: WS_FieldRow + WS_Section are top-level function declarations in
// this file. Babel-standalone shares top-level scope across <script
// type="text/babel"> tags, so providers.jsx + templates.jsx (loaded
// AFTER this file per the <script> ordering in ui/console/index.html)
// reference them as bare identifiers. No window.* registration needed.
