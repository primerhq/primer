/* global React, Icon, Btn, Modal, Banner, Card, CardList, Fab */

// Channel rule-editor page: a capability-aware event -> action binding
// editor. Lists channel triggers per provider/room and lets an operator
// create bindings (Subscriptions) that map a NormalizedEventType + an
// EventMatcher to an action (start_chat / chat_message / *_fresh_session)
// and a ReplyTarget. The event picker is capability-aware: it reads the
// per-provider capability taxonomy and warns about prerequisites (Discord
// MESSAGE CONTENT intent, Telegram privacy-mode-off, Slack scopes).
//
// Wiring note: there is no /channel_triggers surface in this branch.
// Channel triggers are ordinary triggers whose config.kind == "channel",
// so this page consumes the real REST surface:
//   GET  /v1/triggers?kind=channel                  (list channel triggers)
//   POST /v1/triggers                               (create a channel trigger)
//   GET  /v1/triggers/{id}/subscriptions            (list bindings)
//   POST /v1/triggers/{id}/subscriptions            (create a binding)
//   DELETE /v1/triggers/{id}/subscriptions/{sid}    (delete a binding)
// Provider capabilities are a static per-provider taxonomy (the same data
// the backend ProviderCapabilities normalizers declare); there is no
// capabilities HTTP endpoint, so nothing is fetched for it (no 404).
//
// Idiom mirrors channels.jsx: every const is prefixed CR_ so the shared
// babel-standalone IIFE scope stays clash-free, and window.primerApi is
// read inside each component (never destructured at module top) so the
// docs-embed stub install order is honoured.

const { apiFetch, useResource, useMutation, useViewport } = window.primerApi;

const CR_EVENT_TYPES = [
  "message.posted",
  "command.invoked",
  "component.acted",
  "reaction.added",
  "reaction.removed",
  "message.edited",
  "member.joined",
  "bot.installed",
  "bot.removed",
  "room.created",
];

// Static per-provider capability taxonomy. Mirrors the backend
// ProviderCapabilities each normalizer declares (primer/channel/*/
// normalizer.py): which normalized event types the provider can emit, and
// the operator prerequisites to surface. Keyed by the provider's `provider`
// type string. An unknown provider type falls back to "everything
// supported, no prereqs" so the picker still works.
const CR_PROVIDER_CAPS = {
  slack: {
    supported: ["message.posted", "command.invoked"],
    prerequisites: [
      "bot needs chat:write, channels:read, channels:history scopes",
      "subscribe to message.channels and app_mention event subscriptions",
    ],
  },
  telegram: {
    supported: ["message.posted", "command.invoked", "component.acted"],
    prerequisites: [
      "disable BotFather privacy mode (or make the bot a group admin) to receive group messages",
    ],
  },
  discord: {
    supported: ["message.posted", "command.invoked", "component.acted"],
    prerequisites: [
      "enable the MESSAGE CONTENT privileged intent in the Developer Portal",
    ],
  },
};

// Build the capability-annotated event-type list for a provider: each entry
// carries `supported` (false greys it out) and the provider-wide
// `prerequisites` (surfaced as warning banners under the picker).
function CR_capEventsFor(providerType) {
  const caps = CR_PROVIDER_CAPS[providerType];
  if (!caps) {
    return CR_EVENT_TYPES.map((t) => ({ type: t, supported: true, prerequisites: [] }));
  }
  const supp = new Set(caps.supported);
  return CR_EVENT_TYPES.map((t) => ({
    type: t,
    supported: supp.has(t),
    prerequisites: supp.has(t) ? caps.prerequisites : [],
  }));
}

const CR_SURFACES = ["dm", "channel", "thread"];

// Each action's config requires a discriminated `kind` plus its own fields.
// The form collects those extra fields per action so a valid
// SubscriptionConfig is posted (config: { action } alone is rejected by the
// SubscriptionConfig discriminated union).
const CR_ACTIONS = [
  { value: "start_chat", label: "start_chat", fields: [{ key: "agent_id", label: "Agent id" }] },
  { value: "chat_message", label: "chat_message", fields: [{ key: "chat_id", label: "Chat id" }] },
  {
    value: "agent_fresh_session",
    label: "agent_fresh_session",
    fields: [
      { key: "workspace_id", label: "Workspace id" },
      { key: "agent_id", label: "Agent id" },
    ],
  },
  {
    value: "graph_fresh_session",
    label: "graph_fresh_session",
    fields: [
      { key: "workspace_id", label: "Workspace id" },
      { key: "graph_id", label: "Graph id" },
    ],
  },
];

