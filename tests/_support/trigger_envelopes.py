"""The REAL problem envelopes the auth gate and the trigger router answer with, for the UI tests that read them.

A FastAPI app with the real ``register_error_handlers``, the real ``require_user`` dependency and the trigger router's own ``_raise_code`` answers through
``TestClient``, so a test never hand-builds the dict (the #572 review: a hand-built ``extensions.code`` hid that the auth gate puts its code in
``extensions.error`` and sends no message).
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run ``coro`` to completion from sync code, whether or not this thread already runs an event loop.

    The Playwright journeys call these builders from the sync Playwright API, whose thread runs a loop, and ``asyncio.run`` refuses there (round 3 of
    #927). With a loop running the coroutine runs on a fresh loop in a worker thread; with none, here.
    """
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def real_envelopes() -> dict[str, dict]:
    from datetime import datetime, timedelta, timezone

    from fastapi import Depends, FastAPI, Request
    from fastapi.testclient import TestClient

    from primer.api.deps import get_claim_engine, get_event_bus, get_storage_provider, require_user
    from primer.api.errors import register_error_handlers
    from primer.api.routers.triggers import _raise_code, triggers_router
    from primer.model.access import Access
    from primer.model.principal import Principal, PrincipalRef
    from primer.model.trigger import DelayedTriggerConfig, Trigger
    from primer.model.user import User
    from tests.conftest import _FakeStorageProvider

    app = FastAPI()
    register_error_handlers(app)

    # The REAL fire_now and rotate_token routes over the REAL service: an admin's trigger shared read with the signed-in user, who fires it
    # (full needed) and rotates its token (owner needed), so the console reads the 403 ``access_denied`` bodies the routes really answer
    # (access-control P3 Task 4).
    sp = _FakeStorageProvider()
    admin = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
    _run(sp.get_storage(Trigger).create(Trigger(
        id="tr-admin", slug="tr-admin", name="nightly", created_at=datetime.now(timezone.utc), owner=admin,
        config=DelayedTriggerConfig(fire_at=datetime.now(timezone.utc) + timedelta(days=1)),
        access=Access(owners=["u-admin"], grants={"u-1": "read"}),
    )))
    app.include_router(triggers_router)
    app.dependency_overrides[get_storage_provider] = lambda: sp
    app.dependency_overrides[get_claim_engine] = lambda: None
    app.dependency_overrides[get_event_bus] = lambda: None

    @app.middleware("http")
    async def _sign_in(request: Request, call_next):
        role = request.headers.get("x-test-role")
        if role:
            request.state.user = User(id="u-1", username="someone", role=role, created_at=datetime.now(timezone.utc))
        actor_role = request.headers.get("x-test-actor-role")
        if actor_role:
            request.state.actor = Principal(type="user", id="u-1", display="someone", role=actor_role, source="local", user_id="u-1")
        return await call_next(request)

    @app.post("/v1/gated", dependencies=[Depends(require_user)])
    def gated():
        return {}

    @app.post("/v1/not_found")
    def not_found():
        _raise_code(404, "trigger_not_found", "tr-1")

    @app.post("/v1/not_found_underscored_id")
    def not_found_underscored():
        _raise_code(404, "trigger_not_found", "nightly_job")

    @app.post("/v1/not_found_empty")
    def not_found_empty():
        _raise_code(404, "trigger_not_found", "")

    @app.post("/v1/slug")
    def slug():
        _raise_code(409, "trigger_slug_conflict", "slug 'nightly' already in use")

    client = TestClient(app, raise_server_exceptions=False)
    return {
        "session_ended": client.post("/v1/gated").json(),
        "role_refused": client.post("/v1/gated", headers={"x-test-role": "restricted"}).json(),
        "not_found": client.post("/v1/not_found").json(),
        "not_found_underscored_id": client.post("/v1/not_found_underscored_id").json(),
        "not_found_empty": client.post("/v1/not_found_empty").json(),
        "slug": client.post("/v1/slug").json(),
        "router_forbidden": client.post("/v1/triggers/tr-admin/rotate_token", headers={"x-test-actor-role": "user"}).json(),
        "fire_refused": client.post("/v1/triggers/tr-admin/fire_now", headers={"x-test-actor-role": "user"}).json(),
    }


def real_fire_bodies() -> dict[str, dict]:
    """The REAL bodies ``POST /v1/triggers/{id}/fire_now`` answers with, one per outcome.

    The real route function (``fire_now_endpoint``) is called with the service call it makes (``fire_now``) answering the real ``FireResult``, whose delivery rows are the real
    ``SubscriptionDispatchResult`` plus the ``subscription_id`` the dispatcher adds (``primer/trigger/dispatch.py``): a disabled or missing trigger is a 200 with ``skipped: true`` and no
    ``fire_id`` (``tests/api/test_triggers_router.py::test_fire_now_disabled_trigger_returns_skipped``), not an error.
    """
    import json
    from types import SimpleNamespace

    import primer.api.routers.triggers as router
    from primer.trigger.dispatch import FireResult
    from primer.trigger.subscribers import SubscriptionDispatchResult

    def row(subscription_id: str, **fields) -> dict:
        return {"subscription_id": subscription_id, **SubscriptionDispatchResult(**fields).model_dump()}

    results = {
        "skipped": FireResult(skipped=True),
        "fired": FireResult(fire_id="fire-1", results=[row("sub-1", ok=True, artefact_id="sess-1")]),
        "no_subscriptions": FireResult(fire_id="fire-2"),
        "partial": FireResult(fire_id="fire-3", results=[row("sub-1", ok=True), row("sub-2", ok=False, error_code="dispatch_failed", error_message="the target session is gone")]),
        "all_failed": FireResult(fire_id="fire-4", results=[row("sub-1", ok=False, error_code="dispatch_failed", error_message="boom"), row("sub-2", ok=False, error_code="payload_template_failed", error_message="bad template")]),
        "delivery_skipped": FireResult(fire_id="fire-5", results=[row("sub-1", ok=True, skipped=True, error_code="skipped_no_match", error_message="event_matcher did not match")]),
    }
    original = router.fire_now
    bodies: dict[str, dict] = {}
    try:
        for name, result in results.items():
            # The service call's real keywords (the route passes who fires, access-control P3 Task 4): a stale signature is a TypeError here.
            async def answer(*, trigger_id, person, deps, _result=result):
                return _result

            router.fire_now = answer
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())
            response = _run(router.fire_now_endpoint(request=request, trigger_id="tr-1", sp=object(), claim_engine=None, event_bus=None))
            bodies[name] = json.loads(response.body)
    finally:
        router.fire_now = original
    return bodies
