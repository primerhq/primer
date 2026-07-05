/* global React, Icon */
//
// <SchemaPanel> — the collapsible structured-output side panel
// (Task F2 of the chat-refactor plan, R3/§8.3). <Conversation>
// (ui/components/chat/conversation.jsx) mounts this as an optional
// right-hand sibling of the timeline + composer, gated by its
// `showSchemaPanel` prop (Task B2). Collapsed by default; a header
// toggle opens it.
//
// Two tabs, kept in sync, JSON as the source of truth (§8.3):
//   - JSON: a free-edit textarea with live validation — valid JSON
//     AND a lightweight structural JSON-Schema sanity check (top-level
//     `type`/`properties`/`required`/`enum`/`items` shape). The
//     authoritative check is server-side (A1/A3's
//     `_validate_response_format_schema`, Draft 2020-12) — this is
//     defense-in-depth so the operator gets instant feedback, not a
//     replacement for it.
//   - Builder: a SUBSET editor (flat fields + nested objects/arrays +
//     scalar types `string`/`number`/`integer`/`boolean` + `required` +
//     `enum`). Builder edits regenerate the JSON tab's text; JSON edits
//     re-hydrate the Builder's fields when the schema is representable
//     in that subset, otherwise the Builder shows a graceful "edit in
//     JSON" escape notice (the JSON tab always accepts the full
//     schema regardless).
//
// Whichever tab produced the change, both `onChange` (the canonical
// schema object, or null) and `onValidityChange` (bool) fire the same
// way — <Composer>'s `schemaInvalid` gate (wired since Task B4) reads
// the latter regardless of which tab is open.
//
// This component stays a pure, controlled shell: no network fetch or
// WebSocket code here. The Persistent toggle's actual PUT /chats/{id}/response_format
// (ON) / ephemeral send-frame application (OFF) lives in
// <Conversation>, which owns data/network lifecycle — this only reports
// the toggle flipping via `onPersistentChange`.

const SP_SCALAR_TYPES = ["string", "number", "integer", "boolean"];
const SP_ALL_TYPES = [...SP_SCALAR_TYPES, "object", "array"];
const SP_JSON_SCHEMA_TYPES = [...SP_SCALAR_TYPES, "object", "array", "null"];

let spFieldSeq = 0;
function SP_nextFieldId() {
  spFieldSeq += 1;
  return `sf-${spFieldSeq}-${Math.random().toString(36).slice(2, 7)}`;
}

// Default shape for a freshly-added Builder field. Keeps every
// type-specific slot (children/itemType/itemChildren/enum strings)
// present up front so switching a field's `type` back and forth never
// throws away previously-entered nested state.
function SP_emptyField() {
  return {
    id: SP_nextFieldId(),
    name: "",
    type: "string",
    required: false,
    enum: "",
    children: [],
    itemType: "string",
    itemEnum: "",
    itemChildren: [],
  };
}

// Resets/primes the type-specific slots when a field's own `type`
// select changes — e.g. switching to "object" makes sure `children`
// exists. Merged into the field via updateField's `{...f, ...patch}`,
// so unrelated slots (an array's itemType, say) are preserved rather
// than wiped, in case the operator switches back.
function SP_retypeField(field, nextType) {
  const patch = { type: nextType };
  if (nextType === "object" && !Array.isArray(field.children)) patch.children = [];
  if (nextType === "array") {
    if (!field.itemType) patch.itemType = "string";
    if (!Array.isArray(field.itemChildren)) patch.itemChildren = [];
  }
  return patch;
}

function SP_retypeArrayItem(field, nextItemType) {
  const patch = { itemType: nextItemType };
  if (nextItemType === "object" && !Array.isArray(field.itemChildren)) patch.itemChildren = [];
  return patch;
}

// ---------------------------------------------------------------------
// Builder fields <-> JSON Schema conversion
// ---------------------------------------------------------------------

function SP_parseEnumInput(text, type) {
  const raw = String(text || "").trim();
  if (!raw) return undefined;
  const parts = raw.split(",").map((s) => s.trim()).filter((s) => s.length > 0);
  if (parts.length === 0) return undefined;
  if (type === "number") {
    const nums = parts.map(Number).filter((n) => !Number.isNaN(n));
    return nums.length > 0 ? nums : undefined;
  }
  if (type === "integer") {
    const ints = parts.map((s) => parseInt(s, 10)).filter((n) => !Number.isNaN(n));
    return ints.length > 0 ? ints : undefined;
  }
  if (type === "boolean") return parts.map((s) => s === "true");
  return parts;
}

