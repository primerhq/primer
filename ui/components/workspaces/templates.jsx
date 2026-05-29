/* global React, Icon, Btn, Modal, Banner, WS_FieldRow, WS_Section */

const WT_LIST_KEY = "ws:templates";

// ---- ContainerMountEditor: list of {host, container, readonly} rows.
// Matches the ContainerMount pydantic model used by ContainerTemplateConfig
// (host path → container mount point, optional read-only flag).
function ContainerMountEditor({ value, onChange }) {
  const arr = Array.isArray(value) ? value : [];
  const setAt = (i, patch) => {
    const next = arr.slice();
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };
  const remove = (i) => {
    const next = arr.slice();
    next.splice(i, 1);
    onChange(next);
  };
  const add = () => onChange([...arr, { host: "", container: "", readonly: false }]);
  return (
    <div className="col" style={{ gap: 6 }}>
      {arr.map((m, i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto auto", gap: 6, alignItems: "center" }}>
          <input className="input mono" placeholder="/host/path" value={m.host || ""} onChange={(e) => setAt(i, { host: e.target.value })} />
          <input className="input mono" placeholder="/container/path" value={m.container || ""} onChange={(e) => setAt(i, { container: e.target.value })} />
          <label className="mono text-sm" style={{ display: "inline-flex", gap: 4, alignItems: "center", color: "var(--text-3)" }}>
            <input type="checkbox" checked={!!m.readonly} onChange={(e) => setAt(i, { readonly: e.target.checked })} />
            ro
          </label>
          <button className="icon-btn" style={{ width: 26, height: 26 }} onClick={() => remove(i)} title="Remove"><Icon name="x" size={10} /></button>
        </div>
      ))}
      <button className="btn" style={{ alignSelf: "flex-start", padding: "4px 10px" }} onClick={add}>
        <Icon name="plus" size={11} /> Add mount
      </button>
    </div>
  );
}

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
  const { useResource, useRouter, apiFetch } = window.primerApi;
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
              A WorkspaceTemplate is the declarative recipe — files, env, init commands — that materialises into a workspace. Each template targets a previously-registered WorkspaceProvider.
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

    // ---- container variant fields ----
    c_image: "",
    c_entrypoint: [],
    c_user: "",
    c_workdir: "/workspace",
    c_cpu_cores: "",
    c_memory_bytes: "",
    c_extra_mounts: [],
    c_network_egress: "",  // "" = runtime default (null), "allow_all", "deny_all"

    // ---- kubernetes variant fields ----
    k_image: "",
    k_entrypoint: [],
    k_args: [],
    k_workdir: "/workspace",
    k_cpu_request: "",
    k_cpu_limit: "",
    k_memory_request: "",
    k_memory_limit: "",
    k_pvc_size: "10Gi",
    k_pvc_access_modes: ["ReadWriteOnce"],
    k_storage_class: "",
    k_extra_volumes: null,
    k_extra_volume_mounts: null,
    k_network_policy_name: "",
    k_pod_overrides: null,
    k_container_security_context_overrides: null,

    // ---- shared recipe ----
    files: [],
    env: {},
    init_commands: "",
    state_path: ".state",
    tmp_path: ".tmp",
  };
}

