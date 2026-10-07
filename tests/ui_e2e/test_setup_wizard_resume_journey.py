"""First-boot wizard: a reload or an abandoned run never leaves the operator in a dead end (console review 2026-10-08, C-001).

The wizard saves the LLM provider at the end of step 1. It used to keep "which step am I on" in React state only, and the setup
gate re-enters the wizard while the model profile is missing, so a reload at step 2 restarted at step 1 and every retry hit a 409
on the fixed id ``llm-<type>``, reported as "Could not reach that provider". The operator could not get out.

This is a real-DOM journey. The shared ui_e2e install is already set up (``install_is_set_up`` seeds it past the wizard for every
other journey), so the setup API is played by a small in-test fake behind ``page.route``: only the endpoints the wizard and the
auth gate read are answered here, and everything else (the console shell after the gate, static assets) goes to the real server.
That makes the journey independent of the install's own state and of auth being on.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse


from tests.ui_e2e._shell_helpers import open_setup_wizard

_PROVIDER_ID = "llm-openchat"
_MODELS = [{"name": "model-one", "context_length": 8000}, {"name": "model-two", "context_length": 16000}]


class _FakeSetupApi:
    """The slice of the API the first-boot gate and wizard use, with the same conflict semantics as the real routes."""

    def __init__(self, *, providers: list[dict] | None = None, profiles: list[dict] | None = None,
                 provider_answers: bool = True) -> None:
        self.providers = list(providers or [])
        self.profiles = list(profiles or [])
        self.provider_answers = provider_answers      # does the SAVED provider answer a live probe (the llm_provider predicate)
        self.seeded = False
        self.calls: list[str] = []

    # -- derived facts ---------------------------------------------------------------------------------------------------
    @property
    def complete(self) -> bool:
        return bool(self.providers and self.profiles and self.seeded and self.provider_answers)

    def _predicates(self) -> list[dict]:
        provider_ok = bool(self.providers) and self.provider_answers
        return [
            {"key": "llm_provider", "label": "LLM provider responds", "ok": provider_ok,
             "detail": None if provider_ok else "no LLM provider configured" if not self.providers else "connection refused"},
            {"key": "model_profile", "label": "Model profile registered", "ok": bool(self.profiles), "detail": None},
            {"key": "default_workspace", "label": "Default workspace reachable", "ok": True, "detail": None},
            {"key": "operator_agent", "label": "Operator agent seeded", "ok": self.seeded, "detail": None},
            {"key": "builder_agent", "label": "Builder agent seeded", "ok": self.seeded, "detail": None},
            {"key": "system_collection", "label": "System collection exists", "ok": True, "detail": None},
        ]

    # -- the route handler ---------------------------------------------------------------------------------------------
    def handle(self, route) -> None:
        request = route.request
        path = urlparse(request.url).path
        method = request.method
        body = request.post_data_json if request.post_data else None

        def reply(status: int, payload) -> None:
            route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

        def conflict(what: str) -> None:
            reply(409, {"type": "/errors/conflict", "title": "Conflict", "status": 409, "detail": f"{what} already exists"})

        if path == "/v1/auth/status":
            missing = [p["key"] for p in self._predicates() if not p["ok"]]
            return reply(200, {"has_user": True, "authenticated": True, "username": "e2e", "role": "admin",
                               "must_change_password": False, "setup_complete": self.complete, "setup_missing": missing})
        if path == "/v1/setup/state":
            return reply(200, {"complete": self.complete, "predicates": self._predicates()})
        if path == "/v1/setup/seed" and method == "POST":
            self.calls.append("POST /setup/seed")
            self.seeded = True
            return reply(200, {})
        if path == "/v1/llm_providers/_discover_models" and method == "POST":
            self.calls.append("POST /llm_providers/_discover_models")
            return reply(200, {"models": _MODELS})
        if path == "/v1/llm_providers" and method == "GET":
            return reply(200, {"items": self.providers, "total": len(self.providers)})
        if path == "/v1/llm_providers" and method == "POST":
            self.calls.append("POST /llm_providers")
            if any(p["id"] == body["id"] for p in self.providers):
                return conflict(f"LLMProvider with id {body['id']!r}")
            self.providers.append(body)
            return reply(201, body)
        match = re.fullmatch(r"/v1/llm_providers/([^/]+)", path)
        if match and method == "PUT":
            self.calls.append(f"PUT /llm_providers/{match.group(1)}")
            self.providers = [body if p["id"] == match.group(1) else p for p in self.providers]
            self.provider_answers = True              # the corrected details are what answers now
            return reply(200, body)
        match = re.fullmatch(r"/v1/llm_providers/([^/]+)/discovered_models", path)
        if match and method == "GET":
            self.calls.append(f"GET /llm_providers/{match.group(1)}/discovered_models")
            if not self.provider_answers:
                return reply(502, {"type": "/errors/provider-error", "title": "Provider error", "status": 502,
                                   "detail": "connection refused"})
            return reply(200, {"models": _MODELS})
        if path == "/v1/model_profiles" and method == "GET":
            return reply(200, {"items": self.profiles, "total": len(self.profiles)})
        if path == "/v1/model_profiles" and method == "POST":
            self.calls.append("POST /model_profiles")
            if any(p["id"] == body["id"] for p in self.profiles):
                return conflict(f"ModelProfile with id {body['id']!r}")
            self.profiles.append(body)
            return reply(201, body)
        return route.fallback()


def _open_wizard(page, console_url: str, api: _FakeSetupApi) -> None:
    page.route("**/v1/**", api.handle)
    open_setup_wizard(page, console_url)


def _step_text(page) -> str:
    return page.locator(".setup-progress").inner_text().strip()


def _connect_step_one(page, url: str = "http://llm.example/v1") -> None:
    page.locator("#setup-url").fill(url)
    page.get_by_role("button", name="Connect and list models").click()


def test_a_reload_at_step_two_resumes_at_step_two_and_finishes(page, console_url: str) -> None:
    api = _FakeSetupApi()
    _open_wizard(page, console_url, api)
    assert "step 1 of 2" in _step_text(page).lower()

    _connect_step_one(page)
    page.locator("#setup-model").wait_for(state="visible", timeout=10_000)
    assert "step 2 of 2" in _step_text(page).lower()
    assert [p["id"] for p in api.providers] == [_PROVIDER_ID], "step 1 saved the provider"

    # The operator's tab restores, the laptop sleeps, the page is refreshed: the server already holds the provider.
    page.reload(wait_until="domcontentloaded")
    page.get_by_text("Configure this install").wait_for(state="visible", timeout=15_000)
    page.locator("#setup-model").wait_for(state="visible", timeout=10_000)   # step 2's field is only there once the saved state is read
    assert "step 2 of 2" in _step_text(page).lower(), "a reload past step 1 must not send the operator back to step 1"
    options = page.locator("#setup-model option").all_inner_texts()
    assert options == [m["name"] for m in _MODELS], "step 2 lists the saved provider's own models"

    page.locator("#setup-model").select_option("model-two")
    page.get_by_role("button", name="Finish setup").click()
    page.get_by_test_id("setup-gate-enter").wait_for(state="visible", timeout=15_000)
    assert [p["model_name"] for p in api.profiles] == ["model-two"]
    assert "POST /llm_providers" in api.calls and api.calls.count("POST /llm_providers") == 1, "the provider was not saved twice"
    page.get_by_test_id("setup-gate-enter").click()
    page.get_by_test_id("nv-root").wait_for(state="visible", timeout=20_000)
    assert api.complete


def test_correcting_a_saved_provider_that_does_not_answer_updates_it_instead_of_a_409(page, console_url: str) -> None:
    """The gate re-enters the wizard when the saved provider stops answering. The operator fixes the URL; that must update the row."""
    saved = {"id": _PROVIDER_ID, "provider": "openchat", "config": {"url": "http://wrong.example/v1"}, "limits": {"max_concurrency": 4}}
    profile = {"id": f"{_PROVIDER_ID}--model-one", "provider_id": _PROVIDER_ID, "model_name": "model-one", "context_length": 8000}
    api = _FakeSetupApi(providers=[saved], profiles=[profile], provider_answers=False)
    api.seeded = True
    _open_wizard(page, console_url, api)

    assert "step 1 of 2" in _step_text(page).lower()
    assert page.locator("#setup-url").input_value() == "http://wrong.example/v1", "the saved details are prefilled"
    assert _PROVIDER_ID in page.locator(".auth-banner").inner_text(), "the operator is told which saved provider did not answer"

    _connect_step_one(page, "http://right.example/v1")
    page.locator("#setup-model").wait_for(state="visible", timeout=10_000)
    assert f"PUT /llm_providers/{_PROVIDER_ID}" in api.calls, "an existing provider is updated"
    assert "Could not" not in page.locator(".setup-steps").inner_text(), "no error banner after a correct retry"
    assert api.providers[0]["config"]["url"] == "http://right.example/v1"
