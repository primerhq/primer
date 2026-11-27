/* global React, Banner, PK_Field, PK_Fold, PK_Segmented */
// A single profile's model settings (PMS Task 7, design 7): the section the single-profile overlay draws under the profile's fields. It is the
// provider's own settings for the profile's `config`: the Reasoning control (the model's levels plus Custom, which opens a value field datalisted
// from the catalogue's suggestions), one typed control per catalogue setting fetched from /v1/llm_providers/<provider>/model_settings when the
// chosen provider changes (a select or a free entry for an enum, a number with its bounds, a toggle, sub-controls for an object's fields), a
// "Server body fields" box on a wire with body passthrough, and the Extended (JSON) fold. One extended object is the source of truth: the typed
// controls and the fold both read and write it, an empty control removes its key, and the fold keeps showing everything, including keys the
// catalogue does not name. The form pushes its draft (the wire's reasoning value, the extended object, the client-side errors) to the overlay on
// every change, so the overlay's Save gate and the body it sends read it in one place (model-profiles.jsx keeps the wiring; this file draws the
// section and loads before it in ui/index.html). A catalogue read that fails is a Banner and the JSON fold still works, so a profile is never
// uneditable because of the read; a catalogue with no settings says so instead of drawing an empty box.
function MPS_helpText(setting) {
  var text = setting.description || "";
  if (setting.wire_path && setting.wire_path !== setting.key) text += " Sent to " + setting.wire_path + ".";
  if (setting.set_by_reasoning) text += " The Reasoning control also writes this.";
  return text;
}

// The client-side errors of the draft's three writable pieces (spec section 4's rule for the custom value, the two JSON boxes' object rule,
// which MP_parseExtended holds): a non-null entry keeps Save off.
function MPS_errors(choice, custom, extText, bodyText) {
  return {
    reasoning: (choice === "custom") ? MP_customReasoningError(custom) : null,
    extended: MP_parseExtended(extText).error || null,
    body: MP_parseExtended(bodyText).error || null,
  };
}

// The form's draft from the stored config: the stored values passed the server's own check, so the client starts with no errors of its own.
function MPS_initialDraft(config) {
  var c = config || {};
  var reasoning = c.reasoning == null ? null : c.reasoning;
  var extended = c.extended && typeof c.extended === "object" && !Array.isArray(c.extended) ? c.extended : {};
  return { reasoning: reasoning, extended: extended, errors: { reasoning: null, extended: null, body: null } };
}

// One control for one setting, by the catalogue's type: a select over the values for a closed enum, a free entry datalisted from the values for
// an open one, a number with its bounds, a toggle, a text entry for a string, and nothing the server would refuse unasked. `onWrite(undefined)`
// removes the key (an empty control is an unset one); `onWrite(value)` sets it.
function MPS_settingControl(props) {
  var setting = props.setting, value = props.value, testid = props.testid, onWrite = props.onWrite;
  if (setting.type === "enum") {
    var values = (setting.values || []).map(String);
    if (!setting.open) {
      var current = (typeof value === "string" && values.indexOf(value) >= 0) ? value : "";
      return (
        <select data-testid={testid} value={current}
          onChange={function (e) { onWrite(e.target.value === "" ? undefined : e.target.value); }}>
          <option value="" />
          {values.map(function (v) { return <option key={v} value={v}>{v}</option>; })}
        </select>
      );
    }
    return (
      <React.Fragment>
        <input data-testid={testid} value={value == null ? "" : String(value)} list={"mps-enum-" + setting.key} autoComplete="off" spellCheck={false}
          onChange={function (e) { onWrite(e.target.value === "" ? undefined : e.target.value); }} />
        <datalist id={"mps-enum-" + setting.key} data-testid={"mps-enum-" + setting.key}>
          {values.map(function (v) { return <option key={v} value={v} />; })}
        </datalist>
      </React.Fragment>
    );
  }
  if (setting.type === "integer" || setting.type === "number") {
    return (
      <input type="number" step={setting.type === "integer" ? 1 : undefined}
        min={setting.min != null ? setting.min : undefined} max={setting.max != null ? setting.max : undefined}
        data-testid={testid} value={value == null ? "" : String(value)}
        onChange={function (e) { onWrite(e.target.value === "" ? undefined : Number(e.target.value)); }} />
    );
  }
  if (setting.type === "boolean") {
    return <input type="checkbox" data-testid={testid} checked={value === true}
      onChange={function (e) { onWrite(e.target.checked ? true : undefined); }} />;
  }
  return (
    <input type="text" data-testid={testid} value={value == null ? "" : String(value)} autoComplete="off" spellCheck={false}
      onChange={function (e) { onWrite(e.target.value === "" ? undefined : e.target.value); }} />
  );
}

