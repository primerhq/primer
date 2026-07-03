/* global React, Modal, Btn, Icon */
// ---------------------------------------------------------------------------
// SharedNewSessionForm (FD2) — the ONE create-session form.
//
// This is the SUPERSET of the two forms it replaces:
//   * app.jsx        NewSessionModal   (modal chrome, workspace picker,
//                                        graph Begin.input_schema support)
//   * studio-sidebar NewSessionForm    (inline overlay chrome, fixed wid,
//                                        optional session `name` — bug #22)
// Both diverged (only the modal understood a graph's input_schema); this
// unifies the field set + submit logic so every call site gets the full
// feature set. Two thin wrappers (NewSessionModal / NewSessionForm) keep
// each site's outer chrome and just render this.
//
// Props:
//   wid        (string?)   — fixed workspace. When omitted, a workspace
//                            selector is shown (the app-modal flow).
//   variant    (accepted for back-compat; the form now ALWAYS renders as a
//               comfortably-sized centered Modal — the old "inline" positioned
//               overlay used by the Studio sidebar was enlarged into this modal
//               so a detailed prompt can be pasted into the big instructions box.)
//   onCreated  (fn)        — called with the created session on success.
//   onCancel   (fn)        — cancel / close.
//   pushToast  (fn?)       — toast enqueuer; falls back to primerApi.toastPush.
//
// POST /workspaces/{wid}/sessions  body:
//   { binding: { kind, agent_id? | graph_id? }, auto_start,
//     name?, graph_input? | initial_instructions? }
//
// No-build rules: top-level `function`/`var`; unconditional hooks in fixed
// order; exported as window.SharedNewSessionForm. Unique top-level names
// (SharedNewSessionForm / SharedNewSessionSchemaField) so the flat-bundle
// dup-decl lint (FD3) stays green.
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
    var long = schema && typeof schema.maxLength === "number" && schema.maxLength >= 200;
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

  return (
    <div className="field">
      <label className="field-label">{label}</label>
      {control}
      {help && <div className="field-help">{help}</div>}
    </div>
  );
}

