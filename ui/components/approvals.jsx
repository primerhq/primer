/* global React, Icon, Btn, StatusPill, Modal, Banner, relativeTime, fmtDate */

const POLICIES = [
  {
    id: "approve-delete-workspace",
    type: "required",
    tool_pattern: "fs.delete | exec.shell",
    toolset_id: "_workspaces",
    description: "Require operator approval before destructive workspace ops",
    gate_reason_template: "deletion targets a non-empty workspace",
    timeout_s: 600,
    created_at_ago: 3600 * 24 * 4,
  },
  {
    id: "approve-stripe-refund",
    type: "policy",
    tool_pattern: "stripe.create_refund",
    toolset_id: "stripe-mcp",
    description: "Refunds over $100 require approval",
    policy_expr: "args.amount == null || args.amount > 10000",
    timeout_s: 900,
    created_at_ago: 3600 * 12,
  },
  {
    id: "approve-llm-judged-write",
    type: "llm",
    tool_pattern: "fs.write",
    toolset_id: "_workspaces",
    description: "LLM-judged: ask before writes outside src/",
    judge_prompt: "Decide if this write touches anything outside the src/ directory. Approve if scoped, reject otherwise.",
    timeout_s: 300,
    created_at_ago: 3600 * 2,
  },
];

const PENDING_APPROVALS = [
  {
    tool_call_id: "tc-abc7d4",
    tool_name: "delete_workspace",
    toolset_id: "_workspaces",
    arguments: { id: "ws-prod-customer-data" },
    policy_id: "approve-delete-workspace",
    approval_type: "required",
    gate_reason: "deletion targets a non-empty workspace",
    parked_at_ago: 92,
    timeout_at_in: 508,
    scope: { kind: "session", id: "sess-7f3a9c2b8d14" },
  },
  {
    tool_call_id: "tc-f2c918",
    tool_name: "stripe.create_refund",
    toolset_id: "stripe-mcp",
    arguments: { charge: "ch_3OZ4mQ", amount: 14850, reason: "duplicate" },
    policy_id: "approve-stripe-refund",
    approval_type: "policy",
    gate_reason: "amount 14850 > 10000 cents",
    parked_at_ago: 38,
    timeout_at_in: 862,
    scope: { kind: "session", id: "sess-9b2e6f1a4c87" },
  },
  {
    tool_call_id: "tc-e91d6c",
    tool_name: "fs.write",
    toolset_id: "_workspaces",
    arguments: { path: "/etc/secrets.env", content: "***" },
    policy_id: "approve-llm-judged-write",
    approval_type: "llm",
    gate_reason: "LLM judge: path '/etc/secrets.env' is outside src/",
    parked_at_ago: 14,
    timeout_at_in: 286,
    scope: { kind: "chat", id: "chat-2b8e1c" },
  },
];

function ApprovalsPage({ pushToast, onNavigate }) {
  const [tab, setTab] = React.useState("pending");
  const [showNew, setShowNew] = React.useState(false);
  const [pending, setPending] = React.useState(PENDING_APPROVALS);

  const respond = (tc, decision, reason) => {
    setPending((p) => p.filter((x) => x.tool_call_id !== tc));
    pushToast({
      kind: decision === "approved" ? "success" : "warning",
      title: `Approval ${decision}`,
      detail: `POST /tool_approval/respond → 202${reason ? ` · "${reason}"` : ""}`,
    });
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel">
        <div style={{ display: "flex", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {[
            { id: "pending", label: "Pending", icon: "warn-circle", count: pending.length },
            { id: "policies", label: "Policies", icon: "settings", count: POLICIES.length },
          ].map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              style={{
                background: "none", border: "none",
                padding: "10px 14px", cursor: "pointer",
                color: tab === t.id ? "var(--text)" : "var(--text-3)",
                fontSize: 12.5, fontWeight: tab === t.id ? 600 : 400,
                borderBottom: tab === t.id ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: -1,
                display: "inline-flex", alignItems: "center", gap: 6,
              }}
            >
              <Icon name={t.icon} size={13} />
              {t.label}
              {t.count > 0 && <span className="count" style={{ marginLeft: 4 }}>{t.count}</span>}
            </button>
          ))}
          {tab === "policies" && (
            <div style={{ marginLeft: "auto", padding: 6 }}>
              <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowNew(true)}>New policy</Btn>
            </div>
          )}
        </div>

        <div style={{ padding: 0 }}>
          {tab === "pending" && <PendingPanel pending={pending} onRespond={respond} onNavigate={onNavigate} />}
          {tab === "policies" && <PoliciesTable />}
        </div>
      </div>

      {showNew && <NewPolicyModal onClose={() => setShowNew(false)} onCreate={() => { setShowNew(false); pushToast({ kind: "success", title: "Policy created", detail: "POST /v1/tool_approval_policies → 201" }); }} />}
    </div>
  );
}

