// Graph builder - references: Jinja <-> chip tokens, available-reference index,
// and rename rewriting. Pure logic, no JSX. WIRING.md §7.
//
// The stored value is ALWAYS a Jinja string; chips are a view over it. Round
// tripping must be lossless: a graph authored in JSON survives open/save here.

// A "simple" reference is the subset we can render as a chip. Anything else
// (filters, {% %}, arithmetic) stays a raw token so we never mangle it.
const GB_REF_RE = /^(initial_input|iteration|ctx|nodes)((?:[.[][\w'"\]\[.-]*)*)$/;

// Fields (per node kind) whose value is a Jinja template.
const GB_TEMPLATE_FIELDS = {
  agent: ["input_template"],
  graph: ["input_template"],
  end: ["output_template"],
  fan_in: ["aggregate_template"],
  tool_call: ["arguments_template"],
};

// Split "nodes.draft_1.text" / "nodes['draft_1[0]'].text" into node id + path.
function GB_splitRef(expr) {
  const m = String(expr || "").match(/^nodes(?:\.([\w-]+)|\[\s*['"]([^'"]+)['"]\s*\])(.*)$/);
  if (!m) return { nodeId: null, path: "" };
  const nodeId = m[1] || m[2] || null;
  const rest = (m[3] || "").replace(/^\./, "");
  return { nodeId, path: rest };
}

// Parse a template into tokens. Lossless: GB_serialize(GB_parseTemplate(s)) === s.
function GB_parseTemplate(str) {
  const s = str == null ? "" : String(str);
  const tokens = [];
  // Match {{ ... }} and {% ... %} in one pass so statements become raw tokens.
  const re = /\{\{([\s\S]*?)\}\}|\{%([\s\S]*?)%\}/g;
  let last = 0;
  let m;
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) tokens.push({ t: "text", v: s.slice(last, m.index) });
    const whole = m[0];
    if (m[1] !== undefined) {
      const expr = m[1].trim();
      const ref = GB_REF_RE.test(expr);
      if (ref) {
        const { nodeId, path } = GB_splitRef(expr);
        // Preserve the exact source so serialisation is byte-identical.
        tokens.push({ t: "ref", v: expr, nodeId, path, src: whole });
      } else {
        tokens.push({ t: "raw", v: expr, src: whole });
      }
    } else {
      tokens.push({ t: "raw", v: (m[2] || "").trim(), src: whole, stmt: true });
    }
    last = m.index + whole.length;
  }
  if (last < s.length) tokens.push({ t: "text", v: s.slice(last) });
  return tokens;
}

// Serialise tokens back to a Jinja string. Tokens carrying `src` round-trip
// byte-for-byte; tokens built by the editor are rendered canonically.
function GB_serialize(tokens) {
  return (tokens || []).map((tk) => {
    if (!tk) return "";
    if (tk.t === "text") return tk.v == null ? "" : String(tk.v);
    if (tk.src) return tk.src;
    if (tk.t === "ref") return `{{ ${tk.v} }}`;
    return tk.stmt ? `{% ${tk.v} %}` : `{{ ${tk.v} }}`;
  }).join("");
}

// Build a chip expression for a node id + path (handles instance ids needing
// bracket syntax, e.g. nodes['worker[0]'].text).
function GB_refExpr(nodeId, path) {
  const safe = /^[A-Za-z_][\w-]*$/.test(String(nodeId || ""));
  const head = safe ? `nodes.${nodeId}` : `nodes['${nodeId}']`;
  return path ? `${head}.${path}` : `${head}.text`;
}

// Rewrite every `nodes.<oldId>` / `nodes['<oldId>...']` inside every template
// field of the draft. Called by the reducer's RENAME_NODE (gb-model.jsx).
function GB_renameInTemplates(draft, oldId, newId) {
  const dotRe = new RegExp(`\\bnodes\\.${GB_escapeRe(oldId)}\\b`, "g");
  const brRe = new RegExp(`\\bnodes\\[\\s*(['"])${GB_escapeRe(oldId)}(\\[[^'"\\]]*\\])?\\1\\s*\\]`, "g");
  const safe = /^[A-Za-z_][\w-]*$/.test(String(newId || ""));
  const rewrite = (text) => {
    if (typeof text !== "string" || !text) return text;
    let out = text.replace(dotRe, safe ? `nodes.${newId}` : `nodes['${newId}']`);
    out = out.replace(brRe, (_all, q, idx) => `nodes[${q}${newId}${idx || ""}${q}]`);
    return out;
  };
  const d = { ...(draft || {}) };
  d.nodes = (d.nodes || []).map((n) => {
    const node = { ...n };
    for (const f of GB_TEMPLATE_FIELDS[node.kind] || []) {
      if (typeof node[f] === "string") node[f] = rewrite(node[f]);
    }
    // Tool arguments: every string leaf is a Jinja template at runtime.
    if (node.kind === "tool_call" && node.arguments && typeof node.arguments === "object") {
      node.arguments = GB_mapStrings(node.arguments, rewrite);
    }
    return node;
  });
  return d;
}

function GB_escapeRe(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

// Deep-map every string leaf of a JSON value.
function GB_mapStrings(value, fn) {
  if (typeof value === "string") return fn(value);
  if (Array.isArray(value)) return value.map((v) => GB_mapStrings(v, fn));
  if (value && typeof value === "object") {
    const out = {};
    for (const k of Object.keys(value)) out[k] = GB_mapStrings(value[k], fn);
    return out;
  }
  return value;
}

// ---------------------------------------------------------------------------
// Available references (§7)
// ---------------------------------------------------------------------------

// Walk a JSON Schema's properties into dotted paths (one level of nesting).
function GB_schemaPaths(schema, prefix, depth) {
  const out = [];
  if (!schema || typeof schema !== "object") return out;
  const props = schema.properties || {};
  for (const key of Object.keys(props)) {
    const p = props[key] || {};
    const path = prefix ? `${prefix}.${key}` : key;
    out.push({ path, type: p.type || "any", description: p.description || "", example: p.example });
    if (p.type === "object" && p.properties && (depth || 0) < 1) {
      out.push(...GB_schemaPaths(p, path, (depth || 0) + 1));
    }
  }
  return out;
}

// Everything `nodeId` may reference, grouped for the picker. Nodes that only
// become available on a later loop pass are flagged `laterLoop`.
function GB_availableRefs(draft, nodeId) {
  const d = draft || {};
  const nodes = d.nodes || [];
  const preds = window.GB_predecessors ? window.GB_predecessors(d, nodeId) : new Set();
  const groups = [];

  const begin = nodes.find((n) => n.kind === "begin");
  const startRows = [{ path: "initial_input", expr: "initial_input", type: "any", label: "the whole input" }];
  for (const f of GB_schemaPaths(begin && begin.input_schema, "", 0)) {
    startRows.push({
      path: f.path, expr: `initial_input.${f.path}`, type: f.type,
      label: f.description, example: f.example,
    });
  }
  groups.push({ id: "__start", title: begin ? (begin.description || "Start") : "Start", rows: startRows });

  const fanoutTargets = new Set();
  for (const n of nodes) {
    if (n.kind !== "fan_out") continue;
    for (const s of n.specs || []) {
      if (s.target_node_id) fanoutTargets.add(s.target_node_id);
      for (const t of s.target_node_ids || []) fanoutTargets.add(t);
    }
  }

  for (const n of nodes) {
    if (n.kind === "begin" || n.id === nodeId) continue;
    const reachable = preds.has(n.id);
    const rows = [{ path: "text", expr: GB_refExpr(n.id, "text"), type: "text", label: "what it produced" }];
    const schema = n.response_format || n.output_schema || null;
    for (const f of GB_schemaPaths(schema, "", 0)) {
      rows.push({
        path: `parsed.${f.path}`, expr: GB_refExpr(n.id, `parsed.${f.path}`),
        type: f.type, label: f.description, example: f.example,
      });
    }
    if (fanoutTargets.has(n.id)) {
      rows.push({ path: "(all copies)", expr: `nodes.${n.id}`, type: "list", label: "every copy's output" });
      rows.push({ path: "[0].text", expr: GB_refExpr(`${n.id}[0]`, "text"), type: "text", label: "one copy" });
    }
    rows.push({ path: "error", expr: GB_refExpr(n.id, "error"), type: "text", label: "error, if it failed", more: true });
    rows.push({ path: "iteration", expr: GB_refExpr(n.id, "iteration"), type: "number", label: "which pass", more: true });
    groups.push({
      id: n.id, title: n.description || n.id, kind: n.kind, rows,
      laterLoop: !reachable, note: reachable ? "" : "runs after this - available on loop 2+",
    });
  }

  groups.push({
    id: "__ctx", title: "Run context", collapsed: true,
    rows: [
      { path: "iteration", expr: "iteration", type: "number", label: "current pass" },
      { path: "ctx.workspace_id", expr: "ctx.workspace_id", type: "text" },
      { path: "ctx.session_id", expr: "ctx.session_id", type: "text" },
      { path: "ctx.artifact_dir", expr: "ctx.artifact_dir", type: "text" },
    ],
  });
  return groups;
}

// Chip label = "<upstream description> · <path>" so renames re-label for free.
function GB_chipLabel(draft, token) {
  if (!token || token.t !== "ref") return "";
  const expr = token.v || "";
  if (expr === "initial_input" || expr.startsWith("initial_input.")) {
    const begin = ((draft || {}).nodes || []).find((n) => n.kind === "begin");
    const head = (begin && begin.description) || "Start";
    const rest = expr.slice("initial_input".length).replace(/^\./, "");
    return rest ? `${head} · ${rest}` : head;
  }
  if (expr.startsWith("ctx.") || expr === "iteration") return expr;
  const base = String(token.nodeId || "").replace(/\[\d+\]$/, "");
  const node = ((draft || {}).nodes || []).find((n) => n.id === base);
  const head = (node && node.description) || token.nodeId || "step";
  return token.path ? `${head} · ${token.path}` : head;
}

// A chip whose node no longer exists must render red, not fail silently (§7).
function GB_refIsBroken(draft, token) {
  if (!token || token.t !== "ref" || !token.nodeId) return false;
  const base = String(token.nodeId).replace(/\[\d+\]$/, "");
  return !((draft || {}).nodes || []).some((n) => n.id === base);
}

Object.assign(window, {
  GB_REF_RE, GB_TEMPLATE_FIELDS, GB_parseTemplate, GB_serialize, GB_refExpr,
  GB_splitRef, GB_renameInTemplates, GB_mapStrings, GB_schemaPaths,
  GB_availableRefs, GB_chipLabel, GB_refIsBroken,
});
