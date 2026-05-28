/* global React, Icon, Btn, Modal, Banner, relativeTime, fmtDate */

// Top-level scope is shared with the babel-standalone IIFE; prefix all
// consts with AP_ to avoid clashes with other components.

const AP_INTERNAL_TOOLSETS = ["workspaces", "system", "misc", "search", "web"];

function AP_ageSec(iso) {
  if (!iso) return null;
  if (iso instanceof Date) return (Date.now() - iso.getTime()) / 1000;
  return (Date.now() - new Date(iso).getTime()) / 1000;
}

function AP_toastErr(pushToast, fallbackTitle) {
  return (err) => {
    if (typeof pushToast !== "function") return;
    pushToast({
      kind: "error",
      title: err?.title || fallbackTitle,
      detail: err?.detail || err?.message,
      requestId: err?.requestId,
    });
  };
}

// Parked-status predicate envelope shared by /sessions/find calls.
const AP_PARKED_PREDICATE = {
  kind: "predicate",
  left: { kind: "field", name: "parked_status" },
  op: "=",
  right: { kind: "value", value: "parked" },
};

function ApprovalsPage({ pushToast, onNavigate }) {
  const { useResource, apiFetch } = window.primerApi;
  const [tab, setTab] = React.useState("pending");
  const [showNew, setShowNew] = React.useState(false);

  // Parked sessions — sessions in parked_status="parked" state. Each row
  // may or may not be parked on tool_approval (could be ask_user, sleep,
  // watch_files); the per-row pending fetch (404=skip) sorts that out.
  const parkedSessions = useResource(
    "approvals:parked-sessions",
    (signal) => apiFetch(
      "POST",
      "/sessions/find",
      { predicate: AP_PARKED_PREDICATE, page: { kind: "offset", offset: 0, length: 100 } },
      { signal },
    ),
    { pollMs: 5000 },
  );

  // Chats: no /v1/chats/find route, no parked_status query param. Pull
  // the full list (limit=200 — current scale) and filter client-side.
  const parkedChats = useResource(
    "approvals:parked-chats",
    (signal) => apiFetch("GET", "/chats?limit=200", null, { signal }),
    { pollMs: 5000 },
  );

  const policies = useResource(
    "approvals:policies",
    // GET /tool_approval_policies caps limit at 200 (per openapi). Use the
    // max; the policy list is operator-curated and unlikely to exceed it.
    (signal) => apiFetch("GET", "/tool_approval_policies?limit=200", null, { signal }),
    {},
  );

  const sessionRows = (parkedSessions.data?.items ?? []);
  const chatRows = (parkedChats.data?.items ?? []).filter((c) => c.parked_status === "parked");

  // The Pending list is built by combining per-row tool_approval/pending
  // fetches. Each row renders a <PendingRow> that does its own useResource
  // and renders nothing on 404. Reusing the same cache keys as the
  // ApprovalBanner (tool-approval:session:${id}) — handy for invalidation.
  const totalParked = sessionRows.length + chatRows.length;
  const policiesCount = (policies.data?.items ?? []).length;

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel">
        <div style={{ display: "flex", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {[
            { id: "pending", label: "Pending", icon: "warn-circle", count: totalParked },
            { id: "policies", label: "Policies", icon: "settings", count: policiesCount },
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
              data-testid={`approvals-tab-${t.id}`}
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
          {tab === "pending" && (
            <AP_PendingPanel
              sessions={sessionRows}
              chats={chatRows}
              loading={(parkedSessions.loading && !parkedSessions.data) || (parkedChats.loading && !parkedChats.data)}
              error={parkedSessions.error || parkedChats.error}
              onNavigate={onNavigate}
              pushToast={pushToast}
            />
          )}
          {tab === "policies" && (
            <AP_PoliciesTable
              policies={policies.data?.items ?? []}
              loading={policies.loading && !policies.data}
              error={policies.error}
              pushToast={pushToast}
            />
          )}
        </div>
      </div>

      {showNew && (
        <AP_NewPolicyModal
          onClose={() => setShowNew(false)}
          pushToast={pushToast}
        />
      )}
    </div>
  );
}

// =============================================================
// Pending tab
// =============================================================

function AP_PendingPanel({ sessions, chats, loading, error, onNavigate, pushToast }) {
  if (error) {
    return (
      <div className="panel-body">
        <Banner
          kind="error"
          title={error.title || "Couldn't load pending approvals"}
          detail={error.detail || error.message}
          requestId={error.requestId}
        />
      </div>
    );
  }
  if (loading) {
    return (
      <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>
        Loading pending approvals…
      </div>
    );
  }
  // Per-row pending fetches render nothing on 404 (different yield tool
  // or no approval parked). The empty-state below is what shows when
  // there are zero parked sessions+chats; rows with no approval pending
  // collapse silently inside <PendingRow>.
  if (sessions.length === 0 && chats.length === 0) {
    return (
      <div className="empty" data-testid="approvals-pending-empty">
        <div className="ico-wrap"><Icon name="check-circle" size={22} /></div>
        <div className="head">No pending approvals</div>
        <div className="sub">When a tool call hits a policy gate, it'll show up here. Polling every 5s across all sessions + chats.</div>
      </div>
    );
  }
  return (
    <div>
      {sessions.map((s) => (
        <AP_PendingRow key={`session:${s.id}`} scope="sessions" id={s.id} parent={s} onNavigate={onNavigate} pushToast={pushToast} />
      ))}
      {chats.map((c) => (
        <AP_PendingRow key={`chats:${c.id}`} scope="chats" id={c.id} parent={c} onNavigate={onNavigate} pushToast={pushToast} />
      ))}
    </div>
  );
}

function AP_PendingRow({ scope, id, parent, onNavigate, pushToast }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const [rejecting, setRejecting] = React.useState(false);
  const [reason, setReason] = React.useState("");

  // Reuse the same cache key as the banner so a respond from either
  // surface refetches the other.
  const cacheKey = `tool-approval:${scope === "sessions" ? "session" : "chat"}:${id}`;
  const pending = useResource(
    cacheKey,
    (signal) => apiFetch(
      "GET",
      `/${scope}/${encodeURIComponent(id)}/tool_approval/pending`,
      null,
      { signal },
    ),
    { pollMs: 5000, deps: [id] },
  );

  const respond = useMutation(
    (body) => apiFetch(
      "POST",
      `/${scope}/${encodeURIComponent(id)}/tool_approval/respond`,
      body,
    ),
    {
      invalidates: [
        cacheKey,
        "approvals:parked-sessions",
        "approvals:parked-chats",
      ],
      onSuccess: () => pushToast && pushToast({ kind: "success", title: "Decision sent" }),
      onError: AP_toastErr(pushToast, "Respond failed"),
    },
  );

  // 404 means "not parked on approval" (could be ask_user, sleep, etc.)
  // — render nothing. Keep the row mounted so the poll runs and a new
  // approval that lands later picks up.
  if (pending.error?.status === 404) return null;
  if (!pending.data) return null;

  const a = pending.data;
  const parkedSec = a.parked_at ? AP_ageSec(a.parked_at) : null;
  const timeoutSec = a.timeout_at ? Math.max(0, (new Date(a.timeout_at).getTime() - Date.now()) / 1000) : null;

  const onApprove = () => respond.mutate({ tool_call_id: a.tool_call_id, decision: "approved" });
  const onReject = () => {
    if (!reason.trim()) return;
    respond.mutate({ tool_call_id: a.tool_call_id, decision: "rejected", reason: reason.trim() });
    setRejecting(false);
    setReason("");
  };
  const navTarget = scope === "chats" ? "chat-detail" : "session-detail";

  return (
    <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--border)" }} data-testid={`approval-row-${id}`}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <Icon name="warn-circle" size={18} style={{ color: "var(--amber)", marginTop: 2 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span className="mono" style={{ fontSize: 14, fontWeight: 600 }}>{a.tool_name}</span>
            {a.toolset_id && <span className="muted text-sm mono">· {a.toolset_id}</span>}
            {a.approval_type && (
              <span className="pill pill-paused" style={{ fontSize: 9.5 }}>
                <span className="dot"></span>{a.approval_type}
              </span>
            )}
            <span className="muted text-sm" style={{ marginLeft: "auto" }}>
              {parkedSec != null && <>parked {relativeTime(parkedSec)}</>}
              {timeoutSec != null && <> · timeout in {Math.floor(timeoutSec / 60)}m</>}
            </span>
          </div>
          <div className="muted text-sm mt-2">
            from{" "}
            <a
              className="mono"
              style={{ color: "var(--accent)", cursor: "pointer" }}
              onClick={() => onNavigate && onNavigate(navTarget, id)}
            >
              {scope === "chats" ? "chat" : "session"} · {id}
            </a>
            {a.policy_id && <> · policy <span className="mono">{a.policy_id}</span></>}
          </div>
          {a.gate_reason && (
            <div className="muted text-sm mt-2">
              <strong style={{ color: "var(--text)" }}>Gate:</strong> {a.gate_reason}
            </div>
          )}
          {a.arguments && Object.keys(a.arguments).length > 0 && (
            <div className="code-block mt-2" style={{ maxHeight: 100, overflow: "auto" }}>
              {JSON.stringify({ arguments: a.arguments }, null, 2)}
            </div>
          )}
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            {!rejecting ? (
              <>
                <Btn
                  size="sm"
                  kind="primary"
                  icon="check"
                  disabled={respond.loading}
                  onClick={onApprove}
                  data-testid="approval-approve"
                >
                  Approve
                </Btn>
                <Btn
                  size="sm"
                  kind="danger"
                  icon="x"
                  disabled={respond.loading}
                  onClick={() => setRejecting(true)}
                  data-testid="approval-reject"
                >
                  Reject
                </Btn>
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
                  data-testid="approval-reject-reason"
                />
                <Btn
                  size="sm"
                  kind="danger"
                  icon="send"
                  disabled={!reason.trim() || respond.loading}
                  onClick={onReject}
                  data-testid="approval-reject-submit"
                >
                  Send rejection
                </Btn>
                <Btn size="sm" kind="ghost" onClick={() => { setRejecting(false); setReason(""); }}>Cancel</Btn>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// =============================================================
// Policies tab
// =============================================================

function AP_PoliciesTable({ policies, loading, error, pushToast }) {
  const { useMutation, apiFetch } = window.primerApi;
  const [confirmDelete, setConfirmDelete] = React.useState(null);

  const updatePolicy = useMutation(
    ({ pid, body }) => apiFetch("PUT", `/tool_approval_policies/${encodeURIComponent(pid)}`, body),
    {
      invalidates: ["approvals:policies"],
      onSuccess: () => pushToast && pushToast({ kind: "success", title: "Policy updated" }),
      onError: AP_toastErr(pushToast, "Update failed"),
    },
  );
  const deletePolicy = useMutation(
    (pid) => apiFetch("DELETE", `/tool_approval_policies/${encodeURIComponent(pid)}`),
    {
      invalidates: ["approvals:policies"],
      onSuccess: () => pushToast && pushToast({ kind: "warning", title: "Policy deleted" }),
      onError: AP_toastErr(pushToast, "Delete failed"),
    },
  );

  if (error) {
    return (
      <div className="panel-body">
        <Banner
          kind="error"
          title={error.title || "Couldn't load policies"}
          detail={error.detail || error.message}
          requestId={error.requestId}
        />
      </div>
    );
  }
  if (loading) {
    return (
      <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>
        Loading policies…
      </div>
    );
  }
  if (policies.length === 0) {
    return (
      <div className="empty" data-testid="approvals-policies-empty">
        <div className="ico-wrap"><Icon name="settings" size={22} /></div>
        <div className="head">No approval policies yet</div>
        <div className="sub">Create one to gate tool calls (required, Rego policy, or LLM judge).</div>
      </div>
    );
  }
  const onToggle = (p) => updatePolicy.mutate({ pid: p.id, body: { ...p, enabled: !p.enabled } });

  return (
    <table className="tbl" data-testid="approvals-policies-table">
      <thead>
        <tr>
          <th>ID</th>
          <th>Type</th>
          <th>Toolset</th>
          <th>Tool</th>
          <th style={{ textAlign: "right" }}>Timeout</th>
          <th style={{ width: 90 }}>Enabled</th>
          <th style={{ width: 60, textAlign: "right" }}></th>
        </tr>
      </thead>
      <tbody>
        {policies.map((p) => {
          const type = p.approval?.type || "—";
          const typeColor = type === "required" ? "var(--amber)" : type === "policy" ? "var(--blue)" : type === "llm" ? "var(--violet)" : "var(--text-3)";
          return (
            <tr key={p.id} data-testid={`approvals-policy-row-${p.id}`}>
              <td className="mono">{p.id}</td>
              <td>
                <span className="pill" style={{ background: "var(--bg-2)", color: typeColor, border: "1px solid var(--border)" }}>
                  <span className="dot" style={{ background: typeColor }}></span>
                  {type}
                </span>
              </td>
              <td className="mono muted text-sm">{p.toolset_id}</td>
              <td className="mono text-sm">{p.tool_name}</td>
              <td className="mono num tabular muted">{p.timeout_seconds != null ? `${p.timeout_seconds}s` : "—"}</td>
              <td>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                  <input
                    type="checkbox"
                    checked={!!p.enabled}
                    disabled={updatePolicy.loading}
                    onChange={() => onToggle(p)}
                    data-testid={`approvals-policy-enabled-${p.id}`}
                  />
                  <span className="muted text-sm">{p.enabled ? "on" : "off"}</span>
                </label>
              </td>
              <td style={{ textAlign: "right", paddingRight: 12 }}>
                <Btn
                  size="sm"
                  kind="ghost"
                  icon="trash"
                  disabled={deletePolicy.loading}
                  onClick={() => setConfirmDelete(p)}
                  title="Delete policy"
                  data-testid={`approvals-policy-delete-${p.id}`}
                />
              </td>
            </tr>
          );
        })}
      </tbody>
      {confirmDelete && (
        <Modal
          title={`Delete policy ${confirmDelete.id}?`}
          danger
          onClose={() => setConfirmDelete(null)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setConfirmDelete(null)}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                onClick={() => { const id = confirmDelete.id; setConfirmDelete(null); deletePolicy.mutate(id); }}
              >
                Delete
              </Btn>
            </>
          }
        >
          This will permanently remove the policy <span className="mono">{confirmDelete.id}</span>. Sessions currently parked on this policy stay parked until decided.
        </Modal>
      )}
    </table>
  );
}

// =============================================================
// New-policy modal — Required/Policy/LLM tabbed create
// =============================================================

function AP_NewPolicyModal({ onClose, pushToast }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const [type, setType] = React.useState("required");
  const [id, setId] = React.useState("");
  const [toolsetId, setToolsetId] = React.useState("workspaces");
  const [toolName, setToolName] = React.useState("");
  const [timeoutSec, setTimeoutSec] = React.useState("");
  const [policyRego, setPolicyRego] = React.useState(
    "package primer.approval\n\ndefault required := false\n\n# Set `required = true` when the tool call must wait for a human.\nrequired { input.arguments.amount > 10000 }\n",
  );
  const [providerId, setProviderId] = React.useState("");
  const [model, setModel] = React.useState("");
  const [prompt, setPrompt] = React.useState("");
  const [fieldErrors, setFieldErrors] = React.useState({});

  // Provider dropdown source — keyed separately from the page-level
  // approvals:policies cache so the modal can reuse cached data on
  // subsequent opens without colliding with anything else.
  const providers = useResource(
    "approvals-modal:llm",
    (signal) => apiFetch("GET", "/llm_providers?limit=200", null, { signal }),
    {},
  );
  const providerItems = providers.data?.items ?? [];
  const selectedProvider = providerItems.find((p) => p.id === providerId);
  const modelOptions = selectedProvider?.models ?? [];

  // Reset model when provider changes.
  React.useEffect(() => {
    if (selectedProvider && modelOptions.length > 0 && !modelOptions.some((m) => m.name === model)) {
      setModel(modelOptions[0].name);
    }
    if (!selectedProvider) {
      setModel("");
    }
  }, [providerId]);

  const create = useMutation(
    (body) => apiFetch("POST", "/tool_approval_policies", body),
    {
      invalidates: ["approvals:policies"],
      onSuccess: () => {
        if (pushToast) pushToast({ kind: "success", title: "Policy created" });
        onClose();
      },
      onError: (err) => {
        if (err && err.status === 422 && Array.isArray(err.fieldErrors)) {
          const next = {};
          for (const fe of err.fieldErrors) next[(fe.loc || []).join(".")] = fe.msg;
          setFieldErrors(next);
        } else if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err?.title || "Create failed",
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      },
    },
  );

  const submit = async () => {
    setFieldErrors({});
    let approval;
    if (type === "required") approval = { type: "required" };
    else if (type === "policy") approval = { type: "policy", policy: policyRego };
    else approval = { type: "llm", provider_id: providerId, model, prompt };
    const body = {
      id: id.trim(),
      toolset_id: toolsetId.trim(),
      tool_name: toolName.trim(),
      enabled: true,
      approval,
      ...(timeoutSec ? { timeout_seconds: Number(timeoutSec) } : {}),
    };
    try { await create.mutate(body); } catch (_e) { /* surfaced via onError */ }
  };

  const requiredOk = id.trim() && toolsetId.trim() && toolName.trim();
  const policyOk = requiredOk && policyRego.trim().length > 0;
  const llmOk = requiredOk && providerId && model && prompt.trim().length > 0;
  const canSubmit =
    (type === "required" && requiredOk) ||
    (type === "policy" && policyOk) ||
    (type === "llm" && llmOk);

  // Render the inline error for a field path if present.
  const fieldErr = (loc) => fieldErrors[loc] ? (
    <div className="field-help" style={{ color: "var(--red)" }} data-testid={`approval-policy-err-${loc.replace(/\./g, "-")}`}>
      {fieldErrors[loc]}
    </div>
  ) : null;

  return (
    <Modal
      title="New approval policy"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn
            kind="primary"
            icon="plus"
            disabled={!canSubmit || create.loading}
            onClick={submit}
            data-testid="approval-policy-create"
          >
            {create.loading ? "Creating…" : "Create policy"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">approval type</label>
        <div className="chip-group">
          {[
            { v: "required", l: "Required", h: "Always ask the operator" },
            { v: "policy", l: "Policy (Rego)", h: "Evaluate a Rego policy" },
            { v: "llm", l: "LLM judge", h: "Ask an LLM" },
          ].map((o) => (
            <span
              key={o.v}
              className={`chip ${type === o.v ? "active" : ""}`}
              onClick={() => setType(o.v)}
              title={o.h}
              data-testid={`approval-policy-type-${o.v}`}
            >
              {o.l}
            </span>
          ))}
        </div>
        {fieldErr("body.approval")}
        {fieldErr("body.approval.type")}
      </div>

      <div className="field">
        <label className="field-label">id <span className="hint">unique policy identifier</span></label>
        <input
          className="input mono"
          value={id}
          onChange={(e) => setId(e.target.value)}
          style={{ width: "100%" }}
          placeholder="approve-stripe-refund"
          data-testid="approval-policy-id"
        />
        {fieldErr("body.id")}
      </div>

      <div className="field">
        <label className="field-label">toolset</label>
        <select
          className="select mono"
          value={toolsetId}
          onChange={(e) => setToolsetId(e.target.value)}
          style={{ width: "100%" }}
          data-testid="approval-policy-toolset"
        >
          {AP_INTERNAL_TOOLSETS.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <div className="field-help">
          Pick a built-in toolset, or type a user-defined toolset id below.
        </div>
        <input
          className="input mono"
          value={toolsetId}
          onChange={(e) => setToolsetId(e.target.value)}
          style={{ width: "100%", marginTop: 4 }}
          placeholder="or type a toolset id…"
        />
        {fieldErr("body.toolset_id")}
      </div>

      <div className="field">
        <label className="field-label">tool name</label>
        <input
          className="input mono"
          value={toolName}
          onChange={(e) => setToolName(e.target.value)}
          style={{ width: "100%" }}
          placeholder="fs.delete"
          data-testid="approval-policy-tool"
        />
        {fieldErr("body.tool_name")}
      </div>

      <div className="field">
        <label className="field-label">timeout (seconds) <span className="hint">optional — falls back to global yield cap</span></label>
        <input
          className="input mono"
          type="number"
          min="1"
          value={timeoutSec}
          onChange={(e) => setTimeoutSec(e.target.value)}
          placeholder="600"
          data-testid="approval-policy-timeout"
        />
        {fieldErr("body.timeout_seconds")}
      </div>

      {type === "policy" && (
        <div className="field">
          <label className="field-label">
            policy (Rego)
            <span className="hint">must set <span className="mono">required</span> boolean</span>
          </label>
          <textarea
            className="textarea mono"
            value={policyRego}
            onChange={(e) => setPolicyRego(e.target.value)}
            rows={10}
            style={{ width: "100%", fontSize: 12 }}
            data-testid="approval-policy-rego"
          />
          {fieldErr("body.approval.policy")}
        </div>
      )}

      {type === "llm" && (
        <>
          <div className="field">
            <label className="field-label">provider</label>
            {providers.loading && !providers.data ? (
              <div className="field-help muted">Loading providers…</div>
            ) : providerItems.length === 0 ? (
              <div className="field-help warn">
                No LLM providers configured yet. Create one under <span className="mono">/providers/llm</span>.
              </div>
            ) : (
              <select
                className="select mono"
                value={providerId}
                onChange={(e) => setProviderId(e.target.value)}
                style={{ width: "100%" }}
                data-testid="approval-policy-provider"
              >
                <option value="">— select provider —</option>
                {providerItems.map((p) => (
                  <option key={p.id} value={p.id}>{p.id} ({p.provider})</option>
                ))}
              </select>
            )}
            {fieldErr("body.approval.provider_id")}
          </div>
          <div className="field">
            <label className="field-label">model</label>
            <select
              className="select mono"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              style={{ width: "100%" }}
              disabled={!selectedProvider}
              data-testid="approval-policy-model"
            >
              <option value="">— select model —</option>
              {modelOptions.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name}{m.context_length ? ` · ${m.context_length} ctx` : ""}
                </option>
              ))}
            </select>
            {fieldErr("body.approval.model")}
          </div>
          <div className="field">
            <label className="field-label">judge prompt</label>
            <textarea
              className="textarea"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={5}
              placeholder="Decide if this tool call is safe to proceed…"
              data-testid="approval-policy-prompt"
            />
            {fieldErr("body.approval.prompt")}
          </div>
        </>
      )}
    </Modal>
  );
}

// =============================================================
// ApprovalBanner — embedded in session-detail.jsx + chats.jsx
// =============================================================

function ApprovalBanner({ data, scope, id, pushToast }) {
  const { useMutation, apiFetch } = window.primerApi;
  const [rejecting, setRejecting] = React.useState(false);
  const [reason, setReason] = React.useState("");

  const cacheKey = `tool-approval:${scope === "sessions" ? "session" : "chat"}:${id}`;
  const respond = useMutation(
    (body) => apiFetch(
      "POST",
      `/${scope}/${encodeURIComponent(id)}/tool_approval/respond`,
      body,
    ),
    {
      invalidates: [
        cacheKey,
        scope === "sessions" ? `session-detail:${id}` : null,
        "approvals:parked-sessions",
        "approvals:parked-chats",
      ].filter(Boolean),
      onSuccess: () => pushToast && pushToast({ kind: "success", title: "Decision sent" }),
      onError: AP_toastErr(pushToast, "Respond failed"),
    },
  );

  if (!data) return null;
  const onApprove = () => respond.mutate({ tool_call_id: data.tool_call_id, decision: "approved" });
  const onReject = () => {
    if (!reason.trim()) return;
    respond.mutate({ tool_call_id: data.tool_call_id, decision: "rejected", reason: reason.trim() });
    setRejecting(false);
    setReason("");
  };

  return (
    <div
      className="panel"
      style={{ borderColor: "var(--amber)", boxShadow: "0 0 0 3px var(--amber-dim)" }}
      data-testid="approval-banner"
    >
      <div className="panel-h" style={{ background: "var(--amber-dim)" }}>
        <Icon name="warn-circle" size={14} style={{ color: "var(--amber)" }} />
        <span style={{ color: "var(--amber)" }}>Awaiting your approval for {data.tool_name}</span>
        <span className="mono sub">· {data.tool_call_id}</span>
        <div className="right">
          <span className="muted text-sm">
            {data.policy_id && <>policy <span className="mono">{data.policy_id}</span></>}
            {data.approval_type && <> · {data.approval_type}</>}
          </span>
        </div>
      </div>
      <div className="panel-body">
        {data.gate_reason && (
          <div className="muted text-sm mb-2">
            <strong style={{ color: "var(--text)" }}>Gate:</strong> {data.gate_reason}
          </div>
        )}
        {data.arguments && Object.keys(data.arguments).length > 0 && (
          <div className="code-block" style={{ maxHeight: 140, overflow: "auto" }}>
            {JSON.stringify({ arguments: data.arguments }, null, 2)}
          </div>
        )}
        <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
          {!rejecting ? (
            <>
              <Btn
                kind="primary"
                icon="check"
                disabled={respond.loading}
                onClick={onApprove}
                data-testid="approval-banner-approve"
              >
                Approve
              </Btn>
              <Btn
                kind="danger"
                icon="x"
                disabled={respond.loading}
                onClick={() => setRejecting(true)}
                data-testid="approval-banner-reject"
              >
                Reject
              </Btn>
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
                data-testid="approval-banner-reason"
              />
              <Btn
                kind="danger"
                icon="send"
                disabled={!reason.trim() || respond.loading}
                onClick={onReject}
                data-testid="approval-banner-reject-submit"
              >
                Send rejection
              </Btn>
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
