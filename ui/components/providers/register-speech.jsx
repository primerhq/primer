/* global React, window, Btn */
// register-speech.jsx - the ASR and TTS pages of the Register provider overlay (plan 02 Task 15; design 6.6 and 13.1) and PC_RadioList, the one single-choice list they need.
//
// A speech provider is registered in two pages, and the first needs nothing but a URL and a key:
//
//   page 1  the connection: id, URL, API key, the limits the adapter reads (ASR: the two timeouts; TTS: the timeouts, the retries and the backoffs) and, for TTS only, the default
//           model (there is no model listing for a TTS draft, design 13.1). "Next" posts {provider, config} to POST /stt_providers/_test or /tts_providers/_test, which accept
//           that partial draft and answer {ok, models} or {ok, voices}. `ok: false` and an empty list stay here and say why.
//   page 2  ONE choice from what the server listed, as a radio list with nothing picked: the model for ASR (saved as `default_model`), the voice for TTS (saved as
//           `default_voice`). "Register" is off until something is picked and posts the whole row in one request.
//
// Editing a provider (design 6.9) is one page: the id locked, the URL, the key, the default model (and, for TTS, the default voice) as text, the limits, and "Save". The draft is the
// WHOLE served row, so every field the page does not show goes back unchanged (the stored max_concurrency, and for ASR the retries its adapter never reads). An edit has no Next and
// tests nothing: its draft holds the MASK of the key, and a test would authenticate with it.
//
// No speech adapter reads max_concurrency, but the model requires it, so a new provider sends max_concurrency 1 (PC_initialLimits) and the page never shows it. No voice or model
// name is written in this file: they come from the server (the console's rule since M11c).
//
// The page is a page of the Register overlay (register-overlay.jsx, "the shell's slots"): it is handed { klass, editRow, excludeKinds, frame, blocked, onSaved, onClose }, it ALWAYS
// returns frame({ body, footerLeft, footerRight, error, busy }), and it holds its actions off while the overlay's own request, a Delete, is out (`blocked`). The state every wizard page
// keeps, the hand-off of the focus after a page change (a component INSIDE the body: the page's effects run before the body is in the DOM), the served field and the lines for loading
// kinds are register-common.jsx's. The body is drawn beside the page, not under it, so nothing is handed to it through context: it gets one props object.

const PC_SPEECH_LOCAL_HELP = "Optional for local servers";
const PC_SPEECH_NO_ANSWER = "The test gave no answer.";
const PC_SPEECH_URL = {
  stt: { placeholder: "http://asr.local:8006/v1", help: "The API base, not the /audio/transcriptions path" },
  tts: { placeholder: "http://tts.local:8004/v1", help: "The API base, not the /audio/speech path" },
};
const PC_SPEECH_ROW = {
  stt: {
    default_model: { label: "Default model", help: "The model the provider transcribes with when a call does not name one" },
  },
  tts: {
    default_model: { label: "Default model", help: "The model the provider speaks with when a call does not name one" },
    default_voice: { label: "Default voice", help: "The voice the provider speaks with when a call does not name one" },
  },
};

// ---- the radio list ------------------------------------------------------------------------------------------------------------------------------------------

// A single-choice list: a kit section titled `name` holding one radio input per option, named by its label, all of one group (the browser lets one be chosen and the arrow keys move
// between them). The kit has no single-choice control (flagged to the lead); this is the one local control, built from PK_Section. Each radio is `data-testid="<testid>:<value>"`.
function PC_RadioList({ name, options, value, onChange, testid }) {
  const PK_Section = window.PK_Section;
  const group = React.useId() + "-" + name;
  return (
    <PK_Section title={name} cols={1} testid={testid}>
      <div className="pc-radio-list">
        {(options || []).map((o) => (
          <label key={o.value} className="pc-radio-row">
            <input type="radio" name={group} value={o.value} checked={o.value === value} data-testid={testid + ":" + o.value} onChange={() => onChange(o.value)} />
            <span>{o.label}</span>
          </label>
        ))}
      </div>
    </PK_Section>
  );
}

// ---- the answers -------------------------------------------------------------------------------------------------------------------------------------------

