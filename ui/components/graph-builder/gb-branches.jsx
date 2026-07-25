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

function GB_BranchBuilder(props) {
  const { edge, edgeIdx, draft, sourceNode, dispatch, onAddResponseFormat, readOnly } = props;
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
              <select
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
              <select
                data-testid="gb-branch-op"
                value={c.op || "eq"}
                disabled={readOnly}
                onChange={(e) => dispatch({ type: "UPDATE_BRANCH", idx: edgeIdx, bi, patch: { conditions: b.conditions.map((x, j) => (j === ci ? { ...x, op: e.target.value } : x)) } })}
                style={{ padding: "3px 8px", borderRadius: 6, background: "var(--bg-2)", border: "1px solid var(--border)", color: "var(--text)", fontSize: "var(--fs-11)" }}
              >
                {GB_OPS.map((op) => <option key={op} value={op}>{GB_OP_LABELS[op]}</option>)}
              </select>
              {c.op !== "exists" ? (
                <input
                  value={Array.isArray(c.value) ? c.value.join(", ") : (c.value == null ? "" : String(c.value))}
                  disabled={readOnly}
                  placeholder={c.op === "in" || c.op === "not_in" ? "a, b, c" : "value"}
                  onChange={(e) => {
                    const parsed = typeof GR_parseBranchValue === "function"
                      ? GR_parseBranchValue(e.target.value, c.op)
                      : e.target.value;
                    dispatch({ type: "UPDATE_BRANCH", idx: edgeIdx, bi, patch: { conditions: b.conditions.map((x, j) => (j === ci ? { ...x, value: parsed } : x)) } });
                  }}
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
            <select
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
        <select
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
    </div>
  );
}

Object.assign(window, { GB_BranchBuilder, GB_OP_LABELS, GB_OPS });
