"""The shared profile id helper (plan 02 Task 6, seam I-5).

A profile id is what the console's URL paths and the agents reference, so
it must match the id rule ``^[A-Za-z0-9][A-Za-z0-9._-]*$``: a model name
like ``anthropic/claude-3.5:beta`` (OpenRouter-style) carries ``/`` and
``:``, which the rule rejects, and the wizard's raw concatenation built
such ids. ``PC_idPart`` rewrites one part (every other character becomes
``-``, runs collapse, leading and trailing ``-``, ``.`` and ``_`` are
trimmed), and ``PC_profileIdFor`` joins the provider and model parts with
``--`` - or returns ``""`` when either part is ``""``, so the server can
make an id.

The helper is transpiled with the same Babel the server bundles with and
run for real in MiniRacer.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

CASES = {
    ("or", "anthropic/claude-3.5:beta"): "or--anthropic-claude-3.5-beta",
    ("or", "openai/gpt-4o"): "or--openai-gpt-4o",
    ("ollama-1", "llama3.1:8b"): "ollama-1--llama3.1-8b",
    ("lm", "Qwen/Qwen3-8B-GGUF:Q4_K_M"): "lm--Qwen-Qwen3-8B-GGUF-Q4_K_M",
    ("lm", "a  b@@c"): "lm--a-b-c",
    ("lm", "/models/x/"): "lm--models-x",
    ("lm", "__x__"): "lm--x",
    ("vllm-gx10", "qwen3.8-27b"): "vllm-gx10--qwen3.8-27b",
    ("my prov", "m"): "my-prov--m",
    ("lm", "???"): "",
}


def _ctx():
    """MiniRacer with register-common.jsx loaded. The id helpers are pure
    logic, so they run for real (same harness as
    test_provider_form.py's ``_submittable_ctx``)."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(transpile(UI / "components" / "providers" / "register-common.jsx"))
    return ctx


def test_profile_id_follows_the_id_rule() -> None:
    """Every case gives its value, and every non-empty value matches the
    rule - checked with the helper's own PC_ID_RE, not a copy."""
    ctx = _ctx()
    for (provider, model), expected in CASES.items():
        got = ctx.call("PC_profileIdFor", provider, model)
        assert got == expected, (provider, model, got)
        if expected:
            assert ctx.eval(f"PC_ID_RE.test({json.dumps(got)})") is True


def test_the_separator_survives_collapsing() -> None:
    """A part that ends (or starts) with a rewritten character still
    leaves exactly the two-character separator between the parts."""
    ctx = _ctx()
    assert ctx.call("PC_profileIdFor", "a-", "-b") == "a--b"


def test_the_helpers_are_exported_on_window() -> None:
    ctx = _ctx()
    assert (
        ctx.eval(
            "typeof window.PC_profileIdFor === 'function'"
            " && typeof window.PC_idPart === 'function'"
            " && window.PC_ID_RE instanceof RegExp"
        )
        is True
    )


def test_index_loads_register_common_before_the_catalog() -> None:
    """The file loads with its users: after the form, before the catalog
    (the wizard reads it too, and the catalog's register pieces of later
    tasks import from it)."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    tag = 'src="components/providers/register-common.jsx"'
    assert tag in html
    assert html.index(tag) > html.index('src="components/provider-form.jsx"')
    assert html.index(tag) < html.index('src="components/provider-catalog.jsx"')
