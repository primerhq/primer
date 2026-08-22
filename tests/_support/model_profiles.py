"""Seeding helpers for :class:`primer.model.model_profile.ModelProfile`.

An agent names a model PROFILE, not a provider + model pair. The journey
suites still think in terms of "this model on this provider", because that
is the fact each test is actually about, so these helpers derive the
profile id from the pair and create the row.

The id rule is the same one the ``m002`` data migration uses, so a profile
seeded here has the id a migrated deployment would have produced. That
matters beyond tidiness: a test that seeds the same pair twice converges on
one row instead of colliding, which is why every helper here tolerates the
409 a duplicate id produces.

Both an async (``httpx.AsyncClient``) and a sync (``base_url``) variant
exist because tests/e2e drives an async client while tests/ui_e2e seeds
over plain sync HTTP alongside Playwright.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

_UNSAFE = re.compile(r"[^a-z0-9-]+")

# Discovery probes cannot report a context window and neither can a mock,
# so seeded profiles get a window large enough that no journey test trips
# the compaction threshold by accident.
DEFAULT_CONTEXT_LENGTH = 32_768


def profile_id_for(provider_id: str, model_name: str) -> str:
    """Deterministic profile id for a (provider, model) pair.

    Mirrors ``primer.storage.migrations.m002_model_profiles.synth_profile_id``
    so ids seeded by tests and ids produced by the migration agree.
    """
    slug = _UNSAFE.sub("-", model_name.lower()).strip("-")
    return f"{provider_id}--{slug}"


def agent_model(provider_id: str, model_name: str) -> dict[str, str]:
    """The ``model`` block of an agent create body.

    Use with :func:`seed_profile` / :func:`seed_profile_sync`: this only
    names the profile, it does not create it.
    """
    return {"profile_id": profile_id_for(provider_id, model_name)}


def profile_body(
    provider_id: str,
    model_name: str,
    *,
    context_length: int = DEFAULT_CONTEXT_LENGTH,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": profile_id_for(provider_id, model_name),
        "description": f"{model_name} on {provider_id}",
        "provider_id": provider_id,
        "model_name": model_name,
        "context_length": context_length,
        "config": config or {},
    }


def _check(status: int, text: str, pid: str) -> None:
    # 409 means this pair was already seeded. Two tests naming the same
    # model on the same provider is normal and must not fail the second.
    if status not in (200, 201, 409):
        raise AssertionError(f"seed model profile {pid!r} failed: {status} {text}")


async def seed_profile(
    client: httpx.AsyncClient,
    provider_id: str,
    model_name: str,
    *,
    context_length: int = DEFAULT_CONTEXT_LENGTH,
    config: dict[str, Any] | None = None,
) -> str:
    """Create the profile for one (provider, model) pair; return its id."""
    body = profile_body(
        provider_id, model_name, context_length=context_length, config=config,
    )
    r = await client.post("/v1/model_profiles", json=body)
    _check(r.status_code, r.text, body["id"])
    return body["id"]


def seed_profile_sync(
    base_url: str,
    provider_id: str,
    model_name: str,
    *,
    context_length: int = DEFAULT_CONTEXT_LENGTH,
    config: dict[str, Any] | None = None,
) -> str:
    """Sync counterpart of :func:`seed_profile` for the ui_e2e suite."""
    body = profile_body(
        provider_id, model_name, context_length=context_length, config=config,
    )
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/model_profiles", json=body)
    _check(r.status_code, r.text, body["id"])
    return body["id"]


def seed_profile_with(client_or_c: Any, provider_id: str, model_name: str, **kw):
    """Seed through an already-open sync client (``httpx.Client``).

    Some ui_e2e helpers hold one client open across several seeds; opening
    a second one per profile would work but wastes a connection per call.
    """
    body = profile_body(provider_id, model_name, **kw)
    r = client_or_c.post("/v1/model_profiles", json=body)
    _check(r.status_code, r.text, body["id"])
    return body["id"]


async def seed_llm_provider(
    client: httpx.AsyncClient, body: dict[str, Any], **request_kw: Any,
):
    """POST an LLM provider, then a ModelProfile per declared model.

    ``body["models"]`` is the test's declaration of what this provider
    serves. The field no longer exists on ``LLMProvider`` -- profiles are
    the registry -- so it is popped here and each entry becomes a profile,
    exactly the translation the ``m002`` migration performs on stored rows.
    Keeping the declaration next to the provider keeps each test's setup in
    one place instead of splitting it across two POSTs.

    Returns the PROVIDER response so existing status assertions still read
    naturally. If the provider POST failed (tests that deliberately provoke
    a 409 or 422), no profiles are created.
    """
    models = body.pop("models", None) or []
    # request_kw forwards per-call transport options (timeout, headers) --
    # the oversized-body tests need a longer timeout than the default.
    r = await client.post("/v1/llm_providers", json=body, **request_kw)
    if r.status_code not in (200, 201):
        return r
    pid = body.get("id") or r.json().get("id")
    for m in models:
        await seed_profile(
            client, pid, m["name"],
            context_length=m.get("context_length", DEFAULT_CONTEXT_LENGTH),
        )
    return r


def seed_llm_provider_sync(base_url: str, body: dict[str, Any]):
    """Sync counterpart of :func:`seed_llm_provider` for the ui_e2e suite."""
    models = body.pop("models", None) or []
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json=body)
        if r.status_code not in (200, 201):
            return r
        pid = body.get("id") or r.json().get("id")
        for m in models:
            seed_profile_with(
                c, pid, m["name"],
                context_length=m.get("context_length", DEFAULT_CONTEXT_LENGTH),
            )
    return r


def seed_llm_provider_with(c: Any, body: dict[str, Any]):
    """:func:`seed_llm_provider` through an already-open sync client.

    The ui_e2e helpers open one ``httpx.Client`` and seed several entities
    inside it, so this takes the client rather than a base URL.
    """
    models = body.pop("models", None) or []
    r = c.post("/v1/llm_providers", json=body)
    if r.status_code not in (200, 201):
        return r
    pid = body.get("id") or r.json().get("id")
    for m in models:
        seed_profile_with(
            c, pid, m["name"],
            context_length=m.get("context_length", DEFAULT_CONTEXT_LENGTH),
        )
    return r


def profile_manifests(provider_id: str, models: list[dict[str, Any]]):
    """``(name, kind, spec)`` triples for one profile per declared model.

    The cookbook tests declare a provider's models inline the way
    they always did; this turns that declaration into the manifests they
    now need, so each test keeps saying what it serves in one place.
    """
    out = []
    for m in models:
        body = profile_body(
            provider_id, m["name"],
            context_length=m.get("context_length", DEFAULT_CONTEXT_LENGTH),
        )
        out.append((body["id"].replace(":", "-"), "model_profile", body))
    return out
