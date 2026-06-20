/* global React, Icon, Btn, Modal, Banner, Card, CardList, Fab */

// Channel rule-editor page: a capability-aware event -> action binding
// editor. Lists channel triggers per provider/room and lets an operator
// create bindings (Subscriptions) that map a NormalizedEventType + an
// EventMatcher to an action (start_chat / chat_message / *_fresh_session)
// and a ReplyTarget. The event picker reads ProviderCapabilities and
// warns about prerequisites (Discord MESSAGE CONTENT intent, Telegram
// privacy-mode-off, Slack scopes).
//
// Idiom mirrors channels.jsx: every const is prefixed CR_ so the shared
// babel-standalone IIFE scope stays clash-free, and window.primerApi is
// read inside each component (never destructured at module top) so the
// docs-embed stub install order is honoured.

const { apiFetch, useResource, useMutation, useRouter, useViewport } = window.primerApi;

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

const CR_SURFACES = ["dm", "channel", "thread"];

const CR_ACTIONS = [
  { value: "start_chat", label: "start_chat" },
  { value: "chat_message", label: "chat_message" },
  { value: "agent_fresh_session", label: "agent_fresh_session" },
  { value: "graph_fresh_session", label: "graph_fresh_session" },
];

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

function ChannelRulesPage({ pushToast }) {
  const { isMobile } = useViewport();
  const providers = useResource("cr-providers", (signal) => apiFetch("GET", "/channel_providers?limit=200", null, { signal }), {});
  const triggers = useResource("cr-triggers", (signal) => apiFetch("GET", "/channel_triggers?limit=200", null, { signal }), {});
  const [showNew, setShowNew] = React.useState(false);

  if (providers.error && !providers.data) {
    return <Banner kind="error" title={providers.error.title || "Couldn't load channel providers"} detail={providers.error.detail || providers.error.message} actions={<Btn size="sm" icon="refresh" onClick={providers.refetch}>Retry</Btn>} />;
  }
  if (providers.loading && !providers.data) {
    return <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading...</div>;
  }
  const provs = (providers.data && providers.data.items) || [];
  const trigs = (triggers.data && triggers.data.items) || [];
  // empty state
  if (provs.length === 0) {
    return <Banner kind="info" title="No channel providers yet" detail="Register a Slack, Telegram, or Discord provider under Channels first, then add rules here." />;
  }
  return (
    <div className="col" style={{ gap: 14 }}>
      {provs.map((p) => <CR_ProviderSection key={p.id} provider={p} triggers={trigs.filter((t) => t.config && t.config.provider_id === p.id)} pushToast={pushToast} onChanged={() => { triggers.refetch(); }} />)}
      {isMobile && <Fab icon="plus" label="New rule" onClick={() => setShowNew(true)} />}
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
  const subs = useResource(subsKey, (signal) => apiFetch("GET", "/channel_triggers/" + encodeURIComponent(trigger.id) + "/bindings", null, { signal }), {});
  const room = (trigger.config && trigger.config.channel_id) || "(all rooms)";
  const items = (subs.data && subs.data.items) || [];

  const del = useMutation(
    (subscription_id) => apiFetch("DELETE", "/channel_triggers/" + encodeURIComponent(trigger.id) + "/bindings/" + encodeURIComponent(subscription_id)),
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
          <span className="mono text-sm">{(s.config && s.config.action) || s.action || "?"}</span>
          <span className="muted text-sm mono">reply: {(s.reply_target && s.reply_target.kind) || "none"}</span>
          <div style={{ marginLeft: "auto" }}>
            <Btn size="sm" kind="ghost" icon="trash" onClick={() => del.mutate(s.id)} disabled={del.loading}>Delete</Btn>
          </div>
        </div>
      ))}
    </div>
  );
}

// The capability-aware rule editor. Fetches ProviderCapabilities, renders
// the event picker (warning on unsupported / prerequisite-bearing types),
// the matcher predicate fields, the action select, and the ReplyTarget
// select. Creates the binding (creating the channel trigger first if the
// room has none yet).
function CR_RuleModal({ provider, triggers, onClose, onChanged, pushToast }) {
  const caps = useResource("cr-caps-" + provider.id, (signal) => apiFetch("GET", "/channel_providers/" + encodeURIComponent(provider.id) + "/capabilities", null, { signal }), {});

  const [channelId, setChannelId] = React.useState("");
  const [eventType, setEventType] = React.useState("message.posted");
  const [surface, setSurface] = React.useState([]);
  const [commandName, setCommandName] = React.useState("");
  const [mentionsBot, setMentionsBot] = React.useState(false);
  const [textPattern, setTextPattern] = React.useState("");
  const [senderRoles, setSenderRoles] = React.useState("");
  const [action, setAction] = React.useState("start_chat");
  const [replyTarget, setReplyTarget] = React.useState("source_thread");
  const [creating, setCreating] = React.useState(false);

  // Capability lookup: ProviderCapabilities is { event_types: [{type,
  // supported, prerequisites: [...]}] }. Fall back to the static taxonomy
  // (all supported, no prereqs) when the response hasn't arrived yet.
  const capEvents = (caps.data && caps.data.event_types) || CR_EVENT_TYPES.map((t) => ({ type: t, supported: true, prerequisites: [] }));
  const selectedCap = capEvents.find((e) => e.type === eventType) || { type: eventType, supported: true, prerequisites: [] };
  const prereqs = selectedCap.prerequisites || [];
  const unsupported = selectedCap.supported === false;

  // Resolve (or lazily create) the channel trigger for the chosen room,
  // then create the binding.
  const submit = async () => {
    setCreating(true);
    try {
      let trig = triggers.find((t) => (t.config && t.config.channel_id) === (channelId || null) || (t.config && t.config.channel_id || "") === channelId);
      if (!trig) {
        const cfg = { provider_id: provider.id };
        if (channelId) cfg.channel_id = channelId;
        trig = await apiFetch("POST", "/channel_triggers", { config: cfg });
      }
      const event_matcher = { event_type: eventType };
      if (surface.length) event_matcher.surface = surface;
      if (eventType === "command.invoked" && commandName) event_matcher.command_name = commandName;
      if (mentionsBot) event_matcher.mentions_bot = true;
      if (textPattern) event_matcher.text_pattern = textPattern;
      const roles = senderRoles.split(",").map((r) => r.trim()).filter(Boolean);
      if (roles.length) event_matcher.sender_roles_any = roles;
      const body = {
        event_matcher,
        config: { action },
        reply_target: { kind: replyTarget },
      };
      await apiFetch("POST", "/channel_triggers/" + encodeURIComponent(trig.id) + "/bindings", body);
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

  const footer = (
    <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
      <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
      <Btn kind="primary" onClick={submit} disabled={creating || unsupported}>Create rule</Btn>
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
          <Banner kind="warning" title="Unsupported" detail={"The " + provider.provider + " provider does not emit " + eventType + " events."} />
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
          <select className="select mono" value={action} onChange={(e) => setAction(e.target.value)}>
            {CR_ACTIONS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
          </select>
        </label>

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
