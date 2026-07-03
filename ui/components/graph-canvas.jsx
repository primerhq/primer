/* global React, G6, Icon */
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

// Palette read LIVE from the Studio design tokens (styles.css :root[data-theme])
// via getComputedStyle — the same pattern as studio-terminal.jsx's ST_xtermTheme
// (~L52). Reading the CSS custom properties instead of hardcoding hex is what
// lets a dark/light toggle restyle the G6 canvas (bug #6): both the init effect
// and the theme MutationObserver call this to get the current-theme colours.
// The `*Dim` entries pull the pre-baked translucent token variants
// (oklch(... / a)) so we never string-append a hex alpha to an oklch() value.
function _g6Palette() {
  const css = window.getComputedStyle(document.documentElement);
  const v = (name, fallback) => {
    const val = css.getPropertyValue(name);
    return val && val.trim() ? val.trim() : fallback;
  };
  return {
    neutral: v("--border-strong", "#3a3f47"),
    neutralDim: v("--bg-2", "#232428"),
    green: v("--green", "#34d399"),
    greenDim: v("--green-dim", "rgba(52,211,153,0.14)"),
    amber: v("--amber", "#fbbf24"),
    amberDim: v("--amber-dim", "rgba(251,191,36,0.14)"),
    red: v("--red", "#f87171"),
    redDim: v("--red-dim", "rgba(248,113,113,0.14)"),
    violet: v("--violet", "#a78bfa"),
    text: v("--text", "#e6e8eb"),
    sub: v("--text-2", "#9aa4af"),
    bg: v("--bg", "#0d0f12"),
    edge: v("--border-strong", "#4b525c"),
    edgeDim: v("--border", "#3b424b"),
  };
}

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
    + (stroke || "#e6e8eb") + '" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
    + inner + '</svg>';
  return "data:image/svg+xml," + encodeURIComponent(svg);
}

