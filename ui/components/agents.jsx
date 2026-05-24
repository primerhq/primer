/* global React, Icon, StatusPill, Btn, Banner, relativeTime */

const AGENT_DETAILS = {
  "support-triage": {
    desc: "Routes inbound support tickets to the right queue. Drafts replies for refund requests under $50.",
    llm_provider_id: "openai-1",
    llm_model: "gpt-4o",
    system_prompt: `You are a customer support triage agent. Your job is to read inbound emails and decide which Zendesk queue they belong in (billing, technical, sales). For refund requests under $50 you may draft a reply directly.`,
    toolsets: ["_workspaces", "zendesk-mcp"],
    metadata: { team: "support", priority_tier: 1 },
    ok: true,
    issues: [],
  },
  "stripe-refunds": {
    desc: "Issues partial refunds against Stripe based on policy.",
    llm_provider_id: "anthropic-1",
    llm_model: "claude-sonnet-4",
    system_prompt: `You are a refunds processor with read+write access to Stripe.`,
    toolsets: ["stripe-mcp", "_system"],
    metadata: {},
    ok: true,
    issues: [],
  },
  "pr-reviewer": {
    desc: "Reviews pull requests and posts inline comments.",
    llm_provider_id: "anthropic-1",
    llm_model: "claude-sonnet-4",
    system_prompt: `You are a senior engineer reviewing pull requests.`,
    toolsets: ["github-mcp", "_workspaces"],
    metadata: { team: "platform" },
    ok: true,
    issues: [],
  },
  "code-explainer": {
    desc: "Walks junior engineers through unfamiliar code paths.",
    llm_provider_id: "anthropic-1",
    llm_model: "claude-sonnet-4",
    system_prompt: `You are a senior engineer explaining unfamiliar code.`,
    toolsets: ["_workspaces"],
    metadata: {},
    ok: true,
    issues: [],
  },
  "doc-ingestion": {
    desc: "Ingests new docs into the knowledge base.",
    llm_provider_id: "openai-1",
    llm_model: "gpt-4o-mini",
    system_prompt: `You ingest markdown docs into the docs collection.`,
    toolsets: ["_workspaces", "_search"],
    metadata: {},
    ok: false,
    issues: [
      { kind: "toolset_missing", target: "_search", detail: "Internal Collections subsystem is not bootstrapped — toolset _search returns 503 on every call." },
    ],
  },
  "sql-helper": {
    desc: "Translates English into safe read-only SQL.",
    llm_provider_id: "openai-1",
    llm_model: "gpt-4o",
    system_prompt: `You translate English to read-only PostgreSQL.`,
    toolsets: ["postgres-readonly-mcp"],
    metadata: {},
    ok: true,
    issues: [],
  },
  "release-notes": {
    desc: "Drafts release notes from a range of commits.",
    llm_provider_id: "anthropic-1",
    llm_model: "claude-sonnet-4",
    system_prompt: `Generate human-readable release notes from a commit range.`,
    toolsets: ["github-mcp", "_workspaces"],
    metadata: {},
    ok: true,
    issues: [],
  },
  "agent-broken-llm": {
    desc: "Test fixture with a deleted LLM provider.",
    llm_provider_id: "openai-deleted",
    llm_model: "gpt-4o",
    system_prompt: "(test fixture)",
    toolsets: [],
    metadata: { fixture: true },
    ok: false,
    issues: [
      { kind: "llm_provider_missing", target: "openai-deleted", detail: "LLM provider 'openai-deleted' no longer exists. Update the agent's llm_provider_id." },
    ],
  },
};

