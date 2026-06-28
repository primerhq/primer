/* global React, G6 */
// SPIKE — AntV G6 v5 run-view canvas. A vendored-UMD alternative to the
// hand-rolled SVG SD_StatusCanvas, to compare animation/reactive UX.
// Props: { graph, statusByNode, metaByNode, selectedNodeId, onSelectNode }.
// Gated behind an in-UI toggle (NOT the default). Topology + dagre layout
// are built ONCE on mount; per-node status/selection/pulse are driven by
// G6 element STATES (animated, no relayout), and the per-node token/
// duration metric updates via updateNodeData as a node finishes.

const _G6_COLORS = {
  neutral: "#3a3f47", green: "#34d399", amber: "#fbbf24", red: "#f87171",
  violet: "#a78bfa", text: "#e6e8eb", sub: "#9aa4af", bg: "#0d0f12", edge: "#4b525c",
};

// Node-kind icons — reuse the console's icon language (24x24 stroke paths)
// as SVG data-URIs so the canvas-rendered G6 nodes show type via iconSrc.
// begin/end stay as circle SHAPES (no icon); the work nodes get an icon.
const _G6_KIND_SVG = {
  agent: '<circle cx="12" cy="9" r="3.5"/><path d="M5 20c0-3.5 3-6 7-6s7 2.5 7 6"/>',
  tool_call: '<path d="M14 6l4-4 4 4-4 4M14 6L8 12M5 19l-3 3v-3h3l9-9 3 3-9 9z"/>',
  fan_out: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M6 8.5v3a3 3 0 003 3h6a3 3 0 003-3v-3M12 14.5v.5"/>',
  fan_in: '<g transform="rotate(180 12 12)"><circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M6 8.5v3a3 3 0 003 3h6a3 3 0 003-3v-3M12 14.5v.5"/></g>',
  graph: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7.5 7.5L11 16M16.5 7.5L13 16"/>',
};
function _g6IconUri(kind, stroke) {
  const inner = _G6_KIND_SVG[kind];
  if (!inner) return undefined;
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="'
    + (stroke || _G6_COLORS.text) + '" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
    + inner + '</svg>';
  return "data:image/svg+xml," + encodeURIComponent(svg);
}
window._g6IconUri = _g6IconUri;

