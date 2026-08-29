/* global React, Icon, Btn, StatusPill, Banner, Modal, relativeTime */
// ============================================================================
// Unified provider catalog (S4 P2; platform wave P1a reconciliation).
//
// One surface for every provider class: a family CHIPS row, a card grid of
// instances for the selected family, and a "Register provider" dropdown that
// opens a create modal. Classes that already have a purpose-built panel
// (vector stores, workspaces, channels) host that component rather than
// reimplementing it.
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
  { key: "ssp", label: "Semantic Search", plural: "ssp", form: "panel",
    panel: () => window.SSPListPage,
    // Without a detail the catalog selected a store and then showed the
    // list again, so a vector store's own page was unreachable. It takes
    // its id as `sspId`, hence detailProp.
    detail: () => window.SSPDetail, detailProp: "sspId" },
  { key: "stt", label: "Speech-to-Text", plural: "stt_providers", form: "crud" },
  { key: "tts", label: "Text-to-Speech", plural: "tts_providers", form: "crud" },
  { key: "web_search", label: "Web Search", plural: "web_search_providers", form: "crud" },
  { key: "web_fetch", label: "Web Fetch", plural: "web_fetch_providers", form: "crud" },
  { key: "artifact_storage", label: "Artifact Storage", plural: "artifact_storage_providers", form: "crud" },
  { key: "workspace", label: "Workspaces", plural: "workspace_providers", form: "panel",
    panel: () => window.WorkspaceProvidersPage,
    detail: () => window.WorkspaceProviderDetail },
  { key: "channel", label: "Channels", plural: "channel_providers", form: "panel",
    panel: () => window.ChannelProvidersPage,
    // A panel class can still have a detail view; without one the
    // catalog can select an instance and then show the list again.
    detail: () => window.ChannelProviderDetail },
];

// One glyph per provider class, same 12x12 stroke language as the
// platform nav (ui-ux pass 2026-08-26: the rail was bare text links).
const PC_CLASS_ICONS = {
  llm: "M2 4.5 7 2l5 2.5-5 2.5Z M2 7l5 2.5L12 7 M2 9.5 7 12l5-2.5",
  embedding: "M2 10h2.2V6.5H2Z M5.4 10h2.2V3H5.4Z M8.8 10H11V5H8.8Z M2 12h9",
  cross_encoder: "M2 3.5h6.5 M5.5 7h6.5 M2 10.5h6.5 M10 2l2 1.5-2 1.5 M4 5.5 2 7l2 1.5 M10 9l2 1.5-2 1.5",
  ssp: "M2 3.5a5 1.8 0 0 0 10 0 5 1.8 0 0 0-10 0Z M2 3.5v7a5 1.8 0 0 0 10 0v-7 M2 7a5 1.8 0 0 0 10 0",
  stt: "M5 1.5h4v5.5a2 2 0 0 1-4 0Z M2.8 6a4.2 4.2 0 0 0 8.4 0 M7 10.2V13",
  tts: "M2 5h2.3L8 2.2v9.6L4.3 9H2Z M9.8 4.6a3.4 3.4 0 0 1 0 4.8",
  web_search: "M5.8 1.5a4.3 4.3 0 1 0 0 8.6 4.3 4.3 0 0 0 0-8.6Z M9 9l3.5 3.5",
  web_fetch: "M7 1.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Z M1.5 7h11 M7 1.5c2.2 2.6 2.2 8.4 0 11",
  artifact: "M2 4.5 7 2l5 2.5v5L7 12 2 9.5Z M2 4.5 7 7l5-2.5 M7 7v5",
  workspaces: "M1.5 3.5h4l1 1.5h6V11h-11Z",
  channel: "M2 2.5h10v7H6L3 12V9.5H2Z",
};

// Platform wave P1a item 1: the family rail becomes a CHIPS row (reuses the
// app's own .chip-group/.chip pattern, already proven elsewhere - e.g. the
// filter bars - rather than inventing a new control). Same classes/order the
// rail always used; only the container markup changed.
function PC_FamilyChips({ classes, selected, onSelect }) {
  return (
    <div className="chip-group pc-chips" role="tablist"
      aria-label="Provider family" data-testid="provider-chips">
      {classes.map((cls) => (
        <span
          key={cls.key}
          data-testid={`provider-chip-${cls.key}`}
          className={`chip touch-target${cls.key === selected ? " active" : ""}`}
          role="tab"
          aria-selected={cls.key === selected}
          tabIndex={0}
          onClick={() => onSelect(cls.key)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onSelect(cls.key);
            }
          }}
        >
          <svg width="12" height="12" viewBox="0 0 14 14" fill="none"
            stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round"
            aria-hidden="true">
            <path d={PC_CLASS_ICONS[cls.key] || PC_CLASS_ICONS.llm} />
          </svg>
          <span>{cls.label}</span>
        </span>
      ))}
    </div>
  );
}

