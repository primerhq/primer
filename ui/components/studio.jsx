/* global React, Icon, Btn */
// Studio — the workspace-scoped IDE shell (PR-B / B1 foundation).
//
// Route component for /workspaces/:wid. Owns useStudioState(wid): the
// open-tab model, sidebar layout, theme/density, and the persistence +
// URL-mirroring seams ported from studio/Studio.dc.html. The three body
// regions (left sidebar / center / right) are STUBBED here as styled
// placeholder boxes; later sub-tasks fill them:
//   B2 → StudioSidebar   (left)
//   B3 → StudioCenter    (center)
//   B4 → StudioActivity  (right)
//   B5 → CommandPalette + ⌘K/⌘P wiring + enrichments
//
// No-build scope rules (see workspace-tap.jsx): top-level declarations use
// `var`; helpers are prefixed ST_ to avoid global collisions; every
// component/hook is exported via `window.X = X`.

// ---------------------------------------------------------------------------
// Persistence contract (STUDIO-INTEGRATION.md §3)
// ---------------------------------------------------------------------------

// The exact key set round-tripped through localStorage["studio:<wid>"].
var ST_PERSIST_KEYS = [
  "openTabs",
  "activeTabId",
  "sessionsOpen",
  "filesOpen",
  "showHidden",
  "expanded",
  "fileModes",
  "terminalOpen",
  "activeTermId",
  "termTabs",
  "density",
  "theme",
  "activeChips",
  "leftWidth",
  "rightWidth",
];

function ST_storageKey(wid) {
  return "studio:" + wid;
}

// Read the persisted partial state for a workspace; returns {} on any
// miss / parse error. Only whitelisted keys survive.
function ST_loadPersisted(wid) {
  try {
    var raw = window.localStorage.getItem(ST_storageKey(wid));
    if (!raw) return {};
    var parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    var keep = {};
    ST_PERSIST_KEYS.forEach(function (k) {
      if (parsed[k] !== undefined) keep[k] = parsed[k];
    });
    return keep;
  } catch (_e) {
    return {};
  }
}

// Serialise the persisted slice of state under studio:<wid>.
function ST_savePersisted(wid, state) {
  try {
    var snap = {};
    ST_PERSIST_KEYS.forEach(function (k) {
      snap[k] = state[k];
    });
    window.localStorage.setItem(ST_storageKey(wid), JSON.stringify(snap));
  } catch (_e) {
    /* quota / disabled storage — non-fatal */
  }
}

// Parse ?open=session:<id> / ?open=file:<path> into a tab id string, or
// null. The Studio uses a hash router (see foundation/router.js), so the
// query lives in the hash fragment, not window.location.search.
function ST_tabFromUrl() {
  try {
    var hash = window.location.hash || "";
    var qIdx = hash.indexOf("?");
    if (qIdx < 0) return null;
    var open = new URLSearchParams(hash.slice(qIdx + 1)).get("open");
    if (!open) return null;
    if (open.indexOf("session:") === 0) return open;
    if (open.indexOf("file:") === 0) return open;
    return null;
  } catch (_e) {
    return null;
  }
}

// Mirror the active tab to the URL query via replaceState — no history
// entry per tab switch. activeTabId is already in `session:<id>` /
// `file:<path>` form so it maps straight onto ?open=.
function ST_syncUrl(activeTabId) {
  try {
    var url = new URL(window.location.href);
    var hash = url.hash || "#/";
    var qIdx = hash.indexOf("?");
    var path = qIdx >= 0 ? hash.slice(0, qIdx) : hash;
    var params = new URLSearchParams(qIdx >= 0 ? hash.slice(qIdx + 1) : "");
    if (activeTabId) params.set("open", activeTabId);
    else params.delete("open");
    var qs = params.toString();
    url.hash = qs ? path + "?" + qs : path;
    window.history.replaceState(null, "", url.toString());
  } catch (_e) {
    /* malformed URL — skip */
  }
}

// ---------------------------------------------------------------------------
// Default (un-persisted) state shape
// ---------------------------------------------------------------------------

