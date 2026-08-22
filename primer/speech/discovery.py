"""Live enumeration of an OpenAI-compatible speech endpoint.

Two read-only probes feeding the catalog's model and voice pickers.
They use httpx directly rather than the openai SDK: the SDK has no
``/audio/voices`` method, and the LLM family already sets the precedent
for a plain-httpx probe in
``primer.api.routers.providers._probe_openai_compatible_models``.

Voices are NEVER hardcoded. ``af_heart`` is a Kokoro name and means
nothing to OpenAI, so the answer always comes off the wire.
"""

from __future__ import annotations

from typing import Any

import httpx


# Test seam: when set to an ``httpx.MockTransport`` the helpers below
# route through it instead of the network. Production leaves it None.
_transport_for_tests: httpx.BaseTransport | None = None


async def _get_json(url: str, api_key: str | None, timeout: float) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(
        timeout=timeout, transport=_transport_for_tests,
    ) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
    return data if isinstance(data, dict) else {}


async def list_models(
    *, url: str, api_key: str | None, timeout: float = 10.0,
) -> list[str]:
    """GET ``{url}/models`` and return the model ids."""
    data = await _get_json(f"{str(url).rstrip('/')}/models", api_key, timeout)
    items = data.get("data") or []
    return [
        item["id"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def _voice_names(data: dict[str, Any]) -> list[str] | None:
    voices = data.get("voices")
    if isinstance(voices, list):
        out: list[str] = []
        for voice in voices:
            if isinstance(voice, str):
                out.append(voice)
            elif isinstance(voice, dict) and isinstance(voice.get("id"), str):
                out.append(voice["id"])
        return out
    return None


async def list_voices(
    *, url: str, api_key: str | None, timeout: float = 10.0,
) -> list[str]:
    """GET ``{url}/audio/voices``, falling back to ``{url}/models``.

    Kokoro serves ``/v1/audio/voices``; an endpoint that does not gets
    its model list as the fallback enumeration rather than a hardcoded
    voice table.
    """
    base = str(url).rstrip("/")
    try:
        data = await _get_json(f"{base}/audio/voices", api_key, timeout)
    except Exception:  # noqa: BLE001 -- any failure means "try the fallback"
        return await list_models(url=url, api_key=api_key, timeout=timeout)
    names = _voice_names(data)
    if names is None:
        return await list_models(url=url, api_key=api_key, timeout=timeout)
    return names


__all__ = ["list_models", "list_voices"]
