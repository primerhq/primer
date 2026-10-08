"""System > Setup > Configure provider: step 1 names what failed, at the field it failed on (ADM-02, ADM-03 and ADM-05 of the 2026-10-08 admin review).

The wizard is the first thing a new operator sees, and it answered every failure with "Could not reach that provider", printed pydantic's dump for a mistyped address, hinted
Ollama's address for every provider type and disabled "Connect and list models" without saying why. This journey drives the real step 1 against the REAL
``POST /v1/llm_providers/_discover_models``, so the wording the classifier reads is the backend's own.

The instance's own setup state is not what is under test (a shared server may already have a provider, which hides the "Configure provider" button), so three READS are
stubbed: ``GET /v1/setup/state`` (a missing provider, so the button is there) and the two list reads the wizard resumes from (empty, so it opens at step 1). Every probe is a
real request, none of them can succeed, and so nothing is ever saved.
"""

from __future__ import annotations

import http.server
import json
import re
import threading

from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")

_STATE = {
    "complete": False,
    "predicates": [{"key": "llm_provider", "ok": False, "label": "An LLM provider", "detail": "No provider is configured yet."}],
}


def _stub_reads(page: Page) -> None:
    def setup_state(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_STATE))

    def empty_list(route):
        if route.request.method != "GET":
            route.continue_()
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": [], "total": 0}))

    page.route(re.compile(r".*/v1/setup/state$"), setup_state)
    page.route(re.compile(r".*/v1/(llm_providers|model_profiles)\?limit=200$"), empty_list)


def _open_step_one(page: Page, console_url: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own normalisation), so wait for it and
    navigate once more if the Setup page is not there after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id("nv-sys-setup-page")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", "system:setup")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            break
        except AssertionError:
            if attempt == 2:
                raise
    page.get_by_test_id("nv-sys-setup-fix:llm_provider").click()
    expect(page.locator("#setup-type")).to_be_visible(timeout=15_000)


def _connect(page: Page) -> None:
    page.get_by_role("button", name="Connect and list models").click()


def _title(page: Page):
    return page.locator(".auth-banner .title")


def test_step_one_names_what_failed_at_the_field_it_failed_on(page: Page, console_url: str) -> None:
    _stub_reads(page)
    probes: list[str] = []
    page.on("request", lambda r: probes.append(r.url) if r.method == "POST" and r.url.endswith("/_discover_models") else None)
    _open_step_one(page, console_url)
    url = page.locator("#setup-url")

    # ADM-03: the hint follows the provider type.
    expect(url).to_have_attribute("placeholder", "https://api.openai.com/v1")
    page.locator("#setup-type").select_option("ollama")
    expect(url).to_have_attribute("placeholder", "http://localhost:11434")
    page.locator("#setup-type").select_option("openchat")
    expect(url).to_have_attribute("placeholder", "https://api.openai.com/v1")

    # ADM-05: Connect is enabled with the Base URL empty, and pressing it says what is missing without asking the server.
    expect(page.get_by_role("button", name="Connect and list models")).to_be_enabled()
    _connect(page)
    expect(page.get_by_test_id("setup-url-error")).to_contain_text("https://api.openai.com/v1", timeout=5_000)
    expect(_title(page)).to_have_text("Enter the server's address")
    assert probes == [], "an empty Base URL must be named in the page, not sent to the server"

    # ADM-02: a mistyped address is an address problem, under the field, with no pydantic text.
    url.fill("not a url")
    expect(page.get_by_test_id("setup-url-error")).to_have_count(0)
    _connect(page)
    expect(_title(page)).to_have_text("That address is not valid", timeout=15_000)
    expect(page.get_by_test_id("setup-url-error")).to_contain_text("full URL")
    shown = page.locator(".setup-steps").inner_text()
    for leak in ("pydantic", "validation error", "LLMProvider", "[type="):
        assert leak not in shown, f"{leak!r} is shown to the operator"
    assert len(probes) == 1

    # The one case the old title was right for: a server that is not there.
    url.fill("http://127.0.0.1:1/v1")
    _connect(page)
    expect(_title(page)).to_have_text("Could not reach that provider", timeout=15_000)
    expect(page.get_by_test_id("setup-url-error")).to_have_count(0)

    # A hosted provider needs its key: named on press, under the key field.
    page.locator("#setup-type").select_option("anthropic")
    expect(page.locator("#setup-key")).to_have_attribute("placeholder", re.compile("required", re.I))
    _connect(page)
    expect(page.get_by_test_id("setup-key-error")).to_be_visible(timeout=5_000)
    expect(_title(page)).to_have_text("Enter the API key")
    assert len(probes) == 2, "a missing key must not be sent to the server either"


class _Upstream(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible server that wants a key: GET /v1/models answers 401, GET /boom/models answers 500."""

    def do_GET(self):  # noqa: N802
        self.send_response(500 if self.path.startswith("/boom/") else 401)
        self.end_headers()

    def log_message(self, *args):  # noqa: D401
        pass


def test_a_server_that_needs_a_key_and_a_base_url_with_credentials(page: Page, console_url: str) -> None:
    """Follow-up round of the lead review: a blank key refused with 401 says the provider needs one (under the key field), editing the field answers the failure, a typed key
    that is refused is a rejected key, and credentials in the Base URL are never echoed back in the error."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        _stub_reads(page)
        _open_step_one(page, console_url)
        url = page.locator("#setup-url")
        key = page.locator("#setup-key")

        url.fill(f"http://127.0.0.1:{port}/v1")
        _connect(page)
        expect(_title(page)).to_have_text("This provider needs an API key", timeout=15_000)
        expect(page.get_by_test_id("setup-key-error")).to_be_visible()

        # Editing the field answers that failure: the banner that named it goes with the message under the field.
        key.fill("sk-wrong")
        expect(page.get_by_test_id("setup-key-error")).to_have_count(0)
        expect(_title(page)).to_have_count(0)

        _connect(page)
        expect(_title(page)).to_have_text("The provider rejected the API key", timeout=15_000)
        expect(page.get_by_test_id("setup-key-error")).to_have_count(0)

        # The Base URL carries credentials and the server answers 500: the address is shown, the credentials are not.
        url.fill(f"http://user:pass@127.0.0.1:{port}/boom")
        _connect(page)
        expect(_title(page)).to_have_text("The provider answered with an error", timeout=15_000)
        shown = page.locator(".setup-steps").inner_text()
        assert "500" in shown and "127.0.0.1" in shown
        assert "user:pass" not in shown and "pass@" not in shown, "the credentials of the Base URL were echoed back"
        # The server masks the userinfo as [REDACTED]@ (ticket 01a11c0d-dd9a) and the banner drops that too: only the address is left.
        assert "REDACTED" not in shown, "the server's mask was printed into the banner"
    finally:
        server.shutdown()
        server.server_close()
