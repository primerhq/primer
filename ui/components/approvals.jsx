/* global React, Icon, StatusPill, Btn, Modal, Banner, CardList, Card, BottomSheet, relativeTime, fmtDate */

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
  const { useResource, useViewport, apiFetch } = window.primerApi;
  // eslint-disable-next-line no-unused-vars
  const { isMobile } = useViewport();
  // Records sort controls. "time" | "status"; "desc" | "asc".
  const [sortBy, setSortBy] = React.useState("time");
  const [sortDir, setSortDir] = React.useState("desc");

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

  const sessionRows = (parkedSessions.data?.items ?? []);
  const chatRows = (parkedChats.data?.items ?? []).filter((c) => c.parked_status === "parked");

  // The records list is built by combining per-row tool_approval/pending
  // fetches. Each row renders an <AP_RecordRow> that does its own
  // useResource and renders nothing on 404. Reusing the same cache keys
  // as the ApprovalBanner (tool-approval:session:${id}) - handy for
  // invalidation.
  //
  // NOTE (backend gap): the only queryable source of approval records is
  // the set of currently-parked sessions/chats, which are all "pending".
  // Resolved (approved/rejected) records are not persisted anywhere, so
  // the live list only ever contains pending records. The view is built
  // to render any status (each record carries `status`) and to sort by
  // time + status, so it is ready for resolved records once they are
  // persisted; until then it is honestly labelled "pending only".
  return (
    <div className="col" style={{ gap: 14 }}>
      <AP_ConfigHint onNavigate={onNavigate} />
      <div className="panel">
        <div
          style={{
            display: "flex", alignItems: "center", gap: 10,
            borderBottom: "1px solid var(--border)", padding: "10px 14px",
          }}
        >
          <Icon name="list" size={14} style={{ color: "var(--text-3)" }} />
          <span style={{ fontSize: 12.5, fontWeight: 600 }}>Approval records</span>
          <div style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 8 }}>
            <span className="muted text-sm">sort</span>
            <select
              className="select"
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              style={{ fontSize: 12 }}
              data-testid="approvals-sort-by"
            >
              <option value="time">by time</option>
              <option value="status">by status</option>
            </select>
            <Btn
              size="sm"
              kind="ghost"
              icon={sortDir === "desc" ? "chevron-down" : "chevron-up"}
              onClick={() => setSortDir((d) => (d === "desc" ? "asc" : "desc"))}
              title={sortDir === "desc" ? "descending" : "ascending"}
              data-testid="approvals-sort-dir"
            >
              {sortDir === "desc" ? "newest" : "oldest"}
            </Btn>
          </div>
        </div>

        <div style={{ padding: 0 }}>
          <AP_RecordsPanel
            sessions={sessionRows}
            chats={chatRows}
            sortBy={sortBy}
            sortDir={sortDir}
            loading={(parkedSessions.loading && !parkedSessions.data) || (parkedChats.loading && !parkedChats.data)}
            error={parkedSessions.error || parkedChats.error}
            onNavigate={onNavigate}
            pushToast={pushToast}
          />
        </div>
      </div>
    </div>
  );
}

// =============================================================
// Config hint - approval configuration is per-tool, on the Tools page
// =============================================================

function AP_ConfigHint({ onNavigate }) {
  return (
    <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 10 }} data-testid="approvals-config-hint">
      <Icon name="settings" size={14} style={{ color: "var(--text-3)" }} />
      <span className="muted text-sm">
        Approval gates are configured <strong style={{ color: "var(--text)" }}>per tool</strong>.
        Add or edit one from the{" "}
        <a
          style={{ color: "var(--accent)", cursor: "pointer" }}
          onClick={() => onNavigate && onNavigate("tools")}
          data-testid="approvals-config-link"
        >
          Tools page
        </a>
        .
      </span>
    </div>
  );
}

// =============================================================
// Records view - all available approval records, sortable
// =============================================================

// Build the comparator for the records list. Time sorts on parked_at;
// status sorts on a fixed rank (pending first, then approved, rejected)
// so the most actionable rows lead. `dir` flips both.
const AP_STATUS_RANK = { pending: 0, approved: 1, rejected: 2 };