function ST_defaultState() {
  return {
    // Layout / appearance
    theme: "dark",
    density: "comfortable",
    leftWidth: 248,
    rightWidth: 332,
    // Left sidebar
    sessionsOpen: true,
    filesOpen: true,
    showHidden: false,
    expanded: {},
    // Center tabs — [{ id, kind:'session'|'file', ref, title, dirty }]
    openTabs: [],
    activeTabId: null,
    fileModes: {},
    // Terminal placeholders (P7)
    terminalOpen: false,
    termTabs: [{ id: "bash", title: "bash" }],
    activeTermId: "bash",
    // Right sidebar activity-feed chip filter
    activeChips: { tool: true, gt: true, asst: true, yield: true, done: true, err: true },
    // Last sidebar selection (ephemeral-ish; persisted for restore)
    lastSelection: null,
  };
}

// ---------------------------------------------------------------------------
// useStudioState(wid) — the single state owner for the Studio shell.
//
// Returns { state, ...actions }. Persists the whitelisted slice to
// localStorage on every change and mirrors the active tab into the URL.
// B2–B4 consume the action callbacks (openTab/focusTab/closeTab/toggle*).
// ---------------------------------------------------------------------------

function useStudioState(wid) {
  // Initial state: defaults <- persisted(wid) <- url(?open=). Computed once
  // per wid via the lazy initialiser; re-keyed below when wid changes.
  var [state, setState] = React.useState(function () {
    var base = Object.assign(ST_defaultState(), ST_loadPersisted(wid));
    var urlTab = ST_tabFromUrl();
    if (urlTab && (base.openTabs || []).some(function (t) { return t.id === urlTab; })) {
      base.activeTabId = urlTab;
    }
    return base;
  });

  // Re-hydrate when switching workspace (wid changes): swap to that
  // workspace's persisted layout. switchWorkspace() in the header navigates
  // the route, which remounts/keys this hook via the wid dependency.
  var widRef = React.useRef(wid);
  React.useEffect(function () {
    if (widRef.current === wid) return;
    widRef.current = wid;
    var base = Object.assign(ST_defaultState(), ST_loadPersisted(wid));
    var urlTab = ST_tabFromUrl();
    if (urlTab && (base.openTabs || []).some(function (t) { return t.id === urlTab; })) {
      base.activeTabId = urlTab;
    }
    setState(base);
  }, [wid]);

  // Persist + URL-mirror after every committed state change.
  React.useEffect(function () {
    ST_savePersisted(wid, state);
    ST_syncUrl(state.activeTabId);
  }, [wid, state]);

  // Apply theme/density attrs to <html> so the design tokens resolve. The
  // Studio root <div> also carries them (see render) for scoped reads.
  React.useEffect(function () {
    var html = document.documentElement;
    var prevTheme = html.getAttribute("data-theme");
    var prevDensity = html.getAttribute("data-density");
    html.setAttribute("data-theme", state.theme);
    html.setAttribute("data-density", state.density);
    return function () {
      // Restore prior attrs on unmount so leaving the Studio doesn't strand
      // the rest of the console in Studio's density.
      if (prevTheme) html.setAttribute("data-theme", prevTheme);
      if (prevDensity) html.setAttribute("data-density", prevDensity);
    };
  }, [state.theme, state.density]);

  // ---- tab management (consumed by B2/B3) ----
  var openTab = React.useCallback(function (tab) {
    setState(function (s) {
      var exists = (s.openTabs || []).some(function (t) { return t.id === tab.id; });
      var openTabs = exists ? s.openTabs : s.openTabs.concat([tab]);
      var fileModes = s.fileModes;
      if (tab.kind === "file" && fileModes[tab.id] === undefined) {
        fileModes = Object.assign({}, fileModes);
        fileModes[tab.id] = tab.mode || "preview";
      }
      return Object.assign({}, s, { openTabs: openTabs, activeTabId: tab.id, fileModes: fileModes });
    });
  }, []);

  var focusTab = React.useCallback(function (id) {
    setState(function (s) { return Object.assign({}, s, { activeTabId: id }); });
  }, []);

  var closeTab = React.useCallback(function (id) {
    setState(function (s) {
      var idx = (s.openTabs || []).findIndex(function (t) { return t.id === id; });
      var openTabs = s.openTabs.filter(function (t) { return t.id !== id; });
      var activeTabId = s.activeTabId;
      if (activeTabId === id) {
        activeTabId = openTabs.length ? openTabs[Math.max(0, idx - 1)].id : null;
      }
      return Object.assign({}, s, { openTabs: openTabs, activeTabId: activeTabId });
    });
  }, []);

  // ---- left sidebar toggles ----
  var toggleSessions = React.useCallback(function () {
    setState(function (s) { return Object.assign({}, s, { sessionsOpen: !s.sessionsOpen }); });
  }, []);
  var toggleFiles = React.useCallback(function () {
    setState(function (s) { return Object.assign({}, s, { filesOpen: !s.filesOpen }); });
  }, []);
  var toggleHidden = React.useCallback(function () {
    setState(function (s) { return Object.assign({}, s, { showHidden: !s.showHidden }); });
  }, []);
  var toggleFolder = React.useCallback(function (path) {
    setState(function (s) {
      var expanded = Object.assign({}, s.expanded);
      expanded[path] = !expanded[path];
      return Object.assign({}, s, { expanded: expanded });
    });
  }, []);

  // ---- appearance ----
  var toggleTheme = React.useCallback(function () {
    setState(function (s) { return Object.assign({}, s, { theme: s.theme === "dark" ? "light" : "dark" }); });
  }, []);
  var toggleDensity = React.useCallback(function () {
    setState(function (s) {
      return Object.assign({}, s, { density: s.density === "comfortable" ? "compact" : "comfortable" });
    });
  }, []);

  // ---- terminal (P7 placeholder) ----
  var toggleTerminal = React.useCallback(function () {
    setState(function (s) { return Object.assign({}, s, { terminalOpen: !s.terminalOpen }); });
  }, []);

  // ---- activity chips (B4) ----
  var toggleChip = React.useCallback(function (cl) {
    setState(function (s) {
      var activeChips = Object.assign({}, s.activeChips);
      activeChips[cl] = !activeChips[cl];
      return Object.assign({}, s, { activeChips: activeChips });
    });
  }, []);

  // ---- column resize (persisted) ----
  var setLeftWidth = React.useCallback(function (w) {
    setState(function (s) { return Object.assign({}, s, { leftWidth: w }); });
  }, []);
  var setRightWidth = React.useCallback(function (w) {
    setState(function (s) { return Object.assign({}, s, { rightWidth: w }); });
  }, []);

  // Generic patch escape hatch for B2–B4 to set fields not covered above
  // (fileModes, lastSelection, etc.) without re-plumbing the hook each time.
  var patch = React.useCallback(function (partial) {
    setState(function (s) { return Object.assign({}, s, partial); });
  }, []);

  return {
    state: state,
    openTab: openTab,
    focusTab: focusTab,
    closeTab: closeTab,
    toggleSessions: toggleSessions,
    toggleFiles: toggleFiles,
    toggleHidden: toggleHidden,
    toggleFolder: toggleFolder,
    toggleTheme: toggleTheme,
    toggleDensity: toggleDensity,
    toggleTerminal: toggleTerminal,
    toggleChip: toggleChip,
    setLeftWidth: setLeftWidth,
    setRightWidth: setRightWidth,
    patch: patch,
  };
}

