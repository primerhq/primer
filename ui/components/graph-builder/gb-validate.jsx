// Graph builder - validation in the backend's two tiers, plus plain-language
// copy for runtime failure codes. Pure logic, no JSX. WIRING.md §10.
//
// Tier 1 (blocking)  = persist-time referential integrity  -> Save disabled.
// Tier 2 (runnable)  = runnability invariants              -> Save fine, Run disabled.
//
// NOTE: this mirrors primer/model/graph.py (_validate_topology vs
// assert_runnable). GR_localViolations conflates the two (it marks "exactly one
// Begin" and "End reachable" as hard), which would wrongly block Save on a
// legitimate draft - so the tiers are computed here and GR_localViolations is
// used only as an extra source of blocking messages.

const GB_FAILURE_COPY = {
  max_iterations_exceeded: "The loop ran its maximum number of passes and stopped. Give it a landing step so it finishes cleanly.",
  routing_failed: "Nothing matched, and there was no “in any other case” path.",
  template_error: "A step referred to something that wasn't there.",
  tool_execution_failed: "The tool returned an error.",
  fanout_source_invalid: "A step was supposed to produce a list to split on, and didn't.",
  end_output_invalid: "The finish step's output didn't match the fields you asked for.",
  tool_output_invalid: "The tool's result didn't match the fields you asked for.",
  fanin_upstream_failed: "One copy failed and this split was set to “finish, then fail”.",
};

