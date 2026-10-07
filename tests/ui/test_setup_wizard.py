"""S5 P2: the bootstrap wizard is an embeddable, no-chrome step sequence."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "setup-wizard.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_three_globals_are_exported() -> None:
    src = _src()
    assert "window.SetupWizardSteps = SetupWizardSteps" in src
    assert "window.SetupWizardGate = SetupWizardGate" in src
    assert "window.SetupWaitingScreen = SetupWaitingScreen" in src


def test_r5_globals_are_exported() -> None:
    src = _src()
    assert "window.SetupPredicatesList = SetupPredicatesList" in src
    assert "window.NV_SetupPage = NV_SetupPage" in src


def test_step_sequence_has_no_chrome_and_no_navigation() -> None:
    """C5 mount contract: S8 re-hosts SetupWizardSteps verbatim, so it must
    not touch the console shell, routes, or the address bar."""
    src = _src()
    start = src.index("function SetupWizardSteps(")
    end = src.index("function SetupWizardGate(")
    body = src[start:end]
    assert "window.location" not in body
    assert "ROUTES" not in body
    assert "auth-shell" not in body


def test_step_one_probes_the_draft_provider() -> None:
    src = _src()
    assert '"/llm_providers/_discover_models"' in src
    assert '"/llm_providers"' in src


def test_step_two_reuses_the_probe_result_and_creates_a_profile() -> None:
    """M11e: the same _discover_models response is step 2's model list."""
    src = _src()
    assert '"/model_profiles"' in src
    assert "discovered" in src
    assert "context_length" in src


def test_provider_is_created_before_the_profile() -> None:
    src = _src()
    assert src.index('"/llm_providers"') < src.index('"/model_profiles"')


def test_limits_block_is_posted_with_the_provider() -> None:
    assert "max_concurrency" in _src()


def test_wizard_is_loaded_by_the_console() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'src="components/setup-wizard.jsx"' in html


def test_every_class_the_wizard_introduces_is_defined() -> None:
    """The wizard reuses the auth-shell system; every class it adds on top
    (the original 2-class step sequence, R5's 6-class predicate list)
    must exist in styles.css or something renders unstyled."""
    css = (UI / "styles.css").read_text(encoding="utf-8")
    for rule in (
        ".setup-steps", ".setup-progress", ".auth-field select",
        ".setup-predicates", ".setup-predicate", ".setup-predicate-dot",
        ".setup-predicate-label", ".setup-predicate-detail", ".setup-predicate-fix",
    ):
        assert rule in css, rule


def test_wizard_transpiles_via_the_server_bundler() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(
        ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text(),
    )
    code = b._transform(_src(), "components/setup-wizard.jsx")
    assert code and "SetupWizardSteps" in code

def test_completion_invokes_the_seed_endpoint() -> None:
    """C3: the wizard explicitly runs the ensure pass once a profile exists."""
    src = _src()
    host = src[src.index("function SetupWizardGate("):src.index("function SetupWaitingScreen(")]
    assert '"/setup/seed"' in host


def test_predicates_list_reads_the_live_state_endpoint() -> None:
    """R5 BUILD: the six-predicate checklist is fed by GET /setup/state
    (live-checked), not GET /auth/status (presence-only, stays cheap for
    its hot unauthenticated-probe role)."""
    src = _src()
    assert '"/setup/state"' in src


def test_predicates_list_renders_ok_and_fix_action_per_row() -> None:
    src = _src()
    start = src.index("function SetupPredicatesList(")
    end = src.index("\nconst _CAPABILITY_GATES", start)
    body = src[start:end]
    assert "state.predicates.map" in body
    assert "p.ok" in body
    assert "p.detail" in body
    assert "Configure provider" in body
    assert "Re-run seed" in body


def test_fetch_capabilities_hits_the_capabilities_endpoint() -> None:
    src = _src()
    start = src.index("function _fetchCapabilities(")
    end = src.index("\n}", start)
    body = src[start:end]
    assert '"/capabilities"' in body


def test_admin_setup_page_has_predicates_capabilities_and_reset_actions() -> None:
    """R5 BUILD: NV_SetupPage is the whole Setup admin surface - six
    predicates + capabilities table + Re-run seed + Reset base agent
    roster (the latter two REUSE - no backend change needed)."""
    src = _src()
    start = src.index("function NV_SetupPage(")
    end = src.index("\n// ====", start)
    body = src[start:end]
    assert "SetupPredicatesList" in body
    assert "_fetchCapabilities()" in body
    assert "nv-sys-setup-rerun-seed" in body
    assert '"/setup/seed"' in body
    assert "nv-sys-setup-reset-roster" in body
    assert '"/setup/reset_agents"' in body
    assert "nv-sys-capabilities-table" in body