// One field for one catalogue setting: an object setting draws a sub-control for each of its fields (an open object also takes unknown keys,
// which the Extended (JSON) box is the way into), every other type its own control. The label is the key the profile's `extended` holds.
function MPS_settingField(props) {
  var setting = props.setting, extended = props.extended, writeSetting = props.writeSetting, writeSubSetting = props.writeSubSetting;
  if (setting.type === "object") {
    var inner = (extended[setting.key] && typeof extended[setting.key] === "object" && !Array.isArray(extended[setting.key])) ? extended[setting.key] : {};
    return (
      <PK_Field label={setting.key} span="full"
        help={MPS_helpText(setting) + (setting.open ? " Other keys through the Extended (JSON) box." : "")}>
        <div data-testid={"mp-setting-" + setting.key} role="group">
          {(setting.fields || []).map(function (f) {
            return <MPS_settingControl key={f.key} setting={f} value={inner[f.key]} testid={"mp-setting-" + setting.key + "-" + f.key}
              onWrite={function (v) { writeSubSetting(setting.key, f.key, v); }} />;
          })}
        </div>
      </PK_Field>
    );
  }
  return (
    <PK_Field label={setting.key} help={MPS_helpText(setting)}>
      <MPS_settingControl setting={setting} value={extended[setting.key]} testid={"mp-setting-" + setting.key}
        onWrite={function (v) { writeSetting(setting.key, v); }} />
    </PK_Field>
  );
}

