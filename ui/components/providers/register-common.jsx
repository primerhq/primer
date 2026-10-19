/* global window, React, Btn */
// register-common.jsx - shared register pieces for the provider catalog,
// the Register provider overlay and the setup wizard (plan 02, Tasks 6
// and 9): the profile id helpers (seam I-5); the provider classes in the
// sidebar order; the built-in rows and kinds; the kind and flavor labels;
// the limits each class's adapters read, with their section and checks;
// the id field and its rule; the shared _types hook; and the small helpers
// the register pages share. Loaded after provider-form.jsx (PC_typesKey)
// and the platform kit, before the overlay and the catalog (ui/index.html).
//
// A profile id is what the console's URL paths and the agents reference,
// so it must match the id rule: the first character alphanumeric, the
// rest alphanumeric plus ".", "_" and "-". A model name like
// "anthropic/claude-3.5:beta" (OpenRouter-style) carries "/" and ":",
// which the rule rejects, so a raw concatenation built ids the server
// could not address (the wizard's own GET 404'd on the "/" it contained).

// The id rule the helpers produce.
const PC_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

// One part of a profile id. Every character outside [A-Za-z0-9._-]
// becomes "-", runs of "-" become one "-", then leading and trailing
// "-", "." and "_" are trimmed, so a part is either "" or matches
// PC_ID_RE by itself.
function PC_idPart(text) {
  const s = String(text == null ? "" : text)
    .replace(/[^A-Za-z0-9._-]/g, "-")
    .replace(/-+/g, "-");
  return s.replace(/^[-._]+/, "").replace(/[-._]+$/, "");
}

// The profile id for one (provider, model) pair: the two parts joined
// with "--", or "" when either part is "" (an empty id omits the key,
// and the server makes one: primer/model/common.py).
function PC_profileIdFor(providerId, modelName) {
  const a = PC_idPart(providerId);
  const b = PC_idPart(modelName);
  if (!a || !b) return "";
  return a + "--" + b;
}

window.PC_ID_RE = PC_ID_RE;
window.PC_idPart = PC_idPart;
window.PC_profileIdFor = PC_profileIdFor;

