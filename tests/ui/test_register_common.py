"""The shared register pieces (plan 02, Task 9): the class table, built-ins, labels, honest limits, the id rule and the types hook.

``register-common.jsx`` holds what every page of the Register provider overlay reads: ``PC_CLASSES`` (the provider classes in the sidebar order of design
4.1, Web Fetch last and hidden), the built-in rows and kinds, the kind and flavor labels, ``PC_LIMIT_FIELDS`` (the ``Limits`` fields each class's adapters
actually read, so the limits box never offers a control the server ignores: design 2, anti-goal 1), the limits section and its checks, the id field and its
rule, ``PC_useTypes`` (one cache entry with the form), the served-kind list, the embed-first ordering and ``PC_mergeKeep`` (an edit keeps what it did not
show). Everything runs for real in V8 with the real kit (``tests/ui/_register_harness.py``); the ``_types`` maps are what the route functions answer.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.ui._register_harness import COMPONENTS, base_code, context, js, types_of

SRC = (COMPONENTS / "providers" / "register-common.jsx").read_text(encoding="utf-8")

# design 4.1's sidebar order, then the hidden class (plan 02 Task 9's table)
CLASSES = [
    ("llm", "LLM", "LLM", "llm_providers", "wizard", True),
    ("embedding", "Embedding", "Embedding", "embedding_providers", "wizard", True),
    ("ssp", "Semantic Search Storage", "Semantic Search Storage", "ssp", "panel", True),
    ("cross_encoder", "Cross Encoding", "Cross Encoding", "cross_encoder_providers", "wizard", True),
    ("workspace", "Workspace", "Workspace", "workspace_providers", "panel", True),
    ("web_search", "Web Search", "Web Search", "web_search_providers", "form", True),
    ("channel", "Channels", "Channel", "channel_providers", "panel", True),
    ("artifact_storage", "Artifact Store", "Artifact Store", "artifact_storage_providers", "form", True),
    ("tts", "TTS", "TTS", "tts_providers", "wizard", True),
    ("stt", "ASR", "ASR", "stt_providers", "wizard", True),
    ("web_fetch", "Web Fetch", "Web Fetch", "web_fetch_providers", "form", False),
]

LLM_LIMITS = [
    "max_concurrency", "request_timeout_seconds", "max_retries", "retry_backoff_seconds",
    "connect_timeout_seconds", "total_timeout_seconds", "retry_backoff_max_seconds",
]

# What each class's adapters read (plan 02 Task 9; every LLM adapter is wrapped in RetryingLLM, primer/api/registries/provider_registry.py; the embedders,
# the cross encoder and the speech adapters read only these).
LIMIT_FIELDS = {
    "llm": LLM_LIMITS,
    "embedding:openai": ["max_concurrency", "connect_timeout_seconds"],
    "embedding:gemini": ["max_concurrency", "connect_timeout_seconds"],
    "embedding:huggingface": ["max_concurrency"],
    "cross_encoder": ["max_concurrency"],
    "stt": ["request_timeout_seconds", "connect_timeout_seconds"],
    "tts": ["request_timeout_seconds", "connect_timeout_seconds", "max_retries", "retry_backoff_seconds", "retry_backoff_max_seconds"],
}


@pytest.fixture(scope="module")
def code() -> str:
    return base_code()


@pytest.fixture
def ctx(code):
    c = context(code)
    try:
        yield c
    finally:
        c.close()


# ---- the class table ---------------------------------------------------------------------------------------------------------------------------------


def test_classes_follow_the_sidebar_order_with_web_fetch_hidden(ctx) -> None:
    got = js(ctx, "PC_CLASSES.map(function (c) { return [c.key, c.label, c.noun, c.plural, c.form, c.nav]; })")
    assert [tuple(row) for row in got] == CLASSES
    flags = js(ctx, "PC_CLASSES.map(function (c) { return [c.key, !!c.invalidate, !!c.profiles, !!c.draftTest, typeof c.detail, c.detailProp || null]; })")
    assert {row[0]: row[1:] for row in flags} == {
        "llm": [True, True, False, "undefined", None],
        "embedding": [True, False, False, "undefined", None],
        "ssp": [False, False, False, "function", "sspId"],
        "cross_encoder": [True, False, False, "undefined", None],
        "workspace": [False, False, False, "function", None],
        "web_search": [False, False, True, "undefined", None],
        "channel": [False, False, False, "function", None],
        "artifact_storage": [False, False, False, "undefined", None],
        "tts": [False, False, False, "undefined", None],
        "stt": [False, False, False, "undefined", None],
        "web_fetch": [False, False, True, "undefined", None],
    }


def test_the_class_table_is_written_with_literal_keys_and_plurals() -> None:
    """Several source pins read the table as text (``key: "..."``, ``plural: "..."``), as they read ``PROVIDER_CLASSES`` today."""
    for key, _label, _noun, plural, _form, _nav in CLASSES:
        assert f'key: "{key}"' in SRC, key
        assert f'plural: "{plural}"' in SRC, plural


def test_the_detail_pages_are_read_when_they_are_needed(ctx) -> None:
    """``detail`` is a function, so a detail page that loads after this file is still found (the panel files load later in ui/index.html)."""
    ctx.eval("window.SSPDetail = function SSPDetail() { return null; };")
    assert ctx.eval("PC_classByKey('ssp').detail() === window.SSPDetail") is True
    ctx.eval("window.WorkspaceProviderDetail = function A() { return null; }; window.ChannelProviderDetail = function B() { return null; };")
    assert ctx.eval("PC_classByKey('workspace').detail() === window.WorkspaceProviderDetail") is True
    assert ctx.eval("PC_classByKey('channel').detail() === window.ChannelProviderDetail") is True


def test_class_by_key(ctx) -> None:
    assert ctx.eval("PC_classByKey('cross_encoder').plural") == "cross_encoder_providers"
    assert ctx.eval("PC_classByKey('nope') === null") is True
    assert ctx.eval("PC_classByKey('all') === null") is True


def test_every_class_has_a_distinct_icon_path(ctx) -> None:
    """One 14x14 stroke path per class (the kit's icon convention, ``PK_Card`` and ``PK_FormOverlay``'s selector draw it in a 14x14 view box)."""
    paths = js(ctx, "PC_CLASSES.map(function (c) { return c.iconPath; })")
    assert all(isinstance(p, str) and p.strip() for p in paths), paths
    assert len(set(paths)) == len(paths), "two classes share an icon"
    for p in paths:
        assert re.fullmatch(r"[MmLlHhVvCcSsQqTtAaZz0-9.,\s-]+", p), p
        assert all(abs(float(n)) <= 14 for n in re.findall(r"-?\d*\.?\d+", p)), f"a coordinate outside the 14x14 box: {p}"


# ---- built-ins ---------------------------------------------------------------------------------------------------------------------------------------


def test_built_in_and_read_only_rows(ctx) -> None:
    assert ctx.eval("PC_isBuiltin('web_search', 'DuckDuckGo')") is True
    assert ctx.eval("PC_isBuiltin('web_fetch', 'local')") is True
    assert ctx.eval("PC_isBuiltin('workspace', 'local')") is True
    assert ctx.eval("PC_isBuiltin('web_search', 'duckduckgo')") is False, "ids are matched exactly, as the server matches them"
    assert ctx.eval("PC_isBuiltin('llm', 'anything')") is False
    assert ctx.eval("PC_isBuiltin('nope', 'local')") is False
    assert ctx.eval("PC_isReadonly('workspace', 'local')") is True
    assert ctx.eval("PC_isReadonly('web_fetch', 'local')") is False, "a built-in fetcher can still be saved (PUT is not refused)"
    assert js(ctx, "PC_BUILTIN_KINDS") == {"web_search": ["duckduckgo"], "web_fetch": ["local"]}


# ---- labels ------------------------------------------------------------------------------------------------------------------------------------------


def test_kind_labels_rename_the_served_kinds(ctx) -> None:
    assert js(ctx, "Object.keys(PC_KIND_LABELS.llm)") == ["openchat", "openresponses", "anthropic", "gemini", "ollama", "openrouter"]
    assert js(ctx, "PC_KIND_LABELS.llm") == {
        "openchat": "OpenAI (completions)", "openresponses": "OpenAI (responses)", "anthropic": "Anthropic",
        "gemini": "Gemini", "ollama": "Ollama", "openrouter": "OpenRouter",
    }
    assert js(ctx, "PC_KIND_LABELS.embedding") == {"openai": "OpenAI", "gemini": "Gemini", "huggingface": "Hugging Face"}
    assert js(ctx, "PC_KIND_LABELS.web_search") == {"tavily": "Tavily", "firecrawl": "Firecrawl", "exa": "Exa", "duckduckgo": "DuckDuckGo"}
    assert js(ctx, "PC_KIND_LABELS.web_fetch") == {"jina": "Jina", "firecrawl": "Firecrawl", "exa": "Exa", "local": "Local"}
    assert js(ctx, "PC_KIND_LABELS.artifact_storage") == {"db": "Database", "filesystem": "Filesystem", "s3": "S3"}
    assert js(ctx, "PC_FLAVOR_LABELS") == {"openai": "OpenAI", "lmstudio": "LM Studio", "vllm": "vLLM", "ollama": "Ollama", "other": "Other"}
    assert ctx.eval("PC_kindLabel('stt', 'openai')") == "OpenAI-compatible"
    assert ctx.eval("PC_kindLabel('ssp', 'lance')") == "LanceDB"
    # a kind the console has no name for: the served label, then the raw kind
    assert ctx.eval("PC_kindLabel('llm', 'brandnew', { label: 'Brand New API' })") == "Brand New API"
    assert ctx.eval("PC_kindLabel('llm', 'brandnew')") == "brandnew"
    assert ctx.eval("PC_kindLabel('nope', 'x', null)") == "x"


def test_every_kind_the_server_serves_has_a_console_name() -> None:
    """The labels only rename what ``_types`` serves; a served kind with no name falls back to the served label, so this pins that the names are complete
    for the classes that serve ``_types`` today."""
    labels = {
        "llm_providers": "llm", "embedding_providers": "embedding", "cross_encoder_providers": "cross_encoder", "stt_providers": "stt",
        "tts_providers": "tts", "web_search_providers": "web_search", "web_fetch_providers": "web_fetch", "ssp": "ssp",
        "artifact_storage_providers": "artifact_storage",
    }
    named = {
        "llm": {"openchat", "openresponses", "anthropic", "gemini", "ollama", "openrouter"},
        "embedding": {"openai", "gemini", "huggingface"}, "cross_encoder": {"huggingface"}, "stt": {"openai"}, "tts": {"openai"},
        "web_search": {"tavily", "firecrawl", "exa", "duckduckgo"}, "web_fetch": {"jina", "firecrawl", "exa", "local"},
        "ssp": {"pgvector", "pgvectorscale", "lance"}, "artifact_storage": {"db", "filesystem", "s3"},
    }
    for plural, key in labels.items():
        assert set(types_of(plural)) <= named[key], (plural, set(types_of(plural)) - named[key])


# ---- limits ------------------------------------------------------------------------------------------------------------------------------------------


def test_limit_fields_match_what_the_adapters_read(ctx) -> None:
    assert js(ctx, "PC_LIMIT_FIELDS") == LIMIT_FIELDS
    assert js(ctx, "PC_limitFieldsFor('llm', 'openchat')") == LLM_LIMITS
    assert js(ctx, "PC_limitFieldsFor('embedding', 'openai')") == ["max_concurrency", "connect_timeout_seconds"]
    assert js(ctx, "PC_limitFieldsFor('embedding', 'huggingface')") == ["max_concurrency"]
    assert js(ctx, "PC_limitFieldsFor('cross_encoder', 'huggingface')") == ["max_concurrency"]
    assert js(ctx, "PC_limitFieldsFor('tts', 'openai')") == LIMIT_FIELDS["tts"]
    assert js(ctx, "PC_limitFieldsFor('web_search', 'tavily')") == []
    assert js(ctx, "PC_limitFieldsFor('embedding', 'brandnew')") == [], "a kind the map does not name shows no limits rather than a guess"


def test_every_limit_field_is_a_field_of_the_limits_model(ctx) -> None:
    """A rename on the server fails here, not as a box that writes a key the model ignores."""
    from primer.model.providers._shared import Limits

    named = {f for fields in js(ctx, "PC_LIMIT_FIELDS").values() for f in fields}
    assert named <= set(Limits.model_fields), named - set(Limits.model_fields)


def test_initial_limits_always_carry_max_concurrency(ctx) -> None:
    """The model requires ``max_concurrency`` (``primer/model/providers/_shared.py``), so a class that does not show it (speech) still sends 1."""
    from primer.model.providers._shared import Limits

    assert js(ctx, "PC_initialLimits([])") == {"max_concurrency": 1}
    assert js(ctx, "PC_initialLimits(PC_LIMIT_FIELDS.stt)") == {"max_concurrency": 1, "request_timeout_seconds": 300}
    full = js(ctx, "PC_initialLimits(PC_LIMIT_FIELDS.llm)")
    assert full == {"max_concurrency": 1, "request_timeout_seconds": 300, "max_retries": 2, "retry_backoff_seconds": 0.5, "retry_backoff_max_seconds": 8}
    for field, value in full.items():
        if field != "max_concurrency":
            assert Limits.model_fields[field].default == value, f"{field}: the console's default is not the model's"
    for field in ("connect_timeout_seconds", "total_timeout_seconds"):
        assert Limits.model_fields[field].default is None and field not in full


def test_limits_problems(ctx) -> None:
    fields = "PC_LIMIT_FIELDS.llm"
    assert js(ctx, f"PC_limitsProblems({fields}, PC_initialLimits({fields}))") == {}
    bad = {
        "max_concurrency": 0, "max_retries": 1.5, "retry_backoff_seconds": "", "retry_backoff_max_seconds": -1,
        "request_timeout_seconds": -5, "connect_timeout_seconds": None, "total_timeout_seconds": "",
    }
    assert js(ctx, f"PC_limitsProblems({fields}, {json.dumps(bad)})") == {
        "max_concurrency": "Enter a whole number of 1 or more",
        "max_retries": "Enter a whole number of 0 or more",
        "retry_backoff_seconds": "Required",
        "retry_backoff_max_seconds": "Enter a number of 0 or more",
        "request_timeout_seconds": "Enter a number of 0 or more",
    }
    assert js(ctx, f"PC_limitsProblems({fields}, {{ max_concurrency: '', max_retries: '', retry_backoff_seconds: 0, retry_backoff_max_seconds: 0 }})") == {
        "max_concurrency": "Enter a whole number of 1 or more",
        "max_retries": "Enter a whole number of 0 or more",
    }
    # a field the class does not show is not checked: speech does not show max_concurrency
    assert js(ctx, "PC_limitsProblems(PC_LIMIT_FIELDS.stt, { max_concurrency: 0, request_timeout_seconds: 60 })") == {}
    # strings typed into a number box are read as numbers
    assert js(ctx, "PC_limitsProblems(['max_concurrency', 'request_timeout_seconds'], { max_concurrency: '4', request_timeout_seconds: '0.5' })") == {}


LIMITS_HOST = r"""
var CHANGES = [];
function LimitsHost(props) {
  var s = React.useState(props.value);
  return el("div", { "data-testid": "host" }, el(PC_LimitsSection, {
    fields: props.fields, value: s[0], problems: props.problems, testid: "pc-limits", classKey: props.classKey,
    onChange: function (next) { CHANGES.push(next); s[1](next); },
  }));
}
"""


def _mount_limits(ctx, fields: str, value: str = "null", problems: str = "undefined", class_key: str = "undefined") -> None:
    ctx.eval(LIMITS_HOST)
    ctx.eval(f"MR.mount(LimitsHost, {{ fields: {fields}, value: {value}, problems: {problems}, classKey: {class_key} }});")


def test_limits_section_titles_and_fold(ctx) -> None:
    _mount_limits(ctx, "PC_LIMIT_FIELDS.llm", "PC_initialLimits(PC_LIMIT_FIELDS.llm)")
    assert ctx.eval('MR.find("pc-limits").props.role') == "group"
    assert ctx.eval('textOf(MR.find("pc-limits"))').startswith("LIMITS AND RETRIES")
    shown = [e["testid"] for e in js(ctx, 'MR.findAll("pc-limit:").map(function (n) { return { testid: n.props["data-testid"] }; })')]
    assert shown == ["pc-limit:max_concurrency", "pc-limit:request_timeout_seconds", "pc-limit:max_retries", "pc-limit:retry_backoff_seconds"]
    assert ctx.eval('MR.find("pc-limits-more") !== null') is True
    assert "More timeouts" in ctx.eval('textOf(MR.find("pc-limits-more"))')
    ctx.eval('MR.click("pc-limits-more:toggle")')
    shown = [e["testid"] for e in js(ctx, 'MR.findAll("pc-limit:").map(function (n) { return { testid: n.props["data-testid"] }; })')]
    assert shown[4:] == ["pc-limit:connect_timeout_seconds", "pc-limit:total_timeout_seconds", "pc-limit:retry_backoff_max_seconds"]
    labels = {
        "max_concurrency": "Max concurrency", "request_timeout_seconds": "Request timeout (s)", "max_retries": "Retries",
        "retry_backoff_seconds": "Backoff delay (s)", "connect_timeout_seconds": "Connect timeout (s)", "total_timeout_seconds": "Total timeout (s)",
        "retry_backoff_max_seconds": "Max backoff (s)",
    }
    for field, label in labels.items():
        row = js(ctx, f'fieldRow("pc-limits", "pc-limit:{field}")')
        assert row is not None, field
        assert row[0]["type"] == "label" and row[0]["text"].replace(" *", "") == label, row
        assert row[1]["testid"] == f"pc-limit:{field}"
    help_text = {
        "request_timeout_seconds": "Empty means no stall timeout",
        "connect_timeout_seconds": "Empty means no connect limit",
        "total_timeout_seconds": "Empty means no limit",
    }
    for field, text in help_text.items():
        row = js(ctx, f'fieldRow("pc-limits", "pc-limit:{field}")')
        assert row[2]["className"] == "field-help" and row[2]["text"] == text, row
        assert text not in row[0]["text"], "help sits under the control, never in the label"
    assert ctx.eval('MR.find("pc-limit:max_concurrency").props.type') == "number"


def test_a_class_that_reads_only_max_concurrency_gets_one_box_and_no_fold(ctx) -> None:
    _mount_limits(ctx, "PC_limitFieldsFor('embedding', 'huggingface')")
    assert ctx.eval('textOf(MR.find("pc-limits"))').startswith("LIMITS")
    assert "RETRIES" not in ctx.eval('textOf(MR.find("pc-limits"))')
    assert js(ctx, 'MR.findAll("pc-limit:").length') == 1
    assert ctx.eval('MR.find("pc-limits-more") === null') is True
    assert ctx.eval('MR.find("pc-limit:max_concurrency").props.value') == "1", "an unset box shows the initial value it will send"


def test_speech_timeouts_say_what_empty_means_for_speech(ctx) -> None:
    """The speech adapters fall back to 300 s and 5 s for an empty timeout (``primer/speech/openai_asr.py``, ``openai_tts.py``), so the LLM help ("no stall
    timeout") would be false there; the section says what the speech adapters do."""
    _mount_limits(ctx, "PC_LIMIT_FIELDS.stt", class_key="'stt'")
    ctx.eval('MR.click("pc-limits-more:toggle")')
    assert js(ctx, 'fieldRow("pc-limits", "pc-limit:request_timeout_seconds")')[2]["text"] == "Empty means the default, 300 s"
    assert js(ctx, 'fieldRow("pc-limits", "pc-limit:connect_timeout_seconds")')[2]["text"] == "Empty means the default, 5 s"


def test_an_emptied_timeout_writes_null(ctx) -> None:
    _mount_limits(ctx, "PC_LIMIT_FIELDS.llm", "PC_initialLimits(PC_LIMIT_FIELDS.llm)")
    ctx.eval('setValue("pc-limit:request_timeout_seconds", "")')
    assert js(ctx, "CHANGES[CHANGES.length - 1]")["request_timeout_seconds"] is None
    ctx.eval('setValue("pc-limit:request_timeout_seconds", "60")')
    assert js(ctx, "CHANGES[CHANGES.length - 1]")["request_timeout_seconds"] == 60, "a typed number is stored as a number"
    ctx.eval('setValue("pc-limit:max_concurrency", "")')
    last = js(ctx, "CHANGES[CHANGES.length - 1]")
    assert last["max_concurrency"] == "", "an emptied required box stays empty, for the check to name, rather than becoming null"
    assert last["max_retries"] == 2 and last["retry_backoff_max_seconds"] == 8, "the other values are kept"


def test_problems_render_under_their_box(ctx) -> None:
    _mount_limits(ctx, "PC_LIMIT_FIELDS.llm", "PC_initialLimits(PC_LIMIT_FIELDS.llm)", problems="{ max_retries: 'Enter a whole number of 0 or more' }")
    row = js(ctx, 'fieldRow("pc-limits", "pc-limit:max_retries")')
    assert row[-1]["role"] == "alert" and "Enter a whole number of 0 or more" in row[-1]["text"], row
    assert ctx.eval('MR.find("pc-limit:max_retries").props["aria-invalid"]') == "true"


# ---- the id ------------------------------------------------------------------------------------------------------------------------------------------


def test_id_problem(ctx) -> None:
    assert ctx.eval("PC_idProblem('')") == "Required"
    assert ctx.eval("PC_idProblem('   ')") == "Required"
    assert ctx.eval("PC_idProblem(undefined)") == "Required"
    rule = "Start with a letter or digit; then letters, digits, '.', '_' or '-'"
    for bad in ("a/b", "_x", "-x", ".x", "a b", "a:b"):
        assert ctx.eval(f"PC_idProblem({json.dumps(bad)})") == rule, bad
    for good in ("a.b_c-1", "vllm-gx10", "A", "9"):
        assert ctx.eval(f"PC_idProblem({json.dumps(good)}) === null") is True, good


def test_id_field_says_the_rule_and_locks_after_creation(ctx) -> None:
    ctx.eval("var IDS = []; function IdHost(p) { return el('div', { 'data-testid': 'host' }, el(PC_IdField, Object.assign({ testid: 'pc-field:id',"
             " onChange: function (v) { IDS.push(v); } }, p))); }")
    ctx.eval("MR.mount(IdHost, { value: 'a/b', locked: false, problem: PC_idProblem('a/b') })")
    row = js(ctx, 'fieldRow("host", "pc-field:id")')
    assert row[0]["text"].startswith("Id")
    assert [r["text"] for r in row[2:]] == ["Letters, digits, '.', '_' and '-'", "Start with a letter or digit; then letters, digits, '.', '_' or '-'"]
    assert row[3]["role"] == "alert"
    ctx.eval('setValue("pc-field:id", "ok-1")')
    assert js(ctx, "IDS") == ["ok-1"]
    assert ctx.eval('MR.find("pc-field:id").props.readOnly') is not True
    ctx.eval("MR.mount(IdHost, { value: 'served-id', locked: true, problem: null })")
    row = js(ctx, 'fieldRow("host", "pc-field:id")')
    assert [r["text"] for r in row[2:]] == ["Fixed after creation"]
    assert ctx.eval('MR.find("pc-field:id").props.readOnly') is True
    assert ctx.eval('MR.find("pc-field:id").props.value') == "served-id"


# ---- the types hook ----------------------------------------------------------------------------------------------------------------------------------


TYPES_HOST = r"""
var SEEN = null;
function TypesHost(props) {
  var t = PC_useTypes(props.plural);
  SEEN = t;
  return el("div", { "data-testid": "host" }, t.types ? Object.keys(t.types).join(",") : "");
}
"""


def test_use_types_shares_the_forms_cache_key(ctx) -> None:
    """One request, not two: the hook and the form (opened with the catalog's ``typesPath``) read one cache entry, the key ``PC_typesKey`` builds."""
    ctx.eval(TYPES_HOST)
    ctx.eval("MR.mount(TypesHost, { plural: 'llm_providers' })")
    hook_keys = js(ctx, "KEYS")
    ctx.eval("RES['provider-types:/llm_providers/_types'].fetcher('SIG')")
    assert js(ctx, "CALLS") == [{"method": "GET", "path": "/llm_providers/_types", "body": None}], "the hook fetches the URL the key names"
    ctx.eval("KEYS.length = 0; MR.mount(PC_ProviderForm, { plural: 'llm_providers', typesPath: '/llm_providers/_types', value: {}, onChange: function () {} })")
    form_keys = js(ctx, "KEYS")
    assert hook_keys and set(hook_keys) == set(form_keys) == {"provider-types:/llm_providers/_types"}, (hook_keys, form_keys)


def test_use_types_reports_the_answer(ctx) -> None:
    ctx.eval(TYPES_HOST)
    ctx.eval("MR.mount(TypesHost, { plural: 'web_search_providers' })")
    assert js(ctx, "[SEEN.types, SEEN.loading, SEEN.error]") == [None, True, None]
    ctx.eval(f"serve('provider-types:/web_search_providers/_types', {json.dumps(types_of('web_search_providers'))}); MR.rerender();")
    assert ctx.eval('textOf(MR.find("host"))') == "duckduckgo,tavily,firecrawl,exa"
    assert ctx.eval("SEEN.loading") is False


@pytest.mark.parametrize(
    ("state", "asks"),
    [
        pytest.param({"error": True}, 1, id="failed-and-idle"),
        pytest.param({"error": True, "loading": True}, 0, id="failed-but-a-retry-is-out"),
        pytest.param({"error": True, "data": True}, 0, id="failed-with-stale-data"),
        pytest.param({"data": True}, 0, id="healthy"),
        pytest.param({"loading": True}, 0, id="first-load-in-flight"),
    ],
)
def test_use_types_asks_once_when_mounted_on_a_failed_idle_entry(ctx, state: dict, asks: int) -> None:
    """The entry is shared with the form, and ``useResource`` only copies an entry's state to whoever joins it: a request that FAILED would reach every page
    opened after it, with nothing asking again (nothing polls). Mounted on a failed entry with nothing in flight, the hook asks once; never when data is
    there (stale or not), when a request is out, or when nothing failed. The intent of the two register-menu tests Task 20 retires."""
    key = "provider-types:/llm_providers/_types"
    envelope = {"type": "/errors/internal", "title": "Internal Server Error", "status": 500, "detail": "the types could not be read", "instance": "/v1/llm_providers/_types"}
    ctx.eval(f"__entry({json.dumps(key)}).loading = {json.dumps(bool(state.get('loading')))};")
    if state.get("error"):
        ctx.eval(f"__entry({json.dumps(key)}).error = new window.primerApi.ApiError({json.dumps(envelope)});")
    if state.get("data"):
        ctx.eval(f"__entry({json.dumps(key)}).data = {{ anthropic: {{ config_fields: [] }} }};")
    ctx.eval(TYPES_HOST)
    ctx.eval("MR.mount(TypesHost, { plural: 'llm_providers' }); MR.rerender();")
    assert ctx.eval(f"RES[{json.dumps(key)}].refetched") == asks


def test_retry_asks_again_unless_a_request_is_out(ctx) -> None:
    key = "provider-types:/llm_providers/_types"
    ctx.eval(TYPES_HOST)
    ctx.eval(f"serve({json.dumps(key)}, {{}}); MR.mount(TypesHost, {{ plural: 'llm_providers' }});")
    ctx.eval("SEEN.retry(); MR.rerender();")
    assert ctx.eval(f"RES[{json.dumps(key)}].refetched") == 1
    ctx.eval(f"RES[{json.dumps(key)}].loading = true; MR.rerender(); SEEN.retry();")
    assert ctx.eval(f"RES[{json.dumps(key)}].refetched") == 1, "a retry while a request is out sent a second one"


# ---- kinds, models and merges ------------------------------------------------------------------------------------------------------------------------


def test_served_kinds_drop_built_ins_and_follow_label_order(ctx) -> None:
    web_search = json.dumps(types_of("web_search_providers"))
    web_fetch = json.dumps(types_of("web_fetch_providers"))
    llm = json.dumps(types_of("llm_providers"))
    assert js(ctx, f"PC_servedKinds('web_search', {web_search})") == ["tavily", "firecrawl", "exa"]
    assert js(ctx, f"PC_servedKinds('web_fetch', {web_fetch}, ['local'])") == ["jina", "firecrawl", "exa"]
    assert js(ctx, f"PC_servedKinds('web_fetch', {web_fetch})") == ["jina", "firecrawl", "exa"], "local is built in whether or not it is excluded"
    assert js(ctx, f"PC_servedKinds('web_fetch', {web_fetch}, ['exa'])") == ["jina", "firecrawl"]
    assert js(ctx, f"PC_servedKinds('llm', {llm})") == ["openchat", "openresponses", "anthropic", "gemini", "ollama", "openrouter"]
    # a kind the console has no name for is still offered (the server says what exists), after the named ones
    assert js(ctx, "PC_servedKinds('llm', { brandnew: {}, anthropic: {}, openchat: {} })") == ["openchat", "anthropic", "brandnew"]
    # a named kind the server does not serve is never offered
    assert js(ctx, "PC_servedKinds('llm', { anthropic: {} })") == ["anthropic"]
    assert js(ctx, "PC_servedKinds('llm', null)") == []


def test_embed_first_sorts_and_ticks(ctx) -> None:
    models = [{"name": "gpt-4o"}, {"name": "text-embedding-3-small"}, {"name": "nomic-embed-text"}, {"name": "whisper"}]
    got = js(ctx, f"PC_embedFirst({json.dumps(models)})")
    assert [(m["name"], m["ticked"]) for m in got] == [
        ("text-embedding-3-small", True), ("nomic-embed-text", True), ("gpt-4o", False), ("whisper", False),
    ]
    assert js(ctx, "PC_embedFirst([{ name: 'Qwen3-Embedding-8B', context_length: 8192 }])") == [
        {"name": "Qwen3-Embedding-8B", "context_length": 8192, "ticked": True},
    ], "the match ignores case and a model keeps what discovery said about it"
    assert js(ctx, "PC_embedFirst(null)") == []


def test_merge_keep_keeps_unknown_fields_and_replaces_switched_kinds(ctx) -> None:
    existing = {"a": 1, "conn": {"kind": "socket", "socket_path": "/x"}, "keep": "y"}
    assert js(ctx, f"PC_mergeKeep({json.dumps(existing)}, {{ a: 2, conn: {{ kind: 'remote', url: 'u' }} }})") == {
        "a": 2, "conn": {"kind": "remote", "url": "u"}, "keep": "y",
    }
    assert js(ctx, f"PC_mergeKeep({json.dumps(existing)}, {{ conn: {{ kind: 'socket', timeout: 5 }} }})") == {
        "a": 1, "conn": {"kind": "socket", "socket_path": "/x", "timeout": 5}, "keep": "y",
    }, "the same kind merges the nested objects"
    assert js(ctx, "PC_mergeKeep({ config: { type: 'tavily', api_key: 'k', extra: 1 } }, { config: { type: 'exa', api_key: 'e' } })") == {
        "config": {"type": "exa", "api_key": "e"},
    }, "type switches a nested object as kind does"
    assert js(ctx, "PC_mergeKeep({ models: [{ name: 'a' }, { name: 'b' }], n: 1 }, { models: [{ name: 'c' }], n: null })") == {
        "models": [{"name": "c"}], "n": None,
    }, "arrays and scalars replace; null is a value"
    assert js(ctx, "PC_mergeKeep({ a: 1, b: 2 }, { a: undefined, c: 3 })") == {"a": 1, "b": 2, "c": 3}, "an undefined in the built body drops nothing"
    assert js(ctx, "PC_mergeKeep(null, { a: 1 })") == {"a": 1}
