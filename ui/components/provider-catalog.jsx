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
  { key: "llm", label: "LLM", plural: "llm_providers", form: "crud", profiles: true, invalidate: true },
  { key: "embedding", label: "Embedding", plural: "embedding_providers", form: "crud", invalidate: true },
  { key: "cross_encoder", label: "Cross-Encoder", plural: "cross_encoder_providers", form: "crud", invalidate: true },
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

// The two actions the deleted per-class detail view had. Invalidate only
// appears where the endpoint does: the model-family registry caches an
// adapter per row id (api/registries/provider_registry.py), while the
// sibling registries drop their entry on the CRUD hook and need no
// button.
function PC_RowActions({ klass, rowId, onChanged }) {
  const { apiFetch } = window.primerApi;
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [err, setErr] = React.useState("");
  const [busy, setBusy] = React.useState("");

  const run = async (what, method, path) => {
    setErr("");
    setBusy(what);
    try {
      await apiFetch(method, path);
      setConfirmDelete(false);
      if (onChanged) onChanged(what);
    } catch (err) {
      // 403 means a reserved row; the backend detail says which and why
      // (routers/providers.py:116-135). Show it where the click was
      // rather than hiding the button behind a copied id list.
      const detail = err && err.detail ? err.detail : null;
      const message =
        (detail && (detail.message || detail)) || (err && err.title) || String(err);
      setErr(err && err.status ? `${err.status}: ${message}` : String(message));
    } finally {
      setBusy("");
    }
  };

  const encoded = encodeURIComponent(rowId);
  return (
    <div className="row-actions">
      {klass.invalidate && (
        <Btn
          type="button"
          kind="ghost"
          disabled={busy !== ""}
          data-testid="provider-row-invalidate"
          onClick={() => run("invalidate", "POST", `/${klass.plural}/${encoded}/invalidate`)}
        >
          Invalidate
        </Btn>
      )}
      {confirmDelete ? (
        <>
          <Btn
            type="button"
            kind="danger"
            disabled={busy !== ""}
            data-testid="provider-row-delete-confirm"
            onClick={() => run("delete", "DELETE", `/${klass.plural}/${encoded}`)}
          >
            Confirm delete
          </Btn>
          <Btn type="button" kind="ghost" onClick={() => setConfirmDelete(false)}>
            Cancel
          </Btn>
        </>
      ) : (
        <Btn
          type="button"
          kind="ghost"
          data-testid="provider-row-delete"
          onClick={() => { setErr(""); setConfirmDelete(true); }}
        >
          Delete
        </Btn>
      )}
      {err && (
        <div className="field-help" data-testid="provider-row-error">{err}</div>
      )}
    </div>
  );
}

function PC_InstanceList({ klass, selectedId, onSelect, onRegisterRefetch }) {
  const { usePagedList, useViewport } = window.primerApi;
  const { isMobile } = useViewport();
  const list = usePagedList({
    key: `catalog:${klass.plural}`,
    path: `/${klass.plural}`,
    pageSize: 25,
    pollMs: null,
    resetKey: { plural: klass.plural },
  });

  // Hand the refetch up so a row action refreshes this list in place. A
  // remount would work too, but it would also throw away the page the
  // operator was on.
  React.useEffect(() => {
    if (onRegisterRefetch) onRegisterRefetch(() => list.refetch());
  }, [onRegisterRefetch, list.refetch]);

  if (list.error) {
    return (
      <Banner
        kind="error"
        title={`Could not load ${klass.label}`}
        detail={String(list.error.detail || list.error.title || list.error)}
      />
    );
  }
  const items = list.items || [];
  if (!list.loading && items.length === 0) {
    return (
      <div className="empty-state" data-testid={`provider-empty-${klass.key}`}>
        <h3>No {klass.label} providers yet</h3>
        <p>Add one to make this capability available.</p>
      </div>
    );
  }

  // Card takes no className (ui/components/shared/card-list.jsx:22), so
  // selection rides `pill`; CardList is items+renderCard, never children.
  const renderRow = (row) => (
    <Card
      title={<span className="mono">{row.id}</span>}
      subtitle={row.provider || null}
      pill={row.id === selectedId ? "selected" : null}
      onClick={() => onSelect(row.id)}
    />
  );

  return (
    <div data-testid={`provider-instances-${klass.key}`}>
      {isMobile ? (
        <CardList
          items={items}
          empty={`No ${klass.label} providers yet.`}
          renderCard={renderRow}
        />
      ) : (
        <div className="provider-rows">
          {items.map((row) => (
            <React.Fragment key={row.id}>{renderRow(row)}</React.Fragment>
          ))}
        </div>
      )}
      <Pager pager={list} label="providers" />
    </div>
  );
}


