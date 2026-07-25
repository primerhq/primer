/* global React, EntityPicker, GB_makeNode, GB_makeSplitPair, GB_KindDot */
// GB_AddStepPalette - purpose-first creation (⌘K). The fix for "nodes are born
// broken": the palette collects the purpose AND the reference before anything
// is created, so the node factory is always called with a complete node.
// WIRING.md §5.

const GB_PURPOSES = [
  {
    id: "agent", group: "Do work", kind: "agent",
    title: "Ask an agent to do something",
    hint: "Writes, reviews, decides - anything that needs judgement",
  },
  {
    id: "tool", group: "Do work", kind: "tool_call",
    title: "Run a tool directly",
    hint: "Fetch, write a file, call an API - no model, no cost",
  },
  {
    id: "subgraph", group: "Do work", kind: "graph",
    title: "Use another graph",
    hint: "Reuse a pipeline you already built",
  },
  {
    id: "split", group: "Change the shape", kind: "fan_out",
    title: "Split the work in parallel",
    hint: "Per item in a list · N copies · several steps at once",
    note: "adds the merge step too",
  },
  {
    id: "branch", group: "Change the shape", kind: "__branch",
    title: "Choose between paths",
    hint: "Send the run one way or another based on a result",
  },
  {
    id: "end", group: "Change the shape", kind: "end",
    title: "Finish and return something",
    hint: "A graph can have several finishes",
  },
];