function SP_enumToInput(enumArr) {
  return Array.isArray(enumArr) ? enumArr.join(", ") : "";
}

function SP_fieldToNode(field) {
  const type = field.type;
  if (type === "object") {
    return SP_fieldsToSchema(field.children || []);
  }
  if (type === "array") {
    let itemNode;
    if (field.itemType === "object") {
      itemNode = SP_fieldsToSchema(field.itemChildren || []);
    } else {
      itemNode = { type: field.itemType || "string" };
      const enumVals = SP_parseEnumInput(field.itemEnum, field.itemType);
      if (enumVals) itemNode.enum = enumVals;
    }
    return { type: "array", items: itemNode };
  }
  const node = { type };
  const enumVals = SP_parseEnumInput(field.enum, type);
  if (enumVals) node.enum = enumVals;
  return node;
}

// Fields with a blank name are dropped rather than surfaced as an
// error — an operator mid-way through adding a field (typed nothing
// yet) shouldn't see a validation failure for it.
function SP_fieldsToSchema(fields) {
  const properties = {};
  const required = [];
  for (const f of fields || []) {
    const name = (f.name || "").trim();
    if (!name) continue;
    properties[name] = SP_fieldToNode(f);
    if (f.required) required.push(name);
  }
  const schema = { type: "object", properties };
  if (required.length > 0) schema.required = required;
  return schema;
}

function SP_schemaNodeToField(name, node, required) {
  const field = SP_emptyField();
  field.name = name;
  const type = node && node.type;
  field.type = SP_ALL_TYPES.includes(type) ? type : "string";
  field.required = !!required;
  if (SP_SCALAR_TYPES.includes(field.type)) {
    field.enum = SP_enumToInput(node && node.enum);
  } else if (field.type === "object") {
    field.children = SP_schemaToFields(node);
  } else if (field.type === "array") {
    const itemNode = (node && node.items) || { type: "string" };
    field.itemType = SP_ALL_TYPES.includes(itemNode.type) ? itemNode.type : "string";
    if (field.itemType === "object") {
      field.itemChildren = SP_schemaToFields(itemNode);
    } else {
      field.itemEnum = SP_enumToInput(itemNode.enum);
    }
  }
  return field;
}

function SP_schemaToFields(schema) {
  if (!schema || schema.type !== "object") return [];
  const props = schema.properties || {};
  const requiredSet = new Set(Array.isArray(schema.required) ? schema.required : []);
  return Object.keys(props).map((name) => SP_schemaNodeToField(name, props[name], requiredSet.has(name)));
}

// ---------------------------------------------------------------------
// Representability (§8.3's Builder SUBSET) + JSON-tab structural check
// ---------------------------------------------------------------------

// Whether `node` can be expressed in the Builder's subset: an object
// with only type/properties/required, an array with only type/items,
// or a scalar with only type/enum — recursively. Anything else
// (oneOf/anyOf/$ref/pattern/format/const/additionalProperties/…) fails
// so the Builder can gracefully defer to the JSON tab instead of
// silently dropping data.
function SP_isRepresentableNode(node) {
  if (!node || typeof node !== "object" || Array.isArray(node)) return false;
  const keys = Object.keys(node);
  const type = node.type;
  if (type === "object") {
    if (!keys.every((k) => k === "type" || k === "properties" || k === "required")) return false;
    const props = node.properties;
    if (props !== undefined && (typeof props !== "object" || props === null || Array.isArray(props))) return false;
    const propNames = props ? Object.keys(props) : [];
    for (const key of propNames) {
      if (!SP_isRepresentableNode(props[key])) return false;
    }
    if (node.required !== undefined) {
      if (!Array.isArray(node.required)) return false;
      for (const r of node.required) {
        if (typeof r !== "string" || !propNames.includes(r)) return false;
      }
    }
    return true;
  }
  if (type === "array") {
    if (!keys.every((k) => k === "type" || k === "items")) return false;
    if (node.items === undefined) return true;
    return SP_isRepresentableNode(node.items);
  }
  if (SP_SCALAR_TYPES.includes(type)) {
    if (!keys.every((k) => k === "type" || k === "enum")) return false;
    if (node.enum !== undefined && !Array.isArray(node.enum)) return false;
    return true;
  }
  return false;
}

function SP_isRepresentableSchema(schema) {
  if (schema === null || schema === undefined) return true; // no schema at all
  return schema.type === "object" && SP_isRepresentableNode(schema);
}

