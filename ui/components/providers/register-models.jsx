/* global React, window, Btn */
// register-models.jsx - the Embedding and Cross Encoding pages of the Register provider overlay (plan 02 Task 14; design 6.5, 13.1, 13.2 and 6.9).
//
// An embedding or cross-encoder provider is registered in two pages, and the models it serves are part of the provider row (`models`), not rows of their own:
//
//   page 1  the connection: id, type, flavor, URL, key or token (whatever the kind serves), the limits its adapters read, and, for a Hugging Face provider and for a cross encoder,
//           the MODELS table (their models are typed: there is no inventory to list). "Next" finds out which models can be registered. OpenAI and Gemini LIST them
//           (POST /embedding_providers/_discover_models); Hugging Face and the cross encoder ASK THE HUB about each typed model with the typed token
//           (POST /huggingface/_check_models). A refusal or an empty list stays here and says why. There is no "Register without models": the `models` list needs an entry.
//   page 2  the models, ticked: the names with "embed" in them first and ticked for a listing (design 13.2), the typed models in the order they were typed, ticked only when the
//           Hub confirmed them, with the Hub's verdict beside each. "Register with N models" posts the provider with the ticked ones as its `models`, in one request.
//
// Editing a provider (design 6.9) is one page with the id and the type locked, the kind's fields, the models as a table of EVERY kind (prefilled from the row), and "Save". The draft
// is the WHOLE served row, so every field the page does not show goes back unchanged. An edit has no Next and probes nothing: its draft holds the MASK of the key or token, and a
// probe would authenticate with it.
//
// The page is a page of the Register overlay (register-overlay.jsx, "the shell's slots"): it is handed { klass, editRow, excludeKinds, frame, blocked, onSaved, onClose }, it ALWAYS
// returns frame({ body, footerLeft, footerRight, error, busy }), and it holds its actions off while the overlay's own request, a Delete, is out (`blocked`). The state every wizard
// page keeps, the hand-off of the focus after a page change (a component INSIDE the body: the page's effects run before the body is in the DOM), the served field and the lines for
// loading kinds are register-common.jsx's. The body is drawn beside the page, not under it, so nothing is handed to it through context: it gets one props object.

const PC_MODELS_NO_MODELS = "The provider answered but listed no models.";
const PC_MODELS_TOKEN_HELP = "Used to pull the models; optional for public ones";
const PC_MODELS_LOCAL_HELP = "Optional for local servers";
const PC_MODELS_NO_VERDICT = "No answer for this model";
const PC_MODELS_DISCOVERED_HELP = 'Each ticked model is registered on the provider. Models with "embed" in the name are listed first and ticked.';
const PC_MODELS_HUB_HELP = "Each ticked model is registered on the provider. One the check could not confirm can be ticked too: it is pulled the first time it is used.";
const PC_MODELS_TABLE_HELP = "Next looks each model up on the Hugging Face Hub.";

// The most models the Hub is asked about at once: the route answers 422 to a longer list (primer/api/routers/hf_hub.py), and each name is a request to the Hub.
const PC_MODELS_HUB_MAX = 50;

// ---- the models table -------------------------------------------------------------------------------------------------------------------------------------

// The columns of the MODELS table: a model is its name, and for a cross encoder a pair length as well (optional).
function PC_modelsColumns(isCE) {
  const columns = [{ key: "name", label: "Model", input: "text" }];
  if (isCE) columns.push({ key: "max_pair_length", label: "Max pair length", input: "number", width: "8rem" });
  return columns;
}

// The rows the table starts with: one empty row for a new provider (design 6.5), the row's own models for an edit.
function PC_modelsStartRows(isCE, editRow) {
  const models = editRow && Array.isArray(editRow.models) ? editRow.models : [];
  if (!models.length) return [isCE ? { name: "", max_pair_length: "" } : { name: "" }];
  return models.map((m) => {
    const row = { name: m && m.name != null ? String(m.name) : "" };
    if (isCE) row.max_pair_length = m && m.max_pair_length != null ? String(m.max_pair_length) : "";
    return row;
  });
}

