/* global React, Icon */

const NAV = [
  {
    group: null,
    items: [
      { id: "dashboard", label: "Dashboard", icon: "home" },
      { id: "sessions", label: "Sessions", icon: "zap", countKey: "sessions" },
    ],
  },
  {
    group: "Compute",
    items: [
      { id: "agents", label: "Agents", icon: "agent" },
      { id: "graphs", label: "Graphs", icon: "graph" },
      { id: "chats", label: "Chats", icon: "send", countKey: "chats" },
    ],
  },
  {
    group: "Knowledge",
    items: [
      { id: "collections", label: "Collections", icon: "collection" },
      { id: "documents", label: "Documents", icon: "doc" },
    ],
  },
  {
    group: "Workspaces",
    items: [
      { id: "workspaces", label: "Workspaces", icon: "box", countKey: "workspaces" },
      { id: "workspace-templates", label: "Templates", icon: "tools" },
      { id: "workspace-providers", label: "Providers", icon: "box" },
    ],
  },
  {
    group: "Toolsets",
    items: [
      { id: "toolsets-user", label: "User toolsets", icon: "tools" },
      { id: "toolsets-builtin", label: "Built-in", icon: "tools" },
    ],
  },
  {
    group: "Providers",
    items: [
      { id: "llm", label: "LLM", icon: "llm" },
      { id: "embedding", label: "Embedding", icon: "emb" },
      { id: "rerank", label: "Cross-Encoder", icon: "emb" },
      { id: "semantic-search", label: "Semantic Search", icon: "subsystem", countKey: "ssps" },
    ],
  },
  {
    group: "Channels",
    items: [
      { id: "channel-providers", label: "Providers", icon: "bell" },
      { id: "channels", label: "Channels", icon: "bell", countKey: "channels" },
      { id: "channel-associations", label: "Associations", icon: "fork" },
    ],
  },
  {
    group: "Distributions",
    items: [{ id: "harnesses", label: "Harnesses", icon: "box" }],
  },
  {
    group: "Subsystems",
    items: [
      { id: "internal-collections", label: "Internal Collections", icon: "subsystem", subsystem: true },
      { id: "approvals", label: "Approvals", icon: "check-circle", countKey: "approvals_pending" },
    ],
  },
  {
    group: "Operations",
    items: [
      { id: "workers", label: "Workers", icon: "worker", countKey: "workers" },
      { id: "health", label: "Health", icon: "heart" },
    ],
  },
];

