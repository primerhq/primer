/* global React */
// The right rail: Attention above Activity, both scoped to the derived
// workspace context and REUSING the classic components wholesale
// (AttentionBar from st-attention.jsx, WorkspaceTap from
// workspace-tap.jsx). Trial note: both are per-workspace surfaces; a
// deployment-wide attention/tap feed needs a global endpoint (future
// backend work) and the empty state says so.
function S2_Right() {
  const ctx = window.S2_Ctx.useCtx();
  const emptyCopy = (what) => (
    <div style={{ padding: "8px 12px", color: "var(--text-4)",
      fontSize: "var(--fs-12)" }}>
      Open a session to scope {what} to its workspace. A deployment-wide
      feed needs a global endpoint (future backend work).
    </div>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: 0,
      height: "100%", position: "relative" }}>
      <div style={{ padding: "9px 12px 5px", fontSize: "var(--fs-11)",
        letterSpacing: ".08em", textTransform: "uppercase",
        color: "var(--text-3)", fontWeight: 600, flex: "none" }}>
        attention
      </div>
      <div data-testid="s2-attention" style={{ flex: "none",
        padding: "0 8px 8px", position: "relative" }}>
        {ctx.ws
          ? <window.AttentionBar wid={ctx.ws} />
          : emptyCopy("attention")}
      </div>
      <div style={{ padding: "9px 12px 5px", fontSize: "var(--fs-11)",
        letterSpacing: ".08em", textTransform: "uppercase",
        color: "var(--text-3)", fontWeight: 600, flex: "none",
        borderTop: "1px solid var(--border)" }}>
        activity
      </div>
      <div data-testid="s2-activity" style={{ flex: 1, minHeight: 0,
        overflowY: "auto", padding: "0 8px 8px" }}>
        {ctx.ws
          ? <window.WorkspaceTap wid={ctx.ws} fillHeight={true} />
          : emptyCopy("the activity feed")}
      </div>
    </div>
  );
}
window.S2_Right = S2_Right;
