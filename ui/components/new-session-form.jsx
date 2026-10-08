/* global React, FormField */
// ---------------------------------------------------------------------------
// The graph-input field of the New session overlay (console/nv-overlays.jsx, NV_CreateSessionOverlay).
//
// This file used to hold the console's create-session form (FD2). The shell's overlay replaced it, nothing rendered it any more, and it was removed together with the static tests
// that pinned its source text. What is left is SharedNewSessionSchemaField: the row the overlay draws for each property of a graph's Begin.input_schema. The name keeps its
// "Shared" prefix because the bundle and the tests know the file and the export by it.
//
// No-build rules: top-level `function`/`var`; exported as window.SharedNewSessionSchemaField.
// ---------------------------------------------------------------------------

// One field of the dynamic Begin.input_schema form. Renders an input control
// chosen from the JSON-Schema fragment (ported from the old app.jsx
// _GraphInputSchemaField).
function SharedNewSessionSchemaField({ propKey, schema, value, onChange }) {
  var label = (schema && schema.title) || propKey;
  var help = schema && schema.description;
  var placeholder =
    schema && Array.isArray(schema.examples) && schema.examples.length > 0
      ? String(schema.examples[0])
      : "";

  var control = null;
  if (schema && Array.isArray(schema.enum)) {
    control = (
      <select
        className="select"
        value={value == null ? "" : value}
        onChange={function (e) { onChange(e.target.value); }}
        style={{ width: "100%" }}
      >
        <option value="">—</option>
        {schema.enum.map(function (v) {
          return <option key={String(v)} value={v}>{String(v)}</option>;
        })}
      </select>
    );
  } else if (schema && schema.type === "boolean") {
    control = (
      <input
        type="checkbox"
        checked={!!value}
        onChange={function (e) { onChange(e.target.checked); }}
      />
    );
  } else if (schema && (schema.type === "integer" || schema.type === "number")) {
    control = (
      <input
        type="number"
        className="input"
        value={value == null ? "" : value}
        placeholder={placeholder}
        onChange={function (e) {
          var raw = e.target.value;
          if (raw === "") { onChange(""); return; }
          var parsed = schema.type === "integer" ? parseInt(raw, 10) : Number(raw);
          onChange(Number.isFinite(parsed) ? parsed : raw);
        }}
        style={{ width: "100%" }}
      />
    );
  } else if (schema && (schema.type === "object" || schema.type === "array")) {
    // JSON textarea — parse-on-change so the submitted value is the structured
    // object/array, not a raw string.
    control = (
      <textarea
        className="textarea mono"
        defaultValue={value != null ? JSON.stringify(value, null, 2) : ""}
        placeholder={placeholder || (schema.type === "array" ? "[]" : "{}")}
        rows={4}
        onChange={function (e) {
          try {
            onChange(JSON.parse(e.target.value));
          } catch (_err) {
            onChange(e.target.value);
          }
        }}
      />
    );
  } else {
    // Plain string fields default to a resizable multi-line textarea — an
    // uncapped (or generously capped) string schema property (e.g. a
    // graph's freeform "question" field, which typically has NO maxLength
    // at all) is often long, and a single-line <input> for unbounded text
    // is a poor fit regardless of the property's name. Only an EXPLICIT
    // short maxLength signals a genuinely short field (e.g. a "name"/"id"
    // property capped well under this threshold).
    var long = !schema || typeof schema.maxLength !== "number" || schema.maxLength >= 120;
    control = long ? (
      <textarea
        className="textarea"
        value={value == null ? "" : value}
        placeholder={placeholder}
        rows={4}
        onChange={function (e) { onChange(e.target.value); }}
      />
    ) : (
      <input
        type="text"
        className="input"
        value={value == null ? "" : value}
        placeholder={placeholder}
        onChange={function (e) { onChange(e.target.value); }}
        style={{ width: "100%" }}
      />
    );
  }

  return <FormField label={label} help={help}>{control}</FormField>;
}

window.SharedNewSessionSchemaField = SharedNewSessionSchemaField;
