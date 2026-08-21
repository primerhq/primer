/* global React, Icon, Btn, Modal, Banner */
// Services console page — agent-published web apps served at /svc/{name}/.
// Spec: docs/superpowers/specs/2026-08-08-services-design.md section 9.
//
// Prefix SV_ to avoid global name collisions (ui-pages convention).
//
// Endpoints (all under the implicit /v1 prefix apiFetch adds):
//   GET/POST/PUT/DELETE /services            — CRUD (make_crud_router)
//   GET  /services/{id}/versions             — published versions, newest first
//   POST /services/{id}/_activate            — activate / roll back
// Publishing itself is NOT a console affordance: agents publish via the
// publish_service tool (spec phase 4); the
// Versions tab says so in its empty state.

// ============================================================================
// Helpers
// ============================================================================

function SV_extractError(err) {
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

function SV_AuthPill({ mode }) {
  const anon = mode === "none";
  return (
    <span
      className="mono"
      title={anon
        ? "Served without login; the manifest tool allowlist still applies"
        : "Console login required to view and call"}
      style={{
        fontSize: 11, padding: "1px 8px", borderRadius: 9,
        background: anon ? "var(--amber-dim)" : "var(--accent-dim)",
        color: anon ? "var(--amber)" : "var(--accent)",
      }}
    >
      {anon ? "anonymous" : "console"}
    </span>
  );
}

// ============================================================================
// SV_ServicesPage — list + detail switch (serviceId prop, like HarnessesPage)
// ============================================================================

function SV_ServicesPage({ serviceId }) {
  if (serviceId) return <SV_ServiceDetail serviceId={serviceId} />;
  return <SV_ServicesList />;
}

function SV_ServicesList() {
  const { useResource, apiFetch, useRouter } = window.primerApi;
  // Row clicks navigate; see the note on the detail view below.
  const { navigate } = useRouter();
  const [filter, setFilter] = React.useState("");
  const [creating, setCreating] = React.useState(false);

  const list = useResource(
    "services:list",
    (signal) => apiFetch("GET", "/services?limit=200", null, { signal }),
    { pollMs: 5000 },
  );
  const items = (list.data && list.data.items) || [];
  const q = filter.trim().toLowerCase();
  const shown = items.filter(
    (s) => !q || s.name.toLowerCase().includes(q)
      || (s.description || "").toLowerCase().includes(q),
  );

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <input
          className="input"
          placeholder="Filter services…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{ maxWidth: 260 }}
          data-testid="services-filter"
        />
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreating(true)} data-testid="new-service-btn">
            New service
          </Btn>
        </div>
      </div>

      {list.loading && items.length === 0 && (
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      )}
      {list.error && items.length === 0 && (
        <Banner
          kind="error"
          title={list.error.title || "Couldn't load services"}
          detail={list.error.detail || list.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
        />
      )}
      {!list.loading && !list.error && shown.length === 0 && (
        <div className="empty" style={{ padding: "40px 20px" }} data-testid="services-empty">
          <div className="ico-wrap"><Icon name="box" size={22} /></div>
          <div className="head">{q ? "No services match" : "No services yet"}</div>
          <div className="sub">
            A service is an agent-published web app served at /svc/&#123;name&#125;/.
            Create one here, then publish a bundle from an agent workspace.
          </div>
        </div>
      )}

      {shown.length > 0 && (
        <div className="tbl-wrap" data-testid="services-table">
          <table className="tbl">
            <thead>
              <tr>
                <th>Name</th>
                <th>Viewer auth</th>
                <th>Status</th>
                <th>Description</th>
                <th style={{ textAlign: "right" }}>App</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((s) => (
                <tr
                  key={s.id}
                  style={{ cursor: "pointer" }}
                  onClick={() => { navigate("/services/" + encodeURIComponent(s.id)); }}
                  data-testid={`service-row-${s.name}`}
                >
                  <td className="mono">{s.name}</td>
                  <td><SV_AuthPill mode={s.viewer_auth} /></td>
                  <td>
                    {s.active_version_id
                      ? <span style={{ color: "var(--green)" }}>serving</span>
                      : <span className="muted">unpublished</span>}
                  </td>
                  <td className="muted text-sm" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {s.description}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    {s.active_version_id && (
                      <a
                        href={"/svc/" + encodeURIComponent(s.name) + "/"}
                        target="_blank"
                        rel="noreferrer"
                        onClick={(e) => e.stopPropagation()}
                      >
                        open ↗
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {creating && (
        <SV_ServiceModal
          onClose={() => setCreating(false)}
          onSaved={() => { setCreating(false); list.refetch(); }}
        />
      )}
    </div>
  );
}

// ============================================================================
// SV_ServiceModal — create AND edit (non-null `existing` switches to PUT)
// ============================================================================

function SV_ServiceModal({ existing, onClose, onSaved }) {
  const { apiFetch } = window.primerApi;
  const [name, setName] = React.useState(existing ? existing.name : "");
  const [description, setDescription] = React.useState(existing ? existing.description : "");
  const [viewerAuth, setViewerAuth] = React.useState(existing ? existing.viewer_auth : "console");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);

  const submit = async () => {
    if (viewerAuth === "none" && (!existing || existing.viewer_auth !== "none")) {
      const ok = await window.confirmDialog({
        title: "Serve without login?",
        message: "Anonymous viewing serves this app WITHOUT login. The "
          + "manifest tool allowlist still applies to gateway calls.",
        confirmLabel: "Continue",
        danger: true,
      });
      if (!ok) return;
    }
    setBusy(true);
    setError(null);
    try {
      if (existing) {
        await apiFetch("PUT", "/services/" + encodeURIComponent(existing.id), {
          ...existing, name, description, viewer_auth: viewerAuth,
        });
      } else {
        await apiFetch("POST", "/services", {
          name, description, viewer_auth: viewerAuth,
        });
      }
      onSaved && onSaved();
    } catch (err) {
      setError(SV_extractError(err));
      setBusy(false);
    }
  };

  return (
    <Modal
      title={existing ? "Edit service" : "New service"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn kind="primary" onClick={submit} disabled={busy || !name.trim() || !description.trim()} data-testid="service-save-btn">
            {busy ? "Saving…" : existing ? "Save" : "Create"}
          </Btn>
        </>
      }
    >
      <div className="col" style={{ gap: 12 }}>
        <label className="field">
          <span className="lbl">Name</span>
          <input
            className="input mono"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="status-page"
            disabled={!!(existing && existing.active_version_id)}
            data-testid="service-name-input"
          />
          <span className="muted text-sm">
            Lowercase letters, digits, dashes. Becomes the public URL
            /svc/&#123;name&#125;/ and locks once published.
          </span>
        </label>
        <label className="field">
          <span className="lbl">Description</span>
          <input
            className="input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this app is for"
            data-testid="service-description-input"
          />
        </label>
        <label className="field">
          <span className="lbl">Viewer auth</span>
          <select
            className="input"
            value={viewerAuth}
            onChange={(e) => setViewerAuth(e.target.value)}
            data-testid="service-auth-select"
          >
            <option value="console">console — login required</option>
            <option value="none">none — anonymous viewing</option>
          </select>
        </label>
        {error && (
          <Banner
            kind="error"
            title={error.code ? `Save failed (${error.code})` : "Save failed"}
            detail={error.message || ""}
          />
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// SV_ServiceDetail — header + Config / Versions tabs (?tab= mirrored)
// ============================================================================

function SV_ServiceDetail({ serviceId }) {
  const { useResource, apiFetch, useRouter } = window.primerApi;
  // navigate, not window.location.hash: a direct hash write bypasses the
  // router shim and speaks the pre-S8 grammar, which the shell's url
  // parser does not understand, so it falls back to the default
  // workspace and rewrites the address away.
  const { query, navigate } = useRouter();
  const tab = query.tab === "versions" ? "versions" : "config";
  const [editing, setEditing] = React.useState(false);
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [error, setError] = React.useState(null);

  const res = useResource(
    "service-detail:" + serviceId,
    (signal) => apiFetch("GET", "/services/" + encodeURIComponent(serviceId), null, { signal }),
    { pollMs: 5000 },
  );
  const versions = useResource(
    "service-versions:" + serviceId,
    (signal) => apiFetch("GET", "/services/" + encodeURIComponent(serviceId) + "/versions", null, { signal }),
    { pollMs: 10000 },
  );
  const svc = res.data;
  const vitems = (versions.data && versions.data.items) || [];

  const setTab = (t) => {
    navigate("/services/" + encodeURIComponent(serviceId)
      + (t === "versions" ? "?tab=versions" : ""));
  };

  const activate = async (versionId) => {
    setError(null);
    try {
      await apiFetch("POST", "/services/" + encodeURIComponent(serviceId) + "/_activate", {
        version_id: versionId,
      });
      res.refetch();
      versions.refetch();
    } catch (err) {
      setError(SV_extractError(err));
    }
  };

  const doDelete = async () => {
    try {
      await apiFetch("DELETE", "/services/" + encodeURIComponent(serviceId));
      navigate("/services");
    } catch (err) {
      setError(SV_extractError(err));
      setConfirmDelete(false);
    }
  };

  if (res.loading && !svc) {
    return <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>;
  }
  if (res.error && !svc) {
    return (
      <Banner
        kind="error"
        title="Couldn't load service"
        detail={res.error.detail || res.error.message}
        actions={<Btn size="sm" icon="refresh" onClick={res.refetch}>Retry</Btn>}
      />
    );
  }
  if (!svc) return null;
  const managed = !!svc.harness_id;

  return (
    <div className="col" style={{ gap: 14 }} data-testid="service-detail">
      {managed && (
        <Banner
          kind="info"
          title={`Managed by harness ${svc.harness_id}`}
          detail="Direct edits are rejected; re-run the harness to change this service."
        />
      )}
      {error && (
        <Banner
          kind="error"
          title={error.code ? `Action failed (${error.code})` : "Action failed"}
          detail={error.message || ""}
        />
      )}

      <div className="filter-bar">
        <div className="tabs">
          <button className={"tab" + (tab === "config" ? " active" : "")} onClick={() => setTab("config")}>Config</button>
          <button className={"tab" + (tab === "versions" ? " active" : "")} onClick={() => setTab("versions")} data-testid="service-versions-tab">
            Versions{vitems.length ? ` (${vitems.length})` : ""}
          </button>
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          {svc.active_version_id && (
            <a
              className="btn btn-sm"
              href={"/svc/" + encodeURIComponent(svc.name) + "/"}
              target="_blank"
              rel="noreferrer"
            >
              Open app ↗
            </a>
          )}
          {!managed && <Btn size="sm" kind="ghost" icon="edit" onClick={() => setEditing(true)}>Edit</Btn>}
          {!managed && (
            <Btn size="sm" kind="danger" icon="trash" onClick={() => setConfirmDelete(true)} data-testid="service-delete-btn">
              Delete
            </Btn>
          )}
        </div>
      </div>

      {tab === "config" && (
        <div className="panel" style={{ padding: 16 }}>
          <div className="kv-grid" style={{ display: "grid", gridTemplateColumns: "160px 1fr", gap: 10, fontSize: 13 }}>
            <span className="muted">name</span>
            <span className="mono">{svc.name}</span>
            <span className="muted">public URL</span>
            <span className="mono">/svc/{svc.name}/</span>
            <span className="muted">viewer auth</span>
            <span><SV_AuthPill mode={svc.viewer_auth} /></span>
            <span className="muted">status</span>
            <span>{svc.active_version_id ? "serving " : "unpublished"}
              {svc.active_version_id && (
                <span className="mono muted text-sm">{svc.active_version_id}</span>
              )}
            </span>
            <span className="muted">description</span>
            <span>{svc.description}</span>
            <span className="muted">id</span>
            <span className="mono muted">{svc.id}</span>
          </div>
        </div>
      )}

      {tab === "versions" && (
        <>
          {vitems.length === 0 && (
            <div className="empty" style={{ padding: "40px 20px" }} data-testid="service-versions-empty">
              <div className="ico-wrap"><Icon name="box" size={22} /></div>
              <div className="head">No versions published</div>
              <div className="sub">
                Publish from an agent with the publish_service tool. Each
                publish is an immutable version; activation is a pointer
                swap.
              </div>
            </div>
          )}
          {vitems.length > 0 && (
            <div className="tbl-wrap" data-testid="service-versions-table">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Version</th>
                    <th>Files</th>
                    <th>Functions</th>
                    <th>Status</th>
                    <th style={{ textAlign: "right" }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {vitems.map((v) => {
                    const active = v.id === svc.active_version_id;
                    return (
                      <tr key={v.id} data-testid={`service-version-row-${v.version}`}>
                        <td className="mono">v{v.version}</td>
                        <td className="mono">{Object.keys(v.files || {}).length}</td>
                        <td className="mono text-sm">
                          {(v.functions || []).map((f) => f.name).join(", ") || "—"}
                        </td>
                        <td>
                          {active
                            ? <span style={{ color: "var(--green)" }}>serving</span>
                            : <span className="muted">staged</span>}
                        </td>
                        <td style={{ textAlign: "right" }}>
                          {!active && !managed && (
                            <Btn size="sm" kind="ghost" onClick={() => activate(v.id)} data-testid={`activate-version-${v.version}`}>
                              Activate
                            </Btn>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {editing && (
        <SV_ServiceModal
          existing={svc}
          onClose={() => setEditing(false)}
          onSaved={() => { setEditing(false); res.refetch(); }}
        />
      )}
      {confirmDelete && (
        <Modal
          title={`Delete service · ${svc.name}`}
          danger
          onClose={() => setConfirmDelete(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setConfirmDelete(false)}>Cancel</Btn>
              <Btn kind="danger" icon="trash" onClick={doDelete} data-testid="service-delete-confirm">
                Delete service
              </Btn>
            </>
          }
        >
          <p>
            Deleting removes the service, every published version, and their
            stored bundles. /svc/{svc.name}/ stops serving immediately.
            This cannot be undone.
          </p>
        </Modal>
      )}
    </div>
  );
}

// ============================================================================
// Exports
// ============================================================================

window.SV_ServicesPage = SV_ServicesPage;
window.SV_ServiceDetail = SV_ServiceDetail;
window.SV_ServiceModal = SV_ServiceModal;
