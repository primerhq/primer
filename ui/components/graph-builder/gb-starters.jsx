/* global React */
// GB_Starters - the six shapes from the brief, as literal Graph documents that
// are runnable the moment agents are chosen. WIRING.md §11.
//
// Each spec uses `__agent__`/`__tool__` placeholders in agent_id/tool_id; the
// picker form swaps them for real ids before the graph is created.

const GB_STARTERS = [
  {
    shape: "linear",
    title: "One step after another",
    blurb: "Research -> draft -> publish. The 80% case.",
    slots: [{ key: "researcher", kind: "agent", label: "Researcher" }, { key: "writer", kind: "agent", label: "Writer" }],
    spec: {
      nodes: [
        { kind: "begin", id: "start", description: "Start" },
        { kind: "agent", id: "research", description: "Research the topic", agent_id: "{{researcher}}", input_template: "{{ initial_input }}" },
        { kind: "agent", id: "draft", description: "Draft the result", agent_id: "{{writer}}", input_template: "Using these findings:\n{{ nodes.research.text }}" },
        { kind: "end", id: "finish", description: "Finish", output_template: "{{ nodes.draft.text }}" },
      ],
      edges: [
        { kind: "static", from_node: "start", to_node: "research" },
        { kind: "static", from_node: "research", to_node: "draft" },
        { kind: "static", from_node: "draft", to_node: "finish" },
      ],
    },
  },
  {
    shape: "loop",
    title: "Draft, review, repeat",
    blurb: "A bounded loop with a graceful landing step.",
    slots: [{ key: "writer", kind: "agent", label: "Writer" }, { key: "editor", kind: "agent", label: "Reviewer" }],
    spec: {
      max_iterations: 3,
      on_max_iterations: "wrapup",
      nodes: [
        { kind: "begin", id: "start", description: "Start" },
        { kind: "agent", id: "draft", description: "Draft the post", agent_id: "{{writer}}", input_template: "{{ initial_input }}" },
        {
          kind: "agent", id: "review", description: "Review the draft", agent_id: "{{editor}}",
          input_template: "Review this draft:\n{{ nodes.draft.text }}",
          response_format: {
            type: "object",
            properties: { approved: { type: "boolean" }, notes: { type: "string" } },
            required: ["approved"], additionalProperties: false,
          },
        },
        { kind: "agent", id: "wrapup", description: "Wrap up with notes", agent_id: "{{editor}}", input_template: "Summarise the outstanding notes:\n{{ nodes.review.text }}" },
        { kind: "end", id: "finish", description: "Finish", output_template: "{{ nodes.draft.text }}" },
      ],
      edges: [
        { kind: "static", from_node: "start", to_node: "draft" },
        { kind: "static", from_node: "draft", to_node: "review" },
        {
          kind: "conditional", from_node: "review",
          router: {
            kind: "json_path",
            branches: [{ conditions: [{ path: "approved", op: "eq", value: true }], to_node: "finish" }],
            default_to: "draft",
          },
        },
        { kind: "static", from_node: "wrapup", to_node: "finish" },
      ],
    },
  },
  {
    shape: "broadcast",
    title: "Many at once, then merge",
    blurb: "N copies of one step, joined by a merge.",
    slots: [{ key: "worker", kind: "agent", label: "Worker" }, { key: "merger", kind: "agent", label: "Merger" }],
    spec: {
      nodes: [
        { kind: "begin", id: "start", description: "Start" },
        { kind: "fan_out", id: "split", description: "Split the work", specs: [{ kind: "broadcast", target_node_id: "worker", count: 3, on_failure: "collect" }] },
        { kind: "agent", id: "worker", description: "Do one piece", agent_id: "{{worker}}", input_template: "{{ initial_input }}" },
        { kind: "fan_in", id: "merge", description: "Merge the results", aggregate_template: "{% for o in nodes.worker %}{{ o.text }}\n{% endfor %}" },
        { kind: "agent", id: "polish", description: "Polish the merged result", agent_id: "{{merger}}", input_template: "{{ nodes.merge.text }}" },
        { kind: "end", id: "finish", description: "Finish", output_template: "{{ nodes.polish.text }}" },
      ],
      edges: [
        { kind: "static", from_node: "start", to_node: "split" },
        { kind: "static", from_node: "worker", to_node: "merge" },
        { kind: "static", from_node: "merge", to_node: "polish" },
        { kind: "static", from_node: "polish", to_node: "finish" },
      ],
    },
  },
  {
    shape: "map",
    title: "One per item in a list",
    blurb: "A step returns a list; each item gets its own worker.",
    slots: [{ key: "planner", kind: "agent", label: "Planner" }, { key: "worker", kind: "agent", label: "Worker" }],
    spec: {
      nodes: [
        { kind: "begin", id: "start", description: "Start" },
        {
          kind: "agent", id: "plan", description: "Split into sections", agent_id: "{{planner}}",
          input_template: "{{ initial_input }}",
          response_format: {
            type: "object",
            properties: { items: { type: "array", items: { type: "string" } } },
            required: ["items"], additionalProperties: false,
          },
        },
        { kind: "fan_out", id: "split", description: "One worker per item", specs: [{ kind: "map", target_node_id: "worker", source_node_id: "plan", source_path: "items", on_failure: "collect" }] },
        { kind: "agent", id: "worker", description: "Handle one item", agent_id: "{{worker}}", input_template: "{{ initial_input }}" },
        { kind: "fan_in", id: "merge", description: "Stitch it together", aggregate_template: "{% for o in nodes.worker %}{{ o.text }}\n{% endfor %}" },
        { kind: "end", id: "finish", description: "Finish", output_template: "{{ nodes.merge.text }}" },
      ],
      edges: [
        { kind: "static", from_node: "start", to_node: "plan" },
        { kind: "static", from_node: "plan", to_node: "split" },
        { kind: "static", from_node: "worker", to_node: "merge" },
        { kind: "static", from_node: "merge", to_node: "finish" },
      ],
    },
  },
  {
    shape: "classify",
    title: "Sort it, then route it",
    blurb: "Classify the input and send it to the right specialist.",
    slots: [
      { key: "classifier", kind: "agent", label: "Classifier" },
      { key: "specialist_a", kind: "agent", label: "Specialist A" },
      { key: "specialist_b", kind: "agent", label: "Specialist B" },
    ],
    spec: {
      nodes: [
        { kind: "begin", id: "start", description: "Start" },
        {
          kind: "agent", id: "classify", description: "Classify the request", agent_id: "{{classifier}}",
          input_template: "{{ initial_input }}",
          response_format: {
            type: "object",
            properties: { category: { type: "string" } },
            required: ["category"], additionalProperties: false,
          },
        },
        { kind: "agent", id: "path_a", description: "Handle category A", agent_id: "{{specialist_a}}", input_template: "{{ initial_input }}" },
        { kind: "agent", id: "path_b", description: "Handle anything else", agent_id: "{{specialist_b}}", input_template: "{{ initial_input }}" },
        { kind: "end", id: "finish", description: "Finish", output_template: "{{ nodes.path_a.text }}{{ nodes.path_b.text }}" },
      ],
      edges: [
        { kind: "static", from_node: "start", to_node: "classify" },
        {
          kind: "conditional", from_node: "classify",
          router: {
            kind: "json_path",
            branches: [{ conditions: [{ path: "category", op: "eq", value: "a" }], to_node: "path_a" }],
            default_to: "path_b",
          },
        },
        { kind: "static", from_node: "path_a", to_node: "finish" },
        { kind: "static", from_node: "path_b", to_node: "finish" },
      ],
    },
  },
  {
    shape: "tools",
    title: "Tools around a model",
    blurb: "Fetch, summarise, write the result back.",
    slots: [
      { key: "fetch", kind: "tool", label: "Fetch tool" },
      { key: "summariser", kind: "agent", label: "Summariser" },
      { key: "write", kind: "tool", label: "Write tool" },
    ],
    spec: {
      nodes: [
        { kind: "begin", id: "start", description: "Start" },
        { kind: "tool_call", id: "fetch", description: "Fetch the source", tool_id: "{{fetch}}", arguments: {} },
        { kind: "agent", id: "summarise", description: "Summarise it", agent_id: "{{summariser}}", input_template: "{{ nodes.fetch.text }}" },
        { kind: "tool_call", id: "write", description: "Write the result", tool_id: "{{write}}", arguments: { content: "{{ nodes.summarise.text }}" } },
        { kind: "end", id: "finish", description: "Finish", output_template: "{{ nodes.summarise.text }}" },
      ],
      edges: [
        { kind: "static", from_node: "start", to_node: "fetch" },
        { kind: "static", from_node: "fetch", to_node: "summarise" },
        { kind: "static", from_node: "summarise", to_node: "write" },
        { kind: "static", from_node: "write", to_node: "finish" },
      ],
    },
  },
];