// What the typed rows come to: the models that have a name (trimmed, in the order typed, each once), whether some row has none, and the first thing wrong with them, or null: a
// name typed twice, a pair length that is not a whole number of 1 or more, and (when the Hub will be asked) more names than it is asked about at once.
function PC_modelsTyped(rows, opts) {
  const o = opts || {};
  const named = [];
  const seen = new Set();
  let blank = false;
  let problem = null;
  (rows || []).forEach((r) => {
    const name = r && r.name != null ? String(r.name).trim() : "";
    if (!name) {
      blank = true;
      return;
    }
    if (seen.has(name)) {
      if (!problem) problem = '"' + name + '" is listed twice';
      return;
    }
    seen.add(name);
    const length = o.ce && r.max_pair_length != null ? String(r.max_pair_length).trim() : "";
    if (length !== "" && !(/^\d+$/.test(length) && Number.isSafeInteger(Number(length)) && Number(length) >= 1)) {
      if (!problem) problem = 'Max pair length of "' + name + '": enter a whole number of 1 or more';
    }
    named.push({ name, length });
  });
  if (!problem && o.hubMax && named.length > o.hubMax) problem = "The Hub is asked about " + o.hubMax + " models at most at a time";
  return { named, blank, problem };
}

// The `models` of a body: one entry per named model. A cross encoder's entry carries `max_pair_length` as a number when one was typed. `kept` (an edit) is the models the row
// came with, by name: a model that is still there keeps every field of its own the page does not know, and a pair length the operator emptied is the null the server reads as none.
function PC_modelsBuild(named, isCE, kept) {
  return named.map((m) => {
    const base = kept && kept.has(m.name) ? { ...kept.get(m.name) } : {};
    const entry = { ...base, name: m.name };
    if (isCE) {
      if (m.length !== "") entry.max_pair_length = Number(m.length);
      else if (Object.prototype.hasOwnProperty.call(base, "max_pair_length")) entry.max_pair_length = null;
      else delete entry.max_pair_length;
    }
    return entry;
  });
}

// ---- the answers -------------------------------------------------------------------------------------------------------------------------------------------