// ---- the provider classes ----------------------------------------------------------------------------------------------------------------------------
//
// Every provider class the console registers, in the sidebar order of design 4.1, then Web Fetch, which has no Providers entry of its own (nav: false: the
// Web page manages fetchers through the overlay, design 8.2). `form` says how the overlay registers one: "wizard" classes get pages of their own
// (PC_REGISTER_PAGES, plan 02 Tasks 12 to 15), "form" classes the schema-driven PC_ProviderForm, "panel" classes the create form their own file keeps
// (semantic-search.jsx, workspaces/providers.jsx, channels.jsx), whose detail page stays (design 20 Q5). `noun` names one provider ("Channel provider").
// `invalidate`: the class's registry caches an adapter per row (Invalidate is offered); `draftTest`: a draft _test route exists, so Test is offered (a
// Test that answers 405 never is, design 6.7); `profiles`: the class carries LLM profiles. `detail` is read when it is needed, because the panel files load
// after this one. Each `iconPath` is one 14x14 stroke path (the kit's icon convention), drawn after the shared Icon glyph of the same idea where one fits.
// The keys and plurals are written as literal properties: source pins read them.
const PC_CLASSES = [
  { key: "llm", label: "LLM", noun: "LLM", plural: "llm_providers", form: "wizard", nav: true,
    iconPath: "M2 2.5h10v9H2Z M4 5h1.5 M4 7.2h4 M4 9.4h2.5", invalidate: true, profiles: true },
  { key: "embedding", label: "Embedding", noun: "Embedding", plural: "embedding_providers", form: "wizard", nav: true,
    iconPath: "M2 7a1.5 1.5 0 1 0 3 0 1.5 1.5 0 1 0-3 0 M5.5 3.5a1.5 1.5 0 1 0 3 0 1.5 1.5 0 1 0-3 0 M9 10.5a1.5 1.5 0 1 0 3 0 1.5 1.5 0 1 0-3 0 "
      + "M4.6 6.1 6 4.4 M7.9 4.8l2 4.5",
    invalidate: true },
  { key: "ssp", label: "Semantic Search Storage", noun: "Semantic Search Storage", plural: "ssp", form: "panel", nav: true,
    iconPath: "M2.5 3.5c0-.8 2-1.5 4.5-1.5s4.5.7 4.5 1.5-2 1.5-4.5 1.5-4.5-.7-4.5-1.5Z M2.5 3.5v7c0 .8 2 1.5 4.5 1.5s4.5-.7 4.5-1.5v-7 "
      + "M2.5 7c0 .8 2 1.5 4.5 1.5s4.5-.7 4.5-1.5",
    detail: () => window.SSPDetail, detailProp: "sspId" },
  { key: "cross_encoder", label: "Cross Encoding", noun: "Cross Encoding", plural: "cross_encoder_providers", form: "wizard", nav: true,
    iconPath: "M2 4.5h9 M9 2.5l2 2-2 2 M12 9.5H3 M5 7.5l-2 2 2 2", invalidate: true },
  { key: "workspace", label: "Workspace", noun: "Workspace", plural: "workspace_providers", form: "panel", nav: true,
    iconPath: "M2 4.5 7 2l5 2.5v5L7 12 2 9.5Z M7 7v5 M2 4.5 7 7l5-2.5",
    detail: () => window.WorkspaceProviderDetail },
  { key: "web_search", label: "Web Search", noun: "Web Search", plural: "web_search_providers", form: "form", nav: true,
    iconPath: "M6.2 2.2a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z M9.2 9.2 12.5 12.5", draftTest: true },
  { key: "channel", label: "Channels", noun: "Channel", plural: "channel_providers", form: "panel", nav: true,
    iconPath: "M2 2.5h10v7H6L3 12V9.5H2Z",
    detail: () => window.ChannelProviderDetail },
  // Plan 09 adds draftTest with the artifact storage _test route.
  { key: "artifact_storage", label: "Artifact Store", noun: "Artifact Store", plural: "artifact_storage_providers", form: "form", nav: true,
    iconPath: "M3 1.5h5.5L11 4v8.5H3Z M8.5 1.5V4H11" },
  { key: "tts", label: "TTS", noun: "TTS", plural: "tts_providers", form: "wizard", nav: true,
    iconPath: "M2 5.5v3h2.3L7.5 11V3L4.3 5.5Z M9.6 5.2a2.6 2.6 0 0 1 0 3.6 M11.1 3.6a5 5 0 0 1 0 6.8" },
  { key: "stt", label: "ASR", noun: "ASR", plural: "stt_providers", form: "wizard", nav: true,
    iconPath: "M5.3 3.2a1.7 1.7 0 0 1 3.4 0v3.4a1.7 1.7 0 0 1-3.4 0Z M3.2 6.6a3.8 3.8 0 0 0 7.6 0 M7 10.4v2.1 M5.2 12.5h3.6" },
  { key: "web_fetch", label: "Web Fetch", noun: "Web Fetch", plural: "web_fetch_providers", form: "form", nav: false,
    iconPath: "M7 1.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Z M1.5 7h11 M7 1.5c2.4 2.6 2.4 8.4 0 11-2.4-2.6-2.4-8.4 0-11", draftTest: true },
];

function PC_classByKey(key) {
  for (let i = 0; i < PC_CLASSES.length; i++) {
    if (PC_CLASSES[i].key === key) return PC_CLASSES[i];
  }
  return null;
}

// ---- built-in rows and kinds ---------------------------------------------------------------------------------------------------------------------
//
// The rows the server seeds and protects (design 6.8): DELETE answers 403, so the console never offers Delete on them. This is the console's ONE copy of
// the reserved ids (the server's: RESERVED_*_IDS in primer/api/registries/provider_registry.py, primer/model/web_search.py, primer/model/web_fetch.py, and
// DEFAULT_ARTIFACT_PROVIDER_ID in primer/artifact/factory.py); tests/ui/test_provider_builtin_ids_match_server.py holds it to them. No LLM row is built in.
// The local workspace provider also refuses a PUT (403, primer/api/routers/workspaces.py), so it never offers Edit either.
const PC_BUILTIN_IDS = {
  embedding: ["huggingface"],
  ssp: ["lance"],
  cross_encoder: ["huggingface-ce"],
  workspace: ["local"],
  web_search: ["DuckDuckGo"],
  web_fetch: ["local"],
  artifact_storage: ["artifact-storage-default"],
};
const PC_READONLY_IDS = { workspace: ["local"] };