// Lightweight client-side structural check: valid JSON alone isn't a
// valid JSON Schema (e.g. `{"type": "not-a-type"}` parses fine). This
// is intentionally NOT a full Draft 2020-12 meta-schema validator —
// that lives server-side (_validate_response_format_schema /
// _validate_json_schema) and its 422 is the authority; this just
// catches obviously-malformed top-level shapes instantly, client-side.
function SP_validateSchemaStructure(schema) {
  if (schema === null || schema === undefined) return { ok: true };
  if (typeof schema !== "object" || Array.isArray(schema)) {
    return { ok: false, error: "schema must be a JSON object" };
  }
  if (schema.type !== undefined) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    for (const t of types) {
      if (typeof t !== "string" || !SP_JSON_SCHEMA_TYPES.includes(t)) {
        return { ok: false, error: `unknown type "${t}"` };
      }
    }
  }
  if (
    schema.properties !== undefined &&
    (typeof schema.properties !== "object" || schema.properties === null || Array.isArray(schema.properties))
  ) {
    return { ok: false, error: '"properties" must be an object' };
  }
  if (schema.required !== undefined && !Array.isArray(schema.required)) {
    return { ok: false, error: '"required" must be an array' };
  }
  if (schema.enum !== undefined && !Array.isArray(schema.enum)) {
    return { ok: false, error: '"enum" must be an array' };
  }
  if (
    schema.items !== undefined &&
    (typeof schema.items !== "object" || schema.items === null || Array.isArray(schema.items))
  ) {
    return { ok: false, error: '"items" must be a schema object' };
  }
  return { ok: true };
}

// ---------------------------------------------------------------------
// Builder UI — a recursive fields editor (object properties recurse
// into more SP_FieldsEditor instances; an array-of-object's item
// fields do too).
// ---------------------------------------------------------------------

function SP_FieldsEditor({ fields, onChange, depth }) {
  const list = Array.isArray(fields) ? fields : [];

  const updateField = (id, patch) => {
    onChange(list.map((f) => (f.id === id ? { ...f, ...patch } : f)));
  };
  const removeField = (id) => {
    onChange(list.filter((f) => f.id !== id));
  };
  const addField = () => {
    onChange([...list, SP_emptyField()]);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, marginLeft: depth ? 14 : 0 }}>
      {list.map((field) => (
        <div
          key={field.id}
          data-testid="schema-builder-field"
          style={{
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: 6,
            display: "flex",
            flexDirection: "column",
            gap: 4,
          }}
        >
          <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
            <input
              className="input"
              placeholder="field name"
              value={field.name}
              onChange={(e) => updateField(field.id, { name: e.target.value })}
              style={{ flex: 1, minWidth: 0, fontSize: 12 }}
            />
            <select
              className="select"
              data-testid="schema-builder-field-type"
              value={field.type}
              onChange={(e) => updateField(field.id, SP_retypeField(field, e.target.value))}
              style={{ fontSize: 11 }}
            >
              {SP_ALL_TYPES.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
            <label style={{ display: "flex", alignItems: "center", gap: 2, fontSize: 10, whiteSpace: "nowrap", color: "var(--text-2)" }}>
              <input
                type="checkbox"
                data-testid="schema-builder-field-required"
                checked={!!field.required}
                onChange={(e) => updateField(field.id, { required: e.target.checked })}
              />
              required
            </label>
            <button
              type="button"
              title="Remove field"
              data-testid="schema-builder-remove-field"
              onClick={() => removeField(field.id)}
              style={{ background: "transparent", border: "none", color: "var(--text-3)", cursor: "pointer", display: "flex" }}
            >
              <Icon name="trash" size={12} />
            </button>
          </div>

          {SP_SCALAR_TYPES.includes(field.type) && (
            <input
              className="input"
              data-testid="schema-builder-field-enum"
              placeholder="enum values, comma-separated (optional)"
              value={field.enum || ""}
              onChange={(e) => updateField(field.id, { enum: e.target.value })}
              style={{ fontSize: 11 }}
            />
          )}

          {field.type === "object" && (
            <SP_FieldsEditor
              fields={field.children}
              onChange={(next) => updateField(field.id, { children: next })}
              depth={(depth || 0) + 1}
            />
          )}

          {field.type === "array" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 4, marginLeft: 14 }}>
              <div style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 10, color: "var(--text-3)" }}>
                items of type
                <select
                  className="select"
                  data-testid="schema-builder-item-type"
                  value={field.itemType || "string"}
                  onChange={(e) => updateField(field.id, SP_retypeArrayItem(field, e.target.value))}
                  style={{ fontSize: 11 }}
                >
                  {SP_ALL_TYPES.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              </div>
              {SP_SCALAR_TYPES.includes(field.itemType || "string") && (
                <input
                  className="input"
                  placeholder="enum values, comma-separated (optional)"
                  value={field.itemEnum || ""}
                  onChange={(e) => updateField(field.id, { itemEnum: e.target.value })}
                  style={{ fontSize: 11 }}
                />
              )}
              {field.itemType === "object" && (
                <SP_FieldsEditor
                  fields={field.itemChildren}
                  onChange={(next) => updateField(field.id, { itemChildren: next })}
                  depth={(depth || 0) + 1}
                />
              )}
            </div>
          )}
        </div>
      ))}
      <button
        type="button"
        data-testid="schema-builder-add-field"
        className="btn btn-sm"
        onClick={addField}
        style={{ alignSelf: "flex-start", display: "flex", alignItems: "center", gap: 4 }}
      >
        <Icon name="plus" size={11} /> Add field
      </button>
    </div>
  );
}

