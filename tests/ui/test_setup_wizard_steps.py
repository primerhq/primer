"""The first-boot wizard's two steps, EXECUTED (ui/components/setup-wizard.jsx: SW_connectProvider, SW_registerProfile).

tests/ui/test_setup_wizard.py only substring-matches the source, and the e2e lane runs with auth disabled, so the wizard's behaviour had no test
at all. The two steps are pure functions of an ``apiFetch``, so they run here under MiniRacer: the repo's own JSX bundler transpiles the file,
a fake ``apiFetch`` records every call and answers from a script.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "setup-wizard.jsx"


def _ctx():
    from py_mini_racer import MiniRacer

    from primer.api._jsx_bundle import JSXBundler

    bundler = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    code = bundler._transform(SRC.read_text(encoding="utf-8"), "components/setup-wizard.jsx")
    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(
        "window.React = { useState: function(){}, useEffect: function(){}, useRef: function(){}, useCallback: function(f){ return f; } };"
        "var Btn, Banner, Icon;"
    )
    ctx.eval(code)
    ctx.eval(
        """
        var __calls = [];
        function __api(script) {
          return function (method, path, body) {
            __calls.push([method, path, body === undefined ? null : body]);
            var r = script(method, path, body);
            return (r instanceof Error) ? Promise.reject(r) : Promise.resolve(r);
          };
        }
        function __http(status, message) { var e = new Error(message || ('http ' + status)); e.status = status; return e; }
        """
    )
    return ctx


def _run(ctx, expr: str) -> dict:
    """Await a promise-returning JS expression; return {"ok": value} or {"error": message}."""
    ctx.eval(
        "globalThis.__state = 'pending'; globalThis.__val = null;"
        f"({expr}).then(function (v) {{ globalThis.__state = 'ok'; globalThis.__val = JSON.stringify(v === undefined ? null : v); }},"
        " function (e) { globalThis.__state = 'error'; globalThis.__val = String(e && e.message ? e.message : e); });"
    )
    state = ctx.eval("globalThis.__state")
    assert state != "pending", "the promise did not settle (a pending apiFetch?)"
    val = ctx.eval("globalThis.__val")
    return {"ok": json.loads(val)} if state == "ok" else {"error": val}


def _calls(ctx) -> list:
    return json.loads(ctx.eval("JSON.stringify(__calls)"))


def test_the_harness_runs_the_real_module() -> None:
    ctx = _ctx()
    assert ctx.eval("typeof window.SW_connectProvider") == "function"
    assert ctx.eval("typeof window.SW_registerProfile") == "function"


def test_step_one_probes_the_draft_then_creates_the_provider() -> None:
    ctx = _ctx()
    ctx.eval(
        "var __api1 = __api(function (m, p) {"
        "  if (p === '/llm_providers/_discover_models') return { models: [{ name: 'gpt-x' }] };"
        "  return {};"
        "});"
    )
    result = _run(ctx, "window.SW_connectProvider(__api1, { type: 'openchat', url: 'http://h:1/v1', apiKey: 'k' })")

    assert result == {"ok": {"id": "llm-openchat", "models": [{"name": "gpt-x"}]}}
    calls = _calls(ctx)
    assert [c[:2] for c in calls] == [["POST", "/llm_providers/_discover_models"], ["POST", "/llm_providers"]]
    assert calls[0][2] == {"provider": "openchat", "config": {"url": "http://h:1/v1", "api_key": "k"}}
    assert calls[1][2] == {
        "id": "llm-openchat", "provider": "openchat",
        "config": {"url": "http://h:1/v1", "api_key": "k"}, "limits": {"max_concurrency": 4},
    }


def test_step_one_writes_nothing_when_the_probe_lists_no_models() -> None:
    ctx = _ctx()
    ctx.eval("var __api2 = __api(function () { return { models: [] }; });")
    result = _run(ctx, "window.SW_connectProvider(__api2, { type: 'openchat', url: 'http://h:1/v1', apiKey: '' })")

    assert result == {"ok": {"id": None, "models": []}}
    assert [c[1] for c in _calls(ctx)] == ["/llm_providers/_discover_models"]


def test_step_two_posts_the_default_profile() -> None:
    ctx = _ctx()
    ctx.eval("var __api3 = __api(function () { return {}; });")
    result = _run(ctx, "window.SW_registerProfile(__api3, { providerId: 'llm-openchat', model: { name: 'gpt-x' } })")

    assert result == {"ok": None}
    (call,) = _calls(ctx)
    assert call[:2] == ["POST", "/model_profiles"]
    assert call[2] == {
        "id": "llm-openchat--gpt-x",
        "description": "Default profile created by first-run setup.",
        "provider_id": "llm-openchat",
        "model_name": "gpt-x",
        "context_length": 32000,
    }


# ---------------------------------------------------------------------------
# ADM-01: the wizard must survive being done once. Step 1 creates the fixed id llm-<type>, and a reload (or an abandoned step 2) restarts at
# step 1, so the second attempt used to meet "LLMProvider with id 'llm-openchat' already exists" (409) for ever, behind a full-screen gate.
# ---------------------------------------------------------------------------


def test_step_one_reuses_a_provider_row_that_already_exists() -> None:
    ctx = _ctx()
    ctx.eval(
        "var __api4 = __api(function (m, p) {"
        "  if (p === '/llm_providers/_discover_models') return { models: [{ name: 'gpt-x' }] };"
        "  if (m === 'POST' && p === '/llm_providers') return __http(409, \"LLMProvider with id 'llm-openchat' already exists\");"
        "  return {};"
        "});"
    )
    result = _run(ctx, "window.SW_connectProvider(__api4, { type: 'openchat', url: 'http://new:1/v1', apiKey: '' })")

    assert result == {"ok": {"id": "llm-openchat", "models": [{"name": "gpt-x"}]}}
    puts = [c for c in _calls(ctx) if c[0] == "PUT"]
    assert [c[1] for c in puts] == ["/llm_providers/llm-openchat"]
    assert puts[0][2] == {
        "id": "llm-openchat", "provider": "openchat",
        "config": {"url": "http://new:1/v1"}, "limits": {"max_concurrency": 4},
    }, "the row takes what the user just typed and the probe just proved"


def test_step_one_still_fails_on_any_error_that_is_not_a_conflict() -> None:
    ctx = _ctx()
    ctx.eval(
        "var __api5 = __api(function (m, p) {"
        "  if (p === '/llm_providers/_discover_models') return { models: [{ name: 'gpt-x' }] };"
        "  return __http(500, 'boom');"
        "});"
    )
    result = _run(ctx, "window.SW_connectProvider(__api5, { type: 'openchat', url: 'http://h:1/v1', apiKey: '' })")

    assert result == {"error": "boom"}
    assert not [c for c in _calls(ctx) if c[0] == "PUT"], "only a 409 may turn into an update"


def test_step_two_treats_a_profile_that_already_exists_as_done() -> None:
    ctx = _ctx()
    ctx.eval("var __api6 = __api(function () { return __http(409, 'ModelProfile already exists'); });")
    result = _run(ctx, "window.SW_registerProfile(__api6, { providerId: 'llm-openchat', model: { name: 'gpt-x' } })")

    assert result == {"ok": None}


def test_step_two_still_fails_on_any_error_that_is_not_a_conflict() -> None:
    ctx = _ctx()
    ctx.eval("var __api7 = __api(function () { return __http(422, 'bad context'); });")
    result = _run(ctx, "window.SW_registerProfile(__api7, { providerId: 'llm-openchat', model: { name: 'gpt-x' } })")

    assert result == {"error": "bad context"}


def test_resume_finds_the_existing_provider_and_its_models() -> None:
    ctx = _ctx()
    ctx.eval(
        "var __api8 = __api(function (m, p) {"
        "  if (p.indexOf('/llm_providers?') === 0) return { items: [{ id: 'llm-openchat' }, { id: 'other' }] };"
        "  if (p === '/llm_providers/llm-openchat/discovered_models') return { models: [{ name: 'gpt-x' }] };"
        "  return __http(404, 'unexpected ' + p);"
        "});"
    )
    result = _run(ctx, "window.SW_resumeFromProvider(__api8)")

    assert result == {"ok": {"providerId": "llm-openchat", "models": [{"name": "gpt-x"}]}}


def test_resume_is_null_when_there_is_no_provider_or_it_cannot_list_models() -> None:
    ctx = _ctx()
    ctx.eval("var __none = __api(function () { return { items: [] }; });")
    assert _run(ctx, "window.SW_resumeFromProvider(__none)") == {"ok": None}

    ctx.eval(
        "var __down = __api(function (m, p) {"
        "  if (p.indexOf('/llm_providers?') === 0) return { items: [{ id: 'llm-openchat' }] };"
        "  return __http(400, 'probe failed');"
        "});"
    )
    assert _run(ctx, "window.SW_resumeFromProvider(__down)") == {"ok": None}

    ctx.eval(
        "var __empty = __api(function (m, p) {"
        "  if (p.indexOf('/llm_providers?') === 0) return { items: [{ id: 'llm-openchat' }] };"
        "  return { models: [] };"
        "});"
    )
    assert _run(ctx, "window.SW_resumeFromProvider(__empty)") == {"ok": None}


def test_resume_is_null_when_the_provider_list_request_itself_fails() -> None:
    ctx = _ctx()
    ctx.eval("var __listdown = __api(function () { return __http(500, 'list down'); });")
    assert _run(ctx, "window.SW_resumeFromProvider(__listdown)") == {"ok": None}


# ---------------------------------------------------------------------------
# The gate's decision to open at step 2 is a pure function of the setup predicates (GET /setup/state), so it is tested here and not only seen in a
# browser. Review of the first version found a mutant that never resumes (an unconditional "start at step 1") left every test green: the wiring in the
# gate's effect was React code the harness does not render.
# ---------------------------------------------------------------------------


def _should_resume(ctx, predicates) -> bool:
    return bool(ctx.eval(f"window.SW_shouldResume({json.dumps(predicates)})"))


def test_resume_at_step_two_when_the_provider_is_ok_and_the_profile_is_missing() -> None:
    ctx = _ctx()
    predicates = [{"key": "llm_provider", "ok": True}, {"key": "model_profile", "ok": False}, {"key": "builder_agent", "ok": False}]
    assert _should_resume(ctx, predicates) is True


def test_no_resume_when_both_the_provider_and_the_profile_exist() -> None:
    ctx = _ctx()
    assert _should_resume(ctx, [{"key": "llm_provider", "ok": True}, {"key": "model_profile", "ok": True}]) is False


def test_no_resume_when_neither_exists_so_the_wizard_starts_at_step_one() -> None:
    ctx = _ctx()
    assert _should_resume(ctx, [{"key": "llm_provider", "ok": False}, {"key": "model_profile", "ok": False}]) is False


def test_no_resume_when_only_the_profile_exists() -> None:
    ctx = _ctx()
    assert _should_resume(ctx, [{"key": "llm_provider", "ok": False}, {"key": "model_profile", "ok": True}]) is False


def test_no_resume_for_missing_or_empty_predicates() -> None:
    ctx = _ctx()
    assert _should_resume(ctx, []) is False
    assert _should_resume(ctx, None) is False
    assert _should_resume(ctx, [{"key": "builder_agent", "ok": True}]) is False


def test_the_gate_effect_asks_the_decision_function() -> None:
    """A source check, not an execution: the effect itself is React the harness does not render, so this only pins that
    the decision is taken by SW_shouldResume and not re-written inline (which is how a mutant slipped past the first version)."""
    src = SRC.read_text(encoding="utf-8")
    gate = src[src.index("function SetupWizardGate("):src.index("function SetupWaitingScreen(")]
    assert "SW_shouldResume(state.predicates)" in gate