const TOOLSET_TOOLS = {
  "_workspaces": [
    { id: "fs.read", desc: "Read a file from the workspace" },
    { id: "fs.write", desc: "Write a file" },
    { id: "fs.ls", desc: "List directory" },
    { id: "fs.grep", desc: "Search files with a pattern" },
    { id: "fs.delete", desc: "Delete a file" },
    { id: "exec.shell", desc: "Run a shell command" },
    { id: "git.log", desc: "Read the workspace .state log" },
    { id: "git.commit", desc: "Commit pending changes" },
  ],
  "_system": [
    { id: "system.now", desc: "Current UTC time" },
    { id: "system.sleep", desc: "Sleep for n seconds" },
  ],
  "_search": [
    { id: "search.query", desc: "Semantic search over a collection" },
    { id: "search.ingest", desc: "Ingest chunks into a collection" },
  ],
  "github-mcp": [
    { id: "github.get_pr", desc: "Fetch a PR by number" },
    { id: "github.list_files", desc: "List files in a PR diff" },
    { id: "github.add_review_comment", desc: "Post an inline review comment" },
    { id: "github.add_issue_comment", desc: "Comment on an issue or PR thread" },
    { id: "github.merge_pr", desc: "Merge a PR" },
    { id: "github.create_branch", desc: "Create a branch" },
  ],
  "stripe-mcp": [
    { id: "stripe.search_charges", desc: "Search charges by criteria" },
    { id: "stripe.create_refund", desc: "Issue a refund" },
    { id: "stripe.get_customer", desc: "Fetch customer by id" },
    { id: "stripe.list_invoices", desc: "List invoices for a customer" },
  ],
  "zendesk-mcp": [
    { id: "zendesk.search_tickets", desc: "Search tickets" },
    { id: "zendesk.create_ticket", desc: "Open a new ticket" },
    { id: "zendesk.add_comment", desc: "Append a comment" },
  ],
  "postgres-readonly-mcp": [
    { id: "db.introspect", desc: "Read schema" },
    { id: "db.query", desc: "Run a read-only SQL query" },
  ],
};