// The entries of a discovery answer ({ models: [{ name }] }), each name once, in the server's order.
function PC_modelsListed(answer) {
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

// The rows of page 2 for a listing: the names with "embed" first, and ticked (PC_embedFirst), the rest after them.
function PC_modelsDiscoveredRows(models) {
  return window.PC_embedFirst(models).map((m) => ({ name: m.name, ticked: m.ticked, hub: false }));
}

// The rows of page 2 for the Hub's verdicts: the typed models in the order they were typed, each with its verdict, ticked only when the Hub confirmed it. A model the answer holds
// no verdict for is still a row, and says so.
function PC_modelsHubRows(typed, answer) {
  const byName = new Map();
  ((answer && answer.models) || []).forEach((v) => {
    if (v && v.name != null) byName.set(String(v.name), v);
  });
  return typed.map((m) => {
    const v = byName.get(m.name);
    const ok = !!(v && v.ok === true);
    return { name: m.name, ticked: ok, hub: true, ok, detail: v ? String(v.detail || v.status || "") : PC_MODELS_NO_VERDICT, length: m.length };
  });
}

// What the Hub column says about a row: the verdict, and the pair length that will be registered with it.
function PC_modelsVerdict(r) {
  const text = r.length ? r.detail + " · Max pair length " + r.length : r.detail;
  return (
    <div className="pc-import-extra">
      <span className="pc-import-status" data-testid={"pc-import-status:" + r.name} data-tone={r.ok ? "ok" : "error"}>{text}</span>
    </div>
  );
}

// ---- page 1 ------------------------------------------------------------------------------------------------------------------------------------------------

// The words a served config field is drawn with on this page, where they are not the server's: the token is named and explained the same for Hugging Face and the cross encoder,
// and a key that a kind with a URL does not require is a server that may run open.
function PC_modelsWords(s, f) {
  if (f.key === "token") return { help: PC_MODELS_TOKEN_HELP };
  if (f.key === "api_key" && !f.required && s.hasUrl) return { help: PC_MODELS_LOCAL_HELP };
  return {};
}

// The connection page. Id, Type (when the class serves more than one kind) and, for OpenAI, Flavor share a row; the URL and the key or token each have a row of their own; then
// the MODELS table (typed models) and the limits box.
function PC_ModelsSettings({ s }) {
  const PK_Grid = window.PK_Grid;
  const PK_Field = window.PK_Field;
  const PK_RowTable = window.PK_RowTable;
  const flavor = s.configFields.filter((f) => f.key === "flavor")[0] || null;
  const wide = s.configFields.filter((f) => f.key !== "flavor");
  // A locked select names the row's own kind, even one this install no longer serves (the page then says so).
  const kindOptions = s.editing ? (s.kind ? [s.kind] : []) : s.kinds;
  const top = 1 + (s.showType ? 1 : 0) + (flavor ? 1 : 0);
  return (
    <div className="col" style={{ gap: 16, minWidth: 0 }} ref={s.rootRef} data-testid="pc-models-settings">
      <window.PC_PageFocus n={s.handoff.n} to={s.handoff.to} rootRef={s.rootRef} />
      <PK_Grid cols={top}>
        <window.PC_IdField value={s.eff.id} locked={s.editing} testid="pc-field:id" problem={s.idTyped ? s.idProblem : null}
          onChange={(next) => s.setField("row", "id", next)} />
        {s.showType ? (
          <PK_Field label="Type">
            <select data-testid="pc-field:type" value={s.kind} disabled={s.editing || !kindOptions.length} onChange={(e) => s.setKind(e.target.value)}>
              {kindOptions.map((k) => (
                <option key={k} value={k}>{window.PC_kindLabel(s.classKey, k, s.typeMap[k])}</option>
              ))}
            </select>
          </PK_Field>
        ) : null}
        {flavor ? (
          <window.PC_ServedField field={flavor} value={(s.eff.config || {}).flavor} onChange={(next) => s.setField("config", "flavor", next)} />
        ) : null}
      </PK_Grid>
      <window.PC_TypesNotice loading={s.loadingShape} failed={s.typesFailed} notServed={s.kindNotServed} kind={s.kind} types={s.types} onRetry={s.retryTypes} />
      {wide.length ? (
        <PK_Grid cols={2}>
          {wide.map((f) => {
            const words = PC_modelsWords(s, f);
            return (
              <window.PC_ServedField key={f.key} field={f} value={(s.eff.config || {})[f.key]} onChange={(next) => s.setField("config", f.key, next)}
                editing={s.editing} span="full" help={words.help} />
            );
          })}
        </PK_Grid>
      ) : null}
      {s.showTable ? (
        <div className="col" style={{ gap: 4, minWidth: 0 }}>
          <PK_RowTable title="MODELS" addLabel="+ Add" testid="pc-models" columns={PC_modelsColumns(s.isCE)} rows={s.table} onChange={s.setTable} />
          {s.typed.problem ? (
            <div className="field-help pc-models-problem" role="alert" data-testid="pc-models-problem">{s.typed.problem}</div>
          ) : null}
          {s.hubChecks && !s.editing ? <div className="field-help" data-testid="pc-models-help">{PC_MODELS_TABLE_HELP}</div> : null}
        </div>
      ) : null}
      {s.shape && s.shape.limits && s.limitFields.length ? (
        <window.PC_LimitsSection fields={s.limitFields} value={s.eff.limits} problems={s.limitProblems} testid="pc-limits" classKey={s.classKey}
          onChange={(next) => s.setField("row", "limits", next)} />
      ) : null}
    </div>
  );
}

// ---- page 2 ------------------------------------------------------------------------------------------------------------------------------------------------

// The models page: what was found (or checked), the checklist, and where each row stands.
function PC_ModelsPick({ s }) {
  const PK_CheckTable = window.PK_CheckTable;
  const hub = s.source === "hub";
  const tableRows = s.rows.map((r) => ({
    key: r.name,
    label: r.name,
    checked: r.ticked,
    disabled: s.out === "register",
    extra: hub ? PC_modelsVerdict(r) : undefined,
  }));
  const passed = s.rows.filter((r) => r.ok).length;
  const title = hub ? "Models checked on the Hugging Face Hub" : "Models found at " + s.providerId;
  const sub = hub
    ? (passed === s.rows.length ? "✓ " : "") + passed + " of " + window.PC_countOf(s.rows.length, "model", "models") + " can be pulled"
    : "✓ Connected; " + window.PC_countOf(s.rows.length, "model", "models") + " listed";
  return (
    <div className="col" style={{ gap: 14, minWidth: 0 }} ref={s.rootRef} data-testid="pc-models-pick">
      <window.PC_PageFocus n={s.handoff.n} to={s.handoff.to} rootRef={s.rootRef} />
      <window.PC_PageHead title={title} sub={sub} tone={hub && passed !== s.rows.length ? "warn" : "ok"} />
      <div className={hub ? "pc-hub-table" : undefined}>
        <PK_CheckTable testid="pc-import" extraLabel={hub ? "Hub check" : undefined} rows={tableRows} onToggle={s.toggle} onToggleAll={s.toggleAll} />
      </div>
      <div className="field-help" data-testid="pc-import-help">{hub ? PC_MODELS_HUB_HELP : PC_MODELS_DISCOVERED_HELP}</div>
    </div>
  );
}

// ---- the page ----------------------------------------------------------------------------------------------------------------------------------------------

function PC_ModelsRegister({ klass, editRow, excludeKinds, frame, blocked, onSaved, onClose }) {
  const api = window.primerApi;
  const editing = !!editRow;
  const isCE = klass.key === "cross_encoder";
  const w = window.PC_useWizard(klass.plural, blocked);
  const typeMap = w.types.types || {};
  const kinds = w.types.types ? window.PC_servedKinds(klass.key, typeMap, excludeKinds) : [];

  const [draft, setDraft] = React.useState(() => (editRow ? window.PC_draftFromRow(klass.plural, editRow) : {}));
  const [table, setTable] = React.useState(() => PC_modelsStartRows(isCE, editRow));
  const [rows, setRows] = React.useState([]);
  const [source, setSource] = React.useState("discovered");
  const found = React.useRef(null);
  const asked = React.useRef("");
  const kept = React.useRef(null);
  if (kept.current === null) {
    kept.current = new Map(((editRow && editRow.models) || []).filter((m) => m && m.name != null).map((m) => [String(m.name), m]));
  }

  // ---- the draft, as it is shown and as it is sent
  const kind = draft.provider || kinds[0] || "";
  const shape = typeMap[kind] || null;
  const configFields = shape ? (shape.config_fields || []).map(window.PC_normalizeField) : [];
  const hasUrl = configFields.some((f) => f.key === "url");
  const limitFields = window.PC_limitFieldsFor(klass.key, kind);
  const eff = editing ? draft : window.PC_fillDraft(draft, kind, configFields, limitFields);
  // Which models are typed: the cross encoder's and a Hugging Face provider's (the Hub is asked about them), and every model of an edit.
  const hubChecks = isCE || kind === "huggingface";
  const showTable = editing || hubChecks;
  const typed = PC_modelsTyped(table, { ce: isCE, hubMax: !editing && hubChecks ? PC_MODELS_HUB_MAX : 0 });
  const missing = configFields.filter((f) => f.required && window.PC_isBlank((eff.config || {})[f.key]));
  const idProblem = editing ? null : window.PC_idProblem(eff.id);
  const idTyped = eff.id != null && String(eff.id) !== "";
  const limitProblems = shape && shape.limits ? window.PC_limitsProblems(limitFields, eff.limits || {}) : {};
  // A new provider whose models are typed needs at least one, none of the rows blank (Next is off until it is filled or removed); an edit needs at least one, and a blank row is
  // dropped; a new provider whose models are listed has none to type (they are ticked on page 2).
  let modelsOk = true;
  if (editing) modelsOk = typed.named.length > 0 && !typed.problem;
  else if (hubChecks) modelsOk = typed.named.length > 0 && !typed.blank && !typed.problem;
  const formOk = !!shape && !idProblem && missing.length === 0 && Object.keys(limitProblems).length === 0 && modelsOk;
  const typesFailed = !!w.types.error && !w.types.types;
  const loadingShape = !w.types.types && !w.types.error;
  const kindNotServed = !!w.types.types && !shape;

  // The body of the provider as the form's wire rules make it (ticket T3: an emptied optional box is omitted, never sent as ""; a kind that serves config fields always posts
  // `config`, so a Hugging Face provider with no token posts `config: {}`), with `models` in place of the draft's. A new provider sends the limits its kind shows and no others
  // (a limit typed for a kind the operator has since left is not the new kind's); an edit sends the draft's, which is the whole row's.
  const bodyWith = (models) => {
    if (!formOk) return null;
    const body = window.PC_submittable(eff, shape, kind);
    if (!editing && body.limits) body.limits = window.PC_shownLimits(body.limits, limitFields);
    body.models = models;
    return body;
  };
  // What Next sends: the listing's {provider, config}, or the Hub's {models, token}. The answer to a request is only used while the draft still makes this same request: a
  // connection (or a model) edited under a request that is out would otherwise be registered with the answer for the one it replaced.
  const token = (eff.config || {}).token;
  const probe = !editing && formOk && !hubChecks ? bodyWith([]) : null;
  let nextBody = null;
  if (!editing && formOk) {
    nextBody = hubChecks
      ? { models: typed.named.map((m) => m.name), token: window.PC_isBlank(token) ? null : String(token).trim() }
      : { provider: probe.provider, config: probe.config || {} };
  }
  // The pair lengths are part of what the Hub's rows are made of, so a length edited under a request that is out makes the answer stale too.
  const nextKey = nextBody ? JSON.stringify(hubChecks ? { ...nextBody, lengths: typed.named.map((m) => m.length) } : nextBody) : "";
  asked.current = nextKey;
  const providerId = editing ? editRow.id : eff.id;

  const setField = (scope, key, value) => {
    if (scope === "config") setDraft({ ...eff, config: { ...(eff.config || {}), [key]: value } });
    else setDraft({ ...eff, [key]: value });
  };
  const setKind = (next) => {
    setDraft({ ...eff, provider: next, config: {} });
    w.setError(null);
  };

  // ---- the rows of page 2
  const ticked = rows.filter((r) => r.ticked);
  const registerReady = ticked.length > 0;
  const toggle = (key) => {
    if (w.out) return;
    setRows(rows.map((r) => (r.name === key ? { ...r, ticked: !r.ticked } : r)));
  };
  const toggleAll = (checked, keys) => {
    if (w.out) return;
    const set = new Set(keys);
    setRows(rows.map((r) => (set.has(r.name) ? { ...r, ticked: !!checked } : r)));
  };

  // ---- requests. One of the page's own at a time (`w.out` says which); the overlay's Delete (`blocked`) holds them all off.
  const goNext = async () => {
    if (editing || !nextBody) return;
    // The same connection as the list already on page 2 (the operator went Back and changed something else): the list, and what was ticked in it, are kept. The Hub is asked
    // again every time: a verdict can change while the operator is away (a gated model's terms accepted, a token fixed).
    if (!hubChecks && found.current === nextKey && rows.length) {
      w.show(2, "head");
      return;
    }
    if (!w.begin("check")) return;
    const key = nextKey;
    const names = typed.named;
    let answer = null;
    let failure = null;
    try {
      answer = hubChecks
        ? await api.apiFetch("POST", "/huggingface/_check_models", nextBody)
        : await api.apiFetch("POST", "/" + klass.plural + "/_discover_models", nextBody);
    } catch (err) {
      failure = err;
    }
    w.end();
    if (!w.live.current) return;
    if (asked.current !== key) {
      w.setError(hubChecks ? "The models changed while the Hub was answering. Press Next again." : "The connection changed while the provider was answering. Press Next again.");
      return;
    }
    if (failure) {
      w.say(failure);
      return;
    }
    if (hubChecks) {
      setRows(PC_modelsHubRows(names, answer));
      setSource("hub");
      found.current = null;
    } else {
      const models = PC_modelsListed(answer);
      if (!models.length) {
        w.setError(PC_MODELS_NO_MODELS);
        return;
      }
      found.current = key;
      setRows(PC_modelsDiscoveredRows(models));
      setSource("discovered");
    }
    w.show(2, "head");
  };

  const back = () => {
    if (w.outRef.current) return;
    w.show(1, "first");
  };

  const register = async () => {
    if (editing || w.step !== 2 || !registerReady) return;
    const wanted = ticked.map((r) => ({ name: r.name, length: r.length || "" }));
    const body = bodyWith(PC_modelsBuild(wanted, isCE, null));
    if (!body || !w.begin("register")) return;
    let answer = null;
    let failure = null;
    try {
      answer = await api.apiFetch("POST", "/" + klass.plural, body);
    } catch (err) {
      failure = err;
    }
    w.end();
    if (!w.live.current) return;
    if (failure) {
      w.say(failure);
      return;
    }
    onSaved(window.PC_savedRow(answer, body), { detail: providerId + ": " + window.PC_countOf(body.models.length, "model", "models") });
  };

  const save = async () => {
    if (!editing) return;
    const body = bodyWith(PC_modelsBuild(typed.named, isCE, kept.current));
    if (!body || !w.begin("save")) return;
    let answer = null;
    let failure = null;
    try {
      answer = await api.apiFetch("PUT", "/" + klass.plural + "/" + encodeURIComponent(editRow.id), body);
    } catch (err) {
      failure = err;
    }
    w.end();
    if (!w.live.current) return;
    if (failure) {
      w.say(failure);
      return;
    }
    onSaved(window.PC_savedRow(answer, body));
  };

  // ---- what the overlay is handed
  const held = !!blocked;
  const view = {
    editing, isCE, classKey: klass.key, eff, kind, kinds, showType: kinds.length > 1, typeMap, shape, configFields, hasUrl, types: w.types, typesFailed, loadingShape,
    kindNotServed, idProblem, idTyped, limitFields, limitProblems, setField, setKind, retryTypes: w.retryTypes, handoff: w.handoff, rootRef: w.rootRef,
    hubChecks, showTable, table, setTable, typed, rows, source, providerId, toggle, toggleAll, out: w.out,
  };
  // The footer's buttons carry keys of their own. React keeps the DOM node of an element with the same type and key at the same place, so without them the Back button of the models
  // page and the Cancel of the connection page (both the first thing in the left of the footer) would be ONE node: a focus left on Back would stay on a button now called Cancel, and
  // a second Enter would close the overlay. With them a page change replaces the button, the focus falls to <body>, and PC_PageFocus hands it to the new body.
  const cancel = <Btn key="cancel" kind="ghost" data-testid="pc-register-cancel" onClick={onClose}>Cancel</Btn>;

  if (w.step === 2 && !editing) {
    return frame({
      body: <PC_ModelsPick s={view} />,
      footerLeft: <Btn key="back" kind="ghost" data-testid="pc-register-back" onClick={back} blocked={held || w.out !== null}>{"‹ Back"}</Btn>,
      footerRight: (
        <Btn key="register" kind="primary" data-testid="pc-register-submit" onClick={register} busy={w.out === "register"} blocked={held} disabled={!registerReady}>
          {"Register with " + window.PC_countOf(ticked.length, "model", "models")}
        </Btn>
      ),
      error: w.error,
      busy: w.out !== null,
    });
  }

  if (editing) {
    return frame({
      body: <PC_ModelsSettings s={view} />,
      footerLeft: cancel,
      footerRight: (
        <Btn key="save" kind="primary" data-testid="pc-register-submit" onClick={save} busy={w.out === "save"} blocked={held} disabled={!formOk}>Save</Btn>
      ),
      error: w.error,
      busy: w.out !== null,
    });
  }

  return frame({
    body: <PC_ModelsSettings s={view} />,
    footerLeft: cancel,
    footerRight: (
      <Btn key="next" kind="primary" data-testid="pc-register-next" onClick={goNext} busy={w.out === "check"} blocked={held} disabled={!formOk}>{"Next ›"}</Btn>
    ),
    error: w.error,
    busy: w.out !== null,
  });
}

window.PC_REGISTER_PAGES = Object.assign(window.PC_REGISTER_PAGES || {}, { embedding: PC_ModelsRegister, cross_encoder: PC_ModelsRegister });
Object.assign(window, { PC_ModelsRegister });
