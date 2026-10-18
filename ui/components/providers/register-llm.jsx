/* global React, window, Btn */
// register-llm.jsx - the LLM pages of the Register provider overlay (plan 02 Tasks 12 and 13; design 6.3, 6.4, 6.9 and 13.3).
//
// An LLM provider is registered in two pages, and its models are brought in as LLM profiles:
//
//   page 1  the connection: id, type, flavor, URL, API key (whatever the kind serves) and the limits. "Next" sends the draft to
//           POST /llm_providers/_discover_models. A list goes to page 2; a refusal or an empty list stays here, says why, and offers "Register without
//           models" so a server that cannot list its models can still be registered (design 6.3).
//   page 2  the models found, one row each, none ticked, with the context window the provider reported (or a box to fill in). "Import N models"
//           creates the provider once, then one LLM profile per ticked model, one after another (there is no bulk route, design 13.3). A row that answers
//           409 is "already imported"; any other refusal marks that row failed, the overlay stays open, and "Retry failed" posts only those rows. The
//           provider is never posted twice.
//
// Editing a provider (design 6.9) is page 1 with the id and the type locked and "Save". The draft is the WHOLE served row, so every field the page does not show
// goes back unchanged. "Import more models" runs page 2 against the SAVED provider (GET .../discovered_models reads the stored secret server-side) and lists only
// the models that have no profile yet. An edit never sends its draft to a probe: the draft holds the MASK of every secret, and a probe would authenticate with it.
//
// The page is a page of the Register overlay (register-overlay.jsx, "the shell's slots"): it is handed { klass, editRow, excludeKinds, frame, blocked, onSaved,
// onClose }, it ALWAYS returns frame({ body, footerLeft, footerRight, error, busy }), and it holds its actions off while the overlay's own request, a Delete, is out
// (`blocked`). Two more consequences of that contract are built in:
// - The focus. The button that took the page forward (Next) or back (Back) is gone once the other page is drawn, and a focus on a removed element falls to <body>,
//   outside the dialog. The page's effects run BEFORE its new body is in the DOM, so the hand-off is a small component INSIDE the body (PC_LlmFocus), whose effect
//   runs once the body it is in has been drawn; it moves the focus only when the focus has been lost (an operator who put it somewhere keeps it).
// - React context. The body is drawn beside the page, not under it, so nothing is handed to it through context: it gets one props object.

const PC_LLM_LABELS = { flavor: "Flavor", url: "URL", api_key: "API key", app_name: "App name", app_url: "App URL" };
const PC_LLM_KEPT_HELP = "Stored secrets are shown masked; leave it as is to keep it.";
const PC_LLM_LOCAL_HELP = "Optional for local servers";
const PC_LLM_NO_MODELS = "The provider answered but listed no models.";
const PC_LLM_UNSAVED_HELP = "Save your changes first";
const PC_LLM_IMPORT_HELP = "The context window is filled in from the provider when it reports one; you can change it. "
  + "Each ticked model becomes one LLM profile (named <provider>--<model>, editable later).";

// "1 model", "3 models": the words of a count.
function PC_llmCount(n, one, many) {
  return n + " " + (n === 1 ? one : many);
}

function PC_llmBlank(v) {
  return v === undefined || v === null || String(v).trim() === "";
}

// Whether two drafts hold the same values (deeply): the page's "unsaved changes". A value typed and typed back is no change.
function PC_llmSame(a, b) {
  if (a === b) return true;
  if (a == null || b == null || typeof a !== "object" || typeof b !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && PC_llmSame(a[k], b[k]));
}

// ---- the draft -------------------------------------------------------------------------------------------------------------------------------------------

// What a new provider of a kind starts with in `config`: the flavor the model defaults to (OTHER), so that the Flavor box shows a value and the same value is sent. A
// page that showed the first option while sending nothing would be showing what it does not send.
function PC_llmStartConfig(configFields) {
  const out = {};
  (configFields || []).forEach((f) => {
    if (f.key !== "flavor" || !f.options || !f.options.length) return;
    out.flavor = f.options.indexOf("other") >= 0 ? "other" : f.options[0];
  });
  return out;
}

// A new provider's draft with its defaults written in: the kind (the Type select shows the first served kind until one is picked), the config the kind starts from and
// the limits a new provider starts from. Everything the page shows, and everything it sends, is read from this, so the two cannot differ. An EDIT is never filled in: its
// draft is the served row, and a field the page does not show is not the page's to add.
function PC_llmFill(draft, kind, configFields, limitFields) {
  return {
    ...draft,
    provider: kind,
    config: { ...PC_llmStartConfig(configFields), ...(draft.config || {}) },
    limits: draft.limits || window.PC_initialLimits(limitFields),
  };
}