function AgentsPage({ onOpen, sessions, onNewSession }) {
  const [query, setQuery] = React.useState("");
  const filtered = window.MOCK.AGENTS.filter((a) => !query || a.id.toLowerCase().includes(query.toLowerCase()) || a.desc.toLowerCase().includes(query.toLowerCase()));

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter agents…" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <div className="sep-v" />
        <select className="select"><option>all providers</option><option>openai</option><option>anthropic</option></select>
        <select className="select"><option>all statuses</option><option>ok</option><option>issues</option></select>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus">New agent</Btn>
        </div>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: 30 }}><input type="checkbox" /></th>
              <th>ID</th>
              <th>Description</th>
              <th>Provider</th>
              <th>Toolsets</th>
              <th style={{ textAlign: "right" }}>Sessions</th>
              <th style={{ width: 80 }}>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((a) => {
              const d = AGENT_DETAILS[a.id] || { ok: true, toolsets: [] };
              const onAgent = sessions.filter((s) => s.agent_id === a.id).length;
              return (
                <tr key={a.id} onClick={() => onOpen(a.id)}>
                  <td onClick={(e) => e.stopPropagation()}><input type="checkbox" /></td>
                  <td className="mono">{a.id}</td>
                  <td className="muted" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.desc}</td>
                  <td>
                    {a.provider ? (
                      <span className="mono text-sm" style={{ color: a.provider === "openai" ? "var(--green)" : "var(--accent)" }}>{a.provider}</span>
                    ) : (
                      <span className="muted">(missing)</span>
                    )}
                  </td>
                  <td className="mono muted text-sm" style={{ fontSize: 11.5 }}>
                    {d.toolsets.slice(0, 2).join(", ")}{d.toolsets.length > 2 && <span> +{d.toolsets.length - 2}</span>}
                  </td>
                  <td className="mono num tabular">{onAgent || <span className="muted">0</span>}</td>
                  <td>
                    {d.ok ? (
                      <span className="pill pill-ended"><span className="dot"></span>ok</span>
                    ) : (
                      <span className="pill pill-failed"><span className="dot"></span>{d.issues.length} issue{d.issues.length === 1 ? "" : "s"}</span>
                    )}
                  </td>
                  <td style={{ textAlign: "right", paddingRight: 12 }}><Icon name="chevron-right" size={12} className="muted" /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function AgentDetail({ agentId, sessions, onTest, pushToast }) {
  const a = window.MOCK.AGENTS.find((x) => x.id === agentId);
  const d = AGENT_DETAILS[agentId];
  const [tab, setTab] = React.useState("config");
  const onAgentSessions = sessions.filter((s) => s.agent_id === agentId).slice(0, 6);

  if (!a || !d) return null;

  return (
    <div className="col" style={{ gap: 14 }}>
      {/* Status panel */}
      <div className="panel" style={{
        background: d.ok ? "linear-gradient(90deg, var(--green-dim) 0%, var(--bg-1) 50%)" : "linear-gradient(90deg, var(--red-dim) 0%, var(--bg-1) 50%)",
        borderColor: d.ok ? "oklch(0.75 0.15 145 / 0.3)" : "oklch(0.7 0.2 25 / 0.3)",
      }}>
        <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 18px" }}>
          <Icon name={d.ok ? "check-circle" : "x-circle"} size={28} style={{ color: d.ok ? "var(--green)" : "var(--red)", flexShrink: 0 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 14, fontWeight: 600 }}>
              {d.ok ? "All references resolve" : `${d.issues.length} issue${d.issues.length === 1 ? "" : "s"} blocking new sessions`}
            </div>
            <div className="muted text-sm">
              <span className="mono">GET /v1/agents/{agentId}/status</span> · last checked just now
            </div>
            {!d.ok && (
              <div className="mt-2">
                {d.issues.map((iss, i) => (
                  <div key={i} className="ref-row" style={{ borderColor: "var(--red-dim)" }}>
                    <Icon name="alert" size={12} className="ico" style={{ color: "var(--red)" }} />
                    <span className="label" style={{ color: "var(--red)" }}>{iss.kind}</span>
                    <span className="val">{iss.detail}</span>
                    <Btn size="sm" kind="ghost">Jump to fix</Btn>
                  </div>
                ))}
              </div>
            )}
          </div>
          <Btn icon="play" kind="primary" onClick={onTest}>Test agent</Btn>
        </div>
      </div>

      {/* Tabbed body */}
      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {[
            { id: "config", label: "Config", icon: "settings" },
            { id: "tools", label: "Tools", icon: "tools" },
            { id: "sessions", label: "Sessions", icon: "zap", count: onAgentSessions.length },
            { id: "metadata", label: "Metadata", icon: "doc" },
          ].map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              style={{
                background: "none",
                border: "none",
                padding: "10px 14px",
                cursor: "pointer",
                color: tab === t.id ? "var(--text)" : "var(--text-3)",
                fontSize: 12.5,
                fontWeight: tab === t.id ? 600 : 400,
                borderBottom: tab === t.id ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: -1,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <Icon name={t.icon} size={13} />
              {t.label}
              {t.count != null && t.count > 0 && <span className="count" style={{ marginLeft: 4 }}>{t.count}</span>}
            </button>
          ))}
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {tab === "config" && <ConfigPanel agentId={agentId} a={a} d={d} />}
          {tab === "tools" && <ToolsPanel d={d} pushToast={pushToast} />}
          {tab === "sessions" && <AgentSessions sessions={onAgentSessions} />}
          {tab === "metadata" && <MetadataPanel d={d} />}
        </div>
      </div>
    </div>
  );
}

function ConfigPanel({ agentId, a, d }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 280px", gap: 18, padding: 18 }}>
      <div className="col" style={{ gap: 14 }}>
        <div className="field">
          <label className="field-label">id <span className="hint">read-only</span></label>
          <div style={{ display: "flex", gap: 6 }}>
            <input className="input mono" value={agentId} readOnly style={{ flex: 1 }} />
            <button className="icon-btn"><Icon name="copy" size={12} /></button>
          </div>
        </div>
        <div className="field">
          <label className="field-label">description</label>
          <textarea className="textarea" defaultValue={d.desc} rows={2} />
        </div>
        <div className="field">
          <label className="field-label">system prompt</label>
          <textarea className="textarea mono" defaultValue={d.system_prompt} rows={6} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <div className="field">
            <label className="field-label">llm_provider_id</label>
            <select className="select mono" defaultValue={d.llm_provider_id} style={{ width: "100%" }}>
              <option>openai-1</option>
              <option>anthropic-1</option>
              <option>openai-deleted</option>
            </select>
          </div>
          <div className="field">
            <label className="field-label">model</label>
            <select className="select mono" defaultValue={d.llm_model} style={{ width: "100%" }}>
              <option>gpt-4o</option>
              <option>gpt-4o-mini</option>
              <option>claude-sonnet-4</option>
              <option>claude-haiku-4-5</option>
            </select>
          </div>
        </div>
        <div className="field">
          <label className="field-label">toolsets</label>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", padding: "4px 0" }}>
            {d.toolsets.map((t) => (
              <span key={t} className="pill" style={{ background: "var(--bg-2)", color: "var(--text-2)", border: "1px solid var(--border)", padding: "3px 8px" }}>
                <Icon name="tools" size={10} />
                <span className="mono">{t}</span>
                <Icon name="x" size={10} className="muted" style={{ cursor: "pointer", marginLeft: 2 }} />
              </span>
            ))}
            <button className="pb-add"><Icon name="plus" size={10} /> add toolset</button>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <Btn kind="ghost">Discard</Btn>
          <Btn kind="primary" icon="check">Save changes</Btn>
        </div>
      </div>

      {/* References sidebar */}
      <div className="col" style={{ gap: 10 }}>
        <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>references</div>
        <div className="ref-row">
          <Icon name="llm" size={13} className="ico" />
          <span className="label">LLM</span>
          <span className="val">{d.llm_provider_id}</span>
          {d.issues.some((i) => i.kind === "llm_provider_missing") ? (
            <span className="pill pill-failed"><span className="dot"></span>missing</span>
          ) : (
            <span className="pill pill-ended"><span className="dot"></span>ok</span>
          )}
        </div>
        {d.toolsets.map((t) => {
          const missing = d.issues.some((i) => i.target === t);
          return (
            <div key={t} className="ref-row">
              <Icon name="tools" size={13} className="ico" />
              <span className="label">Toolset</span>
              <span className="val">{t} <span className="muted">· {(TOOLSET_TOOLS[t] || []).length} tools</span></span>
              {missing ? (
                <span className="pill pill-failed"><span className="dot"></span>503</span>
              ) : (
                <span className="pill pill-ended"><span className="dot"></span>ok</span>
              )}
            </div>
          );
        })}
        <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5, marginTop: 8 }}>used in graphs</div>
        <div className="ref-row">
          <Icon name="graph" size={13} className="ico" />
          <span className="val">graph-tier1-escalation</span>
        </div>
      </div>
    </div>
  );
}

