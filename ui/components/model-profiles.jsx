/* global React, Icon, Btn, Modal, StatusPill */
// Model profiles: one (provider, model) pair plus its API-level config.
//
// The entity exists so ONE model can be registered several times under one
// provider with different settings -- a reasoning and a non-reasoning
// variant of the same model, say. That is why the list groups by provider
// and shows model_name as a secondary column rather than as the identity:
// two rows sharing a model_name is the normal case here, not a duplicate.

const MP_REASONING_LEVELS = ["off", "minimal", "low", "medium", "high"];

function MP_ReasoningChip({ level }) {
  if (!level) {
    return (
      <span style={{ color: "var(--text-4)", fontSize: 11.5 }}>
        vendor default
      </span>
    );
  }
  const strong = level === "high" || level === "off";
  return (
    <span style={{
      display: "inline-block", padding: "1px 7px", borderRadius: 4,
      fontSize: 11, fontWeight: 600, letterSpacing: "0.02em",
      background: strong ? "var(--accent-dim)" : "var(--bg-2)",
      color: strong ? "var(--accent)" : "var(--text-3)",
      border: "1px solid var(--border)",
    }}>{level}</span>
  );
}

// ---------------------------------------------------------------------------
// Platform wave P1b item 1: card anatomy (screenshot 8). Platform wave P4
// wired these into provider-catalog.jsx's PC_ProfilesPanel (the only live
// mount today - see that file) and added the bound-by/unbound and
// provider-down badges below, now that the backend P2 wave actually
// serves agent_count/graph_node_count on the list route
// (api/routers/model_profiles.py's ModelProfileWithUsage) and the panel
// can join its own already-fetched provider row for reachability. The
// "overridable"/"delete blocked" rows stay static copy describing system
// behavior (the reference's own framing), not derived facts.
// ---------------------------------------------------------------------------
function MP_BoundBadge({ agentCount, graphNodeCount }) {
  const agents = agentCount || 0;
  const nodes = graphNodeCount || 0;
  const bound = agents > 0 || nodes > 0;
  const color = bound ? "var(--text-2)" : "var(--amber)";
  return (
    <span className="pill" data-testid="profile-card-bound-badge"
      style={{ color, borderColor: "var(--border)", background: "var(--bg-2)" }}>
      <span className="dot" style={{ background: color }}></span>
      {bound
        ? `bound by ${agents} agent${agents === 1 ? "" : "s"}`
          + (nodes > 0 ? ` · ${nodes} graph node${nodes === 1 ? "" : "s"}` : "")
        : "unbound"}
    </span>
  );
}