// ReplyTarget is a relative literal ("source_thread" etc.) sent verbatim, or
// "none" which means "omit reply_target". (The backend ReplyTarget union is
// a bare string literal, not a {kind} object.)
const CR_REPLY_TARGETS = [
  { value: "source_thread", label: "source_thread" },
  { value: "source_room", label: "source_room" },
  { value: "dm_sender", label: "dm_sender" },
  { value: "none", label: "none" },
];

function CR_toastErr(pushToast, fallbackTitle) {
  return (err) => {
    if (typeof pushToast !== "function") return;
    pushToast({ kind: "error", title: err?.title || fallbackTitle, detail: err?.detail || err?.message, requestId: err?.requestId });
  };
}

function CR_matcherSummary(m) {
  if (!m) return "any";
  const bits = [];
  if (m.surface && m.surface.length) bits.push("surface=" + m.surface.join("|"));
  if (m.command_name) bits.push("cmd=" + m.command_name);
  if (m.mentions_bot === true) bits.push("@bot");
  if (m.text_pattern) bits.push("text~/" + m.text_pattern + "/");
  if (m.sender_roles_any && m.sender_roles_any.length) bits.push("roles=" + m.sender_roles_any.join(","));
  return bits.length ? bits.join(" · ") : "any";
}

// reply_target is a string literal ("source_thread") or an explicit
// {channel_id, anchor} object, or absent (defaults to source thread).
function CR_replyLabel(rt) {
  if (rt == null) return "default";
  if (typeof rt === "string") return rt;
  if (rt.channel_id) return rt.channel_id + (rt.anchor ? "#" + rt.anchor : "");
  return "default";
}

function ChannelRulesPage({ pushToast }) {
  const { isMobile } = useViewport();
  const providers = useResource("cr-providers", (signal) => apiFetch("GET", "/channel_providers?limit=200", null, { signal }), {});
  // Channel triggers are ordinary triggers with config.kind == "channel".
  // The list endpoint filters by kind server-side; we keep the client-side
  // config.kind guard as a belt-and-braces check.
  const triggers = useResource("cr-triggers", (signal) => apiFetch("GET", "/triggers?kind=channel", null, { signal }), {});

  if (providers.error && !providers.data) {
    return <Banner kind="error" title={providers.error.title || "Couldn't load channel providers"} detail={providers.error.detail || providers.error.message} actions={<Btn size="sm" icon="refresh" onClick={providers.refetch}>Retry</Btn>} />;
  }
  if (providers.loading && !providers.data) {
    return <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading...</div>;
  }
  const provs = (providers.data && providers.data.items) || [];
  const trigs = ((triggers.data && triggers.data.items) || []).filter((t) => t.config && t.config.kind === "channel");
  // empty state
  if (provs.length === 0) {
    return <Banner kind="info" title="No channel providers yet" detail="Register a Slack, Telegram, or Discord provider under Channels first, then add rules here." />;
  }
  return (
    <div className="col" style={{ gap: 14 }}>
      {provs.map((p) => <CR_ProviderSection key={p.id} provider={p} triggers={trigs.filter((t) => t.config && t.config.provider_id === p.id)} pushToast={pushToast} onChanged={() => { triggers.refetch(); }} />)}
      {isMobile && <Fab icon="plus" label="New rule" onClick={() => {}} />}
    </div>
  );
}
window.ChannelRulesPage = ChannelRulesPage;

