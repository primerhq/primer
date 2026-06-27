/* global React, Icon */
// Shared read-only graph canvas primitives, extracted verbatim from
// graphs.jsx so BOTH the graph editor (GR_GraphEditor) and the
// session-detail run view (SD_GraphRunView) render identical layout
// without duplicating node-size/edge math. No behavior change vs the
// pre-extraction graphs.jsx; the run view adds a status overlay on top.

const GR_NODE_SIZE = {
  agent: { w: 150, h: 56 },
  graph: { w: 150, h: 56 },
  begin: { w: 22, h: 22 },
  end: { w: 22, h: 22 },
  // Spec B node kinds:
  fan_out: { w: 170, h: 56 },
  fan_in: { w: 150, h: 56 },
  tool_call: { w: 170, h: 56 },
};

const GR_Canvas = React.forwardRef(function GR_Canvas(
  {
    draft,
    selectedNodeId,
    selectedEdgeId,
    addEdgeMode,
    onNodeClick,
    onEdgeClick,
    onNodeDoubleClick,
    onNodeMouseDown,
    onBackgroundClick,
    statusTint,
  },
  ref,
) {
  // Compute canvas extent (auto-grow for far-flung nodes).
  let maxX = 600;
  let maxY = 380;
  for (const n of draft.nodes) {
    const sz = GR_NODE_SIZE[n.kind] || GR_NODE_SIZE.agent;
    if ((n.x || 0) + sz.w + 40 > maxX) maxX = (n.x || 0) + sz.w + 40;
    if ((n.y || 0) + sz.h + 40 > maxY) maxY = (n.y || 0) + sz.h + 40;
  }

  return (
    <div
      ref={ref}
      onClick={onBackgroundClick}
      style={{
        position: "relative",
        minHeight: 500,
        height: 500,
        overflow: "auto",
        background: "var(--bg)",
        backgroundImage: "radial-gradient(circle, var(--border) 1px, transparent 1px)",
        backgroundSize: "20px 20px",
        userSelect: "none",
        cursor: addEdgeMode ? "crosshair" : "default",
      }}
    >
      <div style={{ position: "relative", width: maxX, height: maxY }}>
        <svg
          style={{ position: "absolute", inset: 0, pointerEvents: "none" }}
          width={maxX}
          height={maxY}
        >
          <defs>
            <marker id="arrow-static" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L8,4 L0,8 z" fill="var(--text-3)" />
            </marker>
            <marker id="arrow-cond" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L8,4 L0,8 z" fill="var(--accent)" />
            </marker>
            {/* Smaller arrowhead for the FanOut implicit dashed edges so
                they read as "configured, not wired". */}
            <marker id="arrow-fanout" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L6,3 L0,6 z" fill="var(--text-3)" />
            </marker>
          </defs>
          {(draft.edges || []).map((e, i) => (
            <GR_EdgePath
              key={i}
              edge={e}
              nodes={draft.nodes}
              selected={selectedEdgeId === i}
              onClick={(ev) => { ev.stopPropagation(); if (onEdgeClick) onEdgeClick(i); }}
            />
          ))}
          {/* Spec B §1.3: FanOut nodes have NO entries in `graph.edges`;
              their downstream targets live on per-spec fields. Render
              one dashed path per (FanOut, target) pair so the operator
              sees the implicit wiring on the canvas without it counting
              as a static edge. */}
          <g className="fanout-implicit-edges">
            {GR_collectFanOutImplicitEdges(draft.nodes).map(({ from, to }, i) => (
              <GR_ImplicitFanOutEdge key={`fo-${i}`} from={from} to={to} />
            ))}
          </g>
        </svg>

        {draft.nodes.map((n) => (
          <GR_NodeBox
            key={n.id}
            node={n}
            selected={selectedNodeId === n.id}
            entry={draft.entry_node_id === n.id}
            edgePicking={!!addEdgeMode}
            edgePickStage={addEdgeMode && addEdgeMode.fromId === n.id ? "from" : null}
            onClick={(ev) => { ev.stopPropagation(); onNodeClick(n.id); }}
            onDoubleClick={(ev) => { ev.stopPropagation(); onNodeDoubleClick(n.id); }}
            onMouseDown={(ev) => onNodeMouseDown(ev, n.id)}
          />
        ))}

        {/* Optional per-node status tint (run view). Rendered INSIDE the
            scroll container so the rings scroll with the nodes and never
            overflow the page; the editor passes no statusTint. */}
        {statusTint && draft.nodes.map((n) => {
          const t = statusTint[n.id];
          if (!t) return null;
          const sz = GR_NODE_SIZE[n.kind] || GR_NODE_SIZE.agent;
          return (
            <div
              key={`tint-${n.id}`}
              data-testid={`run-node-${n.id}`}
              data-status={t.status}
              style={{
                position: "absolute",
                left: (n.x || 0) - 2,
                top: (n.y || 0) - 2,
                width: sz.w + 4,
                height: sz.h + 4,
                borderRadius: n.kind === "begin" || n.kind === "end" ? "50%" : 10,
                border: `2px solid ${t.border}`,
                boxShadow: t.glow || undefined,
                animation: t.status === "running" ? "pulse 1.6s ease-in-out infinite" : undefined,
                pointerEvents: "none",
              }}
            />
          );
        })}

        <div style={{
          position: "absolute",
          bottom: 8,
          left: 8,
          fontSize: 10.5,
          color: "var(--text-3)",
          fontFamily: "IBM Plex Mono",
          display: "flex",
          gap: 14,
          pointerEvents: "none",
        }}>
          <span>
            <span style={{ display: "inline-block", width: 14, height: 2, background: "var(--text-3)", verticalAlign: "middle", marginRight: 4 }}></span>
            static
          </span>
          <span>
            <span style={{ display: "inline-block", width: 14, height: 0, borderTop: "1.5px dashed var(--accent)", verticalAlign: "middle", marginRight: 4 }}></span>
            conditional
          </span>
          {addEdgeMode && (
            <span style={{ color: "var(--accent)" }}>
              {addEdgeMode.fromId ? "Click target node…" : "Click source node…"}
            </span>
          )}
        </div>
      </div>
    </div>
  );
});

