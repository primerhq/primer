/* global React, Btn, Banner, PK_FormOverlay, PK_Field, PK_Grid, PK_Fold, PK_Segmented, PK_CheckTable */
// LLM profiles: a "single" profile is one (provider, model) pair plus its API-level config; an "aggregated" one (kind="aggregated", 01a067c4) is an
// ORDERED chain of single profiles with a routing and failover policy, and has no provider of its own.
//
// The entity exists so ONE model can be registered several times under one provider with different settings (a reasoning and a non-reasoning variant
// of the same model, say), so two profiles sharing a model_name is the normal case here, not a duplicate.
//
// Platform revamp Plan 03: the LLM Profiles page (the `profiles` entry of NV_PLAT_PAGES, nv-platform.jsx) draws its cards through the platform kit
// from MP_profileCard, and its forms are overlays on the kit (ui/components/shared/platform-kit.jsx). MP_ProfileModal, which the page's
// NV_ProfileModalHost mounts, is a dispatcher with no state: a new profile or an aggregated one is MP_AggregateOverlay, a single one is
// MP_SingleProfileOverlay. Everything that is not drawing (the order of the members, the smallest window, the bodies that go on the wire, which field
// a refusal belongs under, the cards) is a plain function in the helper block below, which tests/ui/test_llm_profiles_helpers.py evaluates as written.

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

// The LLM Profiles page (design 7.1). The `profiles` entry of NV_PLAT_PAGES (nv-platform.jsx) hands each row to MP_profileCard and its list to MP_withWindows. The card is
// the one card view model of every Platform page (seam I-8): the desktop's PK_Card draws its badge, its icon and up to three facts, and the phone's fact sheet draws each fact
// as a [label, value] pair of strings. The icons are 14 by 14 paths: a chip for a single profile (one model on one provider), a link for an aggregated one (a chain of them).
var MP_ICON_SINGLE = "M4.5 4.5h5v5h-5Z M6 2v2.5 M8 2v2.5 M6 9.5V12 M8 9.5V12 M2 6h2.5 M2 8h2.5 M9.5 6H12 M9.5 8H12";
var MP_ICON_AGGREGATED = "M5.8 8.2 8.2 5.8 M6.6 3.9l.9-.9a2.4 2.4 0 0 1 3.4 3.4l-.9.9 M7.4 10.1l-.9.9a2.4 2.4 0 0 1-3.4-3.4l.9-.9";

// An aggregate's routing as its card says it. A row that names no strategy (or one this console does not know) routes the model's default way, sequentially.
function MP_routingLabel(strategy) {
  return strategy === "round_robin" ? "round robin" : "sequential fallback";
}

// Who uses a profile, from the two counts the list route adds (agent_count, graph_node_count): "unbound", or "bound by 2 agents · 1 graph node". A profile in use cannot be
// deleted (the server answers 409 in_use_by), so the card says so before anyone tries. A count that is not a whole number above 0 is none.
function MP_usageText(row) {
  var count = function (n) { return typeof n === "number" && n > 0 && n % 1 === 0 ? n : 0; };
  var agents = count(row && row.agent_count);
  var nodes = count(row && row.graph_node_count);
  if (!agents && !nodes) return "unbound";
  var text = "bound by " + agents + " agent" + (agents === 1 ? "" : "s");
  if (nodes > 0) text += " · " + nodes + " graph node" + (nodes === 1 ? "" : "s");
  return text;
}