function GB_AddStepPalette(props) {
  const { draft, afterNodeId, tools, onClose, onCreate, onAddBranch } = props;
  const { useState, useMemo } = React;
  const [query, setQuery] = useState("");
  const [stage, setStage] = useState("purpose"); // purpose -> reference
  const [purpose, setPurpose] = useState(null);
  const [cursor, setCursor] = useState(0);

  const afterNode = (draft.nodes || []).find((n) => n.id === afterNodeId);
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return GB_PURPOSES;
    return GB_PURPOSES.filter((p) => (p.title + " " + p.hint).toLowerCase().includes(q));
  }, [query]);

  const choose = (p) => {
    if (p.kind === "__branch") { onAddBranch(afterNodeId); onClose(); return; }
    if (p.kind === "end") {
      onCreate({
        nodes: [GB_makeNode({
          kind: "end", label: "Finish", upstreamId: afterNodeId,
          takenIds: (draft.nodes || []).map((n) => n.id),
        })],
        connectFrom: afterNodeId,
      });
      onClose();
      return;
    }
    setPurpose(p);
    setStage("reference");
  };

  const onKeyDown = (e) => {
    if (e.key === "Escape") { onClose(); return; }
    if (stage !== "purpose") return;
    if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(c + 1, rows.length - 1)); }
    if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); }
    if (e.key === "Enter" && rows[cursor]) { e.preventDefault(); choose(rows[cursor]); }
  };

  const groups = [];
  for (const r of rows) {
    let g = groups.find((x) => x.name === r.group);
    if (!g) { g = { name: r.group, items: [] }; groups.push(g); }
    g.items.push(r);
  }

  return (
    <div
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      style={{
        position: "fixed", inset: 0, background: "rgba(10,11,13,.62)", zIndex: 60,
        display: "flex", alignItems: "flex-start", justifyContent: "center", paddingTop: 104,
      }}
    >
      <div
        data-testid="gb-palette"
        onKeyDown={onKeyDown}
        style={{
          width: 660, maxWidth: "94vw", background: "var(--bg-elev)",
          border: "1px solid var(--border-strong)", borderRadius: 14,
          boxShadow: "0 40px 90px -30px rgba(0,0,0,.95)", overflow: "hidden",
          display: "flex", flexDirection: "column", maxHeight: "72vh",
        }}
      >
        <div className="row" style={{ gap: 10, alignItems: "center", padding: "14px 16px", borderBottom: "1px solid var(--border)" }}>
          {afterNode ? (
            <span style={{ fontSize: "var(--fs-13)", color: "var(--text-3)", flex: "0 0 auto" }}>
              After <span style={{ color: "var(--text-2)" }}>{afterNode.description || afterNode.id}</span> ·
            </span>
          ) : null}
          <input
            data-testid="gb-palette-search"
            autoFocus
            value={query}
            onChange={(e) => { setQuery(e.target.value); setCursor(0); }}
            placeholder={stage === "purpose" ? "What should happen next?" : "Pick the reference…"}
            disabled={stage !== "purpose"}
            style={{
              flex: 1, background: "transparent", border: "none", outline: "none",
              color: "var(--text)", fontSize: "var(--fs-13)",
            }}
          />
          <span className="mono" style={{ fontSize: "var(--fs-11)", color: "var(--text-4)" }}>esc</span>
        </div>

        {stage === "purpose" ? (
          <div className="col" style={{ padding: 6, overflow: "auto" }}>
            {groups.map((g) => (
              <div key={g.name}>
                <div style={{
                  padding: "8px 12px 4px", fontSize: 10, letterSpacing: ".08em",
                  color: "var(--text-4)", textTransform: "uppercase",
                }}
                >
                  {g.name}
                </div>
                {g.items.map((p) => {
                  const idx = rows.indexOf(p);
                  const active = idx === cursor;
                  return (
                    <div
                      key={p.id}
                      data-testid="gb-palette-row"
                      data-purpose={p.id}
                      onMouseEnter={() => setCursor(idx)}
                      onClick={() => choose(p)}
                      className="row"
                      style={{
                        gap: 12, alignItems: "center", padding: "10px 12px", borderRadius: 9, cursor: "pointer",
                        background: active ? "color-mix(in oklab, var(--violet) 12%, transparent)" : "transparent",
                        border: active ? "1px solid color-mix(in oklab, var(--violet) 30%, transparent)" : "1px solid transparent",
                      }}
                    >
                      <GB_KindDot kind={p.kind === "__branch" ? "fan_out" : p.kind} size={30} />
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: "var(--fs-13)", fontWeight: 500 }}>{p.title}</div>
                        <div className="muted" style={{ fontSize: "var(--fs-11)" }}>{p.hint}</div>
                      </div>
                      {p.note ? (
                        <span style={{ marginLeft: "auto", fontSize: "var(--fs-11)", color: "var(--accent)" }}>{p.note}</span>
                      ) : null}
                      {active ? <span className="mono" style={{ marginLeft: p.note ? 8 : "auto", fontSize: 10, color: "var(--violet)" }}>↵</span> : null}
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        ) : (
          <GB_PaletteReference
            purpose={purpose}
            draft={draft}
            tools={tools}
            afterNodeId={afterNodeId}
            onBack={() => { setStage("purpose"); setPurpose(null); }}
            onDone={(payload) => { onCreate(payload); onClose(); }}
          />
        )}

        <div
          className="row"
          style={{
            gap: 10, alignItems: "center", padding: "10px 14px", borderTop: "1px solid var(--border)",
            background: "var(--bg-1)", fontSize: "var(--fs-11)", color: "var(--text-3)",
          }}
        >
          <span>
            {stage === "purpose"
              ? (afterNode
                ? <>Next: pick the agent -> the step is named after it and inherits <span style={{ color: "var(--text-2)" }}>{afterNode.description || afterNode.id}</span>’s output.</>
                : "Next: pick the agent or tool - the step gets a real name.")
              : "Every step is created finished - no empty required fields."}
          </span>
          <span className="mono" style={{ marginLeft: "auto" }}>↑↓ to move · ↵ to choose</span>
        </div>
      </div>
    </div>
  );
}

// Stage 2: collect the reference (agent / tool / graph) so the node is complete.
function GB_PaletteReference({ purpose, draft, tools, afterNodeId, onBack, onDone }) {
  const { useState } = React;
  const [ref, setRef] = useState("");
  const [label, setLabel] = useState("");
  const taken = (draft.nodes || []).map((n) => n.id);

  const commit = () => {
    if (!ref && purpose.kind !== "fan_out") return;
    if (purpose.kind === "fan_out") {
      const pair = GB_makeSplitPair({
        agentId: ref, upstreamId: afterNodeId, takenIds: taken,
        workerLabel: label || undefined,
      });
      onDone({ nodes: pair.nodes, edges: pair.edges, connectFrom: afterNodeId, connectTo: pair.nodes[0].id });
      return;
    }
    const node = GB_makeNode({
      kind: purpose.kind,
      label: label || undefined,
      agentId: purpose.kind === "agent" ? ref : undefined,
      toolId: purpose.kind === "tool_call" ? ref : undefined,
      graphId: purpose.kind === "graph" ? ref : undefined,
      upstreamId: afterNodeId,
      takenIds: taken,
    });
    onDone({ nodes: [node], connectFrom: afterNodeId });
  };

  return (
    <div className="col" style={{ padding: 14, gap: 12, overflow: "auto" }}>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <span style={{ fontSize: "var(--fs-12)", color: "var(--text-2)" }}>{purpose.title}</span>
        <span
          onClick={onBack}
          style={{ marginLeft: "auto", fontSize: "var(--fs-11)", color: "var(--text-3)", cursor: "pointer" }}
        >
          ← back
        </span>
      </div>

      {purpose.kind === "agent" || purpose.kind === "fan_out" ? (
        <EntityPicker
          path="/agents"
          value={ref}
          onChange={setRef}
          placeholder="Search agents…"
          testid="gb-agent-picker"
        />
      ) : null}
      {purpose.kind === "graph" ? (
        <EntityPicker path="/graphs" value={ref} onChange={setRef} placeholder="Search graphs…" testid="gb-graph-picker" />
      ) : null}
      {purpose.kind === "tool_call" ? (
        <GB_ToolPicker tools={tools} value={ref} onChange={setRef} />
      ) : null}

      <label className="col" style={{ gap: 5 }}>
        <span style={{ fontSize: 10.5, fontWeight: 600, letterSpacing: ".07em", color: "var(--text-3)", textTransform: "uppercase" }}>
          Name this step
        </span>
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder={ref ? `e.g. Review with ${String(ref).split("__").pop()}` : "e.g. Review the draft"}
          style={{
            background: "var(--bg-1)", border: "1px solid var(--border)", borderRadius: "var(--r-6)",
            padding: "8px 10px", color: "var(--text)", fontSize: "var(--fs-12)", outline: "none",
          }}
        />
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
          The name is what you'll see everywhere. Leave it blank and we'll name it after the {purpose.kind === "tool_call" ? "tool" : "agent"}.
        </span>
      </label>

      <button
        type="button"
        onClick={commit}
        disabled={!ref}
        style={{
          padding: "9px 14px", borderRadius: 9, border: "none", cursor: ref ? "pointer" : "not-allowed",
          background: ref ? "var(--accent)" : "var(--bg-2)", color: ref ? "var(--accent-fg)" : "var(--text-4)",
          fontWeight: 600, fontSize: "var(--fs-12)",
        }}
      >
        Add step
      </button>
    </div>
  );
}

function GB_ToolPicker({ tools, value, onChange }) {
  const { useState } = React;
  const [q, setQ] = useState("");
  const items = (tools || []).filter((t) => !q || (t.id + " " + (t.description || "")).toLowerCase().includes(q.toLowerCase()));
  return (
    <div data-testid="gb-tool-picker" style={{ border: "1px solid var(--border)", borderRadius: "var(--r-9)", overflow: "hidden" }}>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search tools…"
        style={{
          width: "100%", background: "var(--bg-1)", border: "none", borderBottom: "1px solid var(--border)",
          padding: "8px 10px", color: "var(--text)", fontSize: "var(--fs-12)", outline: "none",
        }}
      />
      <div style={{ maxHeight: 200, overflow: "auto" }}>
        {!items.length ? <div className="muted" style={{ padding: 10, fontSize: "var(--fs-12)" }}>No tools match.</div> : null}
        {items.slice(0, 60).map((t) => (
          <div
            key={t.id}
            onClick={() => onChange(t.id)}
            className="col"
            style={{
              gap: 2, padding: "8px 11px", cursor: "pointer",
              background: t.id === value ? "var(--accent-dim)" : "transparent",
              borderLeft: t.id === value ? "2px solid var(--accent)" : "2px solid transparent",
            }}
          >
            <span className="mono" style={{ fontSize: "var(--fs-11)" }}>{t.id}</span>
            {t.description ? <span className="muted" style={{ fontSize: "var(--fs-11)" }}>{t.description}</span> : null}
          </div>
        ))}
      </div>
    </div>
  );
}

Object.assign(window, { GB_AddStepPalette, GB_PURPOSES, GB_ToolPicker });
