/* global React, Icon, Btn, Modal, Banner, CardList, Card, Fab, WS_FieldRow, WS_Section */

const WP_LIST_KEY = "ws:providers";

// Reserved bootstrap-managed provider ids — mirrors
// RESERVED_WORKSPACE_PROVIDER_IDS in primer/api/registries/provider_registry.py.
// These are auto-recreated on boot from config and are read-only via the API.
const RESERVED_WORKSPACE_PROVIDER_IDS = ["local"];

function _wpIsReserved(id) {
  return RESERVED_WORKSPACE_PROVIDER_IDS.indexOf(id) !== -1;
}

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
    const rt = p.config.runtime || "?";
    const conn = p.config.connection?.kind || "?";
    const reach = p.config.reachability?.kind || "?";
    return `${rt} · ${conn} · ${reach}`;
  }
  if (p.provider === "kubernetes") {
    const variant = p.config.variant || "system";
    const ns = p.config.namespace || "default";
    const conn = p.config.connection?.kind || "?";
    return `${variant} · ${ns} · ${conn}`;
  }
  return "";
}

function WorkspaceProvidersPage({ pushToast }) {
  const { useResource, useRouter, useViewport, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const { isMobile } = useViewport();
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
        ) : isMobile ? (
          <CardList
            items={items}
            empty="No workspace providers."
            renderCard={(p) => (
              <Card
                title={p.id}
                subtitle={p.provider}
                pill={<window.WorkspaceBackendBadge kind={p.provider} />}
                meta={_wpSummary(p)}
                onClick={() => navigate(`/workspaces/providers/${encodeURIComponent(p.id)}`)}
              />
            )}
          />
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
        {isMobile && (
          <Fab icon="plus" label="New provider" onClick={() => setCreateOpen(true)} />
        )}
      </div>
      {modal}
    </>
  );
}

// Invert the form-state builder used at submit time: given a stored
// WorkspaceProvider row, return the flat form state the modal expects.
// Keeps the round-trip lossless for all three backend kinds.
function _wpFromProvider(row) {
  const defaults = {
    id: "",
    backend: "local",
    path: "",
    c_runtime: "docker",
    c_conn_kind: "socket",
    c_socket_path: "/var/run/docker.sock",
    c_remote_url: "",
    c_remote_tls_ca: "",
    c_remote_tls_cert: "",
    c_remote_tls_key: "",
    c_reach_kind: "host_port",
    c_bind_host: "127.0.0.1",
    c_network_name: "",
    c_image_pull_secrets: [],
    k_variant: "system",
    k_conn_kind: "in_cluster",
    k_kubeconfig_path: "",
    k_kubeconfig_context: "",
    k_sat_apiserver_url: "",
    k_sat_ca_data: "",
    k_sat_token: "",
    k_sat_namespace: "default",
    k_namespace: "primer",
    k_reach_kind: "in_cluster",
    k_ingress_url_template: "",
    k_image_pull_secrets: [],
  };
  if (!row) return defaults;
  const out = { ...defaults, id: row.id || "", backend: row.provider || "local" };
  const cfg = row.config || {};
  if (row.provider === "local") {
    out.path = cfg.root_path || cfg.path || "";
  } else if (row.provider === "container") {
    out.c_runtime = cfg.runtime || "docker";
    const conn = cfg.connection || {};
    out.c_conn_kind = conn.kind || "socket";
    if (conn.kind === "socket") {
      out.c_socket_path = conn.socket_path || "";
    } else if (conn.kind === "remote") {
      out.c_remote_url = conn.url || "";
      out.c_remote_tls_ca = conn.tls_ca || "";
      out.c_remote_tls_cert = conn.tls_cert || "";
      out.c_remote_tls_key = conn.tls_key || "";
    }
    const reach = cfg.reachability || {};
    out.c_reach_kind = reach.kind || "host_port";
    if (reach.kind === "host_port") {
      out.c_bind_host = reach.bind_host || "127.0.0.1";
    } else if (reach.kind === "bridge_network") {
      out.c_network_name = reach.network_name || "";
    }
    out.c_image_pull_secrets = Array.isArray(cfg.image_pull_secrets) ? cfg.image_pull_secrets : [];
  } else if (row.provider === "kubernetes") {
    out.k_variant = cfg.variant || "system";
    const conn = cfg.connection || {};
    out.k_conn_kind = conn.kind || "in_cluster";
    if (conn.kind === "kubeconfig") {
      out.k_kubeconfig_path = conn.path || "";
      out.k_kubeconfig_context = conn.context || "";
    } else if (conn.kind === "service_account_token") {
      out.k_sat_apiserver_url = conn.apiserver_url || "";
      out.k_sat_ca_data = conn.ca_data || "";
      out.k_sat_token = conn.token || "";
      out.k_sat_namespace = conn.namespace || "default";
    }
    out.k_namespace = cfg.namespace || "primer";
    const reach = cfg.reachability || {};
    out.k_reach_kind = reach.kind || "in_cluster";
    if (reach.kind === "ingress") {
      out.k_ingress_url_template = reach.url_template || "";
    }
    out.k_image_pull_secrets = Array.isArray(cfg.image_pull_secrets) ? cfg.image_pull_secrets : [];
  }
  return out;
}

