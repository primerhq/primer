/* global React, Icon, Btn, Modal, Banner, relativeTime */

const CHANNEL_PROVIDERS = [
  { id: "cp-slack-prod", provider: "slack", config: { app_token: "xapp-•••••", bot_token: "xoxb-•••••", signing_secret: null }, created_at_ago: 3600 * 24 * 5 },
  { id: "cp-telegram-ops", provider: "telegram", config: { bot_token: "123456:•••••", poll_timeout_seconds: 25 }, created_at_ago: 3600 * 24 * 2 },
  { id: "cp-discord-comm", provider: "discord", config: { bot_token: "•••••", enable_dms: true }, created_at_ago: 3600 * 12 },
];

const CHANNELS = [
  { id: "ch-ops-alerts", provider_id: "cp-slack-prod", external_id: "C0123ABC456", label: "#ops-alerts" },
  { id: "ch-eng-room", provider_id: "cp-slack-prod", external_id: "C0456DEF789", label: "#eng-room" },
  { id: "ch-dev-test", provider_id: "cp-slack-prod", external_id: "D0789GHI012", label: "DM: @dev-test" },
  { id: "ch-tg-ops", provider_id: "cp-telegram-ops", external_id: "-1001234567890", label: "Ops chat" },
  { id: "ch-dc-general", provider_id: "cp-discord-comm", external_id: "1234567890123456", label: "#general" },
];

const ASSOCIATIONS = [
  { id: "wca-1", workspace_id: "ws-3f8a9bc1d4e2", channel_id: "ch-ops-alerts", enabled: true, forward_ask_user: true, forward_tool_approval: true },
  { id: "wca-2", workspace_id: "ws-7c2d4e9a8b15", channel_id: "ch-eng-room", enabled: true, forward_ask_user: true, forward_tool_approval: false },
  { id: "wca-3", workspace_id: "ws-1a5e7d3f9c80", channel_id: "ch-tg-ops", enabled: false, forward_ask_user: true, forward_tool_approval: true },
];

