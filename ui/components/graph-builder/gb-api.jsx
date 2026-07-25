// Graph builder - the only file that names a URL. WIRING.md §2.
// Every function takes an optional AbortSignal so useResource can cancel.

const GB_api = (() => {
  const api = () => window.primerApi;

  const qs = (params) => {
    const p = [];
    for (const k of Object.keys(params || {})) {
      const v = params[k];
      if (v === undefined || v === null || v === "") continue;
      p.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
    }
    return p.length ? `?${p.join("&")}` : "";
  };

  return {
    getGraph: (id, signal) => api().apiFetch("GET", `/graphs/${encodeURIComponent(id)}`, null, { signal }),
    putGraph: (id, body) => api().apiFetch("PUT", `/graphs/${encodeURIComponent(id)}`, body),
    createGraph: (body) => api().apiFetch("POST", "/graphs", body),
    deleteGraph: (id) => api().apiFetch("DELETE", `/graphs/${encodeURIComponent(id)}`),
    listGraphs: (params, signal) => api().apiFetch("GET", `/graphs${qs(params)}`, null, { signal }),
    graphStatus: (id, signal) => api().apiFetch("GET", `/graphs/${encodeURIComponent(id)}/status`, null, { signal }),
    listAgents: (params, signal) => api().apiFetch("GET", `/agents${qs(params)}`, null, { signal }),
    agentStatus: (id, signal) => api().apiFetch("GET", `/agents/${encodeURIComponent(id)}/status`, null, { signal }),
    toolCatalogue: (signal) => api().apiFetch("GET", "/tools/catalogue", null, { signal }),
    nodeStates: (graphId, runId, signal) =>
      api().apiFetch("GET", `/graphs/${encodeURIComponent(graphId)}/runs/${encodeURIComponent(runId)}/node_states`, null, { signal }),
    graphTurnLog: (graphId, runId, params, signal) =>
      api().apiFetch("GET", `/graphs/${encodeURIComponent(graphId)}/runs/${encodeURIComponent(runId)}/turn_log${qs(params)}`, null, { signal }),
    nodeTurnLog: (graphId, runId, nodeId, params, signal) =>
      api().apiFetch("GET", `/graphs/${encodeURIComponent(graphId)}/runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeId)}/turn_log${qs(params)}`, null, { signal }),

    // Start a run - mirrors what new-session-form builds for a graph binding.
    startRun: ({ graphId, graphInput, workspaceId }) => api().apiFetch("POST", "/sessions", {
      binding: { kind: "graph", graph_id: graphId },
      auto_start: true,
      ...(workspaceId ? { workspace_id: workspaceId } : {}),
      ...(graphInput === undefined ? {} : { graph_input: graphInput }),
    }),

    // ---- Endpoints that do not exist yet (WIRING §2.1) --------------------
    // Both degrade to a local, static-only answer so the UI ships first.

    // A. Dry run / template render. Falls back to local template rendering.
    dryRun: async (graphId, draft, input) => {
      try {
        return await api().apiFetch("POST", `/graphs/${encodeURIComponent(graphId)}/dry_run`, { graph: draft, input });
      } catch (err) {
        if (err && (err.status === 404 || err.status === 405)) return GB_localDryRun(draft, input);
        throw err;
      }
    },

    // B. Sample values for the reference picker. Falls back to schema-derived
    // types/examples (handled by the caller via GB_availableRefs).
    nodeOutputs: async (graphId, runId, nodeId, signal) => {
      try {
        return await api().apiFetch(
          "GET",
          `/graphs/${encodeURIComponent(graphId)}/runs/${encodeURIComponent(runId)}/node_outputs${qs({ node_id: nodeId })}`,
          null, { signal },
        );
      } catch (err) {
        if (err && (err.status === 404 || err.status === 405)) return { items: [], fallback: true };
        throw err;
      }
    },
  };
})();

// Local dry run: resolve what we can without executing anything. Reports the
// rendered input per node (literal text + resolved chips it can answer) and
// reuses the client-side validation for blockers.
function GB_localDryRun(draft, input) {
  const d = draft || {};
  const layers = window.GB_supersteps ? window.GB_supersteps(d) : [];
  const stepOf = new Map();
  layers.forEach((layer, i) => layer.forEach((id) => stepOf.set(id, i + 1)));

  const sample = (expr) => {
    if (expr === "initial_input") return typeof input === "string" ? input : JSON.stringify(input);
    if (expr.startsWith("initial_input.")) {
      const path = expr.slice("initial_input.".length).split(".");
      let cur = input;
      for (const p of path) cur = cur == null ? cur : cur[p];
      return cur == null ? null : String(cur);
    }
    return null;
  };

  const nodes = [];
  for (const n of d.nodes || []) {
    const fields = (window.GB_TEMPLATE_FIELDS || {})[n.kind] || [];
    if (!fields.length) continue;
    const templateErrors = [];
    let rendered = "";
    for (const f of fields) {
      for (const tk of (window.GB_parseTemplate ? window.GB_parseTemplate(n[f] || "") : [])) {
        if (tk.t === "text") { rendered += tk.v; continue; }
        if (tk.t === "raw") { rendered += `«${tk.v}»`; continue; }
        if (window.GB_refIsBroken && window.GB_refIsBroken(d, tk)) {
          templateErrors.push(`refers to a deleted step (${tk.nodeId})`);
          rendered += `«missing: ${tk.v}»`;
          continue;
        }
        const s = sample(tk.v);
        rendered += s == null
          ? `«${window.GB_chipLabel ? window.GB_chipLabel(d, tk) : tk.v}»`
          : s;
      }
    }
    nodes.push({
      node_id: n.id, rendered_input: rendered,
      template_errors: templateErrors, superstep: stepOf.get(n.id) || null,
    });
  }

  const v = window.GB_validate ? window.GB_validate(d, {}) : { blocking: [], runnable: [] };
  const blockers = [...v.blocking, ...v.runnable].map((r) => ({
    code: r.code, node_id: r.nodeId || null, message: r.message, fix: r.fix || null,
  }));

  return {
    ok: blockers.length === 0,
    nodes,
    blockers,
    local: true, // the drawer renders "static check only" when this is set
    shape: {
      longest_path: layers.length,
      parallel_groups: layers.filter((l) => l.length > 1).length,
      loops: window.GB_hasCycle && window.GB_hasCycle(d) ? 1 : 0,
      human_pauses: (d.nodes || []).filter((n) => n.kind === "tool_call").length,
    },
  };
}

Object.assign(window, { GB_api, GB_localDryRun });
