/* global React, ST2_RunsRail, ST2_FilesRail */
// Studio revamp - the rail shell (ui/studio/STUDIO-WIRING.md §5).
//
// StudioSidebar stacked Sessions above Files, so both were half-height and both
// were always half-collapsed. One rail with two modes gives whichever one you
// are actually using the full column, at the cost of a click to switch - and
// switching is rare, because runs and files are different jobs.
//
// leftWidth's drag + 180..480 clamp are unchanged and still live in studio.jsx;
// this component only owns which mode is showing.

var ST2_RAIL_MODES = [
  { id: "runs", label: "Runs" },
  { id: "files", label: "Files" },
];

function ST2_RailModeSwitch({ mode, onPick }) {
  return (
    <div
      className="row"
      style={{
        margin: "8px 8px 4px", padding: 2, gap: 2, flex: "0 0 auto",
        background: "var(--bg-1)", borderRadius: 7,
        border: "1px solid var(--border)",
      }}
    >
      {ST2_RAIL_MODES.map(function (m) {
        var active = mode === m.id;
        return (
          <button
            key={m.id}
            data-testid={"rail-mode-" + m.id}
            aria-pressed={active}
            onClick={function () { onPick(m.id); }}
            style={{
              flex: 1, padding: "4px 0", borderRadius: 5, cursor: "pointer",
              border: "none",
              background: active ? "var(--bg-elev)" : "transparent",
              color: active ? "var(--text)" : "var(--text-3)",
              fontSize: "var(--fs-12)",
              fontWeight: active ? 600 : 400,
              boxShadow: active ? "0 1px 2px rgba(0,0,0,0.18)" : "none",
            }}
          >{m.label}</button>
        );
      })}
    </div>
  );
}

function StudioRail({ wid, studio }) {
  var mode = studio.state.railMode || "runs";
  return (
    <div
      data-testid="studio-rail"
      className="col"
      style={{ height: "100%", minHeight: 0, gap: 0, overflow: "hidden" }}
    >
      <ST2_RailModeSwitch
        mode={mode}
        onPick={function (id) { studio.setRailMode(id); }}
      />
      {mode === "files"
        ? <ST2_FilesRail wid={wid} studio={studio} />
        : <ST2_RunsRail wid={wid} studio={studio} />}
    </div>
  );
}

window.StudioRail = StudioRail;
window.ST2_RailModeSwitch = ST2_RailModeSwitch;
window.ST2_RAIL_MODES = ST2_RAIL_MODES;
