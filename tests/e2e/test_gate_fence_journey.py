"""E2E: a decision names the gate it answers (console review C-033, ticket 01a11f52-9d98).

A provider repeats its tool_call_id across rounds, so a card left open for round 1's gate used to decide whatever gate was pending under the same
id in round 3. Each park now carries a ``gate_id``; the pending routes serve it, the respond routes take it back, and a decision naming a gate that
has been replaced is a 409 ``approval_stale`` over the real HTTP surface and the real Postgres row, which stays parked.

The parks are injected into the session row (the shape the worker writes), once for "round 1" and again for "round 3" under the same raw id, as
``test_yields_with_injected_park`` does: no model is needed.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.test_yields_with_injected_park import _cleanup, _inject_park, _read_parked_status, _seed_ladder

pytestmark = pytest.mark.asyncio

G1 = "1" * 32
G2 = "2" * 32


async def _park_twice_under_one_call_id(sid: str, *, tool_name: str, tool_call_id: str, event_key: str, metadata) -> None:
    """Round 1 parks under G1; the card is left open; round 3 parks again under the SAME tool_call_id with G2."""
    for gate_id in (G1, G2):
        await _inject_park(
            sid, tool_name=tool_name, tool_call_id=tool_call_id, event_key=event_key, prompt="Which currency?",
            extra_metadata={**metadata, "gate_id": gate_id},
        )


async def test_a_stale_ask_user_card_cannot_answer_the_later_question(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"call_0-{unique_suffix}"
    try:
        await _park_twice_under_one_call_id(sid, tool_name="ask_user", tool_call_id=tcid, event_key=f"ask_user:{sid}:{tcid}", metadata={})

        pending = await client.get(f"/v1/sessions/{sid}/ask_user/pending")
        assert pending.status_code == 200, pending.text
        assert pending.json()["gate_id"] == G2, "the pending prompt is the later one"

        stale = await client.post(f"/v1/sessions/{sid}/ask_user/respond", json={"tool_call_id": tcid, "gate_id": G1, "response": "EUR"})
        assert stale.status_code == 409, stale.text
        assert stale.headers["content-type"].startswith("application/problem+json")
        assert stale.json()["extensions"]["code"] == "approval_stale"
        assert await _read_parked_status(sid) == "parked", "a stale answer moved the later question"

        malformed = await client.post(f"/v1/sessions/{sid}/ask_user/respond", json={"tool_call_id": tcid, "gate_id": "nope", "response": "EUR"})
        assert malformed.status_code == 422, malformed.text

        current = await client.post(f"/v1/sessions/{sid}/ask_user/respond", json={"tool_call_id": tcid, "gate_id": G2, "response": "EUR"})
        assert current.status_code == 202, current.text
        assert await _read_parked_status(sid) != "parked", "the answer naming the pending prompt did not resume it"
    finally:
        await _cleanup(client, cleanup_urls)


async def test_a_stale_approval_card_cannot_decide_the_later_gate(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"call_0-{unique_suffix}"
    original_call = {"id": tcid, "name": "delete_workspace", "arguments": {"id": "ws-x"}}
    try:
        await _park_twice_under_one_call_id(
            sid, tool_name="_approval", tool_call_id=tcid, event_key=f"tool_approval:{sid}:{tcid}",
            metadata={"policy_id": "pol", "approval_type": "required", "gate_reason": "matched policy", "approvers": None, "original_call": original_call},
        )

        pending = await client.get(f"/v1/sessions/{sid}/tool_approval/pending")
        assert pending.status_code == 200, pending.text
        assert pending.json()["gate_id"] == G2

        stale = await client.post(f"/v1/sessions/{sid}/tool_approval/respond", json={"tool_call_id": tcid, "gate_id": G1, "decision": "approved"})
        assert stale.status_code == 409, stale.text
        assert stale.json()["extensions"]["code"] == "approval_stale"
        assert await _read_parked_status(sid) == "parked", "a stale Approve moved the later gate"
        records = await client.get("/v1/tool_approval/records", params={"session_id": sid})
        assert records.status_code == 200 and records.json()["items"] == [], "a stale decision left an audit record"

        current = await client.post(f"/v1/sessions/{sid}/tool_approval/respond", json={"tool_call_id": tcid, "gate_id": G2, "decision": "rejected", "reason": "no"})
        assert current.status_code == 202, current.text
    finally:
        await _cleanup(client, cleanup_urls)


async def test_a_respond_that_names_no_gate_is_still_accepted_while_clients_catch_up(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"call_0-{unique_suffix}"
    try:
        await _inject_park(
            sid, tool_name="ask_user", tool_call_id=tcid, event_key=f"ask_user:{sid}:{tcid}", prompt="Which currency?",
            extra_metadata={"gate_id": G1},
        )

        bare = await client.post(f"/v1/sessions/{sid}/ask_user/respond", json={"tool_call_id": tcid, "response": "EUR"})

        assert bare.status_code == 202, bare.text
    finally:
        await _cleanup(client, cleanup_urls)
