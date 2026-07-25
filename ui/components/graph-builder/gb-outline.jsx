/* global React, GB_supersteps */
// GB_Outline - the left rail. Lists steps in execution order, shows what each
// one is waiting on, and doubles as the "fix it" jump list. WIRING.md §4.

const GB_KIND_META = {
  begin: { tint: "var(--green)", label: "Start" },
  end: { tint: "var(--text-3)", label: "Finish" },
  agent: { tint: "var(--blue)", label: "Agent" },
  tool_call: { tint: "var(--violet)", label: "Tool" },
  graph: { tint: "var(--accent)", label: "Sub-graph" },
  fan_out: { tint: "var(--green)", label: "Split" },
  fan_in: { tint: "var(--green)", label: "Merge" },
};

function GB_KindDot({ kind, size }) {
  const meta = GB_KIND_META[kind] || GB_KIND_META.agent;
  const s = size || 22;
  return (
    <span
      style={{
        width: s, height: s, borderRadius: 6, flex: "0 0 auto",
        background: `color-mix(in oklab, ${meta.tint} 14%, transparent)`,
        border: `1px solid color-mix(in oklab, ${meta.tint} 32%, transparent)`,
        display: "flex", alignItems: "center", justifyContent: "center",
      }}
      title={meta.label}
    >
      <span style={{
        width: 7, height: 7, background: meta.tint,
        borderRadius: kind === "end" ? 0 : "50%",
      }}
      />
    </span>
  );
}

function GB_Outline(props) {
  const { draft, selectedId, onSelect, problemsByNode, runStates } = props;
  const layers = React.useMemo(() => (GB_supersteps ? GB_supersteps(draft) : []), [draft]);
  const byId = {};
  for (const n of draft.nodes || []) byId[n.id] = n;

  // Execution order, then anything unplaced (keeps every node visible).
  const ordered = [];
  for (const layer of layers) for (const id of layer) if (byId[id]) ordered.push(byId[id]);
  for (const n of draft.nodes || []) if (!ordered.includes(n)) ordered.push(n);

  const branchRowsFor = (nodeId) => {
    const rows = [];
    (draft.edges || []).forEach((e) => {
      if (e.from_node !== nodeId || !e.router || e.router.kind !== "json_path") return;
      for (const b of e.router.branches || []) {
        const cond = (b.conditions || [])[0];
        rows.push({
          icon: "✓", tint: "var(--green)",
          text: cond ? `${cond.path} -> ${(byId[b.to_node] || {}).description || b.to_node}`
            : `always -> ${(byId[b.to_node] || {}).description || b.to_node}`,
        });
      }
      if (e.router.default_to) {
        rows.push({
          icon: "↺", tint: "var(--amber)",
          text: `otherwise -> ${(byId[e.router.default_to] || {}).description || e.router.default_to}`,
        });
      }
    });
    if (draft.on_max_iterations && nodeId && rows.length) {
      const land = byId[draft.on_max_iterations];
      rows.push({
        icon: "⤓", tint: "var(--violet)",
        text: `after ${draft.max_iterations || "?"} loops -> ${(land || {}).description || draft.on_max_iterations}`,
      });
    }
    return rows;
  };

  return (
    <div className="col" data-testid="gb-outline" style={{ gap: 3, padding: "0 10px 10px", overflow: "auto" }}>
      {ordered.map((n) => {
        const problems = (problemsByNode && problemsByNode[n.id]) || [];
        const state = runStates && runStates[n.id];
        const selected = n.id === selectedId;
        const spec = (n.specs || [])[0];
        const copies = spec && spec.kind === "broadcast" ? spec.count
          : spec && spec.kind === "tee" ? (spec.target_node_ids || []).length : null;
        return (
          <div key={n.id}>
            <div
              data-testid="gb-outline-row"
              data-node-id={n.id}
              onClick={() => onSelect(n.id)}
              className="row"
              style={{
                gap: 9, alignItems: "center", padding: "8px 10px", borderRadius: 8, cursor: "pointer",
                background: selected ? "color-mix(in oklab, var(--violet) 12%, transparent)" : "transparent",
                border: selected ? "1px solid color-mix(in oklab, var(--violet) 30%, transparent)" : "1px solid transparent",
              }}
            >
              <GB_KindDot kind={n.kind} />
              <span style={{
                fontSize: "var(--fs-12)", minWidth: 0, overflow: "hidden",
                textOverflow: "ellipsis", whiteSpace: "nowrap",
                color: n.kind === "begin" || n.kind === "end" ? "var(--text-2)" : "var(--text)",
              }}
              >
                {n.description || n.id}
              </span>
              {copies ? (
                <span className="mono" style={{ marginLeft: "auto", fontSize: "var(--fs-11)", color: "var(--accent)" }}>×{copies}</span>
              ) : null}
              {state ? (
                <span
                  title={state}
                  style={{
                    marginLeft: copies ? 6 : "auto", width: 8, height: 8, borderRadius: "50%", flex: "0 0 auto",
                    background: state === "running" ? "var(--accent)"
                      : state === "completed" ? "var(--green)"
                        : state === "failed" ? "var(--red)" : "var(--text-4)",
                  }}
                />
              ) : null}
              {problems.length ? (
                <span
                  title={problems[0].message}
                  style={{
                    marginLeft: copies || state ? 6 : "auto", width: 14, height: 14, borderRadius: "50%",
                    background: "color-mix(in oklab, var(--red) 18%, transparent)", color: "var(--red)",
                    fontSize: 9, display: "flex", alignItems: "center", justifyContent: "center", flex: "0 0 auto",
                  }}
                >
                  !
                </span>
              ) : null}
            </div>
            {branchRowsFor(n.id).map((r, i) => (
              <div
                key={i}
                className="row"
                style={{
                  gap: 7, alignItems: "center", padding: "5px 8px", marginLeft: 20,
                  paddingLeft: 12, borderLeft: "1px dashed var(--border-strong)",
                  fontSize: "var(--fs-11)", color: "var(--text-2)",
                }}
              >
                <span style={{ color: r.tint }}>{r.icon}</span>
                <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.text}</span>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

Object.assign(window, { GB_Outline, GB_KindDot, GB_KIND_META });