function SchemaPanel({
  value,
  onChange,
  persistent,
  onPersistentChange,
  valid,
  onValidityChange,
  collapsed = true,
  onToggle,
}) {
  const [tab, setTab] = React.useState("builder"); // "builder" | "json"
  const [builderFields, setBuilderFields] = React.useState(() => (SP_isRepresentableSchema(value) ? SP_schemaToFields(value) : []));
  const [builderEscape, setBuilderEscape] = React.useState(() => !SP_isRepresentableSchema(value));
  const [jsonText, setJsonText] = React.useState(() => (value ? JSON.stringify(value, null, 2) : ""));
  const [jsonError, setJsonError] = React.useState(null);

  // Tracks the last schema THIS component pushed via onChange (as its
  // JSON string) so the hydration effect below can tell "the parent
  // just echoed our own edit back" apart from "the schema changed out
  // from under us" (e.g. the host loading a persisted Chat.response_format
  // once its fetch resolves) — only the latter should re-hydrate.
  const lastEmittedRef = React.useRef(JSON.stringify(value === undefined ? null : value));

  React.useEffect(() => {
    const incoming = JSON.stringify(value === undefined ? null : value);
    if (incoming === lastEmittedRef.current) return; // our own echo — ignore
    lastEmittedRef.current = incoming;
    setJsonText(value ? JSON.stringify(value, null, 2) : "");
    setJsonError(null);
    if (SP_isRepresentableSchema(value)) {
      setBuilderFields(SP_schemaToFields(value));
      setBuilderEscape(false);
    } else {
      setBuilderEscape(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const emit = (schema) => {
    lastEmittedRef.current = JSON.stringify(schema === undefined ? null : schema);
    if (typeof onChange === "function") onChange(schema);
  };

  // Builder tab: every edit regenerates the JSON tab's text too (JSON
  // is the source of truth per §8.3, but the Builder is what the
  // operator is actively driving, so it writes straight through). A
  // Builder-produced schema is always structurally sound by
  // construction, so validity is unconditionally true here.
  const handleBuilderFieldsChange = (nextFields) => {
    setBuilderFields(nextFields);
    const schema = SP_fieldsToSchema(nextFields);
    setJsonText(JSON.stringify(schema, null, 2));
    setJsonError(null);
    emit(schema);
    if (typeof onValidityChange === "function") onValidityChange(true);
  };

  // JSON tab: live validation — valid JSON AND a valid-looking JSON
  // Schema (client-side structural check; server re-validates against
  // the full Draft 2020-12 meta-schema on apply/send). Only a
  // successfully-validated schema propagates to onChange/the Builder —
  // an in-progress invalid edit blocks Send but doesn't clobber the
  // last known-good `value`.
  const handleJsonTextChange = (text) => {
    setJsonText(text);
    const trimmed = text.trim();
    if (!trimmed) {
      setJsonError(null);
      setBuilderFields([]);
      setBuilderEscape(false);
      emit(null);
      if (typeof onValidityChange === "function") onValidityChange(true);
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (err) {
      setJsonError(err && err.message ? err.message : "invalid JSON");
      if (typeof onValidityChange === "function") onValidityChange(false);
      return;
    }
    const structural = SP_validateSchemaStructure(parsed);
    if (!structural.ok) {
      setJsonError(structural.error);
      if (typeof onValidityChange === "function") onValidityChange(false);
      return;
    }
    setJsonError(null);
    emit(parsed);
    if (typeof onValidityChange === "function") onValidityChange(true);
    if (SP_isRepresentableSchema(parsed)) {
      setBuilderFields(SP_schemaToFields(parsed));
      setBuilderEscape(false);
    } else {
      setBuilderEscape(true);
    }
  };

  if (collapsed) {
    return (
      <div
        className="schema-panel schema-panel-collapsed"
        style={{
          borderLeft: "1px solid var(--border)",
          display: "flex",
          alignItems: "flex-start",
          padding: "8px 4px",
        }}
      >
        <button
          type="button"
          title="Show structured output panel"
          data-testid="schema-panel-toggle"
          onClick={onToggle}
          style={{
            background: "transparent",
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: "6px 4px",
            color: "var(--text-2)",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
          }}
        >
          <Icon name="chevron-left" size={14} />
        </button>
      </div>
    );
  }

  return (
    <div
      className="schema-panel"
      style={{
        borderLeft: "1px solid var(--border)",
        width: 280,
        flexShrink: 0,
        display: "flex",
        flexDirection: "column",
        minWidth: 0,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "8px 10px",
          borderBottom: "1px solid var(--border)",
        }}
      >
        <div className="tab-strip" style={{ display: "flex", gap: 4 }}>
          <button
            type="button"
            data-testid="schema-tab-builder"
            onClick={() => setTab("builder")}
            className={tab === "builder" ? "btn btn-sm btn-primary" : "btn btn-sm"}
          >Builder</button>
          <button
            type="button"
            data-testid="schema-tab-json"
            onClick={() => setTab("json")}
            className={tab === "json" ? "btn btn-sm btn-primary" : "btn btn-sm"}
          >JSON</button>
        </div>
        <button
          type="button"
          title="Collapse structured output panel"
          data-testid="schema-panel-toggle"
          onClick={onToggle}
          style={{ background: "transparent", border: "none", color: "var(--text-3)", cursor: "pointer" }}
        >
          <Icon name="chevron-right" size={14} />
        </button>
      </div>

      <div style={{ padding: "8px 10px", borderBottom: "1px solid var(--border)" }}>
        <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--text-2)" }}>
          <input
            type="checkbox"
            data-testid="schema-persistent-toggle"
            checked={!!persistent}
            onChange={(e) => typeof onPersistentChange === "function" && onPersistentChange(e.target.checked)}
          />
          Persistent
        </label>
        <div style={{ marginTop: 2, fontSize: 10, color: "var(--text-3)" }}>
          {persistent
            ? "Applies to every message in this chat."
            : "Applies to the next message only."}
        </div>
        {valid === false && (
          <div data-testid="schema-invalid-banner" style={{ marginTop: 6, fontSize: 11, color: "var(--red, #c33)" }}>
            <Icon name="warn-circle" size={11} /> {jsonError || "schema invalid"} — send disabled
          </div>
        )}
      </div>

      <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 10, color: "var(--text-3)", fontSize: 12 }}>
        {tab === "builder" ? (
          <div data-testid="schema-builder-body">
            {builderEscape ? (
              <div data-testid="schema-builder-escape" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 4, color: "var(--text-2)" }}>
                  <Icon name="warn-circle" size={12} />
                  This schema is too complex for the Builder.
                </div>
                <button
                  type="button"
                  data-testid="schema-builder-edit-in-json"
                  className="btn btn-sm"
                  onClick={() => setTab("json")}
                >Edit in JSON</button>
              </div>
            ) : (
              <SP_FieldsEditor fields={builderFields} onChange={handleBuilderFieldsChange} depth={0} />
            )}
          </div>
        ) : (
          <div data-testid="schema-json-body" style={{ display: "flex", flexDirection: "column", gap: 6, height: "100%" }}>
            <textarea
              data-testid="schema-json-textarea"
              className="textarea mono"
              value={jsonText}
              onChange={(e) => handleJsonTextChange(e.target.value)}
              placeholder="{ }"
              style={{ width: "100%", minHeight: 160, resize: "vertical", flex: 1 }}
            />
            {jsonError && (
              <div data-testid="schema-json-error" style={{ fontSize: 11, color: "var(--red, #c33)" }}>
                <Icon name="warn-circle" size={11} /> {jsonError}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

window.SchemaPanel = SchemaPanel;
