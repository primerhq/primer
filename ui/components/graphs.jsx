/* global React, Icon, StatusPill, Btn, Banner, relativeTime */

const GRAPH_DETAILS = {
  "graph-tier1-escalation": {
    desc: "Tier-1 support triage → tier-2 escalation flow.",
    created_at_ago: 3600 * 24 * 3,
    ok: false,
    issues: [
      { kind: "executor_unimplemented", target: "engine", detail: "Graph executor raises NotImplementedError. Pinned in spec §12." },
    ],
    nodes: [
      { id: "start", kind: "agent", agent_id: "support-triage", x: 30, y: 60, label: "Triage" },
      { id: "billing", kind: "agent", agent_id: "stripe-refunds", x: 220, y: 20, label: "Billing handler" },
      { id: "tech", kind: "agent", agent_id: "code-explainer", x: 220, y: 110, label: "Tech support" },
      { id: "subflow", kind: "graph", graph_id: "graph-onboarding-wizard", x: 220, y: 200, label: "Onboarding sub" },
      { id: "done", kind: "terminal", x: 410, y: 60, label: "Resolved" },
      { id: "esc", kind: "terminal", x: 410, y: 200, label: "Escalate" },
    ],
    edges: [
      { from: "start", to: "billing", kind: "json_path", expr: "$.category == 'billing'" },
      { from: "start", to: "tech", kind: "json_path", expr: "$.category == 'technical'" },
      { from: "start", to: "subflow", kind: "callable", expr: "needs_onboarding()" },
      { from: "billing", to: "done", kind: "static" },
      { from: "tech", to: "done", kind: "static" },
      { from: "subflow", to: "esc", kind: "static" },
    ],
  },
  "graph-onboarding-wizard": {
    desc: "Onboarding wizard flow.",
    created_at_ago: 3600 * 24 * 8,
    ok: true,
    issues: [],
    nodes: [
      { id: "n1", kind: "agent", agent_id: "doc-ingestion", x: 80, y: 80, label: "Step 1" },
      { id: "n2", kind: "agent", agent_id: "sql-helper", x: 320, y: 80, label: "Step 2" },
      { id: "n3", kind: "terminal", x: 540, y: 80, label: "Done" },
    ],
    edges: [
      { from: "n1", to: "n2", kind: "static" },
      { from: "n2", to: "n3", kind: "static" },
    ],
  },
};