// Platform wave P1a items 2+3: the primary CTA becomes a "Register provider"
// dropdown naming the KIND up front ("kind decides the form") instead of a
// bare "New" button that dumps the operator into a form defaulted to
// whichever kind happened to be first. Annotations are read from the SAME
// /_types response the form already fetches - never a hardcoded kind list,
// so a kind this deployment does not actually serve (e.g. a class with only
// one entry) never shows a phantom row, and a kind the backend marks
// non-discoverable never falsely claims "probes models live".
function PC_RegisterDropdown({ klass, onPick }) {
  const { useResource, apiFetch } = window.primerApi;
  const [open, setOpen] = React.useState(false);
  const types = useResource(
    `provider-register-types:${klass.plural}`,
    (signal) => apiFetch("GET", `/${klass.plural}/_types`, null, { signal }),
    { pollMs: null },
  );
  const typeMap = types.data || {};
  const kinds = Object.keys(typeMap);

  return (
    <div className="pc-register" data-testid="provider-register">
      <Btn kind="primary" data-testid="provider-register-toggle"
        onClick={() => setOpen((v) => !v)}>
        Register provider <Icon name={open ? "chevron-up" : "chevron-down"} size={11} />
      </Btn>
      {open ? (
        <div className="pc-register-panel" role="menu"
          data-testid="provider-register-panel">
          <div className="pc-register-head">Kind — decides the form</div>
          {kinds.length === 0 ? (
            <div className="pc-register-empty muted text-sm">
              {types.loading ? "Loading…" : "No kinds available."}
            </div>
          ) : kinds.map((k) => {
            const meta = typeMap[k] || {};
            // gate on the served data only: a kind's own discoverable flag
            // decides "probes models live"; the aggregated variant (no flat
            // field set - the console mounts its own editor) decides
            // "gated / special". Anything else served with neither signal
            // gets no annotation rather than a guessed one.
            const annotation = meta.discoverable
              ? "probes models live"
              : meta.variant === "aggregated" ? "gated / special" : null;
            return (
              <button type="button" key={k} role="menuitem"
                className="pc-register-row"
                data-testid={`provider-register-kind-${k}`}
                onClick={() => { setOpen(false); onPick(k); }}>
                <span>{meta.label || k}</span>
                {annotation ? (
                  <span className="pc-register-annotation muted">{annotation}</span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

// Platform wave P4: reachable/unreachable badge derived from
// last_probe_at/last_probe_ok (LLMProvider only - primer/model/
// providers/llm.py:341-369; other provider classes don't carry these
// fields, so `row.last_probe_at` reads undefined for them and this
// renders nothing, the same as a genuinely virgin LLM row). A row that
// has never been probed (last_probe_at == null) shows NO badge at all
// rather than a stale/misleading one - the field's own docstring
// mandates this, not a UI guess.
function PC_ReachabilityBadge({ lastProbeAt, lastProbeOk }) {
  if (lastProbeAt == null) return null;
  const ok = !!lastProbeOk;
  const color = ok ? "var(--green)" : "var(--red)";
  return (
    <span className="pill" data-testid="provider-card-reachability"
      style={{ color, borderColor: "var(--border)", background: "var(--bg-2)" }}>
      <span className="dot" style={{ background: color }}></span>
      {ok ? "reachable" : "unreachable"}
    </span>
  );
}

// The three actions a card's footer offers: Open (address the instance),
// Invalidate (only where the endpoint exists - the model-family registry
// caches an adapter per row id, api/registries/provider_registry.py; the
// sibling registries drop their entry on the CRUD hook and need no button),
// Delete (confirm-gated). Reference anatomy is Open+Delete only; Invalidate
// is real, tested, currently-shipped functionality for three classes, kept
// as a third small action rather than silently dropped to match a mockup
// that likely never modeled cache invalidation at all.
function PC_InstanceCard({ klass, row, onOpen, onChanged }) {
  const { apiFetch, useResource } = window.primerApi;
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [err, setErr] = React.useState("");
  const [busy, setBusy] = React.useState("");
  const encoded = encodeURIComponent(row.id);

  // Wave P1a item 3 / CI fix: "models N probed live". Two real gates,
  // not the old (broken) `discoverable ? key : null` pattern - useResource
  // has no null-key skip, so that always fired the fetch anyway, 404ing
  // on embedding/cross_encoder (GET .../discovered_models is an
  // llm_providers-only route - providers.py:617, no per-id GET exists
  // for the other classes) and 400ing on unreachable llm rows.
  //
  // canProbe is true only when BOTH hold: the route exists for this
  // class (llm_providers only), and this row already has a confirmed
  // successful probe (last_probe_ok - virgin/never-probed rows are
  // excluded too, same virgin rule the reachability badge uses). No
  // silent auto-probing on page load; a fresh/unreachable row simply
  // shows no model count until something else establishes reachability.
  const canProbe = klass.plural === "llm_providers" && row.last_probe_ok === true;
  const discovered = useResource(
    `pc:discovered:${row.id}`,
    (signal) => canProbe
      ? apiFetch("GET", `/${klass.plural}/${encoded}/discovered_models`, null, { signal })
      : Promise.resolve(null),
    { pollMs: null },
  );
  const modelCount = canProbe && discovered.data && Array.isArray(discovered.data.models)
    ? discovered.data.models.length
    : null;

  const run = async (what, method, path) => {
    setErr("");
    setBusy(what);
    try {
      await apiFetch(method, path);
      setConfirmDelete(false);
      const toast = window.primerApi && window.primerApi.toastPush;
      if (what === "invalidate" && typeof toast === "function") {
        toast({ kind: "success", title: "Cache dropped", detail: row.id });
      }
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

  const virgin = row.last_probe_at == null;
  const unreachable = !virgin && !row.last_probe_ok;

  return (
    <div className="pc-card" data-testid={`provider-card-${row.id}`}>
      <div className="pc-card-head">
        <span className="pc-card-title mono">{row.id}</span>
        <span style={{ flex: 1 }} />
        <PC_ReachabilityBadge lastProbeAt={row.last_probe_at} lastProbeOk={row.last_probe_ok} />
      </div>
      <div className="pc-card-subtitle">
        {[row.provider, row.config && row.config.url].filter(Boolean).join(" · ")}
      </div>
      <div className="pc-card-facts">
        {modelCount != null ? (
          <div className="pc-card-fact">
            <span className="muted">models</span>
            <span>{modelCount} probed live</span>
          </div>
        ) : null}
        {!virgin ? (
          <div className="pc-card-fact">
            <span className="muted">last probed</span>
            <span>{relativeTime((Date.now() - new Date(row.last_probe_at).getTime()) / 1000)}</span>
          </div>
        ) : null}
        {unreachable && row.last_error ? (
          <div className="pc-card-fact" data-testid={`provider-card-probe-error-${row.id}`}>
            <span className="muted">error</span>
            <span className="mono text-sm" style={{ color: "var(--red)" }}>{row.last_error}</span>
          </div>
        ) : null}
      </div>
      <div className="pc-card-footer">
        <Btn kind="primary" size="sm" data-testid={`provider-card-open-${row.id}`}
          onClick={() => onOpen(row.id)}>
          Open
        </Btn>
        <span style={{ flex: 1 }} />
        {klass.invalidate ? (
          <Btn kind="ghost" size="sm" disabled={busy !== ""}
            data-testid={`provider-card-invalidate-${row.id}`}
            onClick={() => run("invalidate", "POST", `/${klass.plural}/${encoded}/invalidate`)}>
            Invalidate
          </Btn>
        ) : null}
        {confirmDelete ? (
          <>
            <Btn kind="danger" size="sm" disabled={busy !== ""}
              data-testid={`provider-card-delete-confirm-${row.id}`}
              onClick={() => run("delete", "DELETE", `/${klass.plural}/${encoded}`)}>
              Confirm
            </Btn>
            <Btn kind="ghost" size="sm" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Btn>
          </>
        ) : (
          <Btn kind="ghost" size="sm" data-testid={`provider-card-delete-${row.id}`}
            onClick={() => { setErr(""); setConfirmDelete(true); }}>
            Delete
          </Btn>
        )}
      </div>
      {err ? (
        <div className="field-help" data-testid={`provider-card-error-${row.id}`}>{err}</div>
      ) : null}
    </div>
  );
}

function PC_InstanceGrid({ klass, onSelect, onRegisterRefetch }) {
  const { usePagedList } = window.primerApi;
  const list = usePagedList({
    key: `catalog:${klass.plural}`,
    path: `/${klass.plural}`,
    pageSize: 25,
    pollMs: null,
    resetKey: { plural: klass.plural },
  });

  // Hand the refetch up so a card action refreshes this list in place. A
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

  return (
    <div data-testid={`provider-instances-${klass.key}`} style={{ minWidth: 0, flex: "1 1 0" }}>
      <div className="pc-card-grid">
        {items.map((row) => (
          <PC_InstanceCard
            key={row.id}
            klass={klass}
            row={row}
            onOpen={onSelect}
            onChanged={(what) => {
              list.refetch();
              if (what === "delete" && onSelect) onSelect(null);
            }}
          />
        ))}
      </div>
      <Pager pager={list} label="providers" />
    </div>
  );
}


// ---------------------------------------------------------------------------
// Model profiles: LLM instances only
// ---------------------------------------------------------------------------

// Platform wave P4: profile cards wired in via MP_ProfileCard/
// MP_ProfilesGrid (model-profiles.jsx) instead of the bare <ul> this
// used to render - reuses the exact same card anatomy platform wave
// P1b already built (reasoning chip, bound-by/provider-down badges),
// rather than a second hand-rolled row renderer. This is deliberately
// still the panel embedded under a provider's own page, not a
// standalone page - tests/ui/test_model_profiles_page.py hard-bans a
// a standalone "Model Profiles" page component existing anywhere, since a profile is
// LLM-only by design and belongs under its provider.
//
// Also fixes a live bug found while wiring this: MP_ProfileModal was
// being mounted with a `providerId` prop it does not accept (it wants
// `open`/`providers`/`existing`/`prefill`) and no `open` prop at all,
// so `if (!open) return null;` inside it fired unconditionally - "New
// profile" rendered the modal tree but it always resolved to nothing.
function PC_ProfilesPanel({ providerId }) {
  const { useResource, apiFetch } = window.primerApi;
  const [open, setOpen] = React.useState(false);
  const [editing, setEditing] = React.useState(null);
  const models = useResource(
    `pc:models:${providerId}`,
    (signal) => apiFetch(
      "GET", `/llm_providers/${encodeURIComponent(providerId)}/models`, null, { signal },
    ),
    { pollMs: null },
  );
  const discovered_models = models.data?.models ?? [];
  const rows = useResource(
    `catalog:profile-rows:${providerId}`,
    (signal) => apiFetch("GET", "/model_profiles?limit=200", null, { signal }),
    { deps: [providerId] },
  );
  // Needed both for MP_ProfileModal's own Provider|Model picker (it
  // takes the full row list, same as the other working call site,
  // NV_ProfileModalHost in console/nv-platform.jsx) and for the
  // provider-down join below - model_profiles has no such join
  // itself (confirmed: _enrich_with_usage only tallies agent_count/
  // graph_node_count, it never touches LLMProvider storage), so the
  // frontend cross-references this provider's own row.
  const providers = useResource(
    "pc:profiles-providers",
    (signal) => apiFetch("GET", "/llm_providers?limit=200", null, { signal }),
    { pollMs: null },
  );
  const providerRows = (providers.data && providers.data.items) || [];
  const thisProvider = providerRows.find((p) => p.id === providerId);
  // Same virgin-is-not-down rule PC_ReachabilityBadge uses: a
  // never-probed provider is not "down", it is simply unprobed.
  const providerDown = !!(thisProvider && thisProvider.last_probe_at != null
    && !thisProvider.last_probe_ok);

  const mine = ((rows.data && rows.data.items) || []).filter(
    (r) => r.provider_id === providerId,
  );

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
      <window.MP_ProfilesGrid
        profiles={mine}
        providerDown={providerDown}
        onOpen={(row) => setEditing(row)}
        onDeleted={() => rows.refetch()}
      />
      <Btn icon="plus" kind="ghost" onClick={() => setOpen(true)}>New profile</Btn>
      {(open || editing) && window.MP_ProfileModal ? (
        <window.MP_ProfileModal
          open
          existing={editing}
          providers={providerRows}
          prefill={editing ? null : { provider_id: providerId }}
          onClose={() => { setOpen(false); setEditing(null); }}
          onSaved={() => rows.refetch()}
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
        // The voice picker is the one control on this panel a caller
        // needs by name; the panel around it already had a handle.
        data-testid="active-speech-voice"
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
  // The addressed instance wins. This was seeded once and never looked at
  // again, so a page inside the catalog that navigates on its own -- the
  // vector-store list does, to /ssp/<id> -- updated the url and the crumb
  // while the catalog went on showing the list, because its own state had
  // not heard about it. The id slot is what says which instance is open.
  React.useEffect(() => {
    setInstanceId(initialInstanceId || null);
  }, [initialInstanceId]);
  React.useEffect(() => {
    if (initialClass) setClassKey(initialClass);
  }, [initialClass]);
  const listRefetchRef = React.useRef(null);
  const [reloadKey, setReloadKey] = React.useState(0);
  const [draft, setDraft] = React.useState({});
  // Platform wave P1a items 2/3/7: creation moved from an always-mounted
  // inline form to a modal, opened by the register dropdown naming the
  // kind up front. null = closed.
  const [createOpen, setCreateOpen] = React.useState(false);

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
    // Refresh the list beside the form. setReloadKey bumps a counter
    // nothing depends on, so a created provider never appeared until the
    // operator navigated away and back: the row action beside it already
    // used this refetch, the create simply never called it.
    if (listRefetchRef.current) listRefetchRef.current();
    setReloadKey((k) => k + 1);
    if (method === "POST" && body.id) {
      setCreateOpen(false);
      setDraft({});
      // Select what was just made, which is where the operator is
      // already looking.
      selectInstance(body.id);
      const toast = window.primerApi && window.primerApi.toastPush;
      if (typeof toast === "function") {
        toast({ kind: "success", title: "Provider created", detail: body.id });
      }
    }
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
  // Their own panel renders its own create affordance, so the register
  // dropdown below is a crud-class-only control.
  let body;
  if (cls.form === "panel") {
    const Detail = instanceId && cls.detail ? cls.detail() : null;
    const Panel = cls.panel();
    // Detail components predate the catalog and name their id after the
    // thing they show: sspId, providerId. A class says which, rather
    // than every one of them being renamed to suit the host.
    const detailProps = {
      pushToast: window.primerApi && window.primerApi.toastPush,
    };
    detailProps[cls.detailProp || "providerId"] = instanceId;
    body = Detail ? (
      <Detail {...detailProps} />
    ) : Panel ? (
      // These panels take the same two props their pages always took.
      // Mounted bare, onOpen was undefined, so creating a provider threw
      // on the call meant to open it and clicking a row did nothing at
      // all; pushToast being undefined swallowed every confirmation too.
      <Panel
        onOpen={selectInstance}
        pushToast={window.primerApi && window.primerApi.toastPush}
      />
    ) : (
      <Banner kind="info" title={`${cls.label} panel unavailable`}>
        That panel component is not loaded in this bundle.
      </Banner>
    );
  } else {
    body = (
      <PC_InstanceGrid
        klass={cls}
        onSelect={selectInstance}
        onRegisterRefetch={(fn) => { listRefetchRef.current = fn; }}
      />
    );
  }

  return (
    <div className="col pc-catalog" style={{ gap: 16 }}>
      <div className="row" style={{ alignItems: "center", gap: 16, flexWrap: "wrap" }}>
        <h2 className="text-lg" style={{ margin: 0 }}>Providers</h2>
        <span style={{ flex: 1 }} />
        {cls.form === "crud" ? (
          <PC_RegisterDropdown
            klass={cls}
            onPick={(kind) => {
              setDraft({ provider: kind });
              setCreateOpen(true);
            }}
          />
        ) : null}
      </div>
      <PC_FamilyChips
        classes={PROVIDER_CLASSES}
        selected={cls.key}
        onSelect={selectClass}
      />
      <div className="col" style={{ gap: 16 }}>
        {klass.key === "stt" || klass.key === "tts" ? <PC_ActiveSpeechPanel /> : null}
        {klass.key === "web_search" ? <PC_ActiveWebSearchPanel /> : null}
        <div data-testid={`provider-body-${klass.key}`}>{body}</div>
        {klass.profiles && instanceId ? (
          <PC_ProfilesPanel providerId={instanceId} />
        ) : null}
      </div>
      {createOpen ? (
        <Modal
          title={`New ${cls.label} provider`}
          onClose={() => { setCreateOpen(false); setDraft({}); }}
        >
          <div className="pc-modal-chip mono text-sm muted"
            data-testid="provider-modal-schema-chip">
            schema-driven from /providers/_types
          </div>
          <window.PC_ProviderForm
            plural={cls.plural}
            typesPath={`/${cls.plural}/_types`}
            value={draft}
            onChange={setDraft}
            onSubmit={save}
            onCancel={() => { setCreateOpen(false); setDraft({}); }}
          />
        </Modal>
      ) : null}
    </div>
  );
}

window.PROVIDER_CLASSES = PROVIDER_CLASSES;
window.ProviderCatalog = ProviderCatalog;
