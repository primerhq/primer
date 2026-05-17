/* global React, Icon */

// Chrome — sidebar, topbar, command palette, toast container.
//
// As of Milestone 2 these read from foundation hooks directly rather
// than props passed from app.jsx. Parent only owns the sidebar
// collapse mode and the palette open/close state.

const _api = window.matrixApi;
const { apiFetch, useResource, useMutation, useRouter, useToast, useTweaks } = _api;

// Navigation table — `route` is the canonical hash path; `id` is the
// legacy string used by app.jsx's PATH_MAP, kept for places that still
// use the string form.
const NAV = [
  {
    group: null,
    items: [
      { id: "dashboard", label: "Dashboard", icon: "home", route: "/" },
      { id: "sessions", label: "Sessions", icon: "zap", route: "/sessions", count: "sessions" },
      { id: "workspaces", label: "Workspaces", icon: "box", route: "/workspaces", count: "workspaces" },
    ],
  },
  {
    group: "Compute",
    items: [
      { id: "agents", label: "Agents", icon: "agent", route: "/agents" },
      { id: "graphs", label: "Graphs", icon: "graph", route: "/graphs" },
    ],
  },
  {
    group: "Knowledge",
    items: [
      { id: "collections", label: "Collections", icon: "collection", route: "/knowledge/collections" },
      { id: "documents", label: "Documents", icon: "doc", route: "/knowledge/documents" },
      { id: "search", label: "Entity search probe", icon: "search", route: "/knowledge/search" },
    ],
  },
  {
    group: "Toolsets",
    items: [
      { id: "toolsets-user", label: "User toolsets", icon: "tools", route: "/toolsets" },
      { id: "toolsets-builtin", label: "Built-in", icon: "tools", route: "/toolsets/builtin" },
    ],
  },
  {
    group: "Providers",
    items: [
      { id: "llm", label: "LLM", icon: "llm", route: "/providers/llm" },
      { id: "embedding", label: "Embedding", icon: "emb", route: "/providers/embedding" },
      { id: "rerank", label: "Cross-Encoder", icon: "emb", route: "/providers/cross_encoder" },
    ],
  },
  {
    group: "Subsystems",
    items: [
      { id: "internal-collections", label: "Internal Collections", icon: "subsystem", route: "/subsystems/internal-collections", subsystem: true },
    ],
  },
  {
    group: "Operations",
    items: [
      { id: "workers", label: "Workers", icon: "worker", route: "/workers", count: "workers" },
      { id: "health", label: "Health", icon: "heart", route: "/health" },
    ],
  },
];

// Best-prefix active check. `/` matches only `/`; deeper routes match
// themselves AND their detail-page descendants (e.g. /sessions also
// activates when on /sessions/sess-abc).
function _isActive(currentPath, route) {
  if (route === "/") return currentPath === "/";
  return currentPath === route || currentPath.startsWith(route + "/");
}

// Suppress an expected 404 from /v1/internal_collections/config. The
// IC subsystem is OFF by default, and a 404 there is the documented
// signal — not an error to surface as a toast.
async function _fetchIcConfig(signal) {
  try {
    return await apiFetch("GET", "/internal_collections/config", null, { signal });
  } catch (err) {
    if (err && err.status === 404) return null;
    throw err;
  }
}

// Three parallel polls counting CREATED + RUNNING + PAUSED sessions.
// The backend types `?status` as a single enum (not a list) — Sessions
// sub-project P2 may swap this for a single comma-list call if/when
// the backend grows that support.
function _useSessionCount() {
  const created = useResource("sidebar:sessions-created",
    (s) => apiFetch("GET", "/sessions?status=created&limit=1", null, { signal: s }),
    { pollMs: 5000 });
  const running = useResource("sidebar:sessions-running",
    (s) => apiFetch("GET", "/sessions?status=running&limit=1", null, { signal: s }),
    { pollMs: 5000 });
  const paused = useResource("sidebar:sessions-paused",
    (s) => apiFetch("GET", "/sessions?status=paused&limit=1", null, { signal: s }),
    { pollMs: 5000 });
  const allLoaded = created.data && running.data && paused.data;
  if (!allLoaded) return null;
  return (created.data.total ?? 0) + (running.data.total ?? 0) + (paused.data.total ?? 0);
}