function MP_ProfileCard({ profile, onOpen, onDeleted, providerDown }) {
  const { apiFetch } = window.primerApi;
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [err, setErr] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const doDelete = async () => {
    setErr("");
    setBusy(true);
    try {
      await apiFetch("DELETE", `/model_profiles/${encodeURIComponent(profile.id)}`);
      setConfirmDelete(false);
      if (onDeleted) onDeleted();
    } catch (e) {
      // Item 3: the backend's ReferenceCheck (routers/model_profiles.py)
      // 409s with a PLAIN FORMATTED STRING in `detail` -
      // "in_use_by: N agent(s) reference '<id>' (first: '<id>')" - not a
      // structured {child_kind, count} payload (that shape only lives in
      // build_reference_block_hook's own docstring, not its actual
      // raise). It names exactly ONE blocking reference, never a full
      // list of "names" - surfaced verbatim rather than inventing a
      // names list or a JSON shape the wire does not send.
      setErr((e && e.detail) || (e && e.message) || String(e));
    } finally {
      setBusy(false);
    }
  };

  const ctxK = profile.context_length
    ? Math.round(profile.context_length / 1000) : null;
  const reasoning = profile.config && profile.config.reasoning;

  // harness_id is set on a profile a harness owns (managed lifecycle,
  // e.g. a bootstrap operator default) - the pre-P4 <ul> in
  // PC_ProfilesPanel showed a "managed by" pill instead of Delete for
  // these; that guard moves onto the card itself here so it survives
  // wherever the card is mounted next, not just that one panel.
  const harnessManaged = !!profile.harness_id;

  return (
    <div className="pc-card" data-testid={`profile-card-${profile.id}`}>
      <div className="pc-card-head">
        <span className="pc-card-title mono">{profile.id}</span>
        <span style={{ flex: 1 }} />
        <MP_BoundBadge agentCount={profile.agent_count} graphNodeCount={profile.graph_node_count} />
        {providerDown ? (
          <span className="pill" data-testid={`profile-card-provider-down-${profile.id}`}
            style={{ color: "var(--red)", borderColor: "var(--border)", background: "var(--bg-2)" }}>
            <span className="dot" style={{ background: "var(--red)" }}></span>
            provider down
          </span>
        ) : null}
      </div>
      <div className="pc-card-subtitle">
        {[profile.provider_id, profile.model_name,
          ctxK != null ? `${ctxK}k ctx` : null].filter(Boolean).join(" · ")}
      </div>
      <div className="pc-card-facts">
        <div className="pc-card-fact">
          <span className="muted">reasoning</span>
          <MP_ReasoningChip level={reasoning} />
        </div>
        <div className="pc-card-fact">
          <span className="muted">overridable</span>
          <span>at create · switch · per graph node</span>
        </div>
        <div className="pc-card-fact">
          <span className="muted">delete</span>
          <span>blocked while referenced</span>
        </div>
      </div>
      <div className="pc-card-footer">
        <Btn kind="primary" size="sm" onClick={() => onOpen && onOpen(profile)}>
          Open
        </Btn>
        <span style={{ flex: 1 }} />
        {harnessManaged ? (
          <span className="pill" title="managed by a harness">{profile.harness_id}</span>
        ) : confirmDelete ? (
          <>
            <Btn kind="danger" size="sm" disabled={busy}
              data-testid={`profile-card-delete-confirm-${profile.id}`}
              onClick={doDelete}>
              Confirm
            </Btn>
            <Btn kind="ghost" size="sm" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Btn>
          </>
        ) : (
          <Btn kind="ghost" size="sm"
            data-testid={`profile-card-delete-${profile.id}`}
            onClick={() => { setErr(""); setConfirmDelete(true); }}>
            Delete
          </Btn>
        )}
      </div>
      {err ? (
        <div className="field-help" data-testid={`profile-card-error-${profile.id}`}>
          delete blocked while referenced - {err}
        </div>
      ) : null}
    </div>
  );
}

function MP_ProfilesGrid({ profiles, onOpen, onDeleted, providerDown }) {
  const items = profiles || [];
  if (!items.length) {
    return (
      <div className="empty-state" data-testid="profiles-empty">
        <h3>No model profiles yet</h3>
        <p>Register one to give agents a named model + config to bind to.</p>
      </div>
    );
  }
  return (
    <div className="pc-card-grid" data-testid="profiles-grid">
      {items.map((p) => (
        <MP_ProfileCard key={p.id} profile={p} onOpen={onOpen} onDeleted={onDeleted}
          providerDown={providerDown} />
      ))}
    </div>
  );
}

