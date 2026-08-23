/* global React, SH_api, NV_useConsole, NV_identity,
   SharedNewSessionSchemaField */
// Overlays (wiring plan P3 T9). Three tiers behind one URL-addressed
// host:
//
//   new-session    - the designer's create-session panel, ported: one
//                    combined agent+graph binding picker, instructions,
//                    an Advanced fold for autonomy, and the graph
//                    Begin.input_schema form where a graph declares one.
//   new-workspace  - the designer's instantiation panel: template rows,
//                    optional name, per-instantiation env/init overrides.
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
// Panel primitive: scrim + centered panel + header (title, verb chip,
// close) + scrolling body + optional footer. Esc and scrim-click close.
function NV_OverlayPanel(props) {
  var onClose = props.onClose;
  React.useEffect(function () {
    function onKey(ev) {
      if (ev.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return function () { window.removeEventListener("keydown", onKey); };
  }, [onClose]);
  return (
    <div className="nv-scrim" data-testid="nv-overlay-scrim"
      onClick={function () { onClose(); }}>
      <div className="nv-overlay-panel"
        data-wide={props.wide ? "true" : "false"}
        data-testid={props.testid || "nv-overlay-panel"}
        onClick={function (ev) { ev.stopPropagation(); }}>
        <div className="nv-overlay-head">
          {/* THE one title (one-title rule), and the page's h1: the
              re-hosted pages render action bars, never headings, and
              the e2e suite addresses surfaces by h1.page-title. */}
          <h1 className="nv-overlay-title page-title"
            data-testid="nv-overlay-title">{props.title}</h1>
          {props.verb ? (
            <span className="nv-verb-chip">verb: {props.verb}</span>
          ) : null}
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

// Field primitive: label + optional hint above any control.
function NV_Field(props) {
  return (
    <div className="nv-field">
      <div className="nv-field-label">
        {props.label}
        {props.hint ? <span className="nv-field-hint">{props.hint}</span> : null}
      </div>
      {props.children}
    </div>
  );
}

function NV_errText(err) {
  if (!err) return null;
  return err.detail || err.message || "Request failed";
}

// ---------------------------------------------------------------------------
// Create session. Submit-body semantics are the SharedNewSessionForm
// contract verbatim: omitting binding asks for the system default agent;
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

  // Typing instructions implies intent to run (SharedNewSessionForm's
  // rule): the toggle follows along until the operator touches it.
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
    rows.push({ kind: "graph", id: g.id, desc: g.description || "" });
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
    if (bind) {
      body.binding = bind.kind === "graph"
        ? { kind: "graph", graph_id: bind.id }
        : { kind: "agent", agent_id: bind.id };
    }
    if (name.trim()) body.name = name.trim();
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
      }
    }, function (e) {
      setBusy(false);
      setErr(e);
    });
  }

  return (
    <NV_OverlayPanel title="Create session" verb="Create Session"
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
                  <div className="nv-bind-empty">No agent or graph matches.</div>
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
        <NV_Field label="Initial instructions">
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

// ---------------------------------------------------------------------------
// Create workspace: template rows, optional name, per-instantiation
// overrides (env additions + one extra init command). POST /v1/workspaces
// {template_id, name?, overrides?{env, init_commands}}; ids are
// backend-allocated.
function NV_CreateWorkspaceOverlay() {
  var con = NV_useConsole();
  var apiFetch = window.primerApi.apiFetch;
  var templates = window.primerApi.useResource(
    "nv-ov:workspace-templates",
    function (signal) {
      return apiFetch("GET", "/workspace_templates?limit=200", null,
        { signal: signal });
    },
    { pollMs: 0 }
  );
  var tplState = React.useState("");
  var tplId = tplState[0];
  var setTplId = tplState[1];
  var nameState = React.useState("");
  var name = nameState[0];
  var setName = nameState[1];
  var envState = React.useState([]);
  var envRows = envState[0];
  var setEnvRows = envState[1];
  var initState = React.useState("");
  var init = initState[0];
  var setInit = initState[1];
  var busyState = React.useState(false);
  var busy = busyState[0];
  var setBusy = busyState[1];
  var errState = React.useState(null);
  var err = errState[0];
  var setErr = errState[1];
  var seqRef = React.useRef(0);

  var tplItems = (templates.data && templates.data.items) || [];
  React.useEffect(function () {
    if (!tplId && tplItems.length) setTplId(tplItems[0].id);
  }, [tplItems.length]);

  function submit() {
    if (!tplId || busy) return;
    setBusy(true);
    setErr(null);
    var body = { template_id: tplId };
    if (name.trim()) body.name = name.trim();
    var env = {};
    var envCount = 0;
    envRows.forEach(function (row) {
      if (row.k.trim()) {
        env[row.k.trim()] = row.v;
        envCount += 1;
      }
    });
    if (envCount || init.trim()) {
      body.overrides = {};
      if (envCount) body.overrides.env = env;
      if (init.trim()) body.overrides.init_commands = [init.trim()];
    }
    apiFetch("POST", "/workspaces", body).then(function (row) {
      con.closeOverlay();
      con.toast("Workspace created: " + (row.name || row.id));
      var verb = con.registry.get("workspace.switch");
      if (verb && row.id) verb.run({ wid: row.id });
    }, function (e) {
      setBusy(false);
      setErr(e);
    });
  }

  return (
    <NV_OverlayPanel title="Create workspace" verb="Create Workspace"
      testid="nv-overlay:new-workspace" onClose={con.closeOverlay}
      footer={(
        <React.Fragment>
          <button type="button" className="nv-btn-secondary"
            onClick={con.closeOverlay}>Cancel</button>
          <button type="button" className="nv-btn-primary"
            data-testid="nv-nw-create" disabled={!tplId || busy}
            onClick={submit}>
            {busy ? "Creating…" : "Create workspace"}
          </button>
        </React.Fragment>
      )}>
      {err ? (
        <div className="nv-form-error" data-testid="nv-nw-error">
          {NV_errText(err)}
        </div>
      ) : null}
      <NV_Field label="Template">
        {templates.loading && !tplItems.length ? (
          <div className="nv-bind-empty">Loading templates…</div>
        ) : !tplItems.length ? (
          <div className="nv-form-error">
            No workspace templates registered. Create one under
            Workspaces → Templates first.
            <button type="button" className="nv-btn-secondary"
              style={{ marginTop: 8 }}
              onClick={function () {
                con.openOverlay("workspaces", "templates", null);
              }}>Open templates</button>
          </div>
        ) : (
          <div className="nv-pick-list">
            {tplItems.map(function (t) {
              return (
                <div key={t.id} className="nv-pick-row"
                  data-active={t.id === tplId ? "true" : "false"}
                  data-testid={"nv-nw-tpl:" + t.id}
                  onClick={function () { setTplId(t.id); }}>
                  {t.id}
                </div>
              );
            })}
          </div>
        )}
      </NV_Field>
      <NV_Field label="Name"
        hint="optional — shows in the selector in place of the id">
        <input className="nv-input" data-testid="nv-nw-name" value={name}
          placeholder="e.g. checkout-spike"
          onChange={function (ev) { setName(ev.target.value); }} />
      </NV_Field>
      <NV_Field label="Overrides" hint="this instantiation only">
        <div className="nv-kv-box">
          {envRows.map(function (row) {
            return (
              <div key={row.key} className="nv-kv-row">
                <input className="nv-input nv-kv-key" value={row.k}
                  placeholder="KEY"
                  onChange={function (ev) {
                    var val = ev.target.value;
                    setEnvRows(function (prev) {
                      return prev.map(function (r) {
                        return r.key === row.key
                          ? { key: r.key, k: val, v: r.v } : r;
                      });
                    });
                  }} />
                <span className="nv-kv-eq">=</span>
                <input className="nv-input nv-kv-val" value={row.v}
                  placeholder="value (secret-typed)"
                  onChange={function (ev) {
                    var val = ev.target.value;
                    setEnvRows(function (prev) {
                      return prev.map(function (r) {
                        return r.key === row.key
                          ? { key: r.key, k: r.k, v: val } : r;
                      });
                    });
                  }} />
                <button type="button" className="nv-kv-del" title="Remove"
                  onClick={function () {
                    setEnvRows(function (prev) {
                      return prev.filter(function (r) { return r.key !== row.key; });
                    });
                  }}>×</button>
              </div>
            );
          })}
          <div className="nv-kv-row">
            <span className="nv-kv-key nv-kv-label">init</span>
            <input className="nv-input nv-kv-val" value={init}
              data-testid="nv-nw-init"
              placeholder="extra init command"
              onChange={function (ev) { setInit(ev.target.value); }} />
          </div>
          <button type="button" className="nv-btn-secondary nv-kv-add"
            data-testid="nv-nw-add-env"
            onClick={function () {
              seqRef.current += 1;
              setEnvRows(function (prev) {
                return prev.concat([{ key: "e" + seqRef.current, k: "", v: "" }]);
              });
            }}>+ env var</button>
          <div className="nv-kv-note">
            Env values are secret-typed; the template's own mounts,
            network policy and init commands still apply first.
          </div>
        </div>
      </NV_Field>
    </NV_OverlayPanel>
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
  tools: {
    render: function () {
      return <window.ToolsPage pushToast={window.primerApi.toastPush} />;
    },
  },
  // Workers + Health collapse into one overlay; health is a section.
  workers: {
    render: function (state) {
      if (state.section === "health") return <window.HealthPage sessions={null} />;
      return <window.WorkersPage pushToast={window.primerApi.toastPush} />;
    },
  },
  approvals: {
    render: function (state, shell) {
      return (
        <window.ApprovalsPage
          pushToast={window.primerApi.toastPush}
          onNavigate={function (_page, sid) {
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
      // workspace, with the section slot doubling as its tab.
      if (state.section === "templates") {
        if (state.id) {
          return (
            <window.WorkspaceTemplateDetail
              templateId={state.id}
              pushToast={window.primerApi.toastPush}
            />
          );
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
              shell.closeOverlay();
              shell.openDoc({ kind: "session", ref: sid, preview: false });
            }}
          />
        );
      }
      return (
        <window.WorkspacesPage
          pushToast={window.primerApi.toastPush}
          onOpen={function (wid) {
            shell.switchWorkspace(wid);
          }}
        />
      );
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
  tools: "Tools",
  workers: "Workers",
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
  "workers:health": "Health",
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
    openDoc: function (d) {
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
  return (
    <NV_OverlayPanel title={NV_overlayTitle(overlay)} wide
      testid={"nv-overlay:" + name} onClose={con.closeOverlay}>
      {overlay.section || overlay.id ? (
        <div className="nv-overlay-crumb">
          <a data-testid="nv-overlay-crumb"
            onClick={function () { con.openOverlay(name, null, null); }}>
            {NV_OVERLAY_TITLES[name] || name}
          </a>
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
      function (n, s, i) { conRef.current.openOverlay(n, s, i); }
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
window.NV_OVERLAY_MOUNTS = NV_OVERLAY_MOUNTS;
window.NV_OVERLAY_TITLES = NV_OVERLAY_TITLES;
window.NV_overlayTitle = NV_overlayTitle;
window.NV_OverlayHost = NV_OverlayHost;