function WorkspaceProviderCreateModal({ onClose, pushToast, existing = null }) {
  const { useMutation, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const isEdit = !!existing;

  const [form, setForm] = React.useState(() => _wpFromProvider(existing));
  const [fieldErrors, setFieldErrors] = React.useState({});
  const update = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const create = useMutation(
    (body) => isEdit
      ? apiFetch("PUT", `/workspace_providers/${encodeURIComponent(existing.id)}`, body)
      : apiFetch("POST", "/workspace_providers", body),
    {
      invalidates: isEdit
        ? [WP_LIST_KEY, `ws:provider:${existing.id}`]
        : [WP_LIST_KEY],
      onSuccess: (row) => {
        onClose();
        if (pushToast) pushToast({
          kind: "success",
          title: isEdit ? "Provider updated" : "Provider created",
          detail: row.id,
        });
        if (!isEdit) {
          navigate(`/workspaces/providers/${encodeURIComponent(row.id)}`);
        }
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
          _wpToastErr(pushToast, isEdit ? "Save failed" : "Create failed")(err);
        }
      },
    },
  );

  const submit = () => {
    const errs = {};
    if (!form.id) errs.id = "value is required";
    if (form.backend === "local" && !form.path) errs.path = "value is required";
    if (form.backend === "container") {
      if (form.c_conn_kind === "socket" && !form.c_socket_path) errs.c_socket_path = "value is required";
      if (form.c_conn_kind === "remote" && !form.c_remote_url) errs.c_remote_url = "value is required";
      if (form.c_reach_kind === "bridge_network" && !form.c_network_name) errs.c_network_name = "value is required";
    }
    if (form.backend === "kubernetes") {
      if (form.k_conn_kind === "kubeconfig" && !form.k_kubeconfig_path) errs.k_kubeconfig_path = "value is required";
      if (form.k_conn_kind === "service_account_token") {
        if (!form.k_sat_apiserver_url) errs.k_sat_apiserver_url = "value is required";
        if (!form.k_sat_ca_data) errs.k_sat_ca_data = "value is required";
        if (!form.k_sat_token) errs.k_sat_token = "value is required";
      }
      if (!form.k_namespace) errs.k_namespace = "value is required";
      if (form.k_reach_kind === "ingress" && !form.k_ingress_url_template) errs.k_ingress_url_template = "value is required";
    }
    if (Object.keys(errs).length > 0) {
      setFieldErrors(errs);
      return;
    }
    setFieldErrors({});

    let config;
    if (form.backend === "local") {
      config = { kind: "local", path: form.path };
    } else if (form.backend === "container") {
      let connection;
      if (form.c_conn_kind === "socket") {
        connection = { kind: "socket", socket_path: form.c_socket_path };
      } else {
        connection = { kind: "remote", url: form.c_remote_url };
        if (form.c_remote_tls_ca) connection.tls_ca = form.c_remote_tls_ca;
        if (form.c_remote_tls_cert) connection.tls_cert = form.c_remote_tls_cert;
        if (form.c_remote_tls_key) connection.tls_key = form.c_remote_tls_key;
      }
      let reachability;
      if (form.c_reach_kind === "host_port") {
        reachability = { kind: "host_port", bind_host: form.c_bind_host || "127.0.0.1" };
      } else {
        reachability = { kind: "bridge_network", network_name: form.c_network_name };
      }
      config = {
        kind: "container",
        runtime: form.c_runtime,
        connection,
        reachability,
        image_pull_secrets: form.c_image_pull_secrets || [],
      };
    } else {
      let connection;
      if (form.k_conn_kind === "in_cluster") {
        connection = { kind: "in_cluster" };
      } else if (form.k_conn_kind === "kubeconfig") {
        connection = { kind: "kubeconfig", path: form.k_kubeconfig_path };
        if (form.k_kubeconfig_context) connection.context = form.k_kubeconfig_context;
      } else {
        connection = {
          kind: "service_account_token",
          apiserver_url: form.k_sat_apiserver_url,
          ca_data: form.k_sat_ca_data,
          token: form.k_sat_token,
        };
        if (form.k_sat_namespace) connection.namespace = form.k_sat_namespace;
      }
      let reachability;
      if (form.k_reach_kind === "in_cluster") {
        reachability = { kind: "in_cluster" };
      } else {
        reachability = { kind: "ingress", url_template: form.k_ingress_url_template };
      }
      config = {
        kind: "kubernetes",
        variant: form.k_variant,
        connection,
        namespace: form.k_namespace,
        reachability,
        image_pull_secrets: form.k_image_pull_secrets || [],
      };
    }
    const body = { id: form.id, provider: form.backend, config };
    create.mutate(body).catch(() => { /* onError handled */ });
  };

  const isLocal = form.backend === "local";
  const isContainer = form.backend === "container";
  const isK8s = form.backend === "kubernetes";

  return (
    <Modal
      title={isEdit ? `Edit workspace provider · ${existing.id}` : "New workspace provider"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon={isEdit ? "check" : "plus"} disabled={create.loading} onClick={submit}>
            {create.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save changes" : "Create")}
          </Btn>
        </>
      }
    >
      <WS_FieldRow label="id" hint={isEdit ? "locked — id cannot change after create" : "must be unique"} err={fieldErrors.id}>
        <input className="input mono" value={form.id} onChange={(e) => update("id", e.target.value)} placeholder="local-dev" disabled={isEdit} style={{ width: "100%" }} />
      </WS_FieldRow>
      <WS_FieldRow label="backend" hint={isEdit ? "locked — backend kind cannot change after create" : undefined}>
        <select className="select mono" value={form.backend} onChange={(e) => update("backend", e.target.value)} disabled={isEdit} style={{ width: "100%" }}>
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
        <WS_Section label="Runtime" />
        <WS_FieldRow label="runtime">
          <select className="select mono" value={form.c_runtime} onChange={(e) => update("c_runtime", e.target.value)} style={{ width: "100%" }}>
            <option value="docker">docker</option>
            <option value="podman">podman</option>
            <option value="containerd">containerd</option>
          </select>
        </WS_FieldRow>
        <WS_Section label="Connection" />
        <WS_FieldRow label="connection" hint="how the platform reaches the runtime API">
          <select className="select mono" value={form.c_conn_kind} onChange={(e) => update("c_conn_kind", e.target.value)} style={{ width: "100%" }}>
            <option value="socket">socket (local unix socket)</option>
            <option value="remote">remote (tcp endpoint, optional mTLS)</option>
          </select>
        </WS_FieldRow>
        {form.c_conn_kind === "socket" && (
          <WS_FieldRow label="socket_path" err={fieldErrors.c_socket_path}>
            <input className="input mono" value={form.c_socket_path} onChange={(e) => update("c_socket_path", e.target.value)} placeholder="/var/run/docker.sock" style={{ width: "100%" }} />
          </WS_FieldRow>
        )}
        {form.c_conn_kind === "remote" && (<>
          <WS_FieldRow label="url" err={fieldErrors.c_remote_url}>
            <input className="input mono" value={form.c_remote_url} onChange={(e) => update("c_remote_url", e.target.value)} placeholder="tcp://docker:2375" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="tls_ca" hint="optional · PEM CA cert for mTLS">
            <textarea className="input mono" value={form.c_remote_tls_ca} onChange={(e) => update("c_remote_tls_ca", e.target.value)} rows={3} placeholder="-----BEGIN CERTIFICATE-----" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="tls_cert" hint="optional · PEM client cert for mTLS">
            <textarea className="input mono" value={form.c_remote_tls_cert} onChange={(e) => update("c_remote_tls_cert", e.target.value)} rows={3} placeholder="-----BEGIN CERTIFICATE-----" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="tls_key" hint="optional · PEM client key for mTLS (secret)">
            <textarea className="input mono" value={form.c_remote_tls_key} onChange={(e) => update("c_remote_tls_key", e.target.value)} rows={3} placeholder="-----BEGIN PRIVATE KEY-----" style={{ width: "100%" }} />
          </WS_FieldRow>
        </>)}
        <WS_Section label="Reachability" />
        <WS_FieldRow label="reachability" hint="how the platform reaches primer-runtime inside each workspace">
          <select className="select mono" value={form.c_reach_kind} onChange={(e) => update("c_reach_kind", e.target.value)} style={{ width: "100%" }}>
            <option value="host_port">host_port (publish to host interface)</option>
            <option value="bridge_network">bridge_network (shared docker network)</option>
          </select>
        </WS_FieldRow>
        {form.c_reach_kind === "host_port" && (
          <WS_FieldRow label="bind_host" hint="host interface · 127.0.0.1 keeps it loopback-only">
            <input className="input mono" value={form.c_bind_host} onChange={(e) => update("c_bind_host", e.target.value)} placeholder="127.0.0.1" style={{ width: "100%" }} />
          </WS_FieldRow>
        )}
        {form.c_reach_kind === "bridge_network" && (
          <WS_FieldRow label="network_name" err={fieldErrors.c_network_name} hint="docker network shared by primer and workspace containers">
            <input className="input mono" value={form.c_network_name} onChange={(e) => update("c_network_name", e.target.value)} placeholder="primer-net" style={{ width: "100%" }} />
          </WS_FieldRow>
        )}
        <WS_Section label="Registry auth" />
        <WS_FieldRow label="image_pull_secrets" hint="optional · names of registry-auth secret refs">
          <window.WorkspaceStringListEditor value={form.c_image_pull_secrets} onChange={(v) => update("c_image_pull_secrets", v)} placeholder="my-registry-secret" />
        </WS_FieldRow>
      </>)}

      {isK8s && (<>
        <WS_Section label="Variant" />
        <WS_FieldRow label="variant" hint="materialisation strategy">
          <select className="select mono" value={form.k_variant} onChange={(e) => update("k_variant", e.target.value)} style={{ width: "100%" }}>
            <option value="system">system (StatefulSet + PVC)</option>
            <option value="agent_sandbox" disabled>agent_sandbox (coming soon)</option>
          </select>
        </WS_FieldRow>
        <WS_Section label="Connection" />
        <WS_FieldRow label="connection" hint="how the platform reaches the kube apiserver">
          <select className="select mono" value={form.k_conn_kind} onChange={(e) => update("k_conn_kind", e.target.value)} style={{ width: "100%" }}>
            <option value="in_cluster">in_cluster (service-account auth)</option>
            <option value="kubeconfig">kubeconfig (file path + context)</option>
            <option value="service_account_token">service_account_token (out-of-cluster)</option>
          </select>
        </WS_FieldRow>
        {form.k_conn_kind === "kubeconfig" && (<>
          <WS_FieldRow label="path" err={fieldErrors.k_kubeconfig_path} hint="path to kubeconfig file">
            <input className="input mono" value={form.k_kubeconfig_path} onChange={(e) => update("k_kubeconfig_path", e.target.value)} placeholder="/etc/kubernetes/admin.conf" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="context" hint="optional · current-context if blank">
            <input className="input mono" value={form.k_kubeconfig_context} onChange={(e) => update("k_kubeconfig_context", e.target.value)} style={{ width: "100%" }} />
          </WS_FieldRow>
        </>)}
        {form.k_conn_kind === "service_account_token" && (<>
          <WS_FieldRow label="apiserver_url" err={fieldErrors.k_sat_apiserver_url}>
            <input className="input mono" value={form.k_sat_apiserver_url} onChange={(e) => update("k_sat_apiserver_url", e.target.value)} placeholder="https://kube.example.com:6443" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="ca_data" err={fieldErrors.k_sat_ca_data} hint="PEM cluster CA cert">
            <textarea className="input mono" value={form.k_sat_ca_data} onChange={(e) => update("k_sat_ca_data", e.target.value)} rows={3} placeholder="-----BEGIN CERTIFICATE-----" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="token" err={fieldErrors.k_sat_token} hint="bearer token (secret)">
            <input className="input mono" type="password" value={form.k_sat_token} onChange={(e) => update("k_sat_token", e.target.value)} style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="namespace" hint="namespace claimed by the token (informational)">
            <input className="input mono" value={form.k_sat_namespace} onChange={(e) => update("k_sat_namespace", e.target.value)} style={{ width: "100%" }} />
          </WS_FieldRow>
        </>)}
        <WS_Section label="Workspace placement" />
        <WS_FieldRow label="namespace" err={fieldErrors.k_namespace} hint="namespace where workspaces are created">
          <input className="input mono" value={form.k_namespace} onChange={(e) => update("k_namespace", e.target.value)} placeholder="primer" style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_Section label="Reachability" />
        <WS_FieldRow label="reachability" hint="how the platform reaches primer-runtime inside workspace pods">
          <select className="select mono" value={form.k_reach_kind} onChange={(e) => update("k_reach_kind", e.target.value)} style={{ width: "100%" }}>
            <option value="in_cluster">in_cluster (headless-service DNS)</option>
            <option value="ingress">ingress (operator URL pattern)</option>
          </select>
        </WS_FieldRow>
        {form.k_reach_kind === "ingress" && (
          <WS_FieldRow label="url_template" err={fieldErrors.k_ingress_url_template} hint="wss:// URL with {workspace_id} placeholder">
            <input className="input mono" value={form.k_ingress_url_template} onChange={(e) => update("k_ingress_url_template", e.target.value)} placeholder="wss://{workspace_id}.ws.example.com" style={{ width: "100%" }} />
          </WS_FieldRow>
        )}
        <WS_Section label="Registry auth" />
        <WS_FieldRow label="image_pull_secrets" hint="optional · names of pre-created imagePullSecrets in the namespace">
          <window.WorkspaceStringListEditor value={form.k_image_pull_secrets} onChange={(v) => update("k_image_pull_secrets", v)} placeholder="my-registry-secret" />
        </WS_FieldRow>
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
  const [editOpen, setEditOpen] = React.useState(false);

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
          <Btn
            size="sm"
            icon="edit"
            onClick={() => setEditOpen(true)}
            disabled={_wpIsReserved(p.id)}
            title={_wpIsReserved(p.id) ? "Reserved providers are managed by the platform — recreate via config rather than editing" : undefined}
          >
            Edit
          </Btn>
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

      {editOpen && (
        <WorkspaceProviderCreateModal
          existing={p}
          onClose={() => setEditOpen(false)}
          pushToast={pushToast}
        />
      )}

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
