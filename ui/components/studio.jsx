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
//   B5 → StudioCommandPalette + ⌘K/⌘P wiring + enrichments
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
  "debugOpen",
  "activeTermId",
  "termTabs",
  "density",
  "theme",
  "activeChips",
  "leftWidth",
  "rightWidth",
  "terminalHeight",
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

// Synthesize a minimal tab from a parsed url tab id (the ?open= value).
// Returns { id, kind, ref, title, glyph } or null. Used by the initializer
// and the wid-change effect so a fresh deep-link (empty localStorage) still
// creates + activates the tab — the center/right panels self-fetch their
// detail from `ref`, so a minimal synthesized tab renders correctly.
function ST_tabFromUrlId(urlTab) {
  if (!urlTab || typeof urlTab !== "string") return null;
  if (urlTab.indexOf("session:") === 0) {
    var sid = urlTab.slice("session:".length);
    return { id: urlTab, kind: "session", ref: sid, title: sid, glyph: "◆" };
  }
  if (urlTab.indexOf("file:") === 0) {
    var fpath = urlTab.slice("file:".length);
    var parts = String(fpath).split("/");
    var base = parts[parts.length - 1] || fpath;
    return { id: urlTab, kind: "file", ref: fpath, title: base };
  }
  return null;
}

// Reconcile a freshly-built base state with the current ?open= deep-link.
// If the url tab is already an open tab, just activate it; otherwise
// synthesize a minimal tab, append it, and activate it. With activeTabId set
// on mount, the first persist effect's ST_syncUrl re-writes the same ?open=
// value instead of deleting it (so deep-links survive the mount).
function ST_applyUrlTab(base, fallbackOpen) {
  var urlTab = ST_tabFromUrl() || (fallbackOpen || null);
  if (!urlTab) return base;
  var openTabs = base.openTabs || [];
  var present = openTabs.some(function (t) { return t.id === urlTab; });
  if (present) {
    base.activeTabId = urlTab;
    return base;
  }
  var tab = ST_tabFromUrlId(urlTab);
  if (!tab) return base;
  base.openTabs = openTabs.concat([tab]);
  base.activeTabId = urlTab;
  return base;
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
    // Right debug/activity rail — open state (persisted). Default collapsed
    // so an operator who isn't debugging keeps the screen width; the header
    // Debug toggle + the rail's own handle both flip this.
    debugOpen: false,
    terminalHeight: 240,
    termTabs: [{ id: "bash", title: "bash" }],
    activeTermId: "bash",
    // Right sidebar activity-feed chip filter
    activeChips: { tool: true, gt: true, asst: true, yield: true, done: true, err: true },
    // Last sidebar selection (ephemeral-ish; persisted for restore)
    lastSelection: null,
    // Command palette / quick-open (B5) — ephemeral, not persisted.
    paletteOpen: false,
    paletteMode: "command",
    paletteQuery: "",
  };
}

// ---------------------------------------------------------------------------
// useStudioState(wid) — the single state owner for the Studio shell.
//
// Returns { state, ...actions }. Persists the whitelisted slice to
// localStorage on every change and mirrors the active tab into the URL.
// B2–B4 consume the action callbacks (openTab/focusTab/closeTab/toggle*).
// ---------------------------------------------------------------------------

