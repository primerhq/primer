/* global React, EntityPicker, GB_KindDot, GB_RefEditor, GB_SchemaBuilder, GB_BranchBuilder, GB_ToolPicker, GB_KIND_META */
// GB_Inspector - the right panel. Leads with what the step DOES, not with its
// id: `description` is the title, `id` is small mono text in the header.
// WIRING.md §4 / §5.

function GB_Section({ title, hint, children }) {
  return (
    <div className="col" style={{ gap: 7 }}>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <div style={{ fontSize: 10.5, fontWeight: 600, letterSpacing: ".07em", color: "var(--text-3)", textTransform: "uppercase" }}>
          {title}
        </div>
        {hint ? <span style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--text-4)" }}>{hint}</span> : null}
      </div>
      {children}
    </div>
  );
}

function GB_Inspector(props) {
  const {
    draft, node, edgeIdx, dispatch, tools, readOnly, problems,
    onSelectNode, sampleByExpr, onJsonError,
  } = props;
  const { useState } = React;
  const [advanced, setAdvanced] = useState(false);

  // An edge is selected: show its routing.
  if (node == null && edgeIdx != null) {
    const edge = (draft.edges || [])[edgeIdx];
    if (!edge) return null;
    const source = (draft.nodes || []).find((n) => n.id === edge.from_node);
    return (
      <div className="col" style={{ minHeight: 0, flex: 1 }} data-testid="gb-inspector">
        <div className="col" style={{ gap: 8, padding: 14, borderBottom: "1px solid var(--border)" }}>
          <div className="row" style={{ gap: 9, alignItems: "center" }}>
            <div data-testid="gb-inspector-title" style={{ fontSize: "var(--fs-13)", fontWeight: 600 }}>
              What happens after “{(source || {}).description || edge.from_node}”
            </div>
          </div>
          <div className="muted" style={{ fontSize: "var(--fs-11)" }}>Checked in order - the first match wins.</div>
        </div>
        <div className="col" style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 14, gap: 16 }}>
          {edge.kind === "conditional" ? (
            <GB_BranchBuilder
              edge={edge}
              edgeIdx={edgeIdx}
              draft={draft}
              sourceNode={source}
              dispatch={dispatch}
              readOnly={readOnly}
              onAddResponseFormat={(nodeId, paths) => {
                const props2 = {};
                (paths || []).forEach((p, i) => { props2[p] = { type: i === 0 ? "boolean" : "string" }; });
                dispatch({
                  type: "UPDATE_NODE", id: nodeId,
                  patch: { response_format: { type: "object", properties: props2, required: paths.slice(0, 1), additionalProperties: false } },
                });
                onSelectNode(nodeId);
              }}
            />
          ) : (
            <div className="col" style={{ gap: 8 }}>
              <div style={{ fontSize: "var(--fs-12)", color: "var(--text-2)" }}>
                Always continues to “{((draft.nodes || []).find((n) => n.id === edge.to_node) || {}).description || edge.to_node}”.
              </div>
              {!readOnly ? (
                <button
                  type="button"
                  onClick={() => dispatch({
                    type: "UPDATE_EDGE", idx: edgeIdx,
                    patch: { kind: "conditional", to_node: undefined, router: { kind: "json_path", branches: [{ conditions: [{ path: "", op: "eq", value: "" }], to_node: edge.to_node }], default_to: edge.to_node } },
                  })}
                  style={{ alignSelf: "flex-start", padding: "7px 11px", borderRadius: 8, background: "var(--bg-2)", border: "1px solid var(--border)", color: "var(--text)", fontSize: "var(--fs-11)", cursor: "pointer" }}
                >
                  Make this a choice between paths
                </button>
              ) : null}
              {!readOnly ? (
                <span
                  onClick={() => dispatch({ type: "DELETE_EDGE", idx: edgeIdx })}
                  style={{ fontSize: "var(--fs-11)", color: "var(--red)", cursor: "pointer" }}
                >
                  Remove this connection
                </span>
              ) : null}
            </div>
          )}
        </div>
      </div>
    );
  }

  if (!node) {
    return (
      <div className="col" style={{ padding: 20, gap: 8 }} data-testid="gb-inspector">
        <div style={{ fontSize: "var(--fs-13)", fontWeight: 600 }}>Nothing selected</div>
        <div className="muted" style={{ fontSize: "var(--fs-12)" }}>
          Pick a step on the canvas or in the list to see what it does.
        </div>
      </div>
    );
  }

  const meta = GB_KIND_META[node.kind] || {};
  const patch = (p) => dispatch({ type: "UPDATE_NODE", id: node.id, patch: p });
  const outgoing = (draft.edges || [])
    .map((e, i) => ({ e, i }))
    .filter(({ e }) => e.from_node === node.id && e.kind === "conditional");

  return (
    <div className="col" style={{ minHeight: 0, flex: 1 }} data-testid="gb-inspector">
      <div className="col" style={{ gap: 8, padding: 14, borderBottom: "1px solid var(--border)" }}>
        <div className="row" style={{ gap: 9, alignItems: "center" }}>
          <GB_KindDot kind={node.kind} size={26} />
          <input
            data-testid="gb-inspector-title"
            value={node.description || ""}
            readOnly={readOnly}
            placeholder="Name this step"
            onChange={(e) => patch({ description: e.target.value })}
            style={{
              fontSize: "var(--fs-14)", fontWeight: 600, background: "transparent", border: "none",
              outline: "none", color: "var(--text)", flex: 1, minWidth: 0, padding: 0,
            }}
          />
          <span className="mono" style={{ marginLeft: "auto", fontSize: 10, color: "var(--text-4)", flex: "0 0 auto" }}>{node.id}</span>
        </div>
        <div className="muted" style={{ fontSize: "var(--fs-11)" }}>
          {meta.label}{problems && problems.length ? ` · ${problems[0].message}` : ""}
        </div>
      </div>

      <div className="col" style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 14, gap: 16 }}>
        {node.kind === "agent" ? (
          <>
            <GB_Section title="Which agent">
              <EntityPicker
                path="/agents"
                value={node.agent_id || ""}
                onChange={(v) => patch({ agent_id: v })}
                placeholder="Search agents…"
                testid="gb-agent-picker"
              />
            </GB_Section>
            <GB_Section title="What it gets" hint="from earlier steps">
              <GB_RefEditor
                value={node.input_template || ""}
                onChange={(v) => patch({ input_template: v })}
                draft={draft}
                nodeId={node.id}
                readOnly={readOnly}
                sampleByExpr={sampleByExpr}
              />
            </GB_Section>
            <GB_Section title="What it gives back" hint={node.response_format ? "structured" : "free text"}>
              <GB_SchemaBuilder
                value={node.response_format}
                onChange={(v) => patch({ response_format: v })}
                closed
                onError={onJsonError}
                errorKey={`rf:${node.id}`}
                help="Set fields when a branch needs to read this step's answer."
              />
            </GB_Section>
          </>
        ) : null}

        {node.kind === "tool_call" ? (
          <>
            <GB_Section title="Which tool">
              <GB_ToolPicker tools={tools} value={node.tool_id || ""} onChange={(v) => patch({ tool_id: v })} />
            </GB_Section>
            <GB_ToolArguments node={node} tools={tools} draft={draft} patch={patch} readOnly={readOnly} sampleByExpr={sampleByExpr} />
          </>
        ) : null}

        {node.kind === "graph" ? (
          <>
            <GB_Section title="Which graph">
              <EntityPicker path="/graphs" value={node.graph_id || ""} onChange={(v) => patch({ graph_id: v })} placeholder="Search graphs…" testid="gb-graph-picker" />
            </GB_Section>
            <GB_Section title="What it gets">
              <GB_RefEditor value={node.input_template || ""} onChange={(v) => patch({ input_template: v })} draft={draft} nodeId={node.id} readOnly={readOnly} sampleByExpr={sampleByExpr} />
            </GB_Section>
          </>
        ) : null}

        {node.kind === "begin" ? (
          <GB_Section title="What this graph takes">
            <GB_SchemaBuilder
              value={node.input_schema}
              onChange={(v) => patch({ input_schema: v })}
              onError={onJsonError}
              errorKey={`is:${node.id}`}
              help="Named fields here become the chips other steps can insert."
            />
          </GB_Section>
        ) : null}

        {node.kind === "end" ? (
          <>
            <GB_Section title="What it returns">
              <GB_RefEditor value={node.output_template || ""} onChange={(v) => patch({ output_template: v })} draft={draft} nodeId={node.id} readOnly={readOnly} sampleByExpr={sampleByExpr} />
            </GB_Section>
            <GB_Section title="Shape of the result" hint="optional">
              <GB_SchemaBuilder value={node.output_schema} onChange={(v) => patch({ output_schema: v })} onError={onJsonError} errorKey={`os:${node.id}`} />
            </GB_Section>
          </>
        ) : null}

        {node.kind === "fan_in" ? (
          <GB_Section title="How to merge" hint="waits for all">
            <GB_RefEditor value={node.aggregate_template || ""} onChange={(v) => patch({ aggregate_template: v })} draft={draft} nodeId={node.id} readOnly={readOnly} sampleByExpr={sampleByExpr} />
          </GB_Section>
        ) : null}

        {node.kind === "fan_out" ? (
          <GB_FanOutBody node={node} draft={draft} dispatch={dispatch} readOnly={readOnly} />
        ) : null}

        {outgoing.map(({ e, i }) => (
          <GB_Section key={i} title="What happens next" hint="first match wins">
            <GB_BranchBuilder
              edge={e}
              edgeIdx={i}
              draft={draft}
              sourceNode={node}
              dispatch={dispatch}
              readOnly={readOnly}
              onAddResponseFormat={(nodeId, paths) => {
                const props2 = {};
                (paths || []).forEach((p, idx) => { props2[p] = { type: idx === 0 ? "boolean" : "string" }; });
                dispatch({ type: "UPDATE_NODE", id: nodeId, patch: { response_format: { type: "object", properties: props2, required: paths.slice(0, 1), additionalProperties: false } } });
              }}
            />
          </GB_Section>
        ))}

        <div
          onClick={() => setAdvanced(!advanced)}
          className="row"
          style={{
            gap: 8, alignItems: "center", padding: "10px 11px", background: "var(--bg-elev)",
            border: "1px solid var(--border)", borderRadius: 9, color: "var(--text-3)",
            fontSize: "var(--fs-11)", cursor: "pointer",
          }}
        >
          {advanced ? "▾" : "▸"} Advanced · raw template, step id
        </div>
        {advanced ? (
          <div className="col" style={{ gap: 10 }}>
            <label className="col" style={{ gap: 5 }}>
              <span className="muted" style={{ fontSize: "var(--fs-11)" }}>Step id (renaming rewrites every reference)</span>
              <input
                className="mono"
                defaultValue={node.id}
                readOnly={readOnly}
                onBlur={(e) => {
                  const v = e.target.value.trim();
                  if (v && v !== node.id) dispatch({ type: "RENAME_NODE", id: node.id, newId: v });
                }}
                style={{ background: "var(--bg-1)", border: "1px solid var(--border)", borderRadius: 6, padding: "6px 9px", color: "var(--text)", fontSize: "var(--fs-11)" }}
              />
            </label>
            {(window.GB_TEMPLATE_FIELDS[node.kind] || []).map((f) => (
              <label key={f} className="col" style={{ gap: 5 }}>
                <span className="muted mono" style={{ fontSize: "var(--fs-11)" }}>{f}</span>
                <textarea
                  value={node[f] || ""}
                  readOnly={readOnly}
                  onChange={(e) => patch({ [f]: e.target.value })}
                  rows={4}
                  style={{ background: "var(--bg-1)", border: "1px solid var(--border)", borderRadius: 6, padding: "7px 9px", color: "var(--text-2)", fontSize: "var(--fs-11)", fontFamily: "var(--font-mono)" }}
                />
              </label>
            ))}
            {!readOnly ? (
              <span
                onClick={() => dispatch({ type: "DELETE_NODE", id: node.id })}
                style={{ fontSize: "var(--fs-11)", color: "var(--red)", cursor: "pointer" }}
              >
                Delete this step
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// Tool arguments rendered FROM the tool's input_schema (no free-text map).
function GB_ToolArguments({ node, tools, draft, patch, readOnly, sampleByExpr }) {
  const tool = (tools || []).find((t) => t.id === node.tool_id);
  const schema = tool && tool.input_schema;
  const useTemplate = node.arguments_template != null;
  const props = (schema && schema.properties) || {};
  const names = Object.keys(props);
  const required = (schema && schema.required) || [];

  return (
    <GB_Section title="What it runs with">
      <div className="col" style={{ gap: 8 }}>
        <div className="row" style={{ gap: 12, alignItems: "center", fontSize: "var(--fs-11)" }}>
          <label className="row" style={{ gap: 5, alignItems: "center", cursor: "pointer" }}>
            <input type="radio" checked={!useTemplate} readOnly={readOnly} onChange={() => patch({ arguments_template: null })} />
            <span>Fill in the fields</span>
          </label>
          <label className="row" style={{ gap: 5, alignItems: "center", cursor: "pointer" }}>
            <input type="radio" checked={useTemplate} readOnly={readOnly} onChange={() => patch({ arguments_template: node.arguments_template || "{}" })} />
            <span>Build the whole argument object</span>
          </label>
        </div>

        {useTemplate ? (
          <textarea
            value={node.arguments_template || ""}
            readOnly={readOnly}
            onChange={(e) => patch({ arguments_template: e.target.value })}
            rows={5}
            style={{ background: "var(--bg-1)", border: "1px solid var(--border)", borderRadius: 8, padding: 9, color: "var(--text-2)", fontSize: "var(--fs-11)", fontFamily: "var(--font-mono)" }}
          />
        ) : !tool ? (
          <span className="muted" style={{ fontSize: "var(--fs-11)" }}>Choose a tool to see what it needs.</span>
        ) : !names.length ? (
          <span className="muted" style={{ fontSize: "var(--fs-11)" }}>This tool takes no arguments.</span>
        ) : (
          names.map((name) => (
            <div key={name} className="col" style={{ gap: 4 }}>
              <div className="row" style={{ gap: 6, alignItems: "center" }}>
                <span className="mono" style={{ fontSize: "var(--fs-11)" }}>{name}</span>
                <span style={{ fontSize: 10, color: "var(--text-4)" }}>{(props[name] || {}).type || "string"}</span>
                {required.indexOf(name) >= 0 ? <span style={{ fontSize: 10, color: "var(--accent)" }}>required</span> : null}
              </div>
              <GB_RefEditor
                value={(node.arguments || {})[name] == null ? "" : String((node.arguments || {})[name])}
                onChange={(v) => patch({ arguments: { ...(node.arguments || {}), [name]: v } })}
                draft={draft}
                nodeId={node.id}
                readOnly={readOnly}
                sampleByExpr={sampleByExpr}
              />
            </div>
          ))
        )}
      </div>
    </GB_Section>
  );
}

// "How to split" in plain language - the panel that replaces drawing an edge.
function GB_FanOutBody({ node, draft, dispatch, readOnly }) {
  const specs = node.specs || [];
  const spec = specs[0] || { kind: "broadcast", count: 3, on_failure: "collect" };
  const setSpec = (p) => (specs.length
    ? dispatch({ type: "UPDATE_FANOUT_SPEC", id: node.id, i: 0, patch: p })
    : dispatch({ type: "ADD_FANOUT_SPEC", id: node.id, spec: { ...spec, ...p } }));
  const candidates = (draft.nodes || []).filter((n) => n.kind !== "begin" && n.kind !== "fan_out" && n.id !== node.id);
  const label = (id) => (candidates.find((n) => n.id === id) || {}).description || id;

  const KINDS = [
    { id: "map", title: "One copy per item in a list", hint: "Six sections in, six writers out." },
    { id: "broadcast", title: "The same step, N times", hint: "Five drafts of one brief, pick the best." },
    { id: "tee", title: "Several different steps at once", hint: "Fact-check, SEO pass and tone pass in parallel." },
  ];
  const FAILS = [
    { id: "fail_fast", label: "Stop everything" },
    { id: "drain_then_fail", label: "Finish, then fail" },
    { id: "collect", label: "Keep the rest" },
  ];

  return (
    <>
      <GB_Section title="How to split">
        <div className="col" style={{ gap: 6 }}>
          {KINDS.map((k) => {
            const on = spec.kind === k.id;
            return (
              <div
                key={k.id}
                onClick={() => !readOnly && setSpec({
                  kind: k.id,
                  target_node_ids: k.id === "tee" ? (spec.target_node_ids || []) : undefined,
                  target_node_id: k.id === "tee" ? undefined : spec.target_node_id,
                  count: k.id === "broadcast" ? (spec.count || 3) : undefined,
                  source_node_id: k.id === "map" ? spec.source_node_id : undefined,
                  source_path: k.id === "map" ? spec.source_path : undefined,
                })}
                className="row"
                style={{
                  gap: 10, alignItems: "flex-start", padding: 11, borderRadius: 9, cursor: "pointer",
                  background: on ? "var(--bg-1)" : "var(--bg-elev)",
                  border: `1px solid ${on ? "var(--accent-border)" : "var(--border)"}`,
                }}
              >
                <span style={{ width: 15, height: 15, borderRadius: "50%", flex: "0 0 auto", marginTop: 1, border: on ? "4px solid var(--accent)" : "1.5px solid var(--border-strong)", background: on ? "var(--bg-1)" : "transparent" }} />
                <div>
                  <div style={{ fontSize: "var(--fs-12)", fontWeight: on ? 500 : 400, color: on ? "var(--text)" : "var(--text-2)" }}>{k.title}</div>
                  <div className="muted" style={{ fontSize: "var(--fs-11)", marginTop: 2 }}>{k.hint}</div>
                </div>
              </div>
            );
          })}
        </div>
      </GB_Section>

      {spec.kind === "map" ? (
        <GB_Section title="The list">
          <div className="row" style={{ gap: 6, alignItems: "center" }}>
            <select
              value={spec.source_node_id || ""}
              disabled={readOnly}
              onChange={(e) => setSpec({ source_node_id: e.target.value })}
              style={{ padding: "6px 9px", borderRadius: 7, background: "var(--bg-1)", border: "1px solid var(--border)", color: "var(--text)", fontSize: "var(--fs-11)" }}
            >
              <option value="">which step…</option>
              {candidates.map((n) => <option key={n.id} value={n.id}>{label(n.id)}</option>)}
            </select>
            <input
              value={spec.source_path || ""}
              disabled={readOnly}
              placeholder="field holding the list, e.g. items"
              onChange={(e) => setSpec({ source_path: e.target.value })}
              className="mono"
              style={{ flex: 1, padding: "6px 9px", borderRadius: 7, background: "var(--bg-1)", border: "1px solid var(--border)", color: "var(--text)", fontSize: "var(--fs-11)" }}
            />
          </div>
        </GB_Section>
      ) : null}

      {spec.kind === "broadcast" ? (
        <GB_Section title="How many copies">
          <input
            type="number"
            min={1}
            value={spec.count || 1}
            disabled={readOnly}
            onChange={(e) => setSpec({ count: Number(e.target.value) })}
            style={{ width: 90, padding: "6px 9px", borderRadius: 7, background: "var(--bg-1)", border: "1px solid var(--border)", color: "var(--text)", fontSize: "var(--fs-11)" }}
          />
        </GB_Section>
      ) : null}

      <GB_Section title="Who does the work">
        {spec.kind === "tee" ? (
          <div className="col" style={{ gap: 5 }}>
            {candidates.map((n) => {
              const on = (spec.target_node_ids || []).indexOf(n.id) >= 0;
              return (
                <label key={n.id} className="row" style={{ gap: 8, alignItems: "center", fontSize: "var(--fs-12)", cursor: "pointer" }}>
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={readOnly}
                    onChange={() => setSpec({
                      target_node_ids: on
                        ? (spec.target_node_ids || []).filter((t) => t !== n.id)
                        : [...(spec.target_node_ids || []), n.id],
                    })}
                  />
                  <span>{label(n.id)}</span>
                </label>
              );
            })}
          </div>
        ) : (
          <select
            value={spec.target_node_id || ""}
            disabled={readOnly}
            onChange={(e) => setSpec({ target_node_id: e.target.value })}
            style={{ padding: "6px 9px", borderRadius: 7, background: "var(--bg-1)", border: "1px solid var(--border)", color: "var(--text)", fontSize: "var(--fs-11)" }}
          >
            <option value="">which step…</option>
            {candidates.map((n) => <option key={n.id} value={n.id}>{label(n.id)}</option>)}
          </select>
        )}
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
          Chosen here, not by drawing an arrow - the canvas shows the result as a bracket around the copies.
        </span>
      </GB_Section>

      <GB_Section title="If one copy fails">
        <div className="row" style={{ gap: 6 }}>
          {FAILS.map((f) => {
            const on = (spec.on_failure || "fail_fast") === f.id;
            return (
              <span
                key={f.id}
                onClick={() => !readOnly && setSpec({ on_failure: f.id })}
                style={{
                  flex: 1, padding: "9px 8px", borderRadius: 8, textAlign: "center", cursor: "pointer",
                  fontSize: "var(--fs-11)", fontWeight: on ? 500 : 400,
                  background: on ? "var(--accent-dim)" : "var(--bg-elev)",
                  border: `1px solid ${on ? "var(--accent-border)" : "var(--border)"}`,
                  color: on ? "var(--accent)" : "var(--text-2)",
                }}
              >
                {f.label}
              </span>
            );
          })}
        </div>
      </GB_Section>
    </>
  );
}

Object.assign(window, { GB_Inspector, GB_Section, GB_FanOutBody, GB_ToolArguments });
