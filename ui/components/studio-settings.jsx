/* global React, Icon, Btn, Banner */
// Studio → Workspace Settings overlay.
//
// The Studio (studio.jsx) replaced the old WorkspaceDetail page at
// /workspaces/:wid, absorbing only its files / sessions / activity tabs. The
// remaining WorkspaceDetail tabs — channels (reply-binding), config, git-log,
// and destroy — became unreachable. This overlay restores them by RE-USING the
// very same panel components WorkspaceDetail rendered (surfaced on window.* from
// workspaces.jsx): WS_ChannelsTab · WS_ConfigTab · WS_LogTab · WS_DestroyTab.
// Nothing about those panels is reimplemented here; we only build the resource
// objects they expect and lay them out behind a left rail of sections.
//
// No-build scope rules (see studio.jsx): top-level declarations use `var`;
// helpers are prefixed SS_; every component is exported via `window.X = X`.

// The section rail. `id` keys the active section; `label` is the visible rail
// text; `icon` mirrors the icon WorkspaceDetail's tab strip used for the tab.
var SS_SECTIONS = [
  { id: "channels", label: "Channels", icon: "bell" },
  { id: "config", label: "Config", icon: "settings" },
  { id: "log", label: "Git log", icon: "git-commit" },
  { id: "destroy", label: "Destroy", icon: "trash", danger: true },
];

// ---------------------------------------------------------------------------
// WorkspaceSettings — modal surface with a left rail + a reused detail panel.
//
// Props: { wid, onClose, pushToast }. Builds the `ws` (workspace-detail) and
// `sessionsForBadge` (workspace-sessions) resources the reused panels consume,
// keyed identically to WorkspaceDetail so both share the useResource cache.
// ---------------------------------------------------------------------------

function WorkspaceSettings({ wid, onClose, pushToast }) {
  var { useResource, apiFetch } = window.primerApi;
  var [section, setSection] = React.useState("channels");

  // Same cache keys + fetchers WorkspaceDetail uses, so the reused panels see
  // identical data and their invalidations line up with the rest of the app.
  var ws = useResource(
    "workspace-detail:" + wid,
    function (signal) { return apiFetch("GET", "/workspaces/" + encodeURIComponent(wid), null, { signal }); },
    { deps: [wid] }
  );
  var sessionsForBadge = useResource(
    "workspace-sessions:" + wid,
    function (signal) { return apiFetch("GET", "/workspaces/" + encodeURIComponent(wid) + "/sessions?limit=200", null, { signal }); },
    { pollMs: 5000, deps: [wid] }
  );

  // Esc-to-close + focus management. This overlay is a raw modal (not the shared
  // Modal) so it can keep its flush left-rail layout, so we wire the keyboard +
  // focus affordances the shared Modal otherwise provides: Escape closes it, and
  // focus moves into the dialog on open and back to the trigger on close.
  var dialogRef = React.useRef(null);
  React.useEffect(function () {
    var prevFocus = document.activeElement;
    function onKey(e) {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose && onClose();
      }
    }
    window.addEventListener("keydown", onKey);
    if (dialogRef.current) dialogRef.current.focus();
    return function () {
      window.removeEventListener("keydown", onKey);
      if (prevFocus && document.contains(prevFocus) && typeof prevFocus.focus === "function") {
        prevFocus.focus();
      }
    };
  }, []);

  // Resolve the reused panel components from window.* at render time so this
  // file is load-order-independent from workspaces.jsx (which exports them).
  var ChannelsTab = window.WS_ChannelsTab;
  var ConfigTab = window.WS_ConfigTab;
  var LogTab = window.WS_LogTab;
  var DestroyTab = window.WS_DestroyTab;

  function renderPanel() {
    if (section === "channels") {
      return ChannelsTab
        ? <ChannelsTab wid={wid} ws={ws} pushToast={pushToast} />
        : <SS_Missing name="channels" />;
    }
    if (section === "config") {
      return ConfigTab ? <ConfigTab wid={wid} ws={ws} /> : <SS_Missing name="config" />;
    }
    if (section === "log") {
      return LogTab ? <LogTab wid={wid} /> : <SS_Missing name="log" />;
    }
    if (section === "destroy") {
      return DestroyTab
        ? <DestroyTab wid={wid} pushToast={pushToast} sessionsForBadge={sessionsForBadge} />
        : <SS_Missing name="destroy" />;
    }
    return null;
  }

  return (
    <div className="modal-overlay" data-testid="workspace-settings" onClick={onClose}>
      <div
        className="modal"
        ref={dialogRef}
        tabIndex={-1}
        style={{ width: "min(920px, 94vw)", maxWidth: "94vw", outline: "none" }}
        role="dialog"
        aria-modal="true"
        aria-label="Workspace settings"
        onClick={function (e) { e.stopPropagation(); }}
      >
        <div className="modal-h">
          <span className="title">
            Workspace settings <span className="mono muted text-sm">· {wid}</span>
          </span>
          <button className="close" onClick={onClose} aria-label="Close"><Icon name="x" size={14} /></button>
        </div>
        <div
          className="modal-b"
          style={{ padding: 0, display: "flex", minHeight: 420, maxHeight: "72vh", overflow: "hidden" }}
        >
          {/* Left rail of sections. */}
          <div
            className="st-settings-rail"
            style={{
              flex: "0 0 200px",
              borderRight: "1px solid var(--border)",
              padding: "10px 8px",
              display: "flex",
              flexDirection: "column",
              gap: 2,
              overflow: "auto",
            }}
          >
            {SS_SECTIONS.map(function (sec) {
              var active = section === sec.id;
              return (
                <button
                  key={sec.id}
                  type="button"
                  data-testid={"workspace-settings-nav:" + sec.id}
                  onClick={function () { setSection(sec.id); }}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    textAlign: "left",
                    padding: "8px 10px",
                    borderRadius: 6,
                    border: "1px solid " + (active ? "var(--border)" : "transparent"),
                    background: active ? "var(--bg-hover)" : "transparent",
                    color: sec.danger ? "var(--red)" : active ? "var(--text)" : "var(--text-2)",
                    fontSize: 12.5,
                    fontWeight: active ? 600 : 400,
                    cursor: "pointer",
                  }}
                >
                  <Icon name={sec.icon} size={13} style={{ color: sec.danger ? "var(--red)" : undefined }} />
                  {sec.label}
                </button>
              );
            })}
          </div>

          {/* Section body — the reused WorkspaceDetail panel for `section`. */}
          <div style={{ flex: 1, minWidth: 0, overflow: "auto", background: "var(--bg)" }}>
            {renderPanel()}
          </div>
        </div>
      </div>
    </div>
  );
}

// Fallback shown only if workspaces.jsx failed to export a panel (defensive —
// should never render in a healthy bundle).
function SS_Missing({ name }) {
  return (
    <div style={{ padding: 16 }}>
      <Banner
        kind="error"
        title={"Panel unavailable"}
        detail={"The reused \"" + name + "\" panel is not registered (window.WS_* export missing)."}
      />
    </div>
  );
}

// No-build export.
window.WorkspaceSettings = WorkspaceSettings;