// The body of POST /llm_providers and PUT /llm_providers/{id}: the draft as the form's wire rules make it (ticket T3: a config or row field the kind does not mark
// required and whose text is empty after trimming is omitted, never sent as ""; numbers are numbers; `limits` is always there) with every other field of the draft, the
// ones the page does not show included, left as they are. `shape` is the kind's own `_types` entry; a caller without it still gets no empty text in `config`.
function PC_llmBody(draft, shape) {
  const body = window.PC_submittable(draft, shape || null, draft.provider);
  if (!shape && body.config) {
    const config = {};
    Object.keys(body.config).forEach((k) => {
      const v = body.config[k];
      if (typeof v === "string" && v.trim() === "") return;
      config[k] = v;
    });
    body.config = config;
  }
  return body;
}

// ---- the models ------------------------------------------------------------------------------------------------------------------------------------------

// The entries of a discovery answer ({ models: [{ name, context_length? }] }), each name once, in the server's order.
function PC_llmListed(answer) {
  const seen = new Set();
  const out = [];
  ((answer && answer.models) || []).forEach((m) => {
    const entry = typeof m === "string" ? { name: m } : m;
    const name = entry && entry.name != null ? String(entry.name) : "";
    if (!name || seen.has(name)) return;
    seen.add(name);
    out.push({ ...entry, name });
  });
  return out;
}

// What is wrong with a context window a person typed, or null: a whole number of 1 or more.
function PC_llmContextProblem(text) {
  const s = text == null ? "" : String(text).trim();
  if (s === "") return "Required";
  if (!/^\d+$/.test(s) || Number(s) < 1 || Number(s) > Number.MAX_SAFE_INTEGER) return "Enter a whole number of 1 or more";
  return null;
}

// The import checklist's rows: none ticked (design 6.4), the context window the provider reported when it did (Anthropic and real OpenAI report none), and the models
// no profile id can be made from marked "invalid". Two names can make ONE id ("a/b" and "a-b"): the second would answer 409 and be read as already imported, which it
// is not, so it is marked too. The id is the provider's part joined to the model's part (PC_profileIdFor), so it is the model's part that decides.
function PC_llmRows(models) {
  const seen = new Map();
  return (models || []).map((m) => {
    const name = String(m.name);
    const part = window.PC_idPart(name);
    const row = { name, ticked: false, context: Number.isInteger(m.context_length) && m.context_length > 0 ? String(m.context_length) : "", status: "pending" };
    if (!part) {
      row.status = "invalid";
      row.error = "No id can be made from this name";
    } else if (seen.has(part)) {
      row.status = "invalid";
      row.error = "Its id would be the same as the one for " + seen.get(part);
    } else {
      seen.set(part, name);
    }
    return row;
  });
}

// What a row says about itself (the status cell), and in which tone.
function PC_llmRowStatus(row) {
  if (row.status === "imported") return { text: "imported", tone: "ok" };
  if (row.status === "already") return { text: "already imported", tone: "ok" };
  if (row.status === "failed") return { text: "failed: " + (row.error || "no reason given"), tone: "error" };
  if (row.status === "invalid") return { text: row.error || "No id can be made from this name", tone: "error" };
  if (row.ticked) {
    const problem = PC_llmContextProblem(row.context);
    if (problem) return { text: problem, tone: "error" };
  }
  return { text: "", tone: "" };
}

// A row whose profile exists: nothing more is asked of it.
function PC_llmDone(row) {
  return row.status === "imported" || row.status === "already";
}

// Create the LLM profile of every ticked row that is not done yet, ONE AFTER ANOTHER (there is no bulk route: design 13.3), and answer the rows with their results:
// "imported" for a 2xx, "already" for a 409 (the profile is there; not a failure), "failed" with the refusal's words for anything else. A row that is not ticked, is done
// or cannot have an id is left as it was, so a retry posts only what failed. `onProgress(rows)` hears each row as it is answered; `shouldStop()` is asked before each
// post (the overlay was closed: the rows after it stay as they were, and "Import more models" will find them).
async function PC_importProfiles({ apiFetch, providerId, rows, onProgress, shouldStop }) {
  const readRefusal = window.primerApi.readRefusal;
  let current = (rows || []).slice();
  for (let i = 0; i < current.length; i++) {
    const row = current[i];
    if (!row.ticked || PC_llmDone(row) || row.status === "invalid") continue;
    if (typeof shouldStop === "function" && shouldStop()) break;
    const id = window.PC_profileIdFor(providerId, row.name);
    const body = {
      provider_id: providerId,
      model_name: row.name,
      context_length: Number(row.context),
      description: "Imported from " + providerId,
    };
    if (id) body.id = id;
    const rest = { ...row };
    delete rest.error;
    let next;
    try {
      await apiFetch("POST", "/model_profiles", body);
      next = { ...rest, status: "imported" };
    } catch (err) {
      next = err && err.status === 409
        ? { ...rest, status: "already" }
        : { ...rest, status: "failed", error: readRefusal(err, String(err)).message };
    }
    current = current.map((r, j) => (j === i ? next : r));
    if (typeof onProgress === "function") onProgress(current);
  }
  return current;
}