function PendingPanel({ pending, onRespond, onNavigate }) {
  if (pending.length === 0) {
    return (
      <div className="empty">
        <div className="ico-wrap"><Icon name="check-circle" size={22} /></div>
        <div className="head">No pending approvals</div>
        <div className="sub">When a tool call hits a policy gate, it'll show up here. Polling every 5s across all sessions + chats.</div>
      </div>
    );
  }
  return (
    <div>
      {pending.map((a) => <ApprovalCard key={a.tool_call_id} a={a} onRespond={onRespond} onNavigate={onNavigate} />)}
    </div>
  );
}

function ApprovalCard({ a, onRespond, onNavigate }) {
  const [rejecting, setRejecting] = React.useState(false);
  const [reason, setReason] = React.useState("");
  return (
    <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--border)" }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <Icon name="warn-circle" size={18} style={{ color: "var(--amber)", marginTop: 2 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span className="mono" style={{ fontSize: 14, fontWeight: 600 }}>{a.tool_name}</span>
            <span className="muted text-sm mono">· {a.toolset_id}</span>
            <span className="pill pill-paused" style={{ fontSize: 9.5 }}>
              <span className="dot"></span>{a.approval_type}
            </span>
            <span className="muted text-sm" style={{ marginLeft: "auto" }}>
              parked {relativeTime(a.parked_at_ago)} · timeout in {Math.floor(a.timeout_at_in / 60)}m
            </span>
          </div>
          <div className="muted text-sm mt-2">
            from <a className="mono" style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => onNavigate(a.scope.kind === "chat" ? "chats" : "session-detail", a.scope.id)}>{a.scope.kind} · {a.scope.id}</a>
            {" "}· policy <span className="mono">{a.policy_id}</span>
          </div>
          <div className="muted text-sm mt-2">
            <strong style={{ color: "var(--text)" }}>Gate:</strong> {a.gate_reason}
          </div>
          <div className="code-block mt-2" style={{ maxHeight: 100, overflow: "auto" }}>
            {JSON.stringify({ arguments: a.arguments }, null, 2)}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            {!rejecting ? (
              <>
                <Btn size="sm" kind="primary" icon="check" onClick={() => onRespond(a.tool_call_id, "approved")}>Approve</Btn>
                <Btn size="sm" kind="danger" icon="x" onClick={() => setRejecting(true)}>Reject</Btn>
              </>
            ) : (
              <>
                <input
                  className="input"
                  placeholder="Reason for rejection (required)…"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  style={{ flex: 1 }}
                  autoFocus
                />
                <Btn size="sm" kind="danger" icon="send" disabled={!reason.trim()} onClick={() => onRespond(a.tool_call_id, "rejected", reason)}>Send rejection</Btn>
                <Btn size="sm" kind="ghost" onClick={() => { setRejecting(false); setReason(""); }}>Cancel</Btn>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function PoliciesTable() {
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>ID</th>
          <th>Type</th>
          <th>Tool pattern</th>
          <th>Toolset</th>
          <th>Description</th>
          <th style={{ textAlign: "right" }}>Timeout</th>
          <th>Created</th>
        </tr>
      </thead>
      <tbody>
        {POLICIES.map((p) => (
          <tr key={p.id}>
            <td className="mono">{p.id}</td>
            <td>
              <span className="pill" style={{ background: "var(--bg-2)", color: p.type === "required" ? "var(--amber)" : p.type === "policy" ? "var(--blue)" : "var(--violet)", border: "1px solid var(--border)" }}>
                <span className="dot" style={{ background: p.type === "required" ? "var(--amber)" : p.type === "policy" ? "var(--blue)" : "var(--violet)" }}></span>
                {p.type}
              </span>
            </td>
            <td className="mono text-sm">{p.tool_pattern}</td>
            <td className="mono muted text-sm">{p.toolset_id}</td>
            <td className="muted" style={{ maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.description}</td>
            <td className="mono num tabular muted">{p.timeout_s}s</td>
            <td className="mono muted">{relativeTime(p.created_at_ago)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function NewPolicyModal({ onClose, onCreate }) {
  const [type, setType] = React.useState("required");
  const [pattern, setPattern] = React.useState("");
  const [toolset, setToolset] = React.useState("_workspaces");
  const [desc, setDesc] = React.useState("");
  const [timeout, setTimeout] = React.useState(600);
  const [policyExpr, setPolicyExpr] = React.useState("args.amount > 10000");
  const [judgePrompt, setJudgePrompt] = React.useState("");

  return (
    <Modal
      title="New approval policy"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" disabled={!pattern.trim() || !desc.trim()} onClick={onCreate}>Create policy</Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">type</label>
        <div className="chip-group">
          {[
            { v: "required", l: "Required", h: "Always ask" },
            { v: "policy", l: "Policy", h: "Code expression" },
            { v: "llm", l: "LLM-judged", h: "Ask an LLM" },
          ].map((o) => (
            <span key={o.v} className={`chip ${type === o.v ? "active" : ""}`} onClick={() => setType(o.v)} title={o.h}>{o.l}</span>
          ))}
        </div>
      </div>
      <div className="field">
        <label className="field-label">tool pattern <span className="hint">glob · e.g. fs.delete or stripe.*</span></label>
        <input className="input mono" value={pattern} onChange={(e) => setPattern(e.target.value)} style={{ width: "100%" }} placeholder="fs.delete | exec.shell" />
      </div>
      <div className="field">
        <label className="field-label">toolset</label>
        <select className="select mono" value={toolset} onChange={(e) => setToolset(e.target.value)} style={{ width: "100%" }}>
          <option>_workspaces</option><option>_system</option><option>stripe-mcp</option><option>github-mcp</option>
        </select>
      </div>
      <div className="field">
        <label className="field-label">description</label>
        <textarea className="textarea" value={desc} onChange={(e) => setDesc(e.target.value)} rows={2} />
      </div>
      {type === "policy" && (
        <div className="field">
          <label className="field-label">policy expression <span className="hint">python-like · args.* in scope</span></label>
          <textarea className="textarea mono" value={policyExpr} onChange={(e) => setPolicyExpr(e.target.value)} rows={2} style={{ fontSize: 12 }} />
        </div>
      )}
      {type === "llm" && (
        <div className="field">
          <label className="field-label">judge prompt</label>
          <textarea className="textarea" value={judgePrompt} onChange={(e) => setJudgePrompt(e.target.value)} rows={3} placeholder="Decide if this tool call is safe to proceed…" />
        </div>
      )}
      <div className="field">
        <label className="field-label">timeout (seconds)</label>
        <input className="input mono" type="number" value={timeout} onChange={(e) => setTimeout(+e.target.value)} />
      </div>
    </Modal>
  );
}

// ApprovalBanner — to be embedded on session detail or chat detail
function ApprovalBanner({ approval, onApprove, onReject }) {
  const [rejecting, setRejecting] = React.useState(false);
  const [reason, setReason] = React.useState("");
  if (!approval) return null;
  return (
    <div className="panel" style={{ borderColor: "var(--amber)", boxShadow: "0 0 0 3px var(--amber-dim)" }}>
      <div className="panel-h" style={{ background: "var(--amber-dim)" }}>
        <Icon name="warn-circle" size={14} style={{ color: "var(--amber)" }} />
        <span style={{ color: "var(--amber)" }}>Awaiting your approval for {approval.tool_name}</span>
        <span className="mono sub">· {approval.tool_call_id}</span>
        <div className="right">
          <span className="muted text-sm">policy <span className="mono">{approval.policy_id}</span> · {approval.approval_type}</span>
        </div>
      </div>
      <div className="panel-body">
        <div className="muted text-sm mb-2"><strong style={{ color: "var(--text)" }}>Gate:</strong> {approval.gate_reason}</div>
        <div className="code-block" style={{ maxHeight: 140, overflow: "auto" }}>
          {JSON.stringify({ arguments: approval.arguments }, null, 2)}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
          {!rejecting ? (
            <>
              <Btn kind="primary" icon="check" onClick={onApprove}>Approve</Btn>
              <Btn kind="danger" icon="x" onClick={() => setRejecting(true)}>Reject</Btn>
            </>
          ) : (
            <>
              <input className="input" placeholder="Reason…" value={reason} onChange={(e) => setReason(e.target.value)} style={{ flex: 1 }} autoFocus />
              <Btn kind="danger" icon="send" disabled={!reason.trim()} onClick={() => onReject(reason)}>Send rejection</Btn>
              <Btn kind="ghost" onClick={() => { setRejecting(false); setReason(""); }}>Cancel</Btn>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

window.ApprovalsPage = ApprovalsPage;
window.ApprovalBanner = ApprovalBanner;
window.PENDING_APPROVALS = PENDING_APPROVALS;
window.APPROVAL_POLICY_INDEX = POLICIES;