function _fromTemplate(t, providers) {
  const provider = (providers || []).find((p) => p.id === t.provider_id);
  const backend = t.backend || { kind: "local" };
  const kind = backend.kind || (provider?.provider) || "local";
  const isC = kind === "container";
  const isK = kind === "kubernetes";
  return {
    id: t.id,
    description: t.description || "",
    provider_id: t.provider_id,
    backend_kind: kind,

    // ---- container variant fields ----
    c_image: isC ? (backend.image || "") : "",
    c_entrypoint: isC ? (backend.entrypoint || []) : [],
    c_user: isC ? (backend.user || "") : "",
    c_workdir: isC ? (backend.workdir || "/workspace") : "/workspace",
    c_cpu_cores: isC && backend.cpu_cores != null ? String(backend.cpu_cores) : "",
    c_memory_bytes: isC && backend.memory_bytes != null ? String(backend.memory_bytes) : "",
    c_extra_mounts: isC ? (backend.extra_mounts || []) : [],
    c_network_egress: isC ? ((backend.network && backend.network.egress) || "") : "",

    // ---- kubernetes variant fields ----
    k_image: isK ? (backend.image || "") : "",
    k_entrypoint: isK ? (backend.entrypoint || []) : [],
    k_args: isK ? (backend.args || []) : [],
    k_workdir: isK ? (backend.workdir || "/workspace") : "/workspace",
    k_cpu_request: isK ? (backend.cpu_request || "") : "",
    k_cpu_limit: isK ? (backend.cpu_limit || "") : "",
    k_memory_request: isK ? (backend.memory_request || "") : "",
    k_memory_limit: isK ? (backend.memory_limit || "") : "",
    k_pvc_size: isK ? (backend.pvc_size || "10Gi") : "10Gi",
    k_pvc_access_modes: isK ? (backend.pvc_access_modes || ["ReadWriteOnce"]) : ["ReadWriteOnce"],
    k_storage_class: isK ? (backend.storage_class || "") : "",
    k_extra_volumes: isK ? (backend.extra_volumes && backend.extra_volumes.length ? backend.extra_volumes : null) : null,
    k_extra_volume_mounts: isK ? (backend.extra_volume_mounts && backend.extra_volume_mounts.length ? backend.extra_volume_mounts : null) : null,
    k_network_policy_name: isK ? (backend.network_policy_name || "") : "",
    k_pod_overrides: isK ? (backend.pod_overrides || null) : null,
    k_container_security_context_overrides: isK ? (backend.container_security_context_overrides || null) : null,

    // ---- shared recipe ----
    files: t.files || [],
    env: t.env || {},
    init_commands: (t.init_commands || []).join("\n"),
    state_path: t.state_path || ".state",
    tmp_path: t.tmp_path || ".tmp",
  };
}