// Per-provider section: provider header + the rooms (channel triggers)
// it owns and their bindings, plus a "New rule" button that opens the
// capability-aware CR_RuleModal.
function CR_ProviderSection({ provider, triggers, pushToast, onChanged }) {
  const [showNew, setShowNew] = React.useState(false);
  const provider_id = provider.id;

  return (
    <div className="card" style={{ padding: 0 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center", padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          <Icon name="bell" size={14} className="muted" />
          <span className="mono" style={{ fontWeight: 600 }}>{provider_id}</span>
          <span className="pill"><span className="mono text-sm">{provider.provider || "unknown"}</span></span>
        </div>
        <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowNew(true)}>New rule</Btn>
      </div>

      <div className="col" style={{ gap: 0 }}>
        {triggers.length === 0 && (
          <div className="empty" style={{ padding: 18 }}>
            <div className="sub">No rules yet for this provider. Add one to route an event to an action.</div>
          </div>
        )}
        {triggers.map((t) => <CR_TriggerRows key={t.id} trigger={t} pushToast={pushToast} onChanged={onChanged} />)}
      </div>

      {showNew && (
        <CR_RuleModal
          provider={provider}
          triggers={triggers}
          onClose={() => setShowNew(false)}
          onChanged={onChanged}
          pushToast={pushToast}
        />
      )}
    </div>
  );
}

// Renders one channel trigger's room label + each of its bindings
// (Subscriptions) as a row with event_type, matcher summary, action,
// reply_target, and a delete button.
function CR_TriggerRows({ trigger, pushToast, onChanged }) {
  const subsKey = "cr-subs-" + trigger.id;
  const subs = useResource(subsKey, (signal) => apiFetch("GET", "/triggers/" + encodeURIComponent(trigger.id) + "/subscriptions", null, { signal }), {});
  const room = (trigger.config && trigger.config.channel_id) || "(all rooms)";
  const items = (subs.data && subs.data.items) || [];

  const del = useMutation(
    (subscription_id) => apiFetch("DELETE", "/triggers/" + encodeURIComponent(trigger.id) + "/subscriptions/" + encodeURIComponent(subscription_id)),
    { invalidates: [subsKey], onSuccess: () => { subs.refetch(); if (typeof onChanged === "function") onChanged(); if (typeof pushToast === "function") pushToast({ kind: "warning", title: "Rule deleted" }); }, onError: CR_toastErr(pushToast, "Delete rule failed") }
  );

  return (
    <div className="col" style={{ gap: 0, borderBottom: "1px solid var(--border)" }}>
      <div className="row" style={{ gap: 6, alignItems: "center", padding: "8px 14px" }}>
        <Icon name="command" size={12} className="muted" />
        <span className="mono text-sm muted">{room}</span>
      </div>
      {items.length === 0 ? (
        <div className="muted text-sm" style={{ padding: "0 14px 10px 28px" }}>no bindings</div>
      ) : items.map((s) => (
        <div key={s.id} className="row" style={{ gap: 10, alignItems: "center", padding: "6px 14px 8px 28px", flexWrap: "wrap" }}>
          <span className="pill"><span className="mono text-sm">{(s.event_matcher && s.event_matcher.event_type) || "?"}</span></span>
          <span className="muted text-sm">{CR_matcherSummary(s.event_matcher)}</span>
          <Icon name="chevron-right" size={12} className="muted" />
          <span className="mono text-sm">{(s.config && s.config.kind) || "?"}</span>
          <span className="muted text-sm mono">reply: {CR_replyLabel(s.reply_target)}</span>
          <div style={{ marginLeft: "auto" }}>
            <Btn size="sm" kind="ghost" icon="trash" onClick={() => del.mutate(s.id)} disabled={del.loading}>Delete</Btn>
          </div>
        </div>
      ))}
    </div>
  );
}

// The capability-aware rule editor. Renders the event picker (warning on
// unsupported / prerequisite-bearing types from the static per-provider
// taxonomy), the matcher predicate fields, the action select with its
// per-action config fields, and the ReplyTarget select. Creates the binding
// (creating the channel trigger first if the room has none yet).
function CR_RuleModal({ provider, triggers, onClose, onChanged, pushToast }) {
  const capEvents = CR_capEventsFor(provider.provider);

  const [channelId, setChannelId] = React.useState("");
  const [eventType, setEventType] = React.useState("message.posted");
  const [surface, setSurface] = React.useState([]);
  const [commandName, setCommandName] = React.useState("");
  const [mentionsBot, setMentionsBot] = React.useState(false);
  const [textPattern, setTextPattern] = React.useState("");
  const [senderRoles, setSenderRoles] = React.useState("");
  const [action, setAction] = React.useState("start_chat");
  const [actionFields, setActionFields] = React.useState({});
  const [replyTarget, setReplyTarget] = React.useState("source_thread");
  const [creating, setCreating] = React.useState(false);

  const selectedCap = capEvents.find((e) => e.type === eventType) || { type: eventType, supported: true, prerequisites: [] };
  const prereqs = selectedCap.prerequisites || [];
  const unsupported = selectedCap.supported === false;
  const actionDef = CR_ACTIONS.find((a) => a.value === action) || CR_ACTIONS[0];

  const setActionField = (key, value) => {
    setActionFields((cur) => ({ ...cur, [key]: value }));
  };

  // Resolve (or lazily create) the channel trigger for the chosen room,
  // then create the binding (subscription).
  const submit = async () => {
    setCreating(true);
    try {
      const wantChannel = channelId || null;
      let trig = triggers.find((t) => ((t.config && t.config.channel_id) || null) === wantChannel);
      if (!trig) {
        const cfg = { kind: "channel", provider_id: provider.id };
        if (channelId) cfg.channel_id = channelId;
        const roomLabel = channelId || "all";
        const slug = ("ch-" + provider.id + "-" + roomLabel).toLowerCase().replace(/[^a-z0-9-]+/g, "-").slice(0, 64);
        const name = "Channel rule " + provider.id + " / " + roomLabel;
        trig = await apiFetch("POST", "/triggers", { slug, name, config: cfg });
      }
      const event_matcher = { event_type: eventType };
      if (surface.length) event_matcher.surface = surface;
      if (eventType === "command.invoked" && commandName) event_matcher.command_name = commandName;
      if (mentionsBot) event_matcher.mentions_bot = true;
      if (textPattern) event_matcher.text_pattern = textPattern;
      const roles = senderRoles.split(",").map((r) => r.trim()).filter(Boolean);
      if (roles.length) event_matcher.sender_roles_any = roles;

      const config = { kind: action };
      (actionDef.fields || []).forEach((f) => { config[f.key] = (actionFields[f.key] || "").trim(); });

      const body = { event_matcher, config };
      if (replyTarget !== "none") body.reply_target = replyTarget;

      await apiFetch("POST", "/triggers/" + encodeURIComponent(trig.id) + "/subscriptions", body);
      onClose();
      if (typeof onChanged === "function") onChanged();
      if (typeof pushToast === "function") pushToast({ kind: "success", title: "Rule created" });
    } catch (err) {
      CR_toastErr(pushToast, "Create rule failed")(err);
    } finally {
      setCreating(false);
    }
  };

  const toggleSurface = (s) => {
    setSurface((cur) => cur.includes(s) ? cur.filter((x) => x !== s) : cur.concat([s]));
  };

  const missingActionField = (actionDef.fields || []).some((f) => !(actionFields[f.key] || "").trim());

  const footer = (
    <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
      <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
      <Btn kind="primary" onClick={submit} disabled={creating || unsupported || missingActionField}>Create rule</Btn>
    </div>
  );

  return (
    <Modal title={"New rule · " + provider.id} onClose={onClose} footer={footer}>
      <div className="col" style={{ gap: 12 }}>
        <label className="field">
          <span className="lbl">Room / channel id</span>
          <input className="input mono" placeholder="(blank = all rooms)" value={channelId} onChange={(e) => setChannelId(e.target.value)} />
        </label>

        <label className="field">
          <span className="lbl">Event type</span>
          <select className="select mono" value={eventType} onChange={(e) => setEventType(e.target.value)}>
            {capEvents.map((ev) => (
              <option key={ev.type} value={ev.type}>{ev.type}{ev.supported === false ? " (unsupported)" : ""}</option>
            ))}
          </select>
        </label>

        {unsupported && (
          <Banner kind="warning" title="Unsupported" detail={"The " + (provider.provider || "this") + " provider does not emit " + eventType + " events."} />
        )}
        {prereqs.map((pr, i) => (
          <Banner key={i} kind="warning" title="Prerequisite" detail={typeof pr === "string" ? pr : (pr.detail || pr.message || JSON.stringify(pr))} />
        ))}

        <div className="field">
          <span className="lbl">Surface</span>
          <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
            {CR_SURFACES.map((s) => (
              <label key={s} className="row" style={{ gap: 4, alignItems: "center" }}>
                <input type="checkbox" checked={surface.includes(s)} onChange={() => toggleSurface(s)} />
                <span className="text-sm mono">{s}</span>
              </label>
            ))}
          </div>
        </div>

        {eventType === "command.invoked" && (
          <label className="field">
            <span className="lbl">Command name</span>
            <input className="input mono" placeholder="e.g. deploy" value={commandName} onChange={(e) => setCommandName(e.target.value)} />
          </label>
        )}

        <label className="row" style={{ gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={mentionsBot} onChange={(e) => setMentionsBot(e.target.checked)} />
          <span className="text-sm">Only when the bot is mentioned</span>
        </label>

        <label className="field">
          <span className="lbl">Text pattern (regex)</span>
          <input className="input mono" placeholder="optional" value={textPattern} onChange={(e) => setTextPattern(e.target.value)} />
        </label>

        <label className="field">
          <span className="lbl">Sender roles (any, comma-separated)</span>
          <input className="input mono" placeholder="optional" value={senderRoles} onChange={(e) => setSenderRoles(e.target.value)} />
        </label>

        <label className="field">
          <span className="lbl">Action</span>
          <select className="select mono" value={action} onChange={(e) => { setAction(e.target.value); setActionFields({}); }}>
            {CR_ACTIONS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
          </select>
        </label>

        {(actionDef.fields || []).map((f) => (
          <label key={f.key} className="field">
            <span className="lbl">{f.label}</span>
            <input className="input mono" placeholder={f.key} value={actionFields[f.key] || ""} onChange={(e) => setActionField(f.key, e.target.value)} />
          </label>
        ))}

        <label className="field">
          <span className="lbl">Reply target</span>
          <select className="select mono" value={replyTarget} onChange={(e) => setReplyTarget(e.target.value)}>
            {CR_REPLY_TARGETS.map((rt) => <option key={rt.value} value={rt.value}>{rt.label}</option>)}
          </select>
        </label>
      </div>
    </Modal>
  );
}
