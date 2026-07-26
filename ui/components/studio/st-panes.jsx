/* global React, Icon, StudioCenter */
// Studio revamp - the two-pane center (ui/studio/STUDIO-WIRING.md §6).
//
// A diff that replaces the transcript that produced it destroys the context you
// opened it from. PaneHost adds a companion pane so the two sit side by side.
//
// It wraps two instances of the EXISTING StudioCenter body, parameterised on
// which tab array each renders - so SessionAgentPanel, SessionGraphPanel,
// FilePanel and the _SLS_Frame transcript renderer are all untouched, and both
// panes get the same CenterTabs with the same testids.
//
// There is no asideOpen flag: the pane is open exactly when it holds tabs.

// Below this width a second column cannot hold a transcript AND a diff, so the
// companion becomes an overlay sheet. useViewport()'s width is enough - no
// isMobile fork.
var ST2_PANE_STACK_W = 1280;

function ST2_AsideHeader({ narrow, onClose, onMoveBack }) {
  return (
    <div
      className="row"
      style={{
        height: 30, flex: "0 0 auto", alignItems: "center", gap: 6,
        padding: "0 8px", borderBottom: "1px solid var(--border)",
        background: "var(--bg-elev)",
      }}
    >
      <span className="muted" style={{ fontSize: "var(--fs-11)", fontWeight: 600, letterSpacing: "0.04em" }}>
        {narrow ? "BESIDE" : "COMPANION"}
      </span>
      <span style={{ marginLeft: "auto", display: "flex", gap: 2 }}>
        <button
          className="st-row-action"
          data-testid="aside-move-back"
          title="Move back to the main pane (Alt-\)"
          aria-label="Move back to the main pane"
          onClick={onMoveBack}
        >
          <Icon name="panel-left" size={12} />
        </button>
        <button
          className="st-row-action"
          data-testid="aside-close"
          title="Close the companion pane"
          aria-label="Close the companion pane"
          onClick={onClose}
        >
          <Icon name="x" size={12} />
        </button>
      </span>
    </div>
  );
}

function PaneHost({ wid, studio }) {
  var s = studio.state;
  var asideTabs = s.asideTabs || [];
  var asideOpen = asideTabs.length > 0;

  // Unconditional hook call - useViewport is a foundation export, registered
  // before any component runs.
  var vp = window.primerApi.useViewport();
  var narrow = (vp.width || window.innerWidth || 1440) < ST2_PANE_STACK_W;

  // Drag the divider. Widening the companion means dragging LEFT, so the delta
  // is inverted relative to the left rail's handle.
  var dragRef = React.useRef(null);
  function startAsideResize(e) {
    dragRef.current = { startX: e.clientX, startW: s.asideWidth || 520 };
    e.preventDefault();
  }
  React.useEffect(function () {
    function onMove(e) {
      if (!dragRef.current) return;
      studio.setAsideWidth(dragRef.current.startW + (dragRef.current.startX - e.clientX));
    }
    function onUp() { dragRef.current = null; }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return function () {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [studio]);

  var aside = asideOpen ? (
    <div
      data-testid="studio-aside"
      className="col"
      style={narrow ? {
        // Overlay sheet: same content, laid over the primary pane rather than
        // squeezing it into unusability.
        position: "absolute", top: 0, right: 0, bottom: 0,
        width: "min(560px, 92%)", zIndex: 30, gap: 0,
        background: "var(--bg-2)", borderLeft: "1px solid var(--border)",
        boxShadow: "-8px 0 24px rgba(0,0,0,0.28)",
      } : {
        flex: "0 0 auto", width: s.asideWidth || 520, minWidth: 0, gap: 0,
        borderLeft: "1px solid var(--border)", background: "var(--bg-2)",
      }}
    >
      <ST2_AsideHeader
        narrow={narrow}
        onClose={studio.closeAllAsideTabs}
        onMoveBack={studio.moveTabAcross}
      />
      <div style={{ flex: 1, minHeight: 0 }}>
        <StudioCenter
          wid={wid}
          studio={studio}
          testId="studio-aside-inner"
          tabs={asideTabs}
          activeId={s.activeAsideTabId}
          onFocus={studio.focusAsideTab}
          onClose={studio.closeAsideTab}
          onCloseAll={studio.closeAllAsideTabs}
        />
      </div>
    </div>
  ) : null;

  return (
    <div
      data-testid="studio-panes"
      style={{
        display: "flex", flex: 1, minHeight: 0, minWidth: 0,
        position: "relative", overflow: "hidden",
      }}
    >
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <StudioCenter wid={wid} studio={studio} />
      </div>
      {asideOpen && !narrow ? (
        <div
          className="st-resize desktop-only"
          data-testid="aside-resize"
          onMouseDown={startAsideResize}
        />
      ) : null}
      {aside}
    </div>
  );
}

// ---------------------------------------------------------------------------
// WROTE-row derivation (§6.1)
//
// Which tool calls actually wrote a file, and to what path. Kept as pure logic
// separate from any rendering because the two halves unblock at different
// times: this half is decidable from the tool call alone, while the row's
// "+n/-m  open diff" needs the per-turn file trail that no endpoint exposes yet
// (see the plan's §0.2). Task 7 lands the row against this function.
//
// The allowlist is deliberate. workspace__exec can obviously write files - via
// a redirect, a heredoc, sed -i, a build step - but its argv cannot be read
// reliably enough to say WHICH file, and a WROTE row pointing at the wrong path
// is worse than no row. Inferring is out; only tools whose contract names the
// path count.
//
// These are the SCOPED ids the runtime emits (`workspace__<bare>`, see
// tool_manager.WORKSPACE_TOOLSET_ID + SandboxWrite/SandboxEdit.id). WIRING §6
// names `fs__write_file` / `fs__apply_patch`, which do not exist in this
// codebase - matching on those would have quietly matched nothing forever.
var ST2_WRITE_TOOLS = {
  workspace__write: 1,
  workspace__edit: 1,
};

// ST2_wroteFromToolCall(row) -> { path } | null
function ST2_wroteFromToolCall(row) {
  var m = row || {};
  var name = m.name || m.tool_name || "";
  if (!ST2_WRITE_TOOLS[name]) return null;
  var args = m.args || m.arguments || {};
  var path = args.path || args.file_path || args.file || null;
  if (!path || typeof path !== "string") return null;
  return { path: path };
}

window.PaneHost = PaneHost;
window.ST2_PANE_STACK_W = ST2_PANE_STACK_W;
window.ST2_wroteFromToolCall = ST2_wroteFromToolCall;
window.ST2_WRITE_TOOLS = ST2_WRITE_TOOLS;
