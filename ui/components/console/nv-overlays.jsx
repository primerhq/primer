/* global React, SH_api, NV_useConsole, NV_identity,
   SharedNewSessionSchemaField, FormField */
// Overlays (wiring plan P3 T9). Three tiers behind one URL-addressed
// host:
//
//   new-session    - the designer's create-session panel, ported: one
//                    combined agent+graph binding picker, instructions,
//                    an Advanced fold for autonomy, and the graph
//                    Begin.input_schema form where a graph declares one.
//   new-workspace  - the kit form overlay that makes a workspace from a
//                    template: a searchable template dropdown, name and
//                    id, its size, extra env, files and start-up commands.
//   everything else- the existing management surfaces (NV_OVERLAY_MOUNTS)
//                    re-hosted inside an nv panel via a thin shell
//                    adapter. They keep their handlers and data logic and
//                    lose their chrome; the prototype itself stubs these
//                    panels, and P4's card pages take over their entry
//                    points one by one.
//
// The overlay is URL state (overlay=<name>[:<section>[:<id>]]), so every
// entry point - palette verb, platform card, pasted link - lands in the
// same place. That is the shared-overlay contract of the design.

// ---------------------------------------------------------------------------
// Panel primitive: scrim + centered panel + header (title, close) +
// scrolling body + optional footer. Esc and a press-and-release on the
// scrim close.
function NV_OverlayPanel(props) {
  var onClose = props.onClose;
  var panelRef = React.useRef(null);
  // A click after a drag from the panel to the scrim targets the scrim, even
  // though the press began inside. Close only when both ends of the gesture
  // land on the scrim so selecting text cannot discard the open surface.
  var scrimDownRef = React.useRef(false);
  var titleId = React.useId();
  // Console review C-028: focus moves into the overlay (the first field or control of its body, not the close button), Tab cycles inside
  // it, and Escape or Close gives focus back to whatever opened it (foundation/focus-trap.js, the same trap Modal uses).
  window.primerApi.useFocusTrap(panelRef, true, {
    initial: function (node) {
      var body = node.querySelector(".nv-overlay-body");
      var items = body ? window.primerApi.focusablesOf(body) : [];
      return items[0] || null;
    },
  });
  // Escape closes the top-most layer only (foundation/escape-stack.js): a dialog drawn over this overlay answers it first, and this overlay the next one.
  window.primerApi.useEscape(function () { onClose(); });
  return (
    <div className="nv-scrim" data-testid="nv-overlay-scrim"
      onPointerDown={function (ev) { scrimDownRef.current = ev.target === ev.currentTarget; }}
      onClick={function (ev) {
        if (ev.target === ev.currentTarget && scrimDownRef.current) onClose();
      }}>
      <div className="nv-overlay-panel" role="dialog" aria-modal="true" aria-labelledby={titleId}
        tabIndex={-1} ref={panelRef}
        data-wide={props.wide ? "true" : "false"}
        data-testid={props.testid || "nv-overlay-panel"}
        onClick={function (ev) { ev.stopPropagation(); }}>
        <div className="nv-overlay-head">
          {/* THE one title (one-title rule), and the page's h1: the
              re-hosted pages render action bars, never headings, and
              the e2e suite addresses surfaces by h1.page-title. tabIndex -1:
              a triggers row that refetches itself away lands focus here, the
              surface's one heading (ticket 01a12144). */}
          <h1 className="nv-overlay-title page-title" id={titleId} tabIndex={-1}
            data-testid="nv-overlay-title">{props.title}</h1>
          <span style={{ flex: 1 }} />
          <button type="button" className="nv-overlay-close"
            data-testid="nv-overlay-close" title="Close (Esc)"
            onClick={function () { onClose(); }}>×</button>
        </div>
        <div className="nv-overlay-body" data-testid="nv-overlay-body">
          {props.children}
        </div>
        {props.footer ? (
          <div className="nv-overlay-foot">{props.footer}</div>
        ) : null}
      </div>
    </div>
  );
}

// Field primitive: label + optional hint above any control. FormField (shared/form-field.jsx) with the overlays' class names, so the label is a <label> that points at the
// field's control (a field around a custom widget is a group named by its label); it was a div that named nothing.
function NV_Field(props) {
  return <FormField {...props} className="nv-field" labelClassName="nv-field-label" hintClassName="nv-field-hint" />;
}

// The workspace a create dialog acts on (the selected one): its name, else its id, else null when none is selected. Named in the Create
// session dialog because the dialog never said where the session would go.
function NV_workspaceLabel(workspaces, wid) {
  if (!wid) return null;
  var list = Array.isArray(workspaces) ? workspaces : [];
  var ws = list.find(function (w) { return w && w.id === wid; });
  return (ws && ws.name) || wid;
}

function NV_errText(err) {
  if (!err) return null;
  return err.detail || err.message || "Request failed";
}

