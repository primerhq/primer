"""The first-run wizard names what failed, at the field it failed on (ADM-02, ADM-03 and the wizard half of ADM-05 of the 2026-10-08 admin review).

Three things were wrong on step 1:

* ADM-02: every failure of the probe was titled "Could not reach that provider", and a validation error was printed as raw pydantic text with an external link (``Draft provider
  failed validation: 1 validation error for LLMProvider url ... For further information visit https://errors.pydantic.dev/...``). A mistyped address is not an unreachable
  provider, a rejected key is not either, and the operator cannot act on a pydantic dump.
* ADM-03: the Base URL placeholder was Ollama's address for every provider type, including the default "OpenAI-compatible", and the API key placeholder said "leave blank for
  unauthenticated servers" for Anthropic, Gemini and OpenRouter, which cannot be used without a key.
* ADM-05: "Connect and list models" was disabled while the Base URL was empty and said nothing about why; it is now enabled, and pressing it with a required field empty says which.

``POST /v1/llm_providers/_discover_models`` answers every failure with a 400 and a plain detail, so only the text tells a rejected draft from an unreachable server. The pure
helpers that read it (``SW_probeFailure``, ``SW_missingField``, ``SW_urlHint``, ``SW_keyHint``, ``SW_tidy``) run here in MiniRacer on the real source, and the details they are given are
the ones the REAL backend produces (the draft validator and the probe functions are called for real, against local 127.0.0.1 servers; the hosted providers' discover functions are made
to raise a real ``httpx.HTTPStatusError`` and ``_probe_llm_models`` turns it into its own wording), so a change in the backend's wording or in pydantic's format turns this file red
instead of silently returning the wizard to one generic title. How the component calls them is JSX, so that is a source check (this checkout has no render harness);
``tests/ui_e2e/test_setup_wizard_failures_journey.py`` drives the real page against the real endpoint.

Follow-up round (the lead's review of #546): a Base URL with credentials is never echoed back, a 401 or 403 on a request that carried no key says the provider needs one, the HTTP
status is read before bare words in a body, and editing a field answers a failure that was about a field.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import re
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "setup-wizard.jsx").read_text(encoding="utf-8")
_HELPERS_END = "// ---- end of the resume helpers"

_OPEN_CONTEXTS: list = []

COULD_NOT_REACH = "Could not reach that provider"
REJECTED = "The provider rejected the API key"
NEEDS_KEY = "This provider needs an API key"
ANSWERED = "The provider answered with an error"
LEAKS = ["pydantic", "[type=", "validation error", "LLMProvider", "Draft provider failed", "errors.pydantic.dev", "developer.mozilla.org", "input_value"]


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _ctx():
    from py_mini_racer import MiniRacer

    start = SRC.index("const SETUP_PROVIDER_TYPES = [")
    end = SRC.index(_HELPERS_END)
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(SRC[start:end])
    return ctx


def _call(expression: str):
    return json.loads(_ctx().eval(f"JSON.stringify({expression})"))


def _failure(detail, type_: str = "openchat", key: str = "") -> dict:
    err = "null" if detail is None else json.dumps({"detail": detail})
    return _call(f"SW_probeFailure({err}, {json.dumps(type_)}, {json.dumps(key)})")


def _text(failure: dict) -> str:
    return " ".join(str(failure.get(k) or "") for k in ("title", "detail", "message"))


# ---- the real details ---------------------------------------------------------------------------------------------------------------------------------------------


class _Upstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/rejects/"):
            code = 401
        elif self.path.startswith("/boom/"):
            code = 500
        else:
            code = 404
        self.send_response(code)
        self.end_headers()

    def log_message(self, *args):  # noqa: D401
        pass


class _AlwaysUnauthorized(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(401)
        self.end_headers()

    def log_message(self, *args):  # noqa: D401
        pass


@pytest.fixture(scope="module")
def real() -> dict[str, str]:
    """The detail the real backend raises for each failure class the wizard can hit; nothing leaves 127.0.0.1."""
    import httpx

    from primer.api.routers import providers as providers_router
    from primer.model.except_ import BadRequestError
    from primer.model.provider import LLMProvider

    def draft(provider: str, config: dict) -> str:
        try:
            providers_router._build_stub_provider(
                LLMProvider, provider=provider, config=config, models=[{"name": "_probe", "context_length": 1}],
            )
        except BadRequestError as exc:
            return str(exc)
        raise AssertionError(f"{provider} {config} was expected to fail validation")

    out = {
        "bad_url": draft("openchat", {"url": "not a url"}),
        "no_url": draft("openchat", {}),
        "no_key": draft("openrouter", {}),
    }
    server = http.server.HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    ollama_server = http.server.HTTPServer(("127.0.0.1", 0), _AlwaysUnauthorized)
    threading.Thread(target=ollama_server.serve_forever, daemon=True).start()
    ollama_port = ollama_server.server_address[1]

    def status_error(code: int, body: str) -> httpx.HTTPStatusError:
        request = httpx.Request("GET", "https://api.example.test/v1/models")
        try:
            httpx.Response(code, text=body, request=request).raise_for_status()
        except httpx.HTTPStatusError as exc:
            return exc
        raise AssertionError("expected an error status")

    async def probe(fn, config: dict) -> str:
        try:
            await fn(config)
        except BadRequestError as exc:
            return str(exc)
        raise AssertionError(f"{config} was expected to fail")

    async def hosted(provider: str, discover: str, exc: Exception) -> str:
        async def _raise(*args, **kwargs):
            raise exc

        patch = pytest.MonkeyPatch()
        patch.setattr(providers_router, discover, _raise)
        try:
            return await probe(lambda config: providers_router._probe_llm_models(provider, config), {"api_key": "k"})
        finally:
            patch.undo()

    async def go() -> None:
        out["http_401"] = await probe(providers_router._probe_openai_compatible_models, {"url": f"http://127.0.0.1:{port}/rejects"})
        out["http_404"] = await probe(providers_router._probe_openai_compatible_models, {"url": f"http://127.0.0.1:{port}/missing"})
        out["http_500"] = await probe(providers_router._probe_openai_compatible_models, {"url": f"http://127.0.0.1:{port}/boom"})
        out["http_500_creds"] = await probe(providers_router._probe_openai_compatible_models, {"url": f"http://user:pass@127.0.0.1:{port}/boom"})
        out["refused"] = await probe(providers_router._probe_openai_compatible_models, {"url": "http://127.0.0.1:1"})
        out["ollama_refused"] = await probe(providers_router._probe_ollama_models, {"url": "http://127.0.0.1:1"})
        out["ollama_401"] = await probe(providers_router._probe_ollama_models, {"url": f"http://127.0.0.1:{ollama_port}"})
        out["anthropic_401"] = await hosted("anthropic", "_discover_anthropic_models", status_error(401, '{"type":"error"}'))
        out["anthropic_500_words"] = await hosted("anthropic", "_discover_anthropic_models", status_error(500, "upstream said: unauthorized forbidden"))
        out["gemini_401"] = await hosted("gemini", "_discover_gemini_models", status_error(401, "denied"))
        out["gemini_404"] = await hosted("gemini", "_discover_gemini_models", status_error(404, "no such model list"))
        out["openrouter_403"] = await hosted("openrouter", "_discover_openrouter_models", status_error(403, "blocked by WAF"))
        out["openrouter_network"] = await hosted("openrouter", "_discover_openrouter_models", httpx.ConnectError("All connection attempts failed"))

    try:
        asyncio.run(go())
    finally:
        server.shutdown()
        server.server_close()
        ollama_server.shutdown()
        ollama_server.server_close()
    return out


# ---- ADM-03: the hints follow the provider type --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "type_,expected",
    [
        ("openchat", "https://api.openai.com/v1"),
        ("openresponses", "https://api.openai.com/v1"),
        ("ollama", "http://localhost:11434"),
        ("anthropic", ""),
        ("gemini", ""),
        ("openrouter", ""),
    ],
)
def test_the_base_url_hint_matches_the_provider_type(type_: str, expected: str) -> None:
    assert _call(f"SW_urlHint({json.dumps(type_)})") == expected


@pytest.mark.parametrize("type_", ["openchat", "openresponses", "ollama"])
def test_the_key_hint_of_a_server_that_can_run_without_one_says_so(type_: str) -> None:
    assert _call(f"SW_keyHint({json.dumps(type_)})") == "leave blank for unauthenticated servers"


@pytest.mark.parametrize("type_", ["anthropic", "gemini", "openrouter"])
def test_the_key_hint_of_a_hosted_provider_does_not_offer_to_leave_it_blank(type_: str) -> None:
    hint = _call(f"SW_keyHint({json.dumps(type_)})")
    assert "required" in hint.lower() and "unauthenticated" not in hint


def test_the_inputs_take_their_placeholders_from_the_hints_not_from_a_literal() -> None:
    body = SRC[SRC.index("function SetupWizardSteps("):SRC.index("function SetupWizardGate(")]

    assert "placeholder={SW_urlHint(type)}" in body
    assert "placeholder={SW_keyHint(type)}" in body
    assert 'placeholder="http' not in body, "a literal address would be Ollama's for every provider type again"
    assert 'placeholder="leave blank' not in body


# ---- ADM-05: pressing Connect with a required field empty says which --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("type_,url", [("openchat", ""), ("openchat", "   "), ("openresponses", ""), ("ollama", "")])
def test_an_empty_base_url_is_named_with_an_example_for_the_type(type_: str, url: str) -> None:
    miss = _call(f"SW_missingField({json.dumps(type_)}, {json.dumps(url)}, '')")

    assert miss["field"] == "url"
    assert miss["title"] == "Enter the server's address"
    hint = _call(f"SW_urlHint({json.dumps(type_)})")
    assert hint in miss["message"], "the example must be the one for this provider type"


@pytest.mark.parametrize("type_", ["anthropic", "gemini", "openrouter"])
@pytest.mark.parametrize("key", ["", "   "])
def test_an_empty_key_is_named_for_a_hosted_provider(type_: str, key: str) -> None:
    miss = _call(f"SW_missingField({json.dumps(type_)}, '', {json.dumps(key)})")

    assert miss["field"] == "apiKey"
    assert miss["title"] == "Enter the API key"


@pytest.mark.parametrize(
    "type_,url,key",
    [
        ("openchat", "http://llm.example/v1", ""),
        ("ollama", "http://localhost:11434", ""),
        ("anthropic", "", "sk-ant-1"),
        ("openrouter", "", "sk-or-1"),
    ],
)
def test_nothing_is_named_when_what_the_type_needs_is_there(type_: str, url: str, key: str) -> None:
    assert _call(f"SW_missingField({json.dumps(type_)}, {json.dumps(url)}, {json.dumps(key)})") is None


# ---- ADM-02: the failure is read from the real backend's text ------------------------------------------------------------------------------------------------------


def test_a_mistyped_address_is_an_address_problem_shown_under_the_base_url(real) -> None:
    f = _failure(real["bad_url"])

    assert f["field"] == "url"
    assert f["title"] == "That address is not valid"
    assert "https://api.openai.com/v1" in f["message"], "the example is the one for the selected type"
    assert not any(leak in _text(f) for leak in LEAKS), _text(f)


def test_the_example_under_a_mistyped_address_follows_the_provider_type(real) -> None:
    assert "http://localhost:11434" in _failure(real["bad_url"], "ollama")["message"]


def test_a_type_without_a_base_url_does_not_borrow_ollamas_address_as_its_example(real) -> None:
    f = _failure(real["bad_url"], "anthropic")

    assert f["field"] == "url"
    assert "11434" not in f["message"] and "localhost" not in f["message"]
    assert "http" in f["message"], "it still says what a full URL looks like"


def test_a_missing_address_is_named_not_reported_as_unreachable(real) -> None:
    f = _failure(real["no_url"])

    assert f["field"] == "url"
    assert f["title"] == "Enter the server's address"
    assert not any(leak in _text(f) for leak in LEAKS), _text(f)


def test_a_missing_key_is_shown_under_the_api_key(real) -> None:
    f = _failure(real["no_key"], "openrouter")

    assert f["field"] == "apiKey"
    assert f["title"] == "Enter the API key"
    assert not any(leak in _text(f) for leak in LEAKS), _text(f)


def test_any_other_rejected_setting_is_named_by_field_and_reason_without_the_dump() -> None:
    detail = (
        "Draft provider failed validation: 1 validation error for LLMProvider\nconfig.timeout\n  Input should be a valid number"
        " [type=float_parsing, input_value='abc', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.13/v/float_parsing"
    )
    f = _failure(detail)

    assert f["field"] is None
    assert f["title"] == "Those settings are not valid"
    assert f["detail"] == "config.timeout: Input should be a valid number"
    assert not any(leak in _text(f) for leak in LEAKS), _text(f)


# ---- a refused key: with a key sent, and without one -----------------------------------------------------------------------------------------------------------------


def test_a_rejected_key_is_not_reported_as_an_unreachable_provider(real) -> None:
    f = _failure(real["http_401"], key="sk-wrong")

    assert f["title"] == REJECTED
    assert f["field"] is None
    assert f["detail"].startswith("Check the key and connect again."), f["detail"]
    assert "401" in f["detail"], "the advice is followed by what the server actually said"
    assert not any(leak in _text(f) for leak in LEAKS), _text(f)


def test_a_401_on_a_request_that_carried_no_key_says_the_provider_needs_one(real) -> None:
    """openchat/openresponses/ollama may run without a key; pointed at a server that needs one, a blank key is what the 401 answers."""
    f = _failure(real["http_401"], key="")

    assert f["title"] == NEEDS_KEY
    assert f["field"] == "apiKey", "the error goes under the key field"
    assert "401" in f["detail"]
    assert not any(leak in _text(f) for leak in LEAKS), _text(f)


def test_a_key_made_of_spaces_counts_as_no_key(real) -> None:
    assert _failure(real["http_401"], key="   ")["title"] == NEEDS_KEY


@pytest.mark.parametrize(
    "key,title,field",
    [("k", REJECTED, None), ("", NEEDS_KEY, "apiKey")],
)
def test_a_403_from_a_proxy_keeps_what_it_said_after_the_advice(real, key: str, title: str, field) -> None:
    """A WAF or proxy answering 403 is not necessarily about the key: the operator must still see its own words."""
    f = _failure(real["openrouter_403"], "openrouter", key)

    assert f["title"] == title and f["field"] == field
    assert "blocked by WAF" in _text(f)


def test_the_hosted_providers_own_key_rejections_read_the_same(real) -> None:
    assert _failure(real["anthropic_401"], "anthropic", "k")["title"] == REJECTED
    assert _failure(real["gemini_401"], "gemini", "k")["title"] == REJECTED


def test_ollama_behind_auth_is_a_key_problem_too(real) -> None:
    """The real text is ``ollama probe failed: ResponseError:  (status code: 401)``, which carries no word at all, only the status."""
    assert _failure(real["ollama_401"], "ollama", "k")["title"] == REJECTED
    assert _failure(real["ollama_401"], "ollama", "")["title"] == NEEDS_KEY


# ---- the HTTP status is read before bare words ------------------------------------------------------------------------------------------------------------------------


def test_a_500_whose_body_says_unauthorized_is_still_an_answer_not_a_key_problem(real) -> None:
    f = _failure(real["anthropic_500_words"], "anthropic", "k")

    assert f["title"] == ANSWERED
    assert "500" in f["detail"]


def test_a_404_whose_body_says_forbidden_is_still_an_answer() -> None:
    assert _failure("OpenRouter discover failed: HTTP 404 forbidden by policy", "openrouter", "k")["title"] == ANSWERED


@pytest.mark.parametrize("detail", ["proxy said: Unauthorized", "access Forbidden here", "the key invalid"])
def test_a_denial_in_words_with_no_status_is_still_a_key_problem(detail: str) -> None:
    assert _failure(detail, "openchat", "k")["title"] == REJECTED


@pytest.mark.parametrize("detail", ["unauthorized_access_log is full", "forbiddenness of things", "monkey invalidated the cache"])
def test_the_words_are_anchored_and_do_not_match_inside_other_words(detail: str) -> None:
    assert _failure(detail, "openchat", "k")["title"] == COULD_NOT_REACH


def test_a_404_points_at_the_base_url_path(real) -> None:
    f = _failure(real["http_404"])

    assert f["title"] == ANSWERED
    assert "404" in f["detail"] and "/v1" in f["detail"], f["detail"]
    assert not any(leak in _text(f) for leak in LEAKS), _text(f)


def test_the_v1_hint_is_not_offered_to_a_provider_that_has_no_base_url(real) -> None:
    f = _failure(real["gemini_404"], "gemini", "k")

    assert f["title"] == ANSWERED
    assert "404" in f["detail"] and "/v1" not in f["detail"] and "Base URL" not in f["detail"]


def test_any_other_error_status_is_reported_as_an_answer_not_as_silence(real) -> None:
    f = _failure(real["http_500"])

    assert f["title"] == ANSWERED
    assert "500" in f["detail"]


# ---- a Base URL with credentials is never echoed back -------------------------------------------------------------------------------------------------------------------


def test_the_real_error_message_does_echo_the_credentials_so_the_strip_is_needed(real) -> None:
    """Guards the premise: if httpx stopped echoing the URL, the tests below would pass for nothing."""
    assert "user:pass@" in real["http_500_creds"]


def test_credentials_in_the_base_url_are_not_shown_in_an_error_detail(real) -> None:
    f = _failure(real["http_500_creds"])

    assert f["title"] == ANSWERED
    assert "user:pass" not in f["detail"] and "pass@" not in f["detail"] and "@127.0.0.1" not in f["detail"]
    assert "127.0.0.1" in f["detail"], "the address itself is still shown, so the operator can see what was tried"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("see https://user:pass@host.example/v1/models now", "see https://host.example/v1/models now"),
        ("http://user@host/x", "http://host/x"),
        ("http://user:p@ss@host/x", "http://host/x"),
        ("a http://a:b@one/x and ftp://c:d@two:21/y", "a http://one/x and ftp://two:21/y"),
        ("for url 'http://u:p@127.0.0.1:8/b/models'", "for url 'http://127.0.0.1:8/b/models'"),
        ("no credentials at http://host:80/path?x=1", "no credentials at http://host:80/path?x=1"),
        ("write to someone@example.com about it", "write to someone@example.com about it"),
        ("http://host/path@x", "http://host/path@x"),
    ],
)
def test_tidy_strips_credentials_from_every_url_and_leaves_the_rest(text: str, expected: str) -> None:
    assert _call(f"SW_tidy({json.dumps(text)})") == expected


# ---- unreachable, and the rest -----------------------------------------------------------------------------------------------------------------------------------------


def test_a_refused_connection_keeps_the_unreachable_title(real) -> None:
    """The one case the old title was right for."""
    f = _failure(real["refused"])

    assert f["title"] == COULD_NOT_REACH
    assert f["field"] is None
    assert "All connection attempts failed" in f["detail"]


def test_ollama_refusing_the_connection_is_unreachable_too(real) -> None:
    f = _failure(real["ollama_refused"], "ollama")

    assert f["title"] == COULD_NOT_REACH
    assert "Failed to connect to Ollama" in f["detail"]


def test_a_network_error_from_a_hosted_provider_is_unreachable(real) -> None:
    f = _failure(real["openrouter_network"], "openrouter", "k")

    assert f["title"] == COULD_NOT_REACH
    assert "All connection attempts failed" in f["detail"]


@pytest.mark.parametrize("detail", [None, "", "   "])
def test_no_detail_still_reads_cleanly(detail) -> None:
    f = _failure(detail)

    assert f["title"] == COULD_NOT_REACH
    assert f["detail"] == ""
    assert "undefined" not in _text(f) and "null" not in _text(f)


def test_a_failure_with_only_a_message_uses_it() -> None:
    f = _call('SW_probeFailure({message: "Failed to fetch"}, "openchat", "")')

    assert f["title"] == COULD_NOT_REACH
    assert f["detail"] == "Failed to fetch"


# ---- the component calls them ---------------------------------------------------------------------------------------------------------------------------------


def _steps() -> str:
    return SRC[SRC.index("function SetupWizardSteps("):SRC.index("function SetupWizardGate(")]


def test_connect_is_not_disabled_by_an_empty_field() -> None:
    body = _steps()

    button = re.search(r'<button\s+type="submit"[\s\S]*?>\s*\{busy \?', body)
    assert button, "the step 1 submit button is gone"
    assert "disabled={busy}" in button.group(0)
    assert "needsUrl && !url" not in button.group(0), "an empty required field must be named on press, not silently disable the button"


def test_the_submit_names_a_missing_field_before_it_asks_the_server() -> None:
    body = _steps()
    submit = body[body.index("const submitProvider = async"):body.index("const submitProfile = async")]

    check = re.search(r"const missing = SW_missingField\(type, url, apiKey\);\s*if \(missing\) \{[\s\S]*?return;\s*\}", submit)
    assert check, "the submit must check what the type needs and stop with the field named"
    assert check.start() < submit.index('"/llm_providers/_discover_models"')
    assert "setBusy(true)" in submit and check.start() < submit.index("setBusy(true)"), "a missing field must not leave the button busy"


def test_the_probe_failure_is_read_through_the_tested_function_and_the_old_title_is_gone_from_the_component() -> None:
    body = _steps()

    assert re.search(r"catch \(e2\) \{[\s\S]*?SW_probeFailure\(e2, type, apiKey\)", body), "the probe's failure must be classified by the tested function, with the key that was sent"
    assert COULD_NOT_REACH not in body, "the component must not title every failure itself"


def test_a_failure_about_a_field_is_shown_under_that_field() -> None:
    body = _steps()

    assert "if (failure.field) setFieldErr({ [failure.field]: failure.message });" in body


def test_each_field_shows_its_own_error_marks_itself_invalid_and_clears_it_when_edited() -> None:
    body = _steps()

    for field, testid, setter in (("url", "setup-url-error", "setUrl"), ("apiKey", "setup-key-error", "setApiKey")):
        assert f'"auth-field" + (fieldErr.{field} ? " has-err" : "")' in body, f"the {field} field is marked"
        assert f'data-testid="{testid}"' in body, f"the {field} error has a test id"
        assert f"aria-invalid={{!!fieldErr.{field}}}" in body, f"the {field} input says it is invalid to assistive technology"
        assert re.search(setter + r"\(e\.target\.value\);\s*clearFieldFailure\(\);", body), f"editing the {field} field must answer its failure"
    assert re.search(r"setType\(e\.target\.value\);\s*clearFieldFailure\(\);", body), "a type change moves the example, so it answers the failure too"
    submit = body[body.index("const submitProvider = async"):body.index("const submitProfile = async")]
    assert "setFieldErr({})" in submit, "a new submit starts from no field error"


def test_answering_a_field_failure_clears_the_banner_that_named_it_and_nothing_else() -> None:
    """The banner and the message under the field are one failure. A banner with no field error (the resume notice, an unreachable provider) is not about the field being edited."""
    body = _steps()

    handler = re.search(r"const clearFieldFailure = \(\) => \{[\s\S]*?\n  \};", body)
    assert handler, "clearFieldFailure is gone"
    assert re.search(r"if \(Object\.keys\(fieldErr\)\.length === 0\) return;\s*setFieldErr\(\{\}\);\s*setErr\(null\);", handler.group(0))