function PC_isBuiltin(classKey, id) {
  return (PC_BUILTIN_IDS[classKey] || []).indexOf(id) >= 0;
}

function PC_isReadonly(classKey, id) {
  return (PC_READONLY_IDS[classKey] || []).indexOf(id) >= 0;
}

// The built-in kinds: their one row is seeded, and a second is never offered as a type to create (design 6.1 and 8.2).
const PC_BUILTIN_KINDS = { web_search: ["duckduckgo"], web_fetch: ["local"] };

// ---- labels --------------------------------------------------------------------------------------------------------------------------------------------
//
// The server says which kinds exist (GET /{plural}/_types); these maps only rename them, and a kind's place in its map is its place in the Type select.
// A served kind with no name here keeps the served label, then its raw value (PC_kindLabel), and is offered after the named ones (PC_servedKinds).
const PC_KIND_LABELS = {
  llm: { openchat: "OpenAI (completions)", openresponses: "OpenAI (responses)", anthropic: "Anthropic", gemini: "Gemini", ollama: "Ollama",
    openrouter: "OpenRouter" },
  embedding: { openai: "OpenAI", gemini: "Gemini", huggingface: "Hugging Face" },
  cross_encoder: { huggingface: "Hugging Face" },
  tts: { openai: "OpenAI-compatible" },
  stt: { openai: "OpenAI-compatible" },
  web_search: { tavily: "Tavily", firecrawl: "Firecrawl", exa: "Exa", duckduckgo: "DuckDuckGo" },
  web_fetch: { jina: "Jina", firecrawl: "Firecrawl", exa: "Exa", local: "Local" },
  ssp: { pgvector: "pgvector", pgvectorscale: "pgvectorscale", lance: "LanceDB" },
  workspace: { local: "Local", container: "Container", kubernetes: "Kubernetes" },
  channel: { slack: "Slack", telegram: "Telegram", discord: "Discord" },
  artifact_storage: { db: "Database", filesystem: "Filesystem", s3: "S3" },
};

const PC_FLAVOR_LABELS = { openai: "OpenAI", lmstudio: "LM Studio", vllm: "vLLM", ollama: "Ollama", other: "Other" };

// `served` is the kind's own _types entry ({label, config_fields, ...}), when the caller has it.
function PC_kindLabel(classKey, kind, served) {
  const named = (PC_KIND_LABELS[classKey] || {})[kind];
  if (named) return named;
  if (served && typeof served.label === "string" && served.label) return served.label;
  return String(kind == null ? "" : kind);
}

// The kinds a class offers as types to create: the served kinds, minus the built-in kinds and `excludeKinds`, in PC_KIND_LABELS order, unknown kinds last.
function PC_servedKinds(classKey, types, excludeKinds) {
  const served = Object.keys(types || {});
  const drop = (PC_BUILTIN_KINDS[classKey] || []).concat(excludeKinds || []);
  const kept = served.filter((k) => drop.indexOf(k) < 0);
  const order = Object.keys(PC_KIND_LABELS[classKey] || {});
  return order.filter((k) => kept.indexOf(k) >= 0).concat(kept.filter((k) => order.indexOf(k) < 0));
}

// ---- limits ------------------------------------------------------------------------------------------------------------------------------------------
//
// The Limits fields each class's adapters READ, so the limits box never shows a control the server ignores (design 2, anti-goal 1). Every LLM adapter is
// wrapped in RetryingLLM (primer/api/registries/provider_registry.py), which reads all seven; the OpenAI and Gemini embedders read max_concurrency and
// connect_timeout_seconds (primer/embedder/openai.py, gemini.py); the Hugging Face embedder and the cross encoder read max_concurrency only
// (primer/embedder/huggingface.py, primer/cross_encoder/huggingface.py); the ASR adapter reads the two timeouts (primer/speech/openai_asr.py) and the TTS
// adapter the timeouts, retries and backoffs (primer/speech/openai_tts.py). A "<class>:<kind>" entry wins over the class's own.
const PC_LIMIT_FIELDS = {
  llm: ["max_concurrency", "request_timeout_seconds", "max_retries", "retry_backoff_seconds", "connect_timeout_seconds", "total_timeout_seconds",
    "retry_backoff_max_seconds"],
  "embedding:openai": ["max_concurrency", "connect_timeout_seconds"],
  "embedding:gemini": ["max_concurrency", "connect_timeout_seconds"],
  "embedding:huggingface": ["max_concurrency"],
  cross_encoder: ["max_concurrency"],
  stt: ["request_timeout_seconds", "connect_timeout_seconds"],
  tts: ["request_timeout_seconds", "connect_timeout_seconds", "max_retries", "retry_backoff_seconds", "retry_backoff_max_seconds"],
};

