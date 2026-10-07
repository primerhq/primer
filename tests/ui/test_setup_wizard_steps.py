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
