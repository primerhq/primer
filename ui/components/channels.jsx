/* global React, Icon, StatusPill, Btn, Modal, Banner, CardList, Card, Fab, relativeTime */

// Top-level scope is shared with the babel-standalone IIFE; prefix all
// consts with CH_ to avoid clashes with other components (notably the
// `PROVIDER_FIELDS` const which collided with providers.jsx during the
// Task 3 wiring — see plan §"Task 3").

// NOTE: do NOT destructure window.primerApi at module top level. The docs
// embeds install a fixture-backed stub (DocsMakeStubApi) AFTER this module is
// evaluated, so a top-level capture would freeze the real (network) apiFetch.
// Each component reads from window.primerApi inside its own render instead.

const CH_LIST_PROVIDERS = "channels:providers";
const CH_LIST_CHANNELS = "channels:channels";
const CH_DETAIL_PREFIX = "channel-provider-detail:";

const CH_PROVIDER_FIELDS = {
  slack: [
    { key: "app_token", label: "App token", placeholder: "xapp-…", secret: true, required: true, hint: "App-level token (Basic Information panel)" },
    { key: "bot_token", label: "Bot token", placeholder: "xoxb-…", secret: true, required: true, hint: "Bot OAuth token" },
    { key: "signing_secret", label: "Signing secret", secret: true, hint: "Unused in Socket Mode; only for HTTP delivery" },
  ],
  telegram: [
    { key: "bot_token", label: "Bot token", placeholder: "123456:ABCDEF…", secret: true, required: true, hint: "From @BotFather. `<id>:<hash>`, min 20 chars" },
    { key: "poll_timeout_seconds", label: "Poll timeout (s)", type: "number", default: 25, hint: "Long-poll timeout per getUpdates. 25 balances latency vs churn" },
  ],
  discord: [
    { key: "bot_token", label: "Bot token", secret: true, required: true, hint: "From Developer Portal. Don't include the `Bot ` prefix" },
    { key: "enable_dms", label: "Enable DMs", type: "checkbox", default: true, hint: "Request the dm_messages intent" },
  ],
};

const CH_PROVIDER_COLORS = { slack: "var(--violet)", telegram: "var(--blue)", discord: "var(--accent)" };

function CH_toastErr(pushToast, fallbackTitle) {
  return (err) => {
    if (typeof pushToast !== "function") return;
    pushToast({
      kind: "error",
      title: err?.title || fallbackTitle,
      detail: err?.detail || err?.message,
      requestId: err?.requestId,
    });
  };
}

function CH_relAge(iso) {
  if (!iso) return "—";
  const t = typeof iso === "number" ? iso : new Date(iso).getTime();
  if (!Number.isFinite(t)) return "—";
  const sec = Math.max(0, (Date.now() - t) / 1000);
  return relativeTime(sec);
}

function ProviderBadge({ kind }) {
  const tone = CH_PROVIDER_COLORS[kind] || "var(--text-3)";
  return (
    <span className="pill" style={{ background: "var(--bg-2)", color: tone, border: "1px solid var(--border)" }}>
      <span className="dot" style={{ background: tone }}></span>
      <span className="mono text-sm">{kind || "unknown"}</span>
    </span>
  );
}

// ============== Providers list ==============

