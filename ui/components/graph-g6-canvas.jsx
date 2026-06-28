/* global React, G6 */
// SPIKE — AntV G6 v5 run-view canvas. A vendored-UMD alternative to the
// hand-rolled SVG SD_StatusCanvas, to compare animation/reactive UX.
// Same props as SD_StatusCanvas: { graph, statusByNode, selectedNodeId,
// onSelectNode }. Gated behind an in-UI toggle (NOT the default). The
// graph data + dagre layout are built ONCE on mount; per-node status and
// selection are driven by G6 element STATES so live updates animate via
// G6's state transitions without re-running layout.

// Concrete hex (the canvas can't read CSS vars); chosen to echo
// SD_RUN_STATE_TINT so the side-by-side is apples-to-apples.
const _G6_COLORS = {
  neutral: "#3a3f47",
  green: "#34d399",
  amber: "#fbbf24",
  red: "#f87171",
  violet: "#a78bfa",
  text: "#e6e8eb",
  bg: "#0d0f12",
  edge: "#4b525c",
};

// Per-status node state styles. `halo` gives the live "glow"; G6 tweens
// the style change so a node lighting up reads as an animation for free.
function _g6NodeStates() {
  const mk = (c, halo) => ({
    stroke: c, lineWidth: 2, fill: c + "22",
    halo, haloStroke: c, haloStrokeOpacity: 0.35, haloLineWidth: halo ? 10 : 0,
  });
  return {
    pending: mk(_G6_COLORS.neutral, false),
    running: mk(_G6_COLORS.green, true),
    waiting: mk(_G6_COLORS.amber, true),
    ended: mk(_G6_COLORS.green, false),
    failed: mk(_G6_COLORS.red, true),
    selected: { stroke: _G6_COLORS.violet, lineWidth: 3, halo: true, haloStroke: _G6_COLORS.violet, haloStrokeOpacity: 0.4 },
  };
}

function _g6Edges(graph) {
  const edges = [];
  for (const e of (graph?.edges || [])) {
    const s = e.from_node || e.source || e.from;
    const t = e.to_node || e.target || e.to;
    if (s && t) edges.push({ id: `e${edges.length}:${s}->${t}`, source: s, target: t });
  }
  return edges;
}

function SD_G6Canvas({ graph, statusByNode, selectedNodeId, onSelectNode }) {
  const containerRef = React.useRef(null);
  const graphRef = React.useRef(null);
  const readyRef = React.useRef(false);
  const onSelectRef = React.useRef(onSelectNode);
  onSelectRef.current = onSelectNode;

  // Stable identity of the topology so we only rebuild on structural change,
  // not on every status poll.
  const topoKey = React.useMemo(() => {
    const ns = (graph?.nodes || []).map((n) => `${n.id}:${n.kind}`).join(",");
    const es = _g6Edges(graph).map((e) => e.id).join(",");
    return ns + "|" + es;
  }, [graph]);

  // Build the G6 graph once per topology.
  React.useEffect(() => {
    if (!containerRef.current || !window.G6 || !graph) return undefined;
    const G6 = window.G6;
    const nodes = (graph.nodes || []).map((n) => ({
      id: n.id,
      data: { kind: n.kind, label: n.id },
    }));
    const isTiny = (kind) => kind === "begin" || kind === "end";

    let g;
    try {
      g = new G6.Graph({
        container: containerRef.current,
        autoResize: true,
        background: _G6_COLORS.bg,
        data: { nodes, edges: _g6Edges(graph) },
        node: {
          type: "rect",
          style: {
            size: (d) => (isTiny(d.data.kind) ? [24, 24] : [148, 42]),
            radius: (d) => (isTiny(d.data.kind) ? 12 : 8),
            fill: _G6_COLORS.neutral + "22",
            stroke: _G6_COLORS.neutral,
            lineWidth: 2,
            labelText: (d) => (isTiny(d.data.kind) ? "" : d.data.label),
            labelFill: _G6_COLORS.text,
            labelFontSize: 12,
            labelFontFamily: "IBM Plex Mono, monospace",
          },
          state: _g6NodeStates(),
        },
        edge: {
          type: "polyline",
          style: {
            stroke: _G6_COLORS.edge,
            lineWidth: 1.5,
            endArrow: true,
            endArrowSize: 8,
            radius: 8,
          },
          state: {
            active: { stroke: _G6_COLORS.green, lineWidth: 2, lineDash: [6, 6], stroke_opacity: 1 },
          },
        },
        layout: { type: "dagre", rankdir: "LR", nodesep: 16, ranksep: 64 },
        behaviors: ["zoom-canvas", "drag-canvas"],
        // Fit + center the whole graph in the pane on render (the topology
        // is set once, so this runs once and won't fight status updates).
        autoFit: "view",
        padding: 24,
      });
      graphRef.current = g;
      readyRef.current = false;

      g.on("node:click", (e) => {
        const id = (e && (e.target?.id ?? e.itemId ?? e.target?.config?.id)) || null;
        if (id && onSelectRef.current) onSelectRef.current(id);
      });
      g.on("canvas:click", () => { if (onSelectRef.current) onSelectRef.current(null); });

      const done = g.render();
      Promise.resolve(done).then(() => { readyRef.current = true; }).catch(() => {});
    } catch (err) {
      // Spike: surface init failures in the console for iteration.
      console.error("[SD_G6Canvas] init failed:", err);  // eslint-disable-line no-console
    }

    return () => {
      readyRef.current = false;
      try { graphRef.current && graphRef.current.destroy(); } catch (_e) { /* no-op */ }
      graphRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topoKey]);

  // Apply per-node status + selection as element states (animated, no relayout).
  React.useEffect(() => {
    const g = graphRef.current;
    if (!g) return;
    const apply = () => {
      try {
        const nodeStates = {};
        for (const n of (graph?.nodes || [])) {
          const st = statusByNode[n.id] || "pending";
          nodeStates[n.id] = n.id === selectedNodeId ? [st, "selected"] : [st];
        }
        // Light up edges leaving an active (running/ended) node so flow reads.
        const edgeStates = {};
        for (const e of _g6Edges(graph)) {
          const srcStatus = statusByNode[e.source] || "pending";
          edgeStates[e.id] = (srcStatus === "running" || srcStatus === "ended") ? ["active"] : [];
        }
        g.setElementState({ ...nodeStates, ...edgeStates });
      } catch (_e) { /* spike */ }
    };
    if (readyRef.current) apply();
    else { const t = setTimeout(apply, 120); return () => clearTimeout(t); }
    return undefined;
  }, [statusByNode, selectedNodeId, graph]);

  return (
    <div style={{ minWidth: 0, overflow: "hidden" }}>
      <div
        ref={containerRef}
        data-testid="g6-run-canvas"
        style={{ width: "100%", height: 500, minHeight: 500, background: _G6_COLORS.bg }}
      />
    </div>
  );
}

window.SD_G6Canvas = SD_G6Canvas;
