/* global React, Icon */
// studio-palette.jsx — StudioCommandPalette (⌘K) + QuickOpen (⌘P) overlays for Studio.
//
// Exports:
//   window.StudioCommandPalette  ({ wid, studio, open, onClose })
//   window.QuickOpen             ({ wid, studio, open, onClose })
//
// NOTE: this palette is deliberately named StudioCommandPalette (not
// CommandPalette) so it does NOT collide with chrome.jsx's app-global
// `CommandPalette` in the flat, no-IIFE bundle scope. Before the rename the
// later-loaded Studio declaration shadowed chrome's, silently killing the
// top-bar Search box + ⌘K on every non-Studio page.
//
// No-build scope rules (see workspace-tap.jsx): top-level declarations use
// `var`; helpers are prefixed STP_ to avoid global collisions.

// ---------------------------------------------------------------------------
// Fuzzy match — simple subsequence: every char of `query` appears in `text`
// in order (case-insensitive). Returns true/false.
// ---------------------------------------------------------------------------

function STP_fuzzy(text, query) {
  if (!query) return true;
  var t = text.toLowerCase();
  var q = query.toLowerCase();
  var ti = 0;
  for (var qi = 0; qi < q.length; qi++) {
    var idx = t.indexOf(q[qi], ti);
    if (idx < 0) return false;
    ti = idx + 1;
  }
  return true;
}

// ---------------------------------------------------------------------------
// StudioCommandPalette — modal overlay; search input + fuzzy-filtered list.
//
// Commands:
//   - Each open session → studio.openTab(session)
//   - Library nav shortcuts (/graphs, /agents, /chats, /knowledge)
//   - Actions: New session, Toggle terminal, Toggle theme, Toggle density
//
// Props: { wid, studio, open, onClose }
// ---------------------------------------------------------------------------