function GB_validate(draft, opts) {
  const d = draft || {};
  const o = opts || {};
  const nodes = d.nodes || [];
  const edges = d.edges || [];
  const blocking = [];
  const runnable = [];
  const warnings = [];
  const ids = new Set(nodes.map((n) => n.id));

  // ---- Tier 1: referential integrity (blocks Save) ------------------------
  const seen = new Map();
  for (const n of nodes) seen.set(n.id, (seen.get(n.id) || 0) + 1);
  for (const [id, count] of seen) {
    if (count > 1) blocking.push({ code: "duplicate_id", message: `Two steps share the id “${id}”.`, nodeId: id });
  }

  const beginIds = new Set(nodes.filter((n) => n.kind === "begin").map((n) => n.id));
  const endIds = new Set(nodes.filter((n) => n.kind === "end").map((n) => n.id));
  const fanoutIds = new Set(nodes.filter((n) => n.kind === "fan_out").map((n) => n.id));

  edges.forEach((e, idx) => {
    if (e.from_node && !ids.has(e.from_node)) {
      blocking.push({ code: "unknown_from", message: `A connection starts at a step that no longer exists (${e.from_node}).`, edgeIdx: idx });
    }
    if (fanoutIds.has(e.from_node)) {
      blocking.push({
        code: "fanout_has_edge", edgeIdx: idx, nodeId: e.from_node,
        message: "A split feeds its copies through its own list, so it can't have an outgoing connection.",
        fix: "select_node",
      });
    }
    const targets = [];
    if (e.kind === "conditional" && e.router) {
      for (const b of e.router.branches || []) targets.push(b.to_node);
      if (e.router.default_to) targets.push(e.router.default_to);
    } else if (e.to_node) {
      targets.push(e.to_node);
    }
    for (const t of targets) {
      if (t && !ids.has(t)) {
        blocking.push({ code: "unknown_target", message: `A connection points at a step that no longer exists (${t}).`, edgeIdx: idx });
      }
    }
    if (beginIds.has(e.to_node)) {
      blocking.push({ code: "begin_incoming", message: "The start step can't have anything pointing into it.", edgeIdx: idx });
    }
    if (endIds.has(e.from_node)) {
      blocking.push({ code: "end_outgoing", message: "A finish step can't lead anywhere else.", edgeIdx: idx });
    }
  });

  if (d.on_max_iterations && !ids.has(d.on_max_iterations)) {
    blocking.push({ code: "unknown_landing", message: "The loop's landing step no longer exists.", fix: "set_max_iterations" });
  }

  const allFanoutTargets = new Set();
  for (const n of nodes) {
    if (n.kind !== "fan_out") continue;
    for (const s of n.specs || []) {
      const targets = s.kind === "tee" ? (s.target_node_ids || []) : (s.target_node_id ? [s.target_node_id] : []);
      if (!targets.length) {
        blocking.push({ code: "fanout_no_target", nodeId: n.id, message: `“${n.description || n.id}” doesn't say who does the work yet.`, fix: "select_node" });
      }
      for (const t of targets) {
        allFanoutTargets.add(t);
        if (!ids.has(t)) blocking.push({ code: "fanout_unknown_target", nodeId: n.id, message: `The split points at a step that no longer exists (${t}).` });
        else if (beginIds.has(t)) blocking.push({ code: "fanout_targets_begin", nodeId: n.id, message: "A split can't target the start step." });
        else if (fanoutIds.has(t)) blocking.push({ code: "fanout_targets_fanout", nodeId: n.id, message: "A split can't target another split." });
      }
      if (s.kind === "map") {
        if (!s.source_node_id || !s.source_path) {
          blocking.push({ code: "map_incomplete", nodeId: n.id, message: `“${n.description || n.id}” needs a list to split on.`, fix: "select_node" });
        } else if (!ids.has(s.source_node_id)) {
          blocking.push({ code: "map_unknown_source", nodeId: n.id, message: "The list's step no longer exists." });
        }
      }
      if (s.kind === "broadcast" && !(s.count > 0)) {
        blocking.push({ code: "broadcast_no_count", nodeId: n.id, message: "Say how many copies to run.", fix: "select_node" });
      }
    }
  }
  // A map source must be deterministic - it cannot itself be fanned out.
  for (const n of nodes) {
    if (n.kind !== "fan_out") continue;
    for (const s of n.specs || []) {
      if (s.kind === "map" && s.source_node_id && allFanoutTargets.has(s.source_node_id)) {
        blocking.push({ code: "map_source_is_fanout_target", nodeId: n.id, message: "The list has to come from a step that runs once." });
      }
    }
  }

  for (const n of nodes) {
    if (n.kind !== "fan_in") continue;
    const hasIncoming = edges.some((e) => e.to_node === n.id
      || (e.router && ((e.router.branches || []).some((b) => b.to_node === n.id) || e.router.default_to === n.id)));
    if (!hasIncoming) {
      blocking.push({ code: "fanin_no_incoming", nodeId: n.id, message: `“${n.description || n.id}” isn't connected to anything yet.` });
    }
  }

  // Missing required references - these block a save-able but broken node.
  for (const n of nodes) {
    if (n.kind === "agent" && !n.agent_id) {
      blocking.push({ code: "agent_missing", nodeId: n.id, message: `“${n.description || n.id}” has no agent chosen.`, fix: "select_node" });
    }
    if (n.kind === "tool_call" && !n.tool_id) {
      blocking.push({ code: "tool_missing", nodeId: n.id, message: `“${n.description || n.id}” has no tool chosen.`, fix: "select_node" });
    }
    if (n.kind === "graph" && !n.graph_id) {
      blocking.push({ code: "graph_missing", nodeId: n.id, message: `“${n.description || n.id}” has no graph chosen.`, fix: "select_node" });
    }
    if (n.kind === "tool_call" && n.tool_id && o.knownToolIds && o.knownToolIds.length
        && o.knownToolIds.indexOf(n.tool_id) === -1) {
      warnings.push({ code: "tool_unknown", nodeId: n.id, message: `“${n.tool_id}” isn't in the tool catalogue right now.` });
    }
  }

  // ---- Tier 2: runnability (blocks Run only) ------------------------------
  if (!nodes.length) {
    runnable.push({ code: "empty", message: "This graph has no steps yet." });
  } else {
    if (beginIds.size !== 1) {
      runnable.push({
        code: "begin_count",
        message: beginIds.size === 0 ? "There's no start step." : `There are ${beginIds.size} start steps - a graph needs exactly one.`,
        fix: beginIds.size === 0 ? "add_begin" : null,
      });
    }
    if (endIds.size < 1) {
      runnable.push({ code: "no_end", message: "There's no finish step.", fix: "add_end" });
    }
    if (beginIds.size === 1 && endIds.size) {
      const links = window.GB_allLinks ? window.GB_allLinks(d) : [];
      const adj = new Map();
      for (const [from, to] of links) {
        if (!adj.has(from)) adj.set(from, []);
        adj.get(from).push(to);
      }
      const beginId = [...beginIds][0];
      const seenR = new Set([beginId]);
      const stack = [beginId];
      while (stack.length) {
        const cur = stack.pop();
        for (const nx of adj.get(cur) || []) if (!seenR.has(nx)) { seenR.add(nx); stack.push(nx); }
      }
      for (const eid of endIds) {
        if (!seenR.has(eid)) {
          const node = nodes.find((n) => n.id === eid);
          runnable.push({
            code: "end_unreachable", nodeId: eid, fix: "connect_end",
            message: `“${(node && node.description) || eid}” can't be reached from the start.`,
          });
        }
      }
    }
    // Loops must be bounded.
    const hasCallable = edges.some((e) => e.router && e.router.kind === "callable");
    if ((GB_hasCycle(d) || hasCallable) && !(d.max_iterations > 0)) {
      runnable.push({
        code: "unbounded_loop", fix: "set_max_iterations",
        message: hasCallable
          ? "Routing is decided by code, so this graph needs a limit on how many passes it can run."
          : "This graph can loop forever. Set how many passes it may run.",
      });
    }
  }

  // UI-only pre-emptive checks that mirror real runtime failures.
  for (let idx = 0; idx < edges.length; idx++) {
    const e = edges[idx];
    if (!e.router || e.router.kind !== "json_path") continue;
    const src = nodes.find((n) => n.id === e.from_node);
    const usesPaths = (e.router.branches || []).some((b) => (b.conditions || []).length);
    if (src && usesPaths && !src.response_format) {
      runnable.push({
        code: "branch_requires_response_format", nodeId: src.id, edgeIdx: idx, fix: "add_response_format",
        message: `“${src.description || src.id}” must answer in fields - a branch reads its result, and free text can't be branched on.`,
      });
    }
    if (!e.router.default_to) {
      runnable.push({
        code: "no_catch_all", edgeIdx: idx, fix: "add_catch_all",
        message: "Add an “in any other case” path - without one, a run that matches nothing stops with an error.",
      });
    }
    for (const b of e.router.branches || []) {
      for (const c of b.conditions || []) {
        if ((c.op === "ne" || c.op === "not_in")
            && !(b.conditions || []).some((x) => x.op === "exists" && x.path === c.path)) {
          warnings.push({
            code: "ne_without_exists", edgeIdx: idx, nodeId: e.from_node,
            message: `“${c.path}” is missing-safe: if the value isn't there, “is not” is false too. Add a “has a value” check first.`,
          });
        }
      }
    }
  }

  // Broken template references surface here rather than mid-run.
  if (window.GB_parseTemplate && window.GB_refIsBroken) {
    for (const n of nodes) {
      for (const f of (window.GB_TEMPLATE_FIELDS || {})[n.kind] || []) {
        for (const tk of window.GB_parseTemplate(n[f] || "")) {
          if (window.GB_refIsBroken(d, tk)) {
            runnable.push({
              code: "template_error", nodeId: n.id, fix: "select_node",
              message: `“${n.description || n.id}” refers to a step that was deleted.`,
            });
          }
        }
      }
    }
  }

  return { blocking: GB_dedupe(blocking), runnable: GB_dedupe(runnable), warnings: GB_dedupe(warnings) };
}

function GB_dedupe(list) {
  const out = [];
  const seen = new Set();
  for (const item of list) {
    const key = `${item.code}|${item.nodeId || ""}|${item.edgeIdx == null ? "" : item.edgeIdx}|${item.message}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}

function GB_hasCycle(draft) {
  const links = window.GB_allLinks ? window.GB_allLinks(draft) : [];
  const adj = new Map();
  for (const [from, to] of links) {
    if (!adj.has(from)) adj.set(from, []);
    adj.get(from).push(to);
  }
  const WHITE = 0, GREY = 1, BLACK = 2;
  const colour = new Map();
  const nodes = (draft.nodes || []).map((n) => n.id);
  for (const id of nodes) colour.set(id, WHITE);
  const visit = (id) => {
    colour.set(id, GREY);
    for (const nx of adj.get(id) || []) {
      const c = colour.get(nx);
      if (c === GREY) return true;
      if (c === WHITE && visit(nx)) return true;
    }
    colour.set(id, BLACK);
    return false;
  };
  for (const id of nodes) if (colour.get(id) === WHITE && visit(id)) return true;
  return false;
}

Object.assign(window, { GB_validate, GB_hasCycle, GB_FAILURE_COPY });