// Swap {{slot}} placeholders for the chosen agent/tool ids.
function GB_fillStarter(spec, picks) {
  const json = JSON.stringify(spec).replace(/\{\{(\w+)\}\}/g, (m, key) => (picks[key] || m));
  return JSON.parse(json);
}

function GB_Starters({ onApply, onBlank, tools }) {
  const { useState } = React;
  const [chosen, setChosen] = useState(null);
  const [picks, setPicks] = useState({});
  const EntityPickerC = window.EntityPicker;

  if (chosen) {
    const ready = chosen.slots.every((s) => picks[s.key]);
    return (
      <div className="col" style={{ gap: 16, padding: "24px 28px", overflow: "auto" }} data-testid="gb-starters">
        <div className="col" style={{ gap: 4 }}>
          <div style={{ fontSize: 17, fontWeight: 600 }}>{chosen.title}</div>
          <div className="muted" style={{ fontSize: "var(--fs-12)" }}>{chosen.blurb} Choose who does the work - everything else is wired already.</div>
        </div>
        <div className="col" style={{ gap: 12, maxWidth: 460 }}>
          {chosen.slots.map((s) => (
            <div key={s.key} className="col" style={{ gap: 6 }}>
              <span style={{ fontSize: 10.5, fontWeight: 600, letterSpacing: ".07em", color: "var(--text-3)", textTransform: "uppercase" }}>{s.label}</span>
              {s.kind === "agent" && EntityPickerC ? (
                <EntityPickerC path="/agents" value={picks[s.key] || ""} onChange={(v) => setPicks({ ...picks, [s.key]: v })} placeholder="Search agents…" />
              ) : (
                <window.GB_ToolPicker tools={tools} value={picks[s.key] || ""} onChange={(v) => setPicks({ ...picks, [s.key]: v })} />
              )}
            </div>
          ))}
        </div>
        <div className="row" style={{ gap: 10 }}>
          <button
            type="button"
            disabled={!ready}
            onClick={() => onApply(GB_fillStarter(chosen.spec, picks), chosen)}
            style={{
              padding: "8px 14px", borderRadius: 9, border: "none", fontSize: "var(--fs-12)", fontWeight: 600,
              cursor: ready ? "pointer" : "not-allowed",
              background: ready ? "var(--accent)" : "var(--bg-2)", color: ready ? "var(--accent-fg)" : "var(--text-4)",
            }}
          >
            Create this graph
          </button>
          <button
            type="button"
            onClick={() => { setChosen(null); setPicks({}); }}
            style={{ padding: "8px 14px", borderRadius: 9, background: "var(--bg-2)", border: "1px solid var(--border)", color: "var(--text-2)", fontSize: "var(--fs-12)", cursor: "pointer" }}
          >
            Back
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="col" style={{ gap: 20, padding: "28px 32px", overflow: "auto" }} data-testid="gb-starters">
      <div className="col" style={{ gap: 5 }}>
        <div style={{ fontSize: 19, fontWeight: 600 }}>Start from a shape</div>
        <div className="muted" style={{ fontSize: "var(--fs-12)" }}>
          Six shapes cover almost everything people build. Each one arrives complete and runnable - swap in your agents and go.
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 14 }}>
        {GB_STARTERS.map((s) => (
          <div
            key={s.shape}
            data-testid="gb-starter"
            data-shape={s.shape}
            onClick={() => setChosen(s)}
            className="col"
            style={{
              gap: 10, padding: 16, background: "var(--bg-elev)", border: "1px solid var(--border)",
              borderRadius: 12, cursor: "pointer",
            }}
          >
            <div style={{ fontSize: "var(--fs-13)", fontWeight: 600 }}>{s.title}</div>
            <div className="muted" style={{ fontSize: "var(--fs-11)" }}>{s.blurb}</div>
            <div className="mono" style={{ fontSize: 10, color: "var(--text-4)" }}>
              {s.spec.nodes.length} steps{s.spec.max_iterations ? ` · loops ≤${s.spec.max_iterations}` : ""}
            </div>
          </div>
        ))}
      </div>
      <div className="row" style={{ gap: 10, alignItems: "center" }}>
        <button
          type="button"
          onClick={onBlank}
          style={{ padding: "8px 14px", borderRadius: 9, background: "var(--accent)", border: "none", color: "var(--accent-fg)", fontSize: "var(--fs-12)", fontWeight: 600, cursor: "pointer" }}
        >
          Start blank instead
        </button>
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>or paste a graph JSON - import stays exactly where it was.</span>
      </div>
    </div>
  );
}

Object.assign(window, { GB_Starters, GB_STARTERS, GB_fillStarter });
