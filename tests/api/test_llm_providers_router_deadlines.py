"""RED probe (D-A3-04): the LLM provider config must reject both generation deadlines as None.

``Limits.request_timeout_seconds`` (the per-event stall timeout) and
``Limits.total_timeout_seconds`` (the runaway-generation backstop) may each
be None to disable, but with both None a streamed generation is unbounded:
no stall window, no ceiling. The write path must refuse that pair with a
clear message (at least one generation deadline must be set) and keep
accepting a limits block that sets one.
"""

from __future__ import annotations

import pytest


def _body(provider_id: str, limits: dict) -> dict:
    return {
        "id": provider_id,
        "description": "deadline probe",
        "provider": "openresponses",
        "config": {"url": "http://example.invalid", "flavor": "other", "api_key": "sk-x"},
        "limits": limits,
    }


@pytest.mark.asyncio
async def test_both_generation_deadlines_none_is_rejected(client) -> None:
    r = await client.post(
        "/v1/llm_providers",
        json=_body("both-none", {"max_concurrency": 1, "request_timeout_seconds": None}),
    )
    assert r.status_code == 422, (
        f"request_timeout_seconds=None AND total_timeout_seconds=None must be rejected: "
        f"{r.status_code} {r.text}"
    )
    r2 = await client.post(
        "/v1/llm_providers",
        json=_body(
            "single-deadline",
            {"max_concurrency": 1, "request_timeout_seconds": None, "total_timeout_seconds": 600},
        ),
    )
    assert r2.status_code in (200, 201), (
        f"a limits block with one deadline set must still be accepted: {r2.status_code} {r2.text}"
    )
