/* global React */
// GB_ReadinessChip / GB_ReadinessPopover - the honest draft state.
// Drafts always save; these list what blocks a RUN, each with a one-click fix.
// WIRING.md §10.

function GB_ReadinessChip({ validation, serverIssues, open, onToggle }) {
  const runnable = (validation && validation.runnable) || [];
  const blocking = (validation && validation.blocking) || [];
  const server = serverIssues || [];
  const count = runnable.length + blocking.length + server.length;
  const ok = count === 0;
  const tint = blocking.length ? "var(--red)" : ok ? "var(--green)" : "var(--amber)";

  return (
    <div
      data-testid="gb-readiness-chip"
      onClick={onToggle}
      className="row"
      style={{
        gap: 6, alignItems: "center", padding: "5px 10px", borderRadius: 999, cursor: "pointer",
        background: `color-mix(in oklab, ${tint} 14%, transparent)`,
        border: `1px solid color-mix(in oklab, ${tint} 32%, transparent)`,
        color: tint, fontSize: "var(--fs-11)", fontWeight: 500, whiteSpace: "nowrap",
      }}
    >
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: tint }} />
      {ok ? "Ready to run" : `Draft · ${count} thing${count === 1 ? "" : "s"} before it can run`}
      <span style={{ opacity: 0.7 }}>{open ? "▴" : "▾"}</span>
    </div>
  );
}

function GB_ReadinessPopover({ validation, serverIssues, draft, onFix, onSelectNode, onClose }) {
  const blocking = (validation && validation.blocking) || [];
  const runnable = (validation && validation.runnable) || [];
  const warnings = (validation && validation.warnings) || [];
  const server = (serverIssues || []).map((s) => ({ code: "reference", message: s }));
  const rows = [...blocking.map((r) => ({ ...r, tier: "save" })), ...runnable.map((r) => ({ ...r, tier: "run" })), ...server];

  return (
    <div
      data-testid="gb-readiness-popover"
      style={{
        position: "absolute", top: 44, right: 12, zIndex: 40, width: 400, maxWidth: "92vw",
        background: "var(--bg-elev)", border: "1px solid var(--border-strong)", borderRadius: 12,
        boxShadow: "0 30px 60px -20px rgba(0,0,0,.95)", overflow: "hidden",
      }}
    >
      <div className="col" style={{ gap: 3, padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
        <div style={{ fontSize: "var(--fs-12)", fontWeight: 600 }}>Before this graph can run</div>
        <div className="muted" style={{ fontSize: "var(--fs-11)" }}>
          Drafts always save. These block <span style={{ color: "var(--text-2)" }}>Run</span> only.
        </div>
      </div>
      <div className="col" style={{ maxHeight: 340, overflow: "auto" }}>
        {!rows.length ? (
          <div className="row" style={{ gap: 10, padding: "11px 14px", alignItems: "center", color: "var(--text-3)" }}>
            <span
              style={{
                width: 16, height: 16, borderRadius: "50%", background: "var(--accent-dim)", color: "var(--accent)",
                fontSize: 10, display: "flex", alignItems: "center", justifyContent: "center", flex: "0 0 auto",
              }}
            >
              ✓
            </span>
            <span style={{ fontSize: "var(--fs-11)" }}>One start, every finish reachable, all references resolve</span>
          </div>
        ) : null}
        {rows.map((r, i) => (
          <div
            key={i}
            data-testid="gb-readiness-item"
            className="row"
            style={{ gap: 10, padding: "11px 14px", borderBottom: "1px solid var(--bg-2)", alignItems: "flex-start" }}
          >
            <span
              style={{
                width: 16, height: 16, borderRadius: "50%", flex: "0 0 auto", marginTop: 1, fontSize: 10,
                display: "flex", alignItems: "center", justifyContent: "center",
                background: `color-mix(in oklab, ${r.tier === "save" ? "var(--red)" : "var(--amber)"} 16%, transparent)`,
                border: `1px solid color-mix(in oklab, ${r.tier === "save" ? "var(--red)" : "var(--amber)"} 40%, transparent)`,
                color: r.tier === "save" ? "var(--red)" : "var(--amber)",
              }}
            >
              !
            </span>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: "var(--fs-12)", color: "var(--text)" }}>{r.message}</div>
              {r.tier === "save" ? (
                <div className="muted" style={{ fontSize: "var(--fs-11)", marginTop: 2 }}>This one also blocks saving.</div>
              ) : null}
            </div>
            {r.fix || r.nodeId ? (
              <button
                type="button"
                data-testid="gb-readiness-fix"
                onClick={() => (r.fix && r.fix !== "select_node" ? onFix(r) : onSelectNode(r.nodeId))}
                style={{
                  marginLeft: "auto", flex: "0 0 auto", padding: "4px 9px", borderRadius: 7, cursor: "pointer",
                  background: r.fix && r.fix !== "select_node" ? "var(--accent-dim)" : "var(--bg-2)",
                  border: `1px solid ${r.fix && r.fix !== "select_node" ? "var(--accent-border)" : "var(--border-strong)"}`,
                  color: r.fix && r.fix !== "select_node" ? "var(--accent)" : "var(--text)",
                  fontSize: "var(--fs-11)",
                }}
              >
                {GB_FIX_LABEL[r.fix] || "Show me"}
              </button>
            ) : null}
          </div>
        ))}
        {warnings.map((w, i) => (
          <div key={`w${i}`} className="row" style={{ gap: 10, padding: "10px 14px", alignItems: "flex-start", color: "var(--text-3)" }}>
            <span style={{ flex: "0 0 auto", marginTop: 1, fontSize: 11 }}>·</span>
            <div style={{ fontSize: "var(--fs-11)" }}>{w.message}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

const GB_FIX_LABEL = {
  add_response_format: "Add fields",
  set_max_iterations: "Accept",
  add_catch_all: "Add catch-all",
  connect_end: "Show me",
  add_begin: "Add a start",
  add_end: "Add a finish",
  select_node: "Show me",
};

Object.assign(window, { GB_ReadinessChip, GB_ReadinessPopover, GB_FIX_LABEL });
