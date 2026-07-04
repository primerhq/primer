/* global React, Icon, useTweaks, setTweak */

const NAV = [
  {
    group: null,
    items: [
      { id: "dashboard", label: "Dashboard", icon: "home" },
      { id: "studio", label: "Studio", icon: "panel-left" },
    ],
  },
  {
    group: "Compute",
    items: [
      { id: "agents", label: "Agents", icon: "agent" },
      { id: "graphs", label: "Graphs", icon: "graph" },
      { id: "chats", label: "Chats", icon: "send", countKey: "chats" },
      { id: "approvals", label: "Approvals", icon: "check-circle", countKey: "approvals_pending" },
    ],
  },
  {
    group: "Knowledge",
    items: [
      { id: "collections", label: "Collections", icon: "collection" },
      { id: "documents", label: "Documents", icon: "doc" },
      { id: "internal-collections", label: "Internal Collections", icon: "subsystem", subsystem: true },
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
    group: "Web",
    items: [
      { id: "web-search", label: "Web Search", icon: "search" },
    ],
  },
  {
    group: "Toolsets",
    items: [
      { id: "toolsets", label: "Toolsets", icon: "tools" },
      { id: "tools", label: "Tools", icon: "tools" },
    ],
  },
  {
    group: "Providers",
    items: [
      { id: "llm", label: "LLM", icon: "llm" },
      { id: "embedding", label: "Embedding", icon: "emb" },
      { id: "rerank", label: "Cross-Encoder", icon: "emb" },
      { id: "semantic-search", label: "Semantic Search", icon: "subsystem", countKey: "ssps" },
      { id: "channel-providers", label: "Channels", icon: "bell" },
    ],
  },
  {
    group: "Communication",
    items: [
      { id: "channels", label: "Channels", icon: "bell", countKey: "channels" },
      { id: "channel-rules", label: "Rules", icon: "filter" },
    ],
  },
  {
    group: "Distributions",
    items: [{ id: "harnesses", label: "Harnesses", icon: "box" }],
  },
  {
    group: "Automation",
    items: [
      { id: "triggers", label: "Triggers", icon: "clock" },
    ],
  },
  {
    group: "Operations",
    items: [
      { id: "workers", label: "Workers", icon: "worker", countKey: "workers" },
      { id: "health", label: "Health", icon: "heart" },
    ],
  },
  {
    group: "Account",
    items: [
      { id: "admin-users", label: "Users", icon: "user" },
      { id: "api-tokens", label: "API Tokens", icon: "key" },
      { id: "mcp", label: "MCP Server", icon: "code" },
    ],
  },
  {
    group: "Help",
    items: [
      // Docs live on a standalone static site now. The URL is surfaced by
      // the server into window.__PRIMER_DOCS_URL__ (see primer.api.app
      // _install_jsx_bundle); fall back to "#" if it is somehow unset.
      {
        id: "docs",
        label: "Docs",
        icon: "doc",
        href: (typeof window !== "undefined" && window.__PRIMER_DOCS_URL__) || "#",
      },
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
              it.href ? (
                <a
                  key={it.id}
                  className="nav-item"
                  href={it.href}
                  target="_blank"
                  rel="noopener"
                >
                  <Icon name={it.icon} className="icon" />
                  <span className="label">{it.label}</span>
                  <Icon name="external" className="icon" size={12} />
                </a>
              ) : (
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
              )
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

// drawerOpen state is owned by the App shell in app.jsx — see Task 4.2.
function MobileNav({ open, onClose, ...sidebarProps }) {
  React.useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === "Escape") onClose && onClose(); };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="drawer-overlay" onClick={onClose}>
      <aside
        className="drawer open"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <Sidebar {...sidebarProps} />
      </aside>
    </div>
  );
}

function Topbar({ workerStats, onNavigate, onOpenPalette, onOpenDrawer }) {
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
  // Shares the canonical "ic:config" cacheKey with app.jsx (sidebar /
  // dashboard) and the Internal Collections page so useResource dedupes
  // the identical 30s probe into a single background request.
  const icProbe = useResource
    ? useResource(
        "ic:config",
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

  // Instance suffix in the brand — driven by the operator-set tweak
  // (tweaks.instanceLabel, e.g. "primer · localhost:8765") instead of a
  // hardcoded literal. The brand already renders "primer" as the name, so we
  // show the trailing host segment after the "·"; fall back to the current
  // host when the label is unset/empty.
  const [tweaks] = useTweaks();
  const rawInstanceLabel = (tweaks && tweaks.instanceLabel) || "";
  let instanceText = rawInstanceLabel;
  if (rawInstanceLabel.indexOf("·") !== -1) {
    instanceText = rawInstanceLabel.split("·").pop().trim();
  }
  if (!instanceText) {
    try { instanceText = window.location.host; } catch (_e) { instanceText = ""; }
  }

  return (
    <header className="topbar">
      <button
        className="hamburger touch-target mobile-only"
        aria-label="Open navigation"
        onClick={onOpenDrawer}
      >
        <Icon name="panel-left" size={18} />
      </button>
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
        <div className="instance" data-testid="topbar-instance">· {instanceText}</div>
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
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}


function ThemeToggle() {
  // Operator-facing light/dark switch. Reads + writes the same tweaks
  // store the (design-only, hidden in production) TweaksPanel uses,
  // so the existing app.jsx effect that sets
  // `document.documentElement.data-theme` keeps driving the CSS
  // variable swap. No new wiring path: just a visible affordance for
  // the existing mechanism.
  const [tweaks, setTweak] = useTweaks();
  const isDark = (tweaks?.theme || "dark") !== "light";
  const toggle = () => setTweak("theme", isDark ? "light" : "dark");
  return (
    <button
      className="icon-btn"
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
      aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
      onClick={toggle}
    >
      <Icon name={isDark ? "sun" : "moon"} size={14} />
    </button>
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

// Flat, search-friendly page list derived from NAV so the palette
// stays in lockstep with whatever the sidebar advertises. Each entry
// carries: id (used by navigate), label (the display string), group
// (the sidebar section name, also searchable), and an icon for the
// row. Adding a new page to NAV automatically surfaces it here.
const NAV_PAGES = (() => {
  const out = [];
  for (const section of NAV) {
    for (const item of section.items || []) {
      out.push({
        id: item.id,
        label: item.label,
        group: section.group || "",
        icon: item.icon || "chevron-right",
      });
    }
  }
  return out;
})();


function CommandPalette({ onClose, onNavigate, sessions }) {
  const [q, setQ] = React.useState("");
  const [active, setActive] = React.useState(0);
  const inputRef = React.useRef(null);
  React.useEffect(() => { inputRef.current && inputRef.current.focus(); }, []);

  const ql = q.toLowerCase().trim();

  // Pages: match against id, label, AND group so "compute" surfaces
  // sessions/agents/graphs/chats, "tokens" surfaces API tokens, etc.
  // No query → show every page so the palette doubles as a directory.
  const pageHits = NAV_PAGES.filter((p) => {
    if (!ql) return true;
    return (
      p.id.toLowerCase().includes(ql)
      || p.label.toLowerCase().includes(ql)
      || (p.group && p.group.toLowerCase().includes(ql))
    );
  }).map((p) => ({
    kind: "page",
    id: p.id,
    label: p.label,
    sub: p.group || null,
    icon: p.icon,
  }));

  // Sessions: only when a query is typed (otherwise empty queries
  // would dump the entire session list above the page list).
  const sessionHits = !ql
    ? []
    : (sessions || [])
        .filter((s) => s.id.toLowerCase().includes(ql) || (s.agent_id && s.agent_id.toLowerCase().includes(ql)))
        .slice(0, 6)
        .map((s) => ({
          kind: "session",
          id: s.id,
          label: s.id,
          sub: s.agent_id || s.graph_id || null,
          status: s.status,
          icon: "zap",
        }));

  const matches = [...pageHits, ...sessionHits].slice(0, 20);

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
        if (m.kind === "session") {
          onNavigate("session-detail", m.id);
        } else {
          onNavigate(m.id);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, matches, active, onNavigate]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal cmd-palette"
        style={{ width: 540, alignSelf: "flex-start", marginTop: "12vh" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", borderBottom: "1px solid var(--border)" }}>
          <Icon name="search" size={16} />
          <input
            ref={inputRef}
            className="input"
            style={{ border: "none", flex: 1, padding: 0, background: "transparent", fontSize: 14 }}
            placeholder="Search pages, sections, or session ids…"
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
                onClick={() => {
                  onClose();
                  if (m.kind === "session") {
                    onNavigate("session-detail", m.id);
                  } else {
                    onNavigate(m.id);
                  }
                }}
              >
                <Icon name={m.icon || (m.kind === "session" ? "zap" : "chevron-right")} className="icon" size={13} />
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

Object.assign(window, { Sidebar, MobileNav, Topbar, CommandPalette });