def test_admin_setup_page_configure_provider_reuses_the_wizard_steps() -> None:
    """The provider/profile predicates can't be auto-seeded (notes: they
    need real operator input) - their fix-action reopens the existing
    2-step SetupWizardSteps inline rather than duplicating that form."""
    src = _src()
    start = src.index("function NV_SetupPage(")
    end = src.index("\n// ====", start)
    body = src[start:end]
    assert "<SetupWizardSteps" in body


def test_gate_requires_all_predicates_before_entering() -> None:
    """notes section 5: "Enter Primer" enables only when all six pass."""
    src = _src()
    host = src[src.index("function SetupWizardGate("):src.index("function SetupWaitingScreen(")]
    assert "setup-gate-enter" in host
    assert "disabled={!state || !state.complete}" in host


def test_gate_routes_returning_admins_past_a_finished_provider_step() -> None:
    """A returning admin whose provider/profile already exist (e.g. a
    prior ensure pass failed on the workspace backend) must land on the
    predicate checklist, not be forced through the 2-step form again -
    the routing decision reads state.predicates, not just state.complete."""
    src = _src()
    start = src.index("function SetupWizardGate(")
    end = src.index("function SetupWaitingScreen(")
    body = src[start:end]
    assert "providerMissing" in body
    assert "llm_provider" in body and "model_profile" in body


# ---------------------------------------------------------------------------
# C-001 (console review 2026-10-08): the wizard must be resumable and idempotent.
#
# Step 1 persists the provider row (``llm-<type>``), but the step lived only in React state, and the gate sends the user back into
# the wizard while the model profile is missing. A reload at step 2 therefore restarted at step 1 and every retry failed with a
# 409 on the fixed id, reported as "Could not reach that provider": a dead end on the first run. The step is a fact about the
# SERVER (what is already saved), so the decision is a set of pure helpers, run here for real in V8.
# ---------------------------------------------------------------------------

import json  # noqa: E402

_HELPERS_END = "// ---- end of the resume helpers"


def _helpers():
    from py_mini_racer import MiniRacer

    src = _src()
    start = src.index("function SW_providerId(")
    end = src.index(_HELPERS_END)
    ctx = MiniRacer()
    ctx.eval(src[start:end])
    return ctx


def _run(ctx, expression: str):
    """Evaluate ``expression`` (a value or a promise) and hand back its JSON, or {"__rejected": ...}."""
    ctx.eval(
        "var __r = null; (async function () { try { __r = JSON.stringify({ok: await (" + expression + ")}); }"
        " catch (e) { __r = JSON.stringify({rejected: {status: e && e.status, detail: e && e.detail, message: e && e.message}}); } })();"
    )
    return json.loads(ctx.eval("__r"))


_FRESH = "[], []"
_PROVIDER = '{id: "llm-openchat", provider: "openchat", config: {url: "http://llm.example/v1", api_key: "**********"}}'


def test_a_fresh_install_starts_at_step_one() -> None:
    assert _run(_helpers(), "SW_resumePlan([], [])") == {"ok": {"step": 1}}


def test_a_saved_provider_without_a_profile_resumes_at_step_two() -> None:
    """The reload-at-step-2 case: the row is there, the profile is not, so the user is at step 2 whatever React remembers."""
    plan = _run(_helpers(), f"SW_resumePlan([{_PROVIDER}], [])")["ok"]
    assert plan["step"] == 2 and plan["providerId"] == "llm-openchat"


def test_the_provider_without_a_profile_is_the_one_resumed() -> None:
    plan = _run(
        _helpers(),
        'SW_resumePlan([{id: "llm-a", provider: "ollama", config: {}}, {id: "llm-b", provider: "openchat", config: {}}], '
        '[{id: "llm-a--m", provider_id: "llm-a", model_name: "m"}])',
    )["ok"]
    assert plan["step"] == 2 and plan["providerId"] == "llm-b"


def test_a_provider_that_already_has_a_profile_is_edited_at_step_one_prefilled() -> None:
    """The gate re-enters the wizard when the provider does not answer: the user must be able to correct it, not be told it exists."""
    plan = _run(
        _helpers(),
        f'SW_resumePlan([{_PROVIDER}], [{{id: "llm-openchat--m", provider_id: "llm-openchat", model_name: "m"}}])',
    )["ok"]
    assert plan["step"] == 1
    assert plan["prefill"] == {"type": "openchat", "url": "http://llm.example/v1", "providerId": "llm-openchat"}