function ToolsPanel({ d, pushToast }) {
  const allTools = d.toolsets.flatMap((t) => (TOOLSET_TOOLS[t] || []).map((tool) => ({ ...tool, toolset: t })));
  const [openTool, setOpenTool] = React.useState(allTools[0] && `${allTools[0].toolset}/${allTools[0].id}`);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "260px 1fr", minHeight: 360 }}>
      <div style={{ borderRight: "1px solid var(--border)", overflow: "auto", padding: "10px 0" }}>
        <div className="muted text-sm" style={{ padding: "0 14px 8px", fontSize: 11.5 }}>
          {allTools.length} tools across {d.toolsets.length} toolset{d.toolsets.length === 1 ? "" : "s"}
        </div>
        {d.toolsets.map((ts) => (
          <div key={ts}>
            <div style={{ padding: "8px 14px 4px", fontSize: 10.5, fontWeight: 600, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{ts}</div>
            {(TOOLSET_TOOLS[ts] || []).map((tool) => {
              const key = `${ts}/${tool.id}`;
              const sel = key === openTool;
              return (
                <div
                  key={tool.id}
                  onClick={() => setOpenTool(key)}
                  style={{
                    padding: "5px 14px",
                    cursor: "pointer",
                    background: sel ? "var(--accent-dim)" : undefined,
                    fontSize: 12.5,
                  }}
                >
                  <div className="mono" style={{ color: sel ? "var(--text)" : "var(--text-2)" }}>{tool.id}</div>
                  <div className="muted text-sm" style={{ fontSize: 11, marginTop: 1 }}>{tool.desc}</div>
                </div>
              );
            })}
          </div>
        ))}
      </div>
      <div style={{ padding: 18 }}>
        <ToolDetail allTools={allTools} key={openTool} openTool={openTool} pushToast={pushToast} />
      </div>
    </div>
  );
}

