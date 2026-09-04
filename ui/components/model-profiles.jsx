/* global React, Icon, Btn, Modal, StatusPill */
// Model profiles: one (provider, model) pair plus its API-level config --
// OR (01a067c4) an ORDERED pool of other "single" profiles with a routing/
// failover policy, kind="aggregated". The two kinds share this module and
// its card/grid/modal, but not a provider: an aggregated profile has no
// provider_id of its own, which is why it also gets its own provider-
// agnostic browse surface (MP_AllProfilesPanel, below) alongside the
// existing per-provider panel (provider-catalog.jsx's PC_ProfilesPanel,
// unchanged, single-kind only, scoped to one provider's own rows).
//
// The entity exists so ONE model can be registered several times under one
// provider with different settings -- a reasoning and a non-reasoning
// variant of the same model, say. That is why the list groups by provider
// and shows model_name as a secondary column rather than as the identity:
// two rows sharing a model_name is the normal case here, not a duplicate.

const MP_REASONING_LEVELS = ["off", "minimal", "low", "medium", "high"];

// Relocated from provider-aggregated-editor.jsx (deleted, 01a067c4): the
// aggregated concept moved off LLMProvider onto ModelProfile, so the
// routing/failover switches it owned move here with it. Renamed PC_ -> MP_
// for the same reason -- it was never used anywhere else (confirmed via
// grep before the move), so there is no second caller to keep the old name
// for.
function MP_Toggle({ checked, onChange, label, help, disabled, testid }) {
  return (
    <label style={{ display: "flex", alignItems: "flex-start", gap: 10,
      cursor: disabled ? "default" : "pointer", opacity: disabled ? 0.5 : 1 }}>
      <button type="button" role="switch" aria-checked={checked} disabled={disabled}
        data-testid={testid} onClick={() => !disabled && onChange(!checked)}
        style={{ flex: "0 0 auto", width: 34, height: 20, borderRadius: 999,
          border: "1px solid var(--border)", padding: 0, marginTop: 1,
          background: checked ? "var(--accent)" : "var(--bg-2)", position: "relative",
          cursor: disabled ? "default" : "pointer", transition: "background 0.12s ease" }}>
        <span style={{ position: "absolute", top: 1, left: checked ? 15 : 1, width: 16,
          height: 16, borderRadius: "50%", background: checked ? "var(--accent-fg)" : "var(--text-3)",
          transition: "left 0.12s ease" }} />
      </button>
      <span style={{ fontSize: 12.5, lineHeight: 1.4 }}>
        {label}{help && <span className="muted"> - {help}</span>}
      </span>
    </label>
  );
}

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