// ---------------------------------------------------------------------------
// Brand logo — faceted diamond from the prototype (renderVals.logo).
// ---------------------------------------------------------------------------

function ST_Logo() {
  return (
    <svg width={22} height={22} viewBox="0 0 24 24">
      <polygon points="12,3 21,12 12,21 3,12" fill="var(--text)" fillOpacity={0.16} />
      <polygon points="12,3 16.5,7.5 12,12 7.5,7.5" fill="var(--text)" />
      <polygon points="16.5,7.5 21,12 16.5,16.5 12,12" fill="var(--text)" fillOpacity={0.4} />
      <polygon points="12,12 16.5,16.5 12,21 7.5,16.5" fill="var(--accent)" />
      <polygon points="7.5,7.5 12,12 7.5,16.5 3,12" fill="var(--text)" fillOpacity={0.4} />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// StudioHeader — brand · workspace selector ▾ · ⌘K / ⌘P / density / theme
// ---------------------------------------------------------------------------

function StudioHeader({ wid, theme, density, onToggleTheme, onToggleDensity, onTogglePalette, onOpenQuick, onSelectWorkspace }) {
  var { useResource, apiFetch } = window.primerApi;
  var [menuOpen, setMenuOpen] = React.useState(false);

  // Workspace list for the selector dropdown. Reuse the same cache key the
  // app already polls so we ride on its data instead of a second roundtrip.
  var workspaces = useResource(
    "topbar:workspaces",
    function (signal) { return apiFetch("GET", "/workspaces?limit=200", null, { signal }); },
    { pollMs: 5000 }
  );
  var items = Array.isArray(workspaces.data && workspaces.data.items) ? workspaces.data.items : [];

  // Close the dropdown on Escape or outside click.
  React.useEffect(function () {
    if (!menuOpen) return;
    function onKey(e) { if (e.key === "Escape") setMenuOpen(false); }
    function onDoc() { setMenuOpen(false); }
    window.addEventListener("keydown", onKey);
    // defer so the opening click doesn't immediately close it
    var t = setTimeout(function () { document.addEventListener("click", onDoc); }, 0);
    return function () {
      window.removeEventListener("keydown", onKey);
      clearTimeout(t);
      document.removeEventListener("click", onDoc);
    };
  }, [menuOpen]);

  function pick(id) {
    setMenuOpen(false);
    if (id !== wid) onSelectWorkspace(id);
  }

  return (
    <div className="st-topbar" data-testid="studio-header">
      <div className="st-brand">
        <span style={{ width: 22, height: 22, display: "grid", placeItems: "center" }}><ST_Logo /></span>
        Primer <span style={{ color: "var(--text-3)", fontWeight: 500, fontSize: 12 }}>Studio</span>
      </div>

      <div
        className="st-ws-btn"
        data-testid="workspace-selector"
        onClick={function (e) { e.stopPropagation(); setMenuOpen(function (o) { return !o; }); }}
      >
        <span style={{ width: 14, height: 14, borderRadius: 4, background: "var(--accent-dim)", border: "1px solid var(--accent-border)" }} />
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 180 }}>{wid}</span>
        <span style={{ color: "var(--text-3)" }}>▾</span>
        {menuOpen && (
          <div className="st-ws-menu" data-testid="workspace-menu" onClick={function (e) { e.stopPropagation(); }}>
            {items.length === 0 && (
              <div className="st-ws-menu-row" style={{ color: "var(--text-3)", cursor: "default" }}>
                {workspaces.loading ? "Loading…" : "No workspaces"}
              </div>
            )}
            {items.map(function (w) {
              return (
                <div key={w.id} className="st-ws-menu-row" onClick={function () { pick(w.id); }}>
                  <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--green)", flex: "0 0 auto" }} />
                  <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{w.name || w.id}</span>
                  {w.id === wid && <span style={{ color: "var(--accent)" }}>✓</span>}
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div style={{ flex: 1 }} />

      {/* ⌘K palette trigger — inert stub until B5 wires the palette. */}
      <div className="st-palette-trigger" data-testid="palette-trigger" onClick={onTogglePalette}>
        <span style={{ opacity: 0.8 }}>Search · run · jump</span>
        <kbd>⌘K</kbd>
      </div>
      {/* ⌘P quick-open — inert stub until B5. */}
      <div className="st-hbtn" data-testid="quickopen-trigger" title="Quick open (⌘P)" onClick={onOpenQuick}>⌕</div>
      <div className="st-hbtn" data-testid="density-toggle" title="Density" onClick={onToggleDensity}>☰</div>
      <div className="st-hbtn" data-testid="theme-toggle" title="Theme" onClick={onToggleTheme}>
        {theme === "dark" ? "◐" : "○"}
      </div>
      <div className="st-avatar" title="User">DK</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ST_RegionPlaceholder — styled empty box for a not-yet-built region.
// ---------------------------------------------------------------------------

function ST_RegionPlaceholder({ kind, label, testid }) {
  return (
    <div className="st-placeholder" data-testid={testid}>
      <div>
        <div className="st-ph-kind">{kind}</div>
        <div style={{ marginTop: 6 }}>{label}</div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Studio — route shell. Header + 3-column resizable body with placeholders.
// ---------------------------------------------------------------------------

function Studio({ wid }) {
  var studio = useStudioState(wid);
  var s = studio.state;

  // Column resize: drag the handle, write width into state (persisted).
  // We clamp to sane bounds so a column can't be dragged off-screen.
  var dragRef = React.useRef(null);
  function startResize(side, e) {
    e.preventDefault();
    dragRef.current = { side: side, startX: e.clientX, startW: side === "left" ? s.leftWidth : s.rightWidth };
    function onMove(ev) {
      var d = dragRef.current;
      if (!d) return;
      var delta = ev.clientX - d.startX;
      if (d.side === "left") {
        var lw = Math.max(180, Math.min(480, d.startW + delta));
        studio.setLeftWidth(lw);
      } else {
        var rw = Math.max(220, Math.min(560, d.startW - delta));
        studio.setRightWidth(rw);
      }
    }
    function onUp() {
      dragRef.current = null;
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  function selectWorkspace(newWid) {
    // Navigate to the new workspace route; the hook re-hydrates from that
    // wid's persisted layout. Drop any stale ?open= from the previous ws.
    window.location.hash = "#/workspaces/" + encodeURIComponent(newWid);
  }

  return (
    <div
      className="st-root"
      data-theme={s.theme}
      data-density={s.density}
      data-testid="studio-root"
      style={{ "--st-left-w": s.leftWidth + "px", "--st-right-w": s.rightWidth + "px" }}
    >
      <StudioHeader
        wid={wid}
        theme={s.theme}
        density={s.density}
        onToggleTheme={studio.toggleTheme}
        onToggleDensity={studio.toggleDensity}
        onTogglePalette={function () { /* B5: open command palette */ }}
        onOpenQuick={function () { /* B5: open quick-open */ }}
        onSelectWorkspace={selectWorkspace}
      />

      <div className="st-body">
        {/* ---- LEFT: B2 StudioSidebar (sessions + files tree) ---- */}
        <div className="st-col st-col-left" data-testid="studio-sidebar">
          <ST_RegionPlaceholder kind="B2 · left sidebar" label="Sessions + Files tree" testid="region-sidebar" />
        </div>

        <div className="st-resize" onMouseDown={function (e) { startResize("left", e); }} data-testid="studio-resize-left" />

        {/* ---- CENTER: B3 StudioCenter (tabs + active panel + terminal) ---- */}
        <div className="st-col st-col-center" data-testid="studio-center">
          <ST_RegionPlaceholder kind="B3 · center" label="Tab bar + active document panel" testid="region-center" />
        </div>

        <div className="st-resize" onMouseDown={function (e) { startResize("right", e); }} data-testid="studio-resize-right" />

        {/* ---- RIGHT: B4 StudioActivity (action required + workspace tap) ---- */}
        <div className="st-col st-col-right" data-testid="studio-activity">
          <ST_RegionPlaceholder kind="B4 · right sidebar" label="Action Required + Workspace Activity" testid="region-activity" />
        </div>
      </div>

      {/* B5: {paletteOpen && <CommandPalette … />} mounts here. */}
    </div>
  );
}

// No-build exports — every component/hook reachable from app.jsx + B2–B4.
window.Studio = Studio;
window.StudioHeader = StudioHeader;
window.useStudioState = useStudioState;