function ToolDetail({ allTools, openTool, pushToast }) {
  const tool = allTools.find((t) => `${t.toolset}/${t.id}` === openTool);
  if (!tool) return null;
  return (
    <div className="col" style={{ gap: 14 }}>
      <div>
        <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{tool.id}</div>
        <div className="muted text-sm mt-2">{tool.desc} · from <span className="mono">{tool.toolset}</span></div>
      </div>
      <div className="field">
        <label className="field-label">arguments <span className="hint">json-schema rendered</span></label>
        <div className="code-block">
{`{
  "type": "object",
  "properties": {
    "path": { "type": "string", "description": "Workspace-relative path" },
    "encoding": { "type": "string", "enum": ["utf-8", "binary"], "default": "utf-8" }
  },
  "required": ["path"]
}`}
        </div>
      </div>
      <div className="field">
        <label className="field-label">test call</label>
        <textarea className="textarea mono" rows={3} placeholder='{"path": "src/main.py"}' />
        <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
          <Btn size="sm" kind="primary" icon="play" onClick={() => pushToast({ kind: "success", title: "Tool call OK", detail: "Returned 2841 bytes in 84ms" })}>Call</Btn>
        </div>
      </div>
    </div>
  );
}

function AgentSessions({ sessions }) {
  if (sessions.length === 0) return <div style={{ padding: 24 }} className="muted">No sessions yet for this agent.</div>;
  return (
    <table className="tbl">
      <thead><tr><th>Status</th><th>Session</th><th>Workspace</th><th>Turns</th><th>Created</th></tr></thead>
      <tbody>
        {sessions.map((s) => (
          <tr key={s.id}>
            <td><StatusPill status={s.status} /></td>
            <td className="mono">{s.id}</td>
            <td className="mono muted">{s.workspace_id.slice(0, 16)}…</td>
            <td className="mono num tabular">{s.turn_count}</td>
            <td className="mono muted">{relativeTime((Date.now() - s.created_at.getTime()) / 1000)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MetadataPanel({ d }) {
  return (
    <div style={{ padding: 18 }}>
      <div className="muted text-sm mb-3">Free-form metadata. Editable.</div>
      <table className="tbl">
        <thead><tr><th>key</th><th>value</th><th style={{ width: 30 }}></th></tr></thead>
        <tbody>
          {Object.entries(d.metadata || {}).map(([k, v]) => (
            <tr key={k}>
              <td className="mono">{k}</td>
              <td className="mono">{JSON.stringify(v)}</td>
              <td style={{ textAlign: "right" }}><button className="icon-btn" style={{ width: 22, height: 22 }}><Icon name="x" size={10} /></button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-3">
        <Btn size="sm" kind="ghost" icon="plus">Add key</Btn>
      </div>
    </div>
  );
}

window.AgentsPage = AgentsPage;
window.AgentDetail = AgentDetail;
window.AGENT_DETAILS_INDEX = AGENT_DETAILS;
