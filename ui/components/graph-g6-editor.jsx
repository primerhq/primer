/* global React, G6 */
// SPIKE — AntV G6 v5 graph EDITOR proof-of-concept, to evaluate whether G6
// could replace the hand-rolled SVG editor (graphs.jsx, ~2.8k lines).
// Renders the editor draft with G6's INTERACTIVE behaviors enabled:
//   • drag-element  — move nodes (synced back to the draft x/y)
//   • create-edge   — drag from one node to another to wire a static edge
//   • click-select  — select a node (drives the existing side panel)
// Toggled in GR_GraphEditor; NOT the default. This proves the interaction
// model works no-build; full parity (palette drag-in, per-kind property
// forms, conditional-edge routing) is the remaining work.
//
// Props: { draft, selectedNodeId, onSelectNode, onCreateEdge(source,target),
//          onMoveNode(id,x,y) }.

function _g6eEdges(draft) {
  const out = [];
  (draft?.edges || []).forEach((e, i) => {
    const s = e.from_node;
    const t = e.to_node || (e.router && e.router.default_to);
    if (s && t) out.push({ id: `ed${i}:${s}->${t}`, source: s, target: t });
  });
  return out;
}

function GR_G6Editor({ draft, selectedNodeId, onSelectNode, onCreateEdge, onMoveNode }) {
  const ref = React.useRef(null);
  const gRef = React.useRef(null);
  const readyRef = React.useRef(false);
  const cb = React.useRef({});
  cb.current = { onSelectNode, onCreateEdge, onMoveNode };

  const topoKey = React.useMemo(() => {
    const ns = (draft?.nodes || []).map((n) => n.id).join(",");
    const es = _g6eEdges(draft).map((e) => e.id).join(",");
    return ns + "|" + es;
  }, [draft]);

  React.useEffect(() => {
    if (!ref.current || !window.G6 || !draft) return undefined;
    const G6 = window.G6;
    const isTiny = (k) => k === "begin" || k === "end";
    // Respect the draft's positions (the SVG editor stores top-left x/y;
    // G6 centers nodes, so offset by half-size).
    const nodes = (draft.nodes || []).map((n) => ({
      id: n.id,
      style: { x: (n.x || 60) + 75, y: (n.y || 60) + 23 },
      data: { kind: n.kind },
    }));

    let g;
    try {
      g = new G6.Graph({
        container: ref.current,
        autoResize: true,
        background: "#0d0f12",
        data: { nodes, edges: _g6eEdges(draft) },
        node: {
          type: "rect",
          style: {
            size: (d) => (isTiny(d.data.kind) ? [26, 26] : [150, 46]),
            radius: (d) => (isTiny(d.data.kind) ? 13 : 8),
            fill: "#1b2026",
            stroke: "#3f8f6f",
            lineWidth: 1.5,
            labelText: (d) => (isTiny(d.data.kind) ? "" : d.id),
            labelPlacement: "center",
            labelDx: 9,
            labelFill: "#e6e8eb",
            labelFontSize: 12,
            labelFontFamily: "IBM Plex Mono, monospace",
            iconSrc: (d) => (window._g6IconUri ? window._g6IconUri(d.data.kind) : undefined),
            iconWidth: 15,
            iconHeight: 15,
            iconX: -57,
          },
          state: { selected: { stroke: "#a78bfa", lineWidth: 3, halo: true, haloStroke: "#a78bfa", haloStrokeOpacity: 0.35 } },
        },
        edge: {
          type: "polyline",
          style: { stroke: "#5a626c", lineWidth: 1.5, endArrow: true, endArrowSize: 8, radius: 8 },
        },
        behaviors: [
          "zoom-canvas",
          "drag-canvas",
          { type: "drag-element", key: "drag-node" },
          { type: "click-select", key: "sel", multiple: false },
          { type: "create-edge", key: "make-edge", trigger: "drag",
            style: { stroke: "#a78bfa", lineWidth: 1.5, lineDash: [4, 4], endArrow: true } },
        ],
      });
      gRef.current = g;

      // Selection -> existing side panel.
      g.on("node:click", (e) => {
        const id = e && (e.target?.id ?? e.itemId);
        if (id && cb.current.onSelectNode) cb.current.onSelectNode(id);
      });

      // A new edge drawn via create-edge -> add a static edge to the draft.
      const onAddEdge = (evt) => {
        try {
          const ed = evt && (evt.edge || evt.data || evt);
          const source = ed && (ed.source ?? ed.sourceNode ?? (ed.data && ed.data.source));
          const target = ed && (ed.target ?? ed.targetNode ?? (ed.data && ed.data.target));
          if (source && target && source !== target && cb.current.onCreateEdge) {
            cb.current.onCreateEdge(source, target);
          }
        } catch (_e) { /* spike */ }
      };
      // G6 v5 fires one of these after create-edge finishes; wire all defensively.
      g.on("afteraddedge", onAddEdge);
      g.on("edge:create", onAddEdge);
      g.on("aftercreateedge", onAddEdge);

      // Node moved -> sync the top-left x/y back to the draft.
      const onDragEnd = () => {
        try {
          if (!cb.current.onMoveNode) return;
          for (const n of (draft.nodes || [])) {
            const pos = g.getElementPosition ? g.getElementPosition(n.id) : null;
            if (pos && Array.isArray(pos)) cb.current.onMoveNode(n.id, pos[0] - 75, pos[1] - 23);
          }
        } catch (_e) { /* spike */ }
      };
      g.on("afterdragelement", onDragEnd);
      g.on("node:dragend", onDragEnd);

      readyRef.current = false;
      Promise.resolve(g.render()).then(() => { readyRef.current = true; }).catch(() => {});
    } catch (err) {
      console.error("[GR_G6Editor] init failed:", err);  // eslint-disable-line no-console
    }

    return () => {
      readyRef.current = false;
      try { gRef.current && gRef.current.destroy(); } catch (_e) { /* no-op */ }
      gRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topoKey]);

  // Reflect external selection (guarded until the first render completes,
  // so setElementState doesn't run against an un-drawn renderer).
  React.useEffect(() => {
    const g = gRef.current;
    if (!g) return undefined;
    const apply = () => {
      try {
        const s = {};
        for (const n of (draft?.nodes || [])) s[n.id] = n.id === selectedNodeId ? ["selected"] : [];
        g.setElementState(s);
      } catch (_e) { /* spike */ }
    };
    if (readyRef.current) { apply(); return undefined; }
    const t = setTimeout(apply, 160);
    return () => clearTimeout(t);
  }, [selectedNodeId, draft]);

  return (
    <div style={{ minWidth: 0, overflow: "hidden" }}>
      <div ref={ref} data-testid="g6-editor-canvas" style={{ width: "100%", height: 520, minHeight: 520, background: "#0d0f12" }} />
    </div>
  );
}

window.GR_G6Editor = GR_G6Editor;
