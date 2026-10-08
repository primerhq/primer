"""A policy's ``preview_args`` is checked against the tool it gates, on the REST route (design note 01a11cd3-66b0, slice 1 condition (c)).

The check is the shared one (``primer/agent/approval_checks.py``); the router re-raises it as the 422 the console's policy modal already reads, a ``body.preview_args``
field error. These are wire goldens in the style of ``tests/api/test_rest_validator_wire_bodies.py``: status, content type and the raw problem body, byte for byte, with only
the per-request id normalised. The system tool's answer for the same refusals is ``tests/toolset/test_system_preview_args_check.py``.
"""

from __future__ import annotations

import re

import pytest

# Convention: shared API test fixtures (see test_compute.py).
from tests.api.conftest import app, client, fake_provider_registry  # noqa: F401

_REQUEST_ID = re.compile(r'"request_id":"req-[0-9a-f]+"')


def _wire(response) -> tuple[int, str, str]:
    return response.status_code, response.headers.get("content-type", ""), _REQUEST_ID.sub('"request_id":"req-<id>"', response.text)


def _body(policy_id: str = "p-pv", tool_name: str = "get_llm_provider", **fields) -> dict:
    return {"id": policy_id, "toolset_id": "system", "tool_name": tool_name, "approval": {"type": "required"}, **fields}


def _problem(instance: str, message: str) -> str:
    return (
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"One or more request parameters or body fields failed validation.",'
        f'"instance":"{instance}","extensions":{{"errors":[{{"loc":["body","preview_args"],"msg":"{message}","type":"value_error"}}],"request_id":"req-<id>"}}}}'
    )


@pytest.mark.asyncio
async def test_a_path_the_tool_has_is_accepted_and_stored(client) -> None:
    r = await client.post("/v1/tool_approval_policies", json=_body(preview_args=["id"]))

    assert r.status_code == 201, r.text
    assert r.json()["preview_args"] == ["id"]
    assert (await client.get("/v1/tool_approval_policies/p-pv")).json()["preview_args"] == ["id"]


@pytest.mark.asyncio
async def test_a_path_the_tool_lacks_is_a_422_at_the_field_with_the_exact_wire_body(client) -> None:
    r = await client.post("/v1/tool_approval_policies", json=_body(preview_args=["nope"]))

    assert _wire(r) == (
        422,
        "application/problem+json",
        _problem(
            "/v1/tool_approval_policies",
            "preview_args ['nope'] name no argument of the tool 'get_llm_provider' of toolset 'system' (top-level arguments: ['id'])",
        ),
    )
    assert (await client.get("/v1/tool_approval_policies/p-pv")).status_code == 404, "nothing was written"


@pytest.mark.asyncio
async def test_a_tool_that_is_not_in_the_catalogue_cannot_have_paths_checked_exact_wire_body(client) -> None:
    r = await client.post("/v1/tool_approval_policies", json=_body(tool_name="no_such_tool", preview_args=["id"]))

    assert _wire(r) == (
        422,
        "application/problem+json",
        _problem(
            "/v1/tool_approval_policies",
            "tool 'no_such_tool' of toolset 'system' is not in the catalogue right now, so preview_args cannot be checked against its arguments; "
            "set preview_args once the tool is reachable",
        ),
    )


@pytest.mark.asyncio
async def test_a_policy_with_no_paths_on_a_tool_that_is_not_in_the_catalogue_is_still_writable(client) -> None:
    """As before this change: policies are not tied to a live catalogue."""
    assert (await client.post("/v1/tool_approval_policies", json=_body(tool_name="no_such_tool"))).status_code == 201
    assert (await client.post("/v1/tool_approval_policies", json=_body("p-2", tool_name="other_missing", preview_args=[]))).status_code == 201


@pytest.mark.asyncio
async def test_an_update_to_a_path_the_tool_lacks_is_refused_and_the_row_is_unchanged(client) -> None:
    assert (await client.post("/v1/tool_approval_policies", json=_body(preview_args=["id"]))).status_code == 201

    r = await client.put("/v1/tool_approval_policies/p-pv", json=_body(preview_args=["nope"]))

    assert _wire(r) == (
        422,
        "application/problem+json",
        _problem(
            "/v1/tool_approval_policies/p-pv",
            "preview_args ['nope'] name no argument of the tool 'get_llm_provider' of toolset 'system' (top-level arguments: ['id'])",
        ),
    )
    assert (await client.get("/v1/tool_approval_policies/p-pv")).json()["preview_args"] == ["id"]


@pytest.mark.asyncio
async def test_a_malformed_path_is_the_models_own_422_at_the_same_field(client) -> None:
    r = await client.post("/v1/tool_approval_policies", json=_body(preview_args=["a b"]))

    assert r.status_code == 422
    errors = r.json()["extensions"]["errors"]
    assert errors[0]["loc"][-1] == "preview_args" and "not a dotted path" in errors[0]["msg"]


@pytest.mark.asyncio
async def test_a_workspace_tool_is_checked_against_its_argument_model(client) -> None:
    ok = await client.post("/v1/tool_approval_policies", json={**_body("p-ws"), "toolset_id": "workspace", "tool_name": "exec", "preview_args": ["command"]})
    bad = await client.post("/v1/tool_approval_policies", json={**_body("p-ws2"), "toolset_id": "workspace", "tool_name": "write", "preview_args": ["nope"]})

    assert ok.status_code == 201, ok.text
    assert bad.status_code == 422 and "name no argument of the tool 'write' of toolset 'workspace'" in bad.text
