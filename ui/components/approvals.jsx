/* global React, Icon, Btn, Modal, Banner */

// Top-level scope is shared with the babel-standalone IIFE; prefix all
// consts with AP_ to avoid clashes with other components.

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

// uiv2 Wave 3 (a-14 fold): the records-sheet this overlay used to host
// (sort controls, live pending merge, resolved-record retention) moved
// to the page-level DECISIONS - AUDIT table (NV_ApprovalsAudit,
// nv-platform.jsx) - same capabilities, one surface instead of two,
// and that surface can be read-only now since Approve/Reject already
// lives on the Inbox rail + session-detail's NV_DecisionCard/
// ApprovalBanner (was always redundant with those, never the only
// path to act). What's left here: the config-hint + the policy
// create/edit modal, which is this overlay's only remaining job.
function ApprovalsPage({ pushToast, startCreate }) {
  // US-011a: the standalone Tools catalog page retired; configuring a
  // gate opens AP_NewPolicyModal directly - no page to navigate to
  // anymore.
  const [configuringPolicy, setConfiguringPolicy] = React.useState(false);
  // uiv2 Wave 3 (routing fix, item 0): "New policy" now addresses this
  // overlay with section="new" (same convention as AgentsPage's
  // startCreate) instead of landing here and requiring a second click
  // on the in-page config hint.
  React.useEffect(() => {
    if (startCreate) setConfiguringPolicy(true);
  }, [startCreate]);

  return (
    <div className="col" style={{ gap: 14 }}>
      <AP_ConfigHint onConfigure={() => setConfiguringPolicy(true)} />
      {configuringPolicy && (
        <AP_NewPolicyModal
          pushToast={pushToast}
          onClose={() => setConfiguringPolicy(false)}
        />
      )}
    </div>
  );
}

// =============================================================
// Config hint - approval configuration is per-tool, via AP_NewPolicyModal
// =============================================================

function AP_ConfigHint({ onConfigure }) {
  return (
    <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 10 }} data-testid="approvals-config-hint">
      <Icon name="settings" size={14} style={{ color: "var(--text-3)" }} />
      <span className="muted text-sm">
        Approval gates are configured <strong style={{ color: "var(--text)" }}>per tool</strong>.
        {" "}
        <a
          style={{ color: "var(--accent)", cursor: "pointer" }}
          onClick={() => onConfigure && onConfigure()}
          data-testid="approvals-config-link"
        >
          Add or edit one
        </a>
        .
      </span>
    </div>
  );
}

