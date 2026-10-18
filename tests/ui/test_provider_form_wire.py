"""The wire names of the provider form: omit the empties, send the server's names.

Three faults this file pins:

1. T3 (design 13.4): an emptied OPTIONAL box stored ``""`` and was sent
   as ``""``, which the server reads as a value (a base URL of ``""`` is
   a 422, a key of ``""`` is stored). ``PC_submittable`` now omits a row
   or config field the shape does not mark ``required`` when its value is
   a string empty after trimming.

2. The web classes (web_search, web_fetch) name their kind
   ``provider_type`` on the wire and carry the same value as the
   ``config.type`` discriminant; the form speaks ``provider`` everywhere
   (the dropdown, the _types key, PC_submittable's type fallback). The
   old console body ``{id, provider: "tavily", config: {api_key}}`` was
   refused with a 422. ``PC_draftFromRow`` seeds the draft from a served
   row and ``PC_wireBody`` names the wire body; the form internals never
   learn the server's names.

3. The kinds that serve config fields require the ``config`` key itself:
   a Hugging Face embedding with the untouched optional token box posts
   ``config: {}``, not no key (no key is a 422 "body.config: Field
   required"; the cross-encoder form has the same fault).

The shapes come from the real route functions, as
``tests/ui/test_provider_form_save.py`` does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.ui._mini_react import transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def _ctx():
    """MiniRacer with provider-form.jsx loaded.

    The PC_* wire helpers are pure logic, so they run for real; React is
    stubbed because loading the module evaluates the component
    definitions, not their bodies (same harness as
    test_provider_form.py's ``_submittable_ctx``).
    """
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(
        "var window = globalThis; window.primerApi = {};"
        "var React = { createElement: function () { return null; },"
        " useState: function (v) { return [v, function () {}]; },"
        " Fragment: null };"
    )
    ctx.eval(transpile(UI / "components" / "provider-form.jsx"))
    return ctx


def _llm_types() -> dict:
    from primer.api.routers.providers import list_llm_provider_types

    return asyncio.run(list_llm_provider_types())


def _embedding_types() -> dict:
    from primer.api.routers.providers import (
        list_embedding_provider_types,
    )

    return asyncio.run(list_embedding_provider_types())


def _cross_encoder_types() -> dict:
    from primer.api.routers.providers import (
        list_cross_encoder_provider_types,
    )

    return asyncio.run(list_cross_encoder_provider_types())


def test_an_emptied_optional_box_is_omitted_not_sent_as_empty() -> None:
    """T3: an optional text box the operator emptied sends nothing, not
    "" (the server reads "" as a value: a base URL of "" is a 422)."""
    ctx = _ctx()
    out = ctx.call(
        "PC_submittable",
        {"id": "or", "provider": "openrouter",
         "config": {"api_key": "k", "app_name": "", "app_url": "  "}},
        _llm_types()["openrouter"],
    )
    assert out["config"] == {"api_key": "k"}, out


def test_a_cleared_optional_secret_is_omitted() -> None:
    """T3: a cleared optional secret is omitted (a key of "" would be
    stored and masked on read, not read as unset)."""
    ctx = _ctx()
    out = ctx.call(
        "PC_submittable",
        {"id": "oc", "provider": "openchat",
         "config": {"url": "http://x/v1", "api_key": ""}},
        _llm_types()["openchat"],
    )
    assert "api_key" not in out["config"], out
    assert out["config"]["url"] == "http://x/v1"


def test_a_required_box_keeps_its_empty_string() -> None:
    """Only fields the shape does not mark required are omitted: a
    required box the operator emptied keeps its "" (Save is withheld for
    it by missingRequired, and a refusal that names the field is better
    than a silent drop)."""
    ctx = _ctx()
    out = ctx.call(
        "PC_submittable",
        {"id": "oc", "provider": "openchat",
         "config": {"url": "", "flavor": "openai"}},
        _llm_types()["openchat"],
    )
    assert out["config"]["url"] == "", out


def test_an_untouched_optional_token_box_still_sends_the_config_key() -> None:
    """The #758 review: the kinds that serve config fields require the
    ``config`` key itself. The Hugging Face embedding token is optional,
    so a draft that never touched the box must post ``config: {}``, not
    no key (no key is a 422 "body.config: Field required"). The
    cross-encoder form has the same fault (its token is optional too).
    """
    ctx = _ctx()
    draft = {"id": "hf", "provider": "huggingface",
             "models": [{"name": "BAAI/bge-base-en-v1.5"}]}
    for shape in (_embedding_types()["huggingface"],
                  _cross_encoder_types()["huggingface"]):
        out = ctx.call("PC_submittable", draft, shape)
        assert out["config"] == {}, out


def test_web_search_wire_body_names_provider_type_and_config_type() -> None:
    """Design 13.4: the wire body names the kind provider_type and
    carries the config.type discriminant the model validator checks."""
    ctx = _ctx()
    out = ctx.call(
        "PC_wireBody",
        "web_search_providers",
        {"id": "tav", "provider": "tavily", "config": {"api_key": "k"}},
    )
    assert out == {"id": "tav", "provider_type": "tavily",
                   "config": {"type": "tavily", "api_key": "k"}}, out


def test_web_fetch_wire_body_for_a_keyless_kind() -> None:
    """Jina works keyless: the wire body still carries the config.type
    discriminant, so no config key in the draft does not mean no config
    on the wire."""
    ctx = _ctx()
    out = ctx.call(
        "PC_wireBody",
        "web_fetch_providers",
        {"id": "jf", "provider": "jina"},
    )
    assert out == {"id": "jf", "provider_type": "jina",
                   "config": {"type": "jina"}}, out


def test_other_classes_pass_through_unchanged() -> None:
    ctx = _ctx()
    llm_body = {"id": "llm-x", "provider": "anthropic",
                "config": {"api_key": "k"},
                "limits": {"max_concurrency": 1}}
    assert ctx.call("PC_wireBody", "llm_providers", llm_body) == llm_body
    assert ctx.call("PC_wireBody", "embedding_providers", llm_body) == llm_body
    row = {"id": "llm-x", "provider": "anthropic", "config": {"api_key": "k"}}
    assert ctx.call("PC_draftFromRow", "llm_providers", row) == row


def test_a_served_web_row_seeds_the_form_kind() -> None:
    """A served web row names the kind provider_type; the dropdown reads
    draft.provider. Seeding the draft raw left the kind blank, and the
    row saved back without one at all."""
    ctx = _ctx()
    out = ctx.call(
        "PC_draftFromRow",
        "web_search_providers",
        {"id": "DuckDuckGo", "provider_type": "duckduckgo",
         "config": {"type": "duckduckgo"}},
    )
    assert out["provider"] == "duckduckgo", out


def test_the_catalog_saves_through_the_wire_body() -> None:
    """The two catalog call sites: save() routes the body through
    PC_wireBody, openEdit() seeds the draft through PC_draftFromRow."""
    src = _read("components/provider-catalog.jsx")
    assert "window.PC_wireBody(targetKlass.plural, body)" in src
    assert "window.PC_draftFromRow(k.plural, row)" in src