function GraphsPage({ onOpen }) {
  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter graphs…" />
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus">New graph</Btn>
        </div>
      </div>
      <Banner
        kind="warning"
        title="Graph executor is unimplemented"
        detail="Any session bound to a graph fails on turn 1 with NotImplementedError. The list and editor still work — saved graphs are ready for when the engine ships."
      />
      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Description</th>
              <th style={{ textAlign: "right" }}>Nodes</th>
              <th style={{ textAlign: "right" }}>Edges</th>
              <th>Created</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(GRAPH_DETAILS).map(([gid, g]) => (
              <tr key={gid} onClick={() => onOpen(gid)}>
                <td className="mono">{gid}</td>
                <td className="muted" style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{g.desc}</td>
                <td className="mono num tabular">{g.nodes.length}</td>
                <td className="mono num tabular">{g.edges.length}</td>
                <td className="mono muted">{relativeTime(g.created_at_ago)}</td>
                <td>
                  {g.ok ? <span className="pill pill-ended"><span className="dot"></span>ok</span> : <span className="pill pill-failed"><span className="dot"></span>1 issue</span>}
                </td>
                <td style={{ textAlign: "right", paddingRight: 12 }}><Icon name="chevron-right" size={12} className="muted" /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------
// Visual graph editor
// -----------------------------------------------------------------------

function GraphDetail({ graphId, pushToast }) {
  const initial = GRAPH_DETAILS[graphId];
  const [nodes, setNodes] = React.useState(() => initial ? initial.nodes.map(n => ({ ...n })) : []);
  const [edges] = React.useState(() => initial ? initial.edges : []);
  const [selectedId, setSelectedId] = React.useState(null);
  const [dirty, setDirty] = React.useState(0);
  const [drag, setDrag] = React.useState(null);
  const canvasRef = React.useRef(null);

  if (!initial) return null;

  const onMouseDown = (e, nodeId) => {
    e.stopPropagation();
    setSelectedId(nodeId);
    const n = nodes.find((nn) => nn.id === nodeId);
    if (!n) return;
    const rect = canvasRef.current.getBoundingClientRect();
    setDrag({ id: nodeId, dx: e.clientX - rect.left - n.x, dy: e.clientY - rect.top - n.y });
  };

  React.useEffect(() => {
    if (!drag) return;
    const move = (e) => {
      const rect = canvasRef.current.getBoundingClientRect();
      const newX = Math.max(10, Math.min(rect.width - 130, e.clientX - rect.left - drag.dx));
      const newY = Math.max(10, Math.min(rect.height - 50, e.clientY - rect.top - drag.dy));
      setNodes((arr) => arr.map((n) => n.id === drag.id ? { ...n, x: newX, y: newY } : n));
    };
    const up = () => { setDrag(null); setDirty((d) => d + 1); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, [drag]);

  const selected = nodes.find((n) => n.id === selectedId);

  // Compute edge paths
  const nodeSize = { agent: { w: 130, h: 50 }, graph: { w: 130, h: 50 }, terminal: { w: 18, h: 18 } };

  const renderEdge = (e, i) => {
    const a = nodes.find((n) => n.id === e.from);
    const b = nodes.find((n) => n.id === e.to);
    if (!a || !b) return null;
    const aSize = nodeSize[a.kind];
    const bSize = nodeSize[b.kind];
    const ax = a.x + aSize.w;
    const ay = a.y + aSize.h / 2;
    const bx = b.x;
    const by = b.y + bSize.h / 2;
    const mx = (ax + bx) / 2;
    const path = `M ${ax} ${ay} C ${mx} ${ay}, ${mx} ${by}, ${bx} ${by}`;
    const isDashed = e.kind !== "static";
    const color = e.kind === "static" ? "var(--text-3)" : "var(--accent)";
    return (
      <g key={i}>
        <path d={path} stroke={color} strokeWidth="1.6" fill="none" strokeDasharray={isDashed ? "5 4" : "0"} markerEnd="url(#arrow)" />
        {e.expr && (
          <text x={mx} y={(ay + by) / 2 - 6} textAnchor="middle" fontSize="10" fontFamily="IBM Plex Mono" fill={color}>
            {e.expr.length > 24 ? e.expr.slice(0, 22) + "…" : e.expr}
          </text>
        )}
      </g>
    );
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      {!initial.ok && (
        <Banner
          kind="warning"
          title="Graph executor is unimplemented"
          detail="Sessions bound to this graph will fail on turn 1 with NotImplementedError. You can still edit and save the graph for when the engine ships."
        />
      )}

      <div className="panel" style={{ overflow: "hidden" }}>
        {/* Toolbar */}
        <div className="panel-h" style={{ padding: "8px 12px" }}>
          <Btn size="sm" kind="ghost" icon="plus">Node</Btn>
          <Btn size="sm" kind="ghost" icon="plus">Edge</Btn>
          <Btn size="sm" kind="ghost" icon="refresh">Auto-layout</Btn>
          <div className="right">
            {dirty > 0 && <span className="muted text-sm tabular">· {dirty} change{dirty === 1 ? "" : "s"}</span>}
            <Btn size="sm" kind="ghost" onClick={() => { setNodes(initial.nodes.map(n => ({ ...n }))); setDirty(0); }}>Discard</Btn>
            <Btn size="sm" kind="primary" icon="check" disabled={dirty === 0} onClick={() => { pushToast({ kind: "success", title: "Graph saved", detail: `PUT /v1/graphs/${graphId} returned 200. ${dirty} changes committed.` }); setDirty(0); }}>Save</Btn>
          </div>
        </div>

        {/* Editor + side panel */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 240px" }}>
          <div
            ref={canvasRef}
            onClick={() => setSelectedId(null)}
            style={{
              position: "relative",
              minHeight: 480,
              height: 480,
              background: "var(--bg)",
              backgroundImage: "radial-gradient(circle, var(--border) 1px, transparent 1px)",
              backgroundSize: "20px 20px",
              overflow: "auto",
              userSelect: "none",
            }}
          >
            {/* Sized inner so canvas can scroll horizontally on narrow viewports */}
            <div style={{ position: "relative", width: 720, height: 460 }}>
            {/* edges as SVG behind */}
            <svg style={{ position: "absolute", inset: 0, pointerEvents: "none" }} width="720" height="460">
              <defs>
                <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto" markerUnits="strokeWidth">
                  <path d="M0,0 L8,4 L0,8 z" fill="var(--text-3)" />
                </marker>
              </defs>
              {edges.map(renderEdge)}
            </svg>

            {/* nodes */}
            {nodes.map((n) => (
              <Node
                key={n.id}
                node={n}
                selected={selectedId === n.id}
                onMouseDown={(e) => onMouseDown(e, n.id)}
              />
            ))}

            {/* legend */}
            <div style={{ position: "absolute", bottom: 8, left: 8, fontSize: 10.5, color: "var(--text-3)", fontFamily: "IBM Plex Mono", display: "flex", gap: 14 }}>
              <span><span style={{ display: "inline-block", width: 14, height: 2, background: "var(--text-3)", verticalAlign: "middle", marginRight: 4 }}></span>static</span>
              <span><span style={{ display: "inline-block", width: 14, height: 2, background: "var(--accent)", verticalAlign: "middle", marginRight: 4, borderTop: "1px dashed var(--accent)" }}></span>json_path / callable</span>
            </div>
            </div>
          </div>

          {/* Side panel */}
          <div style={{ borderLeft: "1px solid var(--border)", padding: 14, fontSize: 12.5, overflow: "auto", maxHeight: 460 }}>
            {selected ? (
              <NodePanel node={selected} />
            ) : (
              <div className="col" style={{ gap: 8 }}>
                <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>graph</div>
                <div className="mono" style={{ fontWeight: 600, fontSize: 14 }}>{graphId}</div>
                <div className="muted text-sm">{initial.desc}</div>
                <div className="mt-3 muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>stats</div>
                <dl className="kv" style={{ gridTemplateColumns: "100px 1fr", gap: "4px 12px" }}>
                  <dt>nodes</dt><dd>{nodes.length}</dd>
                  <dt>edges</dt><dd>{edges.length}</dd>
                  <dt>entry</dt><dd className="mono">{nodes[0]?.id}</dd>
                  <dt>terminals</dt><dd>{nodes.filter((n) => n.kind === "terminal").length}</dd>
                </dl>
                <div className="muted text-sm mt-3">
                  Click a node to inspect its config. Drag nodes to reposition.
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Status panel */}
      <div className="panel" style={{
        background: initial.ok ? "linear-gradient(90deg, var(--green-dim) 0%, var(--bg-1) 50%)" : "linear-gradient(90deg, var(--red-dim) 0%, var(--bg-1) 50%)",
        borderColor: initial.ok ? "oklch(0.75 0.15 145 / 0.3)" : "oklch(0.7 0.2 25 / 0.3)",
      }}>
        <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 18px" }}>
          <Icon name={initial.ok ? "check-circle" : "x-circle"} size={20} style={{ color: initial.ok ? "var(--green)" : "var(--red)" }} />
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 600 }}>
              {initial.ok ? "All references resolve" : `${initial.issues.length} issue${initial.issues.length === 1 ? "" : "s"} found`}
            </div>
            <div className="muted text-sm"><span className="mono">GET /v1/graphs/{graphId}/status</span> · last checked just now</div>
            {!initial.ok && initial.issues.map((iss, i) => (
              <div key={i} className="muted text-sm mt-2"><span className="mono" style={{ color: "var(--red)" }}>{iss.kind}</span> — {iss.detail}</div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function Node({ node, selected, onMouseDown }) {
  const isTerminal = node.kind === "terminal";
  if (isTerminal) {
    return (
      <div
        onMouseDown={onMouseDown}
        onClick={(e) => e.stopPropagation()}
        style={{
          position: "absolute",
          left: node.x,
          top: node.y,
          display: "flex",
          alignItems: "center",
          gap: 6,
          cursor: "grab",
        }}
      >
        <div style={{
          width: 18, height: 18, borderRadius: "50%",
          background: selected ? "var(--accent)" : "var(--text)",
          border: selected ? "2px solid var(--accent)" : "2px solid var(--border-strong)",
          boxShadow: selected ? "0 0 0 4px var(--accent-dim)" : undefined,
        }}></div>
        <div className="mono text-sm" style={{ color: selected ? "var(--accent)" : "var(--text-2)", whiteSpace: "nowrap" }}>{node.label}</div>
      </div>
    );
  }
  const isGraph = node.kind === "graph";
  return (
    <div
      onMouseDown={onMouseDown}
      onClick={(e) => e.stopPropagation()}
      style={{
        position: "absolute",
        left: node.x,
        top: node.y,
        width: 130,
        height: 50,
        borderRadius: 8,
        background: isGraph ? "transparent" : "var(--bg-1)",
        border: selected ? "2px solid var(--accent)" : `${isGraph ? "1.5px dashed" : "1.5px solid"} var(--border-strong)`,
        boxShadow: selected ? "0 0 0 4px var(--accent-dim)" : "0 1px 0 rgba(0,0,0,0.2)",
        cursor: "grab",
        padding: "6px 10px",
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11 }}>
        <Icon name={isGraph ? "graph" : "agent"} size={11} style={{ color: isGraph ? "var(--violet)" : "var(--accent)" }} />
        <span className="mono" style={{ fontSize: 11, fontWeight: 500 }}>{node.label}</span>
        <span className="pill pill-ended" style={{ marginLeft: "auto", padding: "0 4px", fontSize: 9.5 }}><span className="dot"></span>ok</span>
      </div>
      <div className="mono muted text-sm" style={{ fontSize: 10.5, marginTop: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {isGraph ? node.graph_id : node.agent_id}
      </div>
    </div>
  );
}

function NodePanel({ node }) {
  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>{node.kind} node</div>
      <div className="mono" style={{ fontWeight: 600, fontSize: 14 }}>{node.label}</div>
      <dl className="kv" style={{ gridTemplateColumns: "100px 1fr", gap: "4px 12px" }}>
        <dt>id</dt><dd>{node.id}</dd>
        {node.agent_id && (<><dt>agent_id</dt><dd>{node.agent_id}</dd></>)}
        {node.graph_id && (<><dt>graph_id</dt><dd>{node.graph_id}</dd></>)}
        <dt>x</dt><dd>{Math.round(node.x)}</dd>
        <dt>y</dt><dd>{Math.round(node.y)}</dd>
      </dl>
      <div className="muted text-sm mt-2">
        Edits are made via <span className="mono">PUT /v1/graphs/{`{id}`}</span> — the whole graph is replaced atomically.
      </div>
    </div>
  );
}

window.GraphsPage = GraphsPage;
window.GraphDetail = GraphDetail;
window.GRAPH_DETAILS = GRAPH_DETAILS;