const PROVIDER_FIELDS = {
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

const PROVIDER_COLORS = { slack: "var(--violet)", telegram: "var(--blue)", discord: "var(--accent)" };

function ProviderBadge({ kind }) {
  return (
    <span className="pill" style={{ background: "var(--bg-2)", color: PROVIDER_COLORS[kind], border: "1px solid var(--border)" }}>
      <span className="dot" style={{ background: PROVIDER_COLORS[kind] }}></span>
      <span className="mono text-sm">{kind}</span>
    </span>
  );
}

// ============== Providers list ==============

function ChannelProvidersPage({ onOpen, pushToast }) {
  const [showNew, setShowNew] = React.useState(false);
  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter providers…" />
        </div>
        <div className="sep-v" />
        <select className="select"><option>all platforms</option><option>slack</option><option>telegram</option><option>discord</option></select>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowNew(true)}>New provider</Btn>
        </div>
      </div>
      <div className="tbl-wrap">
        <table className="tbl">
          <thead><tr><th>ID</th><th>Platform</th><th style={{ textAlign: "right" }}>Channels</th><th>Created</th><th></th></tr></thead>
          <tbody>
            {CHANNEL_PROVIDERS.map((p) => {
              const chs = CHANNELS.filter((c) => c.provider_id === p.id);
              return (
                <tr key={p.id} onClick={() => onOpen(p.id)}>
                  <td className="mono">{p.id}</td>
                  <td><ProviderBadge kind={p.provider} /></td>
                  <td className="mono num tabular">{chs.length}</td>
                  <td className="mono muted">{relativeTime(p.created_at_ago)}</td>
                  <td style={{ textAlign: "right", paddingRight: 12 }}><Icon name="chevron-right" size={12} className="muted" /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {showNew && <NewChannelProviderModal onClose={() => setShowNew(false)} onCreate={() => { setShowNew(false); pushToast({ kind: "success", title: "Provider created", detail: "POST /v1/channel_providers → 201" }); }} />}
    </div>
  );
}

function NewChannelProviderModal({ onClose, onCreate }) {
  const [provider, setProvider] = React.useState("slack");
  const [values, setValues] = React.useState({});
  const fields = PROVIDER_FIELDS[provider];

  return (
    <Modal
      title="New channel provider"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={onCreate}>Create provider</Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">id <span className="hint">auto-generated if blank</span></label>
        <input className="input mono" placeholder="auto-generated" style={{ width: "100%" }} />
      </div>
      <div className="field">
        <label className="field-label">platform</label>
        <select className="select mono" value={provider} onChange={(e) => { setProvider(e.target.value); setValues({}); }} style={{ width: "100%" }}>
          <option value="slack">Slack</option>
          <option value="telegram">Telegram</option>
          <option value="discord">Discord</option>
        </select>
      </div>
      <div style={{ borderTop: "1px dashed var(--border)", paddingTop: 12, marginTop: 4 }}>
        <div className="mono" style={{ fontSize: 10.5, fontWeight: 600, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>{provider} config</div>
        {fields.map((f) => (
          <div className="field" key={f.key}>
            <label className="field-label">
              {f.label}
              {f.required && <span className="hint" style={{ color: "var(--amber)" }}>required</span>}
            </label>
            {f.type === "checkbox" ? (
              <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
                <input type="checkbox" defaultChecked={f.default} onChange={(e) => setValues({ ...values, [f.key]: e.target.checked })} />
                <span>{f.hint}</span>
              </label>
            ) : f.type === "number" ? (
              <input className="input mono" type="number" defaultValue={f.default} onChange={(e) => setValues({ ...values, [f.key]: +e.target.value })} style={{ width: "100%" }} />
            ) : (
              <input className="input mono" type={f.secret ? "password" : "text"} placeholder={f.placeholder} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} style={{ width: "100%" }} />
            )}
            {f.hint && f.type !== "checkbox" && <div className="field-help">{f.hint}</div>}
          </div>
        ))}
      </div>
    </Modal>
  );
}

// ============== Provider detail ==============

function ChannelProviderDetail({ providerId, onBack, pushToast }) {
  const p = CHANNEL_PROVIDERS.find((x) => x.id === providerId);
  if (!p) return null;
  const chs = CHANNELS.filter((c) => c.provider_id === providerId);
  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel">
        <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 18px" }}>
          <ProviderBadge kind={p.provider} />
          <div style={{ flex: 1 }}>
            <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{p.id}</div>
            <div className="muted text-sm mono">created {relativeTime(p.created_at_ago)}</div>
          </div>
          <Btn size="sm" kind="ghost" icon="zap" disabled title="Probe endpoint not yet implemented (backend follow-up)">Probe</Btn>
          <Btn size="sm" kind="danger" icon="trash">Delete</Btn>
        </div>
      </div>
      <div className="panel">
        <div className="panel-h"><Icon name="settings" size={13} className="muted" /><span>Config</span></div>
        <div className="panel-body">
          <dl className="kv" style={{ gridTemplateColumns: "180px 1fr" }}>
            {PROVIDER_FIELDS[p.provider].map((f) => (
              <React.Fragment key={f.key}>
                <dt>{f.key}</dt>
                <dd>{f.secret ? (p.config[f.key] ? <span className="mono">{p.config[f.key]} (masked)</span> : <span className="muted">(not set)</span>) : <span className="mono">{String(p.config[f.key] ?? "—")}</span>}</dd>
              </React.Fragment>
            ))}
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
    </div>
  );
}

// ============== Channels list ==============

function ChannelsPage({ onNavigate, pushToast }) {
  const [showNew, setShowNew] = React.useState(false);
  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter channels…" />
        </div>
        <div className="sep-v" />
        <select className="select"><option>all providers</option>{CHANNEL_PROVIDERS.map((p) => <option key={p.id}>{p.id}</option>)}</select>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowNew(true)}>New channel</Btn>
        </div>
      </div>
      <div className="tbl-wrap">
        <table className="tbl">
          <thead><tr><th>ID</th><th>Provider</th><th>External ID</th><th>Label</th></tr></thead>
          <tbody>
            {CHANNELS.map((c) => {
              const p = CHANNEL_PROVIDERS.find((x) => x.id === c.provider_id);
              return (
                <tr key={c.id}>
                  <td className="mono">{c.id}</td>
                  <td>
                    <a className="mono" style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => onNavigate("channel-provider-detail", c.provider_id)}>{c.provider_id}</a>
                    <ProviderBadge kind={p?.provider || "slack"} />
                  </td>
                  <td className="mono muted">{c.external_id}</td>
                  <td>{c.label}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {showNew && (
        <Modal
          title="New channel"
          onClose={() => setShowNew(false)}
          footer={<><Btn kind="ghost" onClick={() => setShowNew(false)}>Cancel</Btn><Btn kind="primary" icon="plus" onClick={() => { setShowNew(false); pushToast({ kind: "success", title: "Channel created", detail: "POST /v1/channels → 201" }); }}>Create channel</Btn></>}
        >
          <div className="field"><label className="field-label">id <span className="hint">auto</span></label><input className="input mono" placeholder="auto-generated" style={{ width: "100%" }} /></div>
          <div className="field"><label className="field-label">provider</label><select className="select mono" style={{ width: "100%" }}>{CHANNEL_PROVIDERS.map((p) => <option key={p.id}>{p.id} ({p.provider})</option>)}</select></div>
          <div className="field"><label className="field-label">external id</label><input className="input mono" placeholder="C0123ABC456 / chat-id / snowflake" style={{ width: "100%" }} /><div className="field-help">Slack: channel ID · Telegram: chat ID · Discord: snowflake</div></div>
          <div className="field"><label className="field-label">label <span className="hint">optional · ≤200 chars</span></label><input className="input" placeholder="#ops-alerts" style={{ width: "100%" }} /></div>
        </Modal>
      )}
    </div>
  );
}

// ============== Associations ==============

function AssociationsPage({ onNavigate, pushToast }) {
  const [rows, setRows] = React.useState(ASSOCIATIONS);
  const [showNew, setShowNew] = React.useState(false);

  const toggle = (id, field) => {
    setRows((arr) => arr.map((r) => r.id === id ? { ...r, [field]: !r[field] } : r));
    pushToast({ kind: "info", title: "Association updated", detail: `PUT /v1/workspace_channel_associations/${id}` });
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter associations…" />
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowNew(true)}>New association</Btn>
        </div>
      </div>
      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>Workspace</th>
              <th>Channel</th>
              <th>Enabled</th>
              <th>Forward ask_user</th>
              <th>Forward tool_approval</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => {
              const ch = CHANNELS.find((c) => c.id === a.channel_id);
              return (
                <tr key={a.id} style={{ opacity: a.enabled ? 1 : 0.5 }}>
                  <td className="mono">
                    <a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => onNavigate("workspace-detail", a.workspace_id)}>{a.workspace_id}</a>
                  </td>
                  <td className="mono">{a.channel_id} {ch && <span className="muted text-sm">· {ch.label}</span>}</td>
                  <td><Toggle on={a.enabled} onChange={() => toggle(a.id, "enabled")} /></td>
                  <td><Toggle on={a.forward_ask_user} onChange={() => toggle(a.id, "forward_ask_user")} /></td>
                  <td><Toggle on={a.forward_tool_approval} onChange={() => toggle(a.id, "forward_tool_approval")} /></td>
                  <td style={{ textAlign: "right", paddingRight: 12 }}>
                    <button className="icon-btn" style={{ width: 22, height: 22 }} title="Remove"><Icon name="x" size={10} /></button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {showNew && (
        <Modal
          title="New workspace channel association"
          onClose={() => setShowNew(false)}
          footer={<><Btn kind="ghost" onClick={() => setShowNew(false)}>Cancel</Btn><Btn kind="primary" icon="plus" onClick={() => { setShowNew(false); pushToast({ kind: "success", title: "Association created" }); }}>Create</Btn></>}
        >
          <div className="field"><label className="field-label">workspace</label><select className="select mono" style={{ width: "100%" }}>{window.MOCK.WORKSPACES.map((w) => <option key={w}>{w}</option>)}</select></div>
          <div className="field"><label className="field-label">channel</label><select className="select mono" style={{ width: "100%" }}>{CHANNELS.map((c) => <option key={c.id}>{c.id} · {c.label}</option>)}</select></div>
          <div className="field" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
              <input type="checkbox" defaultChecked /><span>Enabled <span className="muted">— adapter fan-outs route here</span></span>
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
              <input type="checkbox" defaultChecked /><span>Forward ask_user <span className="muted">— channel-mediated user prompts</span></span>
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
              <input type="checkbox" defaultChecked /><span>Forward tool_approval <span className="muted">— channel-mediated approvals</span></span>
            </label>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Toggle({ on, onChange }) {
  return (
    <button
      onClick={onChange}
      style={{
        width: 32, height: 18, borderRadius: 10,
        background: on ? "var(--accent)" : "var(--bg-2)",
        border: `1px solid ${on ? "var(--accent)" : "var(--border)"}`,
        cursor: "pointer", padding: 0, position: "relative", transition: "0.15s",
      }}
    >
      <span style={{
        position: "absolute", top: 1, left: on ? 15 : 1,
        width: 14, height: 14, borderRadius: "50%",
        background: on ? "var(--accent-fg)" : "var(--text-3)",
        transition: "0.15s",
      }} />
    </button>
  );
}

window.ChannelProvidersPage = ChannelProvidersPage;
window.ChannelProviderDetail = ChannelProviderDetail;
window.ChannelsPage = ChannelsPage;
window.AssociationsPage = AssociationsPage;
window.CHANNEL_ASSOCIATIONS = ASSOCIATIONS;
window.CHANNELS_DATA = CHANNELS;
