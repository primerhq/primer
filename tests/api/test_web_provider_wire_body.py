"""The server accepts the console's web-provider wire body and refuses the old one (design 13.4).

The old form posted ``{id, provider: "tavily", config: {api_key}}``:
``provider`` is not a field on the model (the model names the kind
``provider_type``) and ``config`` carries no ``type`` discriminant, so
the create 422'd. The console now names the body through
``PC_wireBody`` (provider-form.jsx); this file evaluates that helper for
real in MiniRacer and POSTs its answer, so the round trip is pinned at
both ends.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from py_mini_racer import MiniRacer

from tests.ui._mini_react import transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

_WS_PATH = "/v1/web_search_providers"
_WF_PATH = "/v1/web_fetch_providers"


@pytest.fixture(scope="module")
def wire_ctx() -> MiniRacer:
    """provider-form.jsx loaded once per module; PC_wireBody is pure
    logic, so the module-scoped context is safe to share (same harness
    as tests/ui/test_provider_form.py's ``_submittable_ctx``)."""
    ctx = MiniRacer()
    ctx.eval(
        "var window = globalThis; window.primerApi = {};"
        "var React = { createElement: function () { return null; },"
        " useState: function (v) { return [v, function () {}]; },"
        " Fragment: null };"
    )
    ctx.eval(transpile(UI / "components" / "provider-form.jsx"))
    return ctx


async def test_the_console_body_creates_a_web_search_provider(
    client, wire_ctx: MiniRacer,
) -> None:
    body = wire_ctx.call(
        "PC_wireBody",
        "web_search_providers",
        {"id": "tav", "provider": "tavily", "config": {"api_key": "tvly-1"}},
    )
    answer = await client.post(_WS_PATH, json=body)
    assert answer.status_code == 201, answer.text
    row = answer.json()
    assert row["provider_type"] == "tavily"
    assert row["config"]["type"] == "tavily"


async def test_the_console_body_creates_a_web_fetch_provider(
    client, wire_ctx: MiniRacer,
) -> None:
    """Jina is keyless on the server (rate-limited); the console body
    for it is exactly {id, provider_type, config: {type}}."""
    body = wire_ctx.call(
        "PC_wireBody",
        "web_fetch_providers",
        {"id": "jf", "provider": "jina"},
    )
    answer = await client.post(_WF_PATH, json=body)
    assert answer.status_code == 201, answer.text
    row = answer.json()
    assert row["provider_type"] == "jina"
    # The optional secret round-trips as an explicit null, not a missing
    # key: an unkeyed jina row reads back keyless.
    assert row["config"] == {"type": "jina", "api_key": None}


async def test_the_old_body_was_refused(client) -> None:
    """Design 13.4's bug, pinned at the server end: the old console body
    named the kind ``provider`` and carried no ``config.type``; the
    server was right to refuse it."""
    answer = await client.post(
        _WS_PATH,
        json={"id": "tav", "provider": "tavily", "config": {"api_key": "k"}},
    )
    assert answer.status_code == 422, answer.text