// ---- the focus -------------------------------------------------------------------------------------------------------------------------------------------

// Hands the focus to something in the body once the body has been drawn, when the focus has been lost. `n` changes when a hand-off is wanted (a page was left for
// the other, a Try again worked); `to` says where: "first" is the page's first field, "head" the heading of the models page. A focus the operator put somewhere is left
// where it is. The unit harness has no DOM and no focus: the hand-off's rule is exercised there with a document of its own, and tests/ui_e2e/test_register_llm_focus_journey.py drives it in a browser.
function PC_LlmFocus({ n, to, rootRef }) {
  React.useEffect(() => {
    if (!n) return;
    const root = rootRef.current;
    if (!root) return;
    const held = document.activeElement;
    if (held && held !== document.body) return;
    const target = to === "head"
      ? root.querySelector("[data-pc-focus]")
      : root.querySelector("input:not([disabled]):not([readonly]), select:not([disabled]), textarea:not([disabled])");
    if (target) target.focus();
  }, [n]);
  return null;
}

// ---- page 1 ----------------------------------------------------------------------------------------------------------------------------------------------

// One served config field of the kind as a kit field. `s` is the page's view (the body is drawn beside the page, so it is handed everything in props).
function PC_llmField(s, f) {
  const PK_Field = window.PK_Field;
  const value = (s.eff.config || {})[f.key];
  const testid = "pc-field:" + f.key;
  const set = (next) => s.setField("config", f.key, next);
  let control;
  if (f.type === "enum") {
    // A choice already made is never taken back: "Not set" is only offered while there is none.
    control = (
      <select data-testid={testid} value={value == null ? "" : String(value)} aria-required={f.required ? "true" : undefined}
        onChange={(e) => set(e.target.value)}>
        {f.required || value ? null : <option value="">Not set</option>}
        {(f.options || []).map((opt) => (
          <option key={opt} value={opt}>{(f.key === "flavor" && window.PC_FLAVOR_LABELS[opt]) || opt}</option>
        ))}
      </select>
    );
  } else {
    control = (
      <input type={f.type === "password" || f.type === "url" || f.type === "number" ? f.type : "text"} data-testid={testid}
        value={value == null ? "" : String(value)} placeholder={f.placeholder || undefined} aria-required={f.required ? "true" : undefined}
        autoComplete={f.type === "password" ? "off" : undefined} spellCheck={f.type === "password" || f.type === "url" ? false : undefined}
        onChange={(e) => set(e.target.value)} />
    );
  }
  // The key's help: a kind with a URL is a server that may run open ("Optional for local servers"); a hosted kind keeps the words the server serves for it (the real
  // Anthropic and Gemini APIs need a key); a key the kind requires needs no such word. An edit says that a masked secret is kept when it is left as it is.
  let help = f.help;
  if (f.key === "api_key" && !f.required && s.hasUrl) help = PC_LLM_LOCAL_HELP;
  if (f.type === "password" && s.editing) help = [help, PC_LLM_KEPT_HELP].filter(Boolean).join(" ");
  return (
    <PK_Field key={f.key} label={PC_LLM_LABELS[f.key] || f.label} help={help || undefined} required={f.required}
      span={f.key === "url" || f.key === "api_key" ? "full" : undefined}>
      {control}
    </PK_Field>
  );
}