function Sidebar({ collapsed: navCollapsed, onCollapseToggle }) {
  const { path, navigate } = useRouter();
  const sessionCount = _useSessionCount();
  const workspaces = useResource("sidebar:workspaces",
    (s) => apiFetch("GET", "/workspaces?limit=1", null, { signal: s }),
    { pollMs: 5000 });
  const workers = useResource("sidebar:workers",
    (s) => apiFetch("GET", "/workers", null, { signal: s }),
    { pollMs: 5000 });
  const ic = useResource("sidebar:ic-config", _fetchIcConfig, { pollMs: 30000 });
  const subsystemOn = ic.data != null;

  const counts = {
    sessions: sessionCount,
    workspaces: workspaces.data?.total,
    workers: workers.data?.items?.length,
  };

  const [collapsed, setCollapsed] = React.useState(() => {
    try { return JSON.parse(localStorage.getItem("matrix.sidebar.collapsed") || "{}"); } catch { return {}; }
  });
  const toggle = (g) => {
    const next = { ...collapsed, [g]: !collapsed[g] };
    setCollapsed(next);
    try { localStorage.setItem("matrix.sidebar.collapsed", JSON.stringify(next)); } catch {}
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
                className={`nav-item ${_isActive(path, it.route) ? "active" : ""}`}
                onClick={() => navigate(it.route)}
              >
                <Icon name={it.icon} className="icon" />
                <span className="label">{it.label}</span>
                {it.subsystem && (
                  <span className={subsystemOn ? "nav-pill-on" : "nav-pill-off"}>{subsystemOn ? "ON" : "OFF"}</span>
                )}
                {it.count != null && counts[it.count] != null && (
                  <span className="count">{counts[it.count]}</span>
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

function Topbar({ onOpenPalette }) {
  const { navigate } = useRouter();
  const [tweaks] = useTweaks();
  const health = useResource("topbar:health",
    (s) => apiFetch("GET", "/health", null, { signal: s }),
    { pollMs: 2000 });
  const workers = useResource("sidebar:workers",
    (s) => apiFetch("GET", "/workers", null, { signal: s }),
    { pollMs: 5000 });
  const ic = useResource("sidebar:ic-config", _fetchIcConfig, { pollMs: 30000 });

  const items = workers.data?.items ?? [];
  const activeWorkers = items.filter((w) => w.status === "active").length;
  const totalWorkers = items.length;
  const drainingWorkers = items.filter((w) => w.status === "draining");
  const inFlight = health.data?.worker_pool?.in_flight ?? 0;
  const capacity = health.data?.worker_pool?.capacity ?? 0;
  const schedulerAlive = health.data?.scheduler?.alive === true;

  let pillClass = "";
  if (!schedulerAlive || capacity === 0 || activeWorkers === 0) pillClass = "err";
  else if (capacity > 0 && inFlight >= capacity * 0.8) pillClass = "warn";

  // Bell warnings: live draining workers + IC-configured-but-not-bootstrapped.
  // Bootstrapped state is a client-side flag set after a successful
  // POST /v1/internal_collections/bootstrap (Internal Collections
  // sub-project P9 writes it). Cleared on DELETE /config.
  const subsystemOn = ic.data != null;
  let icBootstrapped = false;
  try { icBootstrapped = localStorage.getItem("matrix-console-ic-bootstrapped") === "true"; } catch {}
  const warnings = [];
  drainingWorkers.forEach((w) => warnings.push({
    kind: "draining",
    text: `Worker ${w.id} is draining`,
    onClick: () => navigate("/workers"),
  }));
  if (subsystemOn && !icBootstrapped) {
    warnings.push({
      kind: "ic-not-bootstrapped",
      text: "Internal Collections configured but not bootstrapped",
      onClick: () => navigate("/subsystems/internal-collections"),
    });
  }

  const [bellOpen, setBellOpen] = React.useState(false);
  // Close on outside click.
  React.useEffect(() => {
    if (!bellOpen) return undefined;
    const onDoc = (e) => {
      if (!e.target.closest || !e.target.closest("[data-bell]")) setBellOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [bellOpen]);

  const instanceLabel = tweaks.instanceLabel || "matrix · localhost:8765";
  // Split off the dot-prefixed location half so the brand still shows
  // "matrix" prominently. If the tweak doesn't include a "·", show it whole.
  let brandName = "matrix";
  let brandSub = "";
  const dot = instanceLabel.indexOf("·");
  if (dot >= 0) {
    brandName = instanceLabel.slice(0, dot).trim() || "matrix";
    brandSub = "· " + instanceLabel.slice(dot + 1).trim();
  } else {
    brandName = instanceLabel;
  }

  return (
    <header className="topbar">
      <div className="topbar-brand">
        <BrandMark size={22} />
        <div>
          <div className="name">{brandName}</div>
        </div>
        {brandSub && <div className="instance">{brandSub}</div>}
      </div>
      <div className="topbar-search" onClick={onOpenPalette}>
        <Icon name="search" size={13} />
        <span>Search…</span>
        <kbd>⌘K</kbd>
      </div>
      <div className="topbar-right">
        <div
          className={`worker-pill ${pillClass}`}
          onClick={() => navigate("/workers")}
          title="Worker pool · click to view"
        >
          <span className="dot"></span>
          <span className={capacity > 0 && inFlight >= capacity * 0.8 ? "num-warn" : ""}>
            {activeWorkers}/{totalWorkers || "—"}
          </span>
          <span>workers</span>
          <span className="sep">·</span>
          <span>{inFlight} in flight</span>
        </div>
        <div data-bell style={{ position: "relative" }}>
          <button
            className={`icon-btn ${warnings.length > 0 ? "warn" : ""}`}
            title={warnings.length === 0 ? "No warnings" : warnings.map((w) => w.text).join("\n")}
            onClick={() => setBellOpen((o) => !o)}
          >
            <Icon name="bell" size={14} />
            {warnings.length > 0 && (
              <span style={{
                position: "absolute", top: 2, right: 2,
                background: "var(--amber)", color: "var(--bg)", fontSize: 9, fontWeight: 600,
                padding: "0 4px", borderRadius: 6, minWidth: 12, textAlign: "center", lineHeight: "12px",
              }}>{warnings.length}</span>
            )}
          </button>
          {bellOpen && (
            <div style={{
              position: "absolute", top: "calc(100% + 6px)", right: 0,
              background: "var(--bg-2)", border: "1px solid var(--border)", borderRadius: 6,
              boxShadow: "0 6px 24px rgba(0,0,0,0.35)", minWidth: 260, zIndex: 50, padding: 4,
            }}>
              {warnings.length === 0 ? (
                <div style={{ padding: 12, fontSize: 12.5, color: "var(--text-3)" }}>No warnings.</div>
              ) : warnings.map((w, i) => (
                <div
                  key={i}
                  className="nav-item"
                  style={{ borderRadius: 4, margin: 2 }}
                  onClick={() => { setBellOpen(false); w.onClick(); }}
                >
                  <Icon name="alert" size={12} className="icon" />
                  <span className="label" style={{ fontSize: 12.5 }}>{w.text}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="user-avatar mono" title="No auth (roadmap)">OP</div>
      </div>
    </header>
  );
}

// ----- Command palette -----------------------------------------------------

// Subsequence-based fuzzy scorer. Higher is better. ~30 LoC, no deps.
function _fuzzyScore(query, target) {
  if (!query) return 0;
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  if (t.includes(q)) {
    // Substring match — score by inverse position + length penalty.
    return 1000 - t.indexOf(q) - t.length * 0.1;
  }
  let qi = 0;
  let lastIdx = -1;
  let runs = 0;
  let inRun = false;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      if (lastIdx === ti - 1) {
        if (!inRun) { runs++; inRun = true; }
      } else {
        inRun = false;
      }
      lastIdx = ti;
      qi++;
    }
  }
  if (qi < q.length) return -1;
  return 500 + runs * 10 - t.length * 0.1;
}

const _STATIC_PAGES = [
  { kind: "page", label: "Dashboard", path: "/", icon: "home" },
  { kind: "page", label: "Sessions", path: "/sessions", icon: "zap" },
  { kind: "page", label: "Workspaces", path: "/workspaces", icon: "box" },
  { kind: "page", label: "Agents", path: "/agents", icon: "agent" },
  { kind: "page", label: "Graphs", path: "/graphs", icon: "graph" },
  { kind: "page", label: "Collections", path: "/knowledge/collections", icon: "collection" },
  { kind: "page", label: "Documents", path: "/knowledge/documents", icon: "doc" },
  { kind: "page", label: "Entity search probe", path: "/knowledge/search", icon: "search" },
  { kind: "page", label: "User toolsets", path: "/toolsets", icon: "tools" },
  { kind: "page", label: "Built-in toolsets", path: "/toolsets/builtin", icon: "tools" },
  { kind: "page", label: "LLM providers", path: "/providers/llm", icon: "llm" },
  { kind: "page", label: "Embedding providers", path: "/providers/embedding", icon: "emb" },
  { kind: "page", label: "Cross-Encoder providers", path: "/providers/cross_encoder", icon: "emb" },
  { kind: "page", label: "Internal Collections", path: "/subsystems/internal-collections", icon: "subsystem" },
  { kind: "page", label: "Workers", path: "/workers", icon: "worker" },
  { kind: "page", label: "Health", path: "/health", icon: "heart" },
];

function CommandPalette({ onClose }) {
  const { navigate } = useRouter();
  const [tweaks, setTweak] = useTweaks();
  const [q, setQ] = React.useState("");
  const [active, setActive] = React.useState(0);
  const inputRef = React.useRef(null);
  React.useEffect(() => { inputRef.current && inputRef.current.focus(); }, []);

  // Entity searches: kicked off on open and re-fetched per palette-open.
  // No poll — palette is short-lived.
  const sessions = useResource("palette:sessions",
    (s) => apiFetch("GET", "/sessions?limit=20", null, { signal: s }), {});
  const workspaces = useResource("palette:workspaces",
    (s) => apiFetch("GET", "/workspaces?limit=20", null, { signal: s }), {});
  const agents = useResource("palette:agents",
    (s) => apiFetch("GET", "/agents?limit=20", null, { signal: s }), {});
  const graphs = useResource("palette:graphs",
    (s) => apiFetch("GET", "/graphs?limit=20", null, { signal: s }), {});

  // Quick actions (always shown when query is empty)
  const quickActions = React.useMemo(() => [
    { kind: "action", label: "Toggle theme", icon: "settings",
      onAction: () => setTweak("theme", tweaks.theme === "dark" ? "light" : "dark") },
    { kind: "action", label: "Open OpenAPI spec", icon: "external",
      onAction: () => window.open("/v1/openapi.json", "_blank", "noopener,noreferrer") },
  ], [tweaks.theme, setTweak]);

  const entityRows = React.useMemo(() => {
    const rows = [];
    (sessions.data?.items ?? []).forEach((s) => {
      rows.push({ kind: "session", label: s.id, sub: s.binding?.agent_id || s.binding?.graph_id || "", path: "/sessions/" + s.id, icon: "zap" });
    });
    (workspaces.data?.items ?? []).forEach((w) => {
      rows.push({ kind: "workspace", label: w.id, sub: w.template_id || "", path: "/workspaces/" + w.id, icon: "box" });
    });
    (agents.data?.items ?? []).forEach((a) => {
      rows.push({ kind: "agent", label: a.id, sub: a.llm_provider_id || "", path: "/agents/" + a.id, icon: "agent" });
    });
    (graphs.data?.items ?? []).forEach((g) => {
      rows.push({ kind: "graph", label: g.id, sub: "", path: "/graphs/" + g.id, icon: "graph" });
    });
    return rows;
  }, [sessions.data, workspaces.data, agents.data, graphs.data]);

  const matches = React.useMemo(() => {
    const corpus = [..._STATIC_PAGES, ...entityRows];
    if (!q) {
      return [...quickActions, ..._STATIC_PAGES].slice(0, 20);
    }
    const scored = corpus
      .map((row) => ({ row, score: Math.max(_fuzzyScore(q, row.label), _fuzzyScore(q, row.sub || "")) }))
      .filter((x) => x.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 20)
      .map((x) => x.row);
    return scored;
  }, [q, entityRows, quickActions]);

  React.useEffect(() => { setActive(0); }, [q]);

  const choose = React.useCallback((row) => {
    onClose();
    if (row.kind === "action" && row.onAction) row.onAction();
    else if (row.path) navigate(row.path);
  }, [onClose, navigate]);

  React.useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(matches.length - 1, a + 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
      else if (e.key === "Enter" && matches[active]) {
        e.preventDefault();
        choose(matches[active]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, matches, active, choose]);

  const loading = sessions.loading || workspaces.loading || agents.loading || graphs.loading;

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
            placeholder="Go to page or search entity id…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          {loading && <span className="muted text-sm">loading…</span>}
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
                onClick={() => choose(m)}
              >
                <Icon name={m.icon || "chevron-right"} className="icon" size={13} />
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

// ----- Toast container -----------------------------------------------------

function ToastContainer() {
  const { toasts, dismiss } = useToast();
  // Track "copied" feedback per-toast id so the request-id link can
  // flash "Copied" for ~1s on click.
  const [copiedId, setCopiedId] = React.useState(null);
  const copy = (id, value) => {
    try {
      navigator.clipboard.writeText(value);
      setCopiedId(id);
      setTimeout(() => setCopiedId((cur) => (cur === id ? null : cur)), 1000);
    } catch {
      // Some browsers without clipboard API: fallback to selecting a hidden input.
      // Not worth implementing for v1; the link just no-ops.
    }
  };
  return (
    <div className="toast-stack">
      {toasts.map((t) => {
        // Toast shape from useToast.push():
        //   { id, kind, title, detail, requestId, actions?, durationMs }
        // We also tolerate the legacy `reqId` key (some pre-Milestone-1 callsites).
        const rid = t.requestId || t.reqId;
        return (
          <div key={t.id} className={`toast toast-${t.kind || "info"}`}>
            <Icon
              name={t.kind === "success" ? "check-circle" : t.kind === "error" ? "x-circle" : t.kind === "warning" ? "alert" : "info"}
              size={14}
              className="ico"
            />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="title">{t.title}</div>
              {t.detail && <div className="detail">{t.detail}</div>}
              {rid && (
                <div className="req-id">
                  request-id <span style={{ color: "var(--text)" }}>{rid}</span>
                  {" · "}
                  <a onClick={() => copy(t.id, rid)} style={{ cursor: "pointer" }}>
                    {copiedId === t.id ? "copied" : "copy"}
                  </a>
                </div>
              )}
              {t.actions && t.actions.length > 0 && (
                <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
                  {t.actions.map((a, i) => (
                    <button
                      key={i}
                      className="btn btn-ghost btn-sm"
                      onClick={() => { a.onClick && a.onClick(); dismiss(t.id); }}
                    >{a.label}</button>
                  ))}
                </div>
              )}
            </div>
            <button className="close" onClick={() => dismiss(t.id)}><Icon name="x" size={12} /></button>
          </div>
        );
      })}
    </div>
  );
}

// BrandMark — inlined logo so CSS `color` cascades into `currentColor`.
// Source: brand/logo.svg (5-poly rotated-quad mark). The accent
// polygon stays baked at the brand green; the four ink polygons
// inherit the current text color so they read on both themes.
function BrandMark({ size = 22 }) {
  return (
    <svg
      className="brand-mark"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      role="img"
      aria-label="Matrix logo"
      style={{ display: "block", color: "var(--text)" }}
    >
      <polygon points="12,3 21,12 12,21 3,12" fill="currentColor" fillOpacity="0.16" />
      <polygon points="12,3 16.5,7.5 12,12 7.5,7.5" fill="currentColor" />
      <polygon points="16.5,7.5 21,12 16.5,16.5 12,12" fill="currentColor" fillOpacity="0.4" />
      <polygon points="12,12 16.5,16.5 12,21 7.5,16.5" fill="#61d46a" />
      <polygon points="7.5,7.5 12,12 7.5,16.5 3,12" fill="currentColor" fillOpacity="0.4" />
    </svg>
  );
}

Object.assign(window, { Sidebar, Topbar, CommandPalette, ToastContainer, BrandMark });