// Kind chip: rendered on every aggregated card so an operator scanning the
// provider-agnostic grid (mixed single + aggregated rows) can tell them
// apart at a glance without opening each one.
function MP_KindChip({ kind }) {
  if (kind !== "aggregated") return null;
  return (
    <span className="pill" data-testid="profile-card-kind-chip"
      style={{ color: "var(--accent)", borderColor: "var(--border)", background: "var(--accent-dim)" }}>
      aggregated
    </span>
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
//
// 01a067c4: also mounted from MP_AllProfilesPanel now (provider-agnostic),
// so providerDown is optional -- an aggregated row has no single provider
// to be down, and a single row reached through that panel has no
// pre-fetched provider list to join against the way the per-provider panel
// does.
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
      // names list or a JSON shape the wire does not send. 01a067c4:
      // this also covers the NEW "member of an aggregate" reference kind
      // (model_profiles.py's second ReferenceCheck) - same string shape,
      // no separate branch needed.
      setErr((e && e.detail) || (e && e.message) || String(e));
    } finally {
      setBusy(false);
    }
  };

  const isAggregated = profile.kind === "aggregated";
  const ctxK = profile.context_length
    ? Math.round(profile.context_length / 1000) : null;
  const reasoning = profile.config && profile.config.reasoning;
  const members = profile.members || [];

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
        <MP_KindChip kind={profile.kind} />
        <MP_BoundBadge agentCount={profile.agent_count} graphNodeCount={profile.graph_node_count} />
        {providerDown ? (
          <span className="pill" data-testid={`profile-card-provider-down-${profile.id}`}
            style={{ color: "var(--red)", borderColor: "var(--border)", background: "var(--bg-2)" }}>
            <span className="dot" style={{ background: "var(--red)" }}></span>
            provider down
          </span>
        ) : null}
      </div>
      <div className="pc-card-subtitle" data-testid={`profile-card-subtitle-${profile.id}`}>
        {isAggregated
          ? `${members.length} member${members.length === 1 ? "" : "s"}`
          : [profile.provider_id, profile.model_name,
              ctxK != null ? `${ctxK}k ctx` : null].filter(Boolean).join(" · ")}
      </div>
      <div className="pc-card-facts">
        {isAggregated ? (
          <>
            <div className="pc-card-fact">
              <span className="muted">members</span>
              <span className="mono text-sm" data-testid={`profile-card-members-${profile.id}`}>
                {members.length ? members.join(", ") : "(none)"}
              </span>
            </div>
            <div className="pc-card-fact">
              <span className="muted">strategy</span>
              <span>{profile.strategy || "sequential"}</span>
            </div>
          </>
        ) : (
          <div className="pc-card-fact">
            <span className="muted">reasoning</span>
            <MP_ReasoningChip level={reasoning} />
          </div>
        )}
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
//
// 01a067c4: gained a Single/Aggregated kind toggle -- but only when
// `allProfiles` is supplied. The per-provider PC_ProfilesPanel (provider-
// catalog.jsx) mounts this same component WITHOUT that prop, exactly as it
// always has (its own call site is untouched by this change, per the
// approved IA ruling: that scoped panel "stays exactly as-is"), so it
// degrades to its old single-kind-only behaviour automatically -- no
// separate code path needed. Only MP_AllProfilesPanel (below) passes
// allProfiles, which is where an aggregated profile can actually be
// created or edited.
function MP_ProfileModal({ open, onClose, onSaved, existing, providers, prefill, allProfiles }) {
  const { useMutation, useResource, apiFetch } = window.primerApi;
  const isEdit = existing != null;
  const supportsAggregated = Array.isArray(allProfiles);
  const [kind, setKind] = React.useState("single");
  const [id, setId] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [providerId, setProviderId] = React.useState("");
  const [modelName, setModelName] = React.useState("");
  const [contextLength, setContextLength] = React.useState("");
  const [reasoning, setReasoning] = React.useState("");
  const [members, setMembers] = React.useState([]);
  const [strategy, setStrategy] = React.useState("sequential");
  const [failoverPoint, setFailoverPoint] = React.useState("before_first_token");
  const [failoverOn, setFailoverOn] = React.useState("transient_and_config");
  const [fieldErrors, setFieldErrors] = React.useState({});

  React.useEffect(() => {
    if (!open) return;
    setFieldErrors({});
    // `prefill` seeds a CREATE (from a discovery row on the provider page);
    // `existing` is an EDIT and locks the id. Neither is the other: a
    // prefilled form is still a new row the operator may rename.
    const seed = existing || prefill || null;
    setKind((seed && seed.kind) || "single");
    setId(seed?.id || "");
    setDescription(seed?.description || "");
    setProviderId(seed?.provider_id || (providers[0]?.id || ""));
    setModelName(seed?.model_name || "");
    setContextLength(String(seed?.context_length || 128000));
    setReasoning(seed?.config?.reasoning || "");
    setMembers((seed && seed.members) || []);
    setStrategy((seed && seed.strategy) || "sequential");
    setFailoverPoint((seed && seed.failover_point) || "before_first_token");
    setFailoverOn((seed && seed.failover_on) || "transient_and_config");
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

  // Candidate members: SINGLE-kind profiles only (CRUD-time validation on
  // the router rejects a non-single member; offering only valid choices
  // here is a UX nicety, not the enforcement point), excluding the row
  // being edited (no self-reference) and whichever are already picked
  // (order is the failover chain -- a duplicate would be meaningless, and
  // the backend rejects it outright rather than deduping).
  const memberCandidates = (allProfiles || []).filter((p) =>
    p.kind !== "aggregated" && p.id !== id && !members.includes(p.id));
  const addMember = (pid) => { if (pid) setMembers([...members, pid]); };
  const moveMember = (i, d) => {
    const j = i + d;
    if (j < 0 || j >= members.length) return;
    const next = members.slice();
    [next[i], next[j]] = [next[j], next[i]];
    setMembers(next);
  };
  const removeMember = (i) => setMembers(members.filter((_, j) => j !== i));

  const save = useMutation(
    (body) => (isEdit
      ? apiFetch("PUT", `/model_profiles/${encodeURIComponent(existing.id)}`, body)
      : apiFetch("POST", "/model_profiles", body)),
    { invalidates: ["model-profiles:list"] }
  );

  const submit = async () => {
    setFieldErrors({});
    const body = kind === "aggregated"
      ? {
          id: id.trim(),
          description: description.trim(),
          kind: "aggregated",
          members,
          strategy,
          failover_point: failoverPoint,
          failover_on: failoverOn,
        }
      : {
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
      // The provider-exists / aggregation-validity guards return a 422
      // with a `field` key rather than a pydantic loc list, so surface it
      // on the right control (single: provider_id/model_name; aggregated:
      // members - model_profiles.py's _aggregation_error always names
      // "members" as the field, whichever of the five checks fired).
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
  const canSave = kind === "aggregated"
    ? (id.trim() && members.length >= 2)
    : (id.trim() && providerId && modelName.trim());
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
          <Btn variant="primary" onClick={submit} disabled={save.loading || !canSave}>
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

      {supportsAggregated && (
        <div className="field">
          <label>Kind</label>
          {/* Segmented control, same .chip-group pattern the reasoning
              picker below already uses -- not a third control shape.
              Locked on edit: converting an existing row's kind in place
              is a real, distinct action (a single becoming an aggregate
              member pool, or vice versa) that this modal does not model
              as an edit -- delete and recreate under the intended kind. */}
          <div className="chip-group" data-testid="profile-kind-segment">
            <span className={"chip" + (kind === "single" ? " active" : "")}
              data-testid="profile-kind-single"
              onClick={() => !isEdit && setKind("single")}>Single</span>
            <span className={"chip" + (kind === "aggregated" ? " active" : "")}
              data-testid="profile-kind-aggregated"
              onClick={() => !isEdit && setKind("aggregated")}>Aggregated</span>
          </div>
        </div>
      )}

      {kind === "aggregated" ? (
        <div className="field" data-testid="profile-aggregated-fields">
          <label className="field-label">
            Members (ordered; failover walks top to bottom)
          </label>
          {members.map((mid, i) => (
            <div key={mid} style={{ display: "flex", gap: 6, marginTop: 4, alignItems: "center" }}>
              <span className="mono muted">#{i + 1}</span>
              <span className="mono text-sm" style={{ flex: 1 }}>{mid}</span>
              <Btn size="sm" kind="ghost" onClick={() => moveMember(i, -1)} title="Up">Up</Btn>
              <Btn size="sm" kind="ghost" onClick={() => moveMember(i, 1)} title="Down">Down</Btn>
              <Btn size="sm" kind="ghost" data-testid={`profile-member-remove-${i}`}
                onClick={() => removeMember(i)} title="Remove">Remove</Btn>
            </div>
          ))}
          <div style={{ marginTop: 6, display: "flex", gap: 6 }}>
            <select className="select" data-testid="profile-member-add-select"
              value="" onChange={(e) => addMember(e.target.value)}>
              <option value="">add a member profile...</option>
              {memberCandidates.map((p) => (
                <option key={p.id} value={p.id}>{p.id}</option>
              ))}
            </select>
          </div>
          {errFor("members") && (
            <div className="field-help warn">{errFor("members")}</div>
          )}
          {members.length < 2 && (
            <div className="field-help">
              An aggregation needs at least two members.
            </div>
          )}

          <div style={{ marginTop: 14, display: "grid", gap: 8 }}>
            <MP_Toggle checked={strategy === "round_robin"}
              testid="profile-strategy-toggle"
              onChange={(on) => setStrategy(on ? "round_robin" : "sequential")}
              label="Round-robin" help="rotate the starting member per call (off = sequential)" />
            <MP_Toggle checked={failoverPoint === "mid_stream"}
              testid="profile-failover-point-toggle"
              onChange={(on) => setFailoverPoint(on ? "mid_stream" : "before_first_token")}
              label="Mid-stream failover" help="may duplicate already-shown tokens (off = before first token)" />
            <div className="field">
              <label className="field-label">Failover on</label>
              <select className="select" data-testid="profile-failover-on-select"
                value={failoverOn} onChange={(e) => setFailoverOn(e.target.value)}
                style={{ width: "100%" }}>
                <option value="transient">transient</option>
                <option value="transient_and_config">transient_and_config</option>
              </select>
            </div>
          </div>
        </div>
      ) : (
        <>
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
        </>
      )}
      {/* Item 2: footnote verbatim from the vision doc. */}
      <div className="field-help" data-testid="profile-modal-footnote">
        The unit agents bind to — overridable at session create, binding
        switch, and per graph node. Deleting is blocked while anything
        references it.
      </div>
    </Modal>
  );
}

// Provider-agnostic browse/create/edit surface for EVERY model profile,
// single and aggregated alike (01a067c4, approved IA ruling). Mounted as a
// provider-catalog "class" panel (form:"panel", same precedent as ssp/
// workspace/channel), NOT a standalone page -- tests/ui/test_model_profiles_
// page.py's ban is specifically about a separate PAGE/nav entry breaking
// the providers one-page IA doctrine; this satisfies that same doctrine
// (profiles stay reachable only from the providers page) while giving an
// aggregated profile -- which has no provider_id, so it can never appear
// in PC_ProfilesPanel's provider-scoped list -- a home the original
// per-provider design never needed to have.
function MP_AllProfilesPanel({ pushToast }) {
  const { useResource, apiFetch } = window.primerApi;
  const [open, setOpen] = React.useState(false);
  const [editing, setEditing] = React.useState(null);
  const rows = useResource(
    "mp-all:profiles",
    (signal) => apiFetch("GET", "/model_profiles?limit=200", null, { signal }),
    { pollMs: null },
  );
  const providers = useResource(
    "mp-all:llm-providers",
    (signal) => apiFetch("GET", "/llm_providers?limit=200", null, { signal }),
    { pollMs: null },
  );
  const allProfiles = (rows.data && rows.data.items) || [];
  const providerRows = (providers.data && providers.data.items) || [];

  return (
    <div className="col" style={{ gap: 8 }} data-testid="all-model-profiles">
      <div className="muted text-sm">
        Every model profile across every provider, plus aggregated pools
        of two or more profiles. An aggregated profile has no provider of
        its own, so it lives only here, not under any one provider's page.
      </div>
      <window.MP_ProfilesGrid
        profiles={allProfiles}
        onOpen={(row) => setEditing(row)}
        onDeleted={() => rows.refetch()}
      />
      <Btn icon="plus" kind="ghost" onClick={() => setOpen(true)}>New profile</Btn>
      {(open || editing) && (
        <MP_ProfileModal
          open
          existing={editing}
          providers={providerRows}
          allProfiles={allProfiles}
          onClose={() => { setOpen(false); setEditing(null); }}
          onSaved={() => rows.refetch()}
        />
      )}
    </div>
  );
}

window.MP_ProfileModal = MP_ProfileModal;
window.MP_ProfileCard = MP_ProfileCard;
window.MP_ProfilesGrid = MP_ProfilesGrid;
window.MP_AllProfilesPanel = MP_AllProfilesPanel;
