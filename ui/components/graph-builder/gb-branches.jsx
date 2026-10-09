/* global React, GR_parseBranchValue */
// GB_BranchBuilder - routing in plain language. WIRING.md §9.
// Order IS semantics (first match wins), the catch-all is permanent, and the
// response_format prerequisite is surfaced exactly where it bites.

const GB_OP_LABELS = {
  eq: "is", ne: "is not", gt: "is more than", gte: "is at least",
  lt: "is less than", lte: "is at most", in: "is one of", not_in: "is none of",
  exists: "has a value",
};
const GB_OPS = ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "exists"];

// A condition's value is Any in the model (BranchCondition.value), so the box it is edited in has to show EVERY kind of value in a form that reads back to the same value (ticket 01a11e4e-8910:
// an object showed as "[object Object]" and the first keystroke replaced it by that string). A string is itself, unless it would read back as something else (a string "5", "true", "[1]" is
// shown quoted) or an <input> would change it (a control character such as a newline: shown quoted too); null shows as null (an empty box is "no value yet", the empty string); the comma list of
// "is one of" is kept for a list that reads back the same from it (GB_commaForm); anything else that is not text (an object, a list of anything else, a number, a boolean) is JSON.
const GB_NOT_JSON_YET = "Not JSON yet: the stored value is unchanged. Quote text, or clear the box first.";

// The comma-separated text of a list under "is one of" / "is none of", or null when that text would not read back as the same list: a member that has a comma or blanks around it, or that
// looks like JSON (["1"] shown as `1` reads back as [1]). An empty list is an empty box.
function GB_commaForm(value, op) {
  if (!Array.isArray(value) || !(op === "in" || op === "not_in")) return null;
  if (!value.every((m) => typeof m === "string" && m !== "" && m === m.trim() && m.indexOf(",") === -1)) return null;
  const text = value.join(", ");
  const parse = typeof GR_parseBranchValue === "function" ? GR_parseBranchValue : null;
  return parse && JSON.stringify(parse(text, op)) === JSON.stringify(value) ? text : null;
}