// The connection page. Id, Type and (for the OpenAI kinds) Flavor share a row; then the URL and the key, each on a row of its own, any other field the kind serves two to a
// row, and the limits box.
function PC_LlmSettings({ s }) {
  const PK_Grid = window.PK_Grid;
  const PK_Field = window.PK_Field;
  const flavor = s.configFields.filter((f) => f.key === "flavor")[0] || null;
  const wide = s.configFields.filter((f) => f.key === "url" || f.key === "api_key");
  const others = s.configFields.filter((f) => f.key !== "flavor" && f.key !== "url" && f.key !== "api_key");
  // A locked select names the row's own kind, even one this install no longer serves (the page then says so).
  const kindOptions = s.editing ? (s.kind ? [s.kind] : []) : s.kinds;
  return (
    <div className="col" style={{ gap: 16, minWidth: 0 }} ref={s.rootRef} data-testid="pc-llm-settings">
      <PC_LlmFocus n={s.handoff.n} to={s.handoff.to} rootRef={s.rootRef} />
      <PK_Grid cols={flavor ? 3 : 2}>
        <window.PC_IdField value={s.eff.id} locked={s.editing} testid="pc-field:id" problem={s.idTyped ? s.idProblem : null}
          onChange={(next) => s.setField("row", "id", next)} />
        <PK_Field label="Type">
          <select data-testid="pc-field:type" value={s.kind} disabled={s.editing || !kindOptions.length} onChange={(e) => s.setKind(e.target.value)}>
            {kindOptions.map((k) => (
              <option key={k} value={k}>{window.PC_kindLabel(s.classKey, k, s.typeMap[k])}</option>
            ))}
          </select>
        </PK_Field>
        {flavor ? PC_llmField(s, flavor) : null}
      </PK_Grid>
      {s.loadingShape ? (
        <div className="muted text-sm" role="status" data-testid="pc-register-loading">Loading the fields for this kind…</div>
      ) : null}
      {s.typesFailed ? (
        <div className="nv-form-error" role="alert" data-testid="pc-register-types-error">
          {"Could not load the fields for this kind: " + window.primerApi.readRefusal(s.types.error, String(s.types.error)).message}
        </div>
      ) : null}
      {s.typesFailed ? (
        <div>
          <Btn kind="ghost" data-testid="pc-register-types-retry" onClick={s.retryTypes} busy={!!s.types.loading}>
            {s.types.loading ? "Trying again…" : "Try again"}
          </Btn>
        </div>
      ) : null}
      {s.kindNotServed ? (
        <div className="nv-form-error" role="alert" data-testid="pc-register-kind-missing">
          {s.kind
            ? 'This install does not serve the kind "' + s.kind + '", so the form cannot show its fields.'
            : "This install serves no kind for this class, so the form cannot show its fields."}
        </div>
      ) : null}
      {wide.length || others.length ? <PK_Grid cols={2}>{wide.concat(others).map((f) => PC_llmField(s, f))}</PK_Grid> : null}
      {s.shape && s.shape.limits ? (
        <window.PC_LimitsSection fields={s.limitFields} value={s.eff.limits} problems={s.limitProblems} testid="pc-limits" classKey={s.classKey}
          onChange={(next) => s.setField("row", "limits", next)} />
      ) : null}
      {s.editing && s.dirty ? (
        <div className="field-help" id={s.helpId} data-testid="pc-register-import-more-help">{PC_LLM_UNSAVED_HELP}</div>
      ) : null}
    </div>
  );
}

// ---- page 2 ----------------------------------------------------------------------------------------------------------------------------------------------

// The models page: what was found, the checklist with a context window per model, and where each row stands.
function PC_LlmModels({ s }) {
  const PK_CheckTable = window.PK_CheckTable;
  const tableRows = s.rows.map((r, i) => {
    const status = PC_llmRowStatus(r);
    const settled = PC_llmDone(r) || r.status === "invalid";
    const frozen = s.frozen(r);
    const statusId = s.uid + "-status-" + i;
    return {
      key: r.name,
      label: r.name,
      checked: r.ticked,
      disabled: frozen,
      extra: (
        <div className="pc-import-extra">
          <input type="number" min="1" step="1" inputMode="numeric" className="pc-import-context" data-testid={"pc-import-context:" + r.name}
            aria-label={"Context window for " + r.name} aria-describedby={statusId} aria-invalid={r.ticked && !settled && status.tone === "error" ? "true" : undefined}
            readOnly={frozen} value={r.context} onChange={(e) => s.setContext(r.name, e.target.value)} />
          <span className="pc-import-status" id={statusId} data-testid={"pc-import-status:" + r.name} data-tone={status.tone || undefined}>{status.text}</span>
        </div>
      ),
    };
  });
  return (
    <div className="col" style={{ gap: 14, minWidth: 0 }} ref={s.rootRef} data-testid="pc-llm-models">
      <PC_LlmFocus n={s.handoff.n} to={s.handoff.to} rootRef={s.rootRef} />
      <div className="pc-import-head" tabIndex={-1} data-pc-focus="true" data-testid="pc-import-head">
        <div className="pc-import-title">{"Models found at " + s.providerId}</div>
        <div className="pc-import-sub">{"✓ Connected; " + PC_llmCount(s.listed, "model", "models") + " listed"}</div>
      </div>
      {s.already != null ? (
        <div className="field-help" data-testid="pc-import-already">{"Already imported: " + s.already}</div>
      ) : null}
      {s.rows.length ? (
        <PK_CheckTable testid="pc-import" extraLabel="Context window" rows={tableRows} onToggle={s.toggle} onToggleAll={s.toggleAll} />
      ) : (
        <div className="muted" data-testid="pc-import-none">Every model the provider lists already has a profile.</div>
      )}
      <div className="field-help" data-testid="pc-import-help">{PC_LLM_IMPORT_HELP}</div>
      {s.summary ? (
        <div className="pc-import-summary" role="status" data-testid="pc-import-summary">{s.summary}</div>
      ) : null}
    </div>
  );
}