// Create/edit modal. Doubles as the edit form per the console convention:
// a non-null `existing` locks the id and switches POST to PUT-replace.
function MP_ProfileModal({ open, onClose, onSaved, existing, providers, prefill }) {
  const { useMutation, useResource, apiFetch } = window.primerApi;
  const isEdit = existing != null;
  const [id, setId] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [providerId, setProviderId] = React.useState("");
  const [modelName, setModelName] = React.useState("");
  const [contextLength, setContextLength] = React.useState("");
  const [reasoning, setReasoning] = React.useState("");
  const [fieldErrors, setFieldErrors] = React.useState({});

  React.useEffect(() => {
    if (!open) return;
    setFieldErrors({});
    // `prefill` seeds a CREATE (from a discovery row on the provider page);
    // `existing` is an EDIT and locks the id. Neither is the other: a
    // prefilled form is still a new row the operator may rename.
    const seed = existing || prefill || null;
    setId(seed?.id || "");
    setDescription(seed?.description || "");
    setProviderId(seed?.provider_id || (providers[0]?.id || ""));
    setModelName(seed?.model_name || "");
    setContextLength(String(seed?.context_length || 128000));
    setReasoning(seed?.config?.reasoning || "");
  }, [open, existing, prefill, providers]);

  // Item 2: the two-column picker's right side needs to know whether the
  // SELECTED provider's kind is discoverable - same /_types source and
  // same discoverable gate P1a's card facts use, not a new rule.
  const types = useResource(
    "mp-modal:llm-types",
    (signal) => apiFetch("GET", "/llm_providers/_types", null, { signal }),
    { pollMs: null },
  );
  const selectedProvider = providers.find((p) => p.id === providerId);
  const discoverable = !!(selectedProvider && types.data
    && types.data[selectedProvider.provider]
    && types.data[selectedProvider.provider].discoverable);
  const discovered = useResource(
    discoverable ? `mp-modal:discovered:${providerId}` : null,
    (signal) => apiFetch(
      "GET", `/llm_providers/${encodeURIComponent(providerId)}/discovered_models`,
      null, { signal },
    ),
    { pollMs: null, deps: [providerId] },
  );
  const discoveredModels = (discoverable && discovered.data && discovered.data.models) || [];

  const save = useMutation(
    (body) => (isEdit
      ? apiFetch("PUT", `/model_profiles/${encodeURIComponent(existing.id)}`, body)
      : apiFetch("POST", "/model_profiles", body)),
    { invalidates: ["model-profiles:list"] }
  );

  const submit = async () => {
    setFieldErrors({});
    const body = {
      id: id.trim(),
      description: description.trim(),
      provider_id: providerId,
      model_name: modelName.trim(),
      context_length: Number(contextLength) || 0,
      config: reasoning ? { reasoning } : {},
    };
    try {
      await save.mutate(body);
      onSaved && onSaved();
      onClose();
    } catch (err) {
      const fe = {};
      (err.fieldErrors || []).forEach((fieldErr) => {
        fe[(fieldErr.loc || []).join(".")] = fieldErr.msg;
      });
      // The provider-exists guard returns a 422 with a `field` key rather
      // than a pydantic loc list, so surface it on the right control.
      if (err.envelope?.extensions?.field) {
        fe[`body.${err.envelope.extensions.field}`] =
          err.envelope.extensions.message || err.detail;
      }
      setFieldErrors(fe);
      if (Object.keys(fe).length === 0) throw err;
    }
  };

  if (!open) return null;
  const errFor = (name) => fieldErrors[`body.${name}`];
  return (
    <Modal
      onClose={onClose}
      title={
        <>
          {isEdit ? `Edit ${existing.id}` : "Model profile"}
          {/* Item 2: verb chip, reusing P1a's .pc-modal-chip for visual
              consistency across every create/edit modal. */}
          <span className="pc-modal-chip mono text-sm muted"
            data-testid="profile-modal-verb-chip"
            style={{ marginLeft: 10, marginBottom: 0, verticalAlign: "middle" }}>
            verb: {isEdit ? "Edit" : "Create"} Model Profile
          </span>
        </>
      }
      footer={
        <>
          <Btn onClick={onClose}>Cancel</Btn>
          <Btn variant="primary" onClick={submit} disabled={save.loading
            || !id.trim() || !providerId || !modelName.trim()}>
            {isEdit ? "Save" : "Create"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label>Id</label>
        <input value={id} disabled={isEdit}
          onChange={(e) => setId(e.target.value)}
          placeholder="gx10--qwen-fast" />
        {errFor("id") && <div className="field-help warn">{errFor("id")}</div>}
        {!isEdit && (
          <div className="field-help">
            Any id works. Migrated profiles use
            {" "}<code>&lt;provider&gt;--&lt;model&gt;</code>, but yours can
            say what it is for.
          </div>
        )}
      </div>
      <div className="field">
        <label>Description</label>
        <input value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Qwen with reasoning suppressed for cheap turns." />
        {errFor("description") && (
          <div className="field-help warn">{errFor("description")}</div>
        )}
      </div>

      {/* Item 2: two-column picker - left Provider (rows, selected
          state), right Model ("probed live" when the selected provider's
          kind is discoverable, else a free-text fallback - the SAME
          degrade the generic provider form uses, not a new rule). */}
      <div className="row" style={{ gap: 16, alignItems: "flex-start" }}>
        <div className="col" style={{ flex: 1, minWidth: 0 }}>
          <label>Provider</label>
          <div className="pc-register-panel" data-testid="profile-provider-picker"
            style={{ position: "static", boxShadow: "none", width: "100%" }}>
            {providers.map((p) => (
              <button type="button" key={p.id} className="pc-register-row"
                data-testid={`profile-provider-row-${p.id}`}
                data-selected={p.id === providerId ? "true" : "false"}
                onClick={() => setProviderId(p.id)}>
                <span>{p.id}</span>
                <span className="pc-register-annotation muted">{p.provider}</span>
              </button>
            ))}
          </div>
          {errFor("provider_id") && (
            <div className="field-help warn">{errFor("provider_id")}</div>
          )}
        </div>
        <div className="col" style={{ flex: 1, minWidth: 0 }}>
          <label>
            Model
            {discoverable ? (
              <span className="muted text-sm" style={{ marginLeft: 6 }}>probed live</span>
            ) : null}
          </label>
          {discoverable ? (
            <div className="pc-register-panel" data-testid="profile-model-picker"
              style={{ position: "static", boxShadow: "none", width: "100%" }}>
              {discoveredModels.length === 0 ? (
                <div className="pc-register-empty muted text-sm">
                  {discovered.loading ? "Probing…" : "No models discovered."}
                </div>
              ) : discoveredModels.map((m, i) => {
                const name = (m && m.name) || m;
                return (
                  <button type="button" key={i} className="pc-register-row"
                    data-testid={`profile-model-row-${name}`}
                    data-selected={name === modelName ? "true" : "false"}
                    onClick={() => setModelName(name)}>
                    <span className="mono">{name}</span>
                  </button>
                );
              })}
            </div>
          ) : (
            // Degrade path: the selected provider's kind has no live
            // discovery endpoint (routers/providers.py's discoverable
            // flag) - free text is the only honest option, matching
            // provider-form.jsx's own fallback for the same case.
            <input value={modelName} onChange={(e) => setModelName(e.target.value)}
              placeholder="qwen" data-testid="profile-model-freetext" />
          )}
          <div className="field-help">
            The provider-side wire name. Several profiles may share one -- that
            is the point of profiles.
          </div>
          {errFor("model_name") && (
            <div className="field-help warn">{errFor("model_name")}</div>
          )}
        </div>
      </div>

      <div className="field">
        <label>Context length</label>
        <input type="number" value={contextLength}
          onChange={(e) => setContextLength(e.target.value)} />
        {errFor("context_length") && (
          <div className="field-help warn">{errFor("context_length")}</div>
        )}
      </div>
      <div className="field">
        <label>Reasoning <span className="muted text-sm">vendor-neutral</span></label>
        {/* Item 2: segmented control replaces the plain <select> - same
            off/minimal/low/medium/high values, reusing the app's own
            .chip-group/.chip pattern (P1a's family chips, the Autonomous
            segment on the agent modal) rather than a fifth control shape. */}
        <div className="chip-group" data-testid="profile-reasoning-segment">
          <span className={"chip" + (reasoning === "" ? " active" : "")}
            onClick={() => setReasoning("")}>vendor default</span>
          {MP_REASONING_LEVELS.map((lvl) => (
            <span key={lvl} className={"chip" + (reasoning === lvl ? " active" : "")}
              onClick={() => setReasoning(lvl)}>{lvl}</span>
          ))}
        </div>
        <div className="field-help">
          Mapped onto each vendor's own wire shape. Not every vendor has a
          true off: the OpenAI Responses API floors at "minimal", and vLLM's
          Responses endpoint ignores the setting entirely (use an
          <code>openchat</code> provider there).
        </div>
      </div>
      {/* Item 2: footnote verbatim from the vision doc. */}
      <div className="field-help" data-testid="profile-modal-footnote">
        The unit agents bind to — overridable at session create, binding
        switch, and per graph node. Deleting is blocked while anything
        references it.
      </div>
    </Modal>
  );
}

window.MP_ProfileModal = MP_ProfileModal;
window.MP_ProfileCard = MP_ProfileCard;
window.MP_ProfilesGrid = MP_ProfilesGrid;