def test_saving_a_provider_that_already_exists_updates_it_instead_of_failing() -> None:
    ctx = _helpers()
    ctx.eval(
        "var calls = []; function api(method, path, body) { calls.push(method + ' ' + path);"
        " if (method === 'POST') return Promise.reject({status: 409, detail: 'already exists'});"
        " return Promise.resolve({id: body.id}); }"
    )
    out = _run(ctx, 'SW_saveProvider(api, {id: "llm-openchat", provider: "openchat", config: {url: "http://x"}})')
    assert "ok" in out, out
    assert json.loads(ctx.eval("JSON.stringify(calls)")) == ["POST /llm_providers", "PUT /llm_providers/llm-openchat"]


def test_saving_a_provider_does_not_hide_a_real_failure() -> None:
    ctx = _helpers()
    ctx.eval("var calls = []; function api(m, p) { calls.push(m); return Promise.reject({status: 500, detail: 'boom'}); }")
    out = _run(ctx, 'SW_saveProvider(api, {id: "llm-openchat"})')
    assert out["rejected"]["status"] == 500
    assert json.loads(ctx.eval("JSON.stringify(calls)")) == ["POST"], "only a 409 may fall through to the update"


def test_saving_a_profile_that_already_exists_counts_as_saved() -> None:
    """A retried step 2 (the first request landed, the answer was lost) must not fail on its own earlier success."""
    ctx = _helpers()
    ctx.eval("function api(m, p) { return Promise.reject({status: 409}); }")
    assert "ok" in _run(ctx, 'SW_saveProfile(api, {id: "llm-openchat--m"})')


def test_the_resume_plan_comes_from_the_saved_provider_and_its_live_model_list() -> None:
    ctx = _helpers()
    ctx.eval(
        "var calls = []; function api(method, path) { calls.push(method + ' ' + path);"
        f" if (path.indexOf('/llm_providers?') === 0) return Promise.resolve({{items: [{_PROVIDER}]}});"
        " if (path.indexOf('/model_profiles?') === 0) return Promise.resolve({items: []});"
        " if (path === '/llm_providers/llm-openchat/discovered_models') return Promise.resolve({models: [{name: 'm1', context_length: 8000}]});"
        " return Promise.reject({status: 404}); }"
    )
    plan = _run(ctx, "SW_loadResume(api)")["ok"]
    assert plan["step"] == 2 and plan["providerId"] == "llm-openchat"
    assert plan["models"] == [{"name": "m1", "context_length": 8000}]
    # the saved row's secret is redacted on the wire, so the live probe must be the SAVED-provider route, not a replay of the row
    assert not any("_discover_models" in c for c in json.loads(ctx.eval("JSON.stringify(calls)")))


def test_a_saved_provider_that_does_not_answer_falls_back_to_step_one_with_a_notice() -> None:
    ctx = _helpers()
    ctx.eval(
        "function api(method, path) {"
        f" if (path.indexOf('/llm_providers?') === 0) return Promise.resolve({{items: [{_PROVIDER}]}});"
        " if (path.indexOf('/model_profiles?') === 0) return Promise.resolve({items: []});"
        " return Promise.reject({status: 502, detail: 'connection refused'}); }"
    )
    plan = _run(ctx, "SW_loadResume(api)")["ok"]
    assert plan["step"] == 1
    assert plan["prefill"]["url"] == "http://llm.example/v1"
    assert "llm-openchat" in plan["notice"] and "connection refused" in plan["notice"]


def test_a_failed_read_of_the_saved_state_never_blocks_the_wizard() -> None:
    ctx = _helpers()
    ctx.eval("function api() { return Promise.reject({status: 0, detail: 'offline'}); }")
    assert _run(ctx, "SW_loadResume(api)")["ok"] == {"step": 1}


def test_the_step_sequence_resumes_from_the_server_on_mount() -> None:
    src = _src()
    body = src[src.index("function SetupWizardSteps("):src.index("function SetupWizardGate(")]
    assert "SW_loadResume(" in body, "the step must be derived from the server, not from React state alone"
    assert "initialStep" in body, "the docs harness still starts a capture at a given step"
    assert "SW_saveProvider(" in body and "SW_saveProfile(" in body, "both steps are idempotent"


def test_system_setup_configure_provider_uses_the_same_resumable_steps() -> None:
    """The System > Setup "Configure provider" route had the same dead end; it reuses SetupWizardSteps, so one fix covers both."""
    src = _src()
    page = src[src.index("function NV_SetupPage("):src.index("// ====", src.index("function NV_SetupPage("))]
    assert "<SetupWizardSteps" in page


def test_the_error_titles_name_the_phase_that_failed() -> None:
    """"Could not reach that provider" was shown for a 409 on the save, which is not what happened."""
    src = _src()
    assert "Could not save the provider" in src
    assert "Could not reach that provider" in src
