/* global React, G6 */
// Unified AntV G6 (v5) graph canvas — the single renderer behind the graph
// editor (interactive) and the session run-view (read-only + status). One
// component, one place for the G6 logic; a `layout` prop forks between the
// editor's user-positioned nodes ("preset") and the run-view's auto layout
// ("dagre"). Interaction callbacks are optional: when absent (run-view) the
// canvas is read-only. Rendering parity lives here; interaction wiring is
// layered on in the same file.
//
// Props: { draft, layout, selectedNodeId, selectedEdgeId, statusTint,
//          metaByNode, onNodeClick, onEdgeClick, onNodeDoubleClick,
//          onBackgroundClick, onMoveNode, onConnect, addEdgeMode }.
// `draft` is { nodes:[{id,kind,x?,y?,...}], edges:[...] } (the editor draft
// or the run-view graph — same shape). `layout`: "preset" | "dagre".

const _G6_COLORS = {
  neutral: "#3a3f47", green: "#34d399", amber: "#fbbf24", red: "#f87171",
  violet: "#a78bfa", text: "#e6e8eb", sub: "#9aa4af", bg: "#0d0f12", edge: "#4b525c",
};

// Node sizes by kind (also exported; consumers may read GR_NODE_SIZE).
const GR_NODE_SIZE = {
  begin: { w: 24, h: 24 }, end: { w: 24, h: 24 },
  agent: { w: 152, h: 46 }, graph: { w: 152, h: 46 }, fan_in: { w: 152, h: 46 },
  tool_call: { w: 168, h: 46 }, fan_out: { w: 168, h: 46 },
};
function _g6Size(kind) { return GR_NODE_SIZE[kind] || GR_NODE_SIZE.agent; }
function _g6Tiny(kind) { return kind === "begin" || kind === "end"; }

// Node-kind icons — reuse the console icon language (24x24 stroke paths) as
// SVG data-URIs so the canvas-rendered G6 nodes show type via iconSrc.
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

// Per-status node state styles (the live "glow"); G6 tweens state changes.
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
    pulse: { halo: true, haloStroke: _G6_COLORS.green, haloLineWidth: 20, haloStrokeOpacity: 0.15 },
    selected: { stroke: _G6_COLORS.violet, lineWidth: 3, halo: true, haloStroke: _G6_COLORS.violet, haloStrokeOpacity: 0.4 },
  };
}

function _g6Metric(meta) {
  if (!meta) return "";
  const tot = (meta.tin || 0) + (meta.tout || 0);
  const parts = [];
  if (tot) parts.push((tot >= 1000 ? (tot / 1000).toFixed(1) + "k" : String(tot)) + " tok");
  if (meta.dur != null) parts.push((meta.dur / 1000).toFixed(1) + "s");
  return parts.join("  ·  ");
}
function _g6Label(node, metaByNode) {
  if (_g6Tiny(node.kind)) return "";
  const m = _g6Metric(metaByNode && metaByNode[node.id]);
  return m ? `${node.id}\n${m}` : node.id;
}

// Edges by kind: static (one), conditional (one per branch target +
// default_to), implicit fan-out (one per spec target). Each carries
// data.etype + data.idx (index into draft.edges, or -1 for implicit) so the
// canvas can style + map edge clicks back to the draft.
function _g6Edges(draft) {
  const out = [];
  const push = (s, t, etype, idx, label) => {
    if (s && t) out.push({ id: `e${out.length}:${s}->${t}`, source: s, target: t, data: { etype, idx, label } });
  };
  (draft?.edges || []).forEach((e, idx) => {
    if (e.kind === "conditional" && e.router) {
      const seen = new Set();
      for (const b of (e.router.branches || [])) {
        if (b.to_node && !seen.has(b.to_node)) { seen.add(b.to_node); push(e.from_node, b.to_node, "conditional", idx, ""); }
      }
      const dft = e.router.default_to;
      if (dft && !seen.has(dft)) push(e.from_node, dft, "conditional", idx, "else");
    } else {
      push(e.from_node || e.source, e.to_node || e.target, "static", idx, "");
    }
  });
  // Implicit fan-out edges (FanOut.specs[].target_node_id), not in draft.edges.
  for (const n of (draft?.nodes || [])) {
    if (n.kind === "fan_out") {
      for (const sp of (n.specs || [])) {
        if (sp && sp.target_node_id) push(n.id, sp.target_node_id, "implicit", -1, "");
      }
    }
  }
  return out;
}

function _g6EdgeStateStyle() {
  return {
    selected: { stroke: _G6_COLORS.violet, lineWidth: 2.5 },
    active: { stroke: _G6_COLORS.green, lineWidth: 2, lineDash: [6, 6], endArrow: true },
  };
}