function ChannelProvidersPage({ onOpen, pushToast }) {
  const { apiFetch, useResource, useViewport } = window.primerApi;
  const { isMobile } = useViewport();
  const [showNew, setShowNew] = React.useState(false);
  const [filter, setFilter] = React.useState("");
  const [platform, setPlatform] = React.useState("");

  const providers = useResource(
    CH_LIST_PROVIDERS,
    (signal) => apiFetch("GET", "/channel_providers?limit=200", null, { signal }),
    {},
  );
  const channels = useResource(
    CH_LIST_CHANNELS,
    (signal) => apiFetch("GET", "/channels?limit=200", null, { signal }),
    {},
  );

  const items = providers.data?.items ?? [];
  const channelItems = channels.data?.items ?? [];
  const filtered = items.filter((p) => {
    if (platform && p.provider !== platform) return false;
    if (filter && !(p.id || "").toLowerCase().includes(filter.toLowerCase())) return false;
    return true;
  });

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter providers…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        <div className="sep-v" />
        <select className="select" value={platform} onChange={(e) => setPlatform(e.target.value)}>
          <option value="">all platforms</option>
          <option value="slack">slack</option>
          <option value="telegram">telegram</option>
          <option value="discord">discord</option>
        </select>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowNew(true)}>New provider</Btn>
        </div>
      </div>

      {providers.error && !providers.data && (
        <Banner
          kind="error"
          title={providers.error.title || "Couldn't load channel providers"}
          detail={providers.error.detail || providers.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={providers.refetch}>Retry</Btn>}
        />
      )}

      {isMobile ? (
        <CardList
          items={filtered}
          empty={items.length === 0 ? "No channel providers yet." : "No providers match."}
          renderCard={(p) => {
            const chs = channelItems.filter((c) => c.provider_id === p.id);
            return (
              <Card
                title={p.id}
                subtitle={<ProviderBadge kind={p.provider} />}
                meta={`${chs.length} channel${chs.length === 1 ? "" : "s"} · ${CH_relAge(p.created_at)}`}
                onClick={() => onOpen(p.id)}
              />
            );
          }}
        />
      ) : (
      <div className="tbl-wrap">
        <table className="tbl">
          <thead><tr><th>ID</th><th>Platform</th><th style={{ textAlign: "right" }}>Channels</th><th>Created</th><th></th></tr></thead>
          <tbody>
            {filtered.length === 0 && !providers.loading && (
              <tr><td colSpan={5}>
                <div className="empty" style={{ padding: 20 }}>
                  <div className="head">No channel providers</div>
                  <div className="sub">Create a Slack / Telegram / Discord provider to start routing messages.</div>
                </div>
              </td></tr>
            )}
            {filtered.map((p) => {
              const chs = channelItems.filter((c) => c.provider_id === p.id);
              return (
                <tr key={p.id} onClick={() => onOpen(p.id)} style={{ cursor: "pointer" }}>
                  <td className="mono">{p.id}</td>
                  <td><ProviderBadge kind={p.provider} /></td>
                  <td className="mono num tabular">{chs.length}</td>
                  <td className="mono muted">{CH_relAge(p.created_at)}</td>
                  <td style={{ textAlign: "right", paddingRight: 12 }}><Icon name="chevron-right" size={12} className="muted" /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      )}

      {isMobile && (
        <Fab icon="plus" label="New channel provider" onClick={() => setShowNew(true)} />
      )}

      {showNew && (
        <NewChannelProviderModal
          onClose={() => setShowNew(false)}
          onCreated={(row) => {
            setShowNew(false);
            if (pushToast) pushToast({ kind: "success", title: "Channel provider created", detail: row.id });
            onOpen(row.id);
          }}
          pushToast={pushToast}
        />
      )}
    </div>
  );
}

function NewChannelProviderModal({ onClose, onCreated, pushToast, existing }) {
  const { apiFetch, useMutation } = window.primerApi;
  const isEdit = !!existing;
  const [id, setId] = React.useState(existing?.id || "");
  const [provider, setProvider] = React.useState(existing?.provider || "slack");
  const [values, setValues] = React.useState(() => {
    if (isEdit) {
      // Blank out password fields on edit-mode prefill so the redacted
      // "**********" placeholder doesn't get PUT back over the real secret.
      const seed = { ...(existing?.config || {}) };
      const fs = CH_PROVIDER_FIELDS[existing?.provider] || [];
      for (const f of fs) {
        if (f.secret && /^\*{6,}$/.test(String(seed[f.key] || ""))) {
          seed[f.key] = "";
        }
      }
      return seed;
    }
    const seeded = {};
    for (const f of CH_PROVIDER_FIELDS.slack) {
      if (f.default !== undefined) seeded[f.key] = f.default;
    }
    return seeded;
  });
  const [fieldErrors, setFieldErrors] = React.useState({});

  // Re-seed defaults whenever the provider type changes — skip the
  // FIRST render in edit mode so we keep the prefill we just set up.
  const _isFirstRender = React.useRef(true);
  React.useEffect(() => {
    if (isEdit && _isFirstRender.current) {
      _isFirstRender.current = false;
      return;
    }
    _isFirstRender.current = false;
    const seeded = {};
    for (const f of CH_PROVIDER_FIELDS[provider] || []) {
      if (f.default !== undefined) seeded[f.key] = f.default;
    }
    setValues(seeded);
    setFieldErrors({});
  }, [provider]);  // eslint-disable-line react-hooks/exhaustive-deps

  const fields = CH_PROVIDER_FIELDS[provider] || [];

  const cleanConfig = () => {
    const out = {};
    for (const f of fields) {
      const v = values[f.key];
      if (v === undefined || v === null || v === "") continue;
      if (f.type === "number") {
        const n = Number(v);
        if (Number.isFinite(n)) out[f.key] = n;
      } else {
        out[f.key] = v;
      }
    }
    return out;
  };

  const create = useMutation(
    (body) => isEdit
      ? apiFetch("PUT", "/channel_providers/" + encodeURIComponent(existing.id), body)
      : apiFetch("POST", "/channel_providers", body),
    {
      invalidates: isEdit
        ? [CH_LIST_PROVIDERS, "channel-provider-detail:" + (existing?.id || "")]
        : [CH_LIST_PROVIDERS],
      onSuccess: (row) => { onCreated(row); },
      onError: (err) => {
        if (err.status === 422 && Array.isArray(err.fieldErrors)) {
          const map = {};
          for (const fe of err.fieldErrors) map[(fe.loc || []).join(".")] = fe.msg;
          setFieldErrors(map);
        } else {
          if (pushToast) pushToast({
            kind: "error",
            title: err.title || (isEdit ? "Save failed" : "Create failed"),
            detail: err.detail || err.message,
            requestId: err.requestId,
          });
        }
      },
    },
  );

  const submit = () => {
    setFieldErrors({});
    const body = {
      ...(isEdit ? { id: existing.id } : (id ? { id } : {})),
      provider,
      config: cleanConfig(),
    };
    create.mutate(body);
  };

  const canSubmit = !create.loading && fields.every((f) => {
    if (!f.required) return true;
    const v = values[f.key];
    return v !== undefined && v !== null && v !== "";
  });

  return (
    <Modal
      title={isEdit ? `Edit channel provider · ${existing.id}` : "New channel provider"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon={isEdit ? "check" : "plus"} onClick={submit} disabled={!canSubmit}>
            {create.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save changes" : "Create provider")}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">id {isEdit
          ? <span className="hint">locked — id cannot change after create</span>
          : <span className="hint">auto-generated if blank</span>}
        </label>
        <input
          className="input mono"
          placeholder="auto-generated"
          value={id}
          onChange={(e) => setId(e.target.value)}
          disabled={isEdit}
          style={{ width: "100%" }}
        />
        {fieldErrors["body.id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">platform {isEdit && <span className="hint">locked — recreate to change platform</span>}</label>
        <select
          className="select mono"
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          disabled={isEdit}
          style={{ width: "100%" }}
        >
          <option value="slack">Slack</option>
          <option value="telegram">Telegram</option>
          <option value="discord">Discord</option>
        </select>
        {fieldErrors["body.provider"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.provider"]}</div>}
      </div>
      <div style={{ borderTop: "1px dashed var(--border)", paddingTop: 12, marginTop: 4 }}>
        <div className="mono" style={{ fontSize: 10.5, fontWeight: 600, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>{provider} config</div>
        {fields.map((f) => {
          // The server's per-config field_validators emit loc=("body",
          // "{field}") because ChannelProvider._coerce_config_type pre-
          // instantiates the inner config (e.g. SlackChannelProviderConfig)
          // inside its model_validator(mode="before"), so the "config"
          // path segment is lost from the ValidationError's loc tuple.
          // Match the server emission rather than the request-body shape.
          const errKey = `body.${f.key}`;
          const err = fieldErrors[errKey];
          return (
            <div className="field" key={f.key}>
              <label className="field-label">
                {f.label}
                {f.required && <span className="hint" style={{ color: "var(--amber)" }}>required</span>}
              </label>
              {f.type === "checkbox" ? (
                <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
                  <input
                    type="checkbox"
                    checked={values[f.key] !== undefined ? !!values[f.key] : !!f.default}
                    onChange={(e) => setValues({ ...values, [f.key]: e.target.checked })}
                  />
                  <span>{f.hint}</span>
                </label>
              ) : f.type === "number" ? (
                <input
                  className="input mono"
                  type="number"
                  value={values[f.key] ?? ""}
                  onChange={(e) => setValues({ ...values, [f.key]: e.target.value === "" ? "" : Number(e.target.value) })}
                  style={{ width: "100%" }}
                />
              ) : (
                <input
                  className="input mono"
                  type={f.secret ? "password" : "text"}
                  placeholder={f.placeholder}
                  value={values[f.key] ?? ""}
                  onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                  style={{ width: "100%" }}
                />
              )}
              {f.hint && f.type !== "checkbox" && <div className="field-help">{f.hint}</div>}
              {err && <div className="field-help" style={{ color: "var(--red)" }}>{err}</div>}
            </div>
          );
        })}
      </div>
    </Modal>
  );
}

// ============== Provider detail ==============

function ChannelProviderDetail({ providerId, pushToast }) {
  const { apiFetch, useResource, useMutation, useRouter } = window.primerApi;
  const { navigate } = useRouter();
  const detailKey = CH_DETAIL_PREFIX + providerId;
  const [showDelete, setShowDelete] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState(null);
  const [editing, setEditing] = React.useState(false);

  const detail = useResource(
    detailKey,
    (signal) => apiFetch("GET", `/channel_providers/${encodeURIComponent(providerId)}`, null, { signal }),
    { deps: [providerId] },
  );

  // Channels under this provider — fetched from the channels list and
  // filtered client-side (no per-provider sub-route on the server).
  const channels = useResource(
    CH_LIST_CHANNELS,
    (signal) => apiFetch("GET", "/channels?limit=200", null, { signal }),
    {},
  );
  const chs = (channels.data?.items ?? []).filter((c) => c.provider_id === providerId);

  const del = useMutation(
    () => apiFetch("DELETE", `/channel_providers/${encodeURIComponent(providerId)}`),
    {
      invalidates: [CH_LIST_PROVIDERS, CH_LIST_CHANNELS],
      onSuccess: () => {
        if (pushToast) pushToast({ kind: "warning", title: "Provider deleted", detail: providerId });
        setShowDelete(false);
        navigate("/channels/providers");
      },
      onError: (err) => {
        if (err?.status === 409) {
          setDeleteError(err.detail || "Cannot delete — channels still reference this provider.");
        } else {
          setShowDelete(false);
          CH_toastErr(pushToast, "Delete failed")(err);
        }
      },
    },
  );

  if (detail.loading && !detail.data) {
    return (
      <div className="panel"><div className="panel-body" style={{ padding: 18 }}>
        <span className="muted text-sm">Loading provider…</span>
      </div></div>
    );
  }

  if (detail.error && !detail.data) {
    return (
      <Banner
        kind="error"
        title={detail.error.title || `Couldn't load ${providerId}`}
        detail={detail.error.detail || detail.error.message}
        actions={<Btn size="sm" icon="refresh" onClick={detail.refetch}>Retry</Btn>}
      />
    );
  }

  const p = detail.data;
  if (!p) return null;

  const configFields = CH_PROVIDER_FIELDS[p.provider] || [];

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel">
        <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 18px" }}>
          <ProviderBadge kind={p.provider} />
          <div style={{ flex: 1 }}>
            <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{p.id}</div>
            <div className="muted text-sm mono">created {CH_relAge(p.created_at)}</div>
          </div>
          <Btn
            size="sm"
            kind="ghost"
            icon="zap"
            disabled
            title="Probe endpoint not yet implemented (backend follow-up)"
          >
            Probe
          </Btn>
          <Btn size="sm" kind="secondary" icon="edit" onClick={() => setEditing(true)}>Edit</Btn>
          <Btn size="sm" kind="danger" icon="trash" onClick={() => { setDeleteError(null); setShowDelete(true); }}>Delete</Btn>
        </div>
      </div>

      <div className="panel">
        <div className="panel-h"><Icon name="settings" size={13} className="muted" /><span>Config</span></div>
        <div className="panel-body">
          <dl className="kv" style={{ gridTemplateColumns: "180px 1fr" }}>
            {configFields.map((f) => {
              const v = p.config?.[f.key];
              return (
                <React.Fragment key={f.key}>
                  <dt>{f.key}</dt>
                  <dd>
                    {f.secret
                      ? (v
                          ? <span className="mono">{String(v)} <span className="muted">(masked)</span></span>
                          : <span className="muted">(not set)</span>)
                      : <span className="mono">{v === undefined || v === null ? "—" : String(v)}</span>}
                  </dd>
                </React.Fragment>
              );
            })}
          </dl>
        </div>
      </div>

      <div className="panel">
        <div className="panel-h"><Icon name="bell" size={13} className="muted" /><span>Channels</span><span className="sub">· {chs.length}</span></div>
        <div className="panel-body" style={{ padding: 0 }}>
          {chs.length === 0 ? (
            <div className="empty" style={{ padding: 20 }}>
              <div className="head">No channels</div>
              <div className="sub">Create channels under this provider to start routing.</div>
            </div>
          ) : (
            <table className="tbl">
              <tbody>
                {chs.map((c) => (
                  <tr key={c.id}>
                    <td className="mono">{c.id}</td>
                    <td className="mono muted">{c.external_id}</td>
                    <td>{c.label}</td>
                  </tr>
                ))}
              </tbody>
            </table>
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
                disabled={chs.length > 0 || del.loading}
                onClick={() => del.mutate()}
              >
                {del.loading ? "Deleting…" : "Delete provider"}
              </Btn>
            </>
          }
        >
          <div className="muted text-sm" style={{ marginBottom: 10 }}>
            Deleting a channel provider is irreversible.
            The server returns <span className="mono">409 Conflict</span> if any
            channel still references it; remove those first.
          </div>
          {chs.length > 0 && (
            <div className="field-help" style={{ color: "var(--amber)" }}>
              {chs.length} channel{chs.length === 1 ? "" : "s"} currently reference this provider.
            </div>
          )}
          {deleteError && (
            <div style={{ marginTop: 10 }}>
              <Banner kind="error" title="409 Conflict" detail={deleteError} />
            </div>
          )}
        </Modal>
      )}
      {editing && (
        <NewChannelProviderModal
          existing={p}
          pushToast={pushToast}
          onClose={() => setEditing(false)}
          onCreated={() => {
            setEditing(false);
            if (typeof pushToast === "function") {
              pushToast({ kind: "info", title: "Provider updated", detail: p.id });
            }
            detail.refetch();
          }}
        />
      )}
    </div>
  );
}

// ============== Channels list ==============

function ChannelsPage({ onNavigate, pushToast }) {
  const { apiFetch, useResource, useMutation, useViewport } = window.primerApi;
  const { isMobile } = useViewport();
  const [showNew, setShowNew] = React.useState(false);
  const [editing, setEditing] = React.useState(null);
  const [filter, setFilter] = React.useState("");
  const [providerFilter, setProviderFilter] = React.useState("");

  const providers = useResource(
    CH_LIST_PROVIDERS,
    (signal) => apiFetch("GET", "/channel_providers?limit=200", null, { signal }),
    {},
  );
  const channels = useResource(
    CH_LIST_CHANNELS,
    (signal) => apiFetch("GET", "/channels?limit=200", null, { signal }),
    {},
  );

  const providerItems = providers.data?.items ?? [];
  const channelItems = channels.data?.items ?? [];
  const filtered = channelItems.filter((c) => {
    if (providerFilter && c.provider_id !== providerFilter) return false;
    if (!filter) return true;
    const q = filter.toLowerCase();
    return (
      (c.id || "").toLowerCase().includes(q)
      || (c.external_id || "").toLowerCase().includes(q)
      || (c.label || "").toLowerCase().includes(q)
    );
  });

  const del = useMutation(
    (cid) => apiFetch("DELETE", `/channels/${encodeURIComponent(cid)}`),
    {
      invalidates: [CH_LIST_CHANNELS],
      onSuccess: () => {
        if (pushToast) pushToast({ kind: "warning", title: "Channel deleted" });
      },
      onError: (err) => {
        if (err?.status === 409) {
          if (pushToast) pushToast({
            kind: "error",
            title: "409 Conflict",
            detail: err.detail || "Cannot delete — workspace associations still reference this channel.",
            requestId: err.requestId,
          });
        } else {
          CH_toastErr(pushToast, "Delete failed")(err);
        }
      },
    },
  );

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter channels…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        <div className="sep-v" />
        <select className="select" value={providerFilter} onChange={(e) => setProviderFilter(e.target.value)}>
          <option value="">all providers</option>
          {providerItems.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowNew(true)} disabled={providerItems.length === 0}>
            New channel
          </Btn>
        </div>
      </div>

      {channels.error && !channels.data && (
        <Banner
          kind="error"
          title={channels.error.title || "Couldn't load channels"}
          detail={channels.error.detail || channels.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={channels.refetch}>Retry</Btn>}
        />
      )}

      {isMobile ? (
        <CardList
          items={filtered}
          empty={
            channelItems.length === 0
              ? (providerItems.length === 0 ? "Create a channel provider first." : "No channels yet.")
              : "No channels match."
          }
          renderCard={(c) => {
            const p = providerItems.find((x) => x.id === c.provider_id);
            return (
              <Card
                title={c.id}
                subtitle={c.provider_id}
                pill={p ? <ProviderBadge kind={p.provider} /> : null}
                meta={`${c.external_id || ""}${c.label ? " · " + c.label : ""}`}
                onClick={() => setEditing(c)}
              />
            );
          }}
        />
      ) : (
      <div className="tbl-wrap">
        <table className="tbl">
          <thead><tr><th>ID</th><th>Provider</th><th>External ID</th><th>Label</th><th></th></tr></thead>
          <tbody>
            {filtered.length === 0 && !channels.loading && (
              <tr><td colSpan={5}>
                <div className="empty" style={{ padding: 20 }}>
                  <div className="head">No channels</div>
                  <div className="sub">
                    {providerItems.length === 0
                      ? "Create a channel provider first."
                      : "Bind a Slack/Telegram/Discord conversation to a provider."}
                  </div>
                </div>
              </td></tr>
            )}
            {filtered.map((c) => {
              const p = providerItems.find((x) => x.id === c.provider_id);
              return (
                <tr key={c.id}>
                  <td className="mono">{c.id}</td>
                  <td>
                    <a
                      className="mono"
                      style={{ color: "var(--accent)", cursor: "pointer", marginRight: 6 }}
                      onClick={() => onNavigate("channel-provider-detail", c.provider_id)}
                    >
                      {c.provider_id}
                    </a>
                    {p && <ProviderBadge kind={p.provider} />}
                  </td>
                  <td className="mono muted">{c.external_id}</td>
                  <td>{c.label}</td>
                  <td style={{ textAlign: "right", paddingRight: 12, whiteSpace: "nowrap" }}>
                    <button
                      className="icon-btn"
                      style={{ width: 22, height: 22, marginRight: 4 }}
                      title="Edit channel"
                      onClick={() => setEditing(c)}
                    >
                      <Icon name="edit" size={10} />
                    </button>
                    <button
                      className="icon-btn"
                      style={{ width: 22, height: 22 }}
                      title="Delete channel"
                      onClick={() => del.mutate(c.id)}
                      disabled={del.loading}
                    >
                      <Icon name="trash" size={10} />
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      )}

      {isMobile && providerItems.length > 0 && (
        <Fab icon="plus" label="New channel" onClick={() => setShowNew(true)} />
      )}

      {showNew && (
        <NewChannelModal
          providers={providerItems}
          onClose={() => setShowNew(false)}
          onCreated={() => {
            setShowNew(false);
            if (pushToast) pushToast({ kind: "success", title: "Channel created" });
          }}
          pushToast={pushToast}
        />
      )}
      {editing && (
        <NewChannelModal
          providers={providerItems}
          existing={editing}
          onClose={() => setEditing(null)}
          onCreated={() => {
            const editedId = editing.id;
            setEditing(null);
            if (pushToast) pushToast({ kind: "info", title: "Channel updated", detail: editedId });
          }}
          pushToast={pushToast}
        />
      )}
    </div>
  );
}

// CH_Toggle — a switch-style on/off control (used for the chat-config toggles
// that progressively reveal the rest of the chat controls).
function CH_Toggle({ checked, onChange, label, help, disabled, testid }) {
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

// CH_AllowedAgentsPicker — a searchable, paginated multi-select for the
// allowed-agents list. Selection is a set of agent ids; a search box filters by
// id and the filtered results are paged so a large fleet stays manageable.
function CH_AllowedAgentsPicker({
  agentItems, selected, onChange, search, onSearch, page, onPage, perPage,
}) {
  const selectedSet = new Set(selected);
  const needle = search.trim().toLowerCase();
  const filtered = needle
    ? agentItems.filter((a) => a.id.toLowerCase().includes(needle))
    : agentItems;
  const pageCount = Math.max(1, Math.ceil(filtered.length / perPage));
  const safePage = Math.min(page, pageCount - 1);
  const start = safePage * perPage;
  const pageItems = filtered.slice(start, start + perPage);

  const toggle = (id) => {
    const next = new Set(selectedSet);
    if (next.has(id)) next.delete(id); else next.add(id);
    onChange(Array.from(next));
  };

  if (agentItems.length === 0) {
    return <div className="muted text-sm">No agents registered.</div>;
  }

  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 6, overflow: "hidden" }}>
      <div style={{ position: "relative", borderBottom: "1px solid var(--border)" }}>
        <Icon name="search" size={13} className="icon" style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)", opacity: 0.6 }} />
        <input
          className="input"
          placeholder="Search agents…"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          data-testid="channel-allowed-agents-search"
          style={{ width: "100%", border: "none", borderRadius: 0, paddingLeft: 28 }}
        />
      </div>
      <div style={{ maxHeight: 200, overflowY: "auto" }}>
        {pageItems.length === 0 ? (
          <div className="muted text-sm" style={{ padding: "10px 12px" }}>No matching agents.</div>
        ) : pageItems.map((a) => (
          <label
            key={a.id}
            style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 12px", cursor: "pointer", fontSize: 12.5 }}
          >
            <input
              type="checkbox"
              checked={selectedSet.has(a.id)}
              onChange={() => toggle(a.id)}
            />
            <span className="mono">{a.id}</span>
          </label>
        ))}
      </div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "6px 10px", borderTop: "1px solid var(--border)", background: "var(--bg-2)" }}>
        <span className="muted text-sm">
          {selectedSet.size} selected{filtered.length !== agentItems.length ? ` · ${filtered.length} match` : ""}
        </span>
        {pageCount > 1 && (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <Btn kind="ghost" disabled={safePage <= 0} onClick={() => onPage(safePage - 1)}>‹</Btn>
            <span className="muted text-sm">{safePage + 1}/{pageCount}</span>
            <Btn kind="ghost" disabled={safePage >= pageCount - 1} onClick={() => onPage(safePage + 1)}>›</Btn>
          </span>
        )}
      </div>
    </div>
  );
}

function NewChannelModal({ providers, onClose, onCreated, pushToast, existing }) {
  const { apiFetch, useResource, useMutation } = window.primerApi;
  const isEdit = !!existing;
  const [id, setId] = React.useState(existing?.id || "");
  const [providerId, setProviderId] = React.useState(
    existing?.provider_id || providers[0]?.id || ""
  );
  const [externalId, setExternalId] = React.useState(existing?.external_id || "");
  const [label, setLabel] = React.useState(existing?.label || "");
  const [fieldErrors, setFieldErrors] = React.useState({});

  // Chats config state
  const [chatsEnabled, setChatsEnabled] = React.useState(
    existing?.config?.chats?.enabled ?? false
  );
  const [chatsDefaultAgent, setChatsDefaultAgent] = React.useState(
    existing?.config?.chats?.default_agent || ""
  );
  const [chatsAllowAgentSwitch, setChatsAllowAgentSwitch] = React.useState(
    existing?.config?.chats?.allow_agent_switch ?? false
  );
  const [chatsAllowedAgents, setChatsAllowedAgents] = React.useState(
    existing?.config?.chats?.allowed_agents || []
  );
  const [chatsRelayMode, setChatsRelayMode] = React.useState(
    existing?.config?.chats?.relay_mode || "final"
  );
  // Search + pagination for the allowed-agents picker.
  const [agentSearch, setAgentSearch] = React.useState("");
  const [agentPage, setAgentPage] = React.useState(0);
  const AGENTS_PER_PAGE = 8;

  // Fetch agents for default_agent / allowed_agents pickers
  const agentsRes = useResource(
    "channels:agents",
    (signal) => apiFetch("GET", "/agents?limit=200", null, { signal }),
    {}
  );
  const agentItems = agentsRes.data?.items ?? [];

  const create = useMutation(
    (body) => isEdit
      ? apiFetch("PUT", "/channels/" + encodeURIComponent(existing.id), body)
      : apiFetch("POST", "/channels", body),
    {
      invalidates: [CH_LIST_CHANNELS],
      onSuccess: () => onCreated(),
      onError: (err) => {
        if (err.status === 422 && Array.isArray(err.fieldErrors)) {
          const map = {};
          for (const fe of err.fieldErrors) map[(fe.loc || []).join(".")] = fe.msg;
          setFieldErrors(map);
        } else {
          if (pushToast) pushToast({
            kind: "error",
            title: err.title || (isEdit ? "Save failed" : "Create failed"),
            detail: err.detail || err.message,
            requestId: err.requestId,
          });
        }
      },
    },
  );

  const submit = () => {
    setFieldErrors({});
    // Derive provider type from the selected ChannelProvider so the server
    // gets the provider string even on creates (avoids a second lookup).
    const selectedProvider = providers.find((p) => p.id === providerId);
    const body = {
      ...(isEdit ? { id: existing.id } : (id ? { id } : {})),
      provider_id: providerId,
      ...(selectedProvider ? { provider: selectedProvider.provider } : {}),
      external_id: externalId,
      ...(label ? { label } : {}),
      config: {
        chats: {
          enabled: chatsEnabled,
          default_agent: chatsDefaultAgent || null,
          allow_agent_switch: chatsAllowAgentSwitch,
          allowed_agents: chatsAllowAgentSwitch ? chatsAllowedAgents : [],
          relay_mode: chatsRelayMode,
        },
      },
    };
    create.mutate(body);
  };

  const canSubmit = !!providerId && !!externalId && !create.loading;

  return (
    <Modal
      title={isEdit ? `Edit channel · ${existing.id}` : "New channel"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon={isEdit ? "check" : "plus"} onClick={submit} disabled={!canSubmit}>
            {create.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save changes" : "Create channel")}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">id {isEdit
          ? <span className="hint">locked</span>
          : <span className="hint">auto</span>}
        </label>
        <input
          className="input mono"
          placeholder="auto-generated"
          value={id}
          onChange={(e) => setId(e.target.value)}
          disabled={isEdit}
          style={{ width: "100%" }}
        />
        {fieldErrors["body.id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">provider {isEdit && <span className="hint">locked — recreate to change provider</span>}</label>
        <select
          className="select mono"
          value={providerId}
          onChange={(e) => setProviderId(e.target.value)}
          disabled={isEdit}
          style={{ width: "100%" }}
        >
          {providers.map((p) => <option key={p.id} value={p.id}>{p.id} ({p.provider})</option>)}
        </select>
        {fieldErrors["body.provider_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.provider_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">external id</label>
        <input
          className="input mono"
          placeholder="C0123ABC456 / chat-id / snowflake"
          value={externalId}
          onChange={(e) => setExternalId(e.target.value)}
          style={{ width: "100%" }}
        />
        <div className="field-help">Slack: channel ID · Telegram: chat ID · Discord: snowflake</div>
        {fieldErrors["body.external_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.external_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">label <span className="hint">optional · ≤200 chars</span></label>
        <input
          className="input"
          placeholder="#ops-alerts"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          style={{ width: "100%" }}
        />
        {fieldErrors["body.label"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.label"]}</div>}
      </div>

      <div style={{ borderTop: "1px dashed var(--border)", paddingTop: 12, marginTop: 4 }}>
        <div className="mono" style={{ fontSize: 10.5, fontWeight: 600, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>Chats config</div>
        <div className="field">
          <CH_Toggle
            checked={chatsEnabled}
            onChange={setChatsEnabled}
            label="Chats enabled"
            help="allow inbound chat messages on this channel"
            testid="channel-chats-enabled"
          />
        </div>
        {chatsEnabled && (
          <>
            <div className="field">
              <label className="field-label">default agent <span className="hint">optional</span></label>
              <select
                className="select mono"
                value={chatsDefaultAgent}
                onChange={(e) => setChatsDefaultAgent(e.target.value)}
                style={{ width: "100%" }}
              >
                <option value="">(none)</option>
                {agentItems.map((a) => (
                  <option key={a.id} value={a.id}>{a.id}</option>
                ))}
              </select>
              <div className="field-help">Agent used for new chats when no agent is specified</div>
            </div>
            <div className="field">
              <CH_Toggle
                checked={chatsAllowAgentSwitch}
                onChange={setChatsAllowAgentSwitch}
                label="Allow agent switching"
                help="let users change a chat's agent with /agent"
                testid="channel-allow-agent-switch"
              />
            </div>
            {chatsAllowAgentSwitch && (
              <div className="field">
                <label className="field-label">allowed agents <span className="hint">optional · restricts /agent</span></label>
                <CH_AllowedAgentsPicker
                  agentItems={agentItems}
                  selected={chatsAllowedAgents}
                  onChange={setChatsAllowedAgents}
                  search={agentSearch}
                  onSearch={(v) => { setAgentSearch(v); setAgentPage(0); }}
                  page={agentPage}
                  onPage={setAgentPage}
                  perPage={AGENTS_PER_PAGE}
                />
                <div className="field-help">Restrict which agents users can switch to. None selected = all agents allowed.</div>
              </div>
            )}
            <div className="field">
              <label className="field-label">relay mode</label>
              <select
                className="select mono"
                value={chatsRelayMode}
                onChange={(e) => setChatsRelayMode(e.target.value)}
                style={{ width: "100%" }}
              >
                <option value="final">final — only relay the last agent message</option>
                <option value="all">all — relay every agent message</option>
              </select>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

window.ChannelProvidersPage = ChannelProvidersPage;
window.ChannelProviderDetail = ChannelProviderDetail;
window.ChannelsPage = ChannelsPage;
// Legacy mock export kept as empty stub — app.jsx reads
// `window.CHANNELS_DATA` for the sidebar count.
window.CHANNELS_DATA = [];