// Inline status badge for a record. Pending=amber, approved=green,
// rejected=red. Keeps the records list scannable by status.
function AP_StatusBadge({ status }) {
  const s = status || "pending";
  const color = s === "approved" ? "var(--green)" : s === "rejected" ? "var(--red)" : "var(--amber)";
  return (
    <span
      className="pill"
      style={{ background: "var(--bg-2)", color, border: "1px solid var(--border)", fontSize: 9.5 }}
      data-testid={`approval-record-status-${s}`}
    >
      <span className="dot" style={{ background: color }}></span>
      {s}
    </span>
  );
}

function AP_recordCompare(a, b, sortBy, sortDir) {
  let cmp;
  if (sortBy === "status") {
    const ra = AP_STATUS_RANK[a.status] ?? 99;
    const rb = AP_STATUS_RANK[b.status] ?? 99;
    cmp = ra - rb;
    if (cmp === 0) cmp = new Date(b.parked_at || 0) - new Date(a.parked_at || 0);
  } else {
    cmp = new Date(b.parked_at || 0) - new Date(a.parked_at || 0);
  }
  return sortDir === "asc" ? -cmp : cmp;
}

function AP_RecordsPanel({ sessions, chats, sortBy, sortDir, loading, error, onNavigate, pushToast }) {
  // Each source row resolves its own pending record asynchronously. We
  // collect them into a shared store so the panel can sort across all
  // rows. `onRecord(key, record|null)` reports a resolved record (or
  // null on 404 / non-approval park).
  const [records, setRecords] = React.useState({});
  const onRecord = React.useCallback((key, record) => {
    setRecords((prev) => {
      if (record == null) {
        if (!(key in prev)) return prev;
        const next = { ...prev };
        delete next[key];
        return next;
      }
      return { ...prev, [key]: record };
    });
  }, []);

  if (error) {
    return (
      <div className="panel-body">
        <Banner
          kind="error"
          title={error.title || "Couldn't load approval records"}
          detail={error.detail || error.message}
          requestId={error.requestId}
        />
      </div>
    );
  }
  if (loading) {
    return (
      <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>
        Loading approval records…
      </div>
    );
  }

  // Sources whose pending fetch resolved to a real approval record.
  const sources = [
    ...sessions.map((s) => ({ key: `session:${s.id}`, scope: "sessions", id: s.id })),
    ...chats.map((c) => ({ key: `chats:${c.id}`, scope: "chats", id: c.id })),
  ];
  const resolved = sources
    .map((src) => ({ src, record: records[src.key] }))
    .filter((r) => r.record)
    .sort((a, b) => AP_recordCompare(a.record, b.record, sortBy, sortDir));

  return (
    <div>
      {/* Hidden fetchers: one per parked source. They report up via
          onRecord and render nothing themselves - the sorted rows below
          are the visible surface. */}
      {sources.map((src) => (
        <AP_RecordFetcher key={src.key} recordKey={src.key} scope={src.scope} id={src.id} onRecord={onRecord} />
      ))}
      {resolved.length === 0 ? (
        <div className="empty" data-testid="approvals-records-empty">
          <div className="ico-wrap"><Icon name="check-circle" size={22} /></div>
          <div className="head">No approval records</div>
          <div className="sub">
            Pending records appear here when a tool call hits a gate. Resolved (approved/rejected)
            records are not retained. Polling every 5s across all sessions + chats.
          </div>
        </div>
      ) : (
        resolved.map(({ src, record }) => (
          <AP_RecordRow
            key={src.key}
            scope={src.scope}
            id={src.id}
            record={record}
            onNavigate={onNavigate}
            pushToast={pushToast}
          />
        ))
      )}
    </div>
  );
}