function GB_branchValueText(value, op) {
  if (value === undefined) return "";
  if (value === null) return "null";
  if (typeof value === "string") {
    if (value === "") return "";
    if (/[\u0000-\u001f]/.test(value)) return JSON.stringify(value);
    try { JSON.parse(value); return JSON.stringify(value); } catch (_e) { return value; }
  }
  if (Array.isArray(value)) {
    const comma = GB_commaForm(value, op);
    return comma !== null ? comma : JSON.stringify(value);
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// What typing `text` into the box means for the stored value: { commit: true, value } to store it, or { commit: false } to leave the stored value as it is. A structured value (an object, or
// a list that is shown as JSON) is not replaced by the string a half-typed JSON falls back to; only a list shown in the comma form takes any text. Clearing the box is deliberate and stores
// "" (or an empty list under "is one of"), blanks included. Everything else is parsed as it always was.
function GB_branchValueEdit(text, op, current) {
  const parse = typeof GR_parseBranchValue === "function" ? GR_parseBranchValue : (s) => s;
  const structured = current !== null && typeof current === "object" && GB_commaForm(current, op) === null;
  if (structured) {
    if (text.trim() === "") return { commit: true, value: (op === "in" || op === "not_in") && Array.isArray(current) ? [] : "" };
    try { JSON.parse(text); } catch (_e) { return { commit: false }; }
  }
  return { commit: true, value: parse(text, op) };
}

// The value box of one condition. It keeps the text the operator types while the value behind it is being edited (a half-typed object, "5." on the way to 5.5), stores a value only when the
// text means one (GB_branchValueEdit), says so in an alert when a structured value's text is not JSON yet and reports that to the builder's Save gate (`onJsonError`, keyed `errorKey`; the
// same contract as GR_JsonField: cleared on a commit, on a redraw from outside and when the box goes away), and redraws the text when the stored value or the operator changes from outside.
function GB_BranchValueInput(props) {
  const { value, op, ariaLabel, placeholder, disabled, style, onCommit, onJsonError, errorKey, describedBy } = props;
  const canonical = GB_branchValueText(value, op);
  const [text, setText] = React.useState(canonical);
  const committedText = React.useRef(canonical);
  const errId = React.useId() + "e";
  React.useEffect(() => {
    if (canonical !== committedText.current) {
      committedText.current = canonical;
      setText(canonical);
    }
  }, [canonical]);
  // The text only means something against the value it was typed over: until the redraw above has run there is nothing pending.
  const pending = canonical === committedText.current && text !== canonical ? GB_branchValueEdit(text, op, value) : null;
  const invalid = !!pending && !pending.commit;
  const report = React.useRef(onJsonError);
  report.current = onJsonError;
  const reported = React.useRef(false);
  React.useEffect(() => {
    if (invalid === reported.current) return;
    reported.current = invalid;
    if (report.current && errorKey) report.current(errorKey, invalid ? GB_NOT_JSON_YET : null);
  }, [invalid, errorKey]);
  React.useEffect(() => () => {
    if (reported.current && report.current && errorKey) report.current(errorKey, null);
  }, [errorKey]);
  const describedby = [describedBy, invalid ? errId : null].filter(Boolean).join(" ");
  return (
    <>
      <input
        aria-label={ariaLabel}
        aria-describedby={describedby || undefined}
        data-testid="gb-branch-value"
        value={text}
        disabled={disabled}
        placeholder={placeholder}
        aria-invalid={invalid ? "true" : undefined}
        onChange={(e) => {
          const next = e.target.value;
          setText(next);
          const edit = GB_branchValueEdit(next, op, value);
          if (edit.commit) {
            committedText.current = GB_branchValueText(edit.value, op);
            onCommit(edit.value);
          }
        }}
        style={style}
      />
      {invalid ? (
        <span id={errId} role="alert" data-testid="gb-branch-value-error" style={{ width: "100%", fontSize: 10.5, color: "var(--red)" }}>
          {GB_NOT_JSON_YET}
        </span>
      ) : null}
    </>
  );
}

function GB_BranchBuilder(props) {
  const { edge, edgeIdx, draft, sourceNode, dispatch, onAddResponseFormat, readOnly, onJsonError } = props;
  const helpId = React.useId();
  const router = edge.router || { kind: "json_path", branches: [] };
  const nodesById = {};
  for (const n of draft.nodes || []) nodesById[n.id] = n;
  const label = (id) => (nodesById[id] || {}).description || id || "…";

  if (router.kind === "callable") {
    return (
      <div className="col" style={{ gap: 6 }}>
        <div
          style={{
            padding: 11, borderRadius: 9, background: "var(--bg-1)",
            border: "1px solid var(--border)", fontSize: "var(--fs-12)", color: "var(--text-2)",
          }}
        >
          Routing is decided by code (<span className="mono">{router.callable_id}</span>), so this graph needs a limit on how many passes it can run.
        </div>
      </div>
    );
  }

  const usesPaths = (router.branches || []).some((b) => (b.conditions || []).length);
  const needsFields = usesPaths && sourceNode && !sourceNode.response_format;
  const usedPaths = [];
  for (const b of router.branches || []) {
    for (const c of b.conditions || []) {
      const head = String(c.path || "").split(".")[0];
      if (head && usedPaths.indexOf(head) === -1) usedPaths.push(head);
    }
  }

  const parsedPaths = window.GB_schemaPaths
    ? window.GB_schemaPaths(sourceNode && sourceNode.response_format, "", 0).map((f) => f.path)
    : [];

  return (
    <div className="col" style={{ gap: 6 }}>
      {needsFields ? (
        <div
          className="col"
          style={{
            gap: 9, padding: 11, borderRadius: 9,
            border: "1px solid color-mix(in oklab, var(--red) 40%, transparent)",
            background: "color-mix(in oklab, var(--red) 7%, transparent)",
          }}
        >
          <div style={{ fontSize: "var(--fs-11)", color: "var(--red)", lineHeight: 1.5 }}>
            Your branch reads <span className="mono">{usedPaths[0] || "a field"}</span>, so
            “{sourceNode.description || sourceNode.id}” has to answer in fields - free text can't be branched on.
          </div>
          <button
            type="button"
            onClick={() => onAddResponseFormat(sourceNode.id, usedPaths)}
            style={{
              padding: 7, borderRadius: 7, cursor: "pointer", background: "var(--accent-dim)",
              border: "1px solid var(--accent-border)", color: "var(--accent)", fontSize: "var(--fs-11)",
            }}
          >
            Add fields{usedPaths.length ? `: ${usedPaths.join(", ")}` : ""}
          </button>
        </div>
      ) : null}

      {(router.branches || []).map((b, bi) => (
        <div
          key={bi}
          data-testid="gb-branch"
          data-index={bi}
          className="col"
          style={{
            gap: 7, padding: 10, background: "var(--bg-1)", border: "1px solid var(--border)",
            borderLeft: "2px solid var(--green)", borderRadius: 9,
          }}
        >
          {(b.conditions || []).length === 0 ? (
            <div style={{ fontSize: "var(--fs-12)", color: "var(--text-3)" }}>Always (no condition)</div>
          ) : null}
          {(b.conditions || []).map((c, ci) => (
            <div key={ci} className="row" style={{ gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: "var(--fs-12)" }}>
              <span style={{ color: "var(--text-3)" }}>{ci === 0 ? "If" : "and"}</span>
              <select aria-label={"Branch " + (bi + 1) + ", condition " + (ci + 1) + ": field"}
                value={c.path || ""}
                disabled={readOnly}
                onChange={(e) => dispatch({ type: "UPDATE_BRANCH", idx: edgeIdx, bi, patch: { conditions: b.conditions.map((x, j) => (j === ci ? { ...x, path: e.target.value } : x)) } })}
                className="mono"
                style={{ padding: "3px 8px", borderRadius: 6, background: "var(--bg-2)", border: "1px solid var(--border)", color: "var(--text)", fontSize: "var(--fs-11)" }}
              >
                <option value="">choose a field…</option>
                {parsedPaths.map((p) => <option key={p} value={p}>{p}</option>)}
                {c.path && parsedPaths.indexOf(c.path) === -1 ? <option value={c.path}>{c.path}</option> : null}
              </select>
              <select aria-label={"Branch " + (bi + 1) + ", condition " + (ci + 1) + ": operator"}
                data-testid="gb-branch-op"
                value={c.op || "eq"}
                disabled={readOnly}
                onChange={(e) => dispatch({ type: "UPDATE_BRANCH", idx: edgeIdx, bi, patch: { conditions: b.conditions.map((x, j) => (j === ci ? { ...x, op: e.target.value } : x)) } })}
                style={{ padding: "3px 8px", borderRadius: 6, background: "var(--bg-2)", border: "1px solid var(--border)", color: "var(--text)", fontSize: "var(--fs-11)" }}
              >
                {GB_OPS.map((op) => <option key={op} value={op}>{GB_OP_LABELS[op]}</option>)}
              </select>
              {c.op !== "exists" ? (
                <GB_BranchValueInput
                  key={edgeIdx + ":" + bi + ":" + ci}
                  errorKey={"bv:" + edgeIdx + ":" + bi + ":" + ci}
                  onJsonError={onJsonError}
                  describedBy={helpId}
                  ariaLabel={"Branch " + (bi + 1) + ", condition " + (ci + 1) + ": value"}
                  value={c.value}
                  op={c.op}
                  disabled={readOnly}
                  placeholder={c.op === "in" || c.op === "not_in" ? "a, b, c" : "value"}
                  onCommit={(parsed) => dispatch({ type: "UPDATE_BRANCH", idx: edgeIdx, bi, patch: { conditions: b.conditions.map((x, j) => (j === ci ? { ...x, value: parsed } : x)) } })}
                  style={{ padding: "3px 8px", borderRadius: 6, background: "var(--bg-2)", border: "1px solid var(--border)", color: "var(--text)", fontSize: "var(--fs-11)", width: 110 }}
                />
              ) : null}
              {(c.op === "ne" || c.op === "not_in") ? (
                <span className="muted" style={{ fontSize: 10.5, width: "100%" }}>
                  If “{c.path || "this field"}” is missing, this is false too - add a “has a value” check first.
                </span>
              ) : null}
            </div>
          ))}
          <div className="row" style={{ gap: 6, alignItems: "center", fontSize: "var(--fs-12)" }}>
            <span style={{ color: "var(--text-3)" }}>go to</span>
            <select aria-label={"Branch " + (bi + 1) + ": go to"}
              value={b.to_node || ""}
              disabled={readOnly}
              onChange={(e) => dispatch({ type: "UPDATE_BRANCH", idx: edgeIdx, bi, patch: { to_node: e.target.value } })}
              style={{ padding: "3px 9px", borderRadius: 6, background: "color-mix(in oklab, var(--violet) 16%, transparent)", border: "1px solid color-mix(in oklab, var(--violet) 35%, transparent)", color: "var(--text)", fontSize: "var(--fs-11)" }}
            >
              <option value="">choose a step…</option>
              {(draft.nodes || []).filter((n) => n.kind !== "begin").map((n) => (
                <option key={n.id} value={n.id}>{label(n.id)}</option>
              ))}
            </select>
            {!readOnly ? (
              <>
                <span
                  onClick={() => dispatch({ type: "UPDATE_BRANCH", idx: edgeIdx, bi, patch: { conditions: [...(b.conditions || []), { path: "", op: "eq", value: "" }] } })}
                  style={{ marginLeft: "auto", fontSize: "var(--fs-11)", color: "var(--text-3)", cursor: "pointer" }}
                >
                  + condition
                </span>
                <span
                  onClick={() => dispatch({ type: "DELETE_BRANCH", idx: edgeIdx, bi })}
                  style={{ fontSize: 13, color: "var(--text-4)", cursor: "pointer", lineHeight: 1 }}
                  title="Remove branch"
                >
                  ×
                </span>
              </>
            ) : null}
          </div>
        </div>
      ))}

      {!readOnly ? (
        <span
          onClick={() => dispatch({ type: "ADD_BRANCH", idx: edgeIdx, branch: { conditions: [{ path: "", op: "eq", value: "" }], to_node: "" } })}
          style={{ fontSize: "var(--fs-11)", color: "var(--accent)", cursor: "pointer", padding: "2px 0" }}
        >
          + Add a path
        </span>
      ) : null}

      {/* The catch-all is permanent - it replaces the sharpest edge with a default. */}
      <div
        data-testid="gb-branch-catchall"
        className="row"
        style={{
          gap: 6, alignItems: "center", flexWrap: "wrap", padding: 10, background: "var(--bg-1)",
          border: "1px solid var(--border)", borderLeft: "2px solid var(--amber)", borderRadius: 9,
          fontSize: "var(--fs-12)",
        }}
      >
        <span style={{ color: "var(--amber)" }}>In any other case</span>
        <span style={{ color: "var(--text-3)" }}>go to</span>
        <select aria-label="In any other case: go to"
          value={router.default_to || ""}
          disabled={readOnly}
          onChange={(e) => dispatch({ type: "UPDATE_EDGE", idx: edgeIdx, patch: { router: { ...router, default_to: e.target.value || null } } })}
          style={{ padding: "3px 9px", borderRadius: 6, background: "var(--bg-2)", border: "1px solid var(--border)", color: "var(--text)", fontSize: "var(--fs-11)" }}
        >
          <option value="">- nothing -</option>
          {(draft.nodes || []).filter((n) => n.kind !== "begin").map((n) => (
            <option key={n.id} value={n.id}>{label(n.id)}</option>
          ))}
        </select>
        <span style={{ marginLeft: "auto", fontSize: 10, color: "var(--text-4)" }}>always last</span>
        {!router.default_to ? (
          <span style={{ width: "100%", fontSize: "var(--fs-11)", color: "var(--red)" }}>
            Without one, a run that matches nothing stops with an error.
          </span>
        ) : null}
      </div>
      <span className="muted" style={{ fontSize: "var(--fs-11)" }}>Checked in order - the first match wins.</span>
      {!readOnly && (router.branches || []).some((b) => (b.conditions || []).some((c) => c.op !== "exists")) ? (
        <span id={helpId} className="muted" style={{ fontSize: "var(--fs-11)" }}>A value is text, or JSON: 1 is a number, "1" is text.</span>
      ) : null}
    </div>
  );
}

Object.assign(window, { GB_BranchBuilder, GB_BranchValueInput, GB_branchValueText, GB_branchValueEdit, GB_commaForm, GB_OP_LABELS, GB_OPS });