function PC_limitFieldsFor(classKey, kind) {
  return (PC_LIMIT_FIELDS[classKey + ":" + kind] || PC_LIMIT_FIELDS[classKey] || []).slice();
}

// The Limits model's own defaults (primer/model/providers/_shared.py). The two other timeouts default to none.
const PC_LIMIT_DEFAULTS = { request_timeout_seconds: 300, max_retries: 2, retry_backoff_seconds: 0.5, retry_backoff_max_seconds: 8 };

// The limits a new provider starts with: max_concurrency 1 ALWAYS (the model requires it, and the speech adapters, which never read it, still have to send
// one), plus the defaults of the fields the class shows.
function PC_initialLimits(fields) {
  const out = { max_concurrency: 1 };
  (fields || []).forEach((f) => {
    if (Object.prototype.hasOwnProperty.call(PC_LIMIT_DEFAULTS, f)) out[f] = PC_LIMIT_DEFAULTS[f];
  });
  return out;
}

const PC_LIMIT_TIMEOUTS = ["request_timeout_seconds", "connect_timeout_seconds", "total_timeout_seconds"];

function PC_limitEmpty(v) {
  return v === undefined || v === null || (typeof v === "string" && v.trim() === "");
}

function PC_limitNumber(v) {
  if (typeof v === "number") return v;
  if (typeof v === "string" && v.trim() !== "") return Number(v);
  return NaN;
}

// What is wrong with the limits a person typed, per field, in the words shown under the box. Only the fields the class shows are checked.
function PC_limitsProblems(fields, limits) {
  const out = {};
  const l = limits || {};
  (fields || []).forEach((f) => {
    const v = l[f];
    const n = PC_limitNumber(v);
    if (f === "max_concurrency") {
      if (!Number.isInteger(n) || n < 1) out[f] = "Enter a whole number of 1 or more";
    } else if (f === "max_retries") {
      if (!Number.isInteger(n) || n < 0) out[f] = "Enter a whole number of 0 or more";
    } else if (f === "retry_backoff_seconds" || f === "retry_backoff_max_seconds") {
      if (PC_limitEmpty(v)) out[f] = "Required";
      else if (!Number.isFinite(n) || n < 0) out[f] = "Enter a number of 0 or more";
    } else if (PC_LIMIT_TIMEOUTS.indexOf(f) >= 0) {
      if (!PC_limitEmpty(v) && (!Number.isFinite(n) || n < 0)) out[f] = "Enter a number of 0 or more";
    }
  });
  return out;
}

const PC_LIMIT_LABELS = {
  max_concurrency: "Max concurrency",
  request_timeout_seconds: "Request timeout (s)",
  max_retries: "Retries",
  retry_backoff_seconds: "Backoff delay (s)",
  connect_timeout_seconds: "Connect timeout (s)",
  total_timeout_seconds: "Total timeout (s)",
  retry_backoff_max_seconds: "Max backoff (s)",
};
const PC_LIMIT_MAIN = ["max_concurrency", "request_timeout_seconds", "max_retries", "retry_backoff_seconds"];
const PC_LIMIT_MORE = ["connect_timeout_seconds", "total_timeout_seconds", "retry_backoff_max_seconds"];

