"""Live API coverage for rejecting a catastrophic channel-event regex."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_pathological_text_pattern_is_an_rfc7807_validation_error(
    client: httpx.AsyncClient,
    unique_suffix: str,
) -> None:
    created = await client.post(
        "/v1/triggers",
        json={
            "slug": f"redos-{unique_suffix}",
            "name": "ReDoS validation",
            "config": {
                "kind": "channel",
                "provider_id": "cp-redos-e2e",
            },
        },
    )
    assert created.status_code == 201, created.text
    trigger_id = created.json()["id"]

    try:
        response = await client.post(
            f"/v1/triggers/{trigger_id}/subscriptions",
            json={
                "config": {
                    "kind": "session_append",
                    "session_id": "sess-redos-e2e",
                },
                "event_matcher": {
                    "event_type": "message.posted",
                    "text_pattern": r"(a+)+$",
                },
            },
        )
        assert response.status_code == 422, response.text
        assert response.headers["content-type"].startswith(
            "application/problem+json"
        )
        problem = response.json()
        assert problem["type"] == "/errors/validation-error", problem
        assert problem["extensions"]["code"] == "text_pattern_unsafe", problem
        assert "nested unbounded repetition" in problem["detail"]
    finally:
        deleted = await client.delete(f"/v1/triggers/{trigger_id}")
        assert deleted.status_code == 204, deleted.text