function StudioCommandPalette({ wid, studio, open, onClose }) {
  var { apiFetch, useResource } = window.primerApi;
  var [query, setQuery] = React.useState("");
  var [cursor, setCursor] = React.useState(0);
  var inputRef = React.useRef(null);

  // Fetch sessions for this workspace so they appear as palette entries.
  var sessionsRes = useResource(
    "palette:sessions:" + wid,
    function (signal) {
      return apiFetch("GET", "/workspaces/" + encodeURIComponent(wid) + "/sessions?limit=200", null, { signal });
    },
    { pollMs: 0, deps: [wid] }
  );
  var sessions = Array.isArray(sessionsRes.data && sessionsRes.data.items) ? sessionsRes.data.items : [];

  // Auto-focus the input when palette opens; clear query on open.
  React.useEffect(function () {
    if (!open) return;
    setQuery("");
    setCursor(0);
    var t = setTimeout(function () {
      if (inputRef.current) inputRef.current.focus();
    }, 30);
    return function () { clearTimeout(t); };
  }, [open]);

  // Build command items.
  var allItems = React.useMemo(function () {
    var items = [];

    // Sessions
    sessions.forEach(function (s) {
      items.push({
        id: "session:" + s.id,
        label: s.name || s.id,
        group: s.binding && s.binding.graph_id ? "graph" : "agent",
        icon: s.binding && s.binding.graph_id ? "◈" : "◆",
        run: function () {
          studio.openTab({
            id: "session:" + s.id,
            kind: "session",
            ref: s.id,
            title: s.name || s.id,
          });
        },
      });
    });

    // Library nav
    var navItems = [
      { id: "nav:graphs", label: "Open Graphs library", group: "nav", icon: "⊞", run: function () { window.location.hash = "#/graphs"; } },
      { id: "nav:agents", label: "Open Agents library", group: "nav", icon: "⊞", run: function () { window.location.hash = "#/agents"; } },
      { id: "nav:chats", label: "Open Chats", group: "nav", icon: "⊞", run: function () { window.location.hash = "#/chats"; } },
      { id: "nav:knowledge", label: "Open Knowledge", group: "nav", icon: "⊞", run: function () { window.location.hash = "#/knowledge/collections"; } },
    ];
    navItems.forEach(function (n) { items.push(n); });

    // Actions
    var actionItems = [
      {
        id: "action:new-session",
        label: "New session",
        group: "action",
        icon: "＋",
        run: function () {
          if (studio.pushToast) {
            studio.pushToast({ kind: "info", title: "New session", detail: "Use the + button in the Sessions sidebar to create a session." });
          }
        },
      },
      {
        id: "action:toggle-terminal",
        label: "Toggle terminal",
        group: "action",
        icon: "⌥",
        run: function () { studio.toggleTerminal(); },
      },
      {
        id: "action:toggle-theme",
        label: "Toggle theme",
        group: "action",
        icon: "◐",
        run: function () { studio.toggleTheme(); },
      },
      {
        id: "action:toggle-density",
        label: "Toggle density",
        group: "action",
        icon: "☰",
        run: function () { studio.toggleDensity(); },
      },
    ];
    actionItems.forEach(function (a) { items.push(a); });

    return items;
  }, [sessions, studio]);

  // Filter by query.
  var filtered = React.useMemo(function () {
    if (!query) return allItems.slice(0, 8);
    var results = [];
    for (var i = 0; i < allItems.length; i++) {
      if (STP_fuzzy(allItems[i].label, query)) results.push(allItems[i]);
      if (results.length >= 8) break;
    }
    return results;
  }, [allItems, query]);

  // Clamp cursor when list changes.
  React.useEffect(function () {
    setCursor(function (c) { return Math.min(c, Math.max(0, filtered.length - 1)); });
  }, [filtered.length]);

  function runItem(item) {
    item.run();
    onClose();
  }

  function onKeyDown(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor(function (c) { return Math.min(c + 1, filtered.length - 1); });
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor(function (c) { return Math.max(c - 1, 0); });
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (filtered[cursor]) runItem(filtered[cursor]);
    } else if (e.key === "Escape") {
      e.stopPropagation();
      onClose();
    }
  }

  if (!open) return null;

  return (
    <div
      style={{
        position: "fixed", inset: 0, background: "oklch(0 0 0 / 0.5)",
        zIndex: 60, display: "flex", justifyContent: "center", paddingTop: "12vh",
      }}
      onClick={onClose}
      data-testid="command-palette"
    >
      <div
        style={{
          width: 560, maxWidth: "calc(100vw - 32px)", height: "max-content",
          maxHeight: "64vh", background: "var(--bg-1)", border: "1px solid var(--border-strong)",
          borderRadius: 12, boxShadow: "var(--shadow)", display: "flex",
          flexDirection: "column", overflow: "hidden",
        }}
        onClick={function (e) { e.stopPropagation(); }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
          <span style={{ color: "var(--text-3)", fontSize: 15 }}>⌘</span>
          <input
            data-testid="palette-input"
            ref={inputRef}
            value={query}
            onChange={function (e) { setQuery(e.target.value); setCursor(0); }}
            onKeyDown={onKeyDown}
            placeholder="Search sessions, files, actions…"
            style={{
              flex: 1, background: "transparent", border: 0, outline: "none",
              color: "var(--text)", fontSize: 14,
            }}
          />
          <kbd style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 11, color: "var(--text-3)", background: "var(--bg-2)", border: "1px solid var(--border)", borderRadius: 4, padding: "1px 6px" }}>esc</kbd>
        </div>
        <div style={{ overflow: "auto", padding: 6, minHeight: 0 }}>
          {filtered.length === 0 && (
            <div style={{ padding: 20, textAlign: "center", color: "var(--text-4)", fontSize: 12 }}>No matches.</div>
          )}
          {filtered.map(function (item, idx) {
            const active = idx === cursor;
            return (
              <div
                key={item.id}
                data-testid="palette-item"
                onClick={function () { runItem(item); }}
                onMouseEnter={function () { setCursor(idx); }}
                style={{
                  display: "flex", gap: 10, padding: "8px 10px", borderRadius: 7,
                  cursor: "pointer",
                  background: active ? "var(--bg-active)" : "transparent",
                  alignItems: "center",
                }}
              >
                <span style={{ width: 18, display: "grid", placeItems: "center", color: "var(--text-3)", flex: "0 0 auto", fontSize: 13 }}>{item.icon}</span>
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text)" }}>{item.label}</span>
                <span style={{ fontSize: 10, fontFamily: "'IBM Plex Mono',monospace", color: "var(--text-4)", textTransform: "uppercase", letterSpacing: ".4px", flex: "0 0 auto" }}>{item.group}</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// QuickOpen — modal; fuzzy file open over GET …/files?recursive=true.
//
// Selecting a file calls:
//   studio.openTab({ id:"file:"+path, kind:"file", ref:path, title:basename })
//
// Props: { wid, studio, open, onClose }
// ---------------------------------------------------------------------------

function QuickOpen({ wid, studio, open, onClose }) {
  var { apiFetch, useResource } = window.primerApi;
  var [query, setQuery] = React.useState("");
  var [cursor, setCursor] = React.useState(0);
  var inputRef = React.useRef(null);

  // Fetch flat file list for this workspace.
  var filesRes = useResource(
    "quickopen:files:" + wid,
    function (signal) {
      return apiFetch("GET", "/workspaces/" + encodeURIComponent(wid) + "/files?recursive=true", null, { signal });
    },
    { pollMs: 0, deps: [wid] }
  );
  // The files endpoint may return { items: [...] } or an array directly.
  var rawFiles = filesRes.data;
  var allFiles = React.useMemo(function () {
    if (!rawFiles) return [];
    if (Array.isArray(rawFiles)) return rawFiles;
    if (Array.isArray(rawFiles.items)) return rawFiles.items;
    return [];
  }, [rawFiles]);

  // Auto-focus on open; clear state.
  React.useEffect(function () {
    if (!open) return;
    setQuery("");
    setCursor(0);
    var t = setTimeout(function () {
      if (inputRef.current) inputRef.current.focus();
    }, 30);
    return function () { clearTimeout(t); };
  }, [open]);

  // Filter files by query.
  var filtered = React.useMemo(function () {
    var results = [];
    for (var i = 0; i < allFiles.length; i++) {
      var f = allFiles[i];
      // Skip directories
      if (f.is_dir || f.isDir) continue;
      var path = f.path || f.name || "";
      if (STP_fuzzy(path, query)) {
        results.push({ path: path, name: STP_basename(path) });
      }
      if (results.length >= 10) break;
    }
    return results;
  }, [allFiles, query]);

  // Clamp cursor.
  React.useEffect(function () {
    setCursor(function (c) { return Math.min(c, Math.max(0, filtered.length - 1)); });
  }, [filtered.length]);

  function openFile(path) {
    var name = STP_basename(path);
    studio.openTab({
      id: "file:" + path,
      kind: "file",
      ref: path,
      title: name,
    });
    onClose();
  }

  function onKeyDown(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor(function (c) { return Math.min(c + 1, filtered.length - 1); });
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor(function (c) { return Math.max(c - 1, 0); });
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (filtered[cursor]) openFile(filtered[cursor].path);
    } else if (e.key === "Escape") {
      e.stopPropagation();
      onClose();
    }
  }

  if (!open) return null;

  return (
    <div
      style={{
        position: "fixed", inset: 0, background: "oklch(0 0 0 / 0.5)",
        zIndex: 60, display: "flex", justifyContent: "center", paddingTop: "12vh",
      }}
      onClick={onClose}
      data-testid="quick-open"
    >
      <div
        style={{
          width: 560, maxWidth: "calc(100vw - 32px)", height: "max-content",
          maxHeight: "64vh", background: "var(--bg-1)", border: "1px solid var(--border-strong)",
          borderRadius: 12, boxShadow: "var(--shadow)", display: "flex",
          flexDirection: "column", overflow: "hidden",
        }}
        onClick={function (e) { e.stopPropagation(); }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
          <span style={{ color: "var(--text-3)", fontSize: 15 }}>⌕</span>
          <input
            data-testid="quick-open-input"
            ref={inputRef}
            value={query}
            onChange={function (e) { setQuery(e.target.value); setCursor(0); }}
            onKeyDown={onKeyDown}
            placeholder="Go to file…"
            style={{
              flex: 1, background: "transparent", border: 0, outline: "none",
              color: "var(--text)", fontSize: 14,
            }}
          />
          <kbd style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 11, color: "var(--text-3)", background: "var(--bg-2)", border: "1px solid var(--border)", borderRadius: 4, padding: "1px 6px" }}>esc</kbd>
        </div>
        <div style={{ overflow: "auto", padding: 6, minHeight: 0 }}>
          {allFiles.length === 0 && filesRes.loading && (
            <div style={{ padding: 20, textAlign: "center", color: "var(--text-4)", fontSize: 12 }}>Loading files…</div>
          )}
          {allFiles.length > 0 && filtered.length === 0 && (
            <div style={{ padding: 20, textAlign: "center", color: "var(--text-4)", fontSize: 12 }}>No matches.</div>
          )}
          {filtered.map(function (f, idx) {
            const active = idx === cursor;
            return (
              <div
                key={f.path}
                data-testid="quick-open-item"
                onClick={function () { openFile(f.path); }}
                onMouseEnter={function () { setCursor(idx); }}
                style={{
                  display: "flex", gap: 10, padding: "8px 10px", borderRadius: 7,
                  cursor: "pointer",
                  background: active ? "var(--bg-active)" : "transparent",
                  alignItems: "center",
                }}
              >
                <span style={{ width: 18, display: "grid", placeItems: "center", color: "var(--text-3)", flex: "0 0 auto", fontSize: 13 }}>◻</span>
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text)" }}>{f.path}</span>
                <span style={{ fontSize: 10, fontFamily: "'IBM Plex Mono',monospace", color: "var(--text-4)", textTransform: "uppercase", letterSpacing: ".4px", flex: "0 0 auto" }}>file</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function STP_basename(path) {
  if (!path) return "";
  var parts = path.replace(/\\/g, "/").split("/");
  return parts[parts.length - 1] || path;
}

// ---------------------------------------------------------------------------
// No-build exports
// ---------------------------------------------------------------------------
window.StudioCommandPalette = StudioCommandPalette;
window.QuickOpen = QuickOpen;
