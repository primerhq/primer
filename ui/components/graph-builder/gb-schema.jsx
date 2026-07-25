/* global React, GR_JsonField */
// GB_SchemaBuilder - JSON Schema as field rows. WIRING.md §8.
// Anything the row editor can't represent (oneOf, $ref, patternProperties)
// opens in JSON mode with an explanation rather than being silently dropped.

const GB_TYPES = [
  { id: "string", label: "text" },
  { id: "boolean", label: "yes / no" },
  { id: "number", label: "number" },
  { id: "array", label: "list of…" },
  { id: "object", label: "group" },
];

// True when the schema uses constructs the row editor cannot round-trip.
function GB_schemaTooComplex(schema) {
  if (!schema || typeof schema !== "object") return false;
  const banned = ["oneOf", "anyOf", "allOf", "$ref", "patternProperties", "not", "if", "then"];
  const walk = (s, depth) => {
    if (!s || typeof s !== "object") return false;
    for (const k of banned) if (k in s) return true;
    if (depth > 1) return false;
    for (const key of Object.keys(s.properties || {})) {
      if (walk(s.properties[key], depth + 1)) return true;
    }
    return false;
  };
  return walk(schema, 0);
}

function GB_schemaToRows(schema) {
  const props = (schema && schema.properties) || {};
  const required = (schema && schema.required) || [];
  return Object.keys(props).map((name) => {
    const p = props[name] || {};
    return {
      name,
      type: p.type || "string",
      description: p.description || "",
      required: required.indexOf(name) >= 0,
    };
  });
}

function GB_rowsToSchema(rows, opts) {
  const properties = {};
  const required = [];
  for (const r of rows) {
    if (!r.name) continue;
    const p = { type: r.type || "string" };
    if (r.description) p.description = r.description;
    if (r.type === "array") p.items = { type: "string" };
    if (r.type === "object") p.properties = {};
    properties[r.name] = p;
    if (r.required) required.push(r.name);
  }
  const out = { type: "object", properties };
  if (required.length) out.required = required;
  // Structured output wants closed objects.
  if (opts && opts.closed) out.additionalProperties = false;
  return out;
}

function GB_SchemaBuilder(props) {
  const { value, onChange, closed, help, suggestNames, onError, errorKey } = props;
  const { useState } = React;
  const complex = GB_schemaTooComplex(value);
  const [jsonMode, setJsonMode] = useState(complex);
  const rows = GB_schemaToRows(value);

  const setRows = (next) => onChange(GB_rowsToSchema(next, { closed }));

  if (jsonMode || complex) {
    return (
      <div className="col" style={{ gap: 6 }}>
        {complex ? (
          <div className="muted" style={{ fontSize: "var(--fs-11)" }}>
            This shape uses advanced JSON Schema, so it stays in JSON to avoid losing anything.
          </div>
        ) : null}
        {typeof GR_JsonField === "function" ? (
          <GR_JsonField
            label=""
            value={value}
            onChange={onChange}
            onError={onError}
            errorKey={errorKey || "schema"}
            help={help}
          />
        ) : (
          <textarea
            value={JSON.stringify(value || {}, null, 2)}
            onChange={(e) => { try { onChange(JSON.parse(e.target.value)); } catch (_e) { /* keep typing */ } }}
            rows={8}
            style={{ width: "100%", fontFamily: "var(--font-mono)", fontSize: "var(--fs-11)" }}
          />
        )}
        {!complex ? (
          <span
            data-testid="gb-schema-json-toggle"
            onClick={() => setJsonMode(false)}
            style={{ fontSize: "var(--fs-11)", color: "var(--accent)", cursor: "pointer" }}
          >
            ← back to fields
          </span>
        ) : null}
      </div>
    );
  }

  return (
    <div className="col" style={{ gap: 6 }}>
      {rows.map((r, i) => (
        <div
          key={i}
          data-testid="gb-schema-row"
          data-field={r.name}
          className="row"
          style={{
            gap: 8, alignItems: "center", padding: "7px 9px", background: "var(--bg-1)",
            border: "1px solid var(--border)", borderRadius: 7,
          }}
        >
          <input
            value={r.name}
            onChange={(e) => { const n = [...rows]; n[i] = { ...r, name: e.target.value }; setRows(n); }}
            placeholder="field name"
            className="mono"
            style={{
              background: "transparent", border: "none", outline: "none", color: "var(--text)",
              fontSize: "var(--fs-11)", width: 130,
            }}
          />
          <select
            value={r.type}
            onChange={(e) => { const n = [...rows]; n[i] = { ...r, type: e.target.value }; setRows(n); }}
            style={{
              background: "var(--bg-2)", border: "1px solid var(--border)", borderRadius: 5,
              color: "var(--text-2)", fontSize: 10, padding: "1px 6px",
            }}
          >
            {GB_TYPES.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
          </select>
          <span
            onClick={() => { const n = [...rows]; n[i] = { ...r, required: !r.required }; setRows(n); }}
            style={{
              marginLeft: "auto", fontSize: "var(--fs-11)", cursor: "pointer",
              color: r.required ? "var(--accent)" : "var(--text-3)",
            }}
          >
            {r.required ? "required" : "optional"}
          </span>
          <span
            onClick={() => setRows(rows.filter((_x, j) => j !== i))}
            title="Remove field"
            style={{ color: "var(--text-4)", cursor: "pointer", fontSize: 13, lineHeight: 1 }}
          >
            ×
          </span>
        </div>
      ))}

      <div className="row" style={{ gap: 8 }}>
        {suggestNames && suggestNames.length && !rows.length ? (
          <button
            type="button"
            onClick={() => setRows(suggestNames.map((n, i) => ({
              name: n, type: i === 0 ? "boolean" : "string", required: i === 0,
            })))}
            style={{
              flex: 1, padding: 7, borderRadius: 7, cursor: "pointer",
              background: "var(--accent-dim)", border: "1px solid var(--accent-border)",
              color: "var(--accent)", fontSize: "var(--fs-11)",
            }}
          >
            Use {suggestNames.length === 1 ? "this field" : `these ${suggestNames.length} fields`}: {suggestNames.join(", ")}
          </button>
        ) : null}
        <button
          type="button"
          onClick={() => setRows([...rows, { name: "", type: "string", required: false }])}
          style={{
            padding: "7px 10px", borderRadius: 7, cursor: "pointer", background: "var(--bg-2)",
            border: "1px solid var(--border)", color: "var(--text-2)", fontSize: "var(--fs-11)",
          }}
        >
          + Field
        </button>
        <span
          data-testid="gb-schema-json-toggle"
          onClick={() => setJsonMode(true)}
          style={{ marginLeft: "auto", alignSelf: "center", fontSize: "var(--fs-11)", color: "var(--text-3)", cursor: "pointer" }}
        >
          JSON
        </span>
      </div>
      {help ? <span className="muted" style={{ fontSize: "var(--fs-11)" }}>{help}</span> : null}
    </div>
  );
}

Object.assign(window, { GB_SchemaBuilder, GB_schemaToRows, GB_rowsToSchema, GB_schemaTooComplex, GB_TYPES });