// What an empty timeout means. For the LLM and embedding adapters an empty timeout is no limit (Limits' None); the speech adapters fall back to their own
// defaults instead (primer/speech/openai_asr.py and openai_tts.py: 300 s to read, 5 s to connect), so they say that.
const PC_LIMIT_HELP = {
  request_timeout_seconds: "Empty means no stall timeout",
  connect_timeout_seconds: "Empty means no connect limit",
  total_timeout_seconds: "Empty means no limit",
};
const PC_SPEECH_LIMIT_HELP = {
  request_timeout_seconds: "Empty means the default, 300 s",
  connect_timeout_seconds: "Empty means the default, 5 s",
};

function PC_limitHelp(classKey, field) {
  if ((classKey === "stt" || classKey === "tts") && PC_SPEECH_LIMIT_HELP[field]) return PC_SPEECH_LIMIT_HELP[field];
  return PC_LIMIT_HELP[field];
}

// The limits box: "LIMITS AND RETRIES" when the class retries, else "LIMITS"; the main fields in a two-column grid, the other timeouts in a "More
// timeouts" fold. A box shows `value`'s number (no `value` yet: the initial limits, which are what a create sends); an emptied timeout writes null (no
// limit), an emptied required box writes "" for the check to name, and a typed number is stored as a number. `classKey` (optional) picks what an empty
// timeout means for that class's adapters.
function PC_LimitsSection({ fields, value, onChange, problems, testid, classKey }) {
  const PK_Section = window.PK_Section;
  const PK_Grid = window.PK_Grid;
  const PK_Fold = window.PK_Fold;
  const PK_Field = window.PK_Field;
  const shown = fields || [];
  const limits = value || PC_initialLimits(shown);
  const errors = problems || {};
  const main = PC_LIMIT_MAIN.filter((f) => shown.indexOf(f) >= 0);
  const more = PC_LIMIT_MORE.filter((f) => shown.indexOf(f) >= 0);
  const set = (field, raw) => {
    const next = { ...limits };
    if (raw === "" || raw == null) next[field] = PC_LIMIT_TIMEOUTS.indexOf(field) >= 0 ? null : "";
    else next[field] = Number.isFinite(Number(raw)) ? Number(raw) : raw;
    onChange(next);
  };
  const box = (field) => {
    const whole = field === "max_concurrency" || field === "max_retries";
    const v = limits[field];
    return (
      <PK_Field key={field} label={PC_LIMIT_LABELS[field]} help={PC_limitHelp(classKey, field)} error={errors[field]}
        required={field === "max_concurrency"}>
        <input type="number" data-testid={"pc-limit:" + field} min={field === "max_concurrency" ? "1" : "0"} step={whole ? "1" : "any"}
          aria-required={field === "max_concurrency" ? "true" : undefined}
          value={v == null ? "" : String(v)} onChange={(e) => set(field, e.target.value)} />
      </PK_Field>
    );
  };
  return (
    <PK_Section title={shown.indexOf("max_retries") >= 0 ? "LIMITS AND RETRIES" : "LIMITS"} cols={2} testid={testid}>
      {main.map(box)}
      {more.length ? (
        <PK_Fold label="More timeouts" testid={testid + "-more"}>
          <PK_Grid cols={2}>{more.map(box)}</PK_Grid>
        </PK_Fold>
      ) : null}
    </PK_Section>
  );
}

// ---- the id --------------------------------------------------------------------------------------------------------------------------------------------

// What is wrong with a provider id a person typed (the rule PC_ID_RE states; the overlay asks it of every new id), or null.
function PC_idProblem(id) {
  const s = id == null ? "" : String(id);
  if (!s.trim()) return "Required";
  if (!PC_ID_RE.test(s)) return "Start with a letter or digit; then letters, digits, '.', '_' or '-'";
  return null;
}

// The id box: the rule under it while it can be typed, "Fixed after creation" once it cannot (read-only, so it can still be read and copied).
function PC_IdField({ value, onChange, locked, problem, testid }) {
  const PK_Field = window.PK_Field;
  return (
    <PK_Field label="Id" help={locked ? "Fixed after creation" : "Letters, digits, '.', '_' and '-'"} error={problem || undefined} required={!locked}>
      <input type="text" data-testid={testid} value={value == null ? "" : String(value)} readOnly={!!locked}
        aria-required={locked ? undefined : "true"} autoComplete="off" spellCheck={false}
        onChange={(e) => { if (!locked) onChange(e.target.value); }} />
    </PK_Field>
  );
}

// ---- the types of a class ------------------------------------------------------------------------------------------------------------------------------

