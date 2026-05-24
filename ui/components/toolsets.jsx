/* global React, Icon, Btn, Banner, relativeTime */

const USER_TOOLSETS = [
  { id: "github-mcp", kind: "mcp_stdio", command: "npx", args: ["-y", "@modelcontextprotocol/server-github"], tools: 6, cached: true, last_invalidate_ago: 3600 * 6 },
  { id: "stripe-mcp", kind: "mcp_stdio", command: "uvx", args: ["stripe-mcp"], tools: 4, cached: true, last_invalidate_ago: 18 * 60 },
  { id: "zendesk-mcp", kind: "mcp_http", url: "https://mcp.zendesk.com/v1", tools: 3, cached: true, last_invalidate_ago: 3600 * 12 },
  { id: "postgres-readonly-mcp", kind: "mcp_stdio", command: "/usr/local/bin/pg-mcp", args: ["--readonly"], tools: 2, cached: false, last_invalidate_ago: 4 },
  { id: "ny-times-api", kind: "web", url: "https://api.nytimes.com/v1/openapi.json", tools: 11, cached: true, last_invalidate_ago: 3600 * 48 },
];

const BUILTIN_TOOLSETS = [
  { id: "_system", desc: "System primitives — clock, sleep, env.", tools: 2, available: true },
  { id: "_workspaces", desc: "Filesystem and shell exec inside the bound workspace.", tools: 8, available: true },
  { id: "_search", desc: "Semantic search across collections.", tools: 2, available: false, note: "Requires Internal Collections to be bootstrapped." },
  { id: "web", desc: "Plain HTTP fetch + JSON parse.", tools: 4, available: true },
];

const TOOLSET_TOOLS_FULL = {
  "github-mcp": [
    { id: "github.get_pr", desc: "Fetch a PR by number", input: { number: "integer", repo: "string" } },
    { id: "github.list_files", desc: "List files in a PR diff", input: { number: "integer", repo: "string" } },
    { id: "github.add_review_comment", desc: "Post an inline review comment", input: { path: "string", line: "integer", body: "string" } },
    { id: "github.add_issue_comment", desc: "Comment on a thread", input: { number: "integer", body: "string" } },
    { id: "github.merge_pr", desc: "Merge a PR", input: { number: "integer", method: "squash | merge | rebase" } },
    { id: "github.create_branch", desc: "Create a branch", input: { name: "string", from: "string" } },
  ],
  "stripe-mcp": [
    { id: "stripe.search_charges", desc: "Search charges by criteria", input: { query: "string" } },
    { id: "stripe.create_refund", desc: "Issue a refund", input: { charge: "string", amount: "integer | null", reason: "string" } },
    { id: "stripe.get_customer", desc: "Fetch customer by id", input: { id: "string" } },
    { id: "stripe.list_invoices", desc: "List invoices for a customer", input: { customer: "string", limit: "integer" } },
  ],
  "zendesk-mcp": [
    { id: "zendesk.search_tickets", desc: "Search tickets", input: { query: "string" } },
    { id: "zendesk.create_ticket", desc: "Open a new ticket", input: { subject: "string", body: "string", queue: "string" } },
    { id: "zendesk.add_comment", desc: "Append a comment", input: { id: "integer", body: "string" } },
  ],
  "postgres-readonly-mcp": [
    { id: "db.introspect", desc: "Read schema", input: { schema: "string | null" } },
    { id: "db.query", desc: "Run a read-only SQL query", input: { sql: "string" } },
  ],
  "_system": [
    { id: "system.now", desc: "Current UTC time", input: {} },
    { id: "system.sleep", desc: "Sleep for n seconds", input: { seconds: "number" } },
  ],
  "_workspaces": [
    { id: "fs.read", desc: "Read a file", input: { path: "string", encoding: "utf-8 | binary" } },
    { id: "fs.write", desc: "Write a file", input: { path: "string", content: "string" } },
    { id: "fs.ls", desc: "List directory", input: { path: "string" } },
    { id: "fs.grep", desc: "Search files", input: { pattern: "string", path: "string | null" } },
    { id: "fs.delete", desc: "Delete a file", input: { path: "string" } },
    { id: "exec.shell", desc: "Run a shell command", input: { cmd: "string", cwd: "string | null" } },
    { id: "git.log", desc: "Read .state log", input: { limit: "integer" } },
    { id: "git.commit", desc: "Commit pending changes", input: { message: "string" } },
  ],
  "_search": [
    { id: "search.query", desc: "Semantic search over a collection", input: { collection: "string", query: "string", top_k: "integer" } },
    { id: "search.ingest", desc: "Ingest chunks", input: { collection: "string", chunks: "array<string>" } },
  ],
  "web": [
    { id: "web.fetch", desc: "GET a URL", input: { url: "string", headers: "object | null" } },
    { id: "web.post", desc: "POST a URL", input: { url: "string", body: "object" } },
    { id: "web.parse_json", desc: "Parse a JSON string", input: { text: "string" } },
    { id: "web.openapi_list", desc: "List operations from an OpenAPI spec URL", input: { url: "string" } },
  ],
};

