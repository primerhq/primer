/* global React */

// graph-canvas-three-nodes mockup. SVG canvas with three labelled
// rectangle nodes (begin -> agent -> end) and two arrows. The
// `selected` prop highlights one of the nodes with the accent colour.

function GraphCanvasThreeNodesMockup({ selected = null }) {
  const nodes = [
    { id: "begin", x: 40,  y: 100, label: "begin" },
    { id: "agent", x: 250, y: 100, label: "agent" },
    { id: "end",   x: 460, y: 100, label: "end" },
  ];
  const nw = 100;
  const nh = 50;
  return (
    <div style={{
      background: "var(--bg-2)",
      borderRadius: 6,
      border: "1px solid var(--border)",
      padding: 8,
    }}>
      <svg viewBox="0 0 600 200" style={{ width: "100%", height: 220 }}>
        <defs>
          <marker id="arrowhead-doc" markerWidth="10" markerHeight="8" refX="8" refY="4" orient="auto">
            <polygon points="0 0, 10 4, 0 8" fill="var(--text-3)" />
          </marker>
        </defs>
        {nodes.slice(0, -1).map((n, idx) => {
          const next = nodes[idx + 1];
          return (
            <line
              key={`${n.id}-${next.id}`}
              x1={n.x + nw} y1={n.y + nh / 2}
              x2={next.x}   y2={next.y + nh / 2}
              stroke="var(--text-3)" strokeWidth="1.5"
              markerEnd="url(#arrowhead-doc)"
            />
          );
        })}
        {nodes.map((n) => {
          const isSel = selected === n.id;
          return (
            <g key={n.id}>
              <rect
                x={n.x} y={n.y} width={nw} height={nh} rx="6"
                fill="var(--bg)"
                stroke={isSel ? "var(--accent)" : "var(--border)"}
                strokeWidth={isSel ? "2" : "1"}
              />
              <text
                x={n.x + nw / 2} y={n.y + nh / 2 + 4}
                textAnchor="middle"
                fontFamily="var(--mono)"
                fontSize="13"
                fill="var(--text)"
              >
                {n.label}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

window.GraphCanvasThreeNodesMockup = GraphCanvasThreeNodesMockup;
