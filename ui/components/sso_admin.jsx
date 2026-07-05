/* global React, Icon, Btn, Modal, Banner, confirmDialog */
// SSO Providers console page — admin OIDC-provider management + JIT
// settings (Layer 2, Spec §5). Prefix SSO_ on every top-level name: the
// server-side JSX bundle (primer.api._jsx_bundle) flattens top-level
// const/let/function into ONE shared global scope, so any duplicate name
// across files clobbers the other. Every component is published on
// window.* at the bottom.
//
// CRUD over /v1/admin/oidc-providers (require_admin — Task 3) plus a
// settings panel over /v1/admin/sso-settings (require_admin — Task 9).
// Real authorization is enforced server-side; hiding this page from
// non-admins (chrome.jsx adminOnly) is COSMETIC only.
//
// client_secret is WRITE-ONLY: the server always returns it masked
// ("**********" or null — pydantic's default SecretStr JSON dump), and
// the create/edit modal below NEVER prefills or round-trips that masked
// string. Leaving it blank on edit sends no client_secret at all — the
// server's on_pre_update hook (primer/api/routers/oidc_providers.py)
// preserves the existing secret in that case.
//
// Do NOT destructure window.primerApi at module top level (see
// providers.jsx) — read it inside each render.

// ============================================================================
// Helpers
// ============================================================================

// {code, message} out of an ApiError envelope. Mirrors ADM_extractError
// in admin_users.jsx / AT_extractError in api_tokens.jsx.
function SSO_extractError(err) {
  const env = err && err.envelope;
  const envDetail = env && env.detail;
  let code = null;
  let msg = null;
  if (envDetail && typeof envDetail === "object") {
    code = envDetail.error || envDetail.code || null;
    msg = envDetail.message || null;
  }
  if (!msg && typeof err.detail === "string") msg = err.detail;
  if (!msg) msg = (err && (err.title || err.message)) || "Request failed";
  return { code, message: msg };
}