function MPS_SettingsForm(props) {
  // props: provider (the chosen provider's id, or none), initial (the overlay's current draft: {reasoning, extended}),
  // managed (the fold starts open and cannot be toggled), sectionTestid, onDraft (the draft the overlay gates and sends).
  var init = props.initial || {};
  var reasoning0 = init.reasoning == null ? null : init.reasoning;
  var isCustom0 = reasoning0 != null && typeof reasoning0 === "object";
  var choice0 = isCustom0 ? "custom" : String(reasoning0 || "");
  var custom0 = isCustom0 ? String(reasoning0.custom == null ? "" : reasoning0.custom) : "";
  var extended0 = init.extended || {};
  var extText0 = MP_extendedText(extended0);
  var bodyText0 = extended0.body != null ? MP_extendedText(extended0.body) : "";

  const [choice, setChoice] = React.useState(choice0);
  const [custom, setCustom] = React.useState(custom0);
  const [extended, setExtended] = React.useState(extended0);
  const [extText, setExtText] = React.useState(extText0);
  const [bodyText, setBodyText] = React.useState(bodyText0);
  const [cat, setCat] = React.useState(null);
  const [catState, setCatState] = React.useState("idle");   // idle (no provider) | loading | ready | error
  const [catError, setCatError] = React.useState(null);

  // The provider's own settings (Task 5's route): fetched on open and when the chosen provider changes.
  React.useEffect(function () {
    if (!props.provider) { setCat(null); setCatState("idle"); return; }
    var dead = false;
    setCatState("loading");
    setCatError(null);
    window.primerApi.apiFetch("GET", "/v1/llm_providers/" + encodeURIComponent(props.provider) + "/model_settings")
      .then(function (row) { if (!dead) { setCat(row || null); setCatState("ready"); } })
      .catch(function (err) { if (!dead) { setCatError(window.primerApi.readRefusal(err).message); setCatState("error"); } });
    return function () { dead = true; };
  }, [props.provider]);

  // Every change pushes the draft to the overlay, which gates Save on it and builds the save body from it.
  function commit(nextChoice, nextCustom, nextExtended, nextExtText, nextBodyText) {
    setChoice(nextChoice);
    setCustom(nextCustom);
    setExtended(nextExtended);
    setExtText(nextExtText);
    setBodyText(nextBodyText);
    if (props.onDraft) props.onDraft({
      reasoning: MP_reasoningBody(nextChoice, nextCustom),
      extended: nextExtended,
      errors: MPS_errors(nextChoice, nextCustom, nextExtText, nextBodyText),
    });
  }

  function pick(choice_) { commit(choice_, custom, extended, extText, bodyText); }
  function typeCustom(e) { commit(choice, e.target.value, extended, extText, bodyText); }
  function writeSetting(key, value) {
    var next = Object.assign({}, extended);
    if (value === undefined) delete next[key]; else next[key] = value;
    commit(choice, custom, next, MP_extendedText(next), bodyText);
  }
  function writeSubSetting(key, field, value) {
    var inner = (extended[key] && typeof extended[key] === "object" && !Array.isArray(extended[key])) ? Object.assign({}, extended[key]) : {};
    if (value === undefined) delete inner[field]; else inner[field] = value;
    var next = Object.assign({}, extended);
    if (Object.keys(inner).length === 0) delete next[key]; else next[key] = inner;
    commit(choice, custom, next, MP_extendedText(next), bodyText);
  }
  // The fold's text is free while it is typed; a parse that is an object is the one the typed controls read from, and the controls' writes
  // re-serialize it, so the two views of the one object stay in step.
  function typeExt(e) {
    var text = e.target.value;
    var parsed = MP_parseExtended(text);
    commit(choice, custom, parsed.error ? extended : parsed.value, text, bodyText);
  }
  function typeBody(e) {
    var text = e.target.value;
    var parsed = MP_parseExtended(text);
    if (parsed.error) { commit(choice, custom, extended, extText, text); return; }
    var next = Object.assign({}, extended);
    if (Object.keys(parsed.value).length === 0) delete next.body; else next.body = parsed.value;
    commit(choice, custom, next, MP_extendedText(next), text);
  }

  var settings = (cat && Array.isArray(cat.settings)) ? cat.settings : [];
  var suggestions = (cat && cat.custom_reasoning && Array.isArray(cat.custom_reasoning.suggestions)) ? cat.custom_reasoning.suggestions : [];
  var extError = MP_parseExtended(extText).error;
  var bodyError = MP_parseExtended(bodyText).error;
  var customError = (choice === "custom") ? MP_customReasoningError(custom) : null;

  return (
    <PK_Section title="MODEL SETTINGS" testid={props.sectionTestid} cols={1}>
      <PK_Field label="Reasoning" help={MP_REASONING_HELP}>
        <PK_Segmented testid="mp-single-reasoning" ariaLabel="Reasoning" options={MP_REASONING_OPTIONS} value={choice} onChange={pick} />
      </PK_Field>
      {choice === "custom" ? (
        <PK_Field label="Custom reasoning value" help="Written to the wire as-is; the suggestions are the wire's own values." error={customError || undefined}>
          <input value={custom} list="mp-reasoning-suggestions" data-testid="mp-reasoning-custom" autoComplete="off" spellCheck={false} onChange={typeCustom} />
          <datalist id="mp-reasoning-suggestions" data-testid="mp-reasoning-suggestions">
            {suggestions.map(function (s) { return <option key={String(s)} value={String(s)} />; })}
          </datalist>
        </PK_Field>
      ) : null}
      {props.provider && catState === "loading" ? (
        <div className="field-help" data-testid="mp-settings-loading">Loading the provider's settings…</div>
      ) : null}
      {props.provider && catState === "error" ? (
        <div data-testid="mp-settings-error">
          <Banner kind="error" title="The provider's settings could not be loaded" detail={catError} />
        </div>
      ) : null}
      {props.provider && catState === "ready" && settings.length === 0 ? (
        <div className="field-help" data-testid="mp-settings-empty">This provider takes no extra settings</div>
      ) : null}
      {props.provider && catState === "ready" && settings.length > 0
        ? settings.map(function (s) { return <MPS_settingField key={s.key} setting={s} extended={extended} writeSetting={writeSetting} writeSubSetting={writeSubSetting} />; })
        : null}
      {props.provider && cat && cat.body_passthrough ? (
        <PK_Field label="Server body fields (sent as-is)"
          help="Sent beside the fields primer sets itself; the request fields primer owns cannot be set here."
          error={bodyError || undefined}>
          <textarea value={bodyText} rows={4} spellCheck={false} className="mono" data-testid="mp-settings-body" onChange={typeBody} />
        </PK_Field>
      ) : null}
      {/* A folded box hides its error, so the fold's label says the JSON is not an object (Save is off until it is). */}
      <PK_Fold label={extError ? "Extended settings (not a JSON object)" : "Extended settings"} testid="mp-single-extended-fold"
        open={props.managed || Object.keys(extended).length > 0}>
        <PK_Field label="Extended (JSON)" help={MP_EXTENDED_HELP} error={extError || undefined}>
          <textarea value={extText} rows={6} spellCheck={false} className="mono" data-testid="mp-single-extended" onChange={typeExt} />
        </PK_Field>
      </PK_Fold>
    </PK_Section>
  );
}