// ----------------------------------------------------------------------------
// GR_NodeBox — begin / agent / graph / end nodes
// ----------------------------------------------------------------------------

function GR_NodeBox({ node, selected, entry, edgePicking, edgePickStage, onClick, onDoubleClick, onMouseDown }) {
  const isBegin = node.kind === "begin";
  const isEnd = node.kind === "end";
  const isGraph = node.kind === "graph";

  const baseStyle = {
    position: "absolute",
    left: node.x || 0,
    top: node.y || 0,
    cursor: edgePicking ? "crosshair" : "grab",
  };

  if (isBegin || isEnd) {
    // Begin: hollow ring (accent); End: filled disk (text color).
    const ringStyle = isBegin
      ? {
          background: selected ? "var(--accent-dim)" : "var(--bg-1)",
          border: edgePickStage === "from"
            ? "2px dashed var(--accent)"
            : selected
              ? "2px solid var(--accent)"
              : "2px solid var(--accent)",
        }
      : {
          background: selected ? "var(--accent)" : "var(--text)",
          border: edgePickStage === "from"
            ? "2px dashed var(--accent)"
            : selected
              ? "2px solid var(--accent)"
              : "2px solid var(--border-strong)",
        };
    return (
      <div onMouseDown={onMouseDown} onClick={onClick} onDoubleClick={onDoubleClick} style={baseStyle}>
        <div style={{
          width: 22,
          height: 22,
          borderRadius: "50%",
          ...ringStyle,
          boxShadow: selected ? "0 0 0 4px var(--accent-dim)" : undefined,
        }} />
        <div className="mono text-sm" style={{
          color: selected ? "var(--accent)" : "var(--text-2)",
          whiteSpace: "nowrap",
          marginTop: 2,
          fontSize: 11,
        }}>
          {entry ? "▶ " : ""}{node.id}
        </div>
      </div>
    );
  }

  return (
    <div
      onMouseDown={onMouseDown}
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      style={{
        ...baseStyle,
        width: GR_NODE_SIZE[node.kind].w,
        height: GR_NODE_SIZE[node.kind].h,
        borderRadius: 8,
        background: isGraph ? "transparent" : "var(--bg-1)",
        border: edgePickStage === "from"
          ? "2px dashed var(--accent)"
          : selected
            ? "2px solid var(--accent)"
            : `${isGraph ? "1.5px dashed" : "1.5px solid"} var(--border-strong)`,
        boxShadow: selected ? "0 0 0 4px var(--accent-dim)" : "0 1px 0 rgba(0, 0, 0, 0.2)",
        padding: "6px 10px",
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11 }}>
        <Icon
          name={isGraph ? "graph" : "agent"}
          size={11}
          style={{ color: isGraph ? "var(--violet)" : "var(--accent)" }}
        />
        <span className="mono" style={{ fontSize: 11, fontWeight: 500 }}>
          {entry ? "▶ " : ""}{node.id}
        </span>
        <span className="muted" style={{ fontSize: 9.5, marginLeft: "auto" }}>
          {node.kind}
        </span>
      </div>
      <div className="mono muted text-sm" style={{
        fontSize: 10.5,
        marginTop: 3,
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
      }}>
        {isGraph
          ? (node.graph_id || <span style={{ color: "var(--red)" }}>(graph_id not set)</span>)
          : (node.agent_id || <span style={{ color: "var(--red)" }}>(agent_id not set)</span>)}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------
// GR_EdgePath — per-edge SVG router (static / conditional)
// ----------------------------------------------------------------------------

function GR_EdgePath({ edge, nodes, selected, onClick }) {
  // For conditional edges with a json_path router, draw one curve per
  // branch + an optional default-to curve. For static edges and
  // callable-router conditionals, draw one curve to the single known
  // target (callable routers expose no static target).
  const from = nodes.find((n) => n.id === edge.from_node);
  if (!from) return null;
  const fromSize = GR_NODE_SIZE[from.kind] || GR_NODE_SIZE.agent;
  const fx = (from.x || 0) + fromSize.w;
  const fy = (from.y || 0) + fromSize.h / 2;

  if (edge.kind === "static") {
    const to = nodes.find((n) => n.id === edge.to_node);
    if (!to) return null;
    return (
      <GR_SingleEdge
        fx={fx}
        fy={fy}
        to={to}
        dashed={false}
        label={null}
        selected={selected}
        onClick={onClick}
      />
    );
  }

  // conditional
  const router = edge.router || {};
  if (router.kind === "json_path") {
    const out = [];
    const branches = router.branches || [];
    for (let i = 0; i < branches.length; i += 1) {
      const br = branches[i];
      const to = nodes.find((n) => n.id === br.to_node);
      if (!to) continue;
      // Prefer the new `conditions` shape; fall back to the legacy `when`.
      const condBits = (br.conditions || []).map((c) => {
        if (c.op === "exists") return `${c.path}?`;
        return `${c.path} ${c.op} ${JSON.stringify(c.value)}`;
      });
      const whenBits = Object.entries(br.when || {}).map(([k, v]) => `${k}=${v}`);
      const label = (condBits.length ? condBits : whenBits).join(" ∧ ");
      out.push(
        <GR_SingleEdge
          key={"b" + i}
          fx={fx}
          fy={fy}
          to={to}
          dashed
          label={label}
          selected={selected}
          onClick={onClick}
        />,
      );
    }
    if (router.default_to) {
      const to = nodes.find((n) => n.id === router.default_to);
      if (to) {
        out.push(
          <GR_SingleEdge
            key="def"
            fx={fx}
            fy={fy}
            to={to}
            dashed
            label="(default)"
            selected={selected}
            onClick={onClick}
          />,
        );
      }
    }
    // Branch-count badge near the source midpoint.
    const badgeText = `${branches.length} branch${branches.length === 1 ? "" : "es"}`;
    out.push(
      <text
        key="badge"
        x={fx + 8}
        y={fy - 8}
        fontSize="9.5"
        fontFamily="IBM Plex Mono"
        fill="var(--accent)"
        style={{ pointerEvents: "auto", cursor: "pointer" }}
        onClick={onClick}
      >
        {badgeText}
      </text>,
    );
    return <>{out}</>;
  }

  if (router.kind === "callable") {
    // Callable routers have no static target — draw a stub arrow.
    return (
      <text x={fx + 12} y={fy} fontSize="10" fontFamily="IBM Plex Mono" fill="var(--accent)">
        callable:{router.callable_id || "?"}
      </text>
    );
  }
  return null;
}

function GR_SingleEdge({ fx, fy, to, dashed, label, selected, onClick }) {
  const toSize = GR_NODE_SIZE[to.kind] || GR_NODE_SIZE.agent;
  const tx = to.x || 0;
  const ty = (to.y || 0) + toSize.h / 2;
  const mx = (fx + tx) / 2;
  const path = `M ${fx} ${fy} C ${mx} ${fy}, ${mx} ${ty}, ${tx} ${ty}`;
  const stroke = selected ? "var(--accent)" : (dashed ? "var(--accent)" : "var(--text-3)");
  const marker = dashed ? "url(#arrow-cond)" : "url(#arrow-static)";
  const strokeWidth = selected ? "3.2" : "1.6";
  const interactive = typeof onClick === "function";
  return (
    <g style={interactive ? { pointerEvents: "auto", cursor: "pointer" } : undefined} onClick={onClick}>
      {/* Invisible wide hit target for easier clicking on thin paths. */}
      {interactive && (
        <path
          d={path}
          stroke="transparent"
          strokeWidth="12"
          fill="none"
        />
      )}
      <path
        d={path}
        stroke={stroke}
        strokeWidth={strokeWidth}
        fill="none"
        strokeDasharray={dashed ? "5 4" : "0"}
        markerEnd={marker}
      />
      {label && (
        <text
          x={mx}
          y={(fy + ty) / 2 - 6}
          textAnchor="middle"
          fontSize="10"
          fontFamily="IBM Plex Mono"
          fill={stroke}
        >
          {label.length > 30 ? label.slice(0, 28) + "…" : label}
        </text>
      )}
    </g>
  );
}

// ----------------------------------------------------------------------------
// FanOut implicit-edge collection + render.
//
// Spec B §1.3 forbids FanOut nodes from appearing as `from_node` in
// `graph.edges` — their downstream targets are configured per-spec
// (broadcast.target_node_id, tee.target_node_ids, map.target_node_id).
// The editor renders these as dashed lines on the canvas so the operator
// can see the implicit wiring without it counting as a static edge.
//
// `GR_collectFanOutImplicitEdges` flattens every (FanOut, target) pair,
// de-duped so a spec listing the same target twice still draws once.
// Targets that don't resolve to a known node are skipped silently — the
// topology-violations banner already flags them.
// ----------------------------------------------------------------------------

function GR_collectFanOutImplicitEdges(nodes) {
  if (!Array.isArray(nodes)) return [];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const seen = new Set();
  const out = [];
  for (const node of nodes) {
    if (node.kind !== "fan_out") continue;
    const specs = Array.isArray(node.specs) ? node.specs : [];
    for (const spec of specs) {
      const targets = [];
      if (spec.kind === "broadcast" || spec.kind === "map") {
        if (spec.target_node_id) targets.push(spec.target_node_id);
      } else if (spec.kind === "tee") {
        for (const tid of (spec.target_node_ids || [])) targets.push(tid);
      }
      for (const tid of targets) {
        const key = `${node.id}->${tid}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const to = byId.get(tid);
        if (!to) continue;  // unknown target — flagged elsewhere
        out.push({ from: node, to });
      }
    }
  }
  return out;
}

function GR_ImplicitFanOutEdge({ from, to }) {
  const fromSize = GR_NODE_SIZE[from.kind] || GR_NODE_SIZE.agent;
  const toSize = GR_NODE_SIZE[to.kind] || GR_NODE_SIZE.agent;
  const fx = (from.x || 0) + fromSize.w;
  const fy = (from.y || 0) + fromSize.h / 2;
  const tx = to.x || 0;
  const ty = (to.y || 0) + toSize.h / 2;
  const mx = (fx + tx) / 2;
  const path = `M ${fx} ${fy} C ${mx} ${fy}, ${mx} ${ty}, ${tx} ${ty}`;
  return (
    <path
      d={path}
      stroke="var(--text-3)"
      strokeWidth="1.4"
      fill="none"
      strokeDasharray="6 4"
      markerEnd="url(#arrow-fanout)"
      style={{ pointerEvents: "none" }}
    />
  );
}

Object.assign(window, {
  GR_NODE_SIZE,
  GR_Canvas,
  GR_NodeBox,
  GR_EdgePath,
  GR_SingleEdge,
  GR_collectFanOutImplicitEdges,
  GR_ImplicitFanOutEdge,
});
