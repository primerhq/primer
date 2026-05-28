"""U0XXX — chat survives page refresh mid-LLM-stream.

Boots an LM-Studio backed chat, sends a long-form prompt, refreshes
the browser tab a moment after the first token lands, and asserts the
assistant response completes (``· done`` marker) without operator
intervention.

Pins the chat-turn-detachment contract end-to-end:

* worker pool drives the turn, not the WS coroutine;
* refresh kills the WS but the worker keeps streaming tokens into
  storage;
* on reconnect the WS replays from ``cursor=0`` and tails new rows
  via the tick router until the turn terminates with ``done``.

Requires the LM Studio config from ``docs/testing/02-bringup.md``
(``http://127.0.0.1:8080`` with bearer ``***REMOVED***``).
"""

from __future__ import annotations

import os
import time

import httpx
import pytest
from playwright.sync_api import expect


_LMSTUDIO_URL = os.environ.get(
    "PRIMER_E2E_LMSTUDIO_URL", "http://127.0.0.1:8080",
)
_LMSTUDIO_TOKEN = os.environ.get(
    "PRIMER_E2E_LMSTUDIO_TOKEN", "***REMOVED***",
)
_LMSTUDIO_MODEL = os.environ.get(
    "PRIMER_E2E_LMSTUDIO_MODEL", "google/gemma-4-e4b",
)


def _lmstudio_reachable() -> bool:
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(
                f"{_LMSTUDIO_URL}/v1/models",
                headers={"Authorization": f"Bearer {_LMSTUDIO_TOKEN}"},
            )
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(
    not _lmstudio_reachable(),
    reason=f"LM Studio at {_LMSTUDIO_URL} is not reachable",
)
def test_chat_survives_refresh_mid_stream(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    provider_id = f"u-resume-llm-{unique_suffix}"
    agent_id = f"u-resume-ag-{unique_suffix}"

    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": provider_id,
            "provider": "openresponses",
            "config": {
                "url": f"{_LMSTUDIO_URL}/v1",
                "api_key": _LMSTUDIO_TOKEN,
                "flavor": "lmstudio",
            },
            "models": [
                {"name": _LMSTUDIO_MODEL, "context_length": 4096},
            ],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm: {r.text}"
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "chat resume journey agent",
            "model": {
                "provider_id": provider_id,
                "model_name": _LMSTUDIO_MODEL,
            },
            "tools": [],
            "system_prompt": [
                "Reply at length so the answer takes a few seconds to "
                "stream — at least two full paragraphs."
            ],
        })
        assert r.status_code == 201, f"seed agent: {r.text}"
        r = c.post("/v1/chats", json={"agent_id": agent_id})
        assert r.status_code == 201, f"seed chat: {r.text}"
        chat_id = r.json()["id"]

    cleanup_urls = [
        f"/v1/chats/{chat_id}",
        f"/v1/agents/{agent_id}",
        f"/v1/llm_providers/{provider_id}",
    ]
    try:
        # Open the chat detail page directly.
        page.goto(
            f"{console_url}#/chats/{chat_id}",
            wait_until="domcontentloaded",
        )

        # Send a long-form prompt via the composer.
        composer = page.locator(
            "textarea[placeholder='Send a message…']",
        )
        expect(composer).to_be_visible(timeout=10_000)
        composer.fill(
            "Please describe the history of computing in detail, "
            "from Babbage to modern transformers.",
        )
        page.get_by_role("button", name="Send", exact=True).first.click()

        # Wait for the user message to land in the transcript (kicks
        # off the worker). Then give the LLM a moment to start emitting
        # tokens — we want the refresh to land MID-stream, not before
        # the worker has even claimed the row.
        page.locator("text=Please describe the history").first.wait_for(
            state="visible", timeout=15_000,
        )
        time.sleep(1.5)

        # Refresh. The WS coroutine dies; the worker keeps streaming.
        page.reload(wait_until="domcontentloaded")

        # Eventually the turn completes with the `· done` marker. The
        # window is generous because gemma-4-e4b on LM Studio can take
        # 30-60s for a long answer.
        page.locator("text=· done").first.wait_for(
            state="visible", timeout=120_000,
        )
    finally:
        with httpx.Client(base_url=base_url, timeout=10.0) as c:
            for url in cleanup_urls:
                try:
                    c.delete(url)
                except Exception:  # noqa: BLE001
                    pass
