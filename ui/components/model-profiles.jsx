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

// Create/edit modal. Doubles as the edit form per the console convention:
// a non-null `existing` locks the id and switches POST to PUT-replace.
function MP_ProfileModal({ open, onClose, onSaved, existing, providers, prefill }) {
  const { useMutation, apiFetch } = window.primerApi;
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
      open={open}
      onClose={onClose}
      title={isEdit ? `Edit ${existing.id}` : "New model profile"}
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
      <div className="field">
        <label>Provider</label>
        <select value={providerId} onChange={(e) => setProviderId(e.target.value)}>
          {providers.map((p) => (
            <option key={p.id} value={p.id}>{p.id} ({p.provider})</option>
          ))}
        </select>
        {errFor("provider_id") && (
          <div className="field-help warn">{errFor("provider_id")}</div>
        )}
      </div>
      <div className="field">
        <label>Model name</label>
        <input value={modelName} onChange={(e) => setModelName(e.target.value)}
          placeholder="qwen" />
        <div className="field-help">
          The provider-side wire name. Several profiles may share one -- that
          is the point of profiles.
        </div>
        {errFor("model_name") && (
          <div className="field-help warn">{errFor("model_name")}</div>
        )}
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
        <label>Reasoning</label>
        <select value={reasoning} onChange={(e) => setReasoning(e.target.value)}>
          <option value="">vendor default</option>
          {MP_REASONING_LEVELS.map((lvl) => (
            <option key={lvl} value={lvl}>{lvl}</option>
          ))}
        </select>
        <div className="field-help">
          Mapped onto each vendor's own wire shape. Not every vendor has a
          true off: the OpenAI Responses API floors at "minimal", and vLLM's
          Responses endpoint ignores the setting entirely (use an
          <code>openchat</code> provider there).
        </div>
      </div>
    </Modal>
  );
}

function ModelProfilesPage() {
  const { useResource, useMutation, useRouter, useViewport, apiFetch } =
    window.primerApi;
  const { navigate } = useRouter();
  const { isMobile } = useViewport();
  const [filterText, setFilterText] = React.useState("");
  const [filterFocused, setFilterFocused] = React.useState(false);
  const [modalOpen, setModalOpen] = React.useState(false);
  const [editing, setEditing] = React.useState(null);
  const [confirmDelete, setConfirmDelete] = React.useState(null);
  const [deleteErr, setDeleteErr] = React.useState("");

  const list = useResource(
    "model-profiles:list",
    () => apiFetch("GET", "/model_profiles?limit=200"),
    { pollMs: 5000, pauseWhile: () => filterFocused }
  );
  const providers = useResource(
    "model-profiles:providers",
    () => apiFetch("GET", "/llm_providers?limit=200"),
    { pollMs: 0 }
  );

  const del = useMutation(
    (row) => apiFetch("DELETE", `/model_profiles/${encodeURIComponent(row.id)}`),
    { invalidates: ["model-profiles:list"] }
  );

  const rows = (list.data?.items || []).filter((r) => {
    const q = filterText.trim().toLowerCase();
    if (!q) return true;
    return (r.id + " " + r.model_name + " " + r.provider_id)
      .toLowerCase().includes(q);
  });

  const doDelete = async () => {
    setDeleteErr("");
    try {
      await del.mutate(confirmDelete);
      setConfirmDelete(null);
    } catch (err) {
      // 409 means an agent still points at it. Surfaced inline in the
      // confirm dialog rather than as a toast so the operator can act on
      // the dependency without losing the dialog.
      setDeleteErr(err.detail || err.title || "Delete failed");
    }
  };

  return (
    <>
      <div className="page-header">
        <div>
          <div className="crumb">Providers / Model profiles</div>
          <h1>Model profiles</h1>
          <div className="sub">
            {rows.length} profile{rows.length === 1 ? "" : "s"} &middot; one
            model may have several
          </div>
        </div>
        <div className="header-actions">
          <Btn variant="primary" onClick={() => { setEditing(null); setModalOpen(true); }}>
            <Icon name="plus" size={12} /> New profile
          </Btn>
        </div>
      </div>

      <div className="find-bar">
        <input
          placeholder="Filter profiles..."
          value={filterText}
          onChange={(e) => setFilterText(e.target.value)}
          onFocus={() => setFilterFocused(true)}
          onBlur={() => setFilterFocused(false)}
        />
      </div>

      {list.loading && !list.data && <div className="empty">Loading...</div>}
      {list.error && (
        <div className="empty warn">
          {list.error.title || "Could not load model profiles"}
        </div>
      )}
      {!list.loading && rows.length === 0 && (
        <div className="empty">
          No model profiles yet. A profile names one model on one provider
          plus its API-level settings; an agent points at a profile id.
        </div>
      )}

      {rows.length > 0 && (isMobile ? (
        <div className="card-list">
          {rows.map((r) => (
            <div className="card" key={r.id} onClick={() => navigate(`/model-profiles/${r.id}`)}>
              <div className="card-title">{r.id}</div>
              <div className="card-sub">{r.provider_id} / {r.model_name}</div>
              <MP_ReasoningChip level={r.config?.reasoning} />
            </div>
          ))}
        </div>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Id</th><th>Provider</th><th>Model</th>
              <th>Reasoning</th><th>Context</th><th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td><code>{r.id}</code>{r.harness_id && (
                  <StatusPill tone="muted" text="harness" />
                )}</td>
                <td>{r.provider_id}</td>
                <td><code>{r.model_name}</code></td>
                <td><MP_ReasoningChip level={r.config?.reasoning} /></td>
                <td style={{ fontVariantNumeric: "tabular-nums" }}>
                  {(r.context_length || 0).toLocaleString()}
                </td>
                <td className="row-actions">
                  {/* Harness-managed rows hide mutation, mirroring the
                      backend's 409-on-public-CRUD discipline. */}
                  {!r.harness_id && (
                    <>
                      <Btn size="sm" onClick={() => { setEditing(r); setModalOpen(true); }}>
                        Edit
                      </Btn>
                      <Btn size="sm" variant="danger"
                        onClick={() => { setDeleteErr(""); setConfirmDelete(r); }}>
                        Delete
                      </Btn>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ))}

      <MP_ProfileModal
        open={modalOpen}
        existing={editing}
        providers={providers.data?.items || []}
        onClose={() => setModalOpen(false)}
        onSaved={() => list.refetch()}
      />

      <Modal
        open={confirmDelete != null}
        onClose={() => setConfirmDelete(null)}
        title="Delete model profile"
        footer={
          <>
            <Btn onClick={() => setConfirmDelete(null)}>Cancel</Btn>
            <Btn variant="danger" onClick={doDelete} disabled={del.loading}>
              Delete
            </Btn>
          </>
        }
      >
        <p>
          Delete <code>{confirmDelete?.id}</code>? Any agent that names this
          profile will fail to start until it is repointed.
        </p>
        {deleteErr && <div className="field-help warn">{deleteErr}</div>}
      </Modal>
    </>
  );
}

window.ModelProfilesPage = ModelProfilesPage;
window.MP_ProfileModal = MP_ProfileModal;
