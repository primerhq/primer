/* global React, Icon, Btn, Modal, Banner, WS_FieldRow, WS_Section */

const WP_LIST_KEY = "ws:providers";

function _wpToastErr(pushToast, fallback) {
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

function _wpSummary(p) {
  if (!p || !p.config) return "";
  if (p.provider === "local") return p.config.path || "";
  if (p.provider === "container") {
    const rt = p.config.runtime?.kind || "?";
    const img = p.config.default_image || "(no default image)";
    return `${rt} · ${img}`;
  }
  if (p.provider === "kubernetes") {
    const ns = p.config.namespace || "default";
    const ctx = p.config.in_cluster ? "(in-cluster)" : (p.config.context || "current-context");
    return `${ns} · ${ctx}`;
  }
  return "";
}

function WorkspaceProvidersPage({ pushToast }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const [createOpen, setCreateOpen] = React.useState(false);

  const list = useResource(
    WP_LIST_KEY,
    (signal) => apiFetch("GET", "/workspace_providers?limit=200", null, { signal }),
    { pollMs: 5000 }
  );
  const items = Array.isArray(list.data?.items) ? list.data.items : [];

  const modal = createOpen ? (
    <WorkspaceProviderCreateModal
      onClose={() => setCreateOpen(false)}
      pushToast={pushToast}
    />
  ) : null;

  if (!list.loading && items.length === 0 && !list.error) {
    return (
      <>
        <div className="panel">
          <div className="empty">
            <div className="ico-wrap"><Icon name="box" size={22} /></div>
            <div className="head">No workspace providers</div>
            <div className="sub">
              A WorkspaceProvider configures one backend (local filesystem, container runtime, or Kubernetes) that templates will resolve to. Create one to get started.
            </div>
            <div className="actions">
              <Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New workspace provider</Btn>
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
            <span className="mono" style={{ color: "var(--green)" }}>● live</span> · /v1/workspace_providers every 5s
          </span>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New provider</Btn>
        </div>
        {list.error && items.length === 0 ? (
          <Banner kind="error" title={list.error.title || "Couldn't load providers"} detail={list.error.detail || list.error.message} actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>} />
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Backend</th>
                  <th>Summary</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {items.map((p) => (
                  <tr key={p.id} onClick={() => navigate(`/workspaces/providers/${encodeURIComponent(p.id)}`)} style={{ cursor: "pointer" }}>
                    <td className="mono">{p.id}</td>
                    <td><window.WorkspaceBackendBadge kind={p.provider} /></td>
                    <td className="mono muted text-sm">{_wpSummary(p)}</td>
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

function WorkspaceProviderCreateModal({ onClose, pushToast }) {
  const { useMutation, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();

  const [form, setForm] = React.useState({
    id: "",
    backend: "local",
    path: "",
    runtime_kind: "docker",
    runtime_socket: "",
    runtime_api_version: "",
    runtime_namespace: "default",
    default_image: "",
    name_prefix_container: "primer-ws-",
    volume_driver: "",
    pull_policy_container: "if_missing",
    in_cluster: false,
    kubeconfig_path: "",
    context: "",
    namespace: "default",
    name_prefix_k8s: "primer-ws-",
    storage_class: "",
    default_pvc_size: "10Gi",
    service_account: "",
    image_pull_secrets: [],
    pull_policy_k8s: "IfNotPresent",
    annotations: {},
    labels: {},
    node_selector: {},
    pod_security_context: null,
    container_security_context: null,
    tolerations: null,
  });
  const [fieldErrors, setFieldErrors] = React.useState({});
  const [advancedOpen, setAdvancedOpen] = React.useState(false);
  const update = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const create = useMutation(
    (body) => apiFetch("POST", "/workspace_providers", body),
    {
      invalidates: [WP_LIST_KEY],
      onSuccess: (row) => {
        onClose();
        if (pushToast) pushToast({ kind: "success", title: "Provider created", detail: row.id });
        navigate(`/workspaces/providers/${encodeURIComponent(row.id)}`);
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
          _wpToastErr(pushToast, "Create failed")(err);
        }
      },
    },
  );

  const submit = () => {
    const errs = {};
    if (!form.id) errs.id = "value is required";
    if (form.backend === "local" && !form.path) errs.path = "value is required";
    if (Object.keys(errs).length > 0) {
      setFieldErrors(errs);
      return;
    }
    setFieldErrors({});

    let config;
    if (form.backend === "local") {
      config = { kind: "local", path: form.path };
    } else if (form.backend === "container") {
      const rt = { kind: form.runtime_kind };
      if (form.runtime_kind === "docker") {
        if (form.runtime_socket) rt.socket = form.runtime_socket;
        if (form.runtime_api_version) rt.api_version = form.runtime_api_version;
      } else if (form.runtime_kind === "podman") {
        if (form.runtime_socket) rt.socket = form.runtime_socket;
      } else if (form.runtime_kind === "containerd") {
        rt.socket = form.runtime_socket || "/run/containerd/containerd.sock";
        rt.namespace = form.runtime_namespace || "default";
      }
      config = {
        kind: "container",
        runtime: rt,
        name_prefix: form.name_prefix_container || "primer-ws-",
        pull_policy: form.pull_policy_container || "if_missing",
      };
      if (form.default_image) config.default_image = form.default_image;
      if (form.volume_driver) config.volume_driver = form.volume_driver;
    } else {
      config = {
        kind: "kubernetes",
        in_cluster: !!form.in_cluster,
        namespace: form.namespace || "default",
        name_prefix: form.name_prefix_k8s || "primer-ws-",
        default_pvc_size: form.default_pvc_size || "10Gi",
        image_pull_secrets: form.image_pull_secrets || [],
        pull_policy: form.pull_policy_k8s || "IfNotPresent",
        annotations: form.annotations || {},
        labels: form.labels || {},
        node_selector: form.node_selector || {},
      };
      if (!form.in_cluster) {
        if (form.kubeconfig_path) config.kubeconfig_path = form.kubeconfig_path;
        if (form.context) config.context = form.context;
      }
      if (form.storage_class) config.storage_class = form.storage_class;
      if (form.service_account) config.service_account = form.service_account;
      if (form.pod_security_context) config.pod_security_context = form.pod_security_context;
      if (form.container_security_context) config.container_security_context = form.container_security_context;
      if (form.tolerations) config.tolerations = form.tolerations;
    }
    const body = { id: form.id, provider: form.backend, config };
    create.mutate(body).catch(() => { /* onError handled */ });
  };

  const isLocal = form.backend === "local";
  const isContainer = form.backend === "container";
  const isK8s = form.backend === "kubernetes";

  return (
    <Modal
      title="New workspace provider"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" disabled={create.loading} onClick={submit}>
            {create.loading ? "Creating…" : "Create"}
          </Btn>
        </>
      }
    >
      <WS_FieldRow label="id" hint="must be unique" err={fieldErrors.id}>
        <input className="input mono" value={form.id} onChange={(e) => update("id", e.target.value)} placeholder="local-dev" style={{ width: "100%" }} />
      </WS_FieldRow>
      <WS_FieldRow label="backend">
        <select className="select mono" value={form.backend} onChange={(e) => update("backend", e.target.value)} style={{ width: "100%" }}>
          <option value="local">local (host filesystem)</option>
          <option value="container">container (docker / podman / containerd)</option>
          <option value="kubernetes">kubernetes</option>
        </select>
      </WS_FieldRow>

      {isLocal && (<>
        <WS_Section label="Filesystem" />
        <WS_FieldRow label="path" hint="absolute directory" err={fieldErrors.path}>
          <input className="input mono" value={form.path} onChange={(e) => update("path", e.target.value)} placeholder="/var/lib/primer/workspaces" style={{ width: "100%" }} data-testid="ws-provider-path" />
        </WS_FieldRow>
      </>)}

      {isContainer && (<>
        <WS_Section label="Container runtime" />
        <WS_FieldRow label="runtime">
          <select className="select mono" value={form.runtime_kind} onChange={(e) => update("runtime_kind", e.target.value)} style={{ width: "100%" }}>
            <option value="docker">docker</option>
            <option value="podman">podman</option>
            <option value="containerd">containerd</option>
          </select>
        </WS_FieldRow>
        {form.runtime_kind !== "containerd" && (
          <WS_FieldRow label="socket" hint="optional · default = env or platform default">
            <input className="input mono" value={form.runtime_socket} onChange={(e) => update("runtime_socket", e.target.value)} placeholder="/var/run/docker.sock" style={{ width: "100%" }} />
          </WS_FieldRow>
        )}
        {form.runtime_kind === "docker" && (
          <WS_FieldRow label="api_version" hint="optional · client default">
            <input className="input mono" value={form.runtime_api_version} onChange={(e) => update("runtime_api_version", e.target.value)} style={{ width: "100%" }} />
          </WS_FieldRow>
        )}
        {form.runtime_kind === "containerd" && (<>
          <WS_FieldRow label="socket">
            <input className="input mono" value={form.runtime_socket} onChange={(e) => update("runtime_socket", e.target.value)} placeholder="/run/containerd/containerd.sock" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="namespace" hint="containerd namespace (NOT k8s namespace)">
            <input className="input mono" value={form.runtime_namespace} onChange={(e) => update("runtime_namespace", e.target.value)} style={{ width: "100%" }} />
          </WS_FieldRow>
        </>)}
        <WS_Section label="Defaults" />
        <WS_FieldRow label="default_image" hint="optional · used when template has no image">
          <input className="input mono" value={form.default_image} onChange={(e) => update("default_image", e.target.value)} placeholder="ubuntu:24.04" style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="name_prefix">
          <input className="input mono" value={form.name_prefix_container} onChange={(e) => update("name_prefix_container", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="volume_driver" hint="optional · runtime default">
          <input className="input mono" value={form.volume_driver} onChange={(e) => update("volume_driver", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="pull_policy">
          <select className="select mono" value={form.pull_policy_container} onChange={(e) => update("pull_policy_container", e.target.value)} style={{ width: "100%" }}>
            <option value="always">always</option>
            <option value="if_missing">if_missing</option>
            <option value="never">never</option>
          </select>
        </WS_FieldRow>
      </>)}

      {isK8s && (<>
        <WS_Section label="Cluster" />
        <WS_FieldRow label="in_cluster" hint="check when running primer inside the target cluster">
          <input type="checkbox" checked={!!form.in_cluster} onChange={(e) => update("in_cluster", e.target.checked)} />
        </WS_FieldRow>
        {!form.in_cluster && (<>
          <WS_FieldRow label="kubeconfig_path" hint="optional · ~/.kube/config if blank">
            <input className="input mono" value={form.kubeconfig_path} onChange={(e) => update("kubeconfig_path", e.target.value)} placeholder="/etc/kubernetes/admin.conf" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="context" hint="optional · current-context if blank">
            <input className="input mono" value={form.context} onChange={(e) => update("context", e.target.value)} style={{ width: "100%" }} />
          </WS_FieldRow>
        </>)}
        <WS_FieldRow label="namespace">
          <input className="input mono" value={form.namespace} onChange={(e) => update("namespace", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="name_prefix">
          <input className="input mono" value={form.name_prefix_k8s} onChange={(e) => update("name_prefix_k8s", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_Section label="Storage" />
        <WS_FieldRow label="storage_class" hint="optional · cluster default">
          <input className="input mono" value={form.storage_class} onChange={(e) => update("storage_class", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="default_pvc_size">
          <input className="input mono" value={form.default_pvc_size} onChange={(e) => update("default_pvc_size", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_Section label="Pods" />
        <WS_FieldRow label="service_account" hint="optional">
          <input className="input mono" value={form.service_account} onChange={(e) => update("service_account", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="image_pull_secrets" hint="optional · one per row">
          <window.WorkspaceStringListEditor value={form.image_pull_secrets} onChange={(v) => update("image_pull_secrets", v)} placeholder="my-registry-secret" />
        </WS_FieldRow>
        <WS_FieldRow label="pull_policy">
          <select className="select mono" value={form.pull_policy_k8s} onChange={(e) => update("pull_policy_k8s", e.target.value)} style={{ width: "100%" }}>
            <option value="Always">Always</option>
            <option value="IfNotPresent">IfNotPresent</option>
            <option value="Never">Never</option>
          </select>
        </WS_FieldRow>
        <div style={{ marginTop: 12 }}>
          <button className="btn" style={{ padding: "4px 10px" }} onClick={() => setAdvancedOpen(!advancedOpen)}>
            <Icon name={advancedOpen ? "chevron-down" : "chevron-right"} size={11} />
            <span>Advanced (annotations, labels, security context, tolerations)</span>
          </button>
        </div>
        {advancedOpen && (<>
          <WS_FieldRow label="annotations" hint="key/value pairs">
            <window.WorkspacePairListEditor value={form.annotations} onChange={(v) => update("annotations", v)} keyPlaceholder="prometheus.io/scrape" valuePlaceholder="true" />
          </WS_FieldRow>
          <WS_FieldRow label="labels" hint="key/value pairs">
            <window.WorkspacePairListEditor value={form.labels} onChange={(v) => update("labels", v)} keyPlaceholder="app.kubernetes.io/name" valuePlaceholder="primer" />
          </WS_FieldRow>
          <WS_FieldRow label="node_selector" hint="key/value pairs">
            <window.WorkspacePairListEditor value={form.node_selector} onChange={(v) => update("node_selector", v)} keyPlaceholder="disktype" valuePlaceholder="ssd" />
          </WS_FieldRow>
          <WS_FieldRow label="pod_security_context" hint="JSON object · passthrough to PodSpec.securityContext">
            <window.WorkspaceJsonTextareaField value={form.pod_security_context} onChange={(v) => update("pod_security_context", v)} placeholder='{"runAsNonRoot": true, "fsGroup": 1000}' rows={4} />
          </WS_FieldRow>
          <WS_FieldRow label="container_security_context" hint="JSON object · passthrough to Container.securityContext">
            <window.WorkspaceJsonTextareaField value={form.container_security_context} onChange={(v) => update("container_security_context", v)} placeholder='{"allowPrivilegeEscalation": false}' rows={4} />
          </WS_FieldRow>
          <WS_FieldRow label="tolerations" hint="JSON array of toleration objects">
            <window.WorkspaceJsonTextareaField value={form.tolerations} onChange={(v) => update("tolerations", v)} placeholder='[{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}]' rows={4} />
          </WS_FieldRow>
        </>)}
      </>)}
    </Modal>
  );
}

function WorkspaceProviderDetail({ providerId, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const [tab, setTab] = React.useState("overview");
  const [showDelete, setShowDelete] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState(null);

  const detailKey = `ws:provider:${providerId}`;
  const detail = useResource(
    detailKey,
    (signal) => apiFetch("GET", `/workspace_providers/${encodeURIComponent(providerId)}`, null, { signal }),
    { deps: [providerId] }
  );

  const templates = useResource(
    `ws:provider:${providerId}:templates`,
    (signal) => apiFetch("GET", "/workspace_templates?limit=500", null, { signal }),
    { deps: [providerId] }
  );
  const referencingTemplates = React.useMemo(() => {
    const all = templates.data?.items ?? [];
    return all.filter((t) => t.provider_id === providerId);
  }, [templates.data, providerId]);

  const del = useMutation(
    () => apiFetch("DELETE", `/workspace_providers/${encodeURIComponent(providerId)}`),
    {
      invalidates: [WP_LIST_KEY],
      onSuccess: () => {
        if (pushToast) pushToast({ kind: "warning", title: "Provider deleted", detail: providerId });
        setShowDelete(false);
        navigate("/workspaces/providers");
      },
      onError: (err) => {
        if (err?.status === 409) {
          setDeleteError(err.detail || "Cannot delete — templates still reference this provider.");
        } else {
          setShowDelete(false);
          _wpToastErr(pushToast, "Delete failed")(err);
        }
      },
    },
  );

  if (detail.loading && !detail.data) {
    return <div className="panel"><div className="panel-body" style={{ padding: 18 }}><span className="muted text-sm">Loading provider…</span></div></div>;
  }
  if (detail.error && !detail.data) {
    return <Banner kind="error" title={detail.error.title || `Couldn't load ${providerId}`} detail={detail.error.detail || detail.error.message} actions={<Btn size="sm" icon="refresh" onClick={detail.refetch}>Retry</Btn>} />;
  }
  const p = detail.data;
  if (!p) return null;

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel">
        <div className="panel-body" style={{ padding: "14px 18px", display: "flex", alignItems: "center", gap: 14 }}>
          <window.WorkspaceBackendBadge kind={p.provider} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{p.id}</div>
            <div className="muted text-sm mono" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {_wpSummary(p)}
            </div>
          </div>
          <Btn size="sm" kind="danger" icon="trash" onClick={() => { setDeleteError(null); setShowDelete(true); }}>Delete</Btn>
        </div>
        <div style={{ display: "flex", alignItems: "center", borderTop: "1px solid var(--border)", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {[
            { id: "overview", label: "Overview", icon: "info" },
            { id: "config", label: "Config", icon: "settings" },
            { id: "templates", label: "Templates", icon: "tools", count: referencingTemplates.length },
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
            >
              <Icon name={t.icon} size={13} />
              {t.label}
              {t.count != null && t.count > 0 && <span className="count" style={{ marginLeft: 4 }}>{t.count}</span>}
            </button>
          ))}
        </div>
        <div style={{ padding: 18 }}>
          {tab === "overview" && (
            <dl className="kv" style={{ gridTemplateColumns: "200px 1fr" }}>
              <dt>id</dt><dd className="mono">{p.id}</dd>
              <dt>backend</dt><dd><window.WorkspaceBackendBadge kind={p.provider} /></dd>
              <dt>summary</dt><dd className="mono">{_wpSummary(p)}</dd>
            </dl>
          )}
          {tab === "config" && (
            <div>
              <div className="muted text-sm mb-2">Raw provider config (server-redacted secrets, where applicable).</div>
              <div className="code-block" style={{ maxHeight: 420, overflow: "auto", whiteSpace: "pre", fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}>
                {JSON.stringify({ id: p.id, provider: p.provider, config: p.config }, null, 2)}
              </div>
            </div>
          )}
          {tab === "templates" && (
            referencingTemplates.length === 0 ? (
              <div className="empty" style={{ padding: 20 }}>
                <div className="head">No templates bound</div>
                <div className="sub">No workspace_templates reference this provider yet.</div>
              </div>
            ) : (
              <table className="tbl">
                <thead><tr><th>ID</th><th>Description</th><th>Backend</th></tr></thead>
                <tbody>
                  {referencingTemplates.map((t) => (
                    <tr key={t.id} onClick={() => navigate(`/workspaces/templates/${encodeURIComponent(t.id)}`)} style={{ cursor: "pointer" }}>
                      <td className="mono">{t.id}</td>
                      <td className="muted">{t.description || <span style={{ color: "var(--text-4)" }}>—</span>}</td>
                      <td><window.WorkspaceBackendBadge kind={t.backend?.kind} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </div>
      </div>

      {showDelete && (
        <Modal
          title={`Delete ${providerId}?`}
          danger
          onClose={() => { setShowDelete(false); setDeleteError(null); }}
          footer={
            <>
              <Btn kind="ghost" onClick={() => { setShowDelete(false); setDeleteError(null); }}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                disabled={referencingTemplates.length > 0 || del.loading}
                onClick={() => del.mutate().catch(() => { /* onError */ })}
              >
                {del.loading ? "Deleting…" : "Delete provider"}
              </Btn>
            </>
          }
        >
          {deleteError ? (
            <><strong style={{ color: "var(--red)" }}>409 Conflict</strong> — {deleteError}</>
          ) : referencingTemplates.length > 0 ? (
            <>
              <strong style={{ color: "var(--red)" }}>409 Conflict</strong> — this provider is referenced by{" "}
              <strong>{referencingTemplates.length}</strong> template{referencingTemplates.length === 1 ? "" : "s"}:
              <ul>{referencingTemplates.slice(0, 6).map((t) => <li key={t.id} className="mono">{t.id}</li>)}</ul>
              Delete or repoint those templates first.
            </>
          ) : (
            <>
              No templates reference this provider. Deletion is safe.
              <ul>
                <li>The provider row is removed.</li>
                <li>Materialised workspaces under it stay (their template was their own row).</li>
              </ul>
            </>
          )}
        </Modal>
      )}
    </div>
  );
}

window.WorkspaceProvidersPage = WorkspaceProvidersPage;
window.WorkspaceProviderCreateModal = WorkspaceProviderCreateModal;
window.WorkspaceProviderDetail = WorkspaceProviderDetail;
