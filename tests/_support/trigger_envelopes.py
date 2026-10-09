"""The REAL problem envelopes the auth gate and the trigger router answer with, for the UI tests that read them.

A FastAPI app with the real ``register_error_handlers``, the real ``require_user`` dependency and the trigger router's own ``_raise_code`` answers through
``TestClient``, so a test never hand-builds the dict (the #572 review: a hand-built ``extensions.code`` hid that the auth gate puts its code in
``extensions.error`` and sends no message).
"""

from __future__ import annotations


def real_envelopes() -> dict[str, dict]:
    from datetime import datetime, timezone

    from fastapi import Depends, FastAPI, Request
    from fastapi.testclient import TestClient

    from primer.api.deps import require_user
    from primer.api.errors import register_error_handlers
    from primer.api.routers.triggers import _raise_code
    from primer.model.user import User

    app = FastAPI()
    register_error_handlers(app)

    @app.middleware("http")
    async def _sign_in(request: Request, call_next):
        role = request.headers.get("x-test-role")
        if role:
            request.state.user = User(id="u-1", username="someone", role=role, created_at=datetime.now(timezone.utc))
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

    @app.post("/v1/router_forbidden")
    def router_forbidden():
        _raise_code(403, "forbidden_role", "only the trigger's owner or an admin may rotate its webhook token")

    client = TestClient(app, raise_server_exceptions=False)
    return {
        "session_ended": client.post("/v1/gated").json(),
        "role_refused": client.post("/v1/gated", headers={"x-test-role": "restricted"}).json(),
        "not_found": client.post("/v1/not_found").json(),
        "not_found_underscored_id": client.post("/v1/not_found_underscored_id").json(),
        "not_found_empty": client.post("/v1/not_found_empty").json(),
        "slug": client.post("/v1/slug").json(),
        "router_forbidden": client.post("/v1/router_forbidden").json(),
    }


def real_fire_bodies() -> dict[str, dict]:
    """The REAL bodies ``POST /v1/triggers/{id}/fire_now`` answers with, one per outcome.

    The real route function (``fire_now_endpoint``) is called with the service call it makes (``fire_now``) answering the real ``FireResult``, whose delivery rows are the real
    ``SubscriptionDispatchResult`` plus the ``subscription_id`` the dispatcher adds (``primer/trigger/dispatch.py``): a disabled or missing trigger is a 200 with ``skipped: true`` and no
    ``fire_id`` (``tests/api/test_triggers_router.py::test_fire_now_disabled_trigger_returns_skipped``), not an error.
    """
    import asyncio
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
            async def answer(*, trigger_id, deps, _result=result):
                return _result

            router.fire_now = answer
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())
            response = asyncio.run(router.fire_now_endpoint(request=request, trigger_id="tr-1", sp=object(), claim_engine=None, event_bus=None))
            bodies[name] = json.loads(response.body)
    finally:
        router.fire_now = original
    return bodies
