"""CI-owned real MCP meta-tool journey for the Tools Universal M4 surface: the internal-collections flow over the generated tools.

The e2e conftest collect-ignores this module unless ``PRIMER_RUN_E2E=1``.
It is intentionally not run in the resource-constrained local lane.

The flow walks the enable-internal-collections runbook the way a caller over MCP walks it: the three meta-tools advertise, ``find_tools``
discovers the five generated internal-collections tools, ``list_tools`` carries the nested ``body`` schema of the config write, and
``call_tool`` drives the flow end to end (put_config -> bootstrap -> the durable get_config / bootstrap_status rereads -> a real semantic query
of the system collection -> delete_config). Every acceptance read goes over MCP; REST is used only for the operator setup a journey needs
(the approval policies the writes park on are disabled for the run - a person's act - and the exposure allowlist). Whatever the flow leaves
behind is cleaned in the ``finally``, and every cleanup step is attempted even when an earlier one fails: the
subsystem config is deleted, the exposure and the policies are restored from what was captured BEFORE the first
mutation, and the MCP token is revoked. A cleanup failure after a passed flow fails the test; after a failed
flow it rides the body's exception as its note, so the original failure stays the test's.
"""

from __future__ import annotations

import json

import httpx
import pytest


pytestmark = pytest.mark.asyncio

_META_TOOLS = {"list_tools", "find_tools", "call_tool"}
_IC_TOOLS = {
    "internal_collections__put_config",
    "internal_collections__get_config",
    "internal_collections__delete_config",
    "internal_collections__bootstrap",
    "internal_collections__bootstrap_status",
}
_ALLOWED = sorted(_IC_TOOLS | {"collections__search"})
_POLICY_IDS = {
    tool: f"tool-approval-policy-internal-collections-{tool.replace('_', '-')}"
    for tool in ("put_config", "delete_config", "bootstrap")
}
_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
# The bootstrap runs a real embedding pass over the system collection inline; the call is slow on a cold runner.
# The bound stays under 180s: the CI job's step budget must outlive the slowest expected call, not equal it.
_MCP_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _text(result) -> str:
    return "".join(getattr(part, "text", "") for part in result.content)


def _call_output(result) -> dict:
    assert not result.is_error, _text(result)
    envelope = json.loads(_text(result))
    assert envelope["ok"] is True, envelope
    output = envelope["output"]
    return json.loads(output) if isinstance(output, str) else output


