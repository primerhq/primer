/* global React, ReactDOM, Sidebar, Topbar, SessionsList, SessionDetail, Icon, Btn, StatusPill, CommandPalette, Banner, useTweaks, TweaksPanel, TweakSection, TweakRadio, TweakColor, Sparkline */

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
  "icState": "configured",
  "ssmState": "one-pgvector"
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
  // Routing now driven by foundation/router.js (hash-based) instead of
  // local React.useState. `page` and `currentXId` are derived from the URL
  // so deep links + back/forward work natively. The `navigate(target, extra)`
  // helper below converts Designer's page-name API into hash URLs.
  const { path, params, query } = window.matrixApi.useRouter();
  const page = (() => {
    const m = path.match(/^\/([^/?]*)/);
    const root = m ? m[1] : "";
    if (root === "" || root === "dashboard") return "dashboard";
    if (root === "sessions") return params.id ? "session-detail" : "sessions";
    if (root === "workspaces") return params.id ? "workspace-detail" : "workspaces";
    if (root === "agents") return params.id ? "agent-detail" : "agents";
    if (root === "graphs") return params.id ? "graph-detail" : "graphs";
    if (root === "ssp") return params.id ? "ssp-detail" : "semantic-search";
    if (root === "chats") return params.id ? "chat-detail" : "chats";
    if (root === "channels") {
      if (path.startsWith("/channels/providers/") && params.id) return "channel-provider-detail";
      if (path.startsWith("/channels/providers")) return "channel-providers";
      if (path.startsWith("/channels/channels")) return "channels";
      if (path.startsWith("/channels/associations")) return "channel-associations";
      return "channel-providers";
    }
    if (root === "workers") return "workers";
    if (root === "health") return "health";
    if (root === "approvals") return "approvals";
    if (root === "knowledge") {
      if (path.startsWith("/knowledge/collections")) return "collections";
      if (path.startsWith("/knowledge/documents")) return "documents";
      if (path.startsWith("/knowledge/search")) return "collection-search";
      return "collections";
    }
    if (root === "toolsets") {
      if (path.startsWith("/toolsets/builtin")) return "toolsets-builtin";
      if (params.id) return "toolset-detail";
      return "toolsets-user";
    }
    if (root === "providers") {
      if (path.startsWith("/providers/llm")) return "llm";
      if (path.startsWith("/providers/embedding")) return "embedding";
      if (path.startsWith("/providers/cross_encoder")) return "rerank";
      return "llm";
    }
    if (root === "subsystems") {
      if (path.startsWith("/subsystems/internal-collections")) return "internal-collections";
      return "internal-collections";
    }
    return root;
  })();

  const currentSessionId = page === "session-detail" ? params.id : null;
  const currentWorkspaceId = page === "workspace-detail" ? params.id : null;
  const currentAgentId = page === "agent-detail" ? params.id : null;
  const currentGraphId = page === "graph-detail" ? params.id : null;
  const currentSspId = page === "ssp-detail" ? params.id : null;
  const currentChatId = page === "chat-detail" ? params.id : null;
  const currentChannelProviderId = page === "channel-provider-detail" ? params.id : null;
  const currentToolsetId = page === "toolset-detail" ? params.id : null;
  const docsFilterCollection = query.collection || "";
  // Reflect collection filter in the URL — Documents page reads it back via query.
  const setDocsFilterCollection = (cid) => {
    const next = cid ? "#/knowledge/documents?collection=" + encodeURIComponent(cid) : "#/knowledge/documents";
    window.location.hash = next;
  };
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
  const [toasts, setToasts] = React.useState([]);
  const [newSessionOpen, setNewSessionOpen] = React.useState(false);

  const [sessions, setSessions] = React.useState(() => window.MOCK.buildSessions(Date.now()));
  const [workers, setWorkers] = React.useState(window.MOCK.WORKERS);

  // Real-data overlay (Phase 2 wiring) — Topbar's worker pill, the
  // Dashboard tiles, and the Health page all depend on /v1/workers
  // and /v1/health. We poll both at the top so all consumers stay in
  // sync without duplicating fetches.
  const realWorkers = window.matrixApi.useResource(
    "topbar:workers",
    (signal) => window.matrixApi.apiFetch("GET", "/workers", null, { signal }),
    { pollMs: 5000 }
  );
  const realHealth = window.matrixApi.useResource(
    "topbar:health",
    (signal) => window.matrixApi.apiFetch("GET", "/health", null, { signal }),
    { pollMs: 5000 }
  );
  // Sidebar workspaces count — Workspaces is one of the few nav items
  // with a count badge. The 5s poll cadence is what U0095 + U0024 pin
  // (decrement / increment after API DELETE / POST without a manual
  // refresh). Task 15 owns the broader sidebar wiring; this entry is
  // here so the workspaces list+detail page can ride on the same
  // resource as the sidebar without a second roundtrip.
  const realWorkspaces = window.matrixApi.useResource(
    "topbar:workspaces",
    (signal) => window.matrixApi.apiFetch("GET", "/workspaces?limit=200", null, { signal }),
    { pollMs: 5000 }
  );

  // Semantic Search providers — controlled by the ssmState tweak
  const ssps = React.useMemo(() => {
    if (tweaks.ssmState === "none") return [];
    if (tweaks.ssmState === "many") return window.MOCK.SSP_PROVIDERS;
    return window.MOCK.SSP_PROVIDERS.slice(0, 1);
  }, [tweaks.ssmState]);

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

  // Tweak: simulate "worker pool at capacity" state.
  // Prefer real data from /v1/workers + /v1/health when present;
  // fall back to mock values so the Tweaks demo states still work.
  const workerStats = React.useMemo(() => {
    const realItems = realWorkers.data?.items;
    const useReal = Array.isArray(realItems);
    const totalCap = useReal
      ? (typeof realHealth.data?.worker_pool?.capacity === "number"
          ? realHealth.data.worker_pool.capacity
          : realItems.reduce((a, w) => a + (w.capacity || 0), 0))
      : workers.reduce((a, w) => a + w.capacity, 0);
    const active = useReal
      ? realItems.filter((w) => w.status === "active").length
      : workers.filter((w) => w.status === "active").length;
    const totalWorkers = useReal ? realItems.length : workers.length;
    const inFlight = useReal
      ? (typeof realHealth.data?.worker_pool?.in_flight === "number"
          ? realHealth.data.worker_pool.in_flight
          : 0)
      : workers.reduce((a, w) => a + w.in_flight, 0);
    const overrideCap = tweaks.demoState === "capacity" ? 4 : totalCap;
    const overrideActive = tweaks.demoState === "no-workers" ? 0 : active;
    const overrideInFlight = tweaks.demoState === "capacity" ? Math.min(overrideCap, 4) : inFlight;
    return {
      active: overrideActive,
      total: totalWorkers,
      capacity: overrideCap,
      in_flight: overrideInFlight,
      history: Array.from({ length: 30 }, (_, i) => Math.sin(i / 4) * 2 + 3 + Math.random()),
    };
  }, [sessions, workers, tweaks.demoState, realWorkers.data, realHealth.data]);

  const counts = {
    sessions: sessions.filter((s) => !["ended", "failed", "cancelled"].includes(s.status)).length,
    workspaces: Array.isArray(realWorkspaces.data?.items)
      ? realWorkspaces.data.items.length
      : 0,
    workers: workerStats.total,
    ssps: ssps.length,
    chats: (window.CHATS_DATA || []).filter((c) => c.status === "active").length,
    channels: (window.CHANNELS_DATA || []).length,
    approvals_pending: (window.PENDING_APPROVALS || []).length,
  };

  const subsystemOn = !!tweaks.subsystemOn;

  const pushToast = (t) => {
    const id = Math.random().toString(36).slice(2);
    setToasts((arr) => [...arr, { ...t, id }]);
    setTimeout(() => setToasts((arr) => arr.filter((x) => x.id !== id)), (t.kind === "error" ? 12 : 5) * 1000);
  };
  const removeToast = (id) => setToasts((arr) => arr.filter((x) => x.id !== id));

  const navigate = (target, extra) => {
    // Designer's API: navigate(page, extra?). Map to URL paths.
    const ROUTES = {
      dashboard: "/",
      sessions: "/sessions",
      "session-detail": (e) => `/sessions/${e}`,
      workspaces: "/workspaces",
      "workspace-detail": (e) => `/workspaces/${e}`,
      agents: "/agents",
      "agent-detail": (e) => `/agents/${e}`,
      graphs: "/graphs",
      "graph-detail": (e) => `/graphs/${e}`,
      workers: "/workers",
      health: "/health",
      collections: "/knowledge/collections",
      documents: (e) => e ? `/knowledge/documents?collection=${encodeURIComponent(e)}` : "/knowledge/documents",
      "collection-search": (e) => e ? `/knowledge/search?collection=${encodeURIComponent(e)}` : "/knowledge/search",
      "toolsets-user": "/toolsets",
      "toolsets-builtin": "/toolsets/builtin",
      "toolset-detail": (e) => `/toolsets/${e}`,
      llm: "/providers/llm",
      embedding: "/providers/embedding",
      rerank: "/providers/cross_encoder",
      "semantic-search": "/ssp",
      "ssp-detail": (e) => `/ssp/${e}`,
      "internal-collections": "/subsystems/internal-collections",
      approvals: "/approvals",
      "channel-providers": "/channels/providers",
      channels: "/channels/channels",
      "channel-associations": "/channels/associations",
      "channel-provider-detail": (e) => `/channels/providers/${e}`,
      chats: "/chats",
      "chat-detail": (e) => `/chats/${e}`,
    };
    const route = ROUTES[target];
    const url = typeof route === "function" ? route(extra) : (route || "/");
    // useRouter().navigate(path) is the canonical API, but the navigate helper
    // is invoked from event handlers — set the hash directly to avoid React
    // rule-of-hooks concerns and the defensive fallback covers all callers.
    window.location.hash = "#" + url;
  };

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

  if (page === "session-detail" && currentSessionId) {
    pageHeader = (
      <>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="crumb">
            <a onClick={() => navigate("sessions")} style={{ cursor: "pointer" }}>Sessions</a>
            <span className="sep">/</span>
            <span className="mono" style={{ color: "var(--text)" }}>{currentSessionId}</span>
          </div>
          <h1 className="page-title mono">{currentSessionId}</h1>
          <SessionStatusCaption sid={currentSessionId} />
        </div>
        <div className="page-actions">
          <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("sessions")}>Back to list</Btn>
        </div>
      </>
    );
    pageBody = (
      <SessionDetail
        sid={currentSessionId}
        onBack={() => navigate("sessions")}
        pushToast={pushToast}
      />
    );
  } else if (page === "internal-collections") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>Subsystems</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Internal Collections</span>
          </div>
          <h1 className="page-title">Internal Collections</h1>
          <div className="page-sub">Powers semantic search across agents, graphs, collections, and tools.</div>
        </div>
        <div className="page-actions">
          <span className={tweaks.icState === "active" ? "pill pill-ended" : tweaks.icState === "configured" ? "pill pill-paused" : "pill pill-cancelled"}>
            <span className="dot"></span>{tweaks.icState}
          </span>
        </div>
      </>
    );
    pageBody = (
      <InternalCollectionsPage
        state={tweaks.icState}
        setState={(s) => { setTweak({ icState: s, subsystemOn: s === "active" }); }}
        ssps={ssps}
        ssmState={tweaks.ssmState}
        onNavigate={navigate}
        pushToast={pushToast}
      />
    );
  } else if (page === "approvals") {
    pageHeader = (
      <>
        <div>
          <div className="crumb"><a onClick={() => navigate("dashboard")}>Subsystems</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Approvals</span></div>
          <h1 className="page-title">Approvals</h1>
          <div className="page-sub">Tool-call gating · policies + live pending queue</div>
        </div>
      </>
    );
    pageBody = <ApprovalsPage pushToast={pushToast} onNavigate={navigate} />;
  } else if (page === "chats") {
    pageHeader = (
      <>
        <div>
          <div className="crumb"><a onClick={() => navigate("dashboard")}>Compute</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Chats</span></div>
          <h1 className="page-title">Chats</h1>
          <div className="page-sub">Conversational sessions with an agent · WS-backed</div>
        </div>
      </>
    );
    pageBody = <ChatsPage onOpen={(id) => navigate("chat-detail", id)} pushToast={pushToast} />;
  } else if (page === "chat-detail" && currentChatId) {
    pageHeader = (
      <>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="crumb"><a onClick={() => navigate("chats")}>Chats</a><span className="sep">/</span><span className="mono" style={{ color: "var(--text)" }}>{currentChatId}</span></div>
          <h1 className="page-title mono">{currentChatId}</h1>
        </div>
        <div className="page-actions"><Btn icon="chevron-left" kind="ghost" onClick={() => navigate("chats")}>Back</Btn></div>
      </>
    );
    pageBody = <ChatDetail chatId={currentChatId} onBack={() => navigate("chats")} pushToast={pushToast} />;
  } else if (page === "channel-providers") {
    pageHeader = (
      <>
        <div>
          <div className="crumb"><a onClick={() => navigate("dashboard")}>Channels</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Providers</span></div>
          <h1 className="page-title">Channel providers</h1>
          <div className="page-sub">Slack · Telegram · Discord adapters</div>
        </div>
      </>
    );
    pageBody = <ChannelProvidersPage onOpen={(id) => navigate("channel-provider-detail", id)} pushToast={pushToast} />;
  } else if (page === "channel-provider-detail" && currentChannelProviderId) {
    pageHeader = (
      <>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="crumb"><a onClick={() => navigate("channel-providers")}>Channel providers</a><span className="sep">/</span><span className="mono" style={{ color: "var(--text)" }}>{currentChannelProviderId}</span></div>
          <h1 className="page-title mono">{currentChannelProviderId}</h1>
        </div>
        <div className="page-actions"><Btn icon="chevron-left" kind="ghost" onClick={() => navigate("channel-providers")}>Back</Btn></div>
      </>
    );
    pageBody = <ChannelProviderDetail providerId={currentChannelProviderId} pushToast={pushToast} />;
  } else if (page === "channels") {
    pageHeader = (
      <>
        <div>
          <div className="crumb"><a onClick={() => navigate("dashboard")}>Channels</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Channels</span></div>
          <h1 className="page-title">Channels</h1>
          <div className="page-sub">External rooms / DMs / chats bound to a provider</div>
        </div>
      </>
    );
    pageBody = <ChannelsPage onNavigate={navigate} pushToast={pushToast} />;
  } else if (page === "channel-associations") {
    pageHeader = (
      <>
        <div>
          <div className="crumb"><a onClick={() => navigate("dashboard")}>Channels</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Associations</span></div>
          <h1 className="page-title">Workspace ↔ channel associations</h1>
          <div className="page-sub">Which workspaces fan out to which channels, with per-tool flags</div>
        </div>
      </>
    );
    pageBody = <AssociationsPage onNavigate={navigate} pushToast={pushToast} />;
  } else if (page === "semantic-search") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>Providers</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Semantic Search</span>
          </div>
          <h1 className="page-title">Semantic Search providers</h1>
          <div className="page-sub">
            Vector indexes for collections · pgvector or pgvectorscale ·
            <span className="mono" style={{ marginLeft: 4, color: "var(--text-3)" }}>autorefresh every 5s</span>
          </div>
        </div>
      </>
    );
    pageBody = (
      <SSPListPage
        ssps={ssps}
        ssmState={tweaks.ssmState}
        onOpen={(id) => navigate("ssp-detail", id)}
        onCreate={(p) => pushToast({ kind: "success", title: "Provider created", detail: `${p.id} (${p.provider}) created. POST /v1/ssp → 201` })}
        pushToast={pushToast}
      />
    );
  } else if (page === "ssp-detail" && currentSspId) {
    pageHeader = (
      <>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="crumb">
            <a onClick={() => navigate("semantic-search")}>Semantic Search</a>
            <span className="sep">/</span>
            <span className="mono" style={{ color: "var(--text)" }}>{currentSspId}</span>
          </div>
          <h1 className="page-title mono">{currentSspId}</h1>
        </div>
        <div className="page-actions">
          <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("semantic-search")}>Back</Btn>
        </div>
      </>
    );
    pageBody = (
      <SSPDetail
        sspId={currentSspId}
        ssps={ssps}
        onDelete={() => { navigate("semantic-search"); pushToast({ kind: "success", title: "Provider deleted", detail: "DELETE /v1/ssp → 204" }); }}
        pushToast={pushToast}
      />
    );
  } else if (page === "llm" || page === "embedding" || page === "rerank") {
    const label = { llm: "LLM", embedding: "Embedding", rerank: "Cross-Encoder" }[page];
    // On detail (params.id present), ProvidersPage renders its own page
    // header (crumb + mono id + Invalidate/Delete/Back actions). Leave
    // pageHeader null so we don't double-render.
    if (!params.id) {
      const pluralPath = page === "rerank" ? "cross_encoder_providers" : `${page}_providers`;
      pageHeader = (
        <>
          <div>
            <div className="crumb">
              <a onClick={() => navigate("dashboard")}>Providers</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>{label}</span>
            </div>
            <h1 className="page-title">{label} providers</h1>
            <div className="page-sub">Backed by <span className="mono">/v1/{pluralPath}</span></div>
          </div>
        </>
      );
    }
    pageBody = <ProvidersPage kind={page} sessions={sessions} pushToast={pushToast} />;
  } else if (page === "toolsets-user") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>Toolsets</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>User toolsets</span>
          </div>
          <h1 className="page-title">User toolsets</h1>
          <div className="page-sub">MCP servers, HTTP-based tools, OpenAPI imports</div>
        </div>
      </>
    );
    pageBody = <ToolsetsPage kind="user" pushToast={pushToast} />;
  } else if (page === "toolsets-builtin") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>Toolsets</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Built-in</span>
          </div>
          <h1 className="page-title">Built-in toolsets</h1>
          <div className="page-sub">Runtime-provided primitives</div>
        </div>
      </>
    );
    pageBody = <ToolsetsPage kind="builtin" pushToast={pushToast} />;
  } else if (page === "toolset-detail" && currentToolsetId) {
    pageHeader = (
      <>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="crumb">
            <a onClick={() => navigate("toolsets-user")}>Toolsets</a>
            <span className="sep">/</span>
            <span className="mono" style={{ color: "var(--text)" }}>{currentToolsetId}</span>
          </div>
          <h1 className="page-title mono">{currentToolsetId}</h1>
        </div>
        <div className="page-actions">
          <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("toolsets-user")}>Back</Btn>
        </div>
      </>
    );
    pageBody = (
      <ToolsetDetail
        toolsetId={currentToolsetId}
        pushToast={pushToast}
      />
    );
  } else if (page === "collections") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>Knowledge</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Collections</span>
          </div>
          <h1 className="page-title">Collections</h1>
          <div className="page-sub">Vector stores · each bound to one embedding provider</div>
        </div>
      </>
    );
    pageBody = (
      <CollectionsPage
        ssps={ssps}
        ssmState={tweaks.ssmState}
        onOpen={(cid) => navigate("documents", cid)}
        onSearchCollection={(cid) => navigate("collection-search", cid)}
        onNavigate={navigate}
      />
    );
  } else if (page === "documents") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>Knowledge</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Documents</span>
          </div>
          <h1 className="page-title">Documents</h1>
          <div className="page-sub">Ingested docs across all collections</div>
        </div>
      </>
    );
    pageBody = (
      <DocumentsPage
        filterCollection={docsFilterCollection}
        onClearFilter={() => setDocsFilterCollection("")}
      />
    );
  } else if (page === "collection-search" && !docsFilterCollection) {
    // No collection scoped — render the unscoped SearchBench so deep-linking
    // to `#/knowledge/search` lands on the entity search probe page.
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>Knowledge</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Search</span>
          </div>
          <h1 className="page-title">Entity search probe</h1>
          <div className="page-sub">Try a query against any collection or the internal agent index</div>
        </div>
      </>
    );
    pageBody = <SearchBench subsystemOn={subsystemOn} />;
  } else if (page === "collection-search" && docsFilterCollection) {
    const col = (window.COLLECTIONS_INDEX || []).find((c) => c.id === docsFilterCollection);
    pageHeader = (
      <>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="crumb">
            <a onClick={() => navigate("collections")}>Collections</a>
            <span className="sep">/</span>
            <span className="mono" style={{ color: "var(--text)" }}>{docsFilterCollection}</span>
            <span className="sep">/</span>
            <span style={{ color: "var(--text)" }}>Search</span>
          </div>
          <h1 className="page-title">
            Search <span className="mono" style={{ color: "var(--text-3)", fontSize: 18, fontWeight: 500 }}>· {docsFilterCollection}</span>
          </h1>
          {col && (
            <div className="page-sub tabular">
              <span className="mono">{col.docs.toLocaleString()}</span> docs ·
              <span className="mono"> {col.chunks.toLocaleString()}</span> chunks ·
              embedder <span className="mono">{col.embedding_provider}</span>
            </div>
          )}
        </div>
        <div className="page-actions">
          <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("collections")}>Back to collections</Btn>
        </div>
      </>
    );
    pageBody = <SearchBench subsystemOn={subsystemOn} collectionId={docsFilterCollection} />;
  } else if (page === "graphs") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>Compute</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Graphs</span>
          </div>
          <h1 className="page-title">Graphs</h1>
          <div className="page-sub">Multi-agent flows · executor not yet shipped</div>
        </div>
      </>
    );
    pageBody = <GraphsPage onOpen={(gid) => navigate("graph-detail", gid)} />;
  } else if (page === "graph-detail" && currentGraphId) {
    pageHeader = (
      <>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="crumb">
            <a onClick={() => navigate("graphs")}>Graphs</a>
            <span className="sep">/</span>
            <span className="mono" style={{ color: "var(--text)" }}>{currentGraphId}</span>
          </div>
          <h1 className="page-title mono">{currentGraphId}</h1>
        </div>
        <div className="page-actions">
          <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("graphs")}>Back</Btn>
        </div>
      </>
    );
    pageBody = <GraphDetail graphId={currentGraphId} pushToast={pushToast} />;
  } else if (page === "agents") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>Compute</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Agents</span>
          </div>
          <h1 className="page-title">Agents</h1>
          <div className="page-sub tabular">{window.MOCK.AGENTS.length} agents · 1 with unresolved references</div>
        </div>
      </>
    );
    pageBody = (
      <AgentsPage
        onOpen={(aid) => navigate("agent-detail", aid)}
        pushToast={pushToast}
      />
    );
  } else if (page === "agent-detail" && currentAgentId) {
    pageHeader = (
      <>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="crumb">
            <a onClick={() => navigate("agents")}>Agents</a>
            <span className="sep">/</span>
            <span className="mono" style={{ color: "var(--text)" }}>{currentAgentId}</span>
          </div>
          <h1 className="page-title mono">{currentAgentId}</h1>
        </div>
        <div className="page-actions">
          <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("agents")}>Back</Btn>
        </div>
      </>
    );
    pageBody = (
      <AgentDetail
        agentId={currentAgentId}
        pushToast={pushToast}
      />
    );
  } else if (page === "workspaces") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>matrix</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Workspaces</span>
          </div>
          <h1 className="page-title">Workspaces</h1>
          <div className="page-sub tabular">
            {window.MOCK.WORKSPACES.length} workspaces · <span className="mono" style={{ color: "var(--blue)" }}>{sessions.filter((s) => !["ended","failed","cancelled"].includes(s.status)).length}</span> active sessions
          </div>
        </div>
      </>
    );
    pageBody = (
      <WorkspacesPage
        onOpen={(wid) => navigate("workspace-detail", wid)}
        pushToast={pushToast}
      />
    );
  } else if (page === "workspace-detail" && currentWorkspaceId) {
    pageHeader = (
      <>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="crumb">
            <a onClick={() => navigate("workspaces")}>Workspaces</a>
            <span className="sep">/</span>
            <span className="mono" style={{ color: "var(--text)" }}>{currentWorkspaceId}</span>
          </div>
          <h1 className="page-title mono">{currentWorkspaceId}</h1>
          <div className="page-sub">
            <span className="mono">{(window.MOCK.WORKSPACE_DETAILS[currentWorkspaceId] || {}).template}</span> ·
            <span className="muted"> created {relativeTime(((window.MOCK.WORKSPACE_DETAILS[currentWorkspaceId] || {}).created_at_ago || 0) / 1000)}</span>
          </div>
        </div>
        <div className="page-actions">
          <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("workspaces")}>Back to list</Btn>
        </div>
      </>
    );
    pageBody = (
      <WorkspaceDetail
        workspaceId={currentWorkspaceId}
        sessions={sessions}
        onOpenSession={(sid) => navigate("session-detail", sid)}
        onNavigate={navigate}
        pushToast={pushToast}
      />
    );
  } else if (page === "health") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>Operations</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Health</span>
          </div>
          <h1 className="page-title">Health</h1>
          <div className="page-sub">Live <span className="mono">/v1/health</span> · poll every 5s · client-side history</div>
        </div>
      </>
    );
    pageBody = <HealthPage workerStats={workerStats} sessions={sessions} />;
  } else if (page === "workers") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a onClick={() => navigate("dashboard")}>Operations</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Workers</span>
          </div>
          <h1 className="page-title">Workers</h1>
          <div className="page-sub tabular">
            {workerStats.total} workers · <span className="mono" style={{ color: "var(--blue)" }}>{workerStats.in_flight}</span>/{workerStats.capacity} in flight ·
            <span className="mono" style={{ marginLeft: 4, color: "var(--text-3)" }}>autorefresh every 2s</span>
          </div>
        </div>
        <div className="page-actions">
          <Btn icon="refresh" kind="ghost">Refresh</Btn>
        </div>
      </>
    );
    pageBody = (
      <WorkersPage sessions={sessions} pushToast={pushToast} />
    );
  } else if (page === "dashboard") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <span style={{ color: "var(--text)" }}>Dashboard</span>
          </div>
          <h1 className="page-title">Dashboard</h1>
          <div className="page-sub">Operator overview · <span className="mono">matrix · localhost:8765</span></div>
        </div>
        <div className="page-actions">
          <Btn icon="external" kind="ghost">View OpenAPI</Btn>
          <Btn icon="plus" kind="primary" onClick={() => setNewSessionOpen(true)}>New session</Btn>
        </div>
      </>
    );
    pageBody = (
      <Dashboard
        sessions={sessions}
        workerStats={workerStats}
        subsystemOn={subsystemOn}
        onNavigate={navigate}
        onNewSession={() => setNewSessionOpen(true)}
      />
    );
  } else if (page === "sessions") {
    pageHeader = (
      <>
        <div>
          <div className="crumb">
            <a>Operations</a><span className="sep">/</span><span style={{ color: "var(--text)" }}>Sessions</span>
          </div>
          <h1 className="page-title">Sessions</h1>
          <div className="page-sub tabular">
            <span className="mono" style={{ color: "var(--blue)" }}>● {sessions.filter((s) => s.status === "running" || s.status === "paused").length}</span> live ·{" "}
            {counts.sessions} active · {sessions.length} total ·
            <span className="mono" style={{ marginLeft: 4, color: "var(--text-3)" }}>autorefresh every 3s</span>
          </div>
        </div>
        <div className="page-actions">
          <Btn icon="refresh" kind="ghost">Refresh</Btn>
          <Btn icon="plus" kind="primary" onClick={() => setNewSessionOpen(true)}>New session</Btn>
        </div>
      </>
    );
    pageBody = (
      <SessionsList
        sessions={sessions}
        onOpenSession={openSession}
        onNewSession={() => setNewSessionOpen(true)}
        demoState={tweaks.demoState === "empty" ? "empty" : tweaks.demoState === "loading" ? "loading" : tweaks.demoState === "error-list" ? "error" : null}
      />
    );
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
            The focus pages are <a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => navigate("sessions")}>Sessions</a>,{" "}
            <a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => navigate("collections")}>Collections</a>, and the session control room.
            Other entities follow the same patterns described in §4 of the spec.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={`app ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      <Topbar workerStats={workerStats} onNavigate={navigate} onOpenPalette={() => setPaletteOpen(true)} />
      <Sidebar
        page={
          page === "session-detail" ? "sessions"
          : page === "workspace-detail" ? "workspaces"
          : page === "agent-detail" ? "agents"
          : page === "graph-detail" ? "graphs"
          : page === "ssp-detail" ? "semantic-search"
          : page === "collection-search" ? "collections"
          : page === "chat-detail" ? "chats"
          : page === "channel-provider-detail" ? "channel-providers"
          : page === "toolset-detail" ? "toolsets-user"
          : page
        }
        onNavigate={navigate}
        counts={counts}
        subsystemOn={subsystemOn}
        collapsed={sidebarCollapsed}
        onCollapseToggle={toggleSidebar}
      />
      <main className="main">
        <div className="page-header">
          {pageHeader}
        </div>
        <div className="page-body">
          {pageBody}
        </div>
      </main>

      {/* Toasts */}
      <div className="toast-stack">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind || "info"}`}>
            <Icon
              name={t.kind === "success" ? "check-circle" : t.kind === "error" ? "x-circle" : t.kind === "warning" ? "alert" : "info"}
              size={14}
              className="ico"
            />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="title">{t.title}</div>
              {t.detail && <div className="detail">{t.detail}</div>}
              {t.reqId && (
                <div className="req-id">
                  request-id <span style={{ color: "var(--text)" }}>{t.reqId}</span>{" · "}
                  <a>copy</a>
                </div>
              )}
            </div>
            <button className="close" onClick={() => removeToast(t.id)}><Icon name="x" size={12} /></button>
          </div>
        ))}
      </div>

      {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} onNavigate={navigate} sessions={sessions} />}

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
        <TweakSection label="Demo state" />
          <TweakRadio
            label="Internal Collections"
            value={tweaks.subsystemOn ? "on" : "off"}
            onChange={(v) => setTweak("subsystemOn", v === "on")}
            options={[{ value: "off", label: "Off" }, { value: "on", label: "On" }]}
          />
          <TweakRadio
            label="SSPs configured"
            value={tweaks.ssmState}
            onChange={(v) => setTweak("ssmState", v)}
            options={[
              { value: "none", label: "None" },
              { value: "one-pgvector", label: "One" },
              { value: "many", label: "Many" },
            ]}
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

