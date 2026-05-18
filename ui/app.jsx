/* global React, ReactDOM, Sidebar, Topbar, SessionsList, SessionDetail, Icon, Btn, CommandPalette, ToastContainer, Banner, useTweaks, TweaksPanel, TweakSection, TweakRadio, TweakColor, TweakText, Sparkline, ToolsetDetail, ProviderDetail */

const ACCENT_OPTIONS = {
  "Matrix green": { h: 145, c: 0.18, l: 0.85 },
  "Cobalt": { h: 240, c: 0.18, l: 0.72 },
  "Violet": { h: 290, c: 0.18, l: 0.74 },
  "Amber": { h: 65, c: 0.18, l: 0.82 },
};

const DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "dark",
  "accent": "Matrix green",
  "density": "default",
  "demoState": "happy",
  "subsystemOn": false,
  "icState": "configured"
}/*EDITMODE-END*/;

function App() {
  const [tweaks, setTweak] = useTweaks(DEFAULTS);

  // Apply theme + accent + density to root
  React.useEffect(() => {
    document.documentElement.setAttribute("data-theme", tweaks.theme);
    const acc = ACCENT_OPTIONS[tweaks.accent] || ACCENT_OPTIONS["Matrix green"];
    document.documentElement.style.setProperty("--accent-h", String(acc.h));
    document.documentElement.style.setProperty("--accent-c", String(acc.c));
    document.documentElement.style.setProperty("--accent-l", String(tweaks.theme === "light" ? 0.55 : acc.l));
    document.documentElement.setAttribute("data-density", tweaks.density);
  }, [tweaks.theme, tweaks.accent, tweaks.density]);

  // ----- App-level state
  // Page + per-entity ids are now derived from the URL hash via useRouter
  // instead of being tracked as React state. The full rip-and-replace with
  // <PageComponent {...router.params} /> from spec §5 / plan Task 10 Step 1
  // is deferred; this surgical migration keeps the existing page-switch
  // working while moving navigation onto the router.
  const router = window.matrixApi.useRouter();
  const page = (() => {
    const p = router.path;
    if (p === "/") return "dashboard";
    if (p === "/sessions") return "sessions";
    if (p === "/workspaces") return "workspaces";
    if (p === "/agents") return "agents";
    if (p === "/graphs") return "graphs";
    if (p === "/knowledge/collections") return "collections";
    if (p === "/knowledge/documents") return "documents";
    if (p === "/knowledge/search") return "search";
    if (p === "/toolsets") return "toolsets-user";
    if (p === "/toolsets/builtin") return "toolsets-builtin";
    if (p === "/providers/llm") return "llm";
    if (p === "/providers/embedding") return "embedding";
    if (p === "/providers/cross_encoder") return "rerank";
    if (p === "/subsystems/internal-collections") return "internal-collections";
    if (p === "/workers") return "workers";
    if (p === "/health") return "health";
    if (p.startsWith("/sessions/")) return "session-detail";
    if (p.startsWith("/workspaces/")) return "workspace-detail";
    if (p.startsWith("/agents/")) return "agent-detail";
    if (p.startsWith("/graphs/")) return "graph-detail";
    if (p.startsWith("/toolsets/") && p !== "/toolsets/builtin") return "toolset-detail";
    if (p.startsWith("/providers/llm/")) return "llm-detail";
    if (p.startsWith("/providers/embedding/")) return "embedding-detail";
    if (p.startsWith("/providers/cross_encoder/")) return "rerank-detail";
    return "dashboard";
  })();
  const currentSessionId = router.path.startsWith("/sessions/") ? router.params.id : null;
  const currentWorkspaceId = router.path.startsWith("/workspaces/") ? router.params.id : null;
  const currentAgentId = router.path.startsWith("/agents/") ? router.params.id : null;
  const currentGraphId = router.path.startsWith("/graphs/") ? router.params.id : null;
  const [docsFilterCollection, setDocsFilterCollection] = React.useState("");
  const [paletteOpen, setPaletteOpen] = React.useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = React.useState(() => {
    try { return localStorage.getItem("matrix.sidebar.iconsOnly") === "1"; } catch { return false; }
  });
  const toggleSidebar = () => {
    setSidebarCollapsed((c) => {
      const next = !c;
      try { localStorage.setItem("matrix.sidebar.iconsOnly", next ? "1" : "0"); } catch {}
      return next;
    });
  };
  // Global ⌘K / Ctrl+K to open command palette
  React.useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setPaletteOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  // Toast queue lives in window.matrixApi.useToast (foundation/toast.js).
  // <ToastContainer/> renders the live queue directly; here we only need
  // `push` so page components can surface info/error toasts via the
  // existing `pushToast` prop.
  const { push: pushToast } = window.matrixApi.useToast();
  const [newSessionOpen, setNewSessionOpen] = React.useState(false);

  const [sessions, setSessions] = React.useState(() => window.MOCK.buildSessions(Date.now()));
  const [workers, setWorkers] = React.useState(window.MOCK.WORKERS);
  const [tick, setTick] = React.useState(0);

  // Live tick — bump running sessions' last_turn_at + occasionally a turn count
  React.useEffect(() => {
    const id = setInterval(() => {
      setTick((t) => t + 1);
      setSessions((arr) =>
        arr.map((s) => {
          if (s.status === "running") {
            const sinceLast = (Date.now() - s.last_turn_at.getTime()) / 1000;
            let next = { ...s };
            if (sinceLast > 8 + Math.random() * 8) {
              next.last_turn_at = new Date();
              next.turn_count = s.turn_count + 1;
            }
            return next;
          }
          return s;
        })
      );
    }, 2000);
    return () => clearInterval(id);
  }, []);

  // Tweak: simulate "worker pool at capacity" state
  const workerStats = React.useMemo(() => {
    const running = sessions.filter((s) => s.status === "running").length;
    const totalCap = workers.reduce((a, w) => a + w.capacity, 0);
    const active = workers.filter((w) => w.status === "active").length;
    const inFlight = workers.reduce((a, w) => a + w.in_flight, 0);
    const overrideCap = tweaks.demoState === "capacity" ? 4 : totalCap;
    const overrideActive = tweaks.demoState === "no-workers" ? 0 : active;
    const overrideInFlight = tweaks.demoState === "capacity" ? Math.min(overrideCap, 4) : inFlight;
    return {
      active: overrideActive,
      total: workers.length,
      capacity: overrideCap,
      in_flight: overrideInFlight,
      history: Array.from({ length: 30 }, (_, i) => Math.sin(i / 4) * 2 + 3 + Math.random()),
    };
  }, [sessions, workers, tweaks.demoState]);

  const counts = {
    sessions: sessions.filter((s) => !["ended", "failed", "cancelled"].includes(s.status)).length,
    workspaces: 4,
    workers: 4,
  };

  const subsystemOn = !!tweaks.subsystemOn;

  const navigate = React.useCallback((target, extra) => {
    const enc = (v) => encodeURIComponent(String(v));
    const PATH_MAP = {
      "dashboard": "/",
      "sessions": "/sessions",
      "session-detail": extra ? `/sessions/${enc(extra)}` : "/sessions",
      "workspaces": "/workspaces",
      "workspace-detail": extra ? `/workspaces/${enc(extra)}` : "/workspaces",
      "agents": "/agents",
      "agent-detail": extra ? `/agents/${enc(extra)}` : "/agents",
      "graphs": "/graphs",
      "graph-detail": extra ? `/graphs/${enc(extra)}` : "/graphs",
      "collections": "/knowledge/collections",
      "documents": "/knowledge/documents",
      "search": "/knowledge/search",
      "toolsets-user": "/toolsets",
      "toolsets-builtin": "/toolsets/builtin",
      "llm": "/providers/llm",
      "embedding": "/providers/embedding",
      "rerank": "/providers/cross_encoder",
      "internal-collections": "/subsystems/internal-collections",
      "workers": "/workers",
      "health": "/health",
    };
    const path = PATH_MAP[target];
    if (path) router.navigate(path);
  }, [router]);

  const onPatchSession = (id, patch) => {
    setSessions((arr) => arr.map((s) => s.id === id ? { ...s, ...patch, last_turn_at: patch.status === "running" && !s.last_turn_at ? new Date() : s.last_turn_at } : s));
  };

  const onPatchWorker = (id, patch) => {
    setWorkers((arr) => arr.map((w) => w.id === id ? { ...w, ...patch } : w));
  };

  const openSession = (id) => navigate("session-detail", id);

  const currentSession = sessions.find((s) => s.id === currentSessionId);

  // Demo state shows error toast on mount
  React.useEffect(() => {
    if (tweaks.demoState === "rfc7807") {
      pushToast({
        kind: "error",
        title: "Workspace creation failed",
        detail: "Template 'python-3.11-slim' resolves but the provider config is invalid: missing required field 'image_pull_secret'.",
        reqId: "req_2c4d8b1f9e3a",
      });
    }
  }, [tweaks.demoState]);

  // -----------------------------
  // Page rendering
  // -----------------------------
  let pageHeader = null;
  let pageBody = null;

  if (page === "session-detail") {
    // Page header lives inside <SessionDetail/> per Milestone 4.
    pageHeader = null;
    pageBody = <SessionDetail />;
  } else if (page === "internal-collections") {
    // Page header lives inside <InternalCollectionsPage/> per Milestone 5.
    pageHeader = null;
    pageBody = <InternalCollectionsPage />;
  } else if (page === "llm" || page === "embedding" || page === "rerank") {
    // Page header lives inside <ProvidersPage/> per Milestone 5.
    pageHeader = null;
    pageBody = <ProvidersPage kind={page} />;
  } else if (page === "llm-detail" || page === "embedding-detail" || page === "rerank-detail") {
    pageHeader = null;
    const kind = page === "llm-detail" ? "llm" : page === "embedding-detail" ? "embedding" : "rerank";
    pageBody = <ProviderDetail kind={kind} />;
  } else if (page === "toolsets-user") {
    // Page header lives inside <ToolsetsPage/> per Milestone 5.
    pageHeader = null;
    pageBody = <ToolsetsPage kind="user" />;
  } else if (page === "toolsets-builtin") {
    pageHeader = null;
    pageBody = <ToolsetsPage kind="builtin" />;
  } else if (page === "toolset-detail") {
    pageHeader = null;
    pageBody = <ToolsetDetail />;
  } else if (page === "collections") {
    // Page header lives inside <CollectionsPage/> per Milestone 5.
    pageHeader = null;
    pageBody = <CollectionsPage />;
  } else if (page === "documents") {
    // Page header lives inside <DocumentsPage/> per Milestone 5. The
    // collection filter is now driven by ?collection= query param.
    pageHeader = null;
    pageBody = <DocumentsPage />;
  } else if (page === "search") {
    // Page header lives inside <SearchBench/> per Milestone 5.
    pageHeader = null;
    pageBody = <SearchBench />;
  } else if (page === "graphs") {
    // Page header lives inside <GraphsPage/> per Milestone 6.
    pageHeader = null;
    pageBody = <GraphsPage />;
  } else if (page === "graph-detail") {
    // Page header lives inside <GraphDetail/> per Milestone 6.
    pageHeader = null;
    pageBody = <GraphDetail />;
  } else if (page === "agents") {
    // Page header lives inside <AgentsPage/> per Milestone 6.
    pageHeader = null;
    pageBody = <AgentsPage onNewSession={() => setNewSessionOpen(true)} />;
  } else if (page === "agent-detail") {
    // Page header lives inside <AgentDetail/> per Milestone 6.
    pageHeader = null;
    pageBody = <AgentDetail />;
  } else if (page === "workspaces") {
    // Page header lives inside <WorkspacesPage/> per Milestone 4.
    pageHeader = null;
    pageBody = <WorkspacesPage />;
  } else if (page === "workspace-detail") {
    // Page header lives inside <WorkspaceDetail/> per Milestone 4.
    pageHeader = null;
    pageBody = <WorkspaceDetail />;
  } else if (page === "health") {
    // Page header lives inside <HealthPage/> per Milestone 3.
    pageHeader = null;
    pageBody = <HealthPage />;
  } else if (page === "workers") {
    // Page header lives inside <WorkersPage/> per Milestone 3.
    pageHeader = null;
    pageBody = <WorkersPage />;
  } else if (page === "dashboard") {
    // Page header lives inside <Dashboard/> per Milestone 3.
    pageHeader = null;
    pageBody = <Dashboard onNewSession={() => setNewSessionOpen(true)} />;
  } else if (page === "sessions") {
    // Page header lives inside <SessionsList/> per Milestone 4.
    pageHeader = null;
    pageBody = <SessionsList onNewSession={() => setNewSessionOpen(true)} />;
  } else {
    // Stub pages for sidebar entries
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>matrix</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>{prettyPage(page)}</span>
          </div>
          <h1 className="page-title">{prettyPage(page)}</h1>
          <div className="page-sub">This page is out of scope for the current mockup. Sessions and Predicate Builder are the focus.</div>
        </div>
      </>
    );
    pageBody = (
      <div className="panel">
        <div className="empty">
          <div className="ico-wrap"><Icon name="info" size={22} /></div>
          <div className="head">Not in this mockup</div>
          <div className="sub">
            The focus pages are <a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => navigate("sessions")}>Sessions</a>{" "}
            and the session control room. Other entities follow the same patterns described in §4 of the spec.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={`app ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      <Topbar onOpenPalette={() => setPaletteOpen(true)} />
      <Sidebar
        collapsed={sidebarCollapsed}
        onCollapseToggle={toggleSidebar}
      />
      <main className="main">
        {pageHeader != null && (
          <div className="page-header">
            {pageHeader}
          </div>
        )}
        <div className="page-body">
          {pageBody}
        </div>
      </main>

      <ToastContainer />

      {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} />}

      {newSessionOpen && (
        <NewSessionModal
          onClose={() => setNewSessionOpen(false)}
          onCreate={() => {
            setNewSessionOpen(false);
            pushToast({ kind: "success", title: "Session created", detail: "Status = created — awaiting worker claim." });
          }}
        />
      )}

      <TweaksPanel title="Tweaks">
        <TweakSection label="Appearance" />
          <TweakRadio
            label="Theme"
            value={tweaks.theme}
            onChange={(v) => setTweak("theme", v)}
            options={[{ value: "dark", label: "Dark" }, { value: "light", label: "Light" }]}
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            <div style={{ fontSize: 11, color: "rgba(41,38,27,.72)", fontWeight: 500 }}>Accent</div>
            <div style={{ display: "flex", gap: 6 }}>
              {Object.entries(ACCENT_OPTIONS).map(([name, o]) => {
                const color = `oklch(${o.l} ${o.c} ${o.h})`;
                const on = tweaks.accent === name;
                return (
                  <button
                    key={name}
                    type="button"
                    onClick={() => setTweak("accent", name)}
                    title={name}
                    style={{
                      width: 26, height: 26, borderRadius: 8,
                      background: color, border: 0, cursor: "pointer",
                      outline: on ? "2px solid #29261b" : "1px solid rgba(0,0,0,0.15)",
                      outlineOffset: on ? 1 : 0,
                      padding: 0,
                    }}
                  />
                );
              })}
            </div>
          </div>
          <TweakRadio
            label="Density"
            value={tweaks.density}
            onChange={(v) => setTweak("density", v)}
            options={[
              { value: "compact", label: "Compact" },
              { value: "default", label: "Default" },
              { value: "comfortable", label: "Comfy" },
            ]}
          />
          <TweakText
            label="Instance label"
            value={tweaks.instanceLabel || ""}
            placeholder="matrix · localhost:8765"
            onChange={(v) => setTweak("instanceLabel", v)}
          />
        <TweakSection label="Demo state" />
          <TweakRadio
            label="Internal Collections"
            value={tweaks.subsystemOn ? "on" : "off"}
            onChange={(v) => setTweak("subsystemOn", v === "on")}
            options={[{ value: "off", label: "Off" }, { value: "on", label: "On" }]}
          />
          <TweakRadio
            label="Sessions list"
            value={tweaks.demoState}
            onChange={(v) => setTweak("demoState", v)}
            options={[
              { value: "happy", label: "Happy path" },
              { value: "empty", label: "Empty" },
              { value: "loading", label: "Loading" },
              { value: "error-list", label: "List error" },
            ]}
          />
          <TweakRadio
            label="Pool"
            value={tweaks.demoState === "capacity" || tweaks.demoState === "no-workers" ? tweaks.demoState : "ok"}
            onChange={(v) => setTweak("demoState", v === "ok" ? "happy" : v)}
            options={[
              { value: "ok", label: "Healthy" },
              { value: "capacity", label: "Near cap" },
              { value: "no-workers", label: "0 workers" },
            ]}
          />
          <button className="btn btn-sm" style={{ marginTop: 6 }} onClick={() => pushToast({
            kind: "error",
            title: "Subsystem inactive",
            detail: "POST /v1/agents/search returned 503. Bootstrap Internal Collections to enable semantic search.",
            reqId: "req_5e1a8d2b3f97",
          })}>
            Trigger 503 toast
          </button>
      </TweaksPanel>
    </div>
  );
}

function prettyPage(p) {
  return ({
    dashboard: "Dashboard",
    workspaces: "Workspaces",
    agents: "Agents",
    graphs: "Graphs",
    collections: "Collections",
    documents: "Documents",
    search: "Entity search probe",
    "toolsets-user": "User toolsets",
    "toolsets-builtin": "Built-in toolsets",
    llm: "LLM providers",
    embedding: "Embedding providers",
    rerank: "Cross-Encoder providers",
    "internal-collections": "Internal Collections",
    workers: "Workers",
    health: "Health",
  })[p] || p;
}

function NewSessionModal({ onClose, onCreate, defaultWorkspaceId, defaultAgentId, defaultGraphId }) {
  const { apiFetch, useResource, useMutation, useToast } = window.matrixApi;
  const { push: pushToast } = useToast();

  // Live comboboxes — fetched on open, no polling.
  const workspaces = useResource("new-session:workspaces",
    (s) => apiFetch("GET", "/workspaces?limit=200", null, { signal: s }), {});
  const agents = useResource("new-session:agents",
    (s) => apiFetch("GET", "/agents?limit=200", null, { signal: s }), {});
  const graphs = useResource("new-session:graphs",
    (s) => apiFetch("GET", "/graphs?limit=200", null, { signal: s }), {});

  const [kind, setKind] = React.useState(defaultGraphId ? "graph" : "agent");
  const [agentId, setAgentId] = React.useState(defaultAgentId || "");
  const [graphId, setGraphId] = React.useState(defaultGraphId || "");
  const [workspaceId, setWorkspaceId] = React.useState(defaultWorkspaceId || "");
  const [instructions, setInstructions] = React.useState("");
  const [autoStart, setAutoStart] = React.useState(true);
  const [fieldErrors, setFieldErrors] = React.useState({});  // loc-key -> msg

  // Seed first available option once the lists arrive.
  React.useEffect(() => {
    if (!agentId && agents.data?.items?.length) setAgentId(agents.data.items[0].id);
  }, [agents.data, agentId]);
  React.useEffect(() => {
    if (!graphId && graphs.data?.items?.length) setGraphId(graphs.data.items[0].id);
  }, [graphs.data, graphId]);
  React.useEffect(() => {
    if (!workspaceId && workspaces.data?.items?.length) setWorkspaceId(workspaces.data.items[0].id);
  }, [workspaces.data, workspaceId]);

  const create = useMutation(
    (body) => apiFetch("POST", `/workspaces/${encodeURIComponent(body._workspaceId)}/sessions`, body._payload),
    {
      onSuccess: (created) => {
        pushToast({ kind: "success", title: "Session created", detail: created.id });
        if (onCreate) onCreate(created);
      },
      onError: (err) => {
        if (err.status === 422 && Array.isArray(err.fieldErrors)) {
          // Translate Pydantic field-errors into a flat key→msg map.
          const next = {};
          for (const fe of err.fieldErrors) {
            const loc = Array.isArray(fe.loc) ? fe.loc.join(".") : String(fe.loc);
            next[loc] = fe.msg;
          }
          setFieldErrors(next);
        } else {
          pushToast({ kind: "error", title: err.title || "Create failed", detail: err.detail || err.message, requestId: err.requestId });
        }
      },
      invalidates: ["/sessions?limit=200"],
    }
  );

  const canSubmit = workspaceId && (kind === "agent" ? agentId : graphId);

  const submit = async () => {
    setFieldErrors({});
    const binding = kind === "agent"
      ? { kind: "agent", agent_id: agentId }
      : { kind: "graph", graph_id: graphId };
    const payload = {
      binding,
      initial_instructions: instructions || null,
      auto_start: autoStart,
      metadata: {},
    };
    try {
      await create.mutate({ _workspaceId: workspaceId, _payload: payload });
    } catch (_e) { /* onError already handled */ }
  };

  return (
    <Modal
      title="New session"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={submit} disabled={!canSubmit || create.loading}>
            {create.loading ? "Creating…" : "Create"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">Binding</label>
        <div className="chip-group" style={{ display: "inline-flex" }}>
          <span className={`chip ${kind === "agent" ? "active" : ""}`} onClick={() => { setKind("agent"); setFieldErrors({}); }}>agent</span>
          <span className={`chip ${kind === "graph" ? "active" : ""}`} onClick={() => { setKind("graph"); setFieldErrors({}); }}>graph</span>
        </div>
      </div>
      <div className="field">
        <label className="field-label">Workspace</label>
        <select className="select" value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)} style={{ width: "100%" }}>
          <option value="">-- pick a workspace --</option>
          {(workspaces.data?.items ?? []).map((w) => <option key={w.id} value={w.id}>{w.id}</option>)}
        </select>
        {workspaces.loading && <div className="field-help">Loading workspaces…</div>}
      </div>
      <div className="field">
        <label className="field-label">{kind === "agent" ? "Agent" : "Graph"}</label>
        {kind === "agent" ? (
          <select className="select" value={agentId} onChange={(e) => setAgentId(e.target.value)} style={{ width: "100%" }}>
            <option value="">-- pick an agent --</option>
            {(agents.data?.items ?? []).map((a) => <option key={a.id} value={a.id}>{a.id}</option>)}
          </select>
        ) : (
          <>
            <select className="select" value={graphId} onChange={(e) => setGraphId(e.target.value)} style={{ width: "100%" }}>
              <option value="">-- pick a graph --</option>
              {(graphs.data?.items ?? []).map((g) => <option key={g.id} value={g.id}>{g.id}</option>)}
            </select>
            <div className="field-help">
              The graph runs end-to-end in one turn; per-node state is persisted to the workspace's <span className="mono">.state/graphs/&lt;session_id&gt;/</span> git subtree.
            </div>
          </>
        )}
        {fieldErrors["body.binding"] && (
          <div className="field-help" style={{ color: "var(--red)" }}>
            {fieldErrors["body.binding"]}
          </div>
        )}
      </div>
      <div className="field">
        <label className="field-label">Initial instructions</label>
        <textarea
          className="textarea"
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          rows={4}
          placeholder="Tell the agent what to do (optional)…"
        />
        {fieldErrors["body.initial_instructions"] && (
          <div className="field-help" style={{ color: "var(--red)" }}>
            {fieldErrors["body.initial_instructions"]}
          </div>
        )}
      </div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          id="ns-auto-start"
          type="checkbox"
          checked={autoStart}
          onChange={(e) => setAutoStart(e.target.checked)}
        />
        <label htmlFor="ns-auto-start" className="field-label" style={{ margin: 0, cursor: "pointer" }}>
          Auto-start (begin first turn immediately)
        </label>
      </div>
    </Modal>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