// GET /{plural}/_types under the form's own cache key (PC_typesKey, provider-form.jsx), so a page and the form read ONE entry. The entry is shared, and
// useResource only copies an entry's state to whoever joins it: a request that failed reaches every page opened after it with nothing asking again
// (nothing polls), so mounted on a failed entry with nothing in flight the hook asks once (the rule the form and the Register menu follow).
function PC_useTypes(plural) {
  const api = window.primerApi;
  const res = api.useResource(
    window.PC_typesKey(plural),
    (signal) => api.apiFetch("GET", `/${plural}/_types`, null, { signal }),
    { pollMs: null },
  );
  React.useEffect(() => {
    if (res.error && !res.data && !res.loading && typeof res.refetch === "function") res.refetch();
  }, []);
  return {
    types: res.data || null,
    loading: !!res.loading,
    error: res.error || null,
    retry: () => {
      if (!res.loading && typeof res.refetch === "function") res.refetch();
    },
  };
}

// ---- models and merges ---------------------------------------------------------------------------------------------------------------------------------

// Discovered embedding models: names with "embed" in them first (a stable order otherwise) and ticked; the rest unticked (design 13.2). A model keeps
// what discovery said about it.
function PC_embedFirst(models) {
  const rows = (models || []).map((m) => ({ ...m, ticked: /embed/i.test(String((m && m.name) || "")) }));
  return rows.filter((r) => r.ticked).concat(rows.filter((r) => !r.ticked));
}

function PC_isPlainObject(x) {
  return x != null && typeof x === "object" && !Array.isArray(x);
}

// An edit body built from what a form shows, laid over the row it edits, so a field the form does not show is sent back unchanged (master Review Focus 1):
// values in `built` win, keys only in `existing` are kept, nested plain objects merge, except that a nested object whose `kind` (or `type`) changed is
// replaced whole (its old fields belong to the old kind); arrays, scalars and null in `built` replace; an undefined in `built` changes nothing.
function PC_mergeKeep(existing, built) {
  if (!PC_isPlainObject(existing)) return PC_isPlainObject(built) ? { ...built } : built;
  const out = { ...existing };
  Object.keys(built || {}).forEach((k) => {
    const b = built[k];
    const e = existing[k];
    if (b === undefined) return;
    if (PC_isPlainObject(b) && PC_isPlainObject(e)) {
      const switched = ("kind" in b && "kind" in e && b.kind !== e.kind) || ("type" in b && "type" in e && b.type !== e.type);
      out[k] = switched ? { ...b } : PC_mergeKeep(e, b);
    } else {
      out[k] = b;
    }
  });
  return out;
}

// ---- what the wizard pages share ------------------------------------------------------------------------------------------------------------------------
//
// The Embedding, Cross Encoding, TTS and ASR pages (register-models.jsx, register-speech.jsx; plan 02 Tasks 14 and 15) are each two pages in one: the connection, then a list
// to choose from. What they have in common is here, so that each file holds only what is its own: the state of the request that is out and of the step, the hand-off of the focus
// when a page is left, one served config field as a kit field, the lines that say the fields of a kind are loading or could not be loaded, the heading of the second page, the
// draft a new provider starts from, and the words of a count.

// The console's names for the config fields the servers serve (the server says which exist; these only name them, as PC_KIND_LABELS does the kinds).
const PC_SERVED_LABELS = { flavor: "Flavor", url: "URL", api_key: "API key", token: "API token" };

// What an edit says under a secret: the box holds the MASK the server served, and a mask left as it is keeps the stored secret.
const PC_KEPT_HELP = "Stored secrets are shown masked; leave it as is to keep it.";

// "1 model", "3 models": the words of a count.
function PC_countOf(n, one, many) {
  return n + " " + (n === 1 ? one : many);
}

function PC_isBlank(v) {
  return v === undefined || v === null || String(v).trim() === "";
}

// The row a page hands the overlay (and through it the caller) once it has saved: the row the server answered. The request body is never its stand-in, because it holds every
// secret as it was typed and the caller (a list that reloads, a parent that keeps the row) has no use for a key in the clear. A server that answers no row gives the caller the
// id, which is all it needs to look the row up.
function PC_savedRow(answer, sent) {
  if (answer && typeof answer === "object") return answer;
  return sent && sent.id != null ? { id: sent.id } : {};
}