// ---- the page --------------------------------------------------------------------------------------------------------------------------------------------

function PC_LlmRegister({ klass, editRow, excludeKinds, frame, blocked, onSaved, onClose }) {
  const api = window.primerApi;
  const editing = !!editRow;
  const ty = window.PC_useTypes(klass.plural);
  const typeMap = ty.types || {};
  const kinds = ty.types ? window.PC_servedKinds(klass.key, typeMap, excludeKinds) : [];

  // The row as the overlay opened it: "unsaved changes" are the draft against THIS, not against whatever `editRow` is later handed (a caller that reloads its list under
  // the open overlay, after the probe of Import more models stamped the row, would otherwise make an untouched draft look changed).
  const [opened, setOpened] = React.useState(() => (editRow ? window.PC_draftFromRow(klass.plural, editRow) : null));
  const [draft, setDraft] = React.useState(() => (opened ? { ...opened } : {}));
  const [step, setStep] = React.useState(1);
  const [rows, setRows] = React.useState([]);
  const [listed, setListed] = React.useState(0);
  const [already, setAlready] = React.useState(null);
  const [providerRow, setProviderRow] = React.useState(null);
  const [rounds, setRounds] = React.useState(0);
  const [out, setOut] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [failedOnce, setFailedOnce] = React.useState(false);
  const [handoff, setHandoff] = React.useState({ n: 0, to: "first" });
  const outRef = React.useRef(null);
  const live = React.useRef(true);
  const rootRef = React.useRef(null);
  const found = React.useRef(null);
  const asked = React.useRef("");
  const retried = React.useRef(false);
  const uid = React.useId();

  React.useEffect(() => {
    live.current = true;
    return () => { live.current = false; };
  }, []);
  // A Try again that worked: the button it was pressed on is gone, so the focus goes to the first field (PC_LlmFocus, once the fields are drawn).
  const haveTypes = !!ty.types;
  React.useEffect(() => {
    if (!haveTypes || !retried.current) return;
    retried.current = false;
    setHandoff((h) => ({ n: h.n + 1, to: "first" }));
  }, [haveTypes]);

  // ---- the draft, as it is shown and as it is sent
  const kind = draft.provider || kinds[0] || "";
  const shape = typeMap[kind] || null;
  const configFields = shape ? (shape.config_fields || []).map(window.PC_normalizeField) : [];
  const hasUrl = configFields.some((f) => f.key === "url");
  const limitFields = window.PC_limitFieldsFor(klass.key, kind);
  const eff = editing ? draft : PC_llmFill(draft, kind, configFields, limitFields);
  const missing = configFields.filter((f) => f.required && PC_llmBlank((eff.config || {})[f.key]));
  const idProblem = editing ? null : window.PC_idProblem(eff.id);
  const idTyped = eff.id != null && String(eff.id) !== "";
  const limitProblems = shape && shape.limits ? window.PC_limitsProblems(limitFields, eff.limits || {}) : {};
  const formOk = !!shape && !idProblem && missing.length === 0 && Object.keys(limitProblems).length === 0;
  const body = formOk ? PC_llmBody(eff, shape) : null;
  // What Next sends. The answer to a request is only used while the draft still makes this same body: a connection edited under a request that is out would otherwise be
  // registered with the models of the one it replaced.
  const discoverBody = body ? { provider: body.provider, config: body.config || {} } : null;
  asked.current = discoverBody ? JSON.stringify(discoverBody) : "";
  const dirty = editing && !PC_llmSame(draft, opened);
  const typesFailed = !!ty.error && !ty.types;
  const loadingShape = !ty.types && !ty.error;
  const kindNotServed = !!ty.types && !shape;
  const helpId = uid + "-unsaved";
  const providerId = editing ? editRow.id : (providerRow && providerRow.id) || eff.id;

  const setField = (scope, key, value) => {
    if (scope === "config") setDraft({ ...eff, config: { ...(eff.config || {}), [key]: value } });
    else setDraft({ ...eff, [key]: value });
  };
  const setKind = (next) => {
    setDraft({ ...eff, provider: next, config: {} });
    setFailedOnce(false);
    setError(null);
  };
  const retryTypes = () => {
    if (ty.loading) return;
    retried.current = true;
    ty.retry();
  };

  // ---- the rows of page 2
  const attempted = rows.some((r) => PC_llmDone(r) || r.status === "failed");
  const locked = !!providerRow || attempted;
  // A round of the import has ended and something is left: the footer is "Close" and "Retry failed". Until then (the first round included, with the provider already
  // created and the profiles going in) the right button is still the one that was pressed, "Import N models", busy.
  const retryMode = rounds > 0;
  const toImport = rows.filter((r) => r.ticked && !PC_llmDone(r) && r.status !== "invalid");
  const importReady = toImport.length > 0 && toImport.every((r) => PC_llmContextProblem(r.context) === null);
  // The N of "Import N models" is every row that is ticked, so it does not count down as the rows are answered.
  const tickedCount = rows.filter((r) => r.ticked && r.status !== "invalid").length;
  const failedRows = rows.filter((r) => r.status === "failed");
  const summary = locked && failedRows.length
    ? "The provider is registered, but " + PC_llmCount(failedRows.length, "profile", "profiles") + " could not be created. "
      + "Retry failed tries again; Close leaves " + (failedRows.length === 1 ? "it" : "them") + " for later (Import more models, when you edit the provider)."
    : "";
  // A row is frozen once it is settled, while the import runs, and (once the provider exists) when it was never part of the import; a failed row stays open, so its
  // context window can be corrected and it can be left out of the retry.
  const frozen = (r) => PC_llmDone(r) || r.status === "invalid" || out === "import" || (locked && r.status === "pending");
  const toggle = (key) => setRows(rows.map((r) => (r.name === key && !frozen(r) ? { ...r, ticked: !r.ticked } : r)));
  const toggleAll = (checked, keys) => {
    const set = new Set(keys);
    setRows(rows.map((r) => (set.has(r.name) && !frozen(r) ? { ...r, ticked: !!checked } : r)));
  };
  const setContext = (name, value) => setRows(rows.map((r) => (r.name === name && !frozen(r) ? { ...r, context: value } : r)));

  // ---- requests. One of the page's own at a time (`out` says which); the overlay's Delete (`blocked`) holds them all off.
  const begin = (what) => {
    if (outRef.current || blocked) return false;
    outRef.current = what;
    setOut(what);
    setError(null);
    return true;
  };
  const end = () => {
    outRef.current = null;
    if (live.current) setOut(null);
  };
  const say = (err) => {
    if (live.current) setError(api.readRefusal(err, String(err)).message);
  };
  const showModels = () => {
    setError(null);
    setStep(2);
    setHandoff((h) => ({ n: h.n + 1, to: "head" }));
  };
  const savedRow = (row, sent) => (row && typeof row === "object" ? row : sent);

  const goNext = async () => {
    if (editing || !formOk || !discoverBody) return;
    const key = JSON.stringify(discoverBody);
    // The same connection as the list already on page 2 (the operator went Back and changed something else): the list, and what was ticked in it, are kept.
    if (found.current === key && rows.length) {
      showModels();
      return;
    }
    if (!begin("discover")) return;
    let answer = null;
    let failure = null;
    try {
      answer = await api.apiFetch("POST", "/llm_providers/_discover_models", discoverBody);
    } catch (err) {
      failure = err;
    }
    end();
    if (!live.current) return;
    if (asked.current !== key) {
      setError("The connection changed while the provider was answering. Press Next again.");
      return;
    }
    if (failure) {
      say(failure);
      setFailedOnce(true);
      return;
    }
    const models = PC_llmListed(answer);
    if (!models.length) {
      setError(PC_LLM_NO_MODELS);
      setFailedOnce(true);
      return;
    }
    found.current = key;
    setFailedOnce(false);
    setRows(PC_llmRows(models));
    setListed(models.length);
    setAlready(null);
    showModels();
  };

  const back = () => {
    if (outRef.current || locked) return;
    setError(null);
    setStep(1);
    setHandoff((h) => ({ n: h.n + 1, to: "first" }));
  };

  // "Register without models" (create) and "Save" (edit): the provider alone.
  const post = async (what, method, path) => {
    if (!formOk || !body || !begin(what)) return;
    let row = null;
    let failure = null;
    try {
      row = await api.apiFetch(method, path, body);
    } catch (err) {
      failure = err;
    }
    end();
    if (!live.current) return;
    if (failure) {
      say(failure);
      return;
    }
    onSaved(savedRow(row, body));
  };
  const registerWithout = () => (editing ? undefined : post("register", "POST", "/llm_providers"));
  const save = () => (editing ? post("save", "PUT", "/llm_providers/" + encodeURIComponent(editRow.id)) : undefined);

  // What the toast says when the profiles are in: "<id>: 3 profiles imported", or "<id>: 2 of 3 profiles imported" when the operator closes on a partial import.
  const words = (done, wanted, id) => ({
    title: editing ? "Profiles imported" : "Provider registered",
    detail: id + ": " + (done === wanted ? PC_llmCount(done, "profile", "profiles") : done + " of " + wanted + " profiles") + " imported",
  });
  const closeAfterImport = () => {
    if (out === "import" || blocked) return;
    const wanted = rows.filter((r) => r.ticked).length;
    onSaved(editing ? editRow : providerRow, words(rows.filter((r) => r.ticked && PC_llmDone(r)).length, wanted, providerId));
  };

  // Import (and Retry failed): the provider once per overlay, then the profiles, one after another. A second press while it runs does nothing (`outRef`).
  const importNow = async () => {
    if (step !== 2 || !importReady || !begin("import")) return;
    let parent = providerRow;
    if (!editing && !parent) {
      if (!formOk || !body) {
        end();
        return;
      }
      let made = null;
      let failure = null;
      try {
        made = await api.apiFetch("POST", "/llm_providers", body);
      } catch (err) {
        failure = err;
      }
      if (failure) {
        end();
        say(failure);
        return;
      }
      parent = savedRow(made, body);
      if (live.current) setProviderRow(parent);
    }
    const id = editing ? editRow.id : parent.id || body.id;
    let done = null;
    try {
      done = await PC_importProfiles({
        apiFetch: api.apiFetch,
        providerId: id,
        rows,
        onProgress: (next) => { if (live.current) setRows(next); },
        shouldStop: () => !live.current,
      });
    } catch (err) {
      end();
      say(err);
      return;
    }
    end();
    if (!live.current) return;
    setRows(done);
    setRounds((n) => n + 1);
    const wanted = done.filter((r) => r.ticked);
    if (wanted.every(PC_llmDone)) onSaved(editing ? editRow : parent, words(wanted.length, wanted.length, id));
  };

  // "Import more models" (edit): the SAVED provider is probed (its stored secret is read on the server) and the names that already have a profile are left out. The probe
  // stamps `last_probe_at`, `last_probe_ok` and `last_error` on the stored row, whether it works or not, so the row is read again afterwards and the draft takes the new
  // stamps (and only those: whatever else was typed meanwhile stays): a Save pressed after Back would otherwise send the stamps the row had when the overlay opened and put
  // the provider's reachability badge back to what it was. The read is a courtesy; if it fails the page goes on without it.
  const importMore = async () => {
    if (!editing || dirty || !begin("more")) return;
    const enc = encodeURIComponent(editRow.id);
    let probed = null;
    let have = null;
    let failure = null;
    try {
      const both = await Promise.all([
        api.apiFetch("GET", "/llm_providers/" + enc + "/discovered_models", null),
        api.apiFetch("GET", "/llm_providers/" + enc + "/models", null),
      ]);
      probed = both[0];
      have = both[1];
    } catch (err) {
      failure = err;
    }
    let fresh = null;
    try {
      fresh = await api.apiFetch("GET", "/llm_providers/" + enc, null);
    } catch (err) {
      fresh = null;
    }
    end();
    if (!live.current) return;
    if (fresh && fresh.id === editRow.id) {
      const stamps = {};
      ["last_probe_at", "last_probe_ok", "last_error"].forEach((k) => {
        if (Object.prototype.hasOwnProperty.call(fresh, k)) stamps[k] = fresh[k];
      });
      setOpened((o) => ({ ...o, ...stamps }));
      setDraft((d) => ({ ...d, ...stamps }));
    }
    if (failure) {
      say(failure);
      return;
    }
    const models = PC_llmListed(probed);
    if (!models.length) {
      setError(PC_LLM_NO_MODELS);
      return;
    }
    const registered = new Set(((have && have.models) || []).map(String));
    const unregistered = models.filter((m) => !registered.has(m.name));
    setRows(PC_llmRows(unregistered));
    setListed(models.length);
    setAlready(models.length - unregistered.length);
    showModels();
  };

  // ---- what the overlay is handed
  const held = !!blocked;
  // another request of the page's own is out: this button is held off for it, and says nothing about a request that is not its own
  const elsewhere = (what) => out !== null && out !== what;
  const view = {
    editing, classKey: klass.key, eff, kind, kinds, typeMap, shape, configFields, hasUrl, types: ty, typesFailed, loadingShape, kindNotServed,
    idProblem, idTyped, limitFields, limitProblems, dirty, helpId, setField, setKind, retryTypes, handoff, rootRef,
    providerId, listed, already, rows, uid, summary, frozen, toggle, toggleAll, setContext,
  };
  // The footer's buttons carry keys of their own. React keeps the DOM node of an element with the same type and key at the same place, so without them the Back button of the
  // models page and the Cancel of the connection page (both the first thing in the left of the footer) would be ONE node: a focus left on Back would stay on a button now called
  // Cancel, and a second Enter would close the overlay. With them a page change replaces the button, the focus falls to <body>, and PC_LlmFocus hands it to the new body.
  const cancel = <Btn key="cancel" kind="ghost" data-testid="pc-register-cancel" onClick={onClose}>Cancel</Btn>;

  if (step === 2) {
    return frame({
      body: <PC_LlmModels s={view} />,
      // Back until the provider exists; from then on there is no going back to create it again, and Close (which tells the caller about the provider) takes its place.
      footerLeft: locked ? (
        <Btn key="close" kind="ghost" data-testid="pc-register-close" onClick={closeAfterImport} blocked={held || out === "import"}>Close</Btn>
      ) : (
        <Btn key="back" kind="ghost" data-testid="pc-register-back" onClick={back} blocked={held || out !== null}>‹ Back</Btn>
      ),
      // One button element throughout (same type, same key): React keeps its DOM node, so the press that starts the import keeps its focus when the import ends in
      // failures and the same button says "Retry failed".
      footerRight: (
        <Btn key="models" kind="primary" data-testid={retryMode ? "pc-register-retry-failed" : "pc-register-submit"} onClick={importNow} busy={out === "import"}
          blocked={held} disabled={!importReady}>
          {retryMode ? "Retry failed" : "Import " + PC_llmCount(tickedCount, "model", "models")}
        </Btn>
      ),
      error,
      busy: out !== null,
    });
  }

  if (editing) {
    return frame({
      body: <PC_LlmSettings s={view} />,
      footerLeft: (
        <React.Fragment>
          {cancel}
          {/* Off while the draft differs from the row: the probe reads the SAVED provider, so it would list what the draft does not say. `disabled` is "cannot be pressed now,
              whatever the answer"; `blocked` (aria-disabled) says it to assistive technology too, and the button is described by the help in the body. */}
          <Btn kind="ghost" data-testid="pc-register-import-more" onClick={importMore} busy={out === "more"} blocked={held || dirty || elsewhere("more")}
            disabled={dirty} title={dirty ? PC_LLM_UNSAVED_HELP : undefined} aria-describedby={dirty ? helpId : undefined}>
            Import more models
          </Btn>
        </React.Fragment>
      ),
      footerRight: (
        <Btn key="connection" kind="primary" data-testid="pc-register-submit" onClick={save} busy={out === "save"} blocked={held || elsewhere("save")} disabled={!formOk}>
          Save
        </Btn>
      ),
      error,
      busy: out !== null,
    });
  }

  return frame({
    body: <PC_LlmSettings s={view} />,
    footerLeft: cancel,
    footerRight: (
      <React.Fragment key="connection">
        {failedOnce ? (
          <Btn kind="ghost" data-testid="pc-register-without-models" onClick={registerWithout} busy={out === "register"}
            blocked={held || elsewhere("register")} disabled={!formOk}>
            Register without models
          </Btn>
        ) : null}
        <Btn kind="primary" data-testid="pc-register-next" onClick={goNext} busy={out === "discover"} blocked={held || elsewhere("discover")} disabled={!formOk}>
          Next ›
        </Btn>
      </React.Fragment>
    ),
    error,
    busy: out !== null,
  });
}

window.PC_REGISTER_PAGES = Object.assign(window.PC_REGISTER_PAGES || {}, { llm: PC_LlmRegister });
Object.assign(window, { PC_LlmRegister, PC_llmBody, PC_importProfiles });
