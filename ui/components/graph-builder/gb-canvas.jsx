/* global React, GR_Canvas, GB_supersteps */
// GB_Canvas - GR_Canvas plus the two overlays the redesign needs:
// a fan-out bracket around the copies, and superstep bands. Both are measured
// from real node positions rather than hard-coded widths (WIRING.md §6.2/§6.4).

function GB_Canvas(props) {
  const { draft, showBands, onIllegalEdge } = props;
  const { useState, useEffect, useRef } = React;
  const hostRef = useRef(null);
  const g6Ref = useRef(null);
  const [boxes, setBoxes] = useState(null); // {nodeId: {x,y,w,h}} in host pixels

  // Measure the rendered node cards so overlays track pan/zoom. G6 draws to
  // canvas, so positions come from the draft + the container's transform; a
  // ResizeObserver + rAF loop keeps them fresh without re-initialising G6.
  useEffect(() => {
    let raf = 0;
    let alive = true;
    const measure = () => {
      if (!alive) return;
      const g = g6Ref.current;
      if (g && typeof g.getElementPosition === "function") {
        const next = {};
        for (const n of (draft.nodes || [])) {
          try {
            const p = g.getElementPosition(n.id);
            const sz = (window.GR_NODE_SIZE || {})[n.kind] || { w: 196, h: 64 };
            let zoom = 1;
            try { zoom = typeof g.getZoom === "function" ? g.getZoom() : 1; } catch (_e) { zoom = 1; }
            if (p) next[n.id] = { x: p[0], y: p[1], w: sz.w * zoom, h: sz.h * zoom, zoom };
          } catch (_e) { /* node not rendered yet */ }
        }
        setBoxes(Object.keys(next).length ? next : null);
      }
      raf = window.requestAnimationFrame(measure);
    };
    raf = window.requestAnimationFrame(measure);
    return () => { alive = false; window.cancelAnimationFrame(raf); };
  }, [draft]);

  const layers = React.useMemo(() => (GB_supersteps ? GB_supersteps(draft) : []), [draft]);

  // Fan-out brackets: one per fan_out node, spanning its spec targets.
  const brackets = [];
  if (boxes) {
    for (const n of (draft.nodes || [])) {
      if (n.kind !== "fan_out") continue;
      const targets = [];
      for (const s of n.specs || []) {
        if (s.target_node_id) targets.push({ id: s.target_node_id, spec: s });
        for (const t of (s.target_node_ids || [])) targets.push({ id: t, spec: s });
      }
      const present = targets.map((t) => boxes[t.id]).filter(Boolean);
      if (!present.length) continue;
      const minX = Math.min(...present.map((b) => b.x - b.w / 2));
      const maxX = Math.max(...present.map((b) => b.x + b.w / 2));
      const minY = Math.min(...present.map((b) => b.y - b.h / 2));
      const maxY = Math.max(...present.map((b) => b.y + b.h / 2));
      const spec = (n.specs || [])[0] || {};
      const count = spec.kind === "broadcast" ? spec.count
        : spec.kind === "tee" ? (spec.target_node_ids || []).length
          : null;
      brackets.push({
        nodeId: n.id,
        pad: 18,
        x: minX - 18, y: minY - 18, w: (maxX - minX) + 36, h: (maxY - minY) + 36,
        label: count ? `${count} copies run at the same time` : "one per item, all at the same time",
        sub: spec.kind === "map" ? "one per item in the list" : "",
      });
    }
  }

  return (
    <div ref={hostRef} style={{ position: "relative", width: "100%", height: "100%" }} data-testid="gb-canvas">
      <GR_Canvas
        {...props}
        onIllegalEdge={onIllegalEdge}
        onGraphReady={(g) => { g6Ref.current = g; }}
      />

      {showBands && layers.length > 1 && boxes ? (
        <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
          {layers.map((layer, i) => {
            const bs = layer.map((id) => boxes[id]).filter(Boolean);
            if (!bs.length) return null;
            const left = Math.min(...bs.map((b) => b.x - b.w / 2)) - 16;
            return (
              <div
                key={i}
                data-testid="gb-band"
                style={{
                  position: "absolute", left, top: 0, bottom: 0,
                  borderLeft: i === 0 ? "none" : "1px solid var(--border)",
                  opacity: 0.45,
                }}
              >
                <span
                  className="mono"
                  style={{
                    position: "absolute", top: 8, left: 10, fontSize: "var(--fs-11)",
                    color: "var(--text-4)", letterSpacing: ".06em", whiteSpace: "nowrap",
                  }}
                >
                  STEP {i + 1}
                </span>
              </div>
            );
          })}
        </div>
      ) : null}

      {brackets.map((b) => (
        <div key={b.nodeId} style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
          <div
            data-testid="gb-fanout-bracket"
            data-node-id={b.nodeId}
            style={{
              position: "absolute", left: b.x, top: b.y, width: b.w, height: b.h,
              border: "1.5px dashed var(--accent-border)", borderRadius: 16,
              background: "color-mix(in oklab, var(--accent) 4%, transparent)",
            }}
          />
          <div
            style={{
              position: "absolute", left: b.x, top: Math.max(0, b.y - 26),
              display: "flex", alignItems: "center", gap: 8, whiteSpace: "nowrap",
            }}
          >
            <span
              style={{
                padding: "4px 10px", borderRadius: 999, fontSize: "var(--fs-11)",
                background: "var(--accent-dim)", border: "1px solid var(--accent-border)",
                color: "var(--accent)", fontWeight: 500,
              }}
            >
              {b.label}
            </span>
            {b.sub ? <span className="mono" style={{ fontSize: "var(--fs-11)", color: "var(--text-3)" }}>{b.sub}</span> : null}
          </div>
        </div>
      ))}
    </div>
  );
}

window.GB_Canvas = GB_Canvas;