// Renders the page-sub status pill on the session-detail page header.
// Subscribes to the same `session-detail:${sid}` cache key as SessionDetail
// so it reflects the polled status without a duplicate network call.
function SessionStatusCaption({ sid }) {
  const { useResource, apiFetch } = window.matrixApi;
  const detail = useResource(
    `session-detail:${sid}`,
    (signal) => apiFetch("GET", `/sessions/${encodeURIComponent(sid)}`, null, { signal }),
    { pollMs: 0, deps: [sid] }
  );
  const status = detail.data?.status;
  if (!status) return <div className="page-sub" />;
  const bound = detail.data?.binding?.agent_id || detail.data?.binding?.graph_id;
  const kind = detail.data?.binding?.kind || (detail.data?.binding?.graph_id ? "graph" : "agent");
  return (
    <div className="page-sub tabular">
      <StatusPill status={status} />
      {bound && <span style={{ marginLeft: 8 }} className="mono muted">{kind} {bound}</span>}
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
    search: "Search test bench",
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

function NewSessionModal({ onClose, onCreate }) {
  const [kind, setKind] = React.useState("agent");
  const [agentId, setAgentId] = React.useState(window.MOCK.AGENTS[0].id);
  const [graphId, setGraphId] = React.useState(window.MOCK.GRAPHS[0]);
  const [workspace, setWorkspace] = React.useState(window.MOCK.WORKSPACES[0]);
  const [instructions, setInstructions] = React.useState("");
  return (
    <Modal
      title="New session"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={onCreate} disabled={!instructions.trim()}>Create</Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">Binding</label>
        <div className="chip-group" style={{ display: "inline-flex" }}>
          <span className={`chip ${kind === "agent" ? "active" : ""}`} onClick={() => setKind("agent")}>agent</span>
          <span className={`chip ${kind === "graph" ? "active" : ""}`} onClick={() => setKind("graph")}>graph</span>
        </div>
      </div>
      <div className="field">
        <label className="field-label">{kind === "agent" ? "Agent" : "Graph"}</label>
        {kind === "agent" ? (
          <select className="select" value={agentId} onChange={(e) => setAgentId(e.target.value)} style={{ width: "100%" }}>
            {window.MOCK.AGENTS.map((a) => <option key={a.id} value={a.id}>{a.id}</option>)}
          </select>
        ) : (
          <>
            <select className="select" value={graphId} onChange={(e) => setGraphId(e.target.value)} style={{ width: "100%" }}>
              {window.MOCK.GRAPHS.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
            <div className="field-help warn"><Icon name="alert" size={11} /> Graph executor is unimplemented — this session will end with <span className="mono">failed</span> on turn 1.</div>
          </>
        )}
      </div>
      <div className="field">
        <label className="field-label">Workspace</label>
        <select className="select" value={workspace} onChange={(e) => setWorkspace(e.target.value)} style={{ width: "100%" }}>
          {window.MOCK.WORKSPACES.map((w) => <option key={w} value={w}>{w}</option>)}
        </select>
      </div>
      <div className="field">
        <label className="field-label">Initial instructions</label>
        <textarea className="textarea" value={instructions} onChange={(e) => setInstructions(e.target.value)} rows={4} placeholder="Tell the agent what to do…" />
      </div>
    </Modal>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
