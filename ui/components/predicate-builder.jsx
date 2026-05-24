/* global React, Icon, Btn */

const ENTITIES = {
  sessions: {
    label: "Sessions",
    fields: [
      { name: "id", type: "string" },
      { name: "status", type: "enum", enum: ["created", "running", "paused", "ended", "failed", "cancelled"] },
      { name: "agent_id", type: "string" },
      { name: "workspace_id", type: "string" },
      { name: "graph_id", type: "string" },
      { name: "turn_count", type: "int", warnEq: true },
      { name: "created_at", type: "datetime" },
      { name: "last_turn_at", type: "datetime" },
      { name: "meta.priority", type: "jsonb-num", warnGt: true },
      { name: "meta.score", type: "jsonb-num", warnGt: true },
      { name: "meta.tags", type: "jsonb-arr" },
    ],
  },
  agents: {
    label: "Agents",
    fields: [
      { name: "id", type: "string" },
      { name: "description", type: "string" },
      { name: "llm_provider_id", type: "string" },
    ],
  },
  workspaces: {
    label: "Workspaces",
    fields: [
      { name: "id", type: "string" },
      { name: "template_id", type: "string" },
      { name: "created_at", type: "datetime" },
    ],
  },
  documents: {
    label: "Documents",
    fields: [
      { name: "id", type: "string" },
      { name: "collection_id", type: "string" },
      { name: "name", type: "string" },
      { name: "ingested_at", type: "datetime" },
    ],
  },
  collections: {
    label: "Collections",
    fields: [
      { name: "id", type: "string" },
      { name: "embedding_provider_id", type: "string" },
    ],
  },
  toolsets: {
    label: "Toolsets",
    fields: [
      { name: "id", type: "string" },
      { name: "kind", type: "enum", enum: ["mcp_stdio", "mcp_sse", "mcp_http", "web", "system"] },
    ],
  },
};

const OPS = {
  string: ["=", "!=", "~=", "in"],
  enum: ["=", "!=", "in"],
  int: ["=", "!=", ">", ">=", "<", "<="],
  "jsonb-num": ["=", "!=", ">", ">=", "<", "<="],
  "jsonb-arr": ["contains"],
  datetime: ["=", ">", ">=", "<", "<="],
};

let _id = 1;
const nid = () => ++_id;

const seedPredicate = () => ({
  kind: "group",
  id: nid(),
  op: "AND",
  children: [
    { kind: "clause", id: nid(), field: "status", op: "=", value: "ended" },
    { kind: "clause", id: nid(), field: "agent_id", op: "~=", value: "support-%" },
    {
      kind: "group",
      id: nid(),
      op: "OR",
      children: [
        { kind: "clause", id: nid(), field: "turn_count", op: ">=", value: "3" },
        { kind: "clause", id: nid(), field: "meta.priority", op: ">", value: "2" },
      ],
    },
  ],
});

