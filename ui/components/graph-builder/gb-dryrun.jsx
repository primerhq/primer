/* global React */
// GB_DryRunDrawer - resolve templates before spending a token. WIRING.md §2.1A.
// Falls back to a local static check when POST /graphs/{id}/dry_run is absent.

function GB_DryRunDrawer(props) {
  const { result, loading, sampleInput, onSampleInput, onRecheck, onClose, onFix, onSelectNode, onRun, canRun } = props;
  const nodes = (result && result.nodes) || [];
  const blockers = (result && result.blockers) || [];
  const shape = (result && result.shape) || {};
  const byNode = {};
  for (const b of blockers) if (b.node_id) byNode[b.node_id] = b;

  return (
    <div
      data-testid="gb-dryrun"
      className="col"
      style={{ height: 236, flex: "0 0 auto", borderTop: "1px solid var(--border)", background: "var(--bg-1)" }}
    >
      <div
        className="row"
        style={{ height: 42, flex: "0 0 auto", gap: 10, alignItems: "center", padding: "0 14px", borderBottom: "1px solid var(--border)" }}
      >
        <span style={{ fontSize: "var(--fs-12)", fontWeight: 600 }}>Dry run</span>
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
          {result && result.local
            ? "static check only - templates resolved here, nothing executed"
            : "templates resolved, nothing executed, no tokens spent"}
        </span>
        <div className="row" style={{ marginLeft: "auto", gap: 8, alignItems: "center" }}>
          <span className="muted" style={{ fontSize: "var(--fs-11)" }}>Sample input</span>
          <input
            value={sampleInput}
            onChange={(e) => onSampleInput(e.target.value)}
            placeholder='{"topic": "…"}'
            className="mono"
            style={{
              padding: "4px 9px", borderRadius: 7, background: "var(--bg-2)", border: "1px solid var(--border)",
              color: "var(--text-2)", fontSize: "var(--fs-11)", width: 220, outline: "none",
            }}
          />
          <button
            type="button"
            data-testid="gb-dryrun-run"
            onClick={onRecheck}
            disabled={loading}
            style={{
              padding: "5px 11px", borderRadius: 7, cursor: "pointer", background: "var(--bg-2)",
              border: "1px solid var(--border-strong)", color: "var(--text)", fontSize: "var(--fs-11)",
            }}
          >
            {loading ? "Checking…" : "Re-check"}
          </button>
          <span onClick={onClose} style={{ cursor: "pointer", color: "var(--text-3)", padding: "0 4px" }}>×</span>
        </div>
      </div>

      <div className="row" style={{ flex: 1, minHeight: 0 }}>
        <div className="col" style={{ flex: 1, minWidth: 0, overflow: "auto", padding: "12px 14px", gap: 7 }}>
          {!nodes.length && !loading ? (
            <span className="muted" style={{ fontSize: "var(--fs-12)" }}>Nothing to resolve yet - add a step that takes input.</span>
          ) : null}
          {nodes.map((n) => {
            const blocker = byNode[n.node_id];
            const bad = blocker || (n.template_errors || []).length;
            return (
              <div
                key={n.node_id}
                data-testid="gb-dryrun-row"
                data-node-id={n.node_id}
                className="row"
                style={{
                  gap: 10, alignItems: "center", padding: "8px 10px", borderRadius: 8,
                  background: bad ? "color-mix(in oklab, var(--red) 7%, transparent)" : "var(--bg-2)",
                  border: `1px solid ${bad ? "color-mix(in oklab, var(--red) 35%, transparent)" : "var(--border)"}`,
                }}
              >
                <span
                  style={{
                    width: 15, height: 15, borderRadius: "50%", flex: "0 0 auto", fontSize: 9,
                    display: "flex", alignItems: "center", justifyContent: "center",
                    background: bad ? "color-mix(in oklab, var(--red) 18%, transparent)" : "var(--accent-dim)",
                    color: bad ? "var(--red)" : "var(--accent)",
                  }}
                >
                  {bad ? "!" : "✓"}
                </span>
                <span style={{ fontSize: "var(--fs-12)", flex: "0 0 150px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {n.label || n.node_id}
                </span>
                <span
                  className={bad ? "" : "mono"}
                  style={{
                    fontSize: "var(--fs-11)", color: bad ? "var(--red)" : "var(--text-3)",
                    minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                  }}
                >
                  {blocker ? blocker.message : ((n.template_errors || [])[0] || n.rendered_input || "")}
                </span>
                {blocker && blocker.fix ? (
                  <button
                    type="button"
                    onClick={() => onFix(blocker)}
                    style={{
                      marginLeft: "auto", flex: "0 0 auto", padding: "3px 9px", borderRadius: 6, cursor: "pointer",
                      background: "var(--bg-2)", border: "1px solid var(--border-strong)", color: "var(--text)", fontSize: "var(--fs-11)",
                    }}
                  >
                    Fix
                  </button>
                ) : (
                  <span
                    onClick={() => onSelectNode(n.node_id)}
                    style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--text-4)", flex: "0 0 auto", cursor: "pointer" }}
                  >
                    step {n.superstep ?? "?"}
                  </span>
                )}
              </div>
            );
          })}
        </div>

        <div
          className="col"
          style={{ width: 300, flex: "0 0 auto", borderLeft: "1px solid var(--border)", padding: "12px 14px", gap: 9 }}
        >
          <div style={{ fontSize: 10.5, fontWeight: 600, letterSpacing: ".07em", color: "var(--text-3)", textTransform: "uppercase" }}>
            Shape check
          </div>
          <div style={{ fontSize: "var(--fs-11)", color: "var(--text-2)", lineHeight: 1.6 }}>
            {blockers.length
              ? `${blockers.length} thing${blockers.length === 1 ? "" : "s"} still block the run.`
              : "Every reference resolves."}
          </div>
          <div className="col" style={{ gap: 5, fontSize: "var(--fs-11)", color: "var(--text-3)" }}>
            <div>Longest path · {shape.longest_path ?? "?"} steps</div>
            <div>Runs in parallel · {shape.parallel_groups ? `${shape.parallel_groups} group(s)` : "none in this graph"}</div>
            <div>Loops · {shape.loops || 0}</div>
            <div>Pauses for a human · {shape.human_pauses || 0}</div>
          </div>
          <button
            type="button"
            onClick={onRun}
            disabled={!canRun}
            style={{
              marginTop: "auto", padding: 8, borderRadius: 8, fontSize: "var(--fs-12)",
              cursor: canRun ? "pointer" : "not-allowed",
              background: canRun ? "var(--accent)" : "var(--bg-2)",
              border: `1px solid ${canRun ? "var(--accent)" : "var(--border-strong)"}`,
              color: canRun ? "var(--accent-fg)" : "var(--text-4)",
              fontWeight: canRun ? 600 : 400,
            }}
          >
            {canRun ? "Run this graph" : `Run · fix ${blockers.length || 1} blocker${(blockers.length || 1) === 1 ? "" : "s"} first`}
          </button>
        </div>
      </div>
    </div>
  );
}

window.GB_DryRunDrawer = GB_DryRunDrawer;