// ---------------------------------------------------------------------------
// Create session. Submit-body semantics are the create-session contract
// of the form this overlay replaced, verbatim: omitting binding asks for the system default agent;
// a graph with an object Begin.input_schema submits graph_input instead
// of initial_instructions; auto_start follows typed instructions until
// the operator states a preference.
function NV_CreateSessionOverlay() {
  var con = NV_useConsole();
  var apiFetch = window.primerApi.apiFetch;
  var agents = window.primerApi.useResource(
    "nv-ov:agents",
    function (signal) { return SH_api.agents(signal); },
    { pollMs: 0 }
  );
  var graphs = window.primerApi.useResource(
    "nv-ov:graphs",
    function (signal) { return SH_api.graphs(signal); },
    { pollMs: 0 }
  );
  var bindState = React.useState(null); // null = default agent
  var bind = bindState[0];
  var setBind = bindState[1];
  var menuState = React.useState(false);
  var menuOpen = menuState[0];
  var setMenuOpen = menuState[1];
  // The binding menu is a layer of the Escape stack while it is open: Escape closes the menu, and the overlay under it only on the next one.
  window.primerApi.useEscape(function () { setMenuOpen(false); }, menuOpen);
  var qState = React.useState("");
  var q = qState[0];
  var setQ = qState[1];
  var nameState = React.useState("");
  var name = nameState[0];
  var setName = nameState[1];
  var instrState = React.useState("");
  var instr = instrState[0];
  var setInstr = instrState[1];
  var advState = React.useState(false);
  var advOpen = advState[0];
  var setAdv = advState[1];
  var autoState = React.useState(false);
  var autoStart = autoState[0];
  var setAutoStart = autoState[1];
  var autoTouched = React.useRef(false);
  var busyState = React.useState(false);
  var busy = busyState[0];
  var setBusy = busyState[1];
  var errState = React.useState(null);
  var err = errState[0];
  var setErr = errState[1];
  var graphDraftState = React.useState({});
  var graphDraft = graphDraftState[0];
  var setGraphDraft = graphDraftState[1];

  // Typing instructions implies intent to run (the create-session rule
  // since the first form): the toggle follows along until the operator touches it.
  React.useEffect(function () {
    if (autoTouched.current) return;
    setAutoStart(instr.trim().length > 0);
  }, [instr]);

  // The selected graph's full detail drives the Begin.input_schema form.
  var gid = bind && bind.kind === "graph" ? bind.id : "";
  var graphDetail = window.primerApi.useResource(
    "nv-ov:graph-detail:" + gid,
    function (signal) {
      if (!gid) return Promise.resolve(null);
      return apiFetch("GET", "/graphs/" + encodeURIComponent(gid), null,
        { signal: signal });
    },
    { pollMs: 0, deps: [gid] }
  );
  React.useEffect(function () { setGraphDraft({}); }, [gid]);

  var agentItems = (agents.data && agents.data.items) || [];
  var graphItems = (graphs.data && graphs.data.items) || [];
  var rows = [];
  agentItems.forEach(function (a) {
    rows.push({
      kind: "agent", id: a.id,
      desc: a.description || a.model_profile_id || "",
    });
  });
  graphItems.forEach(function (g) {
    rows.push({ kind: "graph", id: g.id, desc: g.description || "No description" });
  });
  var ql = q.trim().toLowerCase();
  var visible = !ql ? rows : rows.filter(function (r) {
    return (r.id + " " + r.desc).toLowerCase().indexOf(ql) >= 0;
  });

  var beginNode = ((graphDetail.data && graphDetail.data.nodes) || [])
    .find(function (n) { return n.kind === "begin"; }) || null;
  var inputSchema = (beginNode && beginNode.input_schema) || null;
  var hasObjectSchema = !!inputSchema && inputSchema.type === "object"
    && inputSchema.properties && typeof inputSchema.properties === "object";
  var schemaKeys = hasObjectSchema ? Object.keys(inputSchema.properties) : [];
  var usesGraphInput = !!gid && hasObjectSchema;

  var bindIdent = NV_identity(bind
    ? (bind.kind === "graph"
      ? { kind: "graph", graph_id: bind.id }
      : { kind: "agent", agent_id: bind.id })
    : null);
  var bindName = bind ? bind.id : "Default agent";
  var bindKind = bind ? bind.kind : "agent";
  var canSubmit = !busy && con.wid && (!bind || bind.kind !== "graph" || bind.id);

  function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setErr(null);
    var body = { auto_start: autoStart };
    // Omitting binding entirely asks for the system default agent;
    // {kind:"agent", agent_id:null} would name an agent called null.
    var binding = bind
      ? (bind.kind === "graph"
        ? { kind: "graph", graph_id: bind.id }
        : { kind: "agent", agent_id: bind.id })
      : null;
    if (binding) body.binding = binding;
    var trimmedName = name.trim() || null;
    if (trimmedName) body.name = trimmedName;
    if (usesGraphInput) body.graph_input = graphDraft;
    else if (instr.trim()) body.initial_instructions = instr.trim();
    apiFetch(
      "POST",
      "/workspaces/" + encodeURIComponent(con.wid) + "/sessions",
      body
    ).then(function (row) {
      var sid = row && (row.session_id || row.id);
      con.closeOverlay();
      con.toast("Session created" + (sid ? ": " + sid : ""));
      if (sid) {
        con.setDoc({ kind: "session", ref: sid });
        if (con.promoteDoc) con.promoteDoc("session:" + sid);
        // F1/F10 (2026-08-29 UI review): stamp the wid, name and binding
        // we already know (this overlay always creates in con.wid, from
        // exactly what was just submitted) so the new tab's label, glyph
        // and pulse don't sit blind/default until the 5s poll catches up.
        if (con.stampSessionMeta) {
          con.stampSessionMeta(sid, {
            wid: con.wid, name: trimmedName, binding: binding,
          });
        }
      }
    }, function (e) {
      setBusy(false);
      setErr(e);
    });
  }

  return (
    <NV_OverlayPanel title="Create session"
      testid="nv-overlay:new-session" onClose={con.closeOverlay}
      footer={(
        <React.Fragment>
          <button type="button" className="nv-btn-secondary"
            onClick={con.closeOverlay}>Cancel</button>
          <button type="button" className="nv-btn-primary"
            data-testid="nv-ns-create" disabled={!canSubmit}
            onClick={submit}>
            {busy ? "Creating…" : "Create session"}
          </button>
        </React.Fragment>
      )}>
      {err ? (
        <div className="nv-form-error" data-testid="nv-ns-error">
          {NV_errText(err)}
        </div>
      ) : null}
      <div className="nv-ns-workspace" data-testid="nv-ns-workspace">
        {NV_workspaceLabel(con.workspaces, con.wid)
          ? "Creating in workspace " + NV_workspaceLabel(con.workspaces, con.wid)
          : "Select a workspace first"}
      </div>
      <NV_Field label="Bind to an agent or a graph">
        <div className="nv-bind-picker">
          <button type="button" className="nv-bind-btn"
            data-testid="nv-ns-bind"
            onClick={function (ev) {
              ev.stopPropagation();
              setMenuOpen(function (v) { return !v; });
            }}>
            <svg width="13" height="13" viewBox="0 0 12 12"
              style={{ color: bindIdent.color, flexShrink: 0 }}>
              <path d={bindIdent.d} fill="currentColor" />
            </svg>
            <span className="nv-bind-name">{bindName}</span>
            <span className="nv-kind-tag">{bindKind}</span>
            <svg width="9" height="9" viewBox="0 0 10 10" fill="none"
              stroke="currentColor" strokeWidth="1.5">
              <path d="M2 3.5 5 6.5 8 3.5" />
            </svg>
          </button>
          {menuOpen ? (
            <div className="nv-bind-menu" data-testid="nv-ns-bind-menu"
              onClick={function (ev) { ev.stopPropagation(); }}>
              <input className="nv-input nv-bind-search" autoFocus
                value={q} placeholder="Type to search agents & graphs…"
                onChange={function (ev) { setQ(ev.target.value); }} />
              <div className="nv-bind-rows">
                <div className="nv-bind-row"
                  data-testid="nv-ns-bind-default"
                  onClick={function () {
                    setBind(null);
                    setMenuOpen(false);
                  }}>
                  <svg width="12" height="12" viewBox="0 0 12 12"
                    style={{ color: NV_identity(null).color }}>
                    <path d={NV_identity(null).d} fill="currentColor" />
                  </svg>
                  <span className="nv-bind-name">Default agent</span>
                  <span className="nv-kind-tag">agent</span>
                </div>
                {visible.map(function (r) {
                  var ident = NV_identity(r.kind === "graph"
                    ? { kind: "graph", graph_id: r.id }
                    : { kind: "agent", agent_id: r.id });
                  return (
                    <div key={r.kind + ":" + r.id} className="nv-bind-row"
                      data-testid={"nv-ns-bind:" + r.id}
                      onClick={function () {
                        setBind({ kind: r.kind, id: r.id });
                        setMenuOpen(false);
                      }}>
                      <svg width="12" height="12" viewBox="0 0 12 12"
                        style={{ color: ident.color }}>
                        <path d={ident.d} fill="currentColor" />
                      </svg>
                      <span className="nv-bind-name">{r.id}</span>
                      {r.desc ? (
                        <span className="nv-bind-desc">{r.desc}</span>
                      ) : null}
                      <span className="nv-kind-tag">{r.kind}</span>
                    </div>
                  );
                })}
                {!visible.length ? (
                  <div className="nv-bind-empty">
                    {window.NV_emptyText(
                      ["agent or graph", "agents or graphs"], q, rows.length)}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
      </NV_Field>
      <NV_Field label="Name" hint="optional">
        <input className="nv-input" data-testid="nv-ns-name" value={name}
          placeholder="e.g. Wire the payments webhook"
          onChange={function (ev) { setName(ev.target.value); }} />
      </NV_Field>
      {usesGraphInput ? (
        <NV_Field label="Graph input"
          hint="from the graph's Begin input schema">
          {schemaKeys.map(function (key) {
            return (
              <SharedNewSessionSchemaField key={key} propKey={key}
                schema={inputSchema.properties[key]}
                value={graphDraft[key]}
                onChange={function (val) {
                  setGraphDraft(function (prev) {
                    var next = {};
                    Object.keys(prev).forEach(function (k) { next[k] = prev[k]; });
                    next[key] = val;
                    return next;
                  });
                }} />
            );
          })}
        </NV_Field>
      ) : (
        <NV_Field label="Initial instructions"
          help="To attach files, create the session parked and attach them to your first message.">
          <textarea className="nv-textarea" data-testid="nv-ns-instr"
            rows={4} value={instr}
            placeholder="What should this session do first?"
            onChange={function (ev) { setInstr(ev.target.value); }} />
        </NV_Field>
      )}
      <button type="button" className="nv-adv-toggle"
        data-open={advOpen ? "true" : "false"} data-testid="nv-ns-adv"
        onClick={function () { setAdv(function (v) { return !v; }); }}>
        <svg width="8" height="8" viewBox="0 0 10 10" fill="none"
          stroke="currentColor" strokeWidth="1.4">
          <path d="M3.5 2 6.5 5 3.5 8" />
        </svg>
        Advanced
      </button>
      {advOpen ? (
        <div className="nv-adv-body">
          <NV_Field label="Autonomy"
            hint="start on create, or park until you send the first turn">
            <div className="nv-seg" role="group">
              <button type="button" className="nv-seg-btn"
                data-active={autoStart ? "true" : "false"}
                onClick={function () {
                  autoTouched.current = true;
                  setAutoStart(true);
                }}>Start immediately</button>
              <button type="button" className="nv-seg-btn"
                data-active={!autoStart ? "true" : "false"}
                onClick={function () {
                  autoTouched.current = true;
                  setAutoStart(false);
                }}>Create parked</button>
            </div>
          </NV_Field>
        </div>
      ) : null}
    </NV_OverlayPanel>
  );
}

// ---- NV_nw pure helpers (tests/ui/test_nv_new_workspace_helpers.py) ----
// The new-workspace dialog (platform revamp Plan 08, Task 12; design 14.3) is built on these: no JSX and no React, so the test runs them in MiniRacer
// next to the template helpers they use (WT_* in workspaces/templates.jsx). They decide the id rule, which size fields each provider kind offers and
// what they are pre-filled with, the body POST /v1/workspaces is sent (only the sizes the person changed, and only those the provider can take, so a
// pre-filled size in other units is never rewritten), the dialog's own errors, and the field a refusal's loc names.

// The server's one rule for a caller's workspace id (primer/model/workspace.py WORKSPACE_ID_PATTERN).
var NV_NW_ID_PATTERN = "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$";
var NV_NW_ID_RE = new RegExp(NV_NW_ID_PATTERN);

// Null for a blank id (the server makes one) or one the server takes, else the sentence that says the rule.
function NV_nwIdError(text) {
  var s = WT_trim(text);
  if (!s || NV_NW_ID_RE.test(s)) return null;
  return "Use lowercase letters, digits and dashes, starting and ending with a letter or digit (at most 63).";
}

// The size fields each provider kind takes for one workspace (primer/workspace/size_overrides.py): Kubernetes all three, Docker no disk, Local none.
var NV_NW_SIZE_FIELDS = { kubernetes: ["cpu", "memoryGiB", "diskGiB"], container: ["cpu", "memoryGiB"], local: [] };

function NV_nwSizeFields(kind) {
  return WT_has(NV_NW_SIZE_FIELDS, kind) ? NV_NW_SIZE_FIELDS[kind].slice() : [];
}

// The size fields as the template fills them in, as text ("" where the template sets none).
function NV_nwInitialSize(t) {
  var size = WT_templateSize(t);
  return {
    cpu: size.cpu != null ? String(size.cpu) : "",
    memoryGiB: size.memoryGiB != null ? WT_formatGiB(size.memoryGiB) : "",
    diskGiB: size.diskGiB != null ? WT_formatGiB(size.diskGiB) : (size.diskRaw || ""),
  };
}

// The fields of a size the dialog sends: the ones the person changed (sizeDirty), left non-empty, and that the provider takes.
function NV_nwSentSizes(state) {
  var dirty = state.sizeDirty || {};
  var size = state.size || {};
  return NV_nwSizeFields(state.kind).filter(function (key) { return !!dirty[key] && WT_trim(size[key]) !== ""; });
}

// The body of POST /v1/workspaces: {template_id, name?, id?, overrides?}. state = {templateId, kind, name, id, size, sizeDirty, envRows, files, init}.
// An env row with no name, a file with no path and a blank command line are left out, and so is an empty overrides.
function NV_nwBody(state) {
  var body = { template_id: state.templateId };
  var name = WT_trim(state.name);
  if (name) body.name = name;
  var id = WT_trim(state.id);
  if (id) body.id = id;
  var overrides = {};
  var env = {};
  var named = 0;
  (state.envRows || []).forEach(function (r) {
    var k = WT_trim(r && r.key);
    if (!k) return;
    env[k] = r.value == null ? "" : String(r.value);
    named += 1;
  });
  if (named) overrides.env = env;
  var files = (state.files || []).filter(function (f) { return f && WT_trim(f.path); }).map(WT_copy);
  if (files.length) overrides.files = files;
  var init = WT_lines(state.init);
  if (init.length) overrides.init_commands = init;
  var resources = {};
  NV_nwSentSizes(state).forEach(function (key) {
    var size = state.size[key];
    if (key === "cpu") resources.cpu_cores = WT_positiveNumber(size);
    else if (key === "memoryGiB") resources.memory_bytes = WT_giBToBytes(size);
    else if (key === "diskGiB") resources.disk_bytes = WT_giBToBytes(size);
  });
  if (Object.keys(resources).length) overrides.resources = resources;
  if (Object.keys(overrides).length) body.overrides = overrides;
  return body;
}

// The dialog's own errors, by field key (empty when it may be sent): the id, each size the person changed (positive, and no more than a workspace may
// have), a name used twice in the extra environment, and a file mode that is not octal. A pre-filled size nobody changed is not checked: it is not sent.
function NV_nwErrors(state) {
  var errs = {};
  var idError = NV_nwIdError(state.id);
  if (idError) errs.id = idError;
  NV_nwSentSizes(state).forEach(function (key) {
    var text = state.size[key];
    var problem = key === "cpu" ? WT_sizeError(text, WT_MAX_CPU, "At most 1024 CPU cores")
      : WT_giBToBytes(text) == null ? "Enter a positive number" : WT_sizeError(text, WT_MAX_GIB, "At most 65536 GiB (64 TiB)");
    if (problem) errs[key] = problem;
  });
  if (WT_twice(state.envRows)) errs.envRows = "Each name once";
  if ((state.files || []).some(function (f) { return f && WT_trim(f.mode) && !/^[0-7]{3,4}$/.test(WT_trim(f.mode)); })) {
    errs.files = "Use an octal mode such as 0644";
  }
  return errs;
}

// Where each body path is shown: the dialog's field key whose control writes it.
var NV_NW_PATHS = [
  ["id", "id"], ["name", "name"], ["template_id", "template"],
  ["overrides.resources.cpu_cores", "cpu"], ["overrides.resources.memory_bytes", "memoryGiB"], ["overrides.resources.disk_bytes", "diskGiB"],
  ["overrides.env", "envRows"], ["overrides.files", "files"], ["overrides.init_commands", "init"],
];

// The field key a refusal's loc names, or null when the dialog draws no such field (a size the provider's kind does not show, Docker's disk say: the
// refusal then goes to the one line above the footer).
function NV_nwFieldKey(loc, kind) {
  var parts = WT_cleanLoc(loc);
  var hit = null;
  NV_NW_PATHS.forEach(function (entry) {
    var path = entry[0].split(".");
    if (hit || path.length > parts.length) return;
    for (var i = 0; i < path.length; i++) if (path[i] !== parts[i]) return;
    hit = entry[1];
  });
  if (hit && ["cpu", "memoryGiB", "diskGiB"].indexOf(hit) >= 0 && NV_nwSizeFields(kind).indexOf(hit) < 0) return null;
  return hit;
}

Object.assign(window, { NV_NW_ID_PATTERN, NV_nwIdError, NV_nwSizeFields, NV_nwInitialSize, NV_nwBody, NV_nwErrors, NV_nwFieldKey });
// ---- end NV_nw pure helpers ----

// ---------------------------------------------------------------------------
// New workspace (platform revamp Plan 08, Task 13; design 14.3): a kit form overlay. "From template" at the centre top is the kit selector, searchable,
// over EVERY template (WT_fetchAllTemplates, a page of 200 at a time); the template's summary line under it; Name and Id; a SIZE section for a provider
// that takes one, filled in from the template; EXTRA ENVIRONMENT; and folds for extra files and extra start-up commands. Create sends NV_nwBody to
// POST /v1/workspaces: a size goes only when the person changed it. A refusal lands under the field its loc names, and anything the dialog has no field
// for in the one line above the footer (NV_nwFieldKey, WT_splitErrors). With no template yet, "Create a template" opens the template overlay over the
// dialog and hands the new template back, selected: nobody is sent to another page and left to find the way back (lead ruling 2 of slice 5).
var NV_NW_FOLDS = ["files", "init"];

function NV_CreateWorkspaceOverlay() {
  var con = NV_useConsole();
  var api = window.primerApi;
  var apiFetch = api.apiFetch;
  var isAdmin = con.role === "admin";
  var templates = api.useResource("nv-ov:workspace-templates", function (signal) {
    return WT_fetchAllTemplates(apiFetch, signal);
  }, { pollMs: 0 });
  var items = (templates.data && templates.data.items) || [];

  // The picked template, the sizes it filled in (what a size is compared with to know it was changed) and the sizes as they stand now.
  var pickState = React.useState({ id: "", initial: NV_nwInitialSize(null), size: NV_nwInitialSize(null) });
  var picked = pickState[0];
  var setPicked = pickState[1];
  // The template the template overlay handed back: drawn from here until the refreshed list holds it.
  var handedState = React.useState(null);
  var handed = handedState[0];
  var makingState = React.useState(false);
  var making = makingState[0];
  var setMaking = makingState[1];
  var nameState = React.useState("");
  var idState = React.useState("");
  var envState = React.useState([]);
  var filesState = React.useState([]);
  var initState = React.useState("");
  var serverState = React.useState({ fields: {}, general: null });
  var server = serverState[0];
  var setServer = serverState[1];
  // Each fold's open state, and a count that remounts it open when a refusal lands inside it while it is closed.
  var foldState = React.useState({ files: { open: false, gen: 0 }, init: { open: false, gen: 0 } });
  var folds = foldState[0];
  var setFolds = foldState[1];
  var outRef = React.useRef(false);

  var template = items.filter(function (t) { return t && t.id === picked.id; })[0]
    || (handed && handed.id === picked.id ? handed : null);
  var kind = template ? WT_kindOf(template) : "local";

  function pick(t) {
    var initial = NV_nwInitialSize(t);
    setPicked({ id: t.id, initial: initial, size: Object.assign({}, initial) });
    setServer({ fields: {}, general: null });
  }
  // The first template is picked once the list arrives, and again if the picked one has left the list (deleted meanwhile). A template the template
  // overlay handed back is known before the list holds it.
  var known = !!template;
  React.useEffect(function () {
    if (!known && items.length) pick(items[0]);
  }, [items.length, known]);

  function clearServer(key) {
    setServer(function (e) {
      if (!e.fields[key]) return e;
      var fields = Object.assign({}, e.fields);
      delete fields[key];
      return { fields: fields, general: e.general };
    });
  }
  function setSize(key, text) {
    setPicked(function (p) {
      var size = Object.assign({}, p.size);
      size[key] = text;
      return { id: p.id, initial: p.initial, size: size };
    });
    clearServer(key);
  }
  function setField(st, key) {
    return function (v) {
      st[1](v);
      clearServer(key);
    };
  }

  var sizeDirty = {};
  Object.keys(picked.size).forEach(function (k) { if (picked.size[k] !== picked.initial[k]) sizeDirty[k] = true; });
  var state = {
    templateId: picked.id, kind: kind, name: nameState[0], id: idState[0], size: picked.size, sizeDirty: sizeDirty,
    envRows: envState[0], files: filesState[0], init: initState[0],
  };
  var errs = template ? NV_nwErrors(state) : {};
  var ready = !!template && Object.keys(errs).length === 0;
  function fieldError(key) {
    return errs[key] || server.fields[key] || null;
  }

  // A refusal (or an error) inside a closed fold opens it, so no error is drawn where nobody can see it.
  React.useEffect(function () {
    var shut = NV_NW_FOLDS.filter(function (f) { return !folds[f].open && !!fieldError(f); });
    if (!shut.length) return;
    setFolds(function (fs) {
      var next = Object.assign({}, fs);
      shut.forEach(function (f) { next[f] = { open: true, gen: fs[f].gen + 1 }; });
      return next;
    });
  });

  var mutation = api.useMutation(function (body) {
    return apiFetch("POST", "/workspaces", body);
  }, {
    invalidates: ["nv-workspaces"],
    onSuccess: function (row) {
      con.closeOverlay();
      con.toast("Workspace created: " + (row.name || row.id));
      var verb = con.registry.get("workspace.switch");
      if (verb && row.id) verb.run({ wid: row.id });
    },
    onError: function (err) {
      setServer(WT_splitErrors(err, function (loc) { return NV_nwFieldKey(loc, kind); }));
    },
  });
  var busy = !!mutation.loading;

  // One request at a time: a second Create while one is out does nothing (the ref holds before the busy state has rendered).
  function submit() {
    if (!ready || outRef.current) return;
    outRef.current = true;
    setServer({ fields: {}, general: null });
    mutation.mutate(NV_nwBody(state)).then(null, function () {
      outRef.current = false;
    });
  }

  function handBack(row) {
    if (!row || !row.id) return;
    handedState[1](row);
    pick(row);
    templates.refetch();
  }

  var footer = {
    left: <Btn kind="ghost" data-testid="nv-nw-cancel" blocked={busy} onClick={con.closeOverlay}>Cancel</Btn>,
    right: (
      <Btn kind="primary" data-testid="nv-nw-create" disabled={!ready} busy={busy} onClick={submit}>{busy ? "Creating…" : "Create"}</Btn>
    ),
  };

  var options = items.map(function (t) { return { value: t.id, label: t.id, iconPath: WT_KIND_ICONS[WT_kindOf(t)] }; });
  if (handed && !items.some(function (t) { return t && t.id === handed.id; })) {
    options = options.concat([{ value: handed.id, label: handed.id, iconPath: WT_KIND_ICONS[WT_kindOf(handed)] }]);
  }
  var selector = template ? {
    prefix: "From template", value: picked.id, options: options, searchable: true, testid: "nv-nw-template",
    onChange: function (value) {
      var next = items.filter(function (t) { return t && t.id === value; })[0] || (handed && handed.id === value ? handed : null);
      if (next && next.id !== picked.id) pick(next);
    },
  } : null;

  var body;
  if (!template && templates.error && !templates.data) {
    body = (
      <div data-testid="nv-nw-load-error">
        <Banner kind="error" title="Couldn't load workspace templates." detail={api.readRefusal(templates.error).message}
          actions={<Btn size="sm" icon="refresh" data-testid="nv-nw-retry" onClick={templates.refetch}>Retry</Btn>} />
      </div>
    );
  } else if (!template && !templates.data) {
    body = <div className="nv-bind-empty" role="status" data-testid="nv-nw-loading">Loading templates…</div>;
  } else if (!template && !items.length) {
    body = (
      <div className="nv-bind-empty col" data-testid="nv-nw-empty" style={{ gap: 10, alignItems: "center" }}>
        <div>No workspace templates yet.</div>
        <Btn kind="primary" data-testid="nv-nw-create-template" data-verb="template.create" onClick={function () { setMaking(true); }}>Create a template</Btn>
      </div>
    );
  } else if (!template) {
    body = <div className="nv-bind-empty" role="status" data-testid="nv-nw-loading">Loading templates…</div>;
  } else {
    var fields = NV_nwSizeFields(kind);
    var sizeInput = function (key, testid) {
      return (
        <input className="input" data-testid={testid} value={picked.size[key]} inputMode="decimal" placeholder="No limit"
          onChange={function (e) { setSize(key, e.target.value); }} />
      );
    };
    body = (
      <React.Fragment>
        <div className="field-help" data-testid="nv-nw-summary" style={{ textAlign: "center" }}>{WT_summaryLine(template)}</div>
        {fieldError("template") ? (
          <div className="field-help" role="alert" data-testid="nv-nw-template-error" style={{ color: "var(--red)", textAlign: "center" }}>
            {fieldError("template")}
          </div>
        ) : null}
        <PK_Grid cols={2}>
          <PK_Field label="Name" help="Shown in the workspace menu in place of the id" error={fieldError("name")}>
            <input className="input" data-testid="nv-nw-name" value={nameState[0]} onChange={function (e) { setField(nameState, "name")(e.target.value); }} />
          </PK_Field>
          <PK_Field label="Id" help="Made for you if left empty. Lowercase letters, digits and dashes." error={fieldError("id")}>
            <input className="input mono" data-testid="nv-nw-id" value={idState[0]} onChange={function (e) { setField(idState, "id")(e.target.value); }} />
          </PK_Field>
        </PK_Grid>
        {fields.length ? (
          <PK_Section title="SIZE" cols={3} testid="nv-nw-size">
            <PK_Field label="CPU cores" error={fieldError("cpu")}>{sizeInput("cpu", "nv-nw-cpu")}</PK_Field>
            <PK_Field label="Memory (GiB)" error={fieldError("memoryGiB")}>{sizeInput("memoryGiB", "nv-nw-memory")}</PK_Field>
            {fields.indexOf("diskGiB") >= 0 ? (
              <PK_Field label="Disk (GiB)" error={fieldError("diskGiB")}>{sizeInput("diskGiB", "nv-nw-disk")}</PK_Field>
            ) : null}
            <div className="field-help pk-span-full" data-testid="nv-nw-size-help">Filled in from the template. A change applies to this workspace only.</div>
          </PK_Section>
        ) : null}
        <WT_TableWithNotes help="Added to the template's; values are hidden" error={fieldError("envRows")} table={{
          title: "EXTRA ENVIRONMENT", testid: "nv-nw-env", addLabel: "+ Add", rows: envState[0],
          columns: [{ key: "key", label: "Name", input: "text" }, { key: "value", label: "Value", input: "secret" }],
          onChange: setField(envState, "envRows"),
        }} />
        <PK_Fold key={"nv-nw-files-" + folds.files.gen} open={folds.files.open} label="Extra files" testid="nv-nw-files-fold"
          onToggle={function (open) { setFolds(function (fs) { return Object.assign({}, fs, { files: { open: open, gen: fs.files.gen } }); }); }}>
          <WT_FilesEditor value={filesState[0]} isAdmin={isAdmin} testid="nv-nw-files" error={fieldError("files")} onChange={setField(filesState, "files")} />
        </PK_Fold>
        <PK_Fold key={"nv-nw-init-" + folds.init.gen} open={folds.init.open} label="Extra start-up commands" testid="nv-nw-init-fold"
          onToggle={function (open) { setFolds(function (fs) { return Object.assign({}, fs, { init: { open: open, gen: fs.init.gen } }); }); }}>
          <PK_Field label="Commands" help="One per line; they run after the template's own" error={fieldError("init")}>
            <textarea className="input mono" rows={3} data-testid="nv-nw-init" value={initState[0]} style={{ width: "100%" }}
              onChange={function (e) { setField(initState, "init")(e.target.value); }} />
          </PK_Field>
        </PK_Fold>
      </React.Fragment>
    );
  }

  return (
    <React.Fragment>
      <PK_FormOverlay title="New workspace" testid="nv-overlay:new-workspace" onClose={con.closeOverlay} selector={selector} footer={footer}>
        {body}
        {server.general ? <div className="nv-form-error" role="alert" data-testid="nv-nw-error">{server.general}</div> : null}
      </PK_FormOverlay>
      {making ? (
        <window.WT_TemplateOverlay create={true} onSaved={handBack} onClose={function () { setMaking(false); }} />
      ) : null}
    </React.Fragment>
  );
}

// ---------------------------------------------------------------------------
// What a bare address of a list that left its overlay says instead (platform revamp 08, Tasks 14 and 15; design 14.1): overlay=workspaces (the Workspaces
// page is gone: the rail lists the workspaces) and, once the old template list goes, overlay=workspaces:templates (a page of its own). One press closes
// the overlay, onto the templates page for the second.
var NV_WS_MOVED = {
  workspaces: { text: "Workspaces are listed in the studio: pick one from the workspace menu in the rail.", button: "Close", testid: "nv-ws-moved-close" },
  templates: { text: "Workspace templates have their own page now.", button: "Open Workspace templates", testid: "nv-ws-moved-open" },
};

function NV_WorkspacesMoved(props) {
  var con = NV_useConsole();
  var which = props.which === "templates" ? "templates" : "workspaces";
  var copy = NV_WS_MOVED[which];
  return (
    <div className="nv-bind-empty col" data-testid={"nv-ws-moved:" + which} style={{ gap: 10, alignItems: "center" }}>
      <div>{copy.text}</div>
      <Btn kind={which === "templates" ? "primary" : "ghost"} data-testid={copy.testid}
        onClick={function () {
          if (which === "templates") con.closeOverlayTo({ name: "platform", nav: "templates" });
          else con.closeOverlay();
        }}>{copy.button}</Btn>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The management-surface mount table, moved here from the deleted
// sh-overlay-host on flag day. Each entry is a SHALLOW one-decision
// surface addressable as overlay=<name>[:<section>[:<id>]]; the
// no-chrome contract holds: a mount renders the page component and
// nothing else. The old "admin" entry died with the System view
// (users/sso/mcp/setup live there now) and "new-session" has its own
// designer panel above.
var NV_OVERLAY_MOUNTS = {
  providers: {
    // S4's standalone-mountable catalog (provider-catalog.jsx, the
    // M11d props-only contract). Class off the section segment,
    // instance off the id segment.
    render: function (state, shell) {
      return (
        <window.ProviderCatalog
          initialClass={state.section || "llm"}
          initialInstanceId={state.id || null}
          onNavigate={function (ref) {
            if (!ref || typeof ref !== "object") return;
            if (ref.kind === "provider-class") {
              shell.openOverlay("providers", ref.classKey, null);
            } else if (ref.kind === "provider-instance") {
              shell.openOverlay("providers", ref.classKey, ref.id);
            }
          }}
        />
      );
    },
  },
  // The subsystem, not the knowledge browser.
  "internal-collections": {
    render: function () {
      return (
        <window.InternalCollectionsPage
          pushToast={window.primerApi.toastPush}
        />
      );
    },
  },
  activity: {
    // The event log window rides the admin-gated /v1/events surface.
    roles: ["admin"],
    render: function () {
      return <window.SH_ActivityPanel />;
    },
  },
  collections: {
    render: function (state, shell) {
      return (
        <window.CollectionsPage
          pushToast={window.primerApi.toastPush}
          selectedId={state.id || null}
          onNavigate={function (cid) {
            shell.openOverlay("collections", null, cid || null);
          }}
          onOpen={function (cid) {
            shell.openDoc({
              kind: "wiki", ref: cid + "/" + (state.id || ""), preview: false,
            });
          }}
        />
      );
    },
  },
  // List and record are ONE overlay: the id slot decides which renders.
  agents: {
    render: function (state, shell) {
      if (state.id) {
        return (
          <window.AgentDetail
            agentId={state.id}
            pushToast={window.primerApi.toastPush}
          />
        );
      }
      return (
        <window.AgentsPage
          pushToast={window.primerApi.toastPush}
          startCreate={state.section === "new"}
          onOpen={function (aid) {
            shell.openOverlay("agents", null, aid);
          }}
        />
      );
    },
  },
  graphs: {
    render: function (state, shell) {
      if (state.id) {
        return (
          <window.GraphDetail
            graphId={state.id}
            pushToast={window.primerApi.toastPush}
          />
        );
      }
      return (
        <window.GraphsPage
          pushToast={window.primerApi.toastPush}
          startCreate={state.section === "new"}
          onOpen={function (gid) {
            shell.openOverlay("graphs", null, gid);
          }}
        />
      );
    },
  },
  triggers: {
    render: function (state) {
      return <window.TR_TriggersPage triggerId={state.id || null} />;
    },
  },
  toolsets: {
    render: function (state) {
      if (state.id) {
        return (
          <window.ToolsetDetail
            toolsetId={state.id}
            pushToast={window.primerApi.toastPush}
          />
        );
      }
      return <window.ToolsetsPage pushToast={window.primerApi.toastPush} />;
    },
  },
  // List/records and the policy editor are ONE overlay: the id slot
  // decides which renders (same "list and record are ONE overlay"
  // convention as `agents` above) - uiv2 Wave 3 routing fix (item 0).
  approvals: {
    render: function (state, shell) {
      if (state.id) {
        return (
          <window.AP_PolicyDetail
            policyId={state.id}
            pushToast={window.primerApi.toastPush}
            onClose={function () { shell.closeOverlayTo({ name: "platform", nav: "approvals" }); }}
          />
        );
      }
      return (
        <window.ApprovalsPage
          pushToast={window.primerApi.toastPush}
          startCreate={state.section === "new"}
          onNavigate={function (page, sid) {
            if (sid) {
              shell.openDoc({ kind: "session", ref: sid, preview: false });
            }
          }}
        />
      );
    },
  },
  harnesses: {
    render: function (state) {
      return <window.HarnessesPage harnessId={state.id || null} />;
    },
  },
  services: {
    render: function (state) {
      return <window.SV_ServicesPage serviceId={state.id || null} />;
    },
  },
  // Instances plus rules, one overlay.
  channels: {
    render: function (state) {
      if (state.section === "rules") {
        return <window.ChannelRulesPage pushToast={window.primerApi.toastPush} />;
      }
      return (
        <window.ChannelsPage
          pushToast={window.primerApi.toastPush}
          onNavigate={function () {}}
        />
      );
    },
  },
  workspaces: {
    render: function (state, shell) {
      // Templates and workspace providers are workspace-shaped
      // configuration, so they are sections of this overlay; an id
      // under either names ONE record. Otherwise an id names ONE
      // workspace (its Settings), with the section slot doubling as its tab.
      if (state.section === "templates") {
        // A template is the template overlay (Platform revamp 08, Task 11): it draws its own Modal chrome, so NV_LegacyOverlay bypasses its wrapper for
        // this address (bypassChrome below). The workspace provider detail page links here. Close closes the overlay.
        if (state.id) {
          return <window.WT_TemplateOverlay templateId={state.id} onClose={shell.closeOverlay} />;
        }
        return (
          <window.WorkspaceTemplatesPage
            pushToast={window.primerApi.toastPush}
          />
        );
      }
      if (state.section === "providers") {
        if (state.id) {
          return (
            <window.WorkspaceProviderDetail
              providerId={state.id}
              pushToast={window.primerApi.toastPush}
            />
          );
        }
        return (
          <window.WorkspaceProvidersPage
            pushToast={window.primerApi.toastPush}
          />
        );
      }
      if (state.id) {
        return (
          <window.WorkspaceDetail
            workspaceId={state.id}
            pushToast={window.primerApi.toastPush}
            onNavigate={function () {}}
            onOpenSession={function (sid) {
              // The overlay's id IS the workspace the session lives in; openDoc closes the overlay and opens the session in the studio.
              shell.openDoc({ kind: "session", ref: sid, workspace_id: state.id, preview: false });
            }}
          />
        );
      }
      // The bare address: the Workspaces page is gone (Task 14), so it says where workspaces are.
      return <NV_WorkspacesMoved which="workspaces" />;
    },
  },
};

// The heading each overlay shows above its page.
var NV_OVERLAY_TITLES = {
  providers: "Providers",
  collections: "Collections",
  agents: "Agents",
  graphs: "Graphs",
  triggers: "Triggers",
  toolsets: "Toolsets",
  approvals: "Approvals",
  harnesses: "Harnesses",
  services: "Services",
  channels: "Channels",
  workspaces: "Workspaces",
  "internal-collections": "Internal collections",
  activity: "Activity",
};

// Surfaces addressed by SECTION show the section's own name:
// "channels:rules" is the channel rules, not channels.
var NV_OVERLAY_SECTION_TITLES = {
  "channels:rules": "Channel rules",
  "workspaces:templates": "Workspace templates",
};

function NV_overlayTitle(overlay) {
  // A detail view is titled by its record; a list view by the surface.
  if (overlay.id) return overlay.id;
  if (overlay.section) {
    var keyed = NV_OVERLAY_SECTION_TITLES[
      overlay.name + ":" + overlay.section
    ];
    if (keyed) return keyed;
  }
  return NV_OVERLAY_TITLES[overlay.name] || overlay.name;
}

// ---------------------------------------------------------------------------
// Legacy adapter: the existing management surfaces, re-hosted. The mount
// table's render(state, shell) contract is served by a thin adapter over
// the console context; pages keep their own data logic and handlers.
function NV_LegacyOverlay(props) {
  var con = NV_useConsole();
  var overlay = props.overlay;
  var name = overlay.name;
  var mount = NV_OVERLAY_MOUNTS[name];
  var adapter = {
    wid: con.wid,
    role: con.role,
    registry: con.registry,
    openOverlay: function (n, s, i) { con.openOverlay(n, s, i); },
    closeOverlay: con.closeOverlay,
    // Leave the overlay for a view in one navigation (a mount whose Close or Back means "back to its Platform page").
    closeOverlayTo: function (v) { con.closeOverlayTo(v); },
    openDoc: function (d) {
      // A session is a studio document: it opens in the studio whatever view the overlay was opened over (a Platform view would hide the tab it
      // opened), in the workspace the caller names (d.workspace_id) or, failing that, the one the rail knows. The overlay closes with it.
      if (d.kind === "session") {
        con.openSessionInStudio(d.ref, d.workspace_id);
        return;
      }
      con.closeOverlay();
      con.setDoc({ kind: d.kind, ref: d.ref });
      if (con.promoteDoc) con.promoteDoc(d.kind + ":" + d.ref);
    },
    switchWorkspace: function (w) {
      con.closeOverlay();
      var verb = con.registry.get("workspace.switch");
      if (verb) verb.run({ wid: w });
    },
  };
  if (mount.roles && mount.roles.indexOf(con.role) < 0) {
    return (
      <NV_OverlayPanel title={NV_OVERLAY_TITLES[name] || name}
        testid={"nv-overlay:" + name} onClose={con.closeOverlay}>
        <div className="nv-bind-empty">This surface needs a different role.</div>
      </NV_OverlayPanel>
    );
  }
  // uiv2 Wave 2 ("Overlay geometry: centered modal, not near-fullscreen
  // sheet, for both overlays"): the agent editor and collection browser
  // DETAIL sub-views (overlay.id set) now render their own complete
  // Modal chrome (title/close/footer) instead of the shared
  // wrapper's wide sheet + breadcrumb row - stacking the shared VISIBLE
  // chrome around an already-complete inner Modal would produce two
  // title bars and two close buttons. The nv-overlay-body/nv-overlay:
  // {name} testids stay (unstyled passthrough divs) rather than being
  // dropped outright - tests/ui_e2e/_shell_helpers.py's open_legacy_route
  // is shared infrastructure across every overlay in this file, not
  // just these two, and asserts on exactly those testids as its "did
  // the navigation actually land" signal; the inner Modal's own scrim/
  // centering is 100% of the VISIBLE result either way. Scoped to just
  // these two names' detail case (list views - AgentsPage, the
  // collections table - are genuine list pages and keep the wide sheet
  // unchanged; every OTHER overlay in NV_OVERLAY_MOUNTS is untouched,
  // out of this wave's scope).
  //
  // Root-caused a real, deterministic (not flaky) CI failure: these
  // wrapper divs are bare, so their only box-contributing content is
  // Modal's own `.modal-overlay`, which is `position: fixed` and is
  // therefore taken OUT of normal flow - a bare block div with no
  // in-flow content collapses to a zero-size box as a flex child of
  // `.nv-root`, and Playwright's `to_be_visible()` correctly reports a
  // zero-area element as hidden even though the fixed-position Modal
  // still paints on top of it. The non-bypass path never hit this
  // because `.nv-scrim` carries its own explicit full-viewport CSS
  // sizing independent of its children. `position: fixed; inset: 0`
  // here gives both wrapper divs that same guaranteed box without
  // opting into any of `.nv-scrim`'s visible styling or its
  // click-to-close handler (the inner Modal already owns both) - NOT
  // `pointer-events: none`: that CSS property inherits, and Modal
  // never resets it on its own content, so it would silently make
  // every button/input inside unclickable. No dead-click-zone risk
  // either way: Modal's own `.modal-overlay` backdrop is `position:
  // fixed; inset: 0` too, so it exactly coincides with this box.
  //
  // And the wrappers sit on the OVERLAY LAYER, z-index 80, the layer
  // `.nv-scrim` uses. A `position: fixed` element is its own stacking
  // context, and with no z-index it painted at level 0, UNDER the
  // topbar (`.nv-topbar`, 60): the Modal's z-index 85 is only a rank
  // inside that context, so at a short viewport the topbar sat over the
  // editor's header and the Close button could not be clicked (a real
  // user's, and CI's, 1366x768). 80 is above the topbar and below the
  // shared Modals a page opens from inside (`.modal-overlay`, 85, the
  // ConfirmHost's dialogs) and the toasts (100).
  // tests/ui/test_overlay_navigation.py pins the number against styles.css.
  //
  // The workspace template overlay (workspaces:templates:<id>, Platform revamp 08) is a kit PK_FormOverlay, a Modal of its own, so it is bypassed the
  // same way.
  var bypassChrome = ((name === "agents" || name === "collections") && !!overlay.id)
    || (name === "workspaces" && overlay.section === "templates" && !!overlay.id);
  if (bypassChrome) {
    var bypassStyle = { position: "fixed", inset: 0, zIndex: 80 };
    return (
      <div data-testid="nv-overlay-body" style={bypassStyle}>
        <div data-testid={"nv-overlay:" + name} style={bypassStyle}>
          {mount.render(overlay, adapter)}
        </div>
      </div>
    );
  }
  return (
    <NV_OverlayPanel title={NV_overlayTitle(overlay)} wide
      testid={"nv-overlay:" + name} onClose={con.closeOverlay}>
      {(overlay.section || overlay.id)
        && NV_overlayTitle(overlay) !== (NV_OVERLAY_TITLES[name] || name) ? (
          <div className="nv-overlay-crumb">
            {/* A native button, so a keyboard user reaches the way back to the Platform page (audit G1-20); it was an <a> with no href. */}
            <button type="button" className="nv-overlay-crumb-link" data-testid="nv-overlay-crumb"
              onClick={function () {
                // A record's crumb leaves the overlay for the entity's Platform page; opening the bare overlay rendered the LIST inside the
                // overlay (design 10). A section may be the page ("workspaces:templates:<id>" is a template, so its crumb is the templates page) or a
                // tab ("toolsets:tools:<id>", which is not a page), so the section's own row is tried first and the overlay's name second. An
                // overlay with no Platform page of its own yet (providers) keeps opening its root.
                var sectionPath = overlay.section ? "/" + name + "/" + overlay.section : null;
                if (sectionPath && window.SH_shimUsesTemplateListOverlay(sectionPath)) {
                  con.openOverlay(name, overlay.section, null);
                  return;
                }
                var target = (sectionPath && window.SH_shimListTarget(sectionPath))
                  || window.SH_shimListTarget("/" + name);
                if (target) con.closeOverlayTo(target);
                else con.openOverlay(name, null, null);
              }}>
              {NV_OVERLAY_TITLES[name] || name}
            </button>
            <span className="nv-crumb-sep">/</span>
            <span>{NV_overlayTitle(overlay)}</span>
          </div>
        ) : null}
      <div className="nv-legacy-host">
        {mount.render(overlay, adapter)}
      </div>
    </NV_OverlayPanel>
  );
}

// ---------------------------------------------------------------------------
// Host: dispatch on the URL's overlay name. Installs the router shim so
// re-hosted pages that call primerApi.useRouter() navigate overlays
// instead of rewriting the hash (same contract SH_OverlayHost installed).
function NV_OverlayHost() {
  var con = NV_useConsole();
  var conRef = React.useRef(con);
  conRef.current = con;
  var installedRef = React.useRef(false);
  if (!installedRef.current) {
    installedRef.current = true;
    window.SH_installRouterShim(
      function () { return conRef.current.overlay; },
      function (n, s, i) { conRef.current.openOverlay(n, s, i); },
      // A bare list path (a detail's Delete and Back) closes the overlay onto the entity's Platform page, never onto a list inside an overlay.
      function (v) { conRef.current.closeOverlayTo(v); },
      // "/sessions/<id>" (a session row on an agent's, a toolset's or a workspace's detail) is a studio document, not an overlay; a row passes its
      // own workspace_id along.
      function (sid, wid) { conRef.current.openSessionInStudio(sid, wid); }
    );
  }
  var overlay = con.overlay;
  if (!overlay || !overlay.name) return null;
  if (overlay.name === "new-session") return <NV_CreateSessionOverlay />;
  if (overlay.name === "new-workspace") return <NV_CreateWorkspaceOverlay />;
  if (!NV_OVERLAY_MOUNTS[overlay.name]) return null;
  return <NV_LegacyOverlay overlay={overlay} />;
}

window.NV_OverlayPanel = NV_OverlayPanel;
window.NV_Field = NV_Field;
window.NV_CreateSessionOverlay = NV_CreateSessionOverlay;
window.NV_CreateWorkspaceOverlay = NV_CreateWorkspaceOverlay;
window.NV_WorkspacesMoved = NV_WorkspacesMoved;
window.NV_OVERLAY_MOUNTS = NV_OVERLAY_MOUNTS;
window.NV_OVERLAY_TITLES = NV_OVERLAY_TITLES;
window.NV_overlayTitle = NV_overlayTitle;
window.NV_OverlayHost = NV_OverlayHost;
