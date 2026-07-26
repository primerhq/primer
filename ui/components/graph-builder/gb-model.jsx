/* global GR_stripCoords */
// Graph builder - draft model: reducer, node factories, naming, supersteps.
// Pure logic, no JSX. Every action replaces the whole draft object so undo is
// a snapshot stack (graph-builder.jsx) and React re-renders cheaply.
//
// Wired per ui/graph-builder/WIRING.md §3 and §5.

// ---------------------------------------------------------------------------
// Naming - the fix for "nodes are born broken / named write_0"
// ---------------------------------------------------------------------------

// Slug a human label into a stable node id. Falls back to the kind so an
// empty label still yields something legible.
function GB_slug(label, fallback) {
  const base = String(label || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return base || fallback || "step";
}

// Dedupe an id against the ids already in the draft: `review`, `review_2`, ...
function GB_uniqueId(base, takenIds) {
  const taken = takenIds instanceof Set ? takenIds : new Set(takenIds || []);
  if (!taken.has(base)) return base;
  for (let i = 2; i < 500; i++) {
    const cand = `${base}_${i}`;
    if (!taken.has(cand)) return cand;
  }
  return `${base}_${Date.now()}`;
}

// Default input template for a node fed by `upstreamId`.
function GB_defaultInput(upstreamId) {
  return upstreamId ? `{{ nodes.${upstreamId}.text }}` : "{{ initial_input }}";
}

// ---------------------------------------------------------------------------
// Node factories - always produce a COMPLETE node (WIRING §5)
// ---------------------------------------------------------------------------

// GB_makeNode({kind, label, agentId?, toolId?, graphId?, upstreamId?, takenIds?})
// A node is never created before its purpose + reference are known, so the
// factory can always fill the required fields.
function GB_makeNode(opts) {
  const o = opts || {};
  const kind = o.kind || "agent";
  const label = o.label || GB_defaultLabel(kind, o);
  const id = GB_uniqueId(GB_slug(label, kind), o.takenIds || []);
  const base = { kind, id, description: label };

  if (kind === "agent") {
    return Object.assign(base, {
      agent_id: o.agentId || "",
      input_template: o.inputTemplate || GB_defaultInput(o.upstreamId),
    });
  }
  if (kind === "tool_call") {
    return Object.assign(base, { tool_id: o.toolId || "", arguments: o.args || {} });
  }
  if (kind === "graph") {
    return Object.assign(base, {
      graph_id: o.graphId || "",
      input_template: o.inputTemplate || GB_defaultInput(o.upstreamId),
    });
  }
  if (kind === "begin") return Object.assign(base, { input_schema: o.inputSchema || null });
  if (kind === "end") {
    return Object.assign(base, {
      output_template: o.outputTemplate || GB_defaultInput(o.upstreamId),
    });
  }
  if (kind === "fan_out") return Object.assign(base, { specs: o.specs || [] });
  if (kind === "fan_in") return Object.assign(base, { aggregate_template: o.aggregateTemplate || "" });
  return base;
}

function GB_defaultLabel(kind, o) {
  const oo = o || {};
  if (kind === "agent") return oo.agentId ? `Run ${oo.agentId}` : "New step";
  if (kind === "tool_call") return oo.toolId ? `Call ${String(oo.toolId).split("__").pop()}` : "Run a tool";
  if (kind === "graph") return oo.graphId ? `Use ${oo.graphId}` : "Use another graph";
  if (kind === "begin") return "Start";
  if (kind === "end") return "Finish";
  if (kind === "fan_out") return "Split the work";
  if (kind === "fan_in") return "Merge the results";
  return "Step";
}

// "Split the work in parallel" must create BOTH halves plus the edges into the
// merge - a fan_in with no incoming edge is a persist-time violation (§5).
function GB_makeSplitPair(opts) {
  const o = opts || {};
  const taken = new Set(o.takenIds || []);
  const fanOut = GB_makeNode({ kind: "fan_out", label: o.splitLabel || "Split the work", takenIds: taken });
  taken.add(fanOut.id);
  const worker = GB_makeNode({
    kind: "agent", label: o.workerLabel || "Do one piece", agentId: o.agentId,
    upstreamId: o.upstreamId, takenIds: taken,
  });
  taken.add(worker.id);
  const fanIn = GB_makeNode({ kind: "fan_in", label: o.mergeLabel || "Merge the results", takenIds: taken });
  fanOut.specs = [
    o.spec || { kind: "broadcast", target_node_id: worker.id, count: 3, on_failure: "collect" },
  ];
  if (!fanOut.specs[0].target_node_id) fanOut.specs[0].target_node_id = worker.id;
  return {
    nodes: [fanOut, worker, fanIn],
    edges: [{ kind: "static", from_node: worker.id, to_node: fanIn.id }],
  };
}

// ---------------------------------------------------------------------------
// Reference rewriting on rename (WIRING §3 - RENAME_NODE)
// ---------------------------------------------------------------------------

// Rewrite every structural reference to `oldId`. Template rewriting lives in
// gb-refs.jsx (GB_renameInTemplates) and is applied by the reducer.
function GB_renameStructural(draft, oldId, newId) {
  const d = JSON.parse(JSON.stringify(draft || {}));
  d.nodes = (d.nodes || []).map((n) => {
    const node = n.id === oldId ? { ...n, id: newId } : { ...n };
    if (node.kind === "fan_out" && Array.isArray(node.specs)) {
      node.specs = node.specs.map((s) => {
        const sp = { ...s };
        if (sp.target_node_id === oldId) sp.target_node_id = newId;
        if (Array.isArray(sp.target_node_ids)) {
          sp.target_node_ids = sp.target_node_ids.map((t) => (t === oldId ? newId : t));
        }
        if (sp.source_node_id === oldId) sp.source_node_id = newId;
        return sp;
      });
    }
    return node;
  });
  d.edges = (d.edges || []).map((e) => {
    const edge = { ...e };
    if (edge.from_node === oldId) edge.from_node = newId;
    if (edge.to_node === oldId) edge.to_node = newId;
    if (edge.router) {
      const r = { ...edge.router };
      if (Array.isArray(r.branches)) {
        r.branches = r.branches.map((b) => (b.to_node === oldId ? { ...b, to_node: newId } : b));
      }
      if (r.default_to === oldId) r.default_to = newId;
      edge.router = r;
    }
    return edge;
  });
  if (d.on_max_iterations === oldId) d.on_max_iterations = newId;
  return d;
}

// ---------------------------------------------------------------------------
// Supersteps - Kahn-style layering over static + conditional + implicit edges
// ---------------------------------------------------------------------------

// Every edge the layout should follow, including fan-out spec targets which
// are NOT in draft.edges (WIRING §6.4).
function GB_allLinks(draft) {
  const out = [];
  const ids = new Set((draft.nodes || []).map((n) => n.id));
  for (const e of draft.edges || []) {
    if (!ids.has(e.from_node)) continue;
    if (e.kind === "conditional" && e.router) {
      for (const b of e.router.branches || []) if (ids.has(b.to_node)) out.push([e.from_node, b.to_node]);
      if (e.router.default_to && ids.has(e.router.default_to)) out.push([e.from_node, e.router.default_to]);
    } else if (ids.has(e.to_node)) {
      out.push([e.from_node, e.to_node]);
    }
  }
  for (const n of draft.nodes || []) {
    if (n.kind !== "fan_out") continue;
    for (const s of n.specs || []) {
      if (s.target_node_id && ids.has(s.target_node_id)) out.push([n.id, s.target_node_id]);
      for (const t of s.target_node_ids || []) if (ids.has(t)) out.push([n.id, t]);
    }
  }
  return out;
}

// Layer the graph: [[startIds], [next], ...]. Cycles assign on first visit so
// a loop never hangs the layout.
function GB_supersteps(draft) {
  const nodes = (draft && draft.nodes) || [];
  if (!nodes.length) return [];
  const links = GB_allLinks(draft);
  const indeg = new Map(nodes.map((n) => [n.id, 0]));
  const adj = new Map(nodes.map((n) => [n.id, []]));
  for (const [from, to] of links) {
    adj.get(from).push(to);
    indeg.set(to, (indeg.get(to) || 0) + 1);
  }
  const layers = [];
  const placed = new Set();
  let frontier = nodes.filter((n) => (indeg.get(n.id) || 0) === 0).map((n) => n.id);
  // A pure cycle has no zero-indegree node: seed with the Begin node or the first node.
  if (!frontier.length) {
    const begin = nodes.find((n) => n.kind === "begin");
    frontier = [begin ? begin.id : nodes[0].id];
  }
  while (frontier.length) {
    const layer = frontier.filter((id) => !placed.has(id));
    if (!layer.length) break;
    layer.forEach((id) => placed.add(id));
    layers.push(layer);
    const next = [];
    for (const id of layer) {
      for (const to of adj.get(id) || []) {
        if (placed.has(to)) continue; // cycle back-edge: already layered
        indeg.set(to, (indeg.get(to) || 0) - 1);
        if ((indeg.get(to) || 0) <= 0 && !next.includes(to)) next.push(to);
      }
    }
    frontier = next;
  }
  // Anything unreachable (or only reachable on a later loop pass) trails last.
  const orphans = nodes.filter((n) => !placed.has(n.id)).map((n) => n.id);
  if (orphans.length) layers.push(orphans);
  return layers;
}

// Transitive predecessors of nodeId - what a step can reference (§7).
function GB_predecessors(draft, nodeId) {
  const links = GB_allLinks(draft);
  const back = new Map();
  for (const [from, to] of links) {
    if (!back.has(to)) back.set(to, []);
    back.get(to).push(from);
  }
  const seen = new Set();
  const stack = [...(back.get(nodeId) || [])];
  while (stack.length) {
    const id = stack.pop();
    if (seen.has(id) || id === nodeId) continue;
    seen.add(id);
    for (const p of back.get(id) || []) stack.push(p);
  }
  return seen;
}

// ---------------------------------------------------------------------------
// Reducer
// ---------------------------------------------------------------------------

function GB_reducer(draft, action) {
  const a = action || {};
  const d = draft || { nodes: [], edges: [] };
  switch (a.type) {
    case "SET_DRAFT":
      return a.draft;
    case "SET_GRAPH":
      return { ...d, ...a.patch };
    case "ADD_NODE":
      return { ...d, nodes: [...(d.nodes || []), a.node] };
    case "ADD_NODES":
      return {
        ...d,
        nodes: [...(d.nodes || []), ...(a.nodes || [])],
        edges: [...(d.edges || []), ...(a.edges || [])],
      };
    case "UPDATE_NODE":
      return {
        ...d,
        nodes: (d.nodes || []).map((n) => (n.id === a.id ? { ...n, ...a.patch } : n)),
      };
    case "DELETE_NODE": {
      const nodes = (d.nodes || []).filter((n) => n.id !== a.id);
      const edges = (d.edges || []).filter(
        (e) => e.from_node !== a.id && e.to_node !== a.id,
      ).map((e) => {
        if (!e.router) return e;
        const r = { ...e.router };
        if (Array.isArray(r.branches)) r.branches = r.branches.filter((b) => b.to_node !== a.id);
        if (r.default_to === a.id) r.default_to = null;
        return { ...e, router: r };
      });
      // Drop the deleted node from any fan-out spec so the draft stays valid.
      const cleaned = nodes.map((n) => {
        if (n.kind !== "fan_out" || !Array.isArray(n.specs)) return n;
        return {
          ...n,
          specs: n.specs.map((s) => {
            const sp = { ...s };
            if (sp.target_node_id === a.id) sp.target_node_id = "";
            if (Array.isArray(sp.target_node_ids)) {
              sp.target_node_ids = sp.target_node_ids.filter((t) => t !== a.id);
            }
            if (sp.source_node_id === a.id) sp.source_node_id = "";
            return sp;
          }),
        };
      });
      const out = { ...d, nodes: cleaned, edges };
      if (out.on_max_iterations === a.id) out.on_max_iterations = null;
      return out;
    }
    case "RENAME_NODE": {
      // The headline behaviour: renaming rewrites EVERY reference, including
      // Jinja templates, so the human label can be primary (WIRING §3).
      let next = d;
      if (a.newId && a.newId !== a.id) {
        next = GB_renameStructural(next, a.id, a.newId);
        if (window.GB_renameInTemplates) next = window.GB_renameInTemplates(next, a.id, a.newId);
      }
      if (a.newDescription !== undefined) {
        next = {
          ...next,
          nodes: (next.nodes || []).map((n) =>
            n.id === (a.newId || a.id) ? { ...n, description: a.newDescription } : n),
        };
      }
      return next;
    }
    case "MOVE_NODE":
      return {
        ...d,
        nodes: (d.nodes || []).map((n) => (n.id === a.id ? { ...n, x: a.x, y: a.y } : n)),
      };
    case "ADD_EDGE":
      return { ...d, edges: [...(d.edges || []), a.edge] };
    case "UPDATE_EDGE":
      return {
        ...d,
        edges: (d.edges || []).map((e, i) => (i === a.idx ? { ...e, ...a.patch } : e)),
      };
    case "DELETE_EDGE":
      return { ...d, edges: (d.edges || []).filter((_e, i) => i !== a.idx) };
    case "ADD_FANOUT_SPEC":
      return {
        ...d,
        nodes: (d.nodes || []).map((n) =>
          n.id === a.id ? { ...n, specs: [...(n.specs || []), a.spec] } : n),
      };
    case "UPDATE_FANOUT_SPEC":
      return {
        ...d,
        nodes: (d.nodes || []).map((n) =>
          n.id === a.id
            ? { ...n, specs: (n.specs || []).map((s, i) => (i === a.i ? { ...s, ...a.patch } : s)) }
            : n),
      };
    case "DELETE_FANOUT_SPEC":
      return {
        ...d,
        nodes: (d.nodes || []).map((n) =>
          n.id === a.id ? { ...n, specs: (n.specs || []).filter((_s, i) => i !== a.i) } : n),
      };
    case "ADD_BRANCH":
      return GB_patchRouter(d, a.idx, (r) => ({ ...r, branches: [...(r.branches || []), a.branch] }));
    case "UPDATE_BRANCH":
      return GB_patchRouter(d, a.idx, (r) => ({
        ...r,
        branches: (r.branches || []).map((b, i) => (i === a.bi ? { ...b, ...a.patch } : b)),
      }));
    case "DELETE_BRANCH":
      return GB_patchRouter(d, a.idx, (r) => ({
        ...r, branches: (r.branches || []).filter((_b, i) => i !== a.bi),
      }));
    case "AUTO_LAYOUT": {
      // Reuse the shipped layout helper; the caller bumps layoutNonce so the
      // canvas re-seeds (an x/y-only change is otherwise invisible to it).
      const layout = window.primerVendor && window.primerVendor.autoLayout;
      if (!layout) return d;
      try {
        // Spacing is DERIVED from the real card size rather than left at the
        // helper's 200px default, which predates the two-line cards: at 196px
        // wide that default leaves a 4px gutter and the steps visibly collide.
        const size = (window.GR_NODE_SIZE && window.GR_NODE_SIZE.agent) || { w: 196, h: 64 };
        const laid = layout(d, { colWidth: size.w + 64, rowHeight: size.h + 40 });
        return laid && Array.isArray(laid.nodes) ? laid : d;
      } catch (_e) {
        return d;
      }
    }
    case "APPLY_TEMPLATE":
    case "IMPORT_SPEC":
      return { ...d, ...a.spec, id: d.id };
    default:
      return d;
  }
}

function GB_patchRouter(d, idx, fn) {
  return {
    ...d,
    edges: (d.edges || []).map((e, i) =>
      (i === idx ? { ...e, router: fn(e.router || { kind: "json_path", branches: [] }) } : e)),
  };
}

// Strip UI-only coordinates from every node so a pure drag never marks dirty.
function GB_stripAll(d) {
  const strip = window.GR_stripCoords || ((n) => { const { x, y, ...rest } = n; return rest; });
  return { ...(d || {}), nodes: ((d && d.nodes) || []).map(strip) };
}

// The builder body fills the viewport rather than sitting in a fixed slab.
// The subtraction covers the app chrome + page title + status panel + the
// builder's own top bar; minHeight keeps it usable on short screens, where the
// page simply scrolls as normal.
const GB_BODY_STYLE = { flex: 1, minHeight: 520, height: "calc(100vh - 300px)" };

Object.assign(window, {
  GB_BODY_STYLE,
  GB_slug, GB_uniqueId, GB_defaultInput, GB_makeNode, GB_makeSplitPair,
  GB_renameStructural, GB_allLinks, GB_supersteps, GB_predecessors,
  GB_reducer, GB_stripAll,
});
