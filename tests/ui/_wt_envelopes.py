"""Real refusal envelopes of the workspace template and workspace routes, for the console's unit tests (platform revamp Plan 08, Tasks 9, 10 and 13).

A console test that shows a refusal must show the one the server really sends, so these tests never write an envelope by hand: ``envelope(method, path, body)``
builds the real app (``create_test_app``) over the in-memory storage provider of ``tests/conftest.py``, signs in, sends the request and returns the status and
the JSON body. The test turns that body into an error with the real ``ApiError`` of ``ui/foundation/api.js``.

The app holds three workspace providers, ``loc`` (Local), ``dock`` (Docker) and ``k3s`` (Kubernetes), and one template on each (``tpl-loc``, ``tpl-dock``,
``tpl-k3s``). ``role="admin"`` sends as the first account (the admin); any other role sends as a second account with that role. The workspace registry's
backend factory records the provider it was asked for and builds nothing (``FACTORY_CALLS``): a refusal is answered before any backend is made, so a test can
also check that none was. Each distinct request is answered once and remembered for the session.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
from functools import cache
from typing import Any

PROVIDERS: tuple[dict[str, Any], ...] = (
    {"id": "loc", "provider": "local", "config": {"kind": "local", "root_path": "/tmp/wt-envelopes-loc"}},
    {
        "id": "dock", "provider": "container",
        "config": {
            "kind": "container", "runtime": "docker",
            "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
            "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"},
        },
    },
    {
        "id": "k3s", "provider": "kubernetes",
        "config": {
            "kind": "kubernetes", "variant": "system", "connection": {"kind": "in_cluster"},
            "namespace": "primer", "reachability": {"kind": "in_cluster"},
        },
    },
)

TEMPLATES: tuple[dict[str, Any], ...] = (
    {"id": "tpl-loc", "description": "Local", "provider_id": "loc", "backend": {"kind": "local"}},
    {"id": "tpl-dock", "description": "Docker", "provider_id": "dock", "backend": {"kind": "container", "cpu_cores": 1, "memory_bytes": 2 * 2**30}},
    {
        "id": "tpl-k3s", "description": "Kubernetes", "provider_id": "k3s",
        "backend": {"kind": "kubernetes", "image": "rt:1.4", "pvc_size": "10Gi"},
        "resources": {"cpu_cores": 2, "memory_bytes": 4 * 2**30},
    },
)

#: The providers the registry was asked to build a backend for, in order, across every request this module answered.
FACTORY_CALLS: list[str] = []


def envelope(method: str, path: str, body: Any = None, *, role: str = "admin") -> tuple[int, dict]:
    """The status and JSON body the real route answers ``method path`` with ``body``, sent as ``role``."""
    status, answer = _answer(method.upper(), path, json.dumps(body, sort_keys=True), role)
    return status, copy.deepcopy(answer)


@cache
def _answer(method: str, path: str, body_json: str, role: str) -> tuple[int, dict]:
    return asyncio.run(_send(method, path, json.loads(body_json), role))


def _factory(provider):
    FACTORY_CALLS.append(provider.id)
    raise RuntimeError("the envelope harness builds no workspace backend")


async def _send(method: str, path: str, body: Any, role: str) -> tuple[int, dict]:
    import httpx
    from httpx import ASGITransport

    from primer.api.app import create_test_app
    from primer.api.registries import ProviderRegistry, WorkspaceRegistry
    from primer.auth.passwords import hash_password
    from primer.model.user import User
    from primer.model.workspace import WorkspaceProvider, WorkspaceTemplate
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    app = create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=ProviderRegistry(
            sp,  # type: ignore[arg-type]
            llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
            cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
        ),
        workspace_registry=WorkspaceRegistry(sp, factory=_factory),  # type: ignore[arg-type]
    )
    for row in PROVIDERS:
        await sp.get_storage(WorkspaceProvider).create(WorkspaceProvider.model_validate(row))
    for row in TEMPLATES:
        await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate.model_validate(row))
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/v1/auth/register", json={"username": "wtadmin", "password": "wtadminpass1"})
        assert r.status_code == 200, r.text
        if role != "admin":
            await sp.get_storage(User).create(User(
                id="user-wtperson", username="wtperson", password_hash=await hash_password("wtpersonpass1"),
                created_at=datetime.now(timezone.utc), role=role,
            ))
            r = await client.post("/v1/auth/login", json={"username": "wtperson", "password": "wtpersonpass1"})
            assert r.status_code == 200, r.text
        r = await client.request(method, path, json=body)
    return r.status_code, (r.json() if r.content else {})