// One profile's card. A single profile is "imported" (it comes from the Register provider overlay's import) and its type line is its provider and model; an aggregated one
// is "aggregated" and its type line says how many profiles it chains and how. The facts: the context window (an aggregate's is the smallest of its members', which
// MP_withWindows worked out from the loaded list into `_window`; "unknown" when it could not, never a guess), who uses it, and the harness that manages it when one does
// (such a row has no Delete: the entry's delPath answers null for it).
function MP_profileCard(row) {
  var single = MP_isSingle(row);
  var members = Array.isArray(row.members) ? row.members : [];
  var tokens = MP_fmtTokens(single ? row.context_length : row._window);
  var facts = [
    ["context window", tokens ? tokens + (single ? " tokens" : " tokens (smallest)") : "unknown"],
    ["usage", MP_usageText(row)],
  ];
  if (MP_isManaged(row)) facts.push(["managed by", String(row.harness_id)]);
  return {
    id: row.id,
    name: row.id,
    sub: single
      ? [row.provider_id, row.model_name].filter(Boolean).join(" · ")
      : members.length + " profile" + (members.length === 1 ? "" : "s") + " · " + MP_routingLabel(row.strategy),
    badge: single ? "imported" : "aggregated",
    icon: single ? { path: MP_ICON_SINGLE, tone: "var(--text-2)" } : { path: MP_ICON_AGGREGATED, tone: "var(--accent)" },
    facts: facts,
  };
}

// The page's list with each aggregate's context window worked out from the profiles in the same answer (MP_aggregateWindow): the server computes that minimum only when
// it resolves the profile and serves it on no route. A copy: the answer, its items and its single rows are not changed, and each aggregated item is a copy carrying
// `_window` (a number, or null when a member is not in the answer). A "_" key is the page's own, so an edit never sends it back (MP_editBody).
function MP_withWindows(resp) {
  if (!resp || !Array.isArray(resp.items)) return resp;
  var items = resp.items;
  var out = {};
  Object.keys(resp).forEach(function (key) { out[key] = resp[key]; });
  out.items = items.map(function (item) {
    if (MP_isSingle(item)) return item;
    var copy = {};
    Object.keys(item).forEach(function (key) { copy[key] = item[key]; });
    copy._window = MP_aggregateWindow(item.members || [], items);
    return copy;
  });
  return out;
}
// ---- end MP helpers ----

// The profile overlays. MP_ProfileModal is the component the Platform page's NV_ProfileModalHost mounts, and it is a dispatcher with no state and no hook: it
// decides which overlay a profile belongs to and mounts that one. Nothing to draw (open is false) draws nothing. A new profile is the aggregated overlay's
// create: "+ Aggregate" is the page's one create (design 7.1), and a single profile comes from the Register provider overlay's import. An aggregated
// profile is the aggregated overlay's edit, and a single one is the single-profile overlay. The overlay is keyed on the profile's id, so a different
// profile mounted in its place starts from its own fields and never inherits the first one's draft.
function MP_ProfileModal(props) {
  if (!props.open) return null;
  const existing = props.existing || null;
  if (existing && MP_isSingle(existing)) {
    return (
      <MP_SingleProfileOverlay key={existing.id} existing={existing} providers={props.providers || []}
        onClose={props.onClose} onSaved={props.onSaved} />
    );
  }
  return <MP_AggregateOverlay key={existing ? existing.id : "new"} existing={existing} onClose={props.onClose} onSaved={props.onSaved} />;
}

// A harness-managed profile is shown, not edited: its fields sit in a disabled fieldset, so every native control under it (the kit's checkboxes and segmented buttons too) is
// read-only without the kit growing a prop for it. The fieldset takes over the column layout of the overlay body it sits in.
const MP_READONLY_FIELDSET = { display: "flex", flexDirection: "column", gap: 16, minWidth: 0, margin: 0, padding: 0, border: "none" };