// Headless fetcher: resolves the pending record for one parked source
// and reports it (or null) to the parent. Keeps the parent's sort logic
// pure while each source still does its own poll/cache.
function AP_RecordFetcher({ recordKey, scope, id, onRecord }) {
  const { useResource, apiFetch } = window.primerApi;
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

  React.useEffect(() => {
    if (pending.error?.status === 404) { onRecord(recordKey, null); return; }
    if (!pending.data) return;
    // Default status to "pending": a parked source is awaiting a
    // decision. The field is forwarded as-is so the view renders any
    // status the backend ever supplies.
    onRecord(recordKey, { ...pending.data, status: pending.data.status || "pending" });
  }, [pending.data, pending.error?.status, recordKey]);

  return null;
}

function AP_RecordRow({ scope, id, record, onNavigate, pushToast }) {
  const { useMutation, useViewport, apiFetch } = window.primerApi;
  const { isMobile } = useViewport();
  const [rejecting, setRejecting] = React.useState(false);
  const [reason, setReason] = React.useState("");
  const [sheetOpen, setSheetOpen] = React.useState(false);

  // Reuse the same cache key as the banner so a respond from either
  // surface refetches the other.
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
        "approvals:parked-sessions",
        "approvals:parked-chats",
      ],
      onSuccess: () => pushToast && pushToast({ kind: "success", title: "Decision sent" }),
      onError: AP_toastErr(pushToast, "Respond failed"),
    },
  );

  const a = record;
  // Only pending records expose Approve/Reject; resolved records are
  // read-only (and, given the backend gap, never appear live).
  const isPending = (a.status || "pending") === "pending";
  const parkedSec = a.parked_at ? AP_ageSec(a.parked_at) : null;
  const timeoutSec = a.timeout_at ? Math.max(0, (new Date(a.timeout_at).getTime() - Date.now()) / 1000) : null;

  const onApprove = () => respond.mutate({ tool_call_id: a.tool_call_id, decision: "approved" });
  const onReject = () => {
    if (!reason.trim()) return;
    respond.mutate({ tool_call_id: a.tool_call_id, decision: "rejected", reason: reason.trim() });
    setRejecting(false);
    setReason("");
  };
  const onDeny = () => {
    // Mobile shortcut: open the inline rejection form inside the sheet.
    setRejecting(true);
  };
  const navTarget = scope === "chats" ? "chat-detail" : "session-detail";

  if (isMobile) {
    const parkedSecLabel = parkedSec != null ? `parked ${relativeTime(parkedSec)}` : null;
    return (
      <>
        <div style={{ padding: "0 var(--mobile-pad-x)" }}>
          <CardList
            items={[a]}
            empty=""
            renderCard={() => (
              <Card
                title={a.tool_name}
                subtitle={`${scope === "chats" ? "chat" : "session"} · ${id}`}
                pill={isPending ? <StatusPill status="paused" /> : <AP_StatusBadge status={a.status} />}
                meta={parkedSecLabel}
                onClick={() => setSheetOpen(true)}
              />
            )}
          />
        </div>
        <BottomSheet
          open={sheetOpen}
          onClose={() => { setSheetOpen(false); setRejecting(false); setReason(""); }}
          title="Review approval"
          footer={
            !isPending ? (
              <button
                className="btn touch-target"
                onClick={() => setSheetOpen(false)}
              >
                Close
              </button>
            ) : !rejecting ? (
              <>
                <button
                  className="btn touch-target"
                  data-testid="approval-deny"
                  onClick={onDeny}
                >
                  Deny
                </button>
                <button
                  className="btn btn-primary touch-target"
                  data-testid="approval-approve"
                  onClick={() => { onApprove(); setSheetOpen(false); }}
                >
                  Approve
                </button>
              </>
            ) : (
              <>
                <button
                  className="btn touch-target"
                  onClick={() => { setRejecting(false); setReason(""); }}
                >
                  Cancel
                </button>
                <button
                  className="btn btn-primary touch-target"
                  disabled={!reason.trim() || respond.loading}
                  onClick={() => { onReject(); setSheetOpen(false); }}
                >
                  Send rejection
                </button>
              </>
            )
          }
        >
          <div className="mono" style={{ fontWeight: 600 }}>{a.tool_name}</div>
          {a.toolset_id && <div className="muted text-sm mono">{a.toolset_id}</div>}
          {a.gate_reason && (
            <div className="muted text-sm mt-2">
              <strong style={{ color: "var(--text)" }}>Gate:</strong> {a.gate_reason}
            </div>
          )}
          <pre className="mono" style={{ whiteSpace: "pre-wrap", marginTop: 8, fontSize: 12 }}>
            {JSON.stringify(a.arguments || {}, null, 2)}
          </pre>
          {rejecting && (
            <div className="field" style={{ marginTop: 10 }}>
              <input
                className="input"
                placeholder="Reason for rejection (required)…"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                style={{ width: "100%" }}
                autoFocus
              />
            </div>
          )}
        </BottomSheet>
      </>
    );
  }

  return (
    <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--border)" }} data-testid={`approval-row-${id}`}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <Icon name="warn-circle" size={18} style={{ color: "var(--amber)", marginTop: 2 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span className="mono" style={{ fontSize: 14, fontWeight: 600 }}>{a.tool_name}</span>
            {a.toolset_id && <span className="muted text-sm mono">· {a.toolset_id}</span>}
            <AP_StatusBadge status={a.status} />
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
          {isPending && (
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
          )}
        </div>
      </div>
    </div>
  );
}

