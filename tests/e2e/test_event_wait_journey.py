"""An agent parks on the event log and a document push wakes it.

The full pipeline, live: system__wait_for_event creates the one-shot
subscription and parks the session; PUT document emits
collection.document_pushed (plus the storage-level document.created);
the leader-elected dispatcher matches the filter and publishes the
park key; the yield listener flips the session; the worker resumes the
turn with the event envelope as the tool result.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.model_profiles import profile_id_for


async def _wait_for(
    client: httpx.AsyncClient,
    session_id: str,
    needle: str,
    *,
    timeout_s: float = 120.0,
) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        log = await client.get(
            f"/v1/sessions/{session_id}/messages", params={"limit": 500},
        )
        if log.status_code == 200 and needle in json.dumps(log.json()):
            return log.json()
        await asyncio.sleep(0.5)
    pytest.fail(f"{needle!r} never reached the transcript of {session_id}")


@pytest.mark.asyncio
async def test_agent_wakes_on_document_push(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str,
) -> None:
    registry, mock_url = mock_llm
    model = f"evwait-{unique_suffix}"
    provider_id = f"llm-evwait-{unique_suffix}"
    profile = profile_id_for(provider_id, model)
    agent_id = f"ev-waiter-{unique_suffix}"
    collection_id = f"evkb-{unique_suffix}"
    marker = f"WOKE-{unique_suffix}"

    # ----- The script: turn 1 parks on the filter, turn 2 reports -----
    registry.register(
        model,
        [
            Rule(
                when_tool_offered="system__wait_for_event",
                when_tool_result=True,
                emit_text=f"{marker} event received",
            ),
            Rule(
                when_tool_offered="system__wait_for_event",
                emit_tool="system__wait_for_event",
                emit_args={
                    "event_types": ["collection.document_pushed"],
                    "fields": [{
                        "path": "payload.collection_id",
                        "op": "eq",
                        "value": collection_id,
                    }],
                    "timeout_seconds": 110.0,
                },
                emit_tool_call_id="call_wait",
            ),
        ],
    )

    pr = await client.post(
        "/v1/llm_providers",
        json={
            "id": provider_id,
            "provider": "openchat",
            "config": {"url": mock_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        },
    )
    assert pr.status_code in (200, 201), pr.text
    prof = await client.post(
        "/v1/model_profiles",
        json={
            "id": profile,
            "description": "e2e event-wait profile",
            "provider_id": provider_id,
            "model_name": model,
            "context_length": 32000,
        },
    )
    assert prof.status_code in (200, 201), prof.text

    ag = await client.post(
        "/v1/agents",
        json={
            "id": agent_id,
            "description": "Waits for platform events and reports them.",
            "model": {"profile_id": profile},
            "tools": ["system__wait_for_event"],
            "prompt": "Wait for the event you were asked about, then report.",
        },
    )
    assert ag.status_code in (200, 201), ag.text

    coll = await client.post(
        "/v1/collections",
        json={"id": collection_id, "description": "event wait target"},
    )
    assert coll.status_code in (200, 201), coll.text

    try:
        # ----- Park the session on the filter -------------------------
        sess = await client.post(
            "/v1/workspaces/primer/sessions",
            json={"binding": {"kind": "agent", "agent_id": agent_id}},
        )
        assert sess.status_code in (200, 201), sess.text
        session_id = sess.json()["id"]
        steer = await client.post(
            f"/v1/workspaces/primer/sessions/{session_id}/steer",
            json={"instruction":
                  f"Tell me when a document lands in {collection_id}"},
        )
        assert steer.status_code in (200, 201, 202), steer.text

        # The park is visible once the yielded record hits the log.
        await _wait_for(client, session_id, "system__wait_for_event")

        # ----- The wake: push a document into the watched collection --
        put = await client.put(
            f"/v1/collections/{collection_id}/documents",
            params={"path": "notes/hello"},
            json={"content": "the awaited document"},
        )
        assert put.status_code in (200, 201), put.text

        log = await _wait_for(client, session_id, marker)
        text = json.dumps(log)
        # The tool result carried the event envelope, not just a nudge.
        assert "collection.document_pushed" in text
        assert collection_id in text

        # The one-shot subscription cleaned up after itself.
        subs = await client.get("/v1/event_subscriptions")
        assert subs.status_code == 200, subs.text
        leftovers = [
            r for r in subs.json()["items"]
            if r.get("sink", {}).get("session_id") == session_id
        ]
        assert leftovers == [], leftovers

        # And the log's read window saw the push.
        events = await client.get(
            "/v1/events",
            params={"event_type": "collection.document_pushed",
                    "entity_kind": "document"},
        )
        assert events.status_code == 200, events.text
        assert any(
            e["payload"].get("collection_id") == collection_id
            for e in events.json()["items"]
        )
    finally:
        await client.delete(f"/v1/agents/{agent_id}")