// What a `_test` answer lists ({ models: [name] } or { voices: [name] }): each name once, in the server's order, none blank.
function PC_speechListed(items) {
  const seen = new Set();
  const out = [];
  (Array.isArray(items) ? items : []).forEach((m) => {
    const name = typeof m === "string" ? m : m && m.name != null ? String(m.name) : "";
    if (!name || seen.has(name)) return;
    seen.add(name);
    out.push(name);
  });
  return out;
}

// ---- page 1 ------------------------------------------------------------------------------------------------------------------------------------------------

// The words and look a served config field is drawn with on this page: the URL says what it is the base of, and a key that a kind with a URL does not require is a server that may run open.
function PC_speechConfigField(s, f) {
  if (f.key === "url") return { field: { ...f, placeholder: PC_SPEECH_URL[s.classKey].placeholder }, help: PC_SPEECH_URL[s.classKey].help };
  if (f.key === "api_key" && !f.required && s.hasUrl) return { field: f, help: PC_SPEECH_LOCAL_HELP };
  return { field: f, help: undefined };
}

// The connection page: the id (and the Type, when the class serves more than one kind), the URL and the key, the row fields this page shows (the default model of a TTS provider; both
// defaults in an edit), and the limits box.
function PC_SpeechSettings({ s }) {
  const PK_Grid = window.PK_Grid;
  const PK_Field = window.PK_Field;
  // A locked select names the row's own kind, even one this install no longer serves (the page then says so).
  const kindOptions = s.editing ? (s.kind ? [s.kind] : []) : s.kinds;
  return (
    <div className="col" style={{ gap: 16, minWidth: 0 }} ref={s.rootRef} data-testid="pc-speech-settings">
      <window.PC_PageFocus n={s.handoff.n} to={s.handoff.to} rootRef={s.rootRef} />
      <PK_Grid cols={s.showType ? 2 : 1}>
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
      </PK_Grid>
      <window.PC_TypesNotice loading={s.loadingShape} failed={s.typesFailed} notServed={s.kindNotServed} kind={s.kind} types={s.types} onRetry={s.retryTypes} />
      {s.configFields.length ? (
        <PK_Grid cols={2}>
          {s.configFields.map((f) => {
            const made = PC_speechConfigField(s, f);
            return (
              <window.PC_ServedField key={f.key} field={made.field} value={(s.eff.config || {})[f.key]} onChange={(next) => s.setField("config", f.key, next)}
                editing={s.editing} span="full" help={made.help} />
            );
          })}
        </PK_Grid>
      ) : null}
      {s.rowFields.length ? (
        <PK_Grid cols={2}>
          {s.rowFields.map((f) => (
            <window.PC_ServedField key={f.key} field={{ ...f, type: "text" }} value={s.eff[f.key]} onChange={(next) => s.setField("row", f.key, next)}
              label={PC_SPEECH_ROW[s.classKey][f.key].label} help={PC_SPEECH_ROW[s.classKey][f.key].help} />
          ))}
        </PK_Grid>
      ) : null}
      {s.shape && s.shape.limits && s.limitFields.length ? (
        <window.PC_LimitsSection fields={s.limitFields} value={s.eff.limits} problems={s.limitProblems} testid="pc-limits" classKey={s.classKey}
          onChange={(next) => s.setField("row", "limits", next)} />
      ) : null}
    </div>
  );
}

// ---- page 2 ------------------------------------------------------------------------------------------------------------------------------------------------

// The choice page: what the server listed, and the one thing to pick.
function PC_SpeechPick({ s }) {
  const noun = s.isTts ? "voice" : "model";
  return (
    <div className="col" style={{ gap: 14, minWidth: 0 }} ref={s.rootRef} data-testid="pc-speech-pick">
      <window.PC_PageFocus n={s.handoff.n} to={s.handoff.to} rootRef={s.rootRef} />
      <window.PC_PageHead title={(s.isTts ? "Voices" : "Models") + " found at " + s.providerId}
        sub={"✓ Connected; " + window.PC_countOf(s.choices.length, noun, noun + "s") + " listed"} tone="ok" />
      <PC_RadioList name={s.isTts ? "Voice" : "Model"} testid="pc-pick" options={s.choices.map((c) => ({ value: c, label: c }))} value={s.pick}
        onChange={(next) => { if (!s.out) s.setPick(next); }} />
      <div className="field-help" data-testid="pc-pick-help">
        {s.isTts ? "The voice the provider speaks with when a call does not name one." : "The model the provider transcribes with when a call does not name one."}
      </div>
    </div>
  );
}