async def test_m4_internal_collections_flow_over_the_mcp_meta_tools(
    client, base_url: str, unique_suffix: str,
) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    # The restore state is captured BEFORE the first mutation and initialised empty, so the ``finally`` below
    # always restores whatever was captured even when a setup step fails midway.
    originals: dict[str, dict] = {}
    before: dict | None = None
    token_id: str | None = None
    body_failure: BaseException | None = None

    try:
        # Operator setup: the writes park on the shipped admin-tier gates; over MCP nothing can park, so the run
        # disables them (a person's act) and restores them afterwards.
        for tool, policy_id in _POLICY_IDS.items():
            row = await client.get(f"/v1/tool_approval_policies/{policy_id}")
            assert row.status_code == 200, row.text
            originals[tool] = row.json()
            disabled = await client.put(f"/v1/tool_approval_policies/{policy_id}", json={**row.json(), "enabled": False})
            assert disabled.status_code == 200, disabled.text

        before = (await client.get("/v1/mcp_exposure")).json()
        token = await client.post(
            "/v1/auth/tokens",
            json={"name": f"m4-sdk-{unique_suffix}", "scopes": ["mcp"]},
        )
        assert token.status_code == 201, token.text
        token_id = token.json()["id"]
        secret = token.json()["plaintext"]

        enabled = await client.put(
            "/v1/mcp_exposure",
            json={"enabled": True, "mode": "allowlist", "allowed_tools": _ALLOWED},
        )
        assert enabled.status_code in (200, 204), enabled.text

        async with create_mcp_http_client(
            headers={"Authorization": f"Bearer {secret}"}, timeout=_MCP_TIMEOUT,
        ) as http_client:
            async with streamable_http_client(
                f"{base_url}/v1/mcp/", http_client=http_client,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    advertised = await session.list_tools()
                    assert {tool.name for tool in advertised.tools} == _META_TOOLS

                    # Discovery: the five generated internal-collections tools are findable by purpose.
                    found = await session.call_tool(
                        "find_tools",
                        arguments={"query": "internal collections subsystem", "limit": 50},
                    )
                    found_ids = {item["scoped_id"] for item in json.loads(_text(found))["items"]}
                    assert _IC_TOOLS <= found_ids, found_ids

                    # List + schema: the config write carries the nested body schema the generated tools use.
                    # The ``$ref`` is a JSON-pointer into the schema's ``$defs`` (``#/$defs/<name>``): the def's key
                    # is the ref's LAST component, not the pointer stripped of its leading ``#/``.
                    listed = await session.call_tool(
                        "list_tools",
                        arguments={"toolset_id": "internal_collections", "include_schema": True},
                    )
                    assert not listed.is_error, _text(listed)
                    catalogue = json.loads(_text(listed))
                    assert {item["scoped_id"] for item in catalogue["items"]} == _IC_TOOLS
                    put_item = next(
                        item for item in catalogue["items"]
                        if item["scoped_id"] == "internal_collections__put_config"
                    )
                    body_schema = put_item["schema"]["properties"]["body"]
                    defs = put_item["schema"].get("$defs", {})
                    ref = defs.get(body_schema.get("$ref", "").rsplit("/", 1)[-1], body_schema)
                    assert set(ref.get("required", [])) >= {
                        "embedding_provider_id", "embedding_model", "search_provider_id",
                    }

                    # The flow: the config write with the nested body, the bootstrap, the durable rereads, the real query.
                    put = await session.call_tool(
                        "call_tool",
                        arguments={
                            "scoped_id": "internal_collections__put_config",
                            "arguments": {
                                "body": {
                                    "embedding_provider_id": "huggingface",
                                    "embedding_model": _EMBEDDING_MODEL,
                                    "search_provider_id": "lance",
                                }
                            },
                        },
                    )
                    config = _call_output(put)
                    assert config["embedding_model"] == _EMBEDDING_MODEL, config

                    boot = await session.call_tool(
                        "call_tool",
                        arguments={"scoped_id": "internal_collections__bootstrap", "arguments": {}},
                    )
                    # A failed bootstrap is a real failure of the reserved local embedder the journey depends on
                    # (the runbook's refusal table), not a reason to skip: the shared e2e database keeps state, and
                    # a skipped run would leave it configured.
                    outcome = _call_output(boot)
                    assert outcome["status"] == "succeeded", outcome
                    assert outcome["state"] == "ready", outcome

                    # Durable rereads over the same door: the config row and the bootstrap status row.
                    reread = _call_output(await session.call_tool(
                        "call_tool",
                        arguments={
                            "scoped_id": "internal_collections__get_config",
                            "arguments": {"allow_missing": True},
                        },
                    ))
                    assert reread["embedding_model"] == _EMBEDDING_MODEL, reread
                    assert reread["search_provider_id"] == "lance", reread

                    status = _call_output(await session.call_tool(
                        "call_tool",
                        arguments={"scoped_id": "internal_collections__bootstrap_status", "arguments": {}},
                    ))
                    assert status["state"] == "ready", status
                    assert status["documents_indexed"] == status["documents_total"] > 0, status

                    # And the behavior: a real semantic query the freshly indexed corpus must answer.
                    search = _call_output(await session.call_tool(
                        "call_tool",
                        arguments={
                            "scoped_id": "collections__search",
                            "arguments": {
                                "collection": "system",
                                "query": "enable the internal collections subsystem",
                                "limit": 5,
                            },
                        },
                    ))
                    assert search["hits"], (
                        "a semantic query against the freshly bootstrapped system collection must return hits; got "
                        f"{search!r}"
                    )

                    # Deactivation through the same door, and the durable state it leaves.
                    await _call_output(await session.call_tool(
                        "call_tool",
                        arguments={"scoped_id": "internal_collections__delete_config", "arguments": {}},
                    ))
                    after = _call_output(await session.call_tool(
                        "call_tool",
                        arguments={"scoped_id": "internal_collections__bootstrap_status", "arguments": {}},
                    ))
                    assert after["state"] == "disabled", after
    except BaseException as exc:
        # The body's failure is the test's failure; the cleanup failures the finally collects ride it
        # as a note, so this traceback stays the one pytest points at.
        body_failure = exc
        raise
    finally:
        # Every cleanup step is attempted even when an earlier one fails: the shared e2e database keeps
        # the subsystem's state, and a half-cleaned run would poison the next one.
        cleanup_failures: list[str] = []
        try:
            # Always de-activate, whatever the flow left behind: a configured subsystem would make the
            # next run's config write a conflict. 404 when no config row was ever written (the flow
            # failed before the write) is the clean case.
            cleared = await client.delete("/v1/internal_collections/config")
            assert cleared.status_code in (204, 404), cleared.text
        except Exception as exc:
            cleanup_failures.append(f"config de-activation: {exc}")
        if before is not None:
            try:
                restored = await client.put(
                    "/v1/mcp_exposure",
                    json={
                        "enabled": before["enabled"],
                        "mode": before.get("mode", "allowlist"),
                        "allowed_tools": before.get("allowed_tools", []),
                    },
                )
                assert restored.status_code in (200, 204), restored.text
            except Exception as exc:
                cleanup_failures.append(f"exposure restore: {exc}")
        for tool, row in originals.items():
            try:
                back = await client.put(
                    f"/v1/tool_approval_policies/{_POLICY_IDS[tool]}", json=row,
                )
                assert back.status_code == 200, back.text
            except Exception as exc:
                cleanup_failures.append(f"policy restore of {_POLICY_IDS[tool]}: {exc}")
        if token_id is not None:
            try:
                revoked = await client.delete(f"/v1/auth/tokens/{token_id}")
                assert revoked.status_code == 204, revoked.text
            except Exception as exc:
                cleanup_failures.append(f"token revoke: {exc}")
        if cleanup_failures:
            if body_failure is not None:
                body_failure.add_note("the finally cleanup also failed: " + "; ".join(cleanup_failures))
            else:
                raise AssertionError("the flow passed but its cleanup failed: " + "; ".join(cleanup_failures))