function Sidebar({ page, onNavigate, counts, subsystemOn, collapsed: navCollapsed, onCollapseToggle }) {
  const [collapsed, setCollapsed] = React.useState(() => {
    try { return JSON.parse(localStorage.getItem("primer.sidebar.collapsed") || "{}"); } catch { return {}; }
  });
  const toggle = (g) => {
    const next = { ...collapsed, [g]: !collapsed[g] };
    setCollapsed(next);
    try { localStorage.setItem("primer.sidebar.collapsed", JSON.stringify(next)); } catch {}
  };
  return (
    <aside className={`sidebar ${navCollapsed ? "is-collapsed" : ""}`}>
      {NAV.map((section, i) => (
        <div key={i} className={`nav-section ${section.group && collapsed[section.group] ? "collapsed" : ""}`}>
          {section.group && (
            <div className="nav-group" onClick={() => toggle(section.group)}>
              <Icon name="chevron-down" size={10} className="chevron" />
              <span>{section.group}</span>
            </div>
          )}
          <div className="nav-items">
            {section.items.map((it) => (
              <div
                key={it.id}
                className={`nav-item ${page === it.id ? "active" : ""}`}
                onClick={() => onNavigate(it.id)}
              >
                <Icon name={it.icon} className="icon" />
                <span className="label">{it.label}</span>
                {it.subsystem && (
                  <span className={subsystemOn ? "nav-pill-on" : "nav-pill-off"}>{subsystemOn ? "ON" : "OFF"}</span>
                )}
                {it.countKey != null && counts[it.countKey] != null && (
                  <span className="count">{counts[it.countKey]}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      ))}
      <div className="sidebar-foot" onClick={onCollapseToggle} title={navCollapsed ? "Expand sidebar" : "Collapse sidebar"}>
        <Icon name="panel-left" size={13} />
        <span>{navCollapsed ? "Expand sidebar" : "Collapse sidebar"}</span>
      </div>
    </aside>
  );
}

function Topbar({ workerStats, onNavigate, onOpenPalette }) {
  const totalCap = workerStats.capacity;
  const inFlight = workerStats.in_flight;
  const active = workerStats.active;
  let pillClass = "";
  if (active === 0) pillClass = "err";
  else if (inFlight >= totalCap * 0.8) pillClass = "warn";

  // IC bell: only render when the subsystem is configured-but-not-active
  // (the "bootstrap required" state). Hidden when IC is OFF or fully active.
  // Polls GET /v1/internal_collections/config (404 → OFF, 200 with
  // activated_at null → configured, 200 with activated_at set → active).
  const { useResource } = window.primerApi || {};
  const icProbe = useResource
    ? useResource(
        "chrome:ic-config",
        async (signal) => {
          try {
            return await window.primerApi.apiFetch("GET", "/internal_collections/config", null, { signal });
          } catch (e) {
            if (e && e.status === 404) return null;
            throw e;
          }
        },
        { pollMs: 30000 }
      )
    : { data: null, error: null };
  const icBootstrapRequired = icProbe.data != null && !icProbe.data.activated_at;

  return (
    <header className="topbar">
      <div className="topbar-brand">
        <div className="logo" aria-label="primer">
          <svg viewBox="0 0 24 24" width="22" height="22" role="img">
            <polygon points="12,3 21,12 12,21 3,12" fill="currentColor" fillOpacity="0.18"/>
            <polygon points="12,3 16.5,7.5 12,12 7.5,7.5" fill="currentColor"/>
            <polygon points="16.5,7.5 21,12 16.5,16.5 12,12" fill="currentColor" fillOpacity="0.45"/>
            <polygon points="12,12 16.5,16.5 12,21 7.5,16.5" fill="var(--accent)"/>
            <polygon points="7.5,7.5 12,12 7.5,16.5 3,12" fill="currentColor" fillOpacity="0.45"/>
          </svg>
        </div>
        <div>
          <div className="name">primer</div>
        </div>
        <div className="instance">· localhost:8765</div>
      </div>
      <div className="topbar-search" onClick={onOpenPalette}>
        <Icon name="search" size={13} />
        <span>Search…</span>
        <kbd>⌘K</kbd>
      </div>
      <div className="topbar-right">
        <div className={`worker-pill ${pillClass}`} onClick={() => onNavigate("workers")} title="Worker pool · click to view">
          <span className="dot"></span>
          <span className={inFlight >= totalCap * 0.8 ? "num-warn" : ""}>{active}/{workerStats.total}</span>
          <span>workers</span>
          <span className="sep">·</span>
          <span>{inFlight} in flight</span>
        </div>
        {icBootstrapRequired && (
          <button
            className="icon-btn warn"
            title="Internal Collections subsystem: bootstrap required"
            aria-label="Internal Collections subsystem: bootstrap required"
            onClick={() => onNavigate("internal-collections")}
          >
            <Icon name="bell" size={14} />
          </button>
        )}
        <UserMenu />
      </div>
    </header>
  );
}


function UserMenu() {
  // Pulls /v1/auth/status to surface the logged-in username + offer a
  // logout button. The signed session cookie carries the identity; the
  // backend re-fetches the user row on every request.
  const [open, setOpen] = React.useState(false);
  const [status, setStatus] = React.useState(null);

  const refresh = React.useCallback(async () => {
    try {
      const r = await window.primerApi.apiFetch("GET", "/auth/status", null, {});
      setStatus(r);
    } catch {
      setStatus({ authenticated: false, username: null });
    }
  }, []);

  React.useEffect(() => { refresh(); }, [refresh]);

  const initials = (u) =>
    u ? u.split(/[^a-z0-9]+/i).filter(Boolean).slice(0, 2).map(s => s[0].toUpperCase()).join("") || u[0].toUpperCase() : "?";

  const onLogout = async () => {
    try {
      await window.primerApi.apiFetch("POST", "/auth/logout", null, {});
    } catch {}
    // Reload — root redirects to /login because the session cookie
    // is cleared and auth/status will report authenticated=false.
    window.location.reload();
  };

  if (!status || !status.authenticated) {
    return <div className="user-avatar mono" title="not authenticated">?</div>;
  }

  return (
    <div style={{ position: "relative" }}>
      <div
        className="user-avatar mono"
        title={status.username}
        onClick={() => setOpen(o => !o)}
        style={{ cursor: "pointer" }}
      >
        {initials(status.username)}
      </div>
      {open && (
        <div
          style={{
            position: "absolute", right: 0, top: "calc(100% + 6px)",
            background: "var(--surface)", border: "1px solid var(--border)",
            borderRadius: 6, padding: 8, minWidth: 160, zIndex: 100,
            boxShadow: "0 4px 12px rgba(0,0,0,0.15)",
          }}
        >
          <div style={{ fontSize: 12, color: "var(--text-2)", padding: "4px 6px" }}>
            Signed in as
          </div>
          <div className="mono" style={{ padding: "0 6px 6px", fontWeight: 600 }}>
            {status.username}
          </div>
          <div style={{ borderTop: "1px solid var(--border)", margin: "4px -8px" }} />
          <button
            className="btn btn-sm"
            style={{ width: "100%", justifyContent: "flex-start" }}
            onClick={onLogout}
          >
            Log out
          </button>
        </div>
      )}
    </div>
  );
}

function CommandPalette({ onClose, onNavigate, sessions }) {
  const [q, setQ] = React.useState("");
  const [active, setActive] = React.useState(0);
  const inputRef = React.useRef(null);
  React.useEffect(() => { inputRef.current && inputRef.current.focus(); }, []);

  const ql = q.toLowerCase();
  const matches = [
    ...["dashboard", "sessions", "workspaces", "workspace-templates", "workspace-providers", "agents", "graphs", "collections", "documents", "toolsets-user", "toolsets-builtin", "llm", "embedding", "rerank", "semantic-search", "internal-collections", "workers", "health"]
      .filter((p) => p.includes(ql))
      .map((p) => ({ kind: "page", id: p, label: `Go to ${p.replace(/-/g, " ").replace(/^\w/, (c) => c.toUpperCase())}` })),
    ...sessions
      .filter((s) => s.id.toLowerCase().includes(ql) || (s.agent_id && s.agent_id.toLowerCase().includes(ql)))
      .slice(0, 6)
      .map((s) => ({ kind: "session", id: s.id, label: s.id, sub: s.agent_id || s.graph_id, status: s.status })),
  ].slice(0, 12);

  React.useEffect(() => { setActive(0); }, [q]);

  React.useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(matches.length - 1, a + 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
      else if (e.key === "Enter" && matches[active]) {
        e.preventDefault();
        const m = matches[active];
        onClose();
        onNavigate(m.kind === "session" ? "session-detail" : m.id, m.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, matches, active, onNavigate]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal"
        style={{ width: 540, alignSelf: "flex-start", marginTop: "12vh" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", borderBottom: "1px solid var(--border)" }}>
          <Icon name="search" size={16} />
          <input
            ref={inputRef}
            className="input"
            style={{ border: "none", flex: 1, padding: 0, background: "transparent", fontSize: 14 }}
            placeholder="Go to page or session id…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <kbd style={{ fontSize: 10, padding: "2px 6px", border: "1px solid var(--border)", borderRadius: 4, color: "var(--text-3)" }}>esc</kbd>
        </div>
        <div style={{ maxHeight: 360, overflow: "auto", padding: 4 }}>
          {matches.length === 0 ? (
            <div style={{ padding: 16, color: "var(--text-3)", fontSize: 12.5, textAlign: "center" }}>No matches</div>
          ) : (
            matches.map((m, i) => (
              <div
                key={i}
                className={`nav-item ${i === active ? "active" : ""}`}
                style={{ borderRadius: 6, margin: 2 }}
                onMouseEnter={() => setActive(i)}
                onClick={() => { onClose(); onNavigate(m.kind === "session" ? "session-detail" : m.id, m.id); }}
              >
                <Icon name={m.kind === "session" ? "zap" : "chevron-right"} className="icon" size={13} />
                <span className="label mono" style={{ fontSize: 12.5 }}>{m.label}</span>
                {m.sub && <span className="muted mono" style={{ fontSize: 11 }}>{m.sub}</span>}
              </div>
            ))
          )}
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", padding: "8px 14px", borderTop: "1px solid var(--border)", gap: 14, fontSize: 11, color: "var(--text-3)" }}>
          <span><kbd style={{ fontFamily: "IBM Plex Mono", padding: "0 4px", background: "var(--bg-2)", borderRadius: 3 }}>↑↓</kbd> navigate</span>
          <span><kbd style={{ fontFamily: "IBM Plex Mono", padding: "0 4px", background: "var(--bg-2)", borderRadius: 3 }}>↵</kbd> select</span>
          <span><kbd style={{ fontFamily: "IBM Plex Mono", padding: "0 4px", background: "var(--bg-2)", borderRadius: 3 }}>esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { Sidebar, Topbar, CommandPalette });