// Per-status node state styles (the live "glow"); G6 tweens state changes.
// Palette-driven (P from _g6Palette) so the theme observer can rebuild these
// on a dark/light flip. The fill uses the pre-baked `*Dim` token variants
// rather than appending a hex alpha (tokens are oklch(), not hex).
function _g6NodeStates(P) {
  const mk = (c, fillDim, halo) => ({
    stroke: c, lineWidth: 2, fill: fillDim,
    halo, haloStroke: c, haloStrokeOpacity: 0.32, haloLineWidth: halo ? 10 : 0,
  });
  return {
    pending: mk(P.neutral, P.neutralDim, false),
    running: mk(P.green, P.greenDim, true),
    waiting: mk(P.amber, P.amberDim, true),
    ended: mk(P.green, P.greenDim, false),
    failed: mk(P.red, P.redDim, true),
    pulse: { halo: true, haloStroke: P.green, haloLineWidth: 20, haloStrokeOpacity: 0.15 },
    selected: { stroke: P.violet, lineWidth: 3, halo: true, haloStroke: P.violet, haloStrokeOpacity: 0.4 },
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

function _g6EdgeStateStyle(P) {
  return {
    selected: { stroke: P.violet, lineWidth: 2.5 },
    active: { stroke: P.green, lineWidth: 2, lineDash: [6, 6], endArrow: true },
  };
}

// Base node / edge style builders — palette-driven so the initial G6 init and
// the theme MutationObserver (restyle-on-toggle) share one source of truth.
// iconSrc bakes the label colour into the SVG data-URI, so a re-theme must
// rebuild these (not just swap a token) to recolour the node icons too.
function _g6NodeStyle(P) {
  return {
    size: (d) => { const s = _g6Size(d.data.kind); return [s.w, s.h]; },
    radius: (d) => (_g6Tiny(d.data.kind) ? 12 : 8),
    fill: P.neutralDim,
    stroke: P.neutral,
    lineWidth: 2,
    labelText: (d) => d.data.label,
    labelPlacement: "center",
    labelDx: 9,
    labelFill: P.text,
    labelFontSize: 12,
    labelFontFamily: "IBM Plex Mono, monospace",
    labelLineHeight: 15,
    iconSrc: (d) => _g6IconUri(d.data.kind, P.text),
    iconWidth: 15,
    iconHeight: 15,
    iconX: -57,
  };
}
function _g6EdgeStyle(P) {
  return {
    stroke: (d) => (d.data.etype === "implicit" ? P.edgeDim : P.edge),
    lineWidth: 1.5,
    lineDash: (d) => (d.data.etype === "static" ? undefined : [5, 5]),
    endArrow: true,
    endArrowSize: 8,
    radius: 8,
    labelText: (d) => d.data.label || "",
    labelFill: P.sub,
    labelFontSize: 10,
    labelBackground: true,
    labelBackgroundFill: P.bg,
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

  // topoKey drives a full G6 (re)init. It excludes node x/y so a drag never
  // rebuilds the graph (pan/zoom preserved), but includes `layoutNonce` so an
  // explicit Auto-layout re-seeds the canvas with the freshly computed preset
  // positions (an x/y-only change is otherwise invisible to the canvas).
  const topoKey = React.useMemo(() => {
    const ns = (draft?.nodes || []).map((n) => `${n.id}:${n.kind}`).join(",");
    const es = _g6Edges(draft).map((e) => e.id).join(",");
    return (layout || "dagre") + "|" + ns + "|" + es + "|L" + (props.layoutNonce || 0);
  }, [draft, layout, props.layoutNonce]);

  React.useEffect(() => {
    if (!containerRef.current || !window.G6 || !draft) return undefined;
    const G6 = window.G6;
    const preset = layout === "preset";
    // Live theme palette — read fresh on every (re)init so the canvas is born
    // in the current dark/light theme; the observer below keeps it in sync.
    const P = _g6Palette();
    let themeObs = null;
    // Mutating behaviors (drag/connect) only when the editor wires them;
    // the run-view passes no onMoveNode/onConnect and stays read-only.
    const interactive = !!(cb.current.onMoveNode || cb.current.onConnect);
    const behaviors = ["zoom-canvas", "drag-canvas"];
    if (interactive) {
      // Two explicit, mutually-exclusive edit modes keyed off `addEdgeMode`
      // (toolbar "Add edge" toggle). Default = MOVE nodes (drag-element on,
      // create-edge off). Add-edge mode = CONNECT (drag-element off, create-
      // edge on). The `enable` callbacks re-read the live mode from cb.current
      // on every gesture, so toggling the toolbar switches modes with no
      // re-init. This makes it impossible to spawn an edge by dragging a node
      // to move it — the old always-on create-edge turned a node-move drag
      // (start+end on the same node) into a phantom self-loop.
      behaviors.push({ type: "drag-element", key: "drag-node", enable: () => !cb.current.addEdgeMode });
      behaviors.push({ type: "click-select", key: "sel", multiple: false });
      behaviors.push({
        type: "create-edge",
        key: "make-edge",
        trigger: "drag",
        enable: () => !!cb.current.addEdgeMode,
        // Belt: reject self-loops. G6 only commits the edge when onCreate
        // returns truthy, so returning false on source===target cancels the
        // drag cleanly (no phantom edge, no onFinish) — a deliberate drag that
        // ends back on the source node never creates an edge.
        onCreate: (edge) => (edge && edge.source !== edge.target ? edge : false),
        style: { stroke: P.violet, lineWidth: 1.5, lineDash: [4, 4], endArrow: true },
      });
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
        background: P.bg,
        data: { nodes, edges: _g6Edges(draft) },
        node: { type: "rect", style: _g6NodeStyle(P), state: _g6NodeStates(P) },
        edge: { type: "polyline", style: _g6EdgeStyle(P), state: _g6EdgeStateStyle(P) },
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

      // Bug #6 — restyle on dark/light toggle. G6's styles were hardcoded hex,
      // so flipping data-theme left the canvas stuck on its old colours. Watch
      // <html data-theme> (set by studio.jsx + the console theme effect on
      // document.documentElement), re-read the tokens, and push fresh
      // node/edge/background styles. Full re-render is fine here — a theme
      // toggle is rare and re-baking the icon data-URIs needs the mappers to
      // re-run. Disconnected on unmount below.
      const applyTheme = () => {
        const gg = graphRef.current;
        if (!gg) return;
        const P2 = _g6Palette();
        try {
          // Preserve the user's zoom across a theme flip: render() re-applies
          // the run-view's autoFit and would otherwise snap back to the fitted
          // view, throwing away a zoom-in. Capture before, restore after.
          // Fully wrapped — a G6 signature mismatch degrades to the old
          // (reset) behavior rather than crashing.
          var _zoom = null;
          try { _zoom = gg.getZoom(); } catch (_z) { /* */ }
          gg.setOptions({
            background: P2.bg,
            node: { type: "rect", style: _g6NodeStyle(P2), state: _g6NodeStates(P2) },
            edge: { type: "polyline", style: _g6EdgeStyle(P2), state: _g6EdgeStateStyle(P2) },
          });
          Promise.resolve(gg.render()).then(() => {
            if (_zoom != null) {
              try { gg.zoomTo(_zoom, false); } catch (_z) { /* */ }
            }
          }).catch(() => {});
        } catch (_e) { /* canvas */ }
      };
      themeObs = new MutationObserver(applyTheme);
      themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    } catch (err) {
      console.error("[GR_Canvas] init failed:", err);  // eslint-disable-line no-console
    }

    return () => {
      readyRef.current = false;
      try { themeObs && themeObs.disconnect(); } catch (_e) { /* no-op */ }
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

  // Overlaid zoom / traverse controls (bug #1) — wired to the live G6 v5
  // instance. zoomBy(ratio) is relative (>1 in, <1 out); fitView() frames the
  // whole graph; zoomTo(1) resets to 100%. SHARED by both surfaces (editor +
  // run-view) since the canvas is one component; the cluster sits over a corner
  // and only its buttons capture pointer events, so node drags elsewhere are
  // untouched. Each wraps a not-yet-ready graph as a no-op.
  const _zoomBy = (ratio) => {
    const g = graphRef.current;
    if (!g) return;
    try { Promise.resolve(g.zoomBy(ratio, true)).catch(() => {}); } catch (_e) { /* canvas */ }
  };
  const _fitView = () => {
    const g = graphRef.current;
    if (!g) return;
    try { Promise.resolve(g.fitView({ when: "always", padding: 24 }, true)).catch(() => {}); }
    catch (_e) { try { g.fitView(); } catch (_e2) { /* canvas */ } }
  };
  const _resetZoom = () => {
    const g = graphRef.current;
    if (!g) return;
    try { Promise.resolve(g.zoomTo(1, true)).catch(() => {}); } catch (_e) { /* canvas */ }
  };

  return (
    <div style={{ minWidth: 0, overflow: "hidden", position: "relative" }}>
      <div
        ref={containerRef}
        data-testid="graph-canvas"
        style={{ width: "100%", height: 500, minHeight: 500, background: "var(--bg)" }}
      />
      <div className="gr-canvas-controls" data-testid="graph-controls">
        <button type="button" className="gr-ctrl-btn" data-testid="graph-zoom-in"
          aria-label="Zoom in" title="Zoom in" onClick={() => _zoomBy(1.2)}>
          <Icon name="plus" size={15} />
        </button>
        <button type="button" className="gr-ctrl-btn" data-testid="graph-zoom-out"
          aria-label="Zoom out" title="Zoom out" onClick={() => _zoomBy(1 / 1.2)}>
          <Icon name="minus" size={15} />
        </button>
        <button type="button" className="gr-ctrl-btn" data-testid="graph-fit"
          aria-label="Fit graph to view" title="Fit to view" onClick={_fitView}>
          <Icon name="compress" size={15} />
        </button>
        <button type="button" className="gr-ctrl-btn gr-ctrl-btn-text" data-testid="graph-zoom-reset"
          aria-label="Reset zoom to 100%" title="Reset zoom" onClick={_resetZoom}>
          1:1
        </button>
      </div>
    </div>
  );
}

window.GR_Canvas = GR_Canvas;
window.GR_NODE_SIZE = GR_NODE_SIZE;
window._g6IconUri = _g6IconUri;