// ---- the page ----------------------------------------------------------------------------------------------------------------------------------------------

function PC_SpeechRegister({ klass, editRow, excludeKinds, frame, blocked, onSaved, onClose }) {
  const api = window.primerApi;
  const editing = !!editRow;
  const isTts = klass.key === "tts";
  const w = window.PC_useWizard(klass.plural, blocked);
  const typeMap = w.types.types || {};
  const kinds = w.types.types ? window.PC_servedKinds(klass.key, typeMap, excludeKinds) : [];

  const [draft, setDraft] = React.useState(() => (editRow ? window.PC_draftFromRow(klass.plural, editRow) : {}));
  const [choices, setChoices] = React.useState([]);
  const [pick, setPick] = React.useState("");
  const found = React.useRef(null);
  const asked = React.useRef("");

  // ---- the draft, as it is shown and as it is sent
  const kind = draft.provider || kinds[0] || "";
  const shape = typeMap[kind] || null;
  const configFields = shape ? (shape.config_fields || []).map(window.PC_normalizeField) : [];
  const hasUrl = configFields.some((f) => f.key === "url");
  // The row fields this page shows: the default model of a TTS provider (its models are not listed); both defaults in an edit. An ASR provider's model is picked on page 2.
  const shownRowKeys = editing ? Object.keys(PC_SPEECH_ROW[klass.key]) : (isTts ? ["default_model"] : []);
  const rowFields = shape ? (shape.row_fields || []).map(window.PC_normalizeField).filter((f) => shownRowKeys.indexOf(f.key) >= 0) : [];
  const limitFields = window.PC_limitFieldsFor(klass.key, kind);
  const eff = editing ? draft : window.PC_fillDraft(draft, kind, configFields, limitFields);
  const missing = configFields.filter((f) => f.required && window.PC_isBlank((eff.config || {})[f.key])).concat(rowFields.filter((f) => f.required && window.PC_isBlank(eff[f.key])));
  const idProblem = editing ? null : window.PC_idProblem(eff.id);
  const idTyped = eff.id != null && String(eff.id) !== "";
  const limitProblems = shape && shape.limits ? window.PC_limitsProblems(limitFields, eff.limits || {}) : {};
  const formOk = !!shape && !idProblem && missing.length === 0 && Object.keys(limitProblems).length === 0;
  const typesFailed = !!w.types.error && !w.types.types;
  const loadingShape = !w.types.types && !w.types.error;
  const kindNotServed = !!w.types.types && !shape;

  // The body of the provider as the form's wire rules make it (ticket T3: an emptied optional box is omitted, never sent as ""; `limits` is always there). A new provider sends the
  // limits its kind shows and max_concurrency (PC_shownLimits) and the choice made on page 2; an edit sends the draft, which is the whole row.
  const bodyWith = (choice) => {
    if (!formOk) return null;
    const body = window.PC_submittable(eff, shape, kind);
    if (editing) return body;
    if (body.limits) body.limits = window.PC_shownLimits(body.limits, limitFields);
    if (isTts) body.default_voice = choice;
    else body.default_model = choice;
    return body;
  };
  // What Next sends: the connection, {provider, config}, and nothing else (the server's `_test` reads only those). The answer to a request is only used while the draft still makes
  // this same request: a URL fixed under a request that is out would otherwise be registered with the voices of the server it replaced.
  const connection = !editing && formOk ? window.PC_submittable(eff, shape, kind) : null;
  const probeBody = connection ? { provider: connection.provider, config: connection.config || {} } : null;
  const probeKey = probeBody ? JSON.stringify(probeBody) : "";
  asked.current = probeKey;
  const providerId = editing ? editRow.id : eff.id;

  const setField = (scope, key, value) => {
    if (scope === "config") setDraft({ ...eff, config: { ...(eff.config || {}), [key]: value } });
    else setDraft({ ...eff, [key]: value });
  };
  const setKind = (next) => {
    setDraft({ ...eff, provider: next, config: {} });
    w.setError(null);
  };

  // ---- requests. One of the page's own at a time (`w.out` says which); the overlay's Delete (`blocked`) holds them all off.
  const goNext = async () => {
    if (editing || !probeBody) return;
    // The same connection as the list already on page 2 (the operator went Back and changed something else, the default model or a limit): the list and the choice are kept.
    if (found.current === probeKey && choices.length) {
      w.show(2, "head");
      return;
    }
    if (!w.begin("test")) return;
    const key = probeKey;
    let answer = null;
    let failure = null;
    try {
      answer = await api.apiFetch("POST", "/" + klass.plural + "/_test", probeBody);
    } catch (err) {
      failure = err;
    }
    w.end();
    if (!w.live.current) return;
    if (asked.current !== key) {
      w.setError("The connection changed while the server was answering. Press Next again.");
      return;
    }
    if (failure) {
      w.say(failure);
      return;
    }
    // `_test` answers 200 with `ok: false` and the reason when the connection fails (its text is already scrubbed of the key).
    if (!answer || typeof answer !== "object" || answer.ok !== true) {
      w.setError(answer && answer.error ? String(answer.error) : PC_SPEECH_NO_ANSWER);
      return;
    }
    const listed = PC_speechListed(isTts ? answer.voices : answer.models);
    if (!listed.length) {
      w.setError(isTts ? "The server listed no voices." : "The server listed no models.");
      return;
    }
    found.current = key;
    setChoices(listed);
    setPick("");
    w.show(2, "head");
  };

  const back = () => {
    if (w.outRef.current) return;
    w.show(1, "first");
  };

  const register = async () => {
    if (editing || w.step !== 2 || !pick) return;
    const body = bodyWith(pick);
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
    onSaved(window.PC_savedRow(answer, body));
  };

  const save = async () => {
    if (!editing) return;
    const body = bodyWith(null);
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
    editing, isTts, classKey: klass.key, eff, kind, kinds, showType: kinds.length > 1, typeMap, shape, configFields, rowFields, hasUrl, types: w.types, typesFailed,
    loadingShape, kindNotServed, idProblem, idTyped, limitFields, limitProblems, setField, setKind, retryTypes: w.retryTypes, handoff: w.handoff, rootRef: w.rootRef,
    choices, pick, setPick, providerId, out: w.out,
  };
  // The footer's buttons carry keys of their own. React keeps the DOM node of an element with the same type and key at the same place, so without them the Back button of the choice
  // page and the Cancel of the connection page (both the first thing in the left of the footer) would be ONE node: a focus left on Back would stay on a button now called Cancel, and
  // a second Enter would close the overlay. With them a page change replaces the button, the focus falls to <body>, and PC_PageFocus hands it to the new body.
  const cancel = <Btn key="cancel" kind="ghost" data-testid="pc-register-cancel" onClick={onClose}>Cancel</Btn>;

  if (w.step === 2 && !editing) {
    return frame({
      body: <PC_SpeechPick s={view} />,
      footerLeft: <Btn key="back" kind="ghost" data-testid="pc-register-back" onClick={back} blocked={held || w.out !== null}>{"‹ Back"}</Btn>,
      footerRight: (
        <Btn key="register" kind="primary" data-testid="pc-register-submit" onClick={register} busy={w.out === "register"} blocked={held} disabled={!pick}>Register</Btn>
      ),
      error: w.error,
      busy: w.out !== null,
    });
  }

  if (editing) {
    return frame({
      body: <PC_SpeechSettings s={view} />,
      footerLeft: cancel,
      footerRight: (
        <Btn key="save" kind="primary" data-testid="pc-register-submit" onClick={save} busy={w.out === "save"} blocked={held} disabled={!formOk}>Save</Btn>
      ),
      error: w.error,
      busy: w.out !== null,
    });
  }

  return frame({
    body: <PC_SpeechSettings s={view} />,
    footerLeft: cancel,
    footerRight: (
      <Btn key="next" kind="primary" data-testid="pc-register-next" onClick={goNext} busy={w.out === "test"} blocked={held} disabled={!formOk}>{"Next ›"}</Btn>
    ),
    error: w.error,
    busy: w.out !== null,
  });
}

window.PC_REGISTER_PAGES = Object.assign(window.PC_REGISTER_PAGES || {}, { stt: PC_SpeechRegister, tts: PC_SpeechRegister });
Object.assign(window, { PC_SpeechRegister, PC_RadioList });
