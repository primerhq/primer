/* global React, Icon, Btn, Modal, Banner, WS_FieldRow, WS_Section */

const WT_LIST_KEY = "ws:templates";

function _wtToastErr(pushToast, fallback) {
  return (err) => {
    if (typeof pushToast !== "function") return;
    pushToast({
      kind: "error",
      title: err?.title || fallback,
      detail: err?.detail || err?.message,
      requestId: err?.requestId,
    });
  };
}

function WorkspaceTemplatesPage({ pushToast }) {
  const { useResource, useRouter, apiFetch } = window.matrixApi;
  const { navigate } = useRouter();
  const [createOpen, setCreateOpen] = React.useState(false);

  const list = useResource(
    WT_LIST_KEY,
    (signal) => apiFetch("GET", "/workspace_templates?limit=200", null, { signal }),
    { pollMs: 5000 }
  );
  const items = Array.isArray(list.data?.items) ? list.data.items : [];

  const modal = createOpen ? (
    <WorkspaceTemplateCreateModal
      onClose={() => setCreateOpen(false)}
      pushToast={pushToast}
    />
  ) : null;

  if (!list.loading && items.length === 0 && !list.error) {
    return (
      <>
        <div className="panel">
          <div className="empty">
            <div className="ico-wrap"><Icon name="tools" size={22} /></div>
            <div className="head">No workspace templates</div>
            <div className="sub">
              A WorkspaceTemplate is the declarative recipe — packages, files, env, init commands — that materialises into a workspace. Each template targets a previously-registered WorkspaceProvider.
            </div>
            <div className="actions">
              <Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New workspace template</Btn>
            </div>
          </div>
        </div>
        {modal}
      </>
    );
  }

  return (
    <>
      <div className="col" style={{ gap: 14 }}>
        <div className="filter-bar">
          <span className="muted text-sm tabular" style={{ marginLeft: "auto" }}>
            <span className="mono" style={{ color: "var(--green)" }}>● live</span> · /v1/workspace_templates every 5s
          </span>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New template</Btn>
        </div>
        {list.error && items.length === 0 ? (
          <Banner kind="error" title={list.error.title || "Couldn't load templates"} detail={list.error.detail || list.error.message} actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>} />
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Description</th>
                  <th>Backend</th>
                  <th>Provider</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {items.map((t) => (
                  <tr key={t.id} onClick={() => navigate(`/workspaces/templates/${encodeURIComponent(t.id)}`)} style={{ cursor: "pointer" }}>
                    <td className="mono">{t.id}</td>
                    <td className="muted">{t.description || <span style={{ color: "var(--text-4)" }}>—</span>}</td>
                    <td><window.WorkspaceBackendBadge kind={t.backend?.kind} /></td>
                    <td className="mono muted text-sm">
                      <a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={(e) => { e.stopPropagation(); navigate(`/workspaces/providers/${encodeURIComponent(t.provider_id)}`); }}>{t.provider_id}</a>
                    </td>
                    <td style={{ textAlign: "right", paddingRight: 12 }}><Icon name="chevron-right" size={12} className="muted" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {modal}
    </>
  );
}

function _emptyForm(provider) {
  return {
    id: "",
    description: "",
    provider_id: provider?.id || "",
    backend_kind: provider?.provider || "local",
    image: "",
    entrypoint: [],
    workdir: "/workspace",
    user: "",
    extra_mounts: [],
    extra_volume_size: "",
    args: [],
    pvc_size: "10Gi",
    pvc_access_modes: ["ReadWriteOnce"],
    extra_volume_mounts: null,
    extra_volumes: null,
    container_overrides: null,
    pod_overrides: null,
    packages: [],
    files: [],
    env: {},
    init_commands: "",
    state_path: ".state",
    tmp_path: ".tmp",
    cpu_limit: "",
    memory_limit: "",
    disk_limit: "",
    network_egress: "deny",
  };
}

function _fromTemplate(t, providers) {
  const provider = (providers || []).find((p) => p.id === t.provider_id);
  const backend = t.backend || { kind: "local" };
  const r = t.resources || {};
  return {
    id: t.id,
    description: t.description || "",
    provider_id: t.provider_id,
    backend_kind: backend.kind || (provider?.provider) || "local",
    image: backend.image || "",
    entrypoint: backend.entrypoint || [],
    workdir: backend.workdir || "/workspace",
    user: backend.user || "",
    extra_mounts: backend.extra_mounts || [],
    extra_volume_size: backend.extra_volume_size || "",
    args: backend.args || [],
    pvc_size: backend.pvc_size || "10Gi",
    pvc_access_modes: backend.pvc_access_modes || ["ReadWriteOnce"],
    extra_volume_mounts: backend.extra_volume_mounts || null,
    extra_volumes: backend.extra_volumes || null,
    container_overrides: backend.container_overrides || null,
    pod_overrides: backend.pod_overrides || null,
    packages: t.packages || [],
    files: t.files || [],
    env: t.env || {},
    init_commands: (t.init_commands || []).join("\n"),
    state_path: t.state_path || ".state",
    tmp_path: t.tmp_path || ".tmp",
    cpu_limit: r.cpu_limit || "",
    memory_limit: r.memory_limit || "",
    disk_limit: r.disk_limit || "",
    network_egress: r.network_egress || "deny",
  };
}

function WorkspaceTemplateCreateModal({ onClose, pushToast, existing }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.matrixApi;
  const { navigate } = useRouter();

  const providers = useResource(
    "ws:templates:providers-picker",
    (signal) => apiFetch("GET", "/workspace_providers?limit=200", null, { signal }),
    {}
  );
  const providerItems = Array.isArray(providers.data?.items) ? providers.data.items : [];

  const isEdit = !!existing;
  const [form, setForm] = React.useState(() => isEdit ? _fromTemplate(existing, providerItems) : _emptyForm(providerItems[0]));
  const [fieldErrors, setFieldErrors] = React.useState({});
  const [advancedOpen, setAdvancedOpen] = React.useState(false);

  React.useEffect(() => {
    if (isEdit) return;
    if (!form.provider_id && providerItems.length > 0) {
      const p = providerItems[0];
      setForm((f) => ({ ...f, provider_id: p.id, backend_kind: p.provider }));
    }
  }, [providerItems.length, isEdit]);

  const update = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  const onProviderChange = (pid) => {
    const p = providerItems.find((x) => x.id === pid);
    setForm((f) => ({ ...f, provider_id: pid, backend_kind: p ? p.provider : f.backend_kind }));
  };

  const mutation = useMutation(
    (body) => apiFetch(isEdit ? "PUT" : "POST", isEdit ? `/workspace_templates/${encodeURIComponent(existing.id)}` : "/workspace_templates", body),
    {
      invalidates: [WT_LIST_KEY, ...(isEdit ? [`ws:template:${existing.id}`] : [])],
      onSuccess: (row) => {
        onClose();
        if (pushToast) pushToast({ kind: "success", title: isEdit ? "Template updated" : "Template created", detail: row.id });
        if (!isEdit) navigate(`/workspaces/templates/${encodeURIComponent(row.id)}`);
      },
      onError: (err) => {
        if (err?.status === 422 && Array.isArray(err.fieldErrors)) {
          const map = {};
          for (const fe of err.fieldErrors) {
            const loc = (fe.loc || []).filter((s) => s !== "body");
            map[loc.join(".")] = fe.msg;
            if (loc.length > 0) map[loc[loc.length - 1]] = fe.msg;
          }
          setFieldErrors(map);
        } else {
          _wtToastErr(pushToast, isEdit ? "Update failed" : "Create failed")(err);
        }
      },
    },
  );

  const submit = () => {
    const errs = {};
    if (!form.provider_id) errs.provider_id = "value is required";
    if (form.backend_kind !== "local" && !form.image) errs.image = "value is required";
    if (Object.keys(errs).length > 0) {
      setFieldErrors(errs);
      return;
    }
    setFieldErrors({});

    let backend;
    if (form.backend_kind === "local") {
      backend = { kind: "local" };
    } else if (form.backend_kind === "container") {
      backend = {
        kind: "container",
        image: form.image,
        workdir: form.workdir || "/workspace",
        extra_mounts: form.extra_mounts || [],
      };
      if ((form.entrypoint || []).length > 0) backend.entrypoint = form.entrypoint;
      if (form.user) backend.user = form.user;
      if (form.extra_volume_size) backend.extra_volume_size = form.extra_volume_size;
    } else {
      backend = {
        kind: "kubernetes",
        image: form.image,
        workdir: form.workdir || "/workspace",
        pvc_size: form.pvc_size || "10Gi",
        pvc_access_modes: form.pvc_access_modes || ["ReadWriteOnce"],
      };
      if ((form.entrypoint || []).length > 0) backend.entrypoint = form.entrypoint;
      if ((form.args || []).length > 0) backend.args = form.args;
      if (form.extra_volume_mounts) backend.extra_volume_mounts = form.extra_volume_mounts;
      if (form.extra_volumes) backend.extra_volumes = form.extra_volumes;
      if (form.container_overrides) backend.container_overrides = form.container_overrides;
      if (form.pod_overrides) backend.pod_overrides = form.pod_overrides;
    }

    const initCmds = (form.init_commands || "").split("\n").map((s) => s.trim()).filter(Boolean);
    const resources = {
      cpu_limit: form.cpu_limit || null,
      memory_limit: form.memory_limit || null,
      disk_limit: form.disk_limit || null,
      network_egress: form.network_egress || "deny",
    };

    const body = {
      ...(form.id ? { id: form.id } : {}),
      description: form.description || null,
      provider_id: form.provider_id,
      backend,
      packages: form.packages || [],
      files: form.files || [],
      env: form.env || {},
      init_commands: initCmds,
      state_path: form.state_path || ".state",
      tmp_path: form.tmp_path || ".tmp",
      resources,
    };
    mutation.mutate(body).catch(() => { /* onError handled */ });
  };

  const isLocal = form.backend_kind === "local";
  const isContainer = form.backend_kind === "container";
  const isK8s = form.backend_kind === "kubernetes";

  return (
    <Modal
      title={isEdit ? `Edit ${existing.id}` : "New workspace template"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon={isEdit ? "save" : "plus"} disabled={mutation.loading} onClick={submit}>
            {mutation.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save" : "Create")}
          </Btn>
        </>
      }
    >
      {!isEdit && (
        <WS_FieldRow label="id" hint="optional · backend assigns if blank">
          <input className="input mono" value={form.id} onChange={(e) => update("id", e.target.value)} placeholder="dev-template" style={{ width: "100%" }} />
        </WS_FieldRow>
      )}
      <WS_FieldRow label="description">
        <input className="input mono" value={form.description} onChange={(e) => update("description", e.target.value)} placeholder="dev workspace" style={{ width: "100%" }} data-testid="ws-template-description" />
      </WS_FieldRow>
      <WS_FieldRow label="provider" hint={isEdit ? "immutable on edit" : "drives the backend section"} err={fieldErrors.provider_id}>
        {providerItems.length === 0 ? (
          <div className="banner banner-warning" style={{ margin: 0, fontSize: 11.5 }}>
            <Icon name="alert" size={12} className="ico" />
            <div>No workspace_providers registered. Register one before creating a template.</div>
          </div>
        ) : (
          <select
            className="select mono"
            value={form.provider_id}
            onChange={(e) => onProviderChange(e.target.value)}
            disabled={isEdit}
            style={{ width: "100%" }}
            data-testid="ws-template-provider"
          >
            {providerItems.map((p) => <option key={p.id} value={p.id}>{p.id} ({p.provider})</option>)}
          </select>
        )}
      </WS_FieldRow>

      {isContainer && (<>
        <WS_Section label="Container template" />
        <WS_FieldRow label="image" err={fieldErrors.image}>
          <input className="input mono" value={form.image} onChange={(e) => update("image", e.target.value)} placeholder="ubuntu:24.04" style={{ width: "100%" }} data-testid="ws-template-image" />
        </WS_FieldRow>
        <WS_FieldRow label="entrypoint" hint='optional · default ["sleep", "infinity"]'>
          <window.WorkspaceStringListEditor value={form.entrypoint} onChange={(v) => update("entrypoint", v)} placeholder="bash" />
        </WS_FieldRow>
        <WS_FieldRow label="user" hint="optional · uid:gid or username">
          <input className="input mono" value={form.user} onChange={(e) => update("user", e.target.value)} placeholder="1000:1000" style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="workdir">
          <input className="input mono" value={form.workdir} onChange={(e) => update("workdir", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="extra_volume_size" hint="optional · advisory">
          <input className="input mono" value={form.extra_volume_size} onChange={(e) => update("extra_volume_size", e.target.value)} placeholder="10Gi" style={{ width: "100%" }} />
        </WS_FieldRow>
      </>)}

      {isK8s && (<>
        <WS_Section label="Kubernetes template" />
        <WS_FieldRow label="image" err={fieldErrors.image}>
          <input className="input mono" value={form.image} onChange={(e) => update("image", e.target.value)} placeholder="ubuntu:24.04" style={{ width: "100%" }} data-testid="ws-template-image" />
        </WS_FieldRow>
        <WS_FieldRow label="entrypoint" hint="optional">
          <window.WorkspaceStringListEditor value={form.entrypoint} onChange={(v) => update("entrypoint", v)} placeholder="bash" />
        </WS_FieldRow>
        <WS_FieldRow label="args" hint="optional">
          <window.WorkspaceStringListEditor value={form.args} onChange={(v) => update("args", v)} placeholder="-c" />
        </WS_FieldRow>
        <WS_FieldRow label="workdir">
          <input className="input mono" value={form.workdir} onChange={(e) => update("workdir", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="pvc_size">
          <input className="input mono" value={form.pvc_size} onChange={(e) => update("pvc_size", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="pvc_access_modes">
          <window.WorkspaceStringListEditor value={form.pvc_access_modes} onChange={(v) => update("pvc_access_modes", v)} placeholder="ReadWriteOnce" />
        </WS_FieldRow>
        <div style={{ marginTop: 12 }}>
          <button className="btn" style={{ padding: "4px 10px" }} onClick={() => setAdvancedOpen(!advancedOpen)}>
            <Icon name={advancedOpen ? "chevron-down" : "chevron-right"} size={11} />
            <span>Advanced (extra volumes, container/pod overrides)</span>
          </button>
        </div>
        {advancedOpen && (<>
          <WS_FieldRow label="extra_volume_mounts" hint="JSON array of volumeMount objects">
            <window.WorkspaceJsonTextareaField value={form.extra_volume_mounts} onChange={(v) => update("extra_volume_mounts", v)} placeholder='[{"name": "cache", "mountPath": "/cache"}]' rows={4} />
          </WS_FieldRow>
          <WS_FieldRow label="extra_volumes" hint="JSON array of volume objects">
            <window.WorkspaceJsonTextareaField value={form.extra_volumes} onChange={(v) => update("extra_volumes", v)} placeholder='[{"name": "cache", "emptyDir": {}}]' rows={4} />
          </WS_FieldRow>
          <WS_FieldRow label="container_overrides" hint="JSON object · passthrough to Container">
            <window.WorkspaceJsonTextareaField value={form.container_overrides} onChange={(v) => update("container_overrides", v)} placeholder='{"resources": {"limits": {"cpu": "2"}}}' rows={4} />
          </WS_FieldRow>
          <WS_FieldRow label="pod_overrides" hint="JSON object · passthrough to PodSpec">
            <window.WorkspaceJsonTextareaField value={form.pod_overrides} onChange={(v) => update("pod_overrides", v)} placeholder='{"restartPolicy": "Always"}' rows={4} />
          </WS_FieldRow>
        </>)}
      </>)}

      <WS_Section label="Recipe" />
      <WS_FieldRow label="packages" hint="system / language packages installed at materialise time">
        <window.WorkspaceJsonTextareaField value={form.packages.length ? form.packages : null} onChange={(v) => update("packages", Array.isArray(v) ? v : [])} placeholder='[{"name": "git"}, {"name": "python3", "version": "3.13"}]' rows={4} />
      </WS_FieldRow>
      <WS_FieldRow label="files" hint="inline-text only · git/http sources via API">
        <window.WorkspaceFileRowEditor value={form.files} onChange={(v) => update("files", v)} />
      </WS_FieldRow>
      <WS_FieldRow label="env" hint="key/value pairs · values stored encrypted as SecretStr">
        <window.WorkspaceEnvPairEditor value={form.env} onChange={(v) => update("env", v)} />
      </WS_FieldRow>
      <WS_FieldRow label="init_commands" hint="one shell command per line · failure aborts materialisation">
        <textarea className="input mono" style={{ width: "100%", fontSize: 12, minHeight: 90 }} value={form.init_commands} onChange={(e) => update("init_commands", e.target.value)} placeholder="apt-get install -y git" />
      </WS_FieldRow>

      <WS_Section label="Paths" />
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        <WS_FieldRow label="state_path" hint="relative to workspace root">
          <input className="input mono" value={form.state_path} onChange={(e) => update("state_path", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="tmp_path" hint="relative to workspace root">
          <input className="input mono" value={form.tmp_path} onChange={(e) => update("tmp_path", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
      </div>

      <WS_Section label="Resources" />
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
        <WS_FieldRow label="cpu_limit" hint="optional">
          <input className="input mono" value={form.cpu_limit} onChange={(e) => update("cpu_limit", e.target.value)} placeholder="2" style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="memory_limit" hint="optional">
          <input className="input mono" value={form.memory_limit} onChange={(e) => update("memory_limit", e.target.value)} placeholder="2Gi" style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="disk_limit" hint="optional">
          <input className="input mono" value={form.disk_limit} onChange={(e) => update("disk_limit", e.target.value)} placeholder="10Gi" style={{ width: "100%" }} />
        </WS_FieldRow>
      </div>
      <WS_FieldRow label="network_egress">
        <select className="select mono" value={form.network_egress} onChange={(e) => update("network_egress", e.target.value)} style={{ width: "100%" }}>
          <option value="deny">deny</option>
          <option value="allow">allow</option>
        </select>
      </WS_FieldRow>

      {isLocal && (
        <Banner kind="info" title="Local backend" detail="Local templates have no backend-specific fields beyond the shared recipe." />
      )}
    </Modal>
  );
}

function WorkspaceTemplateDetail({ templateId, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.matrixApi;
  const { navigate } = useRouter();
  const [tab, setTab] = React.useState("overview");
  const [editOpen, setEditOpen] = React.useState(false);
  const [showDelete, setShowDelete] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState(null);

  const detailKey = `ws:template:${templateId}`;
  const detail = useResource(
    detailKey,
    (signal) => apiFetch("GET", `/workspace_templates/${encodeURIComponent(templateId)}`, null, { signal }),
    { deps: [templateId] }
  );

  const workspaces = useResource(
    `ws:template:${templateId}:workspaces`,
    (signal) => apiFetch("GET", "/workspaces?limit=500", null, { signal }),
    { deps: [templateId] }
  );
  const referencingWorkspaces = React.useMemo(() => {
    const all = workspaces.data?.items ?? [];
    return all.filter((w) => w.template_id === templateId);
  }, [workspaces.data, templateId]);

  const del = useMutation(
    () => apiFetch("DELETE", `/workspace_templates/${encodeURIComponent(templateId)}`),
    {
      invalidates: [WT_LIST_KEY],
      onSuccess: () => {
        if (pushToast) pushToast({ kind: "warning", title: "Template deleted", detail: templateId });
        setShowDelete(false);
        navigate("/workspaces/templates");
      },
      onError: (err) => {
        if (err?.status === 409) {
          setDeleteError(err.detail || "Cannot delete — workspaces still reference this template.");
        } else {
          setShowDelete(false);
          _wtToastErr(pushToast, "Delete failed")(err);
        }
      },
    },
  );

  if (detail.loading && !detail.data) {
    return <div className="panel"><div className="panel-body" style={{ padding: 18 }}><span className="muted text-sm">Loading template…</span></div></div>;
  }
  if (detail.error && !detail.data) {
    return <Banner kind="error" title={detail.error.title || `Couldn't load ${templateId}`} detail={detail.error.detail || detail.error.message} actions={<Btn size="sm" icon="refresh" onClick={detail.refetch}>Retry</Btn>} />;
  }
  const t = detail.data;
  if (!t) return null;

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel">
        <div className="panel-body" style={{ padding: "14px 18px", display: "flex", alignItems: "center", gap: 14 }}>
          <window.WorkspaceBackendBadge kind={t.backend?.kind || "local"} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{t.id}</div>
            <div className="muted text-sm mono" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {t.description || <span style={{ color: "var(--text-4)" }}>—</span>} · provider {t.provider_id}
            </div>
          </div>
          <Btn size="sm" kind="ghost" icon="edit" onClick={() => setEditOpen(true)}>Edit</Btn>
          <Btn size="sm" kind="danger" icon="trash" onClick={() => { setDeleteError(null); setShowDelete(true); }}>Delete</Btn>
        </div>
        <div style={{ display: "flex", alignItems: "center", borderTop: "1px solid var(--border)", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {[
            { id: "overview", label: "Overview", icon: "info" },
            { id: "recipe", label: "Recipe", icon: "settings" },
            { id: "workspaces", label: "Workspaces", icon: "box", count: referencingWorkspaces.length },
          ].map((tt) => (
            <button
              key={tt.id}
              onClick={() => setTab(tt.id)}
              style={{
                background: "none", border: "none",
                padding: "10px 14px", cursor: "pointer",
                color: tab === tt.id ? "var(--text)" : "var(--text-3)",
                fontSize: 12.5, fontWeight: tab === tt.id ? 600 : 400,
                borderBottom: tab === tt.id ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: -1,
                display: "inline-flex", alignItems: "center", gap: 6,
              }}
            >
              <Icon name={tt.icon} size={13} />
              {tt.label}
              {tt.count != null && tt.count > 0 && <span className="count" style={{ marginLeft: 4 }}>{tt.count}</span>}
            </button>
          ))}
        </div>
        <div style={{ padding: 18 }}>
          {tab === "overview" && (
            <dl className="kv" style={{ gridTemplateColumns: "200px 1fr" }}>
              <dt>id</dt><dd className="mono">{t.id}</dd>
              <dt>description</dt><dd>{t.description || <span style={{ color: "var(--text-4)" }}>—</span>}</dd>
              <dt>provider</dt><dd className="mono"><a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => navigate(`/workspaces/providers/${encodeURIComponent(t.provider_id)}`)}>{t.provider_id}</a></dd>
              <dt>backend</dt><dd><window.WorkspaceBackendBadge kind={t.backend?.kind || "local"} /></dd>
              <dt>files</dt><dd className="mono">{(t.files || []).length}</dd>
              <dt>packages</dt><dd className="mono">{(t.packages || []).length}</dd>
              <dt>env keys</dt><dd className="mono">{Object.keys(t.env || {}).length}</dd>
              <dt>init_commands</dt><dd className="mono">{(t.init_commands || []).length}</dd>
              <dt>state_path</dt><dd className="mono">{t.state_path}</dd>
              <dt>tmp_path</dt><dd className="mono">{t.tmp_path}</dd>
            </dl>
          )}
          {tab === "recipe" && (
            <div>
              <div className="muted text-sm mb-2">Raw template recipe (server-redacted secrets where applicable).</div>
              <div className="code-block" style={{ maxHeight: 420, overflow: "auto", whiteSpace: "pre", fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}>
                {JSON.stringify(t, null, 2)}
              </div>
            </div>
          )}
          {tab === "workspaces" && (
            referencingWorkspaces.length === 0 ? (
              <div className="empty" style={{ padding: 20 }}>
                <div className="head">No workspaces materialised</div>
                <div className="sub">No workspaces are using this template yet.</div>
              </div>
            ) : (
              <table className="tbl">
                <thead><tr><th>ID</th><th>Created</th></tr></thead>
                <tbody>
                  {referencingWorkspaces.map((w) => (
                    <tr key={w.id} onClick={() => navigate(`/workspaces/${encodeURIComponent(w.id)}`)} style={{ cursor: "pointer" }}>
                      <td className="mono">{w.id}</td>
                      <td className="muted text-sm">{w.created_at || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </div>
      </div>

      {editOpen && (
        <WorkspaceTemplateCreateModal
          existing={t}
          onClose={() => setEditOpen(false)}
          pushToast={pushToast}
        />
      )}

      {showDelete && (
        <Modal
          title={`Delete ${templateId}?`}
          danger
          onClose={() => { setShowDelete(false); setDeleteError(null); }}
          footer={
            <>
              <Btn kind="ghost" onClick={() => { setShowDelete(false); setDeleteError(null); }}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                disabled={referencingWorkspaces.length > 0 || del.loading}
                onClick={() => del.mutate().catch(() => { /* onError */ })}
              >
                {del.loading ? "Deleting…" : "Delete template"}
              </Btn>
            </>
          }
        >
          {deleteError ? (
            <><strong style={{ color: "var(--red)" }}>409 Conflict</strong> — {deleteError}</>
          ) : referencingWorkspaces.length > 0 ? (
            <>
              <strong style={{ color: "var(--red)" }}>409 Conflict</strong> — this template is referenced by{" "}
              <strong>{referencingWorkspaces.length}</strong> workspace{referencingWorkspaces.length === 1 ? "" : "s"}.
              Delete those workspaces first.
            </>
          ) : (
            <>No workspaces reference this template. Deletion is safe.</>
          )}
        </Modal>
      )}
    </div>
  );
}

window.WorkspaceTemplatesPage = WorkspaceTemplatesPage;
window.WorkspaceTemplateCreateModal = WorkspaceTemplateCreateModal;
window.WorkspaceTemplateDetail = WorkspaceTemplateDetail;