// ---------------------------------------------------------------------------
// Model profiles: LLM instances only
// ---------------------------------------------------------------------------

function PC_ProfilesPanel({ providerId }) {
  const { useResource, apiFetch } = window.primerApi;
  const [open, setOpen] = React.useState(false);
  const models = useResource(
    `pc:models:${providerId}`,
    (signal) => apiFetch(
      "GET", `/llm_providers/${encodeURIComponent(providerId)}/models`, null, { signal },
    ),
    { pollMs: null },
  );
  const discovered_models = models.data?.models ?? [];

  return (
    <div className="col" style={{ gap: 8 }} data-testid="provider-profiles">
      <div className="muted text-sm">
        Model profiles bind a model on this provider to defaults an agent
        can name. Only LLM providers carry them.
      </div>
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        {discovered_models.map((m) => (
          <span key={m} className="mono text-sm chip">{m}</span>
        ))}
      </div>
      <Btn icon="plus" kind="ghost" onClick={() => setOpen(true)}>New profile</Btn>
      {open && window.MP_ProfileModal ? (
        <window.MP_ProfileModal
          providerId={providerId}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Active defaults, edited in place (amendment M11c)
// ---------------------------------------------------------------------------

function PC_ActiveSpeechPanel() {
  const { useResource, apiFetch } = window.primerApi;
  const [reloadKey, setReloadKey] = React.useState(0);
  const [draft, setDraft] = React.useState(null);

  const active = useResource(
    `pc:speech-active:${reloadKey}`,
    (signal) => apiFetch("GET", "/speech_active_config", null, { signal }),
    { pollMs: null },
  );
  const voices = useResource(
    "pc:voices",
    (signal) => apiFetch("GET", "/audio/voices", null, { signal }),
    { pollMs: null },
  );

  React.useEffect(() => {
    if (active.data && draft === null) setDraft(active.data);
  }, [active.data, draft]);

  const row = draft || active.data || {};
  const voiceNames = voices.data?.voices ?? [];

  const save = async () => {
    await apiFetch("PUT", "/speech_active_config", {
      stt_provider_id: row.stt_provider_id || null,
      tts_provider_id: row.tts_provider_id || null,
      tts_voice: row.tts_voice || null,
    });
    setReloadKey((k) => k + 1);
  };

  return (
    <div className="col" style={{ gap: 8 }} data-testid="active-speech-config">
      <div className="muted text-sm">
        Install-wide speech defaults. An agent may override the voice.
      </div>
      <input
        className="input mono"
        placeholder="stt_provider_id"
        value={row.stt_provider_id || ""}
        onChange={(e) => setDraft({ ...row, stt_provider_id: e.target.value })}
      />
      <input
        className="input mono"
        placeholder="tts_provider_id"
        value={row.tts_provider_id || ""}
        onChange={(e) => setDraft({ ...row, tts_provider_id: e.target.value })}
      />
      <select
        className="input"
        value={row.tts_voice || ""}
        onChange={(e) => setDraft({ ...row, tts_voice: e.target.value })}
      >
        <option value="">(provider default voice)</option>
        {voiceNames.map((v) => <option key={v} value={v}>{v}</option>)}
      </select>
      <Btn onClick={save}>Save defaults</Btn>
    </div>
  );
}

function PC_ActiveWebSearchPanel() {
  const { apiFetch, useResource } = window.primerApi;

  const active = useResource(
    "catalog:active-web-search",
    (signal) => apiFetch("GET", "/web_search_active_config", null, { signal }),
    { pollMs: 0 },
  );
  const rows = useResource(
    "catalog:web-search-rows",
    (signal) => apiFetch("GET", "/web_search_providers?limit=200", null, { signal }),
    { pollMs: 0 },
  );

  const config = (active.data && active.data.config) || {};
  const mode = config.mode || "single";
  const items = (rows.data && rows.data.items) || [];

  const save = async (next) => {
    await apiFetch("PUT", "/web_search_active_config", { config: next });
    active.refetch();
  };

  const toggleMember = (id, on) => {
    const current = config.provider_ids || [];
    const next = on ? current.concat([id]) : current.filter((x) => x !== id);
    save({ mode: "aggregated", provider_ids: next });
  };

  return (
    <section className="catalog-defaults" data-testid="active-web-search-config">
      <h3>Active web search provider</h3>
      <label className="field">
        <span>Mode</span>
        <select
          data-testid="active-web-search-mode"
          value={mode}
          onChange={(e) =>
            save(
              e.target.value === "aggregated"
                ? { mode: "aggregated", provider_ids: config.provider_id ? [config.provider_id] : [] }
                : { mode: "single", provider_id: (config.provider_ids || [])[0] || "" },
            )
          }
        >
          <option value="single">single</option>
          <option value="aggregated">aggregated</option>
        </select>
      </label>
      {mode === "single" ? (
        <label className="field">
          <span>Provider</span>
          <select
            value={config.provider_id || ""}
            onChange={(e) => e.target.value && save({ mode: "single", provider_id: e.target.value })}
          >
            <option value="">(none)</option>
            {items.map((row) => (
              <option key={row.id} value={row.id}>{row.id}</option>
            ))}
          </select>
        </label>
      ) : (
        <div className="field" data-testid="active-web-search-members">
          <span>Providers (results are merged across all of them)</span>
          {items.map((row) => (
            <label key={row.id} className="check">
              <input
                type="checkbox"
                checked={(config.provider_ids || []).indexOf(row.id) >= 0}
                onChange={(e) => toggleMember(row.id, e.target.checked)}
              />
              <span className="mono">{row.id}</span>
            </label>
          ))}
        </div>
      )}
    </section>
  );
}

function ProviderCatalog({ initialClass, initialInstanceId, onNavigate }) {
  const [classKey, setClassKey] = React.useState(
    initialClass || PROVIDER_CLASSES[0].key,
  );
  const [instanceId, setInstanceId] = React.useState(initialInstanceId || null);
  const listRefetchRef = React.useRef(null);
  const [reloadKey, setReloadKey] = React.useState(0);
  const [draft, setDraft] = React.useState({});

  const klass = PROVIDER_CLASSES.find((c) => c.key === classKey) || PROVIDER_CLASSES[0];
  const cls = klass;

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
          klass={cls}
          selectedId={instanceId}
          onSelect={selectInstance}
          onRegisterRefetch={(fn) => { listRefetchRef.current = fn; }}
        />
        <div className="col" style={{ flex: 1, minWidth: 0 }}>
        {instanceId && cls.form === "crud" && (
          <PC_RowActions
            klass={cls}
            rowId={instanceId}
            onChanged={(what) => {
              if (listRefetchRef.current) listRefetchRef.current();
              if (what === "delete") setInstanceId(null);
            }}
          />
        )}
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
      <div className="col" style={{ flex: 1, minWidth: 0, gap: 16 }}>
        <h2 className="text-lg">{klass.label}</h2>
        {klass.key === "stt" || klass.key === "tts" ? <PC_ActiveSpeechPanel /> : null}
        {klass.key === "web_search" ? <PC_ActiveWebSearchPanel /> : null}
        {body}
        {klass.profiles && instanceId ? (
          <PC_ProfilesPanel providerId={instanceId} />
        ) : null}
      </div>
    </div>
  );
}

window.PROVIDER_CLASSES = PROVIDER_CLASSES;
window.ProviderCatalog = ProviderCatalog;
