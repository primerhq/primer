/* global React, Icon, Btn, Banner */
// ============================================================================
// One parameterized provider form (S4 P2 Task 19, P4 Task 29).
//
// Every provider class previously carried its own form component with its
// own field list. They are all the same shape: a provider-type picker, a
// few row-level fields, a config block, and limits. So the field list
// comes from the class's own /_types endpoint and exactly one component
// knows how to render it. No field table lives here: the place that owns
// the provider enums is the place that describes their fields.
//
// Capability hints render INSIDE this form using the existing
// foundation/capabilities.js helpers, which are deliberately untouched.
// ============================================================================

// Field name to input type. A served descriptor carries its own `type`;
// this is the fallback for the bare-name shape.
const PC_FIELD_TYPES = {
  "api_key": "password",
  "token": "password",
  "password": "password",
  "secret_access_key": "password",
  "max_concurrency": "number",
  "connect_timeout_seconds": "number",
  "request_timeout_seconds": "number",
  "max_retries": "number",
  "port": "number",
};

function PC_fieldType(name) {
  return PC_FIELD_TYPES[name] || "text";
}

// Designer reconciliation: "no raw snake_case labels where a human name
// is obvious" - the model-family classes' own /_types response already
// serves real labels ("Base URL", "API key (optional)", ...primer/api/
// routers/providers.py's _form_field calls), left untouched below; this
// is the fallback for the bare-string classes (web_search, web_fetch,
// speech - see the comment on PC_normalizeField) whose served "label" IS
// just the raw key, which PC_normalizeField's old `field.label || field.
// key` fallback rendered verbatim.
function PC_humanizeKey(key) {
  return String(key || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// _types answers in two shapes. web_search, web_fetch and the speech
// classes serve bare field NAMES (routers/web_search.py:216-222); the
// model-family classes serve DESCRIPTORS (routers/providers.py
// list_llm_provider_types). Normalise once so one renderer covers both.
function PC_normalizeField(field) {
  if (typeof field === "string") {
    return {
      key: field,
      label: PC_humanizeKey(field),
      type: PC_fieldType(field),
      required: false,
      help: "",
      options: null,
      placeholder: "",
    };
  }
  return {
    key: field.key,
    label: field.label || PC_humanizeKey(field.key),
    type: field.type || PC_fieldType(field.key),
    required: !!field.required,
    help: field.help || "",
    options: field.options || null,
    placeholder: field.placeholder || "",
  };
}

// EmbeddingProvider and CrossEncoderProvider declare models: min_length=1,
// so the row is rejected outright without one. A comma-joined text box
// would round-trip badly (names contain slashes and dots), so this is a
// real add/remove list of {name} objects, matching the wire shape.
function PC_ModelListField({ value, onChange }) {
  const rows = Array.isArray(value) ? value : [];
  const setName = (i, name) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, name } : r)));
  return (
    <div className="model-list" data-testid="provider-form-model-list">
      {rows.map((row, i) => (
        <div className="model-list-row" key={i}>
          <input
            type="text"
            className="mono"
            value={row.name || ""}
            onChange={(e) => setName(i, e.target.value)}
          />
          <Btn
            type="button"
            kind="ghost"
            onClick={() => onChange(rows.filter((_, j) => j !== i))}
          >
            Remove
          </Btn>
        </div>
      ))}
      <Btn
        type="button"
        data-testid="provider-form-add-model"
        onClick={() => onChange(rows.concat([{ name: "" }]))}
      >
        Add model
      </Btn>
    </div>
  );
}