function SSO_parseScopes(raw) {
  const parts = (raw || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return parts.length > 0 ? parts : ["openid", "email", "profile"];
}

// ============================================================================
// SSO_Toggle — mirrors CH_Toggle (channels.jsx) exactly, prefixed SSO_.
// ============================================================================

function SSO_Toggle({ checked, onChange, label, help, disabled, testid }) {
  return (
    <label
      style={{
        display: "flex", alignItems: "flex-start", gap: 10,
        cursor: disabled ? "default" : "pointer", opacity: disabled ? 0.5 : 1,
      }}
    >
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        data-testid={testid}
        onClick={() => !disabled && onChange(!checked)}
        style={{
          flex: "0 0 auto", width: 34, height: 20, borderRadius: 999,
          border: "1px solid var(--border)", padding: 0, marginTop: 1,
          background: checked ? "var(--accent)" : "var(--bg-2)",
          position: "relative", cursor: disabled ? "default" : "pointer",
          transition: "background 0.12s ease",
        }}
      >
        <span
          style={{
            position: "absolute", top: 1, left: checked ? 15 : 1,
            width: 16, height: 16, borderRadius: "50%",
            background: checked ? "var(--accent-fg)" : "var(--text-3)",
            transition: "left 0.12s ease",
          }}
        />
      </button>
      <span style={{ fontSize: 12.5, lineHeight: 1.4 }}>
        {label}
        {help && <span className="muted"> — {help}</span>}
      </span>
    </label>
  );
}

// ============================================================================
// SSO_ProvidersPage — settings panel + providers table + entry points
// ============================================================================

function SSO_ProvidersPage() {
  const { useResource, apiFetch } = window.primerApi;
  const [createOpen, setCreateOpen] = React.useState(false);
  const [editProvider, setEditProvider] = React.useState(null);   // provider | null
  const [deleteProvider, setDeleteProvider] = React.useState(null); // provider | null

  const list = useResource(
    "sso-providers:list",
    (signal) => apiFetch("GET", "/admin/oidc-providers", null, { signal }),
    { pollMs: 10000 },
  );

  const items = list.data?.items ?? [];

  return (
    <div className="col" style={{ gap: 14 }}>
      <SSO_SettingsPanel />

      <div className="filter-bar">
        <span style={{ fontSize: 13, fontWeight: 600 }}>OIDC providers</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          <Btn
            size="sm"
            kind="primary"
            icon="plus"
            data-testid="create-sso-provider-btn"
            onClick={() => setCreateOpen(true)}
          >
            Add provider
          </Btn>
        </div>
      </div>

      {list.loading && items.length === 0 && (
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      )}
      {list.error && items.length === 0 && (
        <Banner
          kind="error"
          title={list.error.title || "Couldn't load OIDC providers"}
          detail={list.error.detail || list.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
        />
      )}
      {!list.loading && !list.error && items.length === 0 && (
        <div className="empty" style={{ padding: "40px 20px" }}>
          <div className="ico-wrap"><Icon name="key" size={22} /></div>
          <div className="head">No SSO providers yet</div>
          <div className="sub">Add an OIDC provider to enable "Sign in with…" on the login screen.</div>
          <div className="actions">
            <Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>Add provider</Btn>
          </div>
        </div>
      )}

      {items.length > 0 && (
        <div
          data-testid="sso-providers-table"
          className="panel"
          style={{ padding: 0, overflow: "hidden" }}
        >
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Name</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Client ID</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Secret</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Scopes</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Status</th>
                <th style={{ textAlign: "right", padding: "8px 12px" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p) => (
                <SSO_ProviderRow
                  key={p.id}
                  provider={p}
                  onEdit={() => setEditProvider(p)}
                  onDelete={() => setDeleteProvider(p)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {createOpen && (
        <SSO_CreateProviderDialog
          onClose={() => setCreateOpen(false)}
          onCreated={() => { setCreateOpen(false); list.refetch(); }}
        />
      )}
      {editProvider && (
        <SSO_EditProviderDialog
          provider={editProvider}
          onClose={() => setEditProvider(null)}
          onSaved={() => { setEditProvider(null); list.refetch(); }}
        />
      )}
      {deleteProvider && (
        <SSO_DeleteProviderDialog
          provider={deleteProvider}
          onClose={() => setDeleteProvider(null)}
          onDeleted={() => { setDeleteProvider(null); list.refetch(); }}
        />
      )}
    </div>
  );
}

// ============================================================================
// SSO_ProviderRow — one row of the providers table.
// ============================================================================

function SSO_ProviderRow({ provider, onEdit, onDelete }) {
  const hasSecret = !!provider.client_secret;
  return (
    <tr
      data-testid={`sso-provider-row-${provider.id}`}
      style={{ borderTop: "1px solid var(--border)" }}
    >
      <td style={{ padding: "8px 12px", fontWeight: 600 }}>{provider.name}</td>
      <td style={{ padding: "8px 12px" }}>
        <span className="mono">{provider.client_id}</span>
      </td>
      <td style={{ padding: "8px 12px" }}>
        {hasSecret
          ? <span className="pill pill-claimed" style={{ fontSize: 10.5 }}>configured</span>
          : <span className="muted text-sm">not set</span>}
      </td>
      <td style={{ padding: "8px 12px" }}>
        <span className="mono muted text-sm">{(provider.scopes || []).join(", ")}</span>
      </td>
      <td style={{ padding: "8px 12px" }}>
        {provider.enabled
          ? <span className="pill pill-claimed" style={{ fontSize: 10.5 }}>enabled</span>
          : <span className="pill pill-failed" style={{ fontSize: 10.5 }}>disabled</span>}
      </td>
      <td style={{ padding: "8px 12px", textAlign: "right", whiteSpace: "nowrap" }}>
        <Btn
          size="sm"
          kind="ghost"
          icon="edit"
          onClick={onEdit}
          data-testid={`edit-sso-provider-btn-${provider.id}`}
        >
          Edit
        </Btn>
        <Btn
          size="sm"
          kind="danger"
          icon="trash"
          onClick={onDelete}
          data-testid={`delete-sso-provider-btn-${provider.id}`}
          style={{ marginLeft: 6 }}
        >
          Delete
        </Btn>
      </td>
    </tr>
  );
}

// ============================================================================
// SSO_ProviderFields — shared field set for create + edit modals.
// ============================================================================

function SSO_ProviderFields({
  idValue, onId, idLocked,
  name, onName,
  discoveryUrl, onDiscoveryUrl,
  clientId, onClientId,
  clientSecret, onClientSecret, secretHint,
  scopes, onScopes,
  enabled, onEnabled,
  fieldErrors,
}) {
  return (
    <>
      <div className="field">
        <label className="field-label" htmlFor="sso-id">id {idLocked
          ? <span className="hint">locked — id cannot change after create</span>
          : <span className="hint">auto-generated if blank</span>}
        </label>
        <input
          id="sso-id"
          className="input mono"
          placeholder="auto-generated"
          value={idValue}
          onChange={(e) => onId(e.target.value)}
          disabled={idLocked}
          style={{ width: "100%" }}
        />
        {fieldErrors["body.id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label" htmlFor="sso-name">Name</label>
        <input
          id="sso-name"
          className="input"
          value={name}
          onChange={(e) => onName(e.target.value)}
          placeholder="Okta"
          style={{ width: "100%" }}
          autoFocus
        />
        {fieldErrors["body.name"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.name"]}</div>}
      </div>
      <div className="field">
        <label className="field-label" htmlFor="sso-discovery-url">Discovery URL</label>
        <input
          id="sso-discovery-url"
          className="input mono"
          value={discoveryUrl}
          onChange={(e) => onDiscoveryUrl(e.target.value)}
          placeholder="https://issuer.example.com/.well-known/openid-configuration"
          style={{ width: "100%" }}
        />
        {fieldErrors["body.discovery_url"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.discovery_url"]}</div>}
      </div>
      <div className="field">
        <label className="field-label" htmlFor="sso-client-id">Client ID</label>
        <input
          id="sso-client-id"
          className="input mono"
          value={clientId}
          onChange={(e) => onClientId(e.target.value)}
          style={{ width: "100%" }}
        />
        {fieldErrors["body.client_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.client_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label" htmlFor="sso-client-secret">
          Client secret <span className="hint">{secretHint}</span>
        </label>
        <input
          id="sso-client-secret"
          className="input mono"
          type="password"
          value={clientSecret}
          onChange={(e) => onClientSecret(e.target.value)}
          placeholder={secretHint}
          style={{ width: "100%" }}
        />
        {fieldErrors["body.client_secret"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.client_secret"]}</div>}
      </div>
      <div className="field">
        <label className="field-label" htmlFor="sso-scopes">Scopes <span className="hint">comma-separated</span></label>
        <input
          id="sso-scopes"
          className="input mono"
          value={scopes}
          onChange={(e) => onScopes(e.target.value)}
          placeholder="openid, email, profile"
          style={{ width: "100%" }}
        />
      </div>
      <div className="field">
        <label className="row" style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
          <input type="checkbox" checked={enabled} onChange={(e) => onEnabled(e.target.checked)} />
          <span>Enabled <span className="muted text-sm">— shows on the login screen's "Sign in with…" list.</span></span>
        </label>
      </div>
    </>
  );
}

// ============================================================================
// SSO_CreateProviderDialog — POST /v1/admin/oidc-providers
// ============================================================================

function SSO_CreateProviderDialog({ onClose, onCreated }) {
  const { apiFetch } = window.primerApi;
  const [id, setId] = React.useState("");
  const [name, setName] = React.useState("");
  const [discoveryUrl, setDiscoveryUrl] = React.useState("");
  const [clientId, setClientId] = React.useState("");
  const [clientSecret, setClientSecret] = React.useState("");
  const [scopes, setScopes] = React.useState("openid, email, profile");
  const [enabled, setEnabled] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [submitError, setSubmitError] = React.useState(null);
  const [fieldErrors, setFieldErrors] = React.useState({});

  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const canSubmit = !busy && !!name.trim() && !!discoveryUrl.trim() && !!clientId.trim();

  const submit = async () => {
    setSubmitError(null);
    setFieldErrors({});
    setBusy(true);
    try {
      const body = {
        ...(id.trim() ? { id: id.trim() } : {}),
        name: name.trim(),
        discovery_url: discoveryUrl.trim(),
        client_id: clientId.trim(),
        ...(clientSecret ? { client_secret: clientSecret } : {}),
        scopes: SSO_parseScopes(scopes),
        enabled,
      };
      await apiFetch("POST", "/admin/oidc-providers", body);
      if (!mountedRef.current) return;
      onCreated && onCreated();
    } catch (err) {
      if (!mountedRef.current) return;
      if (err.status === 422 && Array.isArray(err.fieldErrors)) {
        const map = {};
        for (const fe of err.fieldErrors) map[(fe.loc || []).join(".")] = fe.msg;
        setFieldErrors(map);
      } else {
        setSubmitError(SSO_extractError(err));
      }
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  return (
    <Modal
      title="Add OIDC provider"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn
            kind="primary"
            icon="check"
            onClick={submit}
            disabled={!canSubmit}
            data-testid="create-sso-provider-submit"
          >
            {busy ? "Adding…" : "Add provider"}
          </Btn>
        </>
      }
    >
      <div data-testid="sso-create-form">
        <SSO_ProviderFields
          idValue={id} onId={setId} idLocked={false}
          name={name} onName={setName}
          discoveryUrl={discoveryUrl} onDiscoveryUrl={setDiscoveryUrl}
          clientId={clientId} onClientId={setClientId}
          clientSecret={clientSecret} onClientSecret={setClientSecret}
          secretHint="optional for public clients"
          scopes={scopes} onScopes={setScopes}
          enabled={enabled} onEnabled={setEnabled}
          fieldErrors={fieldErrors}
        />
        {submitError && (
          <Banner
            kind="error"
            title={submitError.code ? `Create failed (${submitError.code})` : "Create failed"}
            detail={submitError.message || ""}
          />
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// SSO_EditProviderDialog — PUT /v1/admin/oidc-providers/{id}
//
// client_secret starts BLANK (never prefilled with the masked
// "**********" GET/list returns — mirrors NewChannelProviderModal's
// edit-mode secret handling in channels.jsx). Leaving it blank omits
// the key from the PUT body entirely; the server's on_pre_update hook
// (primer/api/routers/oidc_providers.py) then preserves the existing
// secret rather than clearing it.
// ============================================================================

function SSO_EditProviderDialog({ provider, onClose, onSaved }) {
  const { apiFetch } = window.primerApi;
  const [name, setName] = React.useState(provider.name || "");
  const [discoveryUrl, setDiscoveryUrl] = React.useState(provider.discovery_url || "");
  const [clientId, setClientId] = React.useState(provider.client_id || "");
  const [clientSecret, setClientSecret] = React.useState("");
  const [scopes, setScopes] = React.useState((provider.scopes || []).join(", "));
  const [enabled, setEnabled] = React.useState(!!provider.enabled);
  const [busy, setBusy] = React.useState(false);
  const [submitError, setSubmitError] = React.useState(null);
  const [fieldErrors, setFieldErrors] = React.useState({});

  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const canSubmit = !busy && !!name.trim() && !!discoveryUrl.trim() && !!clientId.trim();

  const submit = async () => {
    setSubmitError(null);
    setFieldErrors({});
    setBusy(true);
    try {
      const body = {
        id: provider.id,
        name: name.trim(),
        discovery_url: discoveryUrl.trim(),
        client_id: clientId.trim(),
        // Blank == "leave unchanged" (see comment above the component).
        ...(clientSecret ? { client_secret: clientSecret } : {}),
        scopes: SSO_parseScopes(scopes),
        enabled,
      };
      await apiFetch("PUT", "/admin/oidc-providers/" + encodeURIComponent(provider.id), body);
      if (!mountedRef.current) return;
      onSaved && onSaved();
    } catch (err) {
      if (!mountedRef.current) return;
      if (err.status === 422 && Array.isArray(err.fieldErrors)) {
        const map = {};
        for (const fe of err.fieldErrors) map[(fe.loc || []).join(".")] = fe.msg;
        setFieldErrors(map);
      } else {
        setSubmitError(SSO_extractError(err));
      }
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  return (
    <Modal
      title={`Edit provider · ${provider.name}`}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn
            kind="primary"
            icon="check"
            onClick={submit}
            disabled={!canSubmit}
            data-testid="edit-sso-provider-submit"
          >
            {busy ? "Saving…" : "Save changes"}
          </Btn>
        </>
      }
    >
      <div data-testid="sso-edit-form">
        <SSO_ProviderFields
          idValue={provider.id} onId={() => {}} idLocked={true}
          name={name} onName={setName}
          discoveryUrl={discoveryUrl} onDiscoveryUrl={setDiscoveryUrl}
          clientId={clientId} onClientId={setClientId}
          clientSecret={clientSecret} onClientSecret={setClientSecret}
          secretHint="leave blank to keep the current secret"
          scopes={scopes} onScopes={setScopes}
          enabled={enabled} onEnabled={setEnabled}
          fieldErrors={fieldErrors}
        />
        {submitError && (
          <Banner
            kind="error"
            title={submitError.code ? `Save failed (${submitError.code})` : "Save failed"}
            detail={submitError.message || ""}
          />
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// SSO_DeleteProviderDialog — DELETE /v1/admin/oidc-providers/{id}
// ============================================================================

function SSO_DeleteProviderDialog({ provider, onClose, onDeleted }) {
  const { apiFetch } = window.primerApi;
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);

  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await apiFetch("DELETE", "/admin/oidc-providers/" + encodeURIComponent(provider.id));
      if (!mountedRef.current) return;
      onDeleted && onDeleted();
    } catch (err) {
      if (!mountedRef.current) return;
      setError(SSO_extractError(err));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  return (
    <Modal
      title={`Delete provider · ${provider.name}`}
      danger
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn
            kind="danger"
            icon="trash"
            onClick={submit}
            disabled={busy}
            data-testid="delete-sso-provider-confirm-btn"
          >
            {busy ? "Deleting…" : "Delete provider"}
          </Btn>
        </>
      }
    >
      <div data-testid="sso-delete-confirm">
        <p>This permanently removes the <span className="mono">{provider.name}</span> OIDC provider.</p>
        <ul>
          <li>Users who signed in via this provider keep their local account; only the SSO link is affected.</li>
          <li>The "Sign in with {provider.name}" button disappears from the login screen immediately.</li>
          <li>This action cannot be undone.</li>
        </ul>
        {error && (
          <Banner
            kind="error"
            title={error.code ? `Delete failed (${error.code})` : "Delete failed"}
            detail={error.message || ""}
          />
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// SSO_SettingsPanel — GET/PUT /v1/admin/sso-settings
//
// The two JIT-provisioning knobs: sso_jit_enabled (SSO_Toggle) and
// sso_default_access (restricted|user select). Seeded once from the
// initial fetch (seededRef guards against a poll refresh stomping an
// in-progress edit) and saved via an explicit Save button.
// ============================================================================

function SSO_SettingsPanel() {
  const { useResource, apiFetch } = window.primerApi;
  const settings = useResource(
    "sso-settings",
    (signal) => apiFetch("GET", "/admin/sso-settings", null, { signal }),
  );

  const [jitEnabled, setJitEnabled] = React.useState(false);
  const [defaultAccess, setDefaultAccess] = React.useState("restricted");
  const [busy, setBusy] = React.useState(false);
  const [saveError, setSaveError] = React.useState(null);
  const [saved, setSaved] = React.useState(false);

  const seededRef = React.useRef(false);
  React.useEffect(() => {
    if (seededRef.current) return;
    if (!settings.data) return;
    seededRef.current = true;
    setJitEnabled(!!settings.data.sso_jit_enabled);
    setDefaultAccess(settings.data.sso_default_access || "restricted");
  }, [settings.data]);

  const save = async () => {
    setBusy(true);
    setSaveError(null);
    setSaved(false);
    try {
      const body = {
        sso_jit_enabled: jitEnabled,
        sso_default_access: jitEnabled ? defaultAccess : null,
      };
      const updated = await apiFetch("PUT", "/admin/sso-settings", body);
      setJitEnabled(!!updated.sso_jit_enabled);
      setDefaultAccess(updated.sso_default_access || "restricted");
      setSaved(true);
    } catch (err) {
      setSaveError(SSO_extractError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel" data-testid="sso-settings-panel">
      <div className="panel-h"><Icon name="key" size={13} className="muted" /><span>SSO settings</span></div>
      <div className="panel-body col" style={{ gap: 12 }}>
        {settings.loading && !settings.data && (
          <div className="muted text-sm">Loading…</div>
        )}
        {settings.error && !settings.data && (
          <Banner
            kind="error"
            title={settings.error.title || "Couldn't load SSO settings"}
            detail={settings.error.detail || settings.error.message}
            actions={<Btn size="sm" icon="refresh" onClick={settings.refetch}>Retry</Btn>}
          />
        )}
        {(settings.data || seededRef.current) && (
          <>
            <SSO_Toggle
              checked={jitEnabled}
              onChange={(v) => { setJitEnabled(v); setSaved(false); }}
              label="Just-in-time provisioning"
              help="auto-create a local account the first time a not-yet-linked SSO identity signs in"
              testid="sso-jit-enabled-toggle"
            />
            {jitEnabled && (
              <div className="field" style={{ marginBottom: 0 }}>
                <label className="field-label" htmlFor="sso-default-access">Default access for new SSO users</label>
                <select
                  id="sso-default-access"
                  className="select"
                  value={defaultAccess}
                  onChange={(e) => { setDefaultAccess(e.target.value); setSaved(false); }}
                  style={{ width: 220 }}
                  data-testid="sso-default-access-select"
                >
                  <option value="restricted">restricted</option>
                  <option value="user">user</option>
                </select>
                <div className="field-help">
                  "restricted" parks new SSO users until an admin promotes them; "admin" is never allowed here.
                </div>
              </div>
            )}
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Btn
                size="sm"
                kind="primary"
                icon="check"
                onClick={save}
                disabled={busy}
                data-testid="save-sso-settings-btn"
              >
                {busy ? "Saving…" : "Save settings"}
              </Btn>
              {saved && !busy && <span className="muted text-sm">Saved.</span>}
            </div>
            {saveError && (
              <Banner
                kind="error"
                title={saveError.code ? `Save failed (${saveError.code})` : "Save failed"}
                detail={saveError.message || ""}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ============================================================================
// Exports
// ============================================================================

window.SSO_ProvidersPage = SSO_ProvidersPage;
window.SSO_ProviderRow = SSO_ProviderRow;
window.SSO_CreateProviderDialog = SSO_CreateProviderDialog;
window.SSO_EditProviderDialog = SSO_EditProviderDialog;
window.SSO_DeleteProviderDialog = SSO_DeleteProviderDialog;
window.SSO_SettingsPanel = SSO_SettingsPanel;
window.SSO_Toggle = SSO_Toggle;
