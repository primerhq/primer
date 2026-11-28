"""Console broker registration and saved discovery; network answers are fixtures.

This is browser acceptance of the form contract, not live Codex authentication
or model inference. The live server supplies its real provider type metadata.
"""
from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._route_lifecycle import draining_page_routes


@pytest.mark.ui_e2e
@pytest.mark.parametrize("viewport", [{"width": 1366, "height": 768}, {"width": 390, "height": 844}])
def test_broker_registers_then_imports_only_saved_account_models(page: Page, viewport: dict) -> None:
    created, profiles, draft_probes = [], [], []
    provider_id = "cx-browser-broker"
    saved_path = f"**/v1/llm_providers/{provider_id}"

    def reply(route, body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def create(route):
        if route.request.method != "POST":
            route.fallback()
            return
        body = route.request.post_data_json
        assert body["id"] == provider_id and body["provider"] == "codex"
        assert body["config"] == {}
        saved = {**body, "config": {"binary_path": "codex", "codex_home": None,
                                    "pass_env": [], "model_catalog_path": None}}
        created.append(saved)
        reply(route, saved, 201)

    def draft(route):
        draft_probes.append(route.request.post_data_json)
        reply(route, {"detail": "Draft discovery is unavailable"}, 400)

    def profile(route):
        if route.request.method != "POST":
            route.fallback()
            return
        body = route.request.post_data_json
        assert body["provider_id"] == provider_id
        assert body["model_name"] == "account-model" and body["context_length"] == 8192
        profiles.append(body)
        reply(route, body, 201)

    mount = """row => {
      if (window.__codexRegisterRoot) window.__codexRegisterRoot.unmount();
      let host = document.getElementById('codex-register-test-host');
      if (!host) { host = document.createElement('div'); host.id = 'codex-register-test-host'; document.body.appendChild(host); }
      window.__codexRegisterRoot = ReactDOM.createRoot(host);
      window.__codexSaved = null;
      window.__codexRegisterRoot.render(React.createElement(window.PC_RegisterOverlay, {
        initialClass: 'agent_runtime', allowedClasses: ['agent_runtime'], editRow: row,
        onClose() {}, onSaved(saved) { window.__codexSaved = saved; }
      }));
    }"""
    with draining_page_routes(page):
        page.route("**/v1/llm_providers", create)
        page.route("**/v1/llm_providers/_discover_models", draft)
        page.route("**/v1/model_profiles", profile)
        page.route(saved_path, lambda route: reply(route, created[0]))
        page.route(saved_path + "/models", lambda route: reply(route, {"models": []}))
        page.route(saved_path + "/discovered_models", lambda route: reply(route, {
            "models": [{"name": "account-model", "display_name": "Account model"}],
            "source": "account_policy_filtered_broker", "transport": "model_only_responses",
        }))
        try:
            page.set_viewport_size(viewport)
            page.wait_for_function("() => typeof window.PC_RegisterOverlay === 'function' && typeof ReactDOM !== 'undefined'")
            page.evaluate(mount, None)
            dialog = page.get_by_test_id("pc-register")
            dialog.get_by_test_id("pc-field:type").select_option("codex")
            dialog.get_by_test_id("pc-field:id").fill(provider_id)
            expect(dialog.get_by_test_id("pc-saved-discovery-help")).to_contain_text("Import more models")
            expect(dialog.get_by_test_id("pc-field:binary_path")).to_have_count(0)
            expect(dialog.get_by_test_id("pc-field:codex_home")).to_have_count(0)
            expect(dialog.get_by_test_id("pc-register-next")).to_have_count(0)
            dialog.get_by_test_id("pc-register-submit").click()
            page.wait_for_function("() => window.__codexSaved !== null")
            assert len(created) == 1 and draft_probes == []

            page.evaluate(mount, created[0])
            dialog.get_by_test_id("pc-register-import-more").click()
            expect(dialog.get_by_test_id("pc-import-head")).to_be_visible()
            context = dialog.get_by_test_id("pc-import-context:account-model")
            expect(context).to_have_value("")
            dialog.get_by_test_id("pc-import:account-model").check()
            expect(dialog.get_by_test_id("pc-register-submit")).to_be_disabled()
            context.fill("8192")
            dialog.get_by_test_id("pc-register-submit").click()
            page.wait_for_function("() => window.__codexSaved !== null")
            assert len(profiles) == 1 and len(created) == 1 and draft_probes == []
        finally:
            page.evaluate("() => { if (window.__codexRegisterRoot) window.__codexRegisterRoot.unmount(); document.getElementById('codex-register-test-host')?.remove(); }")


@pytest.mark.ui_e2e
def test_setup_resumes_saved_broker_without_inventing_context_length(page, console_url, monkeypatch):
    from tests.ui_e2e import test_setup_wizard_resume_journey as setup

    monkeypatch.setattr(setup, "_MODELS", [
        {"name": "account-model", "display_name": "Account model"},
        {"name": "reported-model", "context_length": 8192},
    ])

    class BrokerSetup(setup._FakeSetupApi):
        def _served(self, row):
            return dict(row)  # Broker rows carry no API-key field.

    api = BrokerSetup(providers=[{"id": "cx-setup", "provider": "codex", "config": {},
                                 "limits": {"max_concurrency": 1}}])
    with draining_page_routes(page):
        setup._open_wizard(page, console_url, api)
        model = page.locator("#setup-model")
        expect(model).to_have_value("account-model")
        context = page.locator("#setup-context-length")
        expect(context).to_have_value("")
        finish = page.get_by_role("button", name="Finish setup")
        expect(finish).to_be_disabled()
        assert api.profiles == []
        for invalid in ("0", "1.5", "NaN"):
            context.fill(invalid)
            expect(finish).to_be_disabled()
        model.select_option("reported-model")
        expect(context).to_have_value("8192")
        model.select_option("account-model")
        expect(context).to_have_value("")
        context.fill("16384")
        finish.click()
        expect(page.get_by_test_id("setup-gate-enter")).to_be_visible()
        assert len(api.profiles) == 1
        assert api.profiles[0]["context_length"] == 16384
        assert api.profiles[0]["model_name"] == "account-model"
        assert "POST /llm_providers" not in api.calls
        assert "POST /llm_providers/_discover_models" not in api.calls
