/* global React, Icon, Btn, Modal, StatusPill, Banner, PK_FormOverlay, PK_Field, PK_Grid, PK_Fold, PK_Segmented, PK_CheckTable */
// Model profiles: one (provider, model) pair plus its API-level config --
// OR (01a067c4) an ORDERED pool of other "single" profiles with a routing/
// failover policy, kind="aggregated". The two kinds share this module and
// its card/grid/modal, but not a provider: an aggregated profile has no
// provider_id of its own, which is why it also gets its own provider-
// agnostic browse surface (MP_AllProfilesPanel, below) alongside the
// existing per-provider panel (provider-catalog.jsx's PC_ProfilesPanel,
// unchanged, single-kind only, scoped to one provider's own rows).
//
// The entity exists so ONE model can be registered several times under one
// provider with different settings -- a reasoning and a non-reasoning
// variant of the same model, say. That is why the list groups by provider
// and shows model_name as a secondary column rather than as the identity:
// two rows sharing a model_name is the normal case here, not a duplicate.
//
// Platform revamp Plan 03: the profile forms are overlays on the platform kit
// (ui/components/shared/platform-kit.jsx). MP_ProfileModal, which the Platform
// page's NV_ProfileModalHost mounts, is a dispatcher with no state: a new
// profile or an aggregated one is MP_AggregateOverlay, any other is
// MP_SingleProfileOverlay. Everything that is not drawing (the order of the
// members, the smallest window, the bodies that go on the wire, which field a
// refusal belongs under) is a plain function in the helper block below, which
// tests/ui/test_llm_profiles_helpers.py evaluates as written.

const MP_REASONING_LEVELS = ["off", "minimal", "low", "medium", "high"];

// ---- MP helpers: plain JS, no JSX (tests/ui/test_llm_profiles_helpers.py evaluates this block) ----
// Everything the profile forms decide that is not drawing: plain functions of their arguments, `function` declarations and `var` constants, so each is a global in the
// console bundle and in the bare V8 context where the tests evaluate this block as written. No JSX and no hook in it.

// The routing choices of an aggregated profile. The values are the model's enum values and the defaults are the model's own (primer/model/model_profile.py): failover_on
// defaults to the WIDER class, transient_and_config. The labels and the helper texts are design 7.2's.
var MP_AGGREGATE_DEFAULTS = { strategy: "sequential", failover_point: "before_first_token", failover_on: "transient_and_config" };
var MP_ROUTING_OPTIONS = [
  { value: "sequential", label: "Sequential fallback" },
  { value: "round_robin", label: "Round robin" },
];
var MP_ROUTING_HELP = {
  sequential: "Always try profile 1 first; if it answers 429, a 5xx or times out, move to the next.",
  round_robin: "Rotate which profile goes first on each call.",
};
var MP_FAILOVER_POINT_OPTIONS = [
  { value: "before_first_token", label: "Before the first token" },
  { value: "mid_stream", label: "Also mid-stream" },
];
var MP_FAILOVER_POINT_HELP = "Failing over mid-stream can repeat text the user has already seen.";
var MP_FAILOVER_ON_OPTIONS = [
  { value: "transient", label: "Transient errors" },
  { value: "transient_and_config", label: "Also auth and config errors" },
];
var MP_FAILOVER_ON_HELP = "Transient errors are 429, 5xx, timeouts and network failures.";