// =============================================================
// Approval configuration modal - Required/Policy/LLM create + edit.
// Surfaced per-tool from the Tools page (toolsets.jsx); the global
// Approvals page no longer carries a "Policies" tab.
// =============================================================

function AP_NewPolicyModal({ onClose, pushToast, existing }) {
  // Same modal: create (no existing, or existing with empty id) and
  // edit (existing.id set). The Tools page passes a seed row with
  // {toolset_id, tool_name, ...} but no id so the modal stays in
  // create mode while pre-filling the (toolset, tool) pair.
  const isEdit = !!(existing && existing.id);
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const [type, setType] = React.useState(existing?.approval?.type || "required");
  const [id, setId] = React.useState(existing?.id || "");
  const [toolsetId, setToolsetId] = React.useState(existing?.toolset_id || "workspaces");
  const [toolName, setToolName] = React.useState(existing?.tool_name || "");
  const [timeoutSec, setTimeoutSec] = React.useState(
    existing?.timeout_seconds != null ? String(existing.timeout_seconds) : ""
  );
  const [policyRego, setPolicyRego] = React.useState(
    existing?.approval?.policy ||
    "package primer.approval\n\ndefault required := false\n\n# Set `required = true` when the tool call must wait for a human.\nrequired { input.arguments.amount > 10000 }\n",
  );
  const [providerId, setProviderId] = React.useState(existing?.approval?.provider_id || "");
  const [model, setModel] = React.useState(existing?.approval?.model || "");
  const [prompt, setPrompt] = React.useState(existing?.approval?.prompt || "");
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
    (body) => isEdit
      ? apiFetch("PUT", `/tool_approval_policies/${encodeURIComponent(existing.id)}`, body)
      : apiFetch("POST", "/tool_approval_policies", body),
    {
      invalidates: ["approvals:policies"],
      onSuccess: () => {
        if (pushToast) pushToast({ kind: "success", title: isEdit ? "Policy updated" : "Policy created" });
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
            title: err?.title || (isEdit ? "Save failed" : "Create failed"),
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
      id: isEdit ? existing.id : id.trim(),
      toolset_id: toolsetId.trim(),
      tool_name: toolName.trim(),
      // PUT-replace: preserve the toggle state when editing; create
      // defaults to enabled.
      enabled: isEdit ? !!existing.enabled : true,
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
      title={isEdit ? `Edit policy · ${existing.id}` : "New approval policy"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn
            kind="primary"
            icon={isEdit ? "check" : "plus"}
            disabled={!canSubmit || create.loading}
            onClick={submit}
            data-testid="approval-policy-create"
          >
            {create.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save changes" : "Create policy")}
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
        <label className="field-label">id {isEdit
          ? <span className="hint">locked — id cannot change after create</span>
          : <span className="hint">unique policy identifier</span>}
        </label>
        <input
          className="input mono"
          value={id}
          onChange={(e) => setId(e.target.value)}
          style={{ width: "100%" }}
          placeholder="approve-stripe-refund"
          disabled={isEdit}
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