function PredicateBuilder({ pushToast }) {
  const [entity, setEntity] = React.useState("sessions");
  const [tree, setTree] = React.useState(seedPredicate);
  const [orderBy, setOrderBy] = React.useState([{ field: "created_at", dir: "desc" }]);
  const [pageMode, setPageMode] = React.useState("offset");
  const [limit, setLimit] = React.useState(50);
  const [wireOpen, setWireOpen] = React.useState(true);
  const [savedPredicates, setSavedPredicates] = React.useState(() => {
    try { return JSON.parse(localStorage.getItem("matrix.predicates.saved") || "[]"); } catch { return []; }
  });
  const [resultCount, setResultCount] = React.useState(247);
  const [latency, setLatency] = React.useState(38);

  const fields = ENTITIES[entity].fields;

  // Update tree helpers
  const updateNode = (id, patch) => {
    const walk = (n) => {
      if (n.id === id) return { ...n, ...patch };
      if (n.kind === "group") return { ...n, children: n.children.map(walk) };
      return n;
    };
    setTree(walk(tree));
  };
  const removeNode = (id) => {
    const walk = (n) => {
      if (n.kind !== "group") return n;
      return { ...n, children: n.children.filter((c) => c.id !== id).map(walk) };
    };
    setTree(walk(tree));
  };
  const addChild = (parentId, kind) => {
    const newNode = kind === "group"
      ? { kind: "group", id: nid(), op: "AND", children: [{ kind: "clause", id: nid(), field: fields[0].name, op: "=", value: "" }] }
      : { kind: "clause", id: nid(), field: fields[0].name, op: "=", value: "" };
    const walk = (n) => {
      if (n.kind !== "group") return n;
      if (n.id === parentId) return { ...n, children: [...n.children, newNode] };
      return { ...n, children: n.children.map(walk) };
    };
    setTree(walk(tree));
  };
  const setGroupOp = (id, op) => updateNode(id, { op });

  const runQuery = () => {
    setLatency(28 + Math.floor(Math.random() * 50));
    setResultCount(Math.floor(Math.random() * 400 + 12));
    pushToast({ kind: "success", title: "Predicate executed", detail: `Returned ${resultCount} rows in ${latency}ms` });
  };

  const saveNamed = () => {
    const name = window.prompt("Save predicate as…");
    if (!name) return;
    const next = [...savedPredicates, { name, entity, tree, at: Date.now() }];
    setSavedPredicates(next);
    try { localStorage.setItem("matrix.predicates.saved", JSON.stringify(next)); } catch {}
    pushToast({ kind: "success", title: "Saved", detail: `"${name}" saved to local storage` });
  };

  const wireJson = treeToWire(tree);
  const curl = `curl -sS -X POST http://localhost:8765/v1/${entity}/find \\
  -H 'content-type: application/json' \\
  -d '${JSON.stringify({ predicate: wireJson, order_by: orderBy, limit }).replace(/'/g, "'\\''")}'`;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 380px", gap: 18 }}>
      <div className="col">
        <div className="panel">
          <div className="panel-h">
            <Icon name="filter" size={13} className="muted" />
            <span>Predicate</span>
            <span className="sub">· entity</span>
            <select className="select" style={{ marginLeft: 6, fontSize: 11.5, padding: "2px 6px" }} value={entity} onChange={(e) => setEntity(e.target.value)}>
              {Object.entries(ENTITIES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </select>
            <div className="right">
              <Btn size="sm" kind="ghost" icon="external" onClick={saveNamed}>Save…</Btn>
              <Btn size="sm" kind="ghost" icon="trash" onClick={() => setTree(seedPredicate())}>Reset</Btn>
            </div>
          </div>
          <div className="panel-body">
            <PNode node={tree} fields={fields} onUpdate={updateNode} onRemove={removeNode} onAdd={addChild} onOp={setGroupOp} root />
          </div>
        </div>

        {/* Order / page */}
        <div className="panel">
          <div className="panel-h">
            <span>Order &amp; pagination</span>
          </div>
          <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span className="muted text-sm" style={{ width: 80 }}>Order by</span>
              {orderBy.map((o, i) => (
                <div key={i} className="pb-clause" style={{ padding: "3px 6px" }}>
                  <select value={o.field} onChange={(e) => {
                    const next = [...orderBy]; next[i] = { ...next[i], field: e.target.value }; setOrderBy(next);
                  }}>
                    {fields.map((f) => <option key={f.name}>{f.name}</option>)}
                  </select>
                  <select value={o.dir} onChange={(e) => {
                    const next = [...orderBy]; next[i] = { ...next[i], dir: e.target.value }; setOrderBy(next);
                  }}>
                    <option value="desc">desc</option>
                    <option value="asc">asc</option>
                  </select>
                </div>
              ))}
              <button className="pb-add" onClick={() => setOrderBy([...orderBy, { field: fields[0].name, dir: "asc" }])}>
                <Icon name="plus" size={10} /> field
              </button>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span className="muted text-sm" style={{ width: 80 }}>Page</span>
              <div className="chip-group">
                <span className={`chip ${pageMode === "offset" ? "active" : ""}`} onClick={() => setPageMode("offset")}>offset</span>
                <span className={`chip ${pageMode === "cursor" ? "active" : ""}`} onClick={() => setPageMode("cursor")}>cursor</span>
              </div>
              <span className="muted text-sm">limit</span>
              <input
                className="input mono"
                type="number"
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value) || 50)}
                style={{ width: 70 }}
              />
            </div>
          </div>
        </div>

        {/* Run + results */}
        <div className="panel">
          <div className="panel-h">
            <Icon name="play" size={11} />
            <span>Execute</span>
          </div>
          <div className="panel-body" style={{ display: "flex", gap: 12, alignItems: "center" }}>
            <Btn kind="primary" size="lg" icon="play" onClick={runQuery}>Run</Btn>
            <div style={{ flex: 1, fontSize: 13 }}>
              <div className="mono" style={{ fontWeight: 600, fontSize: 18 }}>
                {resultCount.toLocaleString()} <span className="muted text-sm" style={{ fontWeight: 400 }}>rows match</span>
              </div>
              <div className="muted text-sm">
                evaluated with <span className="mono">length=1</span> ·{" "}
                <span className="mono" style={{ color: "var(--accent)" }}>{latency}ms</span> server-side
              </div>
            </div>
            <Btn size="sm" kind="ghost" icon="external">Open in /find</Btn>
          </div>
        </div>
      </div>

      {/* Right column — wire JSON + curl + saved */}
      <div className="col">
        <div className="panel">
          <div className="panel-h" onClick={() => setWireOpen(!wireOpen)} style={{ cursor: "pointer" }}>
            <Icon name={wireOpen ? "chevron-down" : "chevron-right"} size={11} />
            <Icon name="code" size={13} className="muted" />
            <span>Generated wire JSON</span>
            <div className="right">
              <Btn size="sm" kind="ghost" icon="copy">Copy</Btn>
            </div>
          </div>
          {wireOpen && (
            <div className="panel-body" style={{ padding: 0 }}>
              <div className="code-block" style={{ border: 0, borderRadius: 0, background: "transparent", maxHeight: 320, overflow: "auto" }}>
                <SyntaxJson value={{ predicate: wireJson, order_by: orderBy, limit }} />
              </div>
            </div>
          )}
        </div>

        <div className="panel">
          <div className="panel-h">
            <Icon name="code" size={13} className="muted" />
            <span>cURL one-liner</span>
            <div className="right">
              <Btn size="sm" kind="ghost" icon="copy">Copy</Btn>
            </div>
          </div>
          <div className="panel-body" style={{ padding: 0 }}>
            <div className="code-block" style={{ border: 0, borderRadius: 0, background: "transparent", maxHeight: 180, overflow: "auto" }}>
              {curl}
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">
            <Icon name="clock" size={13} className="muted" />
            <span>Saved</span>
            <span className="sub">· local</span>
          </div>
          <div className="panel-body" style={{ padding: "4px 12px" }}>
            {savedPredicates.length === 0 ? (
              <div className="muted text-sm" style={{ padding: 8 }}>None yet. Save a predicate with <strong>Save…</strong>.</div>
            ) : (
              savedPredicates.map((p, i) => (
                <div key={i} className="ref-row" style={{ cursor: "pointer" }} onClick={() => { setEntity(p.entity); setTree(p.tree); }}>
                  <Icon name="filter" size={13} className="ico" />
                  <span className="val">{p.name}</span>
                  <span className="muted text-sm mono">{p.entity}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function PNode({ node, fields, onUpdate, onRemove, onAdd, onOp, root }) {
  if (node.kind === "group") {
    return (
      <div className="pb-node">
        <div className="pb-group-h">
          <div className="pb-op-toggle">
            <button className={node.op === "AND" ? "active" : ""} onClick={() => onOp(node.id, "AND")}>AND</button>
            <button className={node.op === "OR" ? "active" : ""} onClick={() => onOp(node.id, "OR")}>OR</button>
          </div>
          <span className="muted text-sm">· {node.children.length} clause{node.children.length === 1 ? "" : "s"}</span>
          {!root && (
            <button className="icon-btn" style={{ width: 20, height: 20, border: "none", background: "transparent" }} onClick={() => onRemove(node.id)} title="Remove group">
              <Icon name="x" size={11} className="muted" />
            </button>
          )}
        </div>
        <div className="pb-node-children">
          {node.children.map((c) => (
            <PNode key={c.id} node={c} fields={fields} onUpdate={onUpdate} onRemove={onRemove} onAdd={onAdd} onOp={onOp} />
          ))}
          <div style={{ display: "flex", gap: 6 }}>
            <button className="pb-add" onClick={() => onAdd(node.id, "clause")}>
              <Icon name="plus" size={10} /> clause
            </button>
            <button className="pb-add" onClick={() => onAdd(node.id, "group")}>
              <Icon name="plus" size={10} /> group
            </button>
          </div>
        </div>
      </div>
    );
  }
  return <Clause node={node} fields={fields} onUpdate={onUpdate} onRemove={onRemove} />;
}

function Clause({ node, fields, onUpdate, onRemove }) {
  const f = fields.find((x) => x.name === node.field) || fields[0];
  const ops = OPS[f.type] || ["="];
  const warn = (f.warnEq && node.op === "=") || (f.warnGt && (node.op === ">" || node.op === ">="));
  return (
    <div className="pb-clause">
      <select value={node.field} onChange={(e) => onUpdate(node.id, { field: e.target.value })}>
        {fields.map((x) => <option key={x.name} value={x.name}>{x.name}</option>)}
      </select>
      <select value={node.op} onChange={(e) => onUpdate(node.id, { op: e.target.value })}>
        {ops.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
      {f.type === "enum" ? (
        <select value={node.value} onChange={(e) => onUpdate(node.id, { value: e.target.value })}>
          {f.enum.map((v) => <option key={v} value={v}>{v}</option>)}
        </select>
      ) : (
        <input value={node.value} onChange={(e) => onUpdate(node.id, { value: e.target.value })} placeholder={f.type === "int" || f.type === "jsonb-num" ? "0" : '"value"'} style={{ minWidth: 100 }} />
      )}
      {warn && (
        <span className="warn-mark" title="Known JSONB type-coercion bug (T0236/T0361/T0583) — this combo may surface a 502 envelope. Workaround: cast in client.">
          ⚠
        </span>
      )}
      <button className="delete" onClick={() => onRemove(node.id)} title="Delete">
        <Icon name="x" size={12} />
      </button>
    </div>
  );
}

function treeToWire(node) {
  if (node.kind === "group") {
    return { [node.op.toLowerCase()]: node.children.map(treeToWire) };
  }
  let v = node.value;
  if (v !== "" && !isNaN(Number(v)) && /^-?\d+(\.\d+)?$/.test(v)) v = Number(v);
  return { field: node.field, op: node.op, value: v };
}

function SyntaxJson({ value }) {
  const lines = JSON.stringify(value, null, 2);
  // Colorize keys, strings, numbers
  const html = lines
    .replace(/("([^"\\]|\\.)*"):/g, '<span class="key">$1</span>:')
    .replace(/: ("([^"\\]|\\.)*")/g, ': <span class="str">$1</span>')
    .replace(/: (-?\d+(\.\d+)?)/g, ': <span class="num">$1</span>')
    .replace(/: (true|false|null)/g, ': <span class="kw">$1</span>');
  return <span dangerouslySetInnerHTML={{ __html: html }} />;
}

window.PredicateBuilder = PredicateBuilder;