function _g6NodeStates() {
  const mk = (c, halo) => ({
    stroke: c, lineWidth: 2, fill: c + "22",
    halo, haloStroke: c, haloStrokeOpacity: 0.32, haloLineWidth: halo ? 10 : 0,
  });
  return {
    pending: mk(_G6_COLORS.neutral, false),
    running: mk(_G6_COLORS.green, true),
    waiting: mk(_G6_COLORS.amber, true),
    ended: mk(_G6_COLORS.green, false),
    failed: mk(_G6_COLORS.red, true),
    // Pulse is toggled on/off on running nodes; the halo tween reads as a
    // breathing pulse without a custom keyframe.
    pulse: { halo: true, haloStroke: _G6_COLORS.green, haloLineWidth: 20, haloStrokeOpacity: 0.15 },
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

// "1.2k tok · 0.4s" from a node_states meta blob.
function _g6Metric(meta) {
  if (!meta) return "";
  const tot = (meta.tin || 0) + (meta.tout || 0);
  const parts = [];
  if (tot) parts.push((tot >= 1000 ? (tot / 1000).toFixed(1) + "k" : String(tot)) + " tok");
  if (meta.dur != null) parts.push((meta.dur / 1000).toFixed(1) + "s");
  return parts.join("  ·  ");
}

function _g6Label(node, metaByNode) {
  if (node.kind === "begin" || node.kind === "end") return "";
  const m = _g6Metric(metaByNode && metaByNode[node.id]);
  return m ? `${node.id}\n${m}` : node.id;
}

function SD_G6Canvas({ graph, statusByNode, metaByNode, selectedNodeId, onSelectNode }) {
  const containerRef = React.useRef(null);
  const graphRef = React.useRef(null);
  const readyRef = React.useRef(false);
  const onSelectRef = React.useRef(onSelectNode);
  onSelectRef.current = onSelectNode;

  const topoKey = React.useMemo(() => {
    const ns = (graph?.nodes || []).map((n) => `${n.id}:${n.kind}`).join(",");
    const es = _g6Edges(graph).map((e) => e.id).join(",");
    return ns + "|" + es;
  }, [graph]);

  // Build the G6 graph once per topology.
  React.useEffect(() => {
    if (!containerRef.current || !window.G6 || !graph) return undefined;
    const G6 = window.G6;
    const isTiny = (kind) => kind === "begin" || kind === "end";
    const nodes = (graph.nodes || []).map((n) => ({
      id: n.id,
      data: { kind: n.kind, label: _g6Label(n, metaByNode) },
    }));

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
            size: (d) => (isTiny(d.data.kind) ? [24, 24] : [150, 46]),
            radius: (d) => (isTiny(d.data.kind) ? 12 : 8),
            fill: _G6_COLORS.neutral + "22",
            stroke: _G6_COLORS.neutral,
            lineWidth: 2,
            labelText: (d) => d.data.label,
            labelPlacement: "center",
            labelDx: 9,
            labelFill: _G6_COLORS.text,
            labelFontSize: 12,
            labelFontFamily: "IBM Plex Mono, monospace",
            labelLineHeight: 15,
            iconSrc: (d) => _g6IconUri(d.data.kind),
            iconWidth: 15,
            iconHeight: 15,
            iconX: -57,
          },
          state: _g6NodeStates(),
        },
        edge: {
          type: "polyline",
          style: { stroke: _G6_COLORS.edge, lineWidth: 1.5, endArrow: true, endArrowSize: 8, radius: 8 },
          state: { active: { stroke: _G6_COLORS.green, lineWidth: 2, lineDash: [6, 6], endArrow: true } },
        },
        layout: { type: "dagre", rankdir: "LR", nodesep: 18, ranksep: 66 },
        behaviors: ["zoom-canvas", "drag-canvas"],
        autoFit: "view",
        padding: 28,
      });
      graphRef.current = g;
      readyRef.current = false;

      g.on("node:click", (e) => {
        const id = (e && (e.target?.id ?? e.itemId ?? e.target?.config?.id)) || null;
        if (id && onSelectRef.current) onSelectRef.current(id);
      });
      g.on("canvas:click", () => { if (onSelectRef.current) onSelectRef.current(null); });

      Promise.resolve(g.render()).then(() => { readyRef.current = true; }).catch(() => {});
    } catch (err) {
      console.error("[SD_G6Canvas] init failed:", err);  // eslint-disable-line no-console
    }

    return () => {
      readyRef.current = false;
      try { graphRef.current && graphRef.current.destroy(); } catch (_e) { /* no-op */ }
      graphRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topoKey]);

  // Status + selection + edge-flow as element states (animated, no relayout).
  React.useEffect(() => {
    const g = graphRef.current;
    if (!g) return;
    const apply = () => {
      try {
        const s = {};
        for (const n of (graph?.nodes || [])) {
          const st = statusByNode[n.id] || "pending";
          s[n.id] = n.id === selectedNodeId ? [st, "selected"] : [st];
        }
        for (const e of _g6Edges(graph)) {
          const srcStatus = statusByNode[e.source] || "pending";
          s[e.id] = (srcStatus === "running" || srcStatus === "ended") ? ["active"] : [];
        }
        g.setElementState(s);
      } catch (_e) { /* spike */ }
    };
    if (readyRef.current) apply();
    else { const t = setTimeout(apply, 140); return () => clearTimeout(t); }
    return undefined;
  }, [statusByNode, selectedNodeId, graph]);

  // Live token/duration metric — update node labels as nodes finish.
  React.useEffect(() => {
    const g = graphRef.current;
    if (!g || !readyRef.current) return;
    try {
      g.updateNodeData((graph?.nodes || []).map((n) => ({
        id: n.id, data: { kind: n.kind, label: _g6Label(n, metaByNode) },
      })));
      g.draw();
    } catch (_e) { /* spike */ }
  }, [metaByNode, graph]);

  // Running pulse — breathe the halo on running nodes.
  React.useEffect(() => {
    const running = (graph?.nodes || [])
      .filter((n) => (statusByNode[n.id] || "pending") === "running")
      .map((n) => n.id);
    if (!running.length) return undefined;
    let on = false;
    const id = setInterval(() => {
      const g = graphRef.current;
      if (!g || !readyRef.current) return;
      on = !on;
      try {
        const s = {};
        for (const nid of running) {
          const base = nid === selectedNodeId ? ["running", "selected"] : ["running"];
          s[nid] = on ? [...base, "pulse"] : base;
        }
        g.setElementState(s);
      } catch (_e) { /* spike */ }
    }, 680);
    return () => clearInterval(id);
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