function WorkspaceTemplateCreateModal({ onClose, pushToast, existing }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
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
    if (form.backend_kind === "container" && !form.c_image) errs.c_image = "value is required";
    if (form.backend_kind === "kubernetes" && !form.k_image) errs.k_image = "value is required";
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
        image: form.c_image,
        workdir: form.c_workdir || "/workspace",
        extra_mounts: form.c_extra_mounts || [],
      };
      if ((form.c_entrypoint || []).length > 0) backend.entrypoint = form.c_entrypoint;
      if (form.c_user) backend.user = form.c_user;
      if (form.c_cpu_cores !== "" && form.c_cpu_cores != null) {
        const n = Number(form.c_cpu_cores);
        if (!Number.isNaN(n)) backend.cpu_cores = n;
      }
      if (form.c_memory_bytes !== "" && form.c_memory_bytes != null) {
        const n = Number(form.c_memory_bytes);
        if (!Number.isNaN(n)) backend.memory_bytes = Math.trunc(n);
      }
      if (form.c_network_egress) {
        backend.network = { egress: form.c_network_egress };
      }
    } else {
      backend = {
        kind: "kubernetes",
        image: form.k_image,
        workdir: form.k_workdir || "/workspace",
        pvc_size: form.k_pvc_size || "10Gi",
        pvc_access_modes: form.k_pvc_access_modes || ["ReadWriteOnce"],
      };
      if ((form.k_entrypoint || []).length > 0) backend.entrypoint = form.k_entrypoint;
      if ((form.k_args || []).length > 0) backend.args = form.k_args;
      if (form.k_cpu_request) backend.cpu_request = form.k_cpu_request;
      if (form.k_cpu_limit) backend.cpu_limit = form.k_cpu_limit;
      if (form.k_memory_request) backend.memory_request = form.k_memory_request;
      if (form.k_memory_limit) backend.memory_limit = form.k_memory_limit;
      if (form.k_storage_class) backend.storage_class = form.k_storage_class;
      if (Array.isArray(form.k_extra_volumes) && form.k_extra_volumes.length > 0) backend.extra_volumes = form.k_extra_volumes;
      if (Array.isArray(form.k_extra_volume_mounts) && form.k_extra_volume_mounts.length > 0) backend.extra_volume_mounts = form.k_extra_volume_mounts;
      if (form.k_network_policy_name) backend.network_policy_name = form.k_network_policy_name;
      if (form.k_pod_overrides != null) backend.pod_overrides = form.k_pod_overrides;
      if (form.k_container_security_context_overrides != null) {
        backend.container_security_context_overrides = form.k_container_security_context_overrides;
      }
    }

    const initCmds = (form.init_commands || "").split("\n").map((s) => s.trim()).filter(Boolean);

    const body = {
      ...(form.id ? { id: form.id } : {}),
      description: form.description || null,
      provider_id: form.provider_id,
      backend,
      files: form.files || [],
      env: form.env || {},
      init_commands: initCmds,
      state_path: form.state_path || ".state",
      tmp_path: form.tmp_path || ".tmp",
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

      {isLocal && (
        <Banner kind="info" title="Local backend" detail="Local templates have no backend-specific fields beyond the shared recipe (files, env, init_commands)." />
      )}

      {isContainer && (<>
        <WS_Section label="Container template" sub="image, resources, mounts, network" />
        <WS_FieldRow label="image" err={fieldErrors.c_image}>
          <input className="input mono" value={form.c_image} onChange={(e) => update("c_image", e.target.value)} placeholder="ghcr.io/primer/runtime:latest" style={{ width: "100%" }} data-testid="ws-template-image" />
        </WS_FieldRow>
        <WS_FieldRow label="entrypoint" hint="optional · overrides image ENTRYPOINT">
          <window.WorkspaceStringListEditor value={form.c_entrypoint} onChange={(v) => update("c_entrypoint", v)} placeholder="bash" />
        </WS_FieldRow>
        <WS_FieldRow label="user" hint="optional · uid:gid or username">
          <input className="input mono" value={form.c_user} onChange={(e) => update("c_user", e.target.value)} placeholder="1000:1000" style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="workdir">
          <input className="input mono" value={form.c_workdir} onChange={(e) => update("c_workdir", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <WS_FieldRow label="cpu_cores" hint="optional · docker --cpus value" err={fieldErrors.cpu_cores}>
            <input className="input mono" type="number" step="0.1" min="0" value={form.c_cpu_cores} onChange={(e) => update("c_cpu_cores", e.target.value)} placeholder="2" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="memory_bytes" hint="optional · integer bytes" err={fieldErrors.memory_bytes}>
            <input className="input mono" type="number" step="1" min="0" value={form.c_memory_bytes} onChange={(e) => update("c_memory_bytes", e.target.value)} placeholder="2147483648" style={{ width: "100%" }} />
          </WS_FieldRow>
        </div>
        <WS_FieldRow label="extra_mounts" hint="host → container mounts">
          <ContainerMountEditor value={form.c_extra_mounts} onChange={(v) => update("c_extra_mounts", v)} />
        </WS_FieldRow>
        <WS_FieldRow label="network.egress" hint="null = runtime default · deny_all = --internal network">
          <select className="select mono" value={form.c_network_egress} onChange={(e) => update("c_network_egress", e.target.value)} style={{ width: "100%" }}>
            <option value="">(runtime default)</option>
            <option value="allow_all">allow_all</option>
            <option value="deny_all">deny_all</option>
          </select>
        </WS_FieldRow>
      </>)}

      {isK8s && (<>
        <WS_Section label="Kubernetes template" sub="image, resources, PVC, overrides" />
        <WS_FieldRow label="image" err={fieldErrors.k_image}>
          <input className="input mono" value={form.k_image} onChange={(e) => update("k_image", e.target.value)} placeholder="ghcr.io/primer/runtime:latest" style={{ width: "100%" }} data-testid="ws-template-image" />
        </WS_FieldRow>
        <WS_FieldRow label="entrypoint" hint="optional">
          <window.WorkspaceStringListEditor value={form.k_entrypoint} onChange={(v) => update("k_entrypoint", v)} placeholder="bash" />
        </WS_FieldRow>
        <WS_FieldRow label="args" hint="optional">
          <window.WorkspaceStringListEditor value={form.k_args} onChange={(v) => update("k_args", v)} placeholder="-c" />
        </WS_FieldRow>
        <WS_FieldRow label="workdir">
          <input className="input mono" value={form.k_workdir} onChange={(e) => update("k_workdir", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <WS_FieldRow label="cpu_request" hint="e.g. 500m">
            <input className="input mono" value={form.k_cpu_request} onChange={(e) => update("k_cpu_request", e.target.value)} placeholder="500m" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="cpu_limit" hint="e.g. 2">
            <input className="input mono" value={form.k_cpu_limit} onChange={(e) => update("k_cpu_limit", e.target.value)} placeholder="2" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="memory_request" hint="e.g. 1Gi">
            <input className="input mono" value={form.k_memory_request} onChange={(e) => update("k_memory_request", e.target.value)} placeholder="1Gi" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="memory_limit" hint="e.g. 4Gi">
            <input className="input mono" value={form.k_memory_limit} onChange={(e) => update("k_memory_limit", e.target.value)} placeholder="4Gi" style={{ width: "100%" }} />
          </WS_FieldRow>
        </div>
        <WS_FieldRow label="pvc_size">
          <input className="input mono" value={form.k_pvc_size} onChange={(e) => update("k_pvc_size", e.target.value)} placeholder="10Gi" style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="pvc_access_modes">
          <window.WorkspaceStringListEditor value={form.k_pvc_access_modes} onChange={(v) => update("k_pvc_access_modes", v)} placeholder="ReadWriteOnce" />
        </WS_FieldRow>
        <WS_FieldRow label="storage_class" hint="optional · null = cluster default">
          <input className="input mono" value={form.k_storage_class} onChange={(e) => update("k_storage_class", e.target.value)} placeholder="" style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="network_policy_name" hint="optional · pre-existing NetworkPolicy in the namespace">
          <input className="input mono" value={form.k_network_policy_name} onChange={(e) => update("k_network_policy_name", e.target.value)} placeholder="" style={{ width: "100%" }} />
        </WS_FieldRow>
        <div style={{ marginTop: 12 }}>
          <button className="btn" style={{ padding: "4px 10px" }} onClick={() => setAdvancedOpen(!advancedOpen)}>
            <Icon name={advancedOpen ? "chevron-down" : "chevron-right"} size={11} />
            <span>Advanced (extra volumes, pod / container overrides)</span>
          </button>
        </div>
        {advancedOpen && (<>
          <WS_FieldRow label="extra_volumes" hint="JSON array of Volume objects (passthrough)">
            <window.WorkspaceJsonTextareaField value={form.k_extra_volumes} onChange={(v) => update("k_extra_volumes", v)} placeholder='[{"name": "cache", "emptyDir": {}}]' rows={4} />
          </WS_FieldRow>
          <WS_FieldRow label="extra_volume_mounts" hint="JSON array of VolumeMount objects (passthrough)">
            <window.WorkspaceJsonTextareaField value={form.k_extra_volume_mounts} onChange={(v) => update("k_extra_volume_mounts", v)} placeholder='[{"name": "cache", "mountPath": "/cache"}]' rows={4} />
          </WS_FieldRow>
          <WS_FieldRow label="pod_overrides" hint="JSON object · deep-merged into PodSpec">
            <window.WorkspaceJsonTextareaField value={form.k_pod_overrides} onChange={(v) => update("k_pod_overrides", v)} placeholder='{"restartPolicy": "Always"}' rows={4} />
          </WS_FieldRow>
          <WS_FieldRow label="container_security_context_overrides" hint="JSON object · merged into Container.securityContext">
            <window.WorkspaceJsonTextareaField value={form.k_container_security_context_overrides} onChange={(v) => update("k_container_security_context_overrides", v)} placeholder='{"runAsNonRoot": true}' rows={4} />
          </WS_FieldRow>
        </>)}
      </>)}

      <WS_Section label="Recipe" sub="files, env, init commands — shared across backends" />
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
    </Modal>
  );
}

function WorkspaceTemplateDetail({ templateId, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
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