// "262,144". The grouping is a pattern, never the locale's, so the console says the same thing on every machine. "" unless n is a positive integer.
function MP_fmtTokens(n) {
  if (typeof n !== "number" || !(n > 0) || n % 1 !== 0 || n > 9007199254740991) return "";
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function MP_isSingle(row) {
  return !(row && row.kind === "aggregated");
}

// A row a harness owns: the server refuses its PUT and DELETE (409), so the console offers neither.
function MP_isManaged(row) {
  return !!(row && row.harness_id);
}

// The context window of a listed profile, or null when it has none a person can read (an aggregated row stores none).
function MP_windowOf(row) {
  var n = row ? row.context_length : null;
  return typeof n === "number" && n > 0 && n % 1 === 0 ? n : null;
}

// The rows by id. No prototype: a profile may be called "constructor".
function MP_byId(rows) {
  var map = Object.create(null);
  (rows || []).forEach(function (row) {
    if (row && row.id != null) map[row.id] = row;
  });
  return map;
}

// The rows of the members table. The ticked ones come first, in MEMBER ORDER, because that order is the failover chain and the number beside each is its place in it. The rest
// follow by id. Only single profiles are candidates (nesting is refused), and the aggregate being edited is not its own. A member the list does not hold (the list is capped at
// 200 profiles) stays a ticked row with no window, so a save never drops it.
function MP_memberRows(rows, members, selfId) {
  var byId = MP_byId(rows);
  var seen = Object.create(null);
  var ticked = [];
  (members || []).forEach(function (id) {
    if (seen[id]) return;
    seen[id] = true;
    ticked.push({ id: id, checked: true, order: ticked.length + 1, window: MP_windowOf(byId[id]) });
  });
  var rest = [];
  (rows || []).forEach(function (row) {
    if (!row || row.id == null || seen[row.id] || row.id === selfId || !MP_isSingle(row)) return;
    seen[row.id] = true;
    rest.push({ id: row.id, checked: false, order: null, window: MP_windowOf(row) });
  });
  rest.sort(function (a, b) {
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
  return ticked.concat(rest);
}

// Ticking appends and unticking removes; the others keep their places. None of the three below changes its input.
function MP_toggleMember(members, id) {
  var list = (members || []).slice();
  var at = list.indexOf(id);
  if (at >= 0) list.splice(at, 1);
  else list.push(id);
  return list;
}

// Select all appends every candidate that is not ticked yet, in the order the table lists them; clearing it unticks every row.
function MP_toggleAllMembers(members, rows, selfId, checked) {
  if (!checked) return [];
  var list = (members || []).slice();
  MP_memberRows(rows, list, selfId).forEach(function (row) {
    if (!row.checked) list.push(row.id);
  });
  return list;
}

// Swap a member with its neighbour (delta -1 up, 1 down); at an end, or for an id that is not a member, nothing moves.
function MP_moveMember(members, id, delta) {
  var list = (members || []).slice();
  var from = list.indexOf(id);
  var to = from + delta;
  if (from < 0 || to < 0 || to >= list.length) return list;
  var held = list[from];
  list[from] = list[to];
  list[to] = held;
  return list;
}

// The aggregate's context window is the smallest of its members' windows. The server computes the same minimum when it resolves the profile and serves it on no route, so the
// console computes it from the list it holds. null when there is no member or when one has no window the list shows: never a guess.
function MP_aggregateWindow(members, rows) {
  var list = members || [];
  if (!list.length) return null;
  var byId = MP_byId(rows);
  var smallest = null;
  for (var i = 0; i < list.length; i++) {
    var size = MP_windowOf(byId[list[i]]);
    if (size === null) return null;
    if (smallest === null || size < smallest) smallest = size;
  }
  return smallest;
}

function MP_windowText(members, rows) {
  if (!(members || []).length) return "Pick the profiles first.";
  var size = MP_aggregateWindow(members, rows);
  if (size === null) return "Unknown: a picked profile is not loaded.";
  return MP_fmtTokens(size) + " (the smallest of the picked profiles)";
}

// Whether the Advanced fold holds a choice that is not the model's default. The fold is folded on a create and on an edit that left both choices alone; an edit whose failover_point
// or failover_on differs from MP_AGGREGATE_DEFAULTS starts it open, so a person sees what is set without asking. A field the row does not carry reads as its default, the way the
// overlay's own state reads it. `row` is the stored profile, or null on a create. A harness-managed row is a different reason to start open (its toggle is disabled with the rest of
// the form), and the overlay adds it: this answers only "is something in there set".
function MP_advancedIsSet(row) {
  if (!row) return false;
  var point = row.failover_point || MP_AGGREGATE_DEFAULTS.failover_point;
  var classes = row.failover_on || MP_AGGREGATE_DEFAULTS.failover_on;
  return point !== MP_AGGREGATE_DEFAULTS.failover_point || classes !== MP_AGGREGATE_DEFAULTS.failover_on;
}

// The server has no id pattern, but an id with a "/" or a space cannot be addressed (GET and DELETE answer 404). The character set is the one the console builds profile
// ids from (PC_profileIdFor in the Register overlay): a letter or digit first, then letters, digits, dots, dashes and underscores.
var MP_NAME_RULE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
function MP_nameError(name) {
  var text = String(name == null ? "" : name).trim();
  if (!text) return "Enter a name.";
  if (!MP_NAME_RULE.test(text)) return "Use letters, digits, dots, dashes and underscores, and start with a letter or a digit.";
  return null;
}

// The body of a create. The model requires a description and the design (7.2) has no such field, so the console writes one from the members.
function MP_aggregateBody(draft) {
  var members = (draft.members || []).slice();
  return {
    id: String(draft.name == null ? "" : draft.name).trim(),
    description: "Aggregate of " + members.join(", "),
    kind: "aggregated",
    members: members,
    strategy: draft.strategy,
    failover_point: draft.failover_point,
    failover_on: draft.failover_on,
  };
}

// The body of an edit: the stored row with the form's fields laid over it. PUT is a full replace that ignores unknown keys, so a body built from only what a form draws resets
// every field it leaves out (a profile's `config`, an aggregate's description). The list route adds agent_count and graph_node_count, and the page may annotate a row with a
// key that starts with "_": none of those is the profile's, so none is sent. `existing` is not changed.
function MP_editBody(existing, edits) {
  var body = {};
  Object.keys(existing || {}).forEach(function (key) {
    if (key === "agent_count" || key === "graph_node_count" || key.charAt(0) === "_") return;
    body[key] = existing[key];
  });
  Object.keys(edits || {}).forEach(function (key) {
    body[key] = edits[key];
  });
  return body;
}

// Where a refused save is told. `fieldMap` maps a server field to the form's own field name. A refusal about a mapped field goes under it, in the server's own words. A 409
// goes under the name when the form has one (`id` is in the map only on a create: a 409 on an edit is about the row, not the name). Anything else is a line above the footer.
function MP_refusalErrors(err, fieldMap) {
  var map = fieldMap || {};
  var has = Object.prototype.hasOwnProperty;
  var refusal = window.primerApi.readRefusal(err);
  var fields = {};
  if (refusal.field && has.call(map, refusal.field)) {
    fields[map[refusal.field]] = refusal.sentence || refusal.message;
    return { fields: fields, form: null };
  }
  if (err && err.status === 409 && has.call(map, "id")) {
    fields[map.id] = refusal.message;
    return { fields: fields, form: null };
  }
  return { fields: fields, form: refusal.message };
}

// The context window as typed: a whole number of tokens, 1 or more. The field is a number input, but "1e3" and "12.5" are numbers to it and neither is a window.
function MP_contextError(text) {
  var typed = text == null ? "" : String(text);
  if (!typed.trim()) return "Enter the context window.";
  if (!/^[1-9][0-9]*$/.test(typed)) return "Enter a whole number of tokens, 1 or more.";
  return null;
}

// The options of the provider select: every LLM provider as "<id> (<type>)", in the order given. A profile can name a provider that is not in the list (it was deleted, or the
// list is capped): that provider stays an option, marked, so showing the form never changes the profile's provider.
function MP_providerOptions(providers, currentId) {
  var options = [];
  var listed = false;
  (providers || []).forEach(function (p) {
    if (!p || p.id == null) return;
    if (p.id === currentId) listed = true;
    options.push({ value: p.id, label: p.provider ? p.id + " (" + p.provider + ")" : p.id });
  });
  if (currentId && !listed) options.push({ value: currentId, label: currentId + " (not found)" });
  return options;
}
// ---- end MP helpers ----

function MP_ReasoningChip({ level }) {
  if (!level) {
    return (
      <span style={{ color: "var(--text-4)", fontSize: 11.5 }}>
        vendor default
      </span>
    );
  }
  const strong = level === "high" || level === "off";
  return (
    <span style={{
      display: "inline-block", padding: "1px 7px", borderRadius: 4,
      fontSize: 11, fontWeight: 600, letterSpacing: "0.02em",
      background: strong ? "var(--accent-dim)" : "var(--bg-2)",
      color: strong ? "var(--accent)" : "var(--text-3)",
      border: "1px solid var(--border)",
    }}>{level}</span>
  );
}

// Kind chip: rendered on every aggregated card so an operator scanning the
// provider-agnostic grid (mixed single + aggregated rows) can tell them
// apart at a glance without opening each one.
function MP_KindChip({ kind }) {
  if (kind !== "aggregated") return null;
  return (
    <span className="pill" data-testid="profile-card-kind-chip"
      style={{ color: "var(--accent)", borderColor: "var(--border)", background: "var(--accent-dim)" }}>
      aggregated
    </span>
  );
}

// ---------------------------------------------------------------------------
// Platform wave P1b item 1: card anatomy (screenshot 8). Platform wave P4
// wired these into provider-catalog.jsx's PC_ProfilesPanel (the only live
// mount today - see that file) and added the bound-by/unbound and
// provider-down badges below, now that the backend P2 wave actually
// serves agent_count/graph_node_count on the list route
// (api/routers/model_profiles.py's ModelProfileWithUsage) and the panel
// can join its own already-fetched provider row for reachability. The
// "overridable"/"delete blocked" rows stay static copy describing system
// behavior (the reference's own framing), not derived facts.
//
// 01a067c4: also mounted from MP_AllProfilesPanel now (provider-agnostic),
// so providerDown is optional -- an aggregated row has no single provider
// to be down, and a single row reached through that panel has no
// pre-fetched provider list to join against the way the per-provider panel
// does.
// ---------------------------------------------------------------------------
function MP_BoundBadge({ agentCount, graphNodeCount }) {
  const agents = agentCount || 0;
  const nodes = graphNodeCount || 0;
  const bound = agents > 0 || nodes > 0;
  const color = bound ? "var(--text-2)" : "var(--amber)";
  return (
    <span className="pill" data-testid="profile-card-bound-badge"
      style={{ color, borderColor: "var(--border)", background: "var(--bg-2)" }}>
      <span className="dot" style={{ background: color }}></span>
      {bound
        ? `bound by ${agents} agent${agents === 1 ? "" : "s"}`
          + (nodes > 0 ? ` · ${nodes} graph node${nodes === 1 ? "" : "s"}` : "")
        : "unbound"}
    </span>
  );
}

function MP_ProfileCard({ profile, onOpen, onDeleted, providerDown }) {
  const { apiFetch } = window.primerApi;
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [err, setErr] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const doDelete = async () => {
    setErr("");
    setBusy(true);
    try {
      await apiFetch("DELETE", `/model_profiles/${encodeURIComponent(profile.id)}`);
      setConfirmDelete(false);
      if (onDeleted) onDeleted();
    } catch (e) {
      // The backend's ReferenceCheck (routers/model_profiles.py) 409s with a
      // PLAIN FORMATTED STRING in `detail` - "in_use_by: N agent(s) reference
      // '<id>' (first: '<id>')" - not a structured payload, and it names ONE
      // blocking reference (the count is the size of a one-row page: "at
      // least one"). The one reader (window.primerApi.readRefusal) says it in
      // words (admin review ADM-12); this also covers the "member of an
      // aggregate" reference kind (model_profiles.py's second ReferenceCheck),
      // which has the same shape. The referrer is named, not linked.
      setErr(window.primerApi.readRefusal(e, String(e)).message);
    } finally {
      setBusy(false);
    }
  };

  const isAggregated = profile.kind === "aggregated";
  const ctxK = profile.context_length
    ? Math.round(profile.context_length / 1000) : null;
  const reasoning = profile.config && profile.config.reasoning;
  const members = profile.members || [];

  // harness_id is set on a profile a harness owns (managed lifecycle,
  // e.g. a bootstrap operator default) - the pre-P4 <ul> in
  // PC_ProfilesPanel showed a "managed by" pill instead of Delete for
  // these; that guard moves onto the card itself here so it survives
  // wherever the card is mounted next, not just that one panel.
  const harnessManaged = !!profile.harness_id;

  return (
    <div className="pc-card" data-testid={`profile-card-${profile.id}`}>
      <div className="pc-card-head">
        <span className="pc-card-title mono">{profile.id}</span>
        <span style={{ flex: 1 }} />
        <MP_KindChip kind={profile.kind} />
        <MP_BoundBadge agentCount={profile.agent_count} graphNodeCount={profile.graph_node_count} />
        {providerDown ? (
          <span className="pill" data-testid={`profile-card-provider-down-${profile.id}`}
            style={{ color: "var(--red)", borderColor: "var(--border)", background: "var(--bg-2)" }}>
            <span className="dot" style={{ background: "var(--red)" }}></span>
            provider down
          </span>
        ) : null}
      </div>
      <div className="pc-card-subtitle" data-testid={`profile-card-subtitle-${profile.id}`}>
        {isAggregated
          ? `${members.length} member${members.length === 1 ? "" : "s"}`
          : [profile.provider_id, profile.model_name,
              ctxK != null ? `${ctxK}k ctx` : null].filter(Boolean).join(" · ")}
      </div>
      <div className="pc-card-facts">
        {isAggregated ? (
          <>
            <div className="pc-card-fact">
              <span className="muted">members</span>
              <span className="mono text-sm" data-testid={`profile-card-members-${profile.id}`}>
                {members.length ? members.join(", ") : "(none)"}
              </span>
            </div>
            <div className="pc-card-fact">
              <span className="muted">strategy</span>
              <span>{profile.strategy || "sequential"}</span>
            </div>
          </>
        ) : (
          <div className="pc-card-fact">
            <span className="muted">reasoning</span>
            <MP_ReasoningChip level={reasoning} />
          </div>
        )}
        <div className="pc-card-fact">
          <span className="muted">overridable</span>
          <span>at create · switch · per graph node</span>
        </div>
        <div className="pc-card-fact">
          <span className="muted">delete</span>
          <span>blocked while referenced</span>
        </div>
      </div>
      <div className="pc-card-footer">
        <Btn kind="primary" size="sm" onClick={() => onOpen && onOpen(profile)}>
          Open
        </Btn>
        <span style={{ flex: 1 }} />
        {harnessManaged ? (
          <span className="pill" title="managed by a harness">{profile.harness_id}</span>
        ) : confirmDelete ? (
          <>
            <Btn kind="danger" size="sm" disabled={busy}
              data-testid={`profile-card-delete-confirm-${profile.id}`}
              onClick={doDelete}>
              Confirm
            </Btn>
            <Btn kind="ghost" size="sm" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Btn>
          </>
        ) : (
          <Btn kind="ghost" size="sm"
            data-testid={`profile-card-delete-${profile.id}`}
            onClick={() => { setErr(""); setConfirmDelete(true); }}>
            Delete
          </Btn>
        )}
      </div>
      {err ? (
        <div className="field-help" data-testid={`profile-card-error-${profile.id}`}>
          delete blocked while referenced - {err}
        </div>
      ) : null}
    </div>
  );
}

function MP_ProfilesGrid({ profiles, onOpen, onDeleted, providerDown }) {
  const items = profiles || [];
  if (!items.length) {
    return (
      <div className="empty-state" data-testid="profiles-empty">
        <h3>No model profiles yet</h3>
        <p>Register one to give agents a named model + config to bind to.</p>
      </div>
    );
  }
  return (
    <div className="pc-card-grid" data-testid="profiles-grid">
      {items.map((p) => (
        <MP_ProfileCard key={p.id} profile={p} onOpen={onOpen} onDeleted={onDeleted}
          providerDown={providerDown} />
      ))}
    </div>
  );
}

// Create/edit modal. MP_ProfileModal is the component the Platform page's NV_ProfileModalHost mounts, and it is a dispatcher with no state and no hook: it decides which
// overlay a profile belongs to and mounts that one. Nothing to draw (open is false) draws nothing. A new profile with no prefill, and an aggregated one, is the aggregated
// overlay (create / edit): "+ Aggregate" is the page's one create (design 7.1), and a single profile comes from the Register provider overlay's import. Every other profile
// (a single one to view or edit, or the legacy single create the Providers page still reaches with a prefill) is the single-profile overlay. The overlay is keyed on the
// profile's id, so a different profile mounted in its place starts from its own fields and never inherits the first one's draft. `allProfiles` is ignored: the aggregated
// overlay fetches the list it needs itself.
function MP_ProfileModal(props) {
  if (!props.open) return null;
  const existing = props.existing || null;
  const single = existing ? MP_isSingle(existing) : !!props.prefill;
  const key = existing ? existing.id : "new";
  if (single) {
    return (
      <MP_SingleProfileOverlay key={key} existing={existing} providers={props.providers || []}
        prefill={props.prefill || null} onClose={props.onClose} onSaved={props.onSaved} />
    );
  }
  return <MP_AggregateOverlay key={key} existing={existing} onClose={props.onClose} onSaved={props.onSaved} />;
}

// A harness-managed profile is shown, not edited: its fields sit in a disabled fieldset, so every native control under it (the kit's checkboxes and segmented buttons too) is
// read-only without the kit growing a prop for it. The fieldset takes over the column layout of the overlay body it sits in.
const MP_READONLY_FIELDSET = { display: "flex", flexDirection: "column", gap: 16, minWidth: 0, margin: 0, padding: 0, border: "none" };

// The single profile's overlay (design 7.1: "opening a profile shows its overlay for viewing and editing the fields the model allows"): the id (fixed once the profile exists),
// the description, the provider and the model it names, and the context window. A profile's `config` (reasoning, extended) is not drawn here: the Reasoning control returns in
// plan 03 Task 7, and until then `config` is kept as stored. Model calls apply it, so a save must not lose it: an edit sends the stored row back with these four fields laid over
// it (MP_editBody), and `config` and every other field the form does not draw round-trip untouched. `prefill` is the legacy single create that the Providers page still opens
// ("New profile" under a provider, until Plan 02 removes it); there is no other single create. The fields start from the profile the overlay is mounted with and then belong to
// the person typing: a new `providers` array (the host hands over a fresh empty one on every render until its list loads) must not wipe a draft.
function MP_SingleProfileOverlay({ existing, providers, prefill, onClose, onSaved }) {
  const isEdit = existing != null;
  const managed = MP_isManaged(existing);
  const seed = existing || prefill || {};
  const [id, setId] = React.useState(() => seed.id || "");
  const [description, setDescription] = React.useState(() => seed.description || "");
  const [providerId, setProviderId] = React.useState(() => seed.provider_id || "");
  const [modelName, setModelName] = React.useState(() => seed.model_name || "");
  const [contextText, setContextText] = React.useState(() => String(seed.context_length || 128000));
  const [touched, setTouched] = React.useState({});
  const [saving, setSaving] = React.useState(false);
  const [refused, setRefused] = React.useState({ fields: {}, form: null });

  // Until a provider is chosen the select shows the first one, so that is the value (the list may arrive after the overlay opens).
  const provider = providerId || (providers && providers[0] ? providers[0].id : "");
  const idError = !isEdit && id.trim() ? MP_nameError(id) : null;
  const descriptionError = description.trim() ? null : "Enter a description.";
  const modelError = modelName.trim() ? null : "Enter the model name.";
  const contextError = MP_contextError(contextText);
  const formReady = !managed && (isEdit || MP_nameError(id) === null) && !descriptionError && !!provider && !modelError && !contextError;
  // A blank field is not a mistake until it has been typed in; a complaint from the server is about what was in the field, so editing the field takes it away.
  const shown = (field, error) => (touched[field] ? error : null) || undefined;
  const forget = (field) => setRefused((r) => {
    if (!r.fields[field]) return r;
    const fields = Object.assign({}, r.fields);
    delete fields[field];
    return { fields, form: r.form };
  });
  const typed = (field, set) => (e) => {
    set(e.target.value);
    setTouched((t) => (t[field] ? t : Object.assign({}, t, { [field]: true })));
    forget(field);
  };

  const submit = async () => {
    if (saving || !formReady) return;
    setSaving(true);
    setRefused({ fields: {}, form: null });
    const api = window.primerApi;
    const draft = { description: description.trim(), provider_id: provider, model_name: modelName.trim(), context_length: Number(contextText) };
    let row;
    try {
      row = isEdit
        ? await api.apiFetch("PUT", "/model_profiles/" + encodeURIComponent(existing.id), MP_editBody(existing, draft))
        : await api.apiFetch("POST", "/model_profiles", Object.assign({ id: id.trim() }, draft));
    } catch (err) {
      const where = { description: "description", provider_id: "provider", model_name: "model", context_length: "context" };
      if (!isEdit) where.id = "id";
      setRefused(MP_refusalErrors(err, where));
      setSaving(false);
      return;
    }
    setSaving(false);
    const name = isEdit ? existing.id : id.trim();
    api.toastPush({ kind: "success", text: (isEdit ? "Saved " : "Created ") + name });
    if (onSaved) onSaved(row);
    onClose();
  };

  const fields = (
    <>
      <PK_Field label="Id" error={idError || refused.fields.id || undefined}>
        <input value={id} readOnly={isEdit} autoFocus={!isEdit} autoComplete="off" spellCheck={false}
          placeholder="<provider>--<model>" data-testid="mp-single-id" onChange={typed("id", setId)} />
      </PK_Field>
      <PK_Field label="Description" error={shown("description", descriptionError) || refused.fields.description}>
        <input value={description} data-testid="mp-single-description" onChange={typed("description", setDescription)} />
      </PK_Field>
      <PK_Grid cols={2}>
        <PK_Field label="Provider" error={refused.fields.provider || undefined}>
          <select value={provider} data-testid="mp-single-provider" onChange={typed("provider", setProviderId)}>
            {MP_providerOptions(providers, provider).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </PK_Field>
        <PK_Field label="Model" error={shown("model", modelError) || refused.fields.model}>
          <input value={modelName} autoComplete="off" spellCheck={false} data-testid="mp-single-model" onChange={typed("model", setModelName)} />
        </PK_Field>
      </PK_Grid>
      <PK_Field label="Context window" help="The most tokens the model accepts in one request. Compaction uses it."
        error={shown("context", contextError) || refused.fields.context}>
        <input type="number" min={1} step={1} value={contextText} data-testid="mp-single-context" onChange={typed("context", setContextText)} />
      </PK_Field>
    </>
  );

  return (
    <PK_FormOverlay
      title={managed ? "LLM profile" : isEdit ? "Edit LLM profile" : "New LLM profile"}
      testid="mp-single"
      onClose={onClose}
      footer={{
        left: <Btn data-testid="mp-single-cancel" onClick={onClose}>{managed ? "Close" : "Cancel"}</Btn>,
        right: managed ? null : (
          <Btn kind="primary" data-testid="mp-single-submit" disabled={!formReady} busy={saving} onClick={submit}>
            {isEdit ? "Save" : "Create"}
          </Btn>
        ),
      }}
    >
      {managed ? (
        <div data-testid="mp-single-managed">
          <Banner kind="info" title={"Managed by harness " + existing.harness_id + ". Re-run the harness to change it."} />
        </div>
      ) : null}
      {managed ? <fieldset disabled style={MP_READONLY_FIELDSET}>{fields}</fieldset> : fields}
      <div className="field-help" data-testid="profile-modal-footnote">
        Agents bind to a profile. A session, a binding switch or a graph node can override it. Deleting is refused while anything uses it.
      </div>
      {refused.form ? (
        <div className="field-help" role="alert" data-testid="mp-single-error" style={{ color: "var(--red)" }}>{refused.form}</div>
      ) : null}
    </PK_FormOverlay>
  );
}

// The aggregated profile's overlay (design 7.2): create with no `existing`, view and edit with one. Name, the members table with the order (the failover chain), the routing
// switch, the Advanced choices and the smallest context window of the picked members. The Advanced fold is folded on a create and starts open on an edit that holds a choice
// that is not the model's default (MP_advancedIsSet) or on a harness-managed row. The model requires a description the design has no field for: a create writes
// "Aggregate of <ids>", an edit shows the stored one. `existing` is the row the page listed; an edit sends it back whole with the form's fields laid over it (MP_editBody),
// because PUT is a full replace and the row holds fields this form does not draw (`config`).
function MP_AggregateOverlay({ existing, onClose, onSaved }) {
  const isEdit = existing != null;
  const managed = MP_isManaged(existing);
  const selfId = isEdit ? existing.id : null;
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState(() => (isEdit ? existing.description || "" : ""));
  const [members, setMembers] = React.useState(() => (isEdit ? (existing.members || []).slice() : []));
  const [strategy, setStrategy] = React.useState(() => (isEdit && existing.strategy) || MP_AGGREGATE_DEFAULTS.strategy);
  const [failoverPoint, setFailoverPoint] = React.useState(() => (isEdit && existing.failover_point) || MP_AGGREGATE_DEFAULTS.failover_point);
  const [failoverOn, setFailoverOn] = React.useState(() => (isEdit && existing.failover_on) || MP_AGGREGATE_DEFAULTS.failover_on);
  const [saving, setSaving] = React.useState(false);
  const [refused, setRefused] = React.useState({ fields: {}, form: null });
  const list = window.primerApi.useResource(
    "mp-aggregate:profiles",
    (signal) => window.primerApi.apiFetch("GET", "/model_profiles?limit=200", null, { signal }),
    { pollMs: null },
  );

  const rows = (list.data && list.data.items) || [];
  const loaded = !!list.data;
  const loading = !loaded && !list.error;
  const loadError = !loaded && list.error ? window.primerApi.readRefusal(list.error).message : null;
  const tooFew = loaded && rows.filter(MP_isSingle).length < 2;

  const shownName = isEdit ? existing.id : name;
  const nameError = !isEdit && name.trim() ? MP_nameError(name) : null;
  const membersRule = members.length === 1 ? "Pick at least two profiles." : null;
  const formReady = !managed && (isEdit ? description.trim() !== "" : MP_nameError(name) === null) && members.length >= 2;

  // A complaint about a field is about what was in it: editing the field takes the complaint away.
  const forget = (field) => setRefused((r) => {
    if (!r.fields[field]) return r;
    const fields = Object.assign({}, r.fields);
    delete fields[field];
    return { fields, form: r.form };
  });
  const changeMembers = (next) => { setMembers(next); forget("members"); };
  const memberRows = MP_memberRows(rows, members, selfId);

  const submit = async () => {
    if (saving || !formReady) return;
    setSaving(true);
    setRefused({ fields: {}, form: null });
    const api = window.primerApi;
    const routing = { strategy, failover_point: failoverPoint, failover_on: failoverOn };
    let row;
    try {
      row = isEdit
        ? await api.apiFetch("PUT", "/model_profiles/" + encodeURIComponent(existing.id), MP_editBody(existing, Object.assign({ description: description.trim(), members }, routing)))
        : await api.apiFetch("POST", "/model_profiles", MP_aggregateBody(Object.assign({ name, members }, routing)));
    } catch (err) {
      setRefused(MP_refusalErrors(err, isEdit ? { members: "members", description: "description" } : { id: "name", members: "members" }));
      setSaving(false);
      return;
    }
    setSaving(false);
    const id = isEdit ? existing.id : name.trim();
    api.toastPush({ kind: "success", text: isEdit ? "Saved " + id : "Created aggregated profile " + id });
    if (onSaved) onSaved(row);
    onClose();
  };

  const fields = (
    <>
      <PK_Field label="Name" error={nameError || refused.fields.name || undefined}>
        <input value={shownName} readOnly={isEdit} autoFocus={!isEdit} autoComplete="off" spellCheck={false}
          data-testid="mp-aggregate-name"
          onChange={(e) => { setName(e.target.value); forget("name"); }} />
      </PK_Field>
      {isEdit ? (
        <PK_Field label="Description" error={description.trim() ? refused.fields.description || undefined : "Enter a description."}>
          <input value={description} readOnly={managed} data-testid="mp-aggregate-description"
            onChange={(e) => { setDescription(e.target.value); forget("description"); }} />
        </PK_Field>
      ) : null}
      <PK_Field label="Members" help="Tick two or more. The order is the fallback order, or the rotation order for Round robin."
        error={membersRule || refused.fields.members || undefined}>
        <PK_CheckTable testid="mp-aggregate-members" extraLabel="Context window and order"
          rows={memberRows.map((r) => ({
            key: r.id,
            label: r.id,
            checked: r.checked,
            disabled: managed,
            extra: (
              <MP_MemberExtra id={r.id} tokens={r.window} order={r.order} movable={!managed}
                onUp={r.order > 1 ? () => changeMembers(MP_moveMember(members, r.id, -1)) : null}
                onDown={r.order && r.order < members.length ? () => changeMembers(MP_moveMember(members, r.id, 1)) : null} />
            ),
          }))}
          onToggle={(key) => changeMembers(MP_toggleMember(members, key))}
          onToggleAll={(checked) => changeMembers(MP_toggleAllMembers(members, rows, selfId, checked))} />
        {loading ? <div className="field-help" data-testid="mp-aggregate-loading">Loading the profiles</div> : null}
        {loadError ? (
          <div className="field-help" role="alert" data-testid="mp-aggregate-load-error" style={{ color: "var(--red)" }}>
            {loadError}{" "}
            <Btn kind="ghost" size="sm" data-testid="mp-aggregate-load-retry" onClick={() => list.refetch()}>Try again</Btn>
          </div>
        ) : null}
        {tooFew ? (
          <div className="field-help" data-testid="mp-aggregate-too-few">
            Aggregating needs at least two LLM profiles. Register an LLM provider and import its models first.
          </div>
        ) : null}
      </PK_Field>
      <PK_Field label="Routing" help={MP_ROUTING_HELP[strategy]}>
        <PK_Segmented testid="mp-aggregate-routing" ariaLabel="Routing" options={MP_ROUTING_OPTIONS} value={strategy}
          onChange={(v) => setStrategy(v)} />
      </PK_Field>
      <PK_Fold label="Advanced" testid="mp-aggregate-advanced" open={managed || MP_advancedIsSet(existing)}>
        <PK_Grid cols={1}>
          <PK_Field label="Fail over" help={MP_FAILOVER_POINT_HELP}>
            <PK_Segmented testid="mp-aggregate-failover-point" ariaLabel="Fail over" options={MP_FAILOVER_POINT_OPTIONS}
              value={failoverPoint} onChange={(v) => setFailoverPoint(v)} />
          </PK_Field>
          <PK_Field label="Fail over on" help={MP_FAILOVER_ON_HELP}>
            <PK_Segmented testid="mp-aggregate-failover-on" ariaLabel="Fail over on" options={MP_FAILOVER_ON_OPTIONS}
              value={failoverOn} onChange={(v) => setFailoverOn(v)} />
          </PK_Field>
        </PK_Grid>
      </PK_Fold>
      <PK_Field label="Context window">
        <div data-testid="mp-aggregate-window" style={{ minHeight: 34, display: "flex", alignItems: "center", fontSize: 12.5 }}>
          {MP_windowText(members, rows)}
        </div>
      </PK_Field>
    </>
  );

  return (
    <PK_FormOverlay
      title={managed ? "Aggregated profile" : isEdit ? "Edit aggregated profile" : "New aggregated profile"}
      testid="mp-aggregate"
      onClose={onClose}
      footer={{
        left: <Btn data-testid="mp-aggregate-cancel" onClick={onClose}>{managed ? "Close" : "Cancel"}</Btn>,
        right: managed ? null : (
          <Btn kind="primary" data-testid="mp-aggregate-submit" disabled={!formReady} busy={saving} onClick={submit}>
            {isEdit ? "Save" : "Create"}
          </Btn>
        ),
      }}
    >
      {managed ? (
        <div data-testid="mp-aggregate-managed">
          <Banner kind="info" title={"Managed by harness " + existing.harness_id + ". Re-run the harness to change it."} />
        </div>
      ) : null}
      {managed ? <fieldset disabled style={MP_READONLY_FIELDSET}>{fields}</fieldset> : fields}
      {refused.form ? (
        <div className="field-help" role="alert" data-testid="mp-aggregate-error" style={{ color: "var(--red)" }}>{refused.form}</div>
      ) : null}
    </PK_FormOverlay>
  );
}

// The "extra" cell of a row of the members table. PK_CheckTable has ONE extra column and design 7.2 wants two (the context window, the order), so the cell holds both: the
// profile's window and, for a ticked row, its place in the failover chain with the two buttons that move it. The button that cannot move a row at an end of the chain says
// aria-disabled and keeps its tab stop (never `disabled`). `tokens` is the window, a number or null; it is not called `window`, which would hide the global in this function.
// `movable` is false on a row nobody may change (a harness-managed aggregate): the order is told, the buttons are not drawn.
function MP_MemberExtra({ id, tokens, order, onUp, onDown, movable }) {
  const arrow = { minWidth: 24, minHeight: 24, padding: "2px 6px", lineHeight: 1.2 };
  return (
    <span style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6 }}>
      <span data-testid={"mp-member-window:" + id} style={{ color: "var(--text-3)", fontVariantNumeric: "tabular-nums" }}>
        {tokens ? MP_fmtTokens(tokens) : "unknown"}
      </span>
      {order ? (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
          <span data-testid={"mp-member-order:" + id} style={{ minWidth: 14, textAlign: "right", fontWeight: 600 }}>{order}</span>
          {movable ? (
            <>
              <button type="button" className="btn btn-ghost btn-sm" style={arrow} data-testid={"mp-member-up:" + id}
                aria-label={"Move " + id + " up"} aria-disabled={onUp ? undefined : "true"}
                onClick={() => { if (onUp) onUp(); }}>↑</button>
              <button type="button" className="btn btn-ghost btn-sm" style={arrow} data-testid={"mp-member-down:" + id}
                aria-label={"Move " + id + " down"} aria-disabled={onDown ? undefined : "true"}
                onClick={() => { if (onDown) onDown(); }}>↓</button>
            </>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}

// Provider-agnostic browse/create/edit surface for EVERY model profile,
// single and aggregated alike (01a067c4, approved IA ruling). Mounted as a
// provider-catalog "class" panel (form:"panel", same precedent as ssp/
// workspace/channel), NOT a standalone page -- tests/ui/test_model_profiles_
// page.py's ban is specifically about a separate PAGE/nav entry breaking
// the providers one-page IA doctrine; this satisfies that same doctrine
// (profiles stay reachable only from the providers page) while giving an
// aggregated profile -- which has no provider_id, so it can never appear
// in PC_ProfilesPanel's provider-scoped list -- a home the original
// per-provider design never needed to have.
function MP_AllProfilesPanel({ pushToast }) {
  const { useResource, apiFetch } = window.primerApi;
  const [open, setOpen] = React.useState(false);
  const [editing, setEditing] = React.useState(null);
  const rows = useResource(
    "mp-all:profiles",
    (signal) => apiFetch("GET", "/model_profiles?limit=200", null, { signal }),
    { pollMs: null },
  );
  const providers = useResource(
    "mp-all:llm-providers",
    (signal) => apiFetch("GET", "/llm_providers?limit=200", null, { signal }),
    { pollMs: null },
  );
  const allProfiles = (rows.data && rows.data.items) || [];
  const providerRows = (providers.data && providers.data.items) || [];

  return (
    <div className="col" style={{ gap: 8 }} data-testid="all-model-profiles">
      <div className="muted text-sm">
        Every model profile across every provider, plus aggregated pools
        of two or more profiles. An aggregated profile has no provider of
        its own, so it lives only here, not under any one provider's page.
      </div>
      <window.MP_ProfilesGrid
        profiles={allProfiles}
        onOpen={(row) => setEditing(row)}
        onDeleted={() => rows.refetch()}
      />
      <Btn kind="ghost" onClick={() => setOpen(true)}>+ Aggregate</Btn>
      {(open || editing) && (
        <MP_ProfileModal
          open
          existing={editing}
          providers={providerRows}
          allProfiles={allProfiles}
          onClose={() => { setOpen(false); setEditing(null); }}
          onSaved={() => rows.refetch()}
        />
      )}
    </div>
  );
}

window.MP_ProfileModal = MP_ProfileModal;
window.MP_AggregateOverlay = MP_AggregateOverlay;
window.MP_ProfileCard = MP_ProfileCard;
window.MP_ProfilesGrid = MP_ProfilesGrid;
window.MP_AllProfilesPanel = MP_AllProfilesPanel;