// The state every wizard page keeps: the step it shows, which request of its own is out (`out`; one at a time, and none while the overlay's own request, a Delete, is out:
// `blocked`), the line that says what went wrong, and the hand-off the body is asked for when a page is left. `begin(what)` answers whether the request may start,
// `end()` ends it, `say(err)` puts a refusal on the line in the one reader's words, `show(step, to)` shows the other page and asks the body to take the focus to `to` ("head": the
// heading of the second page, "first": the first field of the first). `live` is a ref that goes false when the page leaves: a request answered after that must not touch the
// state. The page also reads the kinds its class serves (PC_useTypes) here, with the Try again that asks once more and, when it works, asks the body to take the focus to the first
// field (the button that was pressed is gone by then).
function PC_useWizard(plural, blocked) {
  const api = window.primerApi;
  const types = window.PC_useTypes(plural);
  const [step, setStep] = React.useState(1);
  const [out, setOut] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [handoff, setHandoff] = React.useState({ n: 0, to: "first" });
  const outRef = React.useRef(null);
  const live = React.useRef(true);
  const rootRef = React.useRef(null);
  const retried = React.useRef(false);
  React.useEffect(() => {
    live.current = true;
    return () => { live.current = false; };
  }, []);
  const haveTypes = !!types.types;
  React.useEffect(() => {
    if (!haveTypes || !retried.current) return;
    retried.current = false;
    setHandoff((h) => ({ n: h.n + 1, to: "first" }));
  }, [haveTypes]);
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
  const show = (next, to) => {
    setError(null);
    setStep(next);
    setHandoff((h) => ({ n: h.n + 1, to }));
  };
  const retryTypes = () => {
    if (types.loading) return;
    retried.current = true;
    types.retry();
  };
  return { types, step, out, outRef, error, setError, handoff, rootRef, live, begin, end, say, show, retryTypes };
}

// What a new provider of a kind starts with in `config`: the flavor the model defaults to (OTHER), so that the Flavor box shows a value and the same value is sent. A page that
// showed the first option while sending nothing would be showing what it does not send.
function PC_startConfig(configFields) {
  const out = {};
  (configFields || []).forEach((f) => {
    if (f.key !== "flavor" || !f.options || !f.options.length) return;
    out.flavor = f.options.indexOf("other") >= 0 ? "other" : f.options[0];
  });
  return out;
}

// A new provider's draft with its defaults written in: the kind (the Type select shows the first served kind until one is picked), the config the kind starts from and the limits a
// new provider starts from. Everything the page shows, and everything it sends, is read from this, so the two cannot differ. An EDIT is never filled in: its draft is the served
// row, and a field the page does not show is not the page's to add.
function PC_fillDraft(draft, kind, configFields, limitFields) {
  return {
    ...draft,
    provider: kind,
    config: { ...PC_startConfig(configFields), ...(draft.config || {}) },
    limits: draft.limits || window.PC_initialLimits(limitFields),
  };
}