function GR_Canvas(props) {
  const { draft, layout, selectedNodeId, selectedEdgeId, statusTint, metaByNode, addEdgeMode } = props;
  const containerRef = React.useRef(null);
  const graphRef = React.useRef(null);
  const readyRef = React.useRef(false);
  // Latest callbacks in a ref so changing them doesn't re-init G6.
  const cb = React.useRef(props);
  cb.current = props;

  const topoKey = React.useMemo(() => {
    const ns = (draft?.nodes || []).map((n) => `${n.id}:${n.kind}`).join(",");
    const es = _g6Edges(draft).map((e) => e.id).join(",");
    return (layout || "dagre") + "|" + ns + "|" + es;
  }, [draft, layout]);

  React.useEffect(() => {
    if (!containerRef.current || !window.G6 || !draft) return undefined;
    const G6 = window.G6;
    const preset = layout === "preset";
    // Mutating behaviors (drag/connect) only when the editor wires them;
    // the run-view passes no onMoveNode/onConnect and stays read-only.
    const interactive = !!(cb.current.onMoveNode || cb.current.onConnect);
    const behaviors = ["zoom-canvas", "drag-canvas"];
    if (interactive) {
      behaviors.push({ type: "drag-element", key: "drag-node" });
      behaviors.push({ type: "click-select", key: "sel", multiple: false });
      behaviors.push({ type: "create-edge", key: "make-edge", trigger: "drag", style: { stroke: _G6_COLORS.violet, lineWidth: 1.5, lineDash: [4, 4], endArrow: true } });
    }
    const nodes = (draft.nodes || []).map((n) => {
      const sz = _g6Size(n.kind);
      const node = { id: n.id, data: { kind: n.kind, label: _g6Label(n, metaByNode) } };
      if (preset) node.style = { x: (n.x || 0) + sz.w / 2, y: (n.y || 0) + sz.h / 2 };
      return node;
    });

    let g;
    try {
      g = new G6.Graph({
        container: containerRef.current,
        autoResize: true,
        background: _G6_COLORS.bg,
        data: { nodes, edges: _g6Edges(draft) },
        node: {
          type: "rect",
          style: {
            size: (d) => { const s = _g6Size(d.data.kind); return [s.w, s.h]; },
            radius: (d) => (_g6Tiny(d.data.kind) ? 12 : 8),
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
          style: {
            stroke: (d) => (d.data.etype === "implicit" ? "#3b424b" : _G6_COLORS.edge),
            lineWidth: 1.5,
            lineDash: (d) => (d.data.etype === "static" ? undefined : [5, 5]),
            endArrow: true,
            endArrowSize: 8,
            radius: 8,
            labelText: (d) => d.data.label || "",
            labelFill: _G6_COLORS.sub,
            labelFontSize: 10,
            labelBackground: true,
            labelBackgroundFill: _G6_COLORS.bg,
          },
          state: _g6EdgeStateStyle(),
        },
        layout: preset ? { type: "preset" } : { type: "dagre", rankdir: "LR", nodesep: 18, ranksep: 66 },
        behaviors,
        autoFit: preset ? undefined : "view",
        padding: 28,
      });
      graphRef.current = g;
      readyRef.current = false;

      g.on("node:click", (e) => { const id = e && (e.target?.id ?? e.itemId); if (id && cb.current.onNodeClick) cb.current.onNodeClick(id); });
      g.on("node:dblclick", (e) => { const id = e && (e.target?.id ?? e.itemId); if (id && cb.current.onNodeDoubleClick) cb.current.onNodeDoubleClick(id); });
      g.on("canvas:click", () => { if (cb.current.onBackgroundClick) cb.current.onBackgroundClick(); });
      g.on("edge:click", (e) => {
        const eid = e && (e.target?.id ?? e.itemId);
        let idx;
        try { const ed = g.getEdgeData ? g.getEdgeData(eid) : null; idx = ed && ed.data ? ed.data.idx : undefined; } catch (_e) { /* canvas */ }
        if (typeof idx === "number" && idx >= 0 && cb.current.onEdgeClick) cb.current.onEdgeClick(idx);
      });
      if (interactive) {
        const onDragEnd = () => {
          if (!cb.current.onMoveNode) return;
          for (const n of (draft.nodes || [])) {
            try {
              const pos = g.getElementPosition ? g.getElementPosition(n.id) : null;
              if (pos && Array.isArray(pos)) {
                const sz = _g6Size(n.kind);
                cb.current.onMoveNode(n.id, Math.round((pos[0] - sz.w / 2) / 10) * 10, Math.round((pos[1] - sz.h / 2) / 10) * 10);
              }
            } catch (_e) { /* canvas */ }
          }
        };
        g.on("afterdragelement", onDragEnd);
        g.on("node:dragend", onDragEnd);
        const onAddEdge = (evt) => {
          try {
            const ed = evt && (evt.edge || evt.data || evt);
            const source = ed && (ed.source ?? ed.sourceNode ?? (ed.data && ed.data.source));
            const target = ed && (ed.target ?? ed.targetNode ?? (ed.data && ed.data.target));
            if (source && target && source !== target && cb.current.onConnect) cb.current.onConnect(source, target);
          } catch (_e) { /* canvas */ }
        };
        g.on("afteraddedge", onAddEdge);
        g.on("aftercreateedge", onAddEdge);
        g.on("edge:create", onAddEdge);
      }

      Promise.resolve(g.render()).then(() => {
        readyRef.current = true;
        // Preset (editor) skips autoFit so user positions are honored, but on
        // (re)mount / structural change we fit-when-overflowing so far-right
        // nodes aren't clipped. topoKey excludes x/y, so this never fires on
        // a drag — pan/zoom is preserved while editing.
        if (preset) {
          // Defer a tick so the container has settled at full width before we
          // measure — fitting too early frames against a narrower canvas and
          // clips the far nodes.
          setTimeout(() => {
            if (graphRef.current !== g) return;
            try { g.fitView({ when: "overflow", padding: 24 }); }
            catch (_e) { try { g.fitView(); } catch (_e2) { /* canvas */ } }
          }, 60);
        }
      }).catch(() => {});
    } catch (err) {
      console.error("[GR_Canvas] init failed:", err);  // eslint-disable-line no-console
    }

    return () => {
      readyRef.current = false;
      try { graphRef.current && graphRef.current.destroy(); } catch (_e) { /* no-op */ }
      graphRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topoKey]);

  // Status + selection as element states (animated; no relayout).
  React.useEffect(() => {
    const g = graphRef.current;
    if (!g) return undefined;
    const apply = () => {
      try {
        const s = {};
        for (const n of (draft?.nodes || [])) {
          const sel = n.id === selectedNodeId || (addEdgeMode && addEdgeMode.fromId === n.id);
          if (statusTint) {
            const st = (statusTint[n.id] && statusTint[n.id].status) || "pending";
            s[n.id] = sel ? [st, "selected"] : [st];
          } else {
            s[n.id] = sel ? ["selected"] : [];
          }
        }
        for (const e of _g6Edges(draft)) {
          const states = [];
          if (e.data.idx >= 0 && e.data.idx === selectedEdgeId) states.push("selected");
          if (statusTint) {
            const srcStatus = (statusTint[e.source] && statusTint[e.source].status) || "pending";
            if (srcStatus === "running" || srcStatus === "ended") states.push("active");
          }
          s[e.id] = states;
        }
        g.setElementState(s);
      } catch (_e) { /* canvas */ }
    };
    if (readyRef.current) apply();
    else { const t = setTimeout(apply, 140); return () => clearTimeout(t); }
    return undefined;
  }, [statusTint, selectedNodeId, selectedEdgeId, draft, addEdgeMode]);

  // Live token/duration metric — update labels as nodes finish (run-view).
  React.useEffect(() => {
    const g = graphRef.current;
    if (!g || !readyRef.current || !metaByNode) return;
    try {
      g.updateNodeData((draft?.nodes || []).map((n) => ({
        id: n.id, data: { kind: n.kind, label: _g6Label(n, metaByNode) },
      })));
      g.draw();
    } catch (_e) { /* canvas */ }
  }, [metaByNode, draft]);

  // Running pulse (run-view).
  React.useEffect(() => {
    if (!statusTint) return undefined;
    const running = (draft?.nodes || [])
      .filter((n) => (statusTint[n.id] && statusTint[n.id].status) === "running")
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
      } catch (_e) { /* canvas */ }
    }, 680);
    return () => clearInterval(id);
  }, [statusTint, selectedNodeId, draft]);

  return (
    <div style={{ minWidth: 0, overflow: "hidden" }}>
      <div
        ref={containerRef}
        data-testid="graph-canvas"
        style={{ width: "100%", height: 500, minHeight: 500, background: _G6_COLORS.bg }}
      />
    </div>
  );
}

window.GR_Canvas = GR_Canvas;
window.GR_NODE_SIZE = GR_NODE_SIZE;
window._g6IconUri = _g6IconUri;