// The single profile's overlay (design 7.1: "opening a profile shows its overlay for viewing and editing the fields the model allows"): the id (fixed: the profile exists),
// the description, the provider and the model it names, and the context window. It is only ever an edit or, for a harness-managed row, a view: a single profile is created by
// the Register provider overlay's import, never here. A profile's `config` (reasoning, extended) is not drawn here: the Reasoning control returns in plan 03 Task 7, and until
// then `config` is kept as stored. Model calls apply it, so a save must not lose it: an edit sends the stored row back with these four fields laid over it (MP_editBody), and
// `config` and every other field the form does not draw round-trip untouched. The fields start from the profile the overlay is mounted with and then belong to the person
// typing: a new `providers` array (the host hands over a fresh empty one on every render until its list loads) must not wipe a draft.
function MP_SingleProfileOverlay({ existing, providers, onClose, onSaved }) {
  const managed = MP_isManaged(existing);
  const [description, setDescription] = React.useState(() => existing.description || "");
  const [provider, setProvider] = React.useState(() => existing.provider_id || "");
  const [modelName, setModelName] = React.useState(() => existing.model_name || "");
  const [contextText, setContextText] = React.useState(() => (existing.context_length != null ? String(existing.context_length) : ""));
  const [touched, setTouched] = React.useState({});
  const [saving, setSaving] = React.useState(false);
  const [refused, setRefused] = React.useState({ fields: {}, form: null });

  // Whether the overlay is still on screen. A save that is still out when it is dismissed (Cancel, Escape, the scrim, the host taking it away) answers into a screen that has
  // moved on, and onSaved and onClose are the HOST's: NV_ProfileModalHost clears its one modal, which by then may be a different one. A save that lands late closes nothing.
  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const descriptionError = description.trim() ? null : "Enter a description.";
  const modelError = modelName.trim() ? null : "Enter the model name.";
  const contextError = MP_contextError(contextText);
  const formReady = !managed && !descriptionError && !!provider && !modelError && !contextError;
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
      row = await api.apiFetch("PUT", "/model_profiles/" + encodeURIComponent(existing.id), MP_editBody(existing, draft));
    } catch (err) {
      setRefused(MP_refusalErrors(err, { description: "description", provider_id: "provider", model_name: "model", context_length: "context" }));
      setSaving(false);
      return;
    }
    setSaving(false);
    api.toastPush({ kind: "success", text: "Saved " + existing.id });
    // The row is saved, so the toast says so. A save that lands after the overlay was dismissed has nobody to report to, and onClose would close whatever the host shows by now.
    if (!mountedRef.current) return;
    if (onSaved) onSaved(row);
    onClose();
  };

  const fields = (
    <>
      <PK_Field label="Id">
        <input value={existing.id} readOnly autoComplete="off" spellCheck={false} data-testid="mp-single-id" />
      </PK_Field>
      <PK_Field label="Description" error={shown("description", descriptionError) || refused.fields.description}>
        <input value={description} data-testid="mp-single-description" onChange={typed("description", setDescription)} />
      </PK_Field>
      <PK_Grid cols={2}>
        <PK_Field label="Provider" error={refused.fields.provider || undefined}>
          <select value={provider} data-testid="mp-single-provider" onChange={typed("provider", setProvider)}>
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
      title={managed ? "LLM profile" : "Edit LLM profile"}
      testid="mp-single"
      onClose={onClose}
      footer={{
        left: <Btn data-testid="mp-single-cancel" onClick={onClose}>{managed ? "Close" : "Cancel"}</Btn>,
        right: managed ? null : (
          <Btn kind="primary" data-testid="mp-single-submit" disabled={!formReady} busy={saving} onClick={submit}>Save</Btn>
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
  // Whether the overlay is still on screen (see MP_SingleProfileOverlay): a save that lands after it was dismissed closes nothing.
  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
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
    // The row is saved, so the toast says so. A save that lands after the overlay was dismissed has nobody to report to, and onClose would close whatever the host shows by now.
    if (!mountedRef.current) return;
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
            {/* useResource keeps `error` until the answer and turns `loading` on for the retry, so the alert is still up while the request is out: the button says so (busy: aria-busy,
                aria-disabled, the click refused, the focus kept). */}
            <Btn kind="ghost" size="sm" data-testid="mp-aggregate-load-retry" busy={list.loading} onClick={() => list.refetch()}>
              {list.loading ? "Trying again…" : "Try again"}
            </Btn>
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

window.MP_ProfileModal = MP_ProfileModal;
window.MP_AggregateOverlay = MP_AggregateOverlay;