function useStudioState(wid, initialOpen) {
  // Initial state: defaults <- persisted(wid) <- url(?open=). Computed once
  // per wid via the lazy initialiser; re-keyed below when wid changes.
  // `initialOpen` (a "session:<id>" / "file:<path>" string) is a fallback used
  // only when the URL has no ?open= deep-link: it lets a host that mounts the
  // Studio directly (e.g. the docs embed harness) seed an initial open tab.
  var [state, setState] = React.useState(function () {
    return ST_applyUrlTab(Object.assign(ST_defaultState(), ST_loadPersisted(wid)), initialOpen);
  });

  // New-session form visibility, lifted out of the sidebar so BOTH the sidebar
  // "+" button and the ⌘K palette's "New session" action can open it. Kept as
  // ephemeral state (not part of the persisted `state` blob) so it never
  // reopens on reload.
  var [newSessionOpen, setNewSessionOpen] = React.useState(false);

  // Re-hydrate when switching workspace (wid changes): swap to that
  // workspace's persisted layout. switchWorkspace() in the header navigates
  // the route, which remounts/keys this hook via the wid dependency.
  var widRef = React.useRef(wid);
  React.useEffect(function () {
    if (widRef.current === wid) return;
    widRef.current = wid;
    setState(ST_applyUrlTab(Object.assign(ST_defaultState(), ST_loadPersisted(wid)), initialOpen));
  }, [wid]);

  // Persist + URL-mirror after every committed state change.
  React.useEffect(function () {
    ST_savePersisted(wid, state);
    ST_syncUrl(state.activeTabId);
  }, [wid, state]);

  // React to ?open= changes that happen AFTER mount (deep-links followed
  // while the Studio is already mounted — a same-page hash/query change).
  // ST_applyUrlTab only runs on mount / wid-change, so without this a fresh
  // ?open= would do nothing. We listen for hashchange + popstate (our own
  // ST_syncUrl writes use replaceState, which fires neither, so this never
  // loops on our own URL mirroring). Dedup: no-op when the parsed tab is
  // already active — that both avoids redundant renders and, combined with
  // the fact that a manual close clears ?open via replaceState (no event),
  // stops a user-closed tab from reappearing unless the URL truly changes
  // back to it. Mount-time deep-link handling is left untouched.
  React.useEffect(function () {
    function onUrlChange() {
      var urlTab = ST_tabFromUrl();
      if (!urlTab) return; // navigation dropped ?open= — don't clobber tabs
      setState(function (s) {
        if (s.activeTabId === urlTab) return s; // already active — no-op
        var openTabs = s.openTabs || [];
        var present = openTabs.some(function (t) { return t.id === urlTab; });
        if (present) {
          return Object.assign({}, s, { activeTabId: urlTab });
        }
        var tab = ST_tabFromUrlId(urlTab);
        if (!tab) return s;
        return Object.assign({}, s, {
          openTabs: openTabs.concat([tab]),
          activeTabId: urlTab,
        });
      });
    }
    window.addEventListener("hashchange", onUrlChange);
    window.addEventListener("popstate", onUrlChange);
    return function () {
      window.removeEventListener("hashchange", onUrlChange);
      window.removeEventListener("popstate", onUrlChange);
    };
  }, []);

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
      var prev = s.openTabs || [];
      var idx = prev.findIndex(function (t) { return t.id === id; });
      var openTabs = prev.filter(function (t) { return t.id !== id; });
      var activeTabId = s.activeTabId;
      if (activeTabId === id) {
        // Activate the left neighbour in the POST-filter array. The closed
        // tab sat at `idx`, so its left neighbour is now at `idx - 1` (clamped
        // into the filtered array's bounds); compute against one consistent
        // array to avoid an off-by-one.
        if (!openTabs.length) {
          activeTabId = null;
        } else {
          var nextIdx = Math.min(Math.max(0, idx - 1), openTabs.length - 1);
          activeTabId = openTabs[nextIdx].id;
        }
      }
      return Object.assign({}, s, { openTabs: openTabs, activeTabId: activeTabId });
    });
  }, []);

  // Close every open tab at once (the tab bar's "Close all" affordance).
  // Clears openTabs + activeTabId; the persist effect then wipes them from
  // localStorage too. No-op (returns the same state object) when already
  // empty so it doesn't churn a render or the persisted blob.
  var closeAllTabs = React.useCallback(function () {
    setState(function (s) {
      if (!(s.openTabs && s.openTabs.length) && s.activeTabId == null) return s;
      return Object.assign({}, s, { openTabs: [], activeTabId: null });
    });
  }, []);

  // Rename an open tab in place: remap its id/ref/title (and carry its
  // fileMode across to the new id) WITHOUT changing its position in the tab
  // bar or its preview/edit mode. Used by the Files tree when a file/folder is
  // renamed or moved on disk so an already-open editor tab keeps pointing at
  // the file's new path instead of going stale (a stale tab could silently
  // re-create the old file on save). No-op when no tab matches `oldId`.
  var renameTab = React.useCallback(function (oldId, patchTab) {
    setState(function (s) {
      var prev = s.openTabs || [];
      var idx = prev.findIndex(function (t) { return t.id === oldId; });
      if (idx < 0) return s;
      var newId = patchTab && patchTab.id ? patchTab.id : oldId;
      var openTabs = prev.slice();
      openTabs[idx] = Object.assign({}, openTabs[idx], patchTab);
      var fileModes = s.fileModes;
      if (fileModes && fileModes[oldId] !== undefined && newId !== oldId) {
        fileModes = Object.assign({}, fileModes);
        fileModes[newId] = fileModes[oldId];
        delete fileModes[oldId];
      }
      var activeTabId = s.activeTabId === oldId ? newId : s.activeTabId;
      return Object.assign({}, s, {
        openTabs: openTabs,
        activeTabId: activeTabId,
        fileModes: fileModes,
      });
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

  // ---- terminal (TerminalPanel mounts in the center column when open) ----
  var toggleTerminal = React.useCallback(function () {
    setState(function (s) { return Object.assign({}, s, { terminalOpen: !s.terminalOpen }); });
  }, []);

  // ---- right debug/activity rail (StudioActivity) open/collapsed ----
  var toggleDebug = React.useCallback(function () {
    setState(function (s) { return Object.assign({}, s, { debugOpen: !s.debugOpen }); });
  }, []);

  // ---- command palette / quick-open (B5) ----
  var openPalette = React.useCallback(function (mode) {
    setState(function (s) { return Object.assign({}, s, { paletteOpen: true, paletteMode: mode || "command", paletteQuery: "" }); });
  }, []);
  var closePalette = React.useCallback(function () {
    setState(function (s) { return Object.assign({}, s, { paletteOpen: false }); });
  }, []);
  var togglePalette = React.useCallback(function () {
    setState(function (s) {
      if (s.paletteOpen) return Object.assign({}, s, { paletteOpen: false });
      return Object.assign({}, s, { paletteOpen: true, paletteMode: "command", paletteQuery: "" });
    });
  }, []);

  // ---- new-session flow (shared by the sidebar "+" and the ⌘K palette) ----
  var openNewSession = React.useCallback(function () { setNewSessionOpen(true); }, []);
  var closeNewSession = React.useCallback(function () { setNewSessionOpen(false); }, []);

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
  // Terminal (bottom panel) vertical resize — persisted like the column widths.
  var setTerminalHeight = React.useCallback(function (h) {
    setState(function (s) { return Object.assign({}, s, { terminalHeight: h }); });
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
    closeAllTabs: closeAllTabs,
    renameTab: renameTab,
    toggleSessions: toggleSessions,
    toggleFiles: toggleFiles,
    toggleHidden: toggleHidden,
    toggleFolder: toggleFolder,
    toggleTheme: toggleTheme,
    toggleDensity: toggleDensity,
    toggleTerminal: toggleTerminal,
    toggleDebug: toggleDebug,
    toggleChip: toggleChip,
    setLeftWidth: setLeftWidth,
    setRightWidth: setRightWidth,
    setTerminalHeight: setTerminalHeight,
    patch: patch,
    // B5: palette / quick-open
    openPalette: openPalette,
    closePalette: closePalette,
    togglePalette: togglePalette,
    // FB6: new-session flow (sidebar "+" + palette "New session")
    newSessionOpen: newSessionOpen,
    openNewSession: openNewSession,
    closeNewSession: closeNewSession,
  };
}

// ---------------------------------------------------------------------------
// StudioHeader — SLIM in-shell content sub-header.
//
// The Studio renders as an ordinary page inside the app shell (the app Topbar
// owns brand / global search / theme / user — see chrome.jsx Topbar), so this
// row is intentionally minimal: workspace selector ▾ · ⌘K palette ·
// terminal toggle. On phones it also carries the two panel-drawer toggles
// (left = Sessions+Files, right = Action Required+Activity) since the columns
// collapse to a single document there.
// ---------------------------------------------------------------------------

function StudioHeader({ wid, pushToast, onTogglePalette, onSelectWorkspace, terminalOpen, onToggleTerminal, onToggleLeftPanel, onToggleRightPanel }) {
  var { useResource, apiFetch } = window.primerApi;
  var [menuOpen, setMenuOpen] = React.useState(false);
  // Workspace Settings overlay — restores the orphaned WorkspaceDetail tabs
  // (channels / config / git-log / destroy) via the reused panel components.
  var [settingsOpen, setSettingsOpen] = React.useState(false);

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
      {/* Mobile-only panel-drawer toggle (left: Sessions + Files). */}
      <button
        className="st-hbtn mobile-only touch-target"
        data-testid="studio-left-toggle"
        title="Sessions & Files"
        aria-label="Toggle sessions and files panel"
        onClick={onToggleLeftPanel}
      >
        <Icon name="panel-left" size={15} />
      </button>

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

      {/* Workspace settings (gear) — opens the channels / config / git-log /
          destroy panels reused from WorkspaceDetail. Sits next to the
          workspace selector so it reads as "settings for THIS workspace". */}
      <button
        className="st-hbtn touch-target"
        data-testid="studio-settings-btn"
        title="Workspace settings"
        aria-label="Open workspace settings"
        onClick={function () { setSettingsOpen(true); }}
      >
        <Icon name="settings" size={15} />
      </button>

      <div style={{ flex: 1 }} />

      {settingsOpen && window.WorkspaceSettings && (
        <window.WorkspaceSettings
          wid={wid}
          pushToast={pushToast}
          onClose={function () { setSettingsOpen(false); }}
        />
      )}

      {/* ⌘K command palette (run · jump · search within the workspace). The
          wide trigger is hidden on phones (see .st-palette-trigger in the
          mobile block); a compact search button replaces it there. */}
      <div className="st-palette-trigger" data-testid="palette-trigger" onClick={onTogglePalette}>
        <span style={{ opacity: 0.8 }}>Search · run · jump</span>
        <kbd>⌘K</kbd>
      </div>
      {/* Compact ⌘K affordance for phones (the wide trigger is desktop-only). */}
      <button
        className="st-hbtn mobile-only touch-target"
        data-testid="palette-trigger-mobile"
        title="Command palette (⌘K)"
        aria-label="Open command palette"
        onClick={onTogglePalette}
      >
        <Icon name="search" size={15} />
      </button>

      {/* Terminal toggle (Ctrl-`). */}
      <button
        className={"st-hbtn touch-target" + (terminalOpen ? " is-active" : "")}
        data-testid="terminal-toggle"
        title="Toggle terminal (Ctrl-`)"
        aria-label="Toggle terminal"
        aria-pressed={terminalOpen ? "true" : "false"}
        onClick={onToggleTerminal}
      >
        <Icon name="code" size={15} />
      </button>

      {/* Mobile-only panel-drawer toggle (right: Action Required + Activity). */}
      <button
        className="st-hbtn mobile-only touch-target"
        data-testid="studio-right-toggle"
        title="Action required & activity"
        aria-label="Toggle action-required and activity panel"
        onClick={onToggleRightPanel}
      >
        <Icon name="bell" size={15} />
      </button>
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

function Studio({ wid, pushToast, initialOpen }) {
  var studio = useStudioState(wid, initialOpen);
  var s = studio.state;

  // Thread pushToast into the studio object so StudioCenter / StudioActivity
  // can reach it via studio.pushToast without re-plumbing every sub-component.
  // Falls back to the module-level primerApi.toastPush so non-nil calls always
  // work even when app.jsx hasn't passed the prop yet.
  studio.pushToast = pushToast || (window.primerApi && window.primerApi.toastPush) || null;

  // Remember the last workspace whose Studio was opened so the global "Studio"
  // nav item (chrome.jsx) can re-open it without a workspace picker. Written on
  // every mount / wid change; read in app.jsx's openStudio().
  React.useEffect(function () {
    if (!wid) return;
    try { window.localStorage.setItem("studio:lastWid", wid); } catch (_e) { /* storage disabled */ }
  }, [wid]);

  // Mobile-only panel drawers. On phones st-body collapses to the single
  // center document (CSS); the left (Sessions+Files) and right (Action
  // Required+Activity) columns become slide-over sheets toggled from the slim
  // sub-header. Desktop ignores this state entirely (the columns are static).
  var [leftPanelOpen, setLeftPanelOpen] = React.useState(false);
  var [rightPanelOpen, setRightPanelOpen] = React.useState(false);
  var closePanels = React.useCallback(function () { setLeftPanelOpen(false); setRightPanelOpen(false); }, []);

  // Close the drawers when the workspace changes (route switch) and lock body
  // scroll + wire Escape while either is open — mirrors chrome.jsx MobileNav.
  React.useEffect(function () { closePanels(); }, [wid, closePanels]);
  React.useEffect(function () {
    if (!leftPanelOpen && !rightPanelOpen) return undefined;
    function onKey(e) { if (e.key === "Escape") closePanels(); }
    window.addEventListener("keydown", onKey);
    var prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return function () {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [leftPanelOpen, rightPanelOpen, closePanels]);

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

  // Terminal vertical resize: mirror startResize but on the Y axis. The panel
  // sits BELOW StudioCenter, so dragging the divider UP must GROW the terminal
  // — hence delta = startY - clientY (inverted vs. the column handles). Clamp
  // to a sane band so it can't collapse to nothing or eat the whole viewport;
  // xterm re-fits automatically via its ResizeObserver as the height changes.
  var termDragRef = React.useRef(null);
  function startTermResize(e) {
    e.preventDefault();
    termDragRef.current = { startY: e.clientY, startH: s.terminalHeight };
    function onMove(ev) {
      var d = termDragRef.current;
      if (!d) return;
      var delta = d.startY - ev.clientY;
      var maxH = Math.min(800, Math.round(window.innerHeight * 0.7));
      var h = Math.max(120, Math.min(maxH, d.startH + delta));
      studio.setTerminalHeight(h);
    }
    function onUp() {
      termDragRef.current = null;
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

  // B5: Global keydown — ⌘K/Ctrl-K → toggle palette; ⌘P/Ctrl-P → quick-open;
  // Ctrl-` → toggleTerminal; Escape → close whichever overlay is open.
  // Registered on the window (not just the Studio div) so it fires regardless
  // of focus position within the IDE shell.
  React.useEffect(function () {
    function onKeyDown(e) {
      var mod = e.metaKey || e.ctrlKey;
      if (mod && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        studio.togglePalette();
      } else if (mod && (e.key === "p" || e.key === "P")) {
        e.preventDefault();
        studio.openPalette("quickopen");
      } else if (e.ctrlKey && e.key === "`") {
        e.preventDefault();
        studio.toggleTerminal();
      } else if (e.key === "Escape") {
        if (s.paletteOpen) studio.closePalette();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return function () { window.removeEventListener("keydown", onKeyDown); };
  }, [s.paletteOpen, studio.togglePalette, studio.openPalette, studio.closePalette, studio.toggleTerminal]);

  return (
    <div
      className="st-root"
      data-theme={s.theme}
      data-density={s.density}
      data-testid="studio-root"
      // --st-right-w collapses to the 40px rail directly from state (not only
      // via the :has(.is-collapsed) CSS) so the debug rail width is bulletproof
      // regardless of :has() support, and the header Debug toggle controls it.
      style={{ "--st-left-w": s.leftWidth + "px", "--st-right-w": (s.debugOpen ? s.rightWidth : 40) + "px", "--st-term-h": s.terminalHeight + "px" }}
    >
      <StudioHeader
        wid={wid}
        pushToast={studio.pushToast}
        onTogglePalette={studio.togglePalette}
        onSelectWorkspace={selectWorkspace}
        terminalOpen={s.terminalOpen}
        onToggleTerminal={studio.toggleTerminal}
        onToggleLeftPanel={function () { setRightPanelOpen(false); setLeftPanelOpen(function (o) { return !o; }); }}
        onToggleRightPanel={function () { setLeftPanelOpen(false); setRightPanelOpen(function (o) { return !o; }); }}
      />

      <div className="st-body">
        {/* Mobile backdrop: dims the center doc while a panel drawer is open.
            Desktop never shows this (st-panel-overlay is mobile-only in CSS). */}
        {(leftPanelOpen || rightPanelOpen) && (
          <div className="st-panel-overlay mobile-only" data-testid="studio-panel-overlay" onClick={closePanels} />
        )}

        {/* ---- LEFT: B2 StudioSidebar (sessions + files tree) ---- On phones
            this column is an off-canvas sheet toggled from the sub-header. ---- */}
        <div
          className={"st-col st-col-left" + (leftPanelOpen ? " is-drawer-open" : "")}
          data-testid="studio-sidebar"
        >
          <StudioSidebar wid={wid} studio={studio} />
        </div>

        <div className="st-resize desktop-only" onMouseDown={function (e) { startResize("left", e); }} data-testid="studio-resize-left" />

        {/* ---- CENTER: B3 StudioCenter (tabs + active panel) + P7 terminal ---- */}
        <div className="st-col st-col-center" data-testid="studio-center">
          <StudioCenter wid={wid} studio={studio} />
          {/* Horizontal splitter: drag to resize the terminal (writes
              terminalHeight → --st-term-h). Mirrors the column handles. */}
          {s.terminalOpen && (
            <div className="st-term-resize" data-testid="terminal-resize" onMouseDown={startTermResize} />
          )}
          {/* Collapsible bottom terminal panel (Ctrl-` / the header toggle
              flip terminalOpen). Only mounted while open — unmounting tears
              down every tab's xterm instance + WS (see studio-terminal.jsx),
              which matches the terminal's ephemeral, no-reconnect v1 design. */}
          {s.terminalOpen && <TerminalPanel wid={wid} studio={studio} />}
        </div>

        <div className="st-resize desktop-only" onMouseDown={function (e) { startResize("right", e); }} data-testid="studio-resize-right" />

        {/* ---- RIGHT: B4 StudioActivity (action required + workspace tap) ----
            Off-canvas sheet on phones (right edge); static column on desktop. */}
        <div
          className={"st-col st-col-right" + (rightPanelOpen ? " is-drawer-open" : "")}
          data-testid="studio-activity"
        >
          <StudioActivity wid={wid} studio={studio} />
        </div>
      </div>

      {/* B5: Command palette overlay (⌘K). Rendered at Studio root so it sits
          above all three columns. paletteMode "command" vs "quickopen" selects
          which component renders; both share the same open/close state. */}
      {s.paletteOpen && s.paletteMode === "command" && (
        <StudioCommandPalette
          wid={wid}
          studio={studio}
          open={s.paletteOpen}
          onClose={studio.closePalette}
        />
      )}
      {s.paletteOpen && s.paletteMode === "quickopen" && (
        <QuickOpen
          wid={wid}
          studio={studio}
          open={s.paletteOpen}
          onClose={studio.closePalette}
        />
      )}
    </div>
  );
}

// No-build exports — every component/hook reachable from app.jsx + B2–B4.
window.Studio = Studio;
window.StudioHeader = StudioHeader;
window.useStudioState = useStudioState;
