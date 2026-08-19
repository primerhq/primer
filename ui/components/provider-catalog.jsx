/* global React, Icon, Btn, StatusPill, Banner */
// ============================================================================
// Unified provider catalog (S4 P2).
//
// One surface for every provider class: a left rail of classes, a list of
// instances for the selected class, and a form for the selected instance.
// Classes that already have a purpose-built panel (vector stores,
// workspaces, channels) host that component rather than reimplementing it.
//
// MOUNT CONTRACT (amendment M11d): props are exactly
// {initialClass, initialInstanceId, onNavigate} and nothing else. S8
// re-hosts this as an overlay, so any reach for the console's own
// routing or address bar would have to be unpicked there. Navigation
// leaves through onNavigate as a structured ref.
// ============================================================================

const PROVIDER_CLASSES = [
  { key: "llm", label: "LLM", plural: "llm_providers", form: "crud" },
  { key: "embedding", label: "Embedding", plural: "embedding_providers", form: "crud" },
  { key: "cross_encoder", label: "Cross-Encoder", plural: "cross_encoder_providers", form: "crud" },
  { key: "ssp", label: "Vector Stores", plural: "ssp", form: "panel",
    panel: () => window.SSPListPage },
  { key: "stt", label: "Speech-to-Text", plural: "stt_providers", form: "crud" },
  { key: "tts", label: "Text-to-Speech", plural: "tts_providers", form: "crud" },
  { key: "web_search", label: "Web Search", plural: "web_search_providers", form: "crud" },
  { key: "web_fetch", label: "Web Fetch", plural: "web_fetch_providers", form: "crud" },
  { key: "artifact_storage", label: "Artifact Storage", plural: "artifact_storage_providers", form: "crud" },
  { key: "workspace", label: "Workspaces", plural: "workspace_providers", form: "panel",
    panel: () => window.WorkspaceProvidersPage },
  { key: "channel", label: "Channels", plural: "channel_providers", form: "panel",
    panel: () => window.ChannelProvidersPage },
];

function PC_ClassRail({ classes, selected, onSelect }) {
  return (
    <div className="col pc-rail" style={{ minWidth: 180, gap: 2 }}>
      {classes.map((cls) => (
        <a
          key={cls.key}
          className={`pc-rail-item${cls.key === selected ? " selected" : ""}`}
          onClick={() => onSelect(cls.key)}
        >
          {cls.label}
        </a>
      ))}
    </div>
  );
}

function PC_InstanceList({ cls, selectedId, onSelect, reloadKey }) {
  const { useResource, apiFetch } = window.primerApi;
  const rows = useResource(
    `pc:list:${cls.plural}:${reloadKey}`,
    (signal) => apiFetch("GET", `/${cls.plural}?limit=200`, null, { signal }),
    { pollMs: null },
  );
  const items = rows.data?.items ?? [];

  if (rows.error) {
    return (
      <Banner kind="error" title={`Could not load ${cls.label}`}>
        {String(rows.error.detail || rows.error.message || rows.error)}
      </Banner>
    );
  }
  return (
    <div className="col" style={{ gap: 2, minWidth: 200 }}>
      {items.length === 0 ? (
        <div className="muted text-sm">No {cls.label} providers yet.</div>
      ) : null}
      {items.map((row) => (
        <a
          key={row.id}
          className={`pc-instance${row.id === selectedId ? " selected" : ""} mono text-sm`}
          onClick={() => onSelect(row.id)}
        >
          {row.id}
        </a>
      ))}
    </div>
  );
}

function ProviderCatalog({ initialClass, initialInstanceId, onNavigate }) {
  const [classKey, setClassKey] = React.useState(
    initialClass || PROVIDER_CLASSES[0].key,
  );
  const [instanceId, setInstanceId] = React.useState(initialInstanceId || null);
  const [reloadKey, setReloadKey] = React.useState(0);
  const [draft, setDraft] = React.useState({});

  const cls = PROVIDER_CLASSES.find((c) => c.key === classKey) || PROVIDER_CLASSES[0];

  const selectClass = (key) => {
    setClassKey(key);
    setInstanceId(null);
    if (typeof onNavigate === "function") {
      onNavigate({ kind: "provider-class", classKey: key });
    }
  };

  const save = async (body) => {
    const { apiFetch } = window.primerApi;
    const method = body.id && instanceId === body.id ? "PUT" : "POST";
    const path = method === "PUT"
      ? `/${cls.plural}/${encodeURIComponent(body.id)}`
      : `/${cls.plural}`;
    await apiFetch(method, path, body);
    setReloadKey((k) => k + 1);
  };

  const selectInstance = (id) => {
    setInstanceId(id);
    if (typeof onNavigate === "function") {
      onNavigate({ kind: "provider-instance", classKey: cls.key, id });
    }
  };

  // Classes with a purpose-built panel host that component rather than
  // a generic form: vector stores, workspaces and channels each carry
  // behaviour a parameterised form would have to special-case anyway.
  let body;
  if (cls.form === "panel") {
    const Panel = cls.panel();
    body = Panel ? (
      <Panel />
    ) : (
      <Banner kind="info" title={`${cls.label} panel unavailable`}>
        That panel component is not loaded in this bundle.
      </Banner>
    );
  } else {
    body = (
      <div className="row" style={{ gap: 16, alignItems: "flex-start" }}>
        <PC_InstanceList
          cls={cls}
          selectedId={instanceId}
          onSelect={selectInstance}
          reloadKey={reloadKey}
        />
        <div className="col" style={{ flex: 1, minWidth: 0 }}>
          <window.PC_ProviderForm
            plural={cls.plural}
            typesPath={`/${cls.plural}/_types`}
            value={draft}
            onChange={setDraft}
            onSubmit={save}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="row pc-catalog" style={{ gap: 20, alignItems: "flex-start" }}>
      <PC_ClassRail
        classes={PROVIDER_CLASSES}
        selected={cls.key}
        onSelect={selectClass}
      />
      <div className="col" style={{ flex: 1, minWidth: 0 }}>
        <h2 className="text-lg">{cls.label}</h2>
        {body}
      </div>
    </div>
  );
}

window.PROVIDER_CLASSES = PROVIDER_CLASSES;
window.ProviderCatalog = ProviderCatalog;