function ToolsetsPage({ kind, pushToast }) {
  const [selected, setSelected] = React.useState(null);
  if (kind === "user") {
    return (
      <UserToolsets
        selected={selected}
        setSelected={setSelected}
        pushToast={pushToast}
      />
    );
  }
  return <BuiltinToolsets pushToast={pushToast} />;
}

function UserToolsets({ selected, setSelected, pushToast }) {
  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter toolsets…" />
        </div>
        <div className="sep-v" />
        <select className="select"><option>all kinds</option><option>mcp_stdio</option><option>mcp_http</option><option>web</option></select>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus">New toolset</Btn>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: selected ? "1.4fr 1fr" : "1fr", gap: 18 }}>
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>ID</th>
                <th>Kind</th>
                <th>Transport</th>
                <th style={{ textAlign: "right" }}>Tools</th>
                <th>Cache</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {USER_TOOLSETS.map((t) => (
                <tr key={t.id} className={selected === t.id ? "selected" : ""} onClick={() => setSelected(selected === t.id ? null : t.id)}>
                  <td className="mono">{t.id}</td>
                  <td><KindBadge kind={t.kind} /></td>
                  <td className="mono muted text-sm" style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {t.kind === "mcp_stdio" ? `${t.command} ${(t.args || []).join(" ")}` : t.url}
                  </td>
                  <td className="mono num tabular">{t.tools}</td>
                  <td>
                    {t.cached ? (
                      <span className="pill pill-ended"><span className="dot"></span>cached</span>
                    ) : (
                      <span className="pill pill-paused"><span className="dot"></span>cold</span>
                    )}
                  </td>
                  <td style={{ textAlign: "right", paddingRight: 12 }}><Icon name="chevron-right" size={12} className="muted" /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {selected && <ToolsetDetail t={USER_TOOLSETS.find((x) => x.id === selected)} pushToast={pushToast} />}
      </div>
    </div>
  );
}

function ToolsetDetail({ t, pushToast }) {
  const tools = TOOLSET_TOOLS_FULL[t.id] || [];
  const stdioWarn = t.kind === "mcp_stdio" && !["uvx", "npx"].includes(t.command);
  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name="tools" size={13} className="muted" />
        <span className="mono">{t.id}</span>
        <div className="right">
          <Btn size="sm" kind="ghost" icon="refresh" onClick={() => pushToast({ kind: "success", title: "Cache invalidated", detail: `POST /v1/toolsets/${t.id}/invalidate → 200. Next call rebuilds.` })} title="Drops the cached provider; next call rebuilds from the row.">
            Invalidate
          </Btn>
          <Btn size="sm" kind="danger" icon="trash">Delete</Btn>
        </div>
      </div>
      <div className="panel-body">
        <div className="kv" style={{ gridTemplateColumns: "100px 1fr" }}>
          <dt>kind</dt><dd><KindBadge kind={t.kind} /></dd>
          {t.kind === "mcp_stdio" && (
            <>
              <dt>command</dt><dd className="mono">{t.command}</dd>
              <dt>args</dt><dd className="mono">{JSON.stringify(t.args)}</dd>
            </>
          )}
          {(t.kind === "mcp_http" || t.kind === "web" || t.kind === "mcp_sse") && (
            <>
              <dt>url</dt><dd className="mono">{t.url}</dd>
            </>
          )}
          <dt>last invalidate</dt><dd className="mono muted">{relativeTime(t.last_invalidate_ago)}</dd>
        </div>
        {stdioWarn && (
          <div className="banner banner-warning mt-3" style={{ marginBottom: 0, fontSize: 11.5 }}>
            <Icon name="alert" size={12} className="ico" />
            <div>Command <span className="mono">{t.command}</span> isn't in <span className="mono">mcp_stdio_allowed_commands</span>. The toolset will refuse to spawn.</div>
          </div>
        )}
        <div className="mt-3">
          <div className="muted text-sm mono mb-2" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>
            tools <span className="muted">· {tools.length}</span>
          </div>
          <table className="tbl">
            <thead>
              <tr>
                <th>name</th>
                <th>description</th>
                <th>approval</th>
              </tr>
            </thead>
            <tbody>
              {tools.map((tool) => {
                const policy = (window.APPROVAL_POLICY_INDEX || []).find((p) => matchesPattern(tool.id, p.tool_pattern) && (p.toolset_id === t.id || p.toolset_id === "*"));
                return (
                  <tr key={tool.id}>
                    <td className="mono">{tool.id}</td>
                    <td className="muted text-sm" style={{ fontSize: 11 }}>{tool.desc}</td>
                    <td>
                      {policy ? (
                        <span className="pill" style={{ background: "var(--bg-2)", color: policy.type === "required" ? "var(--amber)" : policy.type === "policy" ? "var(--blue)" : "var(--violet)", border: "1px solid var(--border)" }} title={`Policy ${policy.id} · ${policy.description}`}>
                          <span className="dot" style={{ background: policy.type === "required" ? "var(--amber)" : policy.type === "policy" ? "var(--blue)" : "var(--violet)" }}></span>
                          {policy.type}
                        </span>
                      ) : (
                        <span className="muted text-sm">—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function KindBadge({ kind }) {
  const map = {
    mcp_stdio: { color: "var(--accent)", label: "mcp_stdio" },
    mcp_http: { color: "var(--blue)", label: "mcp_http" },
    mcp_sse: { color: "var(--violet)", label: "mcp_sse" },
    web: { color: "var(--amber)", label: "web" },
    system: { color: "var(--text-2)", label: "system" },
  };
  const m = map[kind] || map.system;
  return (
    <span className="pill" style={{ background: "var(--bg-2)", color: m.color, border: "1px solid var(--border)" }}>
      <span className="dot" style={{ background: m.color }}></span>
      <span className="mono text-sm">{m.label}</span>
    </span>
  );
}

function BuiltinToolsets({ pushToast }) {
  return (
    <div className="col" style={{ gap: 14 }}>
      <Banner
        kind="info"
        title="Built-in toolsets are read-only"
        detail="These are wired by the runtime — you can't create, edit, or delete them. _search becomes available once Internal Collections is bootstrapped."
      />
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        {BUILTIN_TOOLSETS.map((t) => <BuiltinCard key={t.id} t={t} />)}
      </div>
    </div>
  );
}

function BuiltinCard({ t }) {
  const tools = TOOLSET_TOOLS_FULL[t.id] || [];
  const [open, setOpen] = React.useState(false);
  return (
    <div className="panel" style={{ opacity: t.available ? 1 : 0.55 }}>
      <div className="panel-h">
        <Icon name="tools" size={13} style={{ color: t.available ? "var(--accent)" : "var(--text-3)" }} />
        <span className="mono">{t.id}</span>
        <div className="right">
          {t.available ? (
            <span className="pill pill-ended"><span className="dot"></span>available</span>
          ) : (
            <span className="pill pill-failed"><span className="dot"></span>unavailable</span>
          )}
        </div>
      </div>
      <div className="panel-body">
        <div className="text-sm muted mb-2">{t.desc}</div>
        {t.note && <div className="banner banner-warning text-sm" style={{ margin: "8px 0", fontSize: 11.5, padding: "6px 10px" }}>
          <Icon name="alert" size={11} className="ico" />
          <div>{t.note}</div>
        </div>}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 4 }}>
          <span className="text-sm muted mono">{tools.length} tools</span>
          <button className="btn btn-sm btn-ghost" onClick={() => setOpen(!open)}>
            {open ? "Hide" : "Show"} <Icon name={open ? "chevron-up" : "chevron-down"} size={10} />
          </button>
        </div>
        {open && (
          <div style={{ marginTop: 8, fontSize: 11.5 }}>
            {tools.map((tool) => (
              <div key={tool.id} style={{ padding: "4px 0", display: "flex", gap: 8, borderBottom: "1px solid var(--border)" }}>
                <span className="mono" style={{ width: 130, flexShrink: 0 }}>{tool.id}</span>
                <span className="muted text-sm" style={{ fontSize: 11 }}>{tool.desc}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function matchesPattern(toolId, pattern) {
  // glob with | as OR separator
  return pattern.split("|").map((p) => p.trim()).some((p) => {
    if (p === toolId) return true;
    if (p.endsWith(".*")) return toolId.startsWith(p.slice(0, -1));
    return false;
  });
}

window.ToolsetsPage = ToolsetsPage;
