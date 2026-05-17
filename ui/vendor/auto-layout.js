// Graph auto-layout: simple BFS-layered top-to-bottom-ish layout.
// Pure function: takes the editor's draft (a Graph-shaped object
// with `nodes`, `edges`, `entry_node_id`) plus optional spacing
// hints; returns a new draft with `nodes[i].x` / `.y` populated.
//
// First-party code; no upstream. The full sugiyama crossing-reduction
// pass is deferred — for v1 we just place nodes in BFS layers (one
// column per depth) which gives a usable visual for the typical
// shallow graph the operator hand-edits. Cyclic graphs are handled
// by capping depth and laying out only the BFS-reachable subgraph;
// unreachable nodes are placed in an extra trailing column.

(function () {
  // autoLayout(draft, opts) -> new draft
  //   draft.nodes : array of { id, kind, x?, y?, ... }
  //   draft.edges : array of { kind, from_node, to_node, ... }  (router
  //     edges may have multiple targets; we follow `branch.to_node` and
  //     `default_to` from json_path routers)
  //   draft.entry_node_id : string (BFS root). If missing or invalid,
  //     the first node id is used as the root.
  //   opts.colWidth : px (default 200)
  //   opts.rowHeight: px (default 88)
  //   opts.marginX  : px (default 30)
  //   opts.marginY  : px (default 30)
  function autoLayout(draft, opts) {
    opts = opts || {};
    const COL = opts.colWidth || 200;
    const ROW = opts.rowHeight || 88;
    const MX = opts.marginX || 30;
    const MY = opts.marginY || 30;

    if (!draft || !Array.isArray(draft.nodes) || draft.nodes.length === 0) {
      return draft;
    }

    // Build adjacency: from_id -> [to_id]
    const adj = {};
    for (const n of draft.nodes) adj[n.id] = [];
    for (const e of (draft.edges || [])) {
      const from = e.from_node;
      if (!from || !(from in adj)) continue;
      if (e.kind === "static") {
        if (e.to_node) adj[from].push(e.to_node);
      } else if (e.kind === "conditional") {
        const r = e.router || {};
        if (r.kind === "json_path") {
          for (const br of (r.branches || [])) if (br.to_node) adj[from].push(br.to_node);
          if (r.default_to) adj[from].push(r.default_to);
        }
        // _CallableRouter targets are resolved at runtime; we cannot
        // statically lay them out.
      }
    }

    // Pick root: entry_node_id if present and known, else first node.
    const entry = draft.entry_node_id;
    const root = (entry && adj[entry]) ? entry : draft.nodes[0].id;

    // BFS to assign each reachable node a depth (column index).
    const depth = {};
    depth[root] = 0;
    const queue = [root];
    while (queue.length) {
      const cur = queue.shift();
      for (const next of adj[cur]) {
        if (!(next in depth)) {
          depth[next] = depth[cur] + 1;
          queue.push(next);
        }
      }
    }

    // Unreachable nodes go into one extra trailing column.
    let maxDepth = 0;
    for (const id in depth) if (depth[id] > maxDepth) maxDepth = depth[id];
    const unreachableDepth = maxDepth + 1;
    for (const n of draft.nodes) {
      if (!(n.id in depth)) depth[n.id] = unreachableDepth;
    }

    // Group node ids by depth.
    const columns = {};
    for (const n of draft.nodes) {
      const d = depth[n.id];
      (columns[d] = columns[d] || []).push(n.id);
    }

    // Stable order within each column: input ordering of draft.nodes.
    const order = {};
    draft.nodes.forEach((n, i) => { order[n.id] = i; });
    for (const d in columns) columns[d].sort((a, b) => order[a] - order[b]);

    // Assign coordinates.
    const newNodes = draft.nodes.map((n) => {
      const d = depth[n.id];
      const col = columns[d];
      const row = col.indexOf(n.id);
      // Vertical-center each column so it looks balanced.
      const colHeight = col.length * ROW;
      const yOffset = MY + ((Math.max(0, maxDepth + 1) * ROW - colHeight) / 2);
      return {
        ...n,
        x: MX + d * COL,
        y: yOffset + row * ROW,
      };
    });

    return { ...draft, nodes: newNodes };
  }

  if (typeof window !== "undefined") {
    window.matrixVendor = window.matrixVendor || {};
    window.matrixVendor.autoLayout = autoLayout;
  }
})();
