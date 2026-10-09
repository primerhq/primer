"""The gate fence, round 3 (console review C-033, ticket 01a11f52-9d98): what the OpenAPI text of the cancel body says about a gate id.

A yield that is not a human gate (sleep, watch_files) has no gate id, so a cancel that NAMES one is a mismatch and is refused 409 ``approval_stale``.
The field's text said such a yield "ignores" the id, which sent clients to omit a check that the server enforces (R19).
"""

from __future__ import annotations

import pytest

from primer.api.routers.yields import CancelYieldedToolBody
from primer.model.workspace_session import WorkspaceSession
from tests.api.conftest import raw_client as auth_client  # noqa: F401  (auth enforced; `app` and `client` come from the conftest)
from tests.api.test_gate_fence import G1
from tests.api.test_gate_fence_cancel import _Published, _url
from tests.api.test_gate_fence_round2 import _yield_session


def _description() -> str:
    return CancelYieldedToolBody.model_json_schema()["properties"]["gate_id"]["description"]


def test_the_cancel_body_does_not_say_a_non_gate_yield_ignores_the_gate_id() -> None:
    text = _description()
    assert "ignore" not in text, text


def test_the_cancel_body_says_a_non_gate_yield_refuses_a_gate_id_with_a_409() -> None:
    text = _description()
    assert "409" in text and "approval_stale" in text, text
    assert "has no gate id" in text and ("refus" in text), text


@pytest.mark.asyncio
async def test_a_non_gate_yield_does_refuse_a_gate_id(app, client) -> None:
    """The behaviour the text describes: the id is a mismatch for a yield that has none, and nothing is published."""
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_yield_session(session_id="k-nogate", tool_name="sleep"))
    published = _Published(app.state.event_bus)

    resp = await client.post(_url("k-nogate", "call_0"), json={"gate_id": G1})

    assert resp.status_code == 409, resp.text
    assert resp.json()["extensions"]["code"] == "approval_stale"
    assert published.events == []