function SharedNewSessionForm(props) {
  var fixedWid = props.wid || null;
  var onCreated = props.onCreated || function () {};
  var onCancel = props.onCancel || function () {};
  var pushToast =
    props.pushToast || (window.primerApi && window.primerApi.toastPush) || null;

  var api = window.primerApi || {};
  var useResource = api.useResource;
  var useMutation = api.useMutation;
  var apiFetch = api.apiFetch;

  var agents = useResource(
    "shared-new-session:agents",
    function (signal) { return apiFetch("GET", "/agents?limit=200", null, { signal }); },
    { pollMs: 0 }
  );
  var graphs = useResource(
    "shared-new-session:graphs",
    function (signal) { return apiFetch("GET", "/graphs?limit=200", null, { signal }); },
    { pollMs: 0 }
  );
  // Always call the hook (Rules of Hooks); only consumed when there is no
  // fixed wid, i.e. the modal flow where the user picks a workspace.
  var workspacesRes = useResource(
    "shared-new-session:workspaces",
    function (signal) { return apiFetch("GET", "/workspaces?limit=200", null, { signal }); },
    { pollMs: 0 }
  );

  var agentItems = (agents.data && agents.data.items) ? agents.data.items : [];
  var graphItems = (graphs.data && graphs.data.items) ? graphs.data.items : [];
  var workspaceItems = (workspacesRes.data && workspacesRes.data.items) ? workspacesRes.data.items : [];

  var [kind, setKind] = React.useState("agent");
  var [agentId, setAgentId] = React.useState("");
  var [graphId, setGraphId] = React.useState("");
  var [workspaceId, setWorkspaceId] = React.useState("");
  var [name, setName] = React.useState("");
  var [instructions, setInstructions] = React.useState("");
  var [autoStart, setAutoStart] = React.useState(true);
  // Dynamic Begin.input_schema form state for the graph binding, keyed by
  // property name. Reset whenever the selected graph (or kind) changes.
  var [graphInputDraft, setGraphInputDraft] = React.useState({});

  // Look up the selected graph + its Begin node to drive the dynamic form.
  var selectedGraph = graphItems.find(function (g) { return g.id === graphId; }) || null;
  var beginNode = ((selectedGraph && selectedGraph.nodes) || []).find(function (n) {
    return n.kind === "begin";
  }) || null;
  var inputSchema = (beginNode && beginNode.input_schema) || null;
  var hasObjectSchema =
    !!inputSchema
    && inputSchema.type === "object"
    && inputSchema.properties
    && typeof inputSchema.properties === "object";
  var schemaPropertyKeys = hasObjectSchema ? Object.keys(inputSchema.properties) : [];

  // Single owner per selection: default the first available option when items
  // load OR the kind toggles to an as-yet-unselected binding.
  React.useEffect(function () {
    if (kind === "agent" && agentItems.length > 0 && !agentId) setAgentId(agentItems[0].id);
  }, [agentItems, kind]);
  React.useEffect(function () {
    if (kind === "graph" && graphItems.length > 0 && !graphId) setGraphId(graphItems[0].id);
  }, [graphItems, kind]);
  React.useEffect(function () {
    if (!fixedWid && workspaceItems.length > 0 && !workspaceId) setWorkspaceId(workspaceItems[0].id);
  }, [workspaceItems]);
  React.useEffect(function () {
    setGraphInputDraft({});
  }, [graphId, kind]);

  var effectiveWid = fixedWid || workspaceId;
  var create = useMutation(
    function (body) {
      var url = "/workspaces/" + encodeURIComponent(effectiveWid) + "/sessions";
      return apiFetch("POST", url, body);
    },
    { invalidates: ["sessions", "workspace-sessions:" + effectiveWid] }
  );

  // Ref-gate the submit so a rapid double-click can't queue two POSTs before
  // React flips the disabled flag, and a mid-flight re-render can't re-arm it.
  var submittingRef = React.useRef(false);

  var loading = agents.loading || graphs.loading || (!fixedWid && workspacesRes.loading);
  var noWorkspaces = !fixedWid && !loading && workspaceItems.length === 0;
  var noBinding =
    !loading && (kind === "agent" ? agentItems.length === 0 : graphItems.length === 0);

  // For graph bindings with an object input_schema, the dynamic form replaces
  // the free-text instructions field.
  var usesGraphInputForm = kind === "graph" && hasObjectSchema;
  var canSubmit =
    !loading
    && !create.loading
    && effectiveWid
    && (kind === "agent" ? !!agentId : !!graphId);

  async function onSubmit(e) {
    if (e && e.preventDefault) e.preventDefault();
    if (!canSubmit || submittingRef.current) return;
    submittingRef.current = true;
    var binding = kind === "agent"
      ? { kind: "agent", agent_id: agentId }
      : { kind: "graph", graph_id: graphId };
    var body = { binding: binding, auto_start: autoStart };
    if (name.trim()) body.name = name.trim();
    if (usesGraphInputForm) {
      // Submit the schema-driven object as `graph_input`; the server validates
      // against Begin.input_schema at session-create time.
      body.graph_input = graphInputDraft;
    } else if (instructions.trim()) {
      body.initial_instructions = instructions.trim();
    }
    try {
      var session = await create.mutate(body);
      onCreated(session);
    } catch (_err) {
      // useMutation already surfaced an error toast; re-arm for another try.
      submittingRef.current = false;
    }
  }

  var createErr = create.error;
  var errText = createErr
    ? (createErr.detail || createErr.message || "Failed to create session")
    : null;

  // Shared field body — identical markup for both variants (design-system
  // classes); only the outer chrome + action buttons differ.
  var fields = (
    <>
      <div className="field">
        <label className="field-label">Name</label>
        <input
          data-testid="new-session-name"
          type="text"
          className="input"
          placeholder="Optional — defaults to the session id"
          value={name}
          onChange={function (e) { setName(e.target.value); }}
          style={{ width: "100%" }}
        />
      </div>
      <div className="field">
        <label className="field-label">Binding</label>
        <div className="chip-group" style={{ display: "inline-flex" }}>
          <span className={"chip " + (kind === "agent" ? "active" : "")} onClick={function () { setKind("agent"); }}>agent</span>
          <span className={"chip " + (kind === "graph" ? "active" : "")} onClick={function () { setKind("graph"); }}>graph</span>
        </div>
      </div>
      <div className="field">
        <label className="field-label">{kind === "agent" ? "Agent" : "Graph"}</label>
        {kind === "agent" ? (
          <select
            className="select"
            value={agentId}
            onChange={function (e) { setAgentId(e.target.value); }}
            style={{ width: "100%" }}
            disabled={loading || agentItems.length === 0}
          >
            {agentItems.length === 0 && (
              <option value="">{loading ? "Loading…" : "No agents available"}</option>
            )}
            {agentItems.map(function (a) { return <option key={a.id} value={a.id}>{a.id}</option>; })}
          </select>
        ) : (
          <select
            className="select"
            value={graphId}
            onChange={function (e) { setGraphId(e.target.value); }}
            style={{ width: "100%" }}
            disabled={loading || graphItems.length === 0}
          >
            {graphItems.length === 0 && (
              <option value="">{loading ? "Loading…" : "No graphs available"}</option>
            )}
            {graphItems.map(function (g) { return <option key={g.id} value={g.id}>{g.id}</option>; })}
          </select>
        )}
        {noBinding && (
          <div className="field-help warn">
            <Icon name="alert" size={11} />{" "}
            No {kind === "agent" ? "agents" : "graphs"} are defined yet — create one first.
          </div>
        )}
      </div>
      {!fixedWid && (
        <div className="field">
          <label className="field-label">Workspace</label>
          <select
            className="select"
            value={workspaceId}
            onChange={function (e) { setWorkspaceId(e.target.value); }}
            style={{ width: "100%" }}
            disabled={loading || workspaceItems.length === 0}
          >
            {workspaceItems.length === 0 && (
              <option value="">{loading ? "Loading…" : "No workspaces available"}</option>
            )}
            {workspaceItems.map(function (w) { return <option key={w.id} value={w.id}>{w.id}</option>; })}
          </select>
          {noWorkspaces && (
            <div className="field-help warn">
              <Icon name="alert" size={11} /> No workspaces yet — create one before starting a session.
            </div>
          )}
        </div>
      )}
      {usesGraphInputForm ? (
        // Schema-driven form for graph bindings whose Begin node declares an
        // object input_schema. One field per property, packaged into
        // `graph_input` on submit.
        schemaPropertyKeys.map(function (key) {
          return (
            <SharedNewSessionSchemaField
              key={key}
              propKey={key}
              schema={inputSchema.properties[key] || {}}
              value={graphInputDraft[key]}
              onChange={function (v) {
                var next = Object.assign({}, graphInputDraft);
                next[key] = v;
                setGraphInputDraft(next);
              }}
            />
          );
        })
      ) : (
        <div className="field">
          <label className="field-label">Initial instructions</label>
          <textarea
            data-testid="new-session-instructions"
            className="textarea"
            value={instructions}
            onChange={function (e) { setInstructions(e.target.value); }}
            rows={8}
            style={{ minHeight: 150, resize: "vertical" }}
            placeholder="Tell the agent what to do — paste a detailed prompt here…"
          />
        </div>
      )}
      <div className="field">
        <label style={{ display: "inline-flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={autoStart}
            onChange={function (e) { setAutoStart(e.target.checked); }}
          />
          <span>Start immediately</span>
        </label>
      </div>
      {errText && (
        <div className="field-help warn" style={{ marginTop: 2 }}>
          <Icon name="alert" size={11} /> {errText}
        </div>
      )}
    </>
  );

  // Enlarged, centered Modal chrome for BOTH call sites — the Studio sidebar's
  // "+" / ⌘K palette "New session" (formerly a small positioned overlay) and
  // the app-level global "New session" dialog. Comfortably wide, with a large
  // multi-line Initial instructions box so a detailed prompt can be pasted.
  // Escape / backdrop-click / Cancel all close via the shared Modal.
  return (
    <Modal
      title="New session"
      width="min(94vw, 640px)"
      onClose={onCancel}
      footer={
        <>
          <Btn kind="ghost" onClick={onCancel}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={onSubmit} disabled={!canSubmit}>
            {create.loading ? "Creating…" : "Create"}
          </Btn>
        </>
      }
    >
      <div data-testid="new-session-form">
        {fields}
      </div>
    </Modal>
  );
}

window.SharedNewSessionForm = SharedNewSessionForm;
window.SharedNewSessionSchemaField = SharedNewSessionSchemaField;