function PC_Field({ field, value, onChange, disabled }) {
  let input;
  if (field.type === "model_list") {
    input = <PC_ModelListField value={value} onChange={onChange} />;
  } else if (field.type === "enum") {
    input = (
      <select value={value || ""} disabled={disabled}
        onChange={(e) => onChange(e.target.value)}>
        {(field.options || []).map((opt) => (
          <option key={opt} value={opt}>{opt}</option>
        ))}
      </select>
    );
  } else {
    input = (
      <input
        type={field.type}
        required={field.required}
        disabled={disabled}
        placeholder={field.placeholder}
        // 01a05198: on edit, a secret field's `value` is the served
        // MASK STRING verbatim (GET's own masked serialization - see
        // primer/model/common.py's _matches_served_mask) - unlike an
        // earlier iteration of this form, it is NOT blanked out. Left
        // untouched, the mask round-trips back on save exactly as
        // received; the backend's preserve_masked_secrets on_pre_update
        // hook recognizes that exact string and restores the real
        // credential instead of persisting the mask literally. Typing a
        // real replacement value submits that instead - it will not
        // match the mask pattern, so it passes through unchanged.
        value={value || ""}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  // Platform wave P1a item 4: secret fields (api_key/token/password/
  // secret_access_key - PC_fieldType above) get the reference's own label
  // pattern rather than an unlabeled password box. The value itself is
  // whatever is in `draft` already - on edit that is the served MASK
  // STRING verbatim (see the input's own comment above), so a masked
  // secret renders as "**********xxxx"-shaped text with no extra code
  // here: the mask IS the value, displayed the same as any other field.
  const isSecret = field.type === "password";
  return (
    <label className="field pc-field" data-field={field.key} data-locked={disabled ? "true" : "false"}>
      <span className="pc-field-label">
        <span className="pc-field-label-main">{field.label}</span>
        {isSecret ? (
          <span className="pc-field-label-annotation muted">secret — masked on read</span>
        ) : null}
        {field.required ? <em className="req"> *</em> : null}
      </span>
      {input}
      {field.help ? <span className="field-help">{field.help}</span> : null}
    </label>
  );
}

// Platform wave P1a item 4: "Live model probe" - a live POST against the
// CURRENT draft config (before Save), for kinds the served /_types data
// marks discoverable. Two REAL, confirmed backend endpoints exist for
// this shape (routers/providers.py): /llm_providers/_discover_models and
// /embedding_providers/_discover_models - other classes keep the existing
// generic runTest()/_test round trip below unchanged (PC_ProviderForm
// renders this panel ADDITIONALLY, not instead).
const PC_DISCOVER_MODELS_PLURALS = ["llm_providers", "embedding_providers"];

function PC_ProbePanel({ plural, draft, selectedType }) {
  const { apiFetch } = window.primerApi;
  const [busy, setBusy] = React.useState(false);
  const [result, setResult] = React.useState(null);

  const probe = async () => {
    setBusy(true);
    setResult(null);
    try {
      const out = await apiFetch("POST", `/${plural}/_discover_models`, {
        provider: selectedType,
        config: draft.config || {},
      });
      setResult({ ok: true, models: (out && out.models) || [] });
    } catch (err) {
      setResult({
        ok: false,
        error: (err && (err.detail || err.message)) || String(err),
      });
    } finally {
      setBusy(false);
    }
  };

  const models = (result && result.ok && result.models) || [];
  // Reference shows 5 named rows plus an overflow count for a 12-model
  // result; not a hard protocol limit, just the display cap.
  const shown = models.slice(0, 5);
  const overflow = models.length - shown.length;

  return (
    <div className="pc-probe" data-testid="provider-probe-panel">
      {/* Designer reconciliation: heading + compact green-outline Test
          connect on ONE row - kind="ghost" (transparent bg + border,
          the closest existing Btn variant to "outline") plus a scoped
          CSS override for the green tint, rather than a new Btn kind
          just for this one button. */}
      <div className="pc-probe-head-row">
        <div className="pc-probe-head">Live model probe</div>
        <Btn kind="ghost" size="sm" disabled={busy}
          data-testid="provider-probe-test" onClick={probe}>
          Test connect
        </Btn>
      </div>
      {result ? (
        result.ok ? (
          <div className="pc-probe-result" data-testid="provider-probe-result">
            <div className="pc-probe-count">
              {models.length} models · probed on the DRAFT config
            </div>
            <div className="pc-probe-models">
              {shown.map((m, i) => (
                <div key={i} className="pc-probe-model-row mono">{(m && m.name) || m}</div>
              ))}
            </div>
            {overflow > 0 ? (
              <div className="pc-probe-model-row muted text-sm">+ {overflow} more</div>
            ) : null}
          </div>
        ) : (
          <div className="field-help" data-testid="provider-probe-error">
            {result.error}
          </div>
        )
      ) : null}
    </div>
  );
}

// Lead ruling (designer reconciliation, Invalidate stop-and-flag): the
// card footer drops Invalidate to match the mockup exactly, but the
// capability itself is real and stays reachable - moved here, beside
// the panel above, as a deliberate capability-preserving addition the
// mockup itself doesn't show. Genuinely distinct from Test connect: that
// probes the DRAFT config pre-save, this drops the STORED adapter cache
// for the row that already exists (api/registries/provider_registry.py)
// - edit-mode only (nothing to invalidate before Save has ever run).
function PC_InvalidateAction({ plural, existingId }) {
  const { apiFetch } = window.primerApi;
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState("");

  const run = async () => {
    setErr("");
    setBusy(true);
    try {
      await apiFetch("POST", `/${plural}/${encodeURIComponent(existingId)}/invalidate`);
      const toast = window.primerApi && window.primerApi.toastPush;
      if (typeof toast === "function") {
        toast({ kind: "success", title: "Cache dropped", detail: existingId });
      }
    } catch (invalidateErr) {
      setErr((invalidateErr && (invalidateErr.detail || invalidateErr.message))
        || String(invalidateErr));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pc-invalidate" data-testid="provider-form-invalidate">
      <button type="button" className="pc-invalidate-link" disabled={busy}
        data-testid="provider-form-invalidate-action" onClick={run}>
        Invalidate model cache
      </button>
      {err ? (
        <span className="field-help" data-testid="provider-form-invalidate-error">{err}</span>
      ) : null}
    </div>
  );
}

// Platform wave P1a item 4: shared across every provider class's form
// modal (screenshots 5 and 7 both carry it verbatim) - explanatory copy
// about how /capabilities-driven install-wide defaults and aggregated
// failover chains work, not data fetched live from that endpoint.
const PC_CAPABILITIES_FOOTNOTE =
  "Speech, web-search and web-fetch families set the install-wide ACTIVE "
  + "config; aggregated kinds carry a failover chain.";

// Limits is required on every model-family provider row and
// max_concurrency inside it has no default, so the form has to offer it.
// The optional timeouts are left blank by default: an empty box means
// "use the model default", which is what the backend's None means.
// Designer reconciliation: the boxed <fieldset> dissolves into the same
// full-width, humanized-label fields every other field above/below it
// uses - a subtle group heading (not a border/legend box) is enough to
// keep the three fields visually together. Distinct labels for the two
// timeout fields (not a shared "Timeout (s)"): a provider row carries
// BOTH simultaneously (primer/model/providers/_shared.py), so one
// ambiguous label would leave an operator unable to tell which box sets
// which - the mockup's single "Timeout (s)" was for a kind (anthropic)
// whose real schema has no config-level timeout field at all, i.e. not
// a real precedent to generalize from (flagged in the PR body).
function PC_LimitsFieldset({ value, onChange }) {
  const limits = value || { max_concurrency: 1 };
  const set = (key, raw) => {
    const next = { ...limits };
    if (raw === "") delete next[key];
    else next[key] = Number(raw);
    onChange(next);
  };
  return (
    <div className="pc-limits-group" data-testid="provider-form-limits">
      <div className="pc-field-group-heading muted text-sm">Limits</div>
      <label className="field pc-field">
        <span className="pc-field-label">
          <span className="pc-field-label-main">Max concurrency</span>
          <em className="req"> *</em>
        </span>
        <input
          type="number"
          min="1"
          value={limits.max_concurrency == null ? 1 : limits.max_concurrency}
          onChange={(e) => set("max_concurrency", e.target.value || "1")}
        />
      </label>
      <label className="field pc-field">
        <span className="pc-field-label">
          <span className="pc-field-label-main">Request timeout (s)</span>
        </span>
        <input
          type="number"
          min="0"
          value={limits.request_timeout_seconds == null ? "" : limits.request_timeout_seconds}
          onChange={(e) => set("request_timeout_seconds", e.target.value)}
        />
      </label>
      <label className="field pc-field">
        <span className="pc-field-label">
          <span className="pc-field-label-main">Connect timeout (s)</span>
        </span>
        <input
          type="number"
          min="0"
          value={limits.connect_timeout_seconds == null ? "" : limits.connect_timeout_seconds}
          onChange={(e) => set("connect_timeout_seconds", e.target.value)}
        />
      </label>
    </div>
  );
}

// What the form shows is what it must send.
//
// PC_LimitsFieldset renders `value || { max_concurrency: 1 }`, a default
// that lives only in that component's render. An operator who fills the
// required fields and presses Save therefore sent no `limits` key at all,
// and every provider class declares limits as required: the create 422'd
// with "limits: Field required" while the form displayed a perfectly good
// max_concurrency of 1. Creating a provider through the catalog only
// worked if you happened to touch a Limits box first.
//
// Number inputs have the mirror problem. PC_Field stores e.target.value,
// which is a string, and an emptied box stores "". Both are rejected:
// "" fails float parsing, and a numeric string only survives because
// Pydantic coerces it. Send numbers as numbers and drop the empties.
//
// Text boxes have the same mirror problem (ticket T3, design 13.4): an
// emptied OPTIONAL box stored "" and was sent as "", which the server
// reads as a value (a base URL of "" is a 422, a key of "" is stored and
// masked on read). A row or config field the shape does not mark required
// is omitted when its value is empty after trimming; a REQUIRED box keeps
// its "" (Save is withheld for it by missingRequired, and the refusal
// names the field).
function PC_submittable(draft, shape, selectedType) {
  const out = { ...draft };
  // The type dropdown has the same fault limits had: `selectedType` falls
  // back to the first key for DISPLAY, and that fallback was never written
  // to the draft. Saving without touching the dropdown therefore sent no
  // `provider` at all, which is required on every class, so the create
  // 422'd while the form showed a type selected.
  if (selectedType && !out.provider) out.provider = selectedType;
  if (shape && shape.limits) {
    out.limits = { max_concurrency: 1, ...(draft.limits || {}) };
  }
  const clean = (obj, fields) => {
    if (!obj) return obj;
    const next = { ...obj };
    (fields || []).forEach((f) => {
      const norm = PC_normalizeField(f);
      if (norm.type === "number") {
        const raw = next[norm.key];
        if (raw === "" || raw == null) delete next[norm.key];
        else if (typeof raw === "string") next[norm.key] = Number(raw);
        return;
      }
      if (norm.required) return;
      const raw = next[norm.key];
      if (typeof raw === "string" && raw.trim() === "") delete next[norm.key];
    });
    return next;
  };
  const cleaned = clean(out, shape && shape.row_fields);
  cleaned.config = clean(out.config, shape && shape.config_fields);
  // The kinds that serve config fields require the `config` key itself,
  // even when every one of them is optional and untouched: a Hugging Face
  // embedding with the token box untouched posts config: {}, not no key
  // (no key is a 422 "body.config: Field required"; the cross-encoder
  // form has the same fault). A kind without config fields keeps sending
  // nothing.
  if (shape && shape.config_fields && shape.config_fields.length
    && (cleaned.config == null || Object.keys(cleaned.config).length === 0)) {
    cleaned.config = {};
  } else if (cleaned.config == null) {
    delete cleaned.config;
  }
  return cleaned;
}

// The web classes (web_search, web_fetch) name their kind
// `provider_type` on the wire and carry the same value as the
// `config.type` discriminant, but the form speaks `provider` everywhere:
// the dropdown, the _types key, PC_submittable's type fallback. A served
// row has provider_type and no provider, and the model validator matches
// provider_type against config.type, so the old console body
// {id, provider: "tavily", config: {api_key}} 422'd (design 13.4). The
// two edges convert: PC_draftFromRow seeds the draft from a served row,
// PC_wireBody names the wire body; the form internals never learn the
// server's names.
const PC_WEB_PLURALS = ["web_search_providers", "web_fetch_providers"];

function PC_draftFromRow(plural, row) {
  if (PC_WEB_PLURALS.includes(plural)) {
    return { ...row, provider: row.provider_type };
  }
  return { ...row };
}

function PC_wireBody(plural, body) {
  if (!PC_WEB_PLURALS.includes(plural)) return body;
  const { provider, ...rest } = body;
  return {
    ...rest,
    provider_type: provider,
    config: { ...(body.config || {}), type: provider },
  };
}

// The cache key of a class's GET /<plural>/_types. The Register menu (provider-catalog.jsx, PC_RegisterDropdown) and this form read the SAME URL, and used to read it under two keys, so a form
// opened from the menu fetched a second copy and had nothing until it arrived (board task 01a12350-792d). One key, built from the URL that is fetched: the form opened from the menu finds the answer the
// menu already has, and a form handed another `typesPath` does not share it.
function PC_typesKey(plural, typesPath) {
  return `provider-types:${typesPath || `/${plural}/_types`}`;
}

// The limits the schema form shows inside the Register overlay's frame: the three it has always shown, cut to the ones the class's adapters read
// (PC_limitFieldsFor, register-common.jsx). A wizard class's own pages show its full set.
const PC_FORM_LIMIT_FIELDS = ["max_concurrency", "request_timeout_seconds", "connect_timeout_seconds"];

const PC_SECRET_KEPT_HELP = "Stored secrets are shown masked; leave it as is to keep it.";

// Where the focus goes after a Try again that worked. The button the operator pressed is gone once the fields arrive, and a focus on a removed element falls to <body>, outside the dialog: it goes
// to the form's first field instead, unless the operator has already put it somewhere else. This is a component INSIDE the body, not an effect of the form, on purpose: inside the Register
// overlay the body is drawn by the shell's slots, in an update that runs AFTER the form's own effects (register-overlay.jsx, "the shell's slots"). At the form's effect time the OLD body (the
// Try again button and the alert) is still in the DOM and still focused, so an effect there saw a focus on a live button, did nothing, and the focus fell to <body> one task later. An effect of
// this component runs once the body it is in has been drawn, which is the first moment the focus can be read. `retried` is the form's ref (set by the press), `failed` its `typesFailed`, and
// `rootRef` the body's root element.
function PC_FocusAfterRetry({ retried, failed, rootRef }) {
  React.useEffect(() => {
    if (failed || !retried.current) return;
    retried.current = false;
    const root = rootRef.current;
    const held = document.activeElement;
    if (!root || (held && held !== document.body)) return;
    const first = root.querySelector("input:not([disabled]), select:not([disabled]), textarea:not([disabled])");
    if (first) first.focus();
  }, [failed]);
  return null;
}

// Two layouts, one form. Without `frame` the form lays itself out (fields, the probe panel, its own footer row): the legacy catalog's modal mounts it so.
// With `frame` (the Register provider overlay, plan 02 Task 10) it hands its parts to the frame and returns what the frame returns:
// frame({ body, footerLeft, footerRight, error, busy }), with Cancel on the left, Test (only when `canTest`: a class with a draft _test route; never to an edit) and the submit
// (`submitLabel`) on the right, and the save refusal as `error`. Inside the frame the fields are kit fields (the label alone on its line, the help under
// the control), `kindSelect` adds a Type select of the kinds the server serves minus the built-in ones and `excludeKinds` (locked when editing; a change
// writes `provider` and clears `config`), the limits are PC_LimitsSection's, and `readOnly` (a built-in row with nothing to configure) says so and offers
// no submit. `classKey` is the PC_CLASSES key the frame registers. `blocked` (the overlay's own request, a Delete, is out) holds Test and the submit off. The kit is used only in the framed branch.
function PC_ProviderForm({
  plural, typesPath, value, onChange, onSubmit, onTest, onCancel, editing,
  existingId, canInvalidate,
  frame, classKey, kindSelect, excludeKinds, canTest, submitLabel, readOnly, blocked,
}) {
  const { useResource, apiFetch, useCapabilities, capabilityHint,
    EXTRA_FOR_PROVIDER_TYPE } = window.primerApi;
  const [busy, setBusy] = React.useState(false);
  const [testResult, setTestResult] = React.useState(null);
  const [saving, setSaving] = React.useState(false);
  const [saveError, setSaveError] = React.useState(null);
  const live = React.useRef(true);
  React.useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const savingRef = React.useRef(false);
  const rootRef = React.useRef(null);
  const retried = React.useRef(false);
  const caps = (useCapabilities() || {}).data;
  const types = useResource(
    PC_typesKey(plural, typesPath),
    (signal) => apiFetch("GET", typesPath || `/${plural}/_types`, null, { signal }),
    { pollMs: null },
  );

  const draft = value || {};

  // A model row seeded by "Add model" starts blank, and submitting one
  // sends models: [{}], which the API rejects with 422
  // body.models.0.name: Field required. The per-class create modals
  // gated Create on every row being filled; the catalog's form replaced
  // them with no gate at all, so the same bad request went out again.
  // A required field that is empty is the same fault as a blank model
  // row: the form knows the request cannot succeed and offers Save
  // anyway, so the operator learns what was missing from a 422 instead
  // of from the form. The marker is already rendered beside the label;
  // this makes it mean something.
  const missingRequired = () => {
    const blank = (v) => v === undefined || v === null
      || String(v).trim() === "";
    if (blank(draft.id)) return true;
    const check = (fields, scope) => (fields || []).some((f) => {
      const norm = PC_normalizeField(f);
      if (!norm.required) return false;
      const holder = scope === "config" ? (draft.config || {}) : draft;
      return blank(holder[norm.key]);
    });
    return check(shape.row_fields, "row") || check(shape.config_fields, "config");
  };

  const modelRowsIncomplete = (fields) =>
    (fields || []).some((f) => {
      const norm = PC_normalizeField(f);
      if (norm.type !== "model_list") return false;
      const rows = Array.isArray(draft[norm.key]) ? draft[norm.key] : [];
      return rows.some((r) => !String((r && r.name) || "").trim());
    });
  // _types answers a MAP keyed by provider-type value.
  const typeMap = types.data || {};
  const typeKeys = Object.keys(typeMap);
  // The framed Type select offers the served kinds minus the built-in ones, so its default is the first of THOSE: the map's first key can be a built-in
  // kind (web search lists duckduckgo first), which is never a type to create.
  const servedKinds = frame && kindSelect && classKey && types.data
    ? window.PC_servedKinds(classKey, typeMap, excludeKinds) : null;
  const selectedType = draft.provider || (servedKinds ? servedKinds[0] : typeKeys[0]) || "";
  const shape = typeMap[selectedType] || {};
  // The fields of the kind come from _types. Until that has answered AND names this kind, `shape` is {}: no config fields (so no API key box), no Limits, nothing required, and a Save that sends a
  // request without what the server requires of the kind (an LLM provider without `limits` is a 422). The form does not offer Save or Test until the shape is known.
  const shapeReady = !!types.data && !!typeMap[selectedType];
  const typesFailed = !!types.error && !types.data;
  const loadingShape = !types.data && !types.error;
  const kindNotServed = !!types.data && !typeMap[selectedType];

  // 01a05198: no strip-on-edit effect needed any more - a secret field's
  // draft value on edit is simply whatever GET served (the mask
  // string), left as-is. Untouched, it submits verbatim and the
  // backend's preserve_masked_secrets on_pre_update hook restores the
  // real credential (primer/model/common.py); typed over, the new value
  // submits and passes through unchanged (it won't match the mask
  // pattern). This is what makes the required-field gate above safe to
  // read literally again: a masked value is never blank, so a
  // schema-required secret left untouched no longer looks "missing."

  // The provider type decides whether an optional extra is needed; the
  // hint text comes from the shared capabilities helper so the wording
  // stays identical to every other surface that gates on extras.
  const extra = EXTRA_FOR_PROVIDER_TYPE[selectedType];
  const missingExtra = extra && caps && caps.extras && !caps.extras[extra]?.installed;

  const setField = (scope, name, v) => {
    if (scope === "config") {
      onChange({ ...draft, config: { ...(draft.config || {}), [name]: v } });
      return;
    }
    onChange({ ...draft, [name]: v });
  };

  // A Save the server refuses is said here, in the words of the one reader (window.primerApi.readRefusal), and the click handler's promise never rejects: the catalog's save() throws what the
  // server answered, and a button ignores what its handler returns, so a refusal used to be an unhandled rejection with nothing on screen. The edit (PUT) path is this same form. While the request is out
  // Save is `aria-disabled` and `aria-busy`, NOT `disabled`: a focused button that turns disabled drops the focus to <body>, outside the dialog (the focus trap listens on the dialog), so the refusal
  // would leave Tab walking the page behind the scrim. The ref refuses a second submit the disabled attribute no longer does.
  const submit = async () => {
    if (typeof onSubmit !== "function" || savingRef.current) return;
    savingRef.current = true;
    setSaveError(null);
    setSaving(true);
    try {
      await onSubmit(PC_submittable(draft, shape, selectedType));
    } catch (err) {
      if (live.current) setSaveError(window.primerApi.readRefusal(err, String(err)).message);
    } finally {
      savingRef.current = false;
      if (live.current) setSaving(false);
    }
  };

  // The entry is SHARED with the Register menu (one request, not two), and useResource only copies an entry's state to whoever joins it: a menu request that FAILED reaches every form opened after it,
  // an edit form included, which would show the menu's old error with Save off and never fetch (nothing polls: pollMs is null). A form that mounts on a failed entry with nothing in flight asks once.
  React.useEffect(() => {
    if (types.error && !types.data && !types.loading && typeof types.refetch === "function") types.refetch();
  }, []);

  // Try again repeats the request for the kind's fields. useResource keeps `error` until the answer, so the retry in flight is `types.loading` with the alert still up: the button says it is trying
  // and stays focusable (aria-disabled, same reason as Save). When the answer arrives the button is gone, whatever the answer says (the fields, or a map that does not name the kind), and the focus it
  // had is lost to <body>: PC_FocusAfterRetry, rendered in the body, hands it to the form's first field (an effect of this form could not: see there).
  const retryTypes = () => {
    if (types.loading || typeof types.refetch !== "function") return;
    retried.current = true;
    types.refetch();
  };

  // The _test routes read the wire names: the web classes' draft is {id, provider_type, config: {type, ...}} (routers/web_search.py _ProviderDraft), so the
  // form's body goes through PC_wireBody as a save does (it passed every other class through unchanged; without it a web Test answered 422).
  const runTest = async () => {
    setBusy(true);
    try {
      const out = typeof onTest === "function"
        ? await onTest(PC_submittable(draft, shape, selectedType))
        : await apiFetch("POST", `/${plural}/_test`,
                         PC_wireBody(plural, PC_submittable(draft, shape, selectedType)));
      setTestResult(out);
    } catch (err) {
      setTestResult({ ok: false, error: err?.detail || err?.message || String(err) });
    } finally {
      setBusy(false);
    }
  };

  // ---- the framed layout (the Register provider overlay) ----------------------------------------------------------------------------------------------
  if (typeof frame === "function") {
    const PK_Grid = window.PK_Grid;
    const PK_Field = window.PK_Field;
    const PK_RowTable = window.PK_RowTable;
    const kindLocked = !!editing;
    // A locked select names the row's own kind, even a built-in one that is never offered to create.
    const kindOptions = kindLocked ? (selectedType ? [selectedType] : []) : (servedKinds || []);
    // The id rule holds for a NEW id; an existing row's id is not the form's to judge.
    const idProblem = editing ? null : window.PC_idProblem(draft.id);
    const idTyped = draft.id != null && String(draft.id) !== "";
    const limitFields = classKey
      ? window.PC_limitFieldsFor(classKey, selectedType).filter((f) => PC_FORM_LIMIT_FIELDS.indexOf(f) >= 0)
      : PC_FORM_LIMIT_FIELDS;
    const limitsValue = draft.limits || window.PC_initialLimits(limitFields);
    const limitProblems = shape.limits ? window.PC_limitsProblems(limitFields, limitsValue) : {};
    // Whether the submit can be pressed at all; the request in flight is said on the button itself (busy), never folded in here.
    const formReady = shapeReady && !readOnly && !idProblem && !missingRequired()
      && !modelRowsIncomplete(shape.row_fields) && Object.keys(limitProblems).length === 0;

    const fieldOf = (scope, f) => {
      const holder = scope === "config" ? (draft.config || {}) : draft;
      const v = holder[f.key];
      const testid = "pc-field:" + f.key;
      const set = (next) => setField(scope, f.key, next);
      if (f.type === "model_list") {
        return (
          <PK_RowTable key={scope + ":" + f.key} title={String(f.label || f.key).toUpperCase()} addLabel="+ Add" testid={testid}
            columns={[{ key: "name", label: "Model", input: "text" }]} rows={Array.isArray(v) ? v : []} onChange={set} />
        );
      }
      let control;
      if (f.type === "enum") {
        // An optional choice the draft has not made reads "Not set": showing the first option while sending nothing would show a value it never sends.
        control = (
          <select data-testid={testid} value={v == null ? "" : String(v)} aria-required={f.required ? "true" : undefined}
            onChange={(e) => set(e.target.value)}>
            {f.required ? null : <option value="">Not set</option>}
            {(f.options || []).map((opt) => (
              <option key={opt} value={opt}>{(f.key === "flavor" && window.PC_FLAVOR_LABELS[opt]) || opt}</option>
            ))}
          </select>
        );
      } else {
        control = (
          <input type={f.type === "password" || f.type === "number" || f.type === "url" ? f.type : "text"} data-testid={testid}
            value={v == null ? "" : String(v)} placeholder={f.placeholder || undefined} aria-required={f.required ? "true" : undefined}
            autoComplete={f.type === "password" ? "off" : undefined} onChange={(e) => set(e.target.value)} />
        );
      }
      // An edit serves a stored secret masked; left as it is, the server keeps the secret (preserve_masked_secrets, primer/model/common.py).
      const help = f.type === "password" && editing ? [f.help, PC_SECRET_KEPT_HELP].filter(Boolean).join(" ") : f.help;
      return (
        <PK_Field key={scope + ":" + f.key} label={f.label} help={help || undefined} required={f.required}
          span={f.type === "url" ? "full" : undefined}>
          {control}
        </PK_Field>
      );
    };

    const rowFields = (shape.row_fields || []).map(PC_normalizeField);
    const configFields = (shape.config_fields || []).map(PC_normalizeField);
    const listed = testResult && testResult.ok
      ? (testResult.models || testResult.voices || []).map((m) => (typeof m === "string" ? m : m && m.name)).filter(Boolean)
      : [];
    const framedBody = (
      <div className="col" style={{ gap: 16, minWidth: 0 }} ref={rootRef} data-testid={`provider-form-${plural}`}>
        <PC_FocusAfterRetry retried={retried} failed={typesFailed} rootRef={rootRef} />
        {missingExtra ? (
          <Banner kind="info" title="Optional dependency required">
            {capabilityHint(extra)}
          </Banner>
        ) : null}
        <PK_Grid cols={2}>
          <window.PC_IdField value={draft.id} locked={kindLocked} testid="pc-field:id" problem={idTyped ? idProblem : null}
            onChange={(next) => setField("row", "id", next)} />
          {kindSelect ? (
            <PK_Field label="Type">
              <select data-testid="pc-field:type" value={selectedType} disabled={kindLocked || !kindOptions.length}
                onChange={(e) => onChange({ ...draft, provider: e.target.value, config: {} })}>
                {kindOptions.map((k) => (
                  <option key={k} value={k}>{window.PC_kindLabel(classKey, k, typeMap[k])}</option>
                ))}
              </select>
            </PK_Field>
          ) : null}
        </PK_Grid>
        {loadingShape ? (
          <div className="muted text-sm" role="status" data-testid="provider-form-loading">
            Loading the fields for this kind…
          </div>
        ) : null}
        {typesFailed ? (
          <div className="nv-form-error" role="alert" data-testid="provider-form-types-error">
            Could not load the fields for this kind: {window.primerApi.readRefusal(types.error, String(types.error)).message}
          </div>
        ) : null}
        {typesFailed ? (
          <div>
            <Btn kind="ghost" data-testid="provider-form-types-retry" onClick={retryTypes} busy={!!types.loading}>
              {types.loading ? "Trying again…" : "Try again"}
            </Btn>
          </div>
        ) : null}
        {kindNotServed ? (
          <div className="nv-form-error" role="alert" data-testid="provider-form-kind-missing">
            {selectedType
              ? `This install does not serve the kind "${selectedType}", so the form cannot show its fields.`
              : "This install serves no kind for this class, so the form cannot show its fields."}
          </div>
        ) : null}
        {readOnly ? (
          <div className="muted" data-testid="provider-form-readonly">Built in; nothing to configure.</div>
        ) : (
          <React.Fragment>
            {rowFields.length || configFields.length ? (
              <PK_Grid cols={2}>
                {rowFields.map((f) => fieldOf("row", f))}
                {configFields.map((f) => fieldOf("config", f))}
              </PK_Grid>
            ) : null}
            {shape.limits && limitFields.length ? (
              <window.PC_LimitsSection fields={limitFields} value={limitsValue} problems={limitProblems} testid="pc-limits"
                classKey={classKey} onChange={(next) => setField("row", "limits", next)} />
            ) : null}
          </React.Fragment>
        )}
        {testResult ? (
          <div className={testResult.ok ? "field-help" : "nv-form-error"} role={testResult.ok ? "status" : "alert"}
            data-testid="provider-form-test-result">
            {testResult.ok
              ? "Reachable" + (listed.length ? ": " + listed.join(", ") : "")
              : "Test failed: " + String(testResult.error || "no reason given")}
          </div>
        ) : null}
      </div>
    );
    const footerLeft = typeof onCancel === "function" ? (
      <Btn kind="ghost" data-testid="provider-form-cancel" onClick={onCancel}>Cancel</Btn>
    ) : null;
    // An edit never offers a draft Test, whatever `canTest` says: the draft holds the served MASK of every secret and `_test` builds its adapter from the draft alone, so
    // it would report a failure for a provider that works (plan 02: "No masked secret reaches a probe. Edit mode never sends a draft to ... `_test`").
    const footerRight = (
      <React.Fragment>
        {canTest && !editing ? (
          <Btn kind="ghost" data-testid="provider-form-test" onClick={runTest} busy={busy} blocked={saving || !!blocked} disabled={!shapeReady}>
            Test
          </Btn>
        ) : null}
        {readOnly ? null : (
          <Btn kind="primary" data-testid="provider-form-save" onClick={submit} busy={saving} blocked={busy || !!blocked} disabled={!formReady}>
            {submitLabel || "Save"}
          </Btn>
        )}
      </React.Fragment>
    );
    // `busy`: a request of the form's own is out (a save or a test), so the frame can keep the page from being switched away under it.
    return frame({ body: framedBody, footerLeft, footerRight, error: saveError, busy: saving || busy });
  }

  // Platform wave P1a item 4: the rich "Live model probe" panel is
  // ADDITIONAL, not a replacement for the generic Test button below -
  // gated on the served /_types discoverable flag for THIS kind (not a
  // hardcoded kind list) and on a confirmed real endpoint existing for
  // this class (see PC_DISCOVER_MODELS_PLURALS's own comment).
  const showProbePanel = !!shape.discoverable
    && PC_DISCOVER_MODELS_PLURALS.indexOf(plural) >= 0;

  // Designer reconciliation: the in-form kind dropdown (and the T0379
  // mismatch warning it existed to explain) is REMOVED - kind arrives
  // preselected from the catalog's own kind-listing Register dropdown
  // (create) or is simply the row's own real value (edit), so
  // `selectedType`/`shape` above are never actually driven by a control
  // in this form any more, only read from `draft.provider`. The T0379
  // mismatch class this warning existed for mostly evaporates once kind
  // can no longer be picked independently of the fields shown for it -
  // flagged in the PR body per the task's own instruction.
  const fields = (
    <div className="col" style={{ gap: 12, minWidth: 0 }}>
      {missingExtra ? (
        <Banner kind="info" title="Optional dependency required">
          {capabilityHint(extra)}
        </Banner>
      ) : null}

      <PC_Field
        field={PC_normalizeField({
          key: "id", label: "Name", required: true,
          placeholder: selectedType ? `e.g. ${selectedType}-prod` : "e.g. my-provider",
        })}
        value={draft.id}
        disabled={editing}
        onChange={(next) => setField("row", "id", next)}
      />

      {loadingShape ? (
        <div className="muted text-sm" role="status" data-testid="provider-form-loading">
          Loading the fields for this kind…
        </div>
      ) : null}
      {typesFailed ? (
        <div className="nv-form-error" role="alert" data-testid="provider-form-types-error">
          Could not load the fields for this kind: {window.primerApi.readRefusal(types.error, String(types.error)).message}
        </div>
      ) : null}
      {typesFailed ? (
        <div>
          <Btn kind="ghost" data-testid="provider-form-types-retry" onClick={retryTypes}
            aria-busy={types.loading ? "true" : undefined} aria-disabled={types.loading ? "true" : undefined}>
            {types.loading ? "Trying again\u2026" : "Try again"}
          </Btn>
        </div>
      ) : null}
      {kindNotServed ? (
        <div className="nv-form-error" role="alert" data-testid="provider-form-kind-missing">
          {selectedType
            ? `This install does not serve the kind "${selectedType}", so the form cannot show its fields.`
            : "This install serves no kind for this class, so the form cannot show its fields."}
        </div>
      ) : null}

      {(shape.row_fields || []).map(PC_normalizeField).map((f) => (
        <PC_Field
          key={`row:${f.key}`}
          field={f}
          value={draft[f.key]}
          onChange={(next) => setField("row", f.key, next)}
        />
      ))}

      {(shape.config_fields || []).map(PC_normalizeField).map((f) => (
        <PC_Field
          key={`config:${f.key}`}
          field={f}
          value={(draft.config || {})[f.key]}
          onChange={(next) => setField("config", f.key, next)}
        />
      ))}

      {shape.limits && (
        <PC_LimitsFieldset
          value={draft.limits}
          onChange={(next) => setField("row", "limits", next)}
        />
      )}

      {testResult ? (
        <Banner
          kind={testResult.ok ? "success" : "error"}
          title={testResult.ok ? "Reachable" : "Test failed"}
        >
          {testResult.ok
            ? (testResult.models || testResult.voices || []).join(", ")
            : testResult.error}
        </Banner>
      ) : null}

      {saveError ? (
        <div className="nv-form-error" role="alert" data-testid="provider-form-save-error">
          {saveError}
        </div>
      ) : null}
    </div>
  );

  // Invalidate is edit-mode-only (nothing to invalidate before Save has
  // ever run) and class-gated (canInvalidate, from the catalog's own
  // klass.invalidate flag - llm/embedding/cross_encoder only). It needs
  // a right column even for a kind/class with no probe panel (cross_
  // encoder has no discover_models route at all, so showProbePanel is
  // always false for it, but it DOES carry klass.invalidate) - the two
  // gates are independent, so the column renders whenever EITHER wants it.
  const showInvalidate = editing && canInvalidate && !!existingId;
  const showRightColumn = showProbePanel || showInvalidate;

  return (
    <div className="col" style={{ gap: 12 }} ref={rootRef}
      data-testid={`provider-form-${plural}`}>
      <PC_FocusAfterRetry retried={retried} failed={typesFailed} rootRef={rootRef} />
      {/* 01a04d6a: was a flex row (fields flex:1 vs. the probe panel's
          fixed 240px) inside the default 420px Modal - the fields
          column computed to a sliver once the probe panel's width and
          the 20px gap were subtracted (S3 screenshot). A real grid with
          an explicit fields column, sized against the WIDER modal this
          form now opens in (ProviderCatalog passes width={720}), fixes
          both the immediate squeeze and any future modal-width drift -
          the column proportions are declared here, not fought over with
          a competing fixed-width sibling. minmax(0, 1fr) - not the bare
          1fr shorthand - so a long unbreakable value (a pasted URL/key)
          cannot blow the fields column past its share (grid's default
          auto minimum forces content-based sizing without it). */}
      <div className="pc-form-grid" style={{
        display: "grid",
        gridTemplateColumns: showRightColumn ? "minmax(0, 1fr) 240px" : "minmax(0, 1fr)",
        gap: 20, alignItems: "start",
      }}>
        {fields}
        {showRightColumn ? (
          <div className="col pc-form-right" style={{ gap: 10, minWidth: 0 }}>
            {showProbePanel ? (
              <React.Fragment>
                <PC_ProbePanel plural={plural} draft={draft} selectedType={selectedType} />
                <div className="field-help" data-testid="provider-form-capabilities-footnote">
                  {PC_CAPABILITIES_FOOTNOTE}
                </div>
              </React.Fragment>
            ) : null}
            {showInvalidate ? (
              <PC_InvalidateAction plural={plural} existingId={existingId} />
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
        {typeof onCancel === "function" ? (
          <Btn kind="ghost" data-testid="provider-form-cancel" onClick={onCancel}>
            Cancel
          </Btn>
        ) : null}
        {/* Designer reconciliation: the footer Test button is redundant
            ONLY where the probe panel already offers Test connect - for
            classes with no probe panel (no discover_models route: stt/
            tts/web_search/web_fetch/artifact_storage/cross_encoder),
            removing it would leave NO way at all to test a draft config
            before saving, which the task's own tripwire says to flag
            rather than drop. Kept, conditionally, for exactly those. */}
        {!showProbePanel ? (
          <Btn
            kind="ghost"
            data-testid="provider-form-test"
            onClick={runTest}
            disabled={busy || saving || !shapeReady}
          >
            Test
          </Btn>
        ) : null}
        <Btn data-testid="provider-form-save"
          onClick={submit}
          aria-busy={saving ? "true" : undefined}
          aria-disabled={saving ? "true" : undefined}
          disabled={!shapeReady || busy || missingRequired()
            || modelRowsIncomplete(shape.row_fields)}>
          Save provider
        </Btn>
      </div>
    </div>
  );
}

window.PC_FIELD_TYPES = PC_FIELD_TYPES;
window.PC_normalizeField = PC_normalizeField;
window.PC_submittable = PC_submittable;
window.PC_draftFromRow = PC_draftFromRow;
window.PC_wireBody = PC_wireBody;
window.PC_typesKey = PC_typesKey;
// The Register overlay shows it under an edit of a class whose registry caches adapters (plan 02 Task 11).
window.PC_InvalidateAction = PC_InvalidateAction;
window.PC_ProviderForm = PC_ProviderForm;