// Hands the focus to something in the body once the body has been drawn, when the focus has been lost. `n` changes when a hand-off is wanted; `to` says where: "head" is the
// heading of the second page, anything else the first field a person can type in. A focus the operator put somewhere is left where it is. This is a component INSIDE the body, not an
// effect of the page, on purpose: the page's effects run BEFORE its new body is in the DOM (register-overlay.jsx, "the shell's slots"), so an effect there would see the old page's
// button still focused, do nothing, and let the focus fall to <body>, outside the dialog. The unit harness has no DOM and no focus: the rule is exercised there with a document of its
// own, and tests/ui_e2e/test_register_models_speech_focus_journey.py drives it in a browser.
function PC_PageFocus({ n, to, rootRef }) {
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

// The heading of a second page ("Models found at <id>" and one line under it), which can take the focus (PC_PageFocus "head"). `tone` ("ok" or "warn") colours the second line.
function PC_PageHead({ title, sub, tone }) {
  return (
    <div className="pc-import-head" tabIndex={-1} data-pc-focus="true" data-testid="pc-import-head">
      <div className="pc-import-title">{title}</div>
      <div className="pc-import-sub" data-tone={tone || undefined}>{sub}</div>
    </div>
  );
}

// One served config field of a kind as a kit field: an enum is a select of the served options (a "Not set" only while no choice has been made: a choice is never taken back), any
// other type an input. The label is the console's name for the field (PC_SERVED_LABELS), then the served one; `label` and `help` are the page's own words, and a `help` of null says
// there is none. A secret in an edit says that a mask left as it is keeps the stored secret.
function PC_ServedField({ field, value, onChange, label, help, editing, span }) {
  const PK_Field = window.PK_Field;
  const f = field;
  const testid = "pc-field:" + f.key;
  let control;
  if (f.type === "enum") {
    control = (
      <select data-testid={testid} value={value == null ? "" : String(value)} aria-required={f.required ? "true" : undefined} onChange={(e) => onChange(e.target.value)}>
        {f.required || value ? null : <option value="">Not set</option>}
        {(f.options || []).map((opt) => (
          <option key={opt} value={opt}>{(f.key === "flavor" && window.PC_FLAVOR_LABELS[opt]) || opt}</option>
        ))}
      </select>
    );
  } else {
    control = (
      <input type={f.type === "password" || f.type === "url" || f.type === "number" ? f.type : "text"} data-testid={testid} value={value == null ? "" : String(value)}
        placeholder={f.placeholder || undefined} aria-required={f.required ? "true" : undefined} autoComplete={f.type === "password" ? "off" : undefined}
        spellCheck={f.type === "password" || f.type === "url" ? false : undefined} onChange={(e) => onChange(e.target.value)} />
    );
  }
  const own = help === undefined ? f.help : help;
  const said = f.type === "password" && editing ? [own, PC_KEPT_HELP].filter(Boolean).join(" ") : own;
  return (
    <PK_Field label={label || PC_SERVED_LABELS[f.key] || f.label} help={said || undefined} required={f.required} span={span}>
      {control}
    </PK_Field>
  );
}

// The lines a page draws while the fields of its kind are loading, when they could not be loaded (with a Try again that stays focusable while it is out: `busy`, not `disabled`),
// and when the install does not serve the kind. `types` is the PC_useTypes answer; the page's own `loading`, `failed` and `notServed` say which applies.
function PC_TypesNotice({ loading, failed, notServed, kind, types, onRetry }) {
  return (
    <React.Fragment>
      {loading ? (
        <div className="muted text-sm" role="status" data-testid="pc-register-loading">Loading the fields for this kind…</div>
      ) : null}
      {failed ? (
        <div className="nv-form-error" role="alert" data-testid="pc-register-types-error">
          {"Could not load the fields for this kind: " + window.primerApi.readRefusal(types.error, String(types.error)).message}
        </div>
      ) : null}
      {failed ? (
        <div>
          <Btn kind="ghost" data-testid="pc-register-types-retry" onClick={onRetry} busy={!!types.loading}>
            {types.loading ? "Trying again…" : "Try again"}
          </Btn>
        </div>
      ) : null}
      {notServed ? (
        <div className="nv-form-error" role="alert" data-testid="pc-register-kind-missing">
          {kind
            ? 'This install does not serve the kind "' + kind + '", so the form cannot show its fields.'
            : "This install serves no kind for this class, so the form cannot show its fields."}
        </div>
      ) : null}
    </React.Fragment>
  );
}

Object.assign(window, {
  PC_SERVED_LABELS, PC_countOf, PC_isBlank, PC_savedRow, PC_startConfig, PC_fillDraft, PC_useWizard, PC_PageFocus, PC_PageHead, PC_ServedField, PC_TypesNotice,
});

Object.assign(window, {
  PC_CLASSES, PC_classByKey, PC_BUILTIN_IDS, PC_READONLY_IDS, PC_isBuiltin, PC_isReadonly, PC_BUILTIN_KINDS,
  PC_KIND_LABELS, PC_FLAVOR_LABELS, PC_kindLabel, PC_servedKinds,
  PC_LIMIT_FIELDS, PC_limitFieldsFor, PC_initialLimits, PC_limitsProblems, PC_LimitsSection,
  PC_idProblem, PC_IdField, PC_useTypes, PC_embedFirst, PC_mergeKeep,
});