// =============================================================
// Approval configuration modal - Required/Policy/LLM create + edit.
// Opened directly from ApprovalsPage's AP_ConfigHint link (US-011a: the
// standalone Tools catalog page retired) and from AP_PolicyDetail (uiv2
// Wave 3 routing fix). The Tool row uses the shared window.ToolPicker
// in single-select mode (uiv2 Wave 3, approved judgment call) - a
// policy gates exactly one tool, so the bespoke browser this used to
// have (plus the redundant toolset-select/tool-name-input pair right
// below it - THREE controls for two fields) collapsed into the one
// compact field the mockup specs ("Tool: workspace__write_file (the
// shared toolset__tool picker)").
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
  // Approver routing (P6): who may decide the calls this policy gates.
  // anyone (default) | roles | users; the list is comma-separated in the
  // form and an array on the wire. A policy/llm evaluation may still
  // override this per call from its verdict.
  const [apprKind, setApprKind] = React.useState(existing?.approvers?.kind || "anyone");
  const [apprList, setApprList] = React.useState(() => {
    const spec = existing?.approvers;
    if (!spec) return "";
    const arr = spec.kind === "roles" ? spec.roles : spec.users;
    return (arr || []).join(", ");
  });
  // uiv2 Wave 3 (approved judgment call): roles are a fixed, small enum
  // (primer/model/user.py's Literal["admin","user","restricted"]), so
  // "role(s)" renders as toggleable chip tokens per the mockup ("admin
  // ✓" green-tinted / "user" neutral) instead of typed free text.
  // "specific users" has no such bounded set (real usernames, not an
  // enum) and the mockup doesn't show that variant, so it keeps the
  // existing comma-separated input unchanged.
  const AP_ROLE_OPTIONS = ["admin", "user", "restricted"];
  const apprRoleSet = React.useMemo(
    () => new Set(apprList.split(",").map((s) => s.trim()).filter(Boolean)),
    [apprList],
  );
  const toggleApprRole = (role) => {
    const next = new Set(apprRoleSet);
    if (next.has(role)) next.delete(role);
    else next.add(role);
    setApprList([...next].join(", "));
  };
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

  // What a provider publishes is its ModelProfile rows, not a models[]
  // list on the provider row. The judge still takes a bare model NAME --
  // it is a direct provider call, not an agent turn, so there is no agent
  // default for a profile to override -- and the backend validates that
  // name against these same rows, so the dropdown has to derive from
  // them or it offers values the API will reject.
  const profiles = useResource(
    "approvals-modal:model-profiles",
    (signal) => apiFetch("GET", "/model_profiles?limit=200", null, { signal }),
    {},
  );
  const modelOptions = React.useMemo(() => {
    const seen = new Map();
    for (const pr of profiles.data?.items ?? []) {
      // Several profiles may name one model; the judge picks a name, so
      // list each name once.
      if (pr.provider_id === providerId && !seen.has(pr.model_name)) {
        seen.set(pr.model_name, {
          name: pr.model_name, context_length: pr.context_length,
        });
      }
    }
    return [...seen.values()];
  }, [profiles.data, providerId]);

  // Reset model when provider changes.
  React.useEffect(() => {
    if (selectedProvider && modelOptions.length > 0 && !modelOptions.some((m) => m.name === model)) {
      setModel(modelOptions[0].name);
    }
    if (!selectedProvider) {
      setModel("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providerId, modelOptions]);

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
      // PUT-replace semantics: omitting approvers on an "anyone" save
      // clears any stored routing, which is exactly what the segment
      // says it does.
      ...(apprKind !== "anyone" ? {
        approvers: {
          kind: apprKind,
          [apprKind]: apprList.split(",").map((s) => s.trim()).filter(Boolean),
        },
      } : {}),
    };
    try { await create.mutate(body); } catch (_e) { /* surfaced via onError */ }
  };

  const requiredOk = id.trim() && toolsetId.trim() && toolName.trim();
  const policyOk = requiredOk && policyRego.trim().length > 0;
  const llmOk = requiredOk && providerId && model && prompt.trim().length > 0;
  // A roles/users routing with an empty list would route to nobody
  // but admins; make the form say so instead of the server.
  const approversOk = apprKind === "anyone"
    || apprList.split(",").some((s) => s.trim());
  const canSubmit = approversOk && (
    (type === "required" && requiredOk) ||
    (type === "policy" && policyOk) ||
    (type === "llm" && llmOk));

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
        <label className="field-label">
          Tool
          <span className="hint">the shared toolset__tool picker</span>
        </label>
        <window.ToolPicker
          mode="single"
          pageSize={6}
          selected={toolsetId && toolName ? new Set([`${toolsetId}__${toolName}`]) : new Set()}
          onChange={(next) => {
            const picked = [...next][0] || "";
            // Toolset ids never contain "__" themselves (builtins are
            // `_system`/`_workspaces`/`_misc`/`_search`/`web`; the same
            // assumption the agent-tools scoped id already relies on),
            // so splitting on the first occurrence recovers both parts.
            const sep = picked.indexOf("__");
            setToolsetId(sep < 0 ? "" : picked.slice(0, sep));
            setToolName(sep < 0 ? "" : picked.slice(sep + 2));
          }}
        />
        {fieldErr("body.toolset_id")}
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

      <div className="field">
        <label className="field-label">
          who may decide
          <span className="hint">admins always may; a policy/LLM verdict can override per call</span>
        </label>
        <div className="chip-group">
          {[
            { v: "anyone", l: "Anyone", h: "Any non-restricted user" },
            { v: "roles", l: "Roles", h: "Only the listed roles" },
            { v: "users", l: "Users", h: "Only the listed usernames" },
          ].map((o) => (
            <span
              key={o.v}
              className={`chip ${apprKind === o.v ? "active" : ""}`}
              onClick={() => setApprKind(o.v)}
              title={o.h}
              data-testid={`approval-policy-approvers-${o.v}`}
            >
              {o.l}
            </span>
          ))}
        </div>
        {apprKind === "roles" && (
          <div className="chip-group" style={{ marginTop: 6 }} data-testid="approval-policy-approvers-role-chips">
            {AP_ROLE_OPTIONS.map((role) => {
              const on = apprRoleSet.has(role);
              return (
                <span
                  key={role}
                  className={`chip${on ? " active" : ""}`}
                  style={on ? { color: "var(--green)", borderColor: "var(--green)" } : undefined}
                  onClick={() => toggleApprRole(role)}
                  data-testid={`approval-policy-approvers-role-${role}`}
                >
                  {role}{on ? " ✓" : ""}
                </span>
              );
            })}
          </div>
        )}
        {apprKind === "users" && (
          <input
            className="input mono"
            style={{ width: "100%", marginTop: 6 }}
            value={apprList}
            onChange={(e) => setApprList(e.target.value)}
            placeholder="alice, bob"
            data-testid="approval-policy-approvers-list"
          />
        )}
        {fieldErr("body.approvers")}
        {fieldErr("body.approvers.kind")}
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
                No LLM providers configured yet. Create one under <span className="mono">/providers?class=llm</span>.
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
// AP_PolicyDetail - uiv2 Wave 3 routing fix (item 0): the "Open" card
// action now addresses this overlay WITH the policy's id
// (nv-platform.jsx's approvals.open), landing here instead of the
// generic records sheet. Fetches the one row and hands it to
// AP_NewPolicyModal's existing edit-mode support - the modal itself
// already had everything it needed, only the entry point was missing.
// =============================================================

function AP_PolicyDetail({ policyId, pushToast, onClose }) {
  const { useResource, apiFetch } = window.primerApi;
  const policy = useResource(
    "approvals:policy:" + policyId,
    (signal) => apiFetch("GET", "/tool_approval_policies/" + encodeURIComponent(policyId), null, { signal }),
    { pollMs: null },
  );
  if (policy.loading && !policy.data) {
    return <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>;
  }
  if (policy.error && !policy.data) {
    return (
      <Banner
        kind="error"
        title={policy.error.title || "Couldn't load policy"}
        detail={policy.error.detail || policy.error.message}
        actions={<Btn size="sm" icon="chevron-left" onClick={onClose}>Back to list</Btn>}
      />
    );
  }
  return <AP_NewPolicyModal existing={policy.data} pushToast={pushToast} onClose={onClose} />;
}

// =============================================================
// ApprovalBanner - embedded in session-detail.jsx
// =============================================================

function ApprovalBanner({ data, scope, id, pushToast }) {
  const { useMutation, apiFetch } = window.primerApi;
  const [rejecting, setRejecting] = React.useState(false);
  const [reason, setReason] = React.useState("");

  const cacheKey = `tool-approval:session:${id}`;
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
window.AP_PolicyDetail = AP_PolicyDetail;
window.ApprovalBanner = ApprovalBanner;
