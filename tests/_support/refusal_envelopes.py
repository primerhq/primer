"""The REAL problem envelopes every kind of refused write reaches the browser as, for the UI tests that read them (ticket 01a11cd1-7aaf).

A FastAPI app with the real ``register_error_handlers`` answers through ``TestClient``; each route raises what the real code raises, by calling the
real producer: the auth gate (``require_user``), the trigger router's ``_raise_code``, the agent routers' ``_agent_check_as_rest_error``, the channel
router's ``_as_rest_error`` over the channel check's own conflict, ``build_reference_block_hook`` for ``in_use_by``, ``default_agent_refusal`` for the
default-agent block, a real pydantic request body for a field validation error, and the primer error classes. Nothing here hand-builds an envelope, so
a server-side change in any of them changes what the tests read (the #572 review: a hand-built ``extensions.code`` hid that the auth gate puts its code
in ``extensions.error``).
"""

# No ``from __future__ import annotations`` here: the routes below are defined inside the function, and FastAPI resolves their parameter annotations
# (``Request``, the pydantic body) from the real objects, not from strings that name a local scope.


def refusal_envelopes() -> dict[str, dict]:
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from fastapi import Depends, FastAPI, Request
    from fastapi.testclient import TestClient
    from pydantic import BaseModel, Field

    from primer.api.deps import require_user
    from primer.api.errors import register_error_handlers
    from primer.api.routers._references import ReferenceCheck, build_reference_block_hook
    from primer.api.routers.channels import _as_rest_error
    from primer.api.routers.compute import _agent_check_as_rest_error
    from primer.api.routers.triggers import _raise_code
    from primer.channel.checks import _refuse_if_pair_taken
    from primer.common.entity_checks import EntityCheckError
    from primer.model.channel import Channel
    from primer.model.except_ import ConflictError, NotFoundError, ProviderError
    from primer.model.user import User
    from primer.storage.references import default_agent_refusal

    app = FastAPI()
    register_error_handlers(app)

    @app.middleware("http")
    async def _sign_in(request: Request, call_next):
        role = request.headers.get("x-test-role")
        if role:
            request.state.user = User(id="u-1", username="someone", role=role, created_at=datetime.now(timezone.utc))
        return await call_next(request)

    class _Body(BaseModel):
        name: str = Field(..., min_length=1)
        count: int

    class _Page:
        def __init__(self, items):
            self.items = items

    class _FindsOne:
        """A storage whose ``find`` answers one row, as the single-item page of the reference check and of the channel pair check does."""

        def __init__(self, row_id: str) -> None:
            self._row = SimpleNamespace(id=row_id)

        async def find(self, predicate, page):
            return _Page([self._row])

    @app.post("/v1/gated", dependencies=[Depends(require_user)])
    def gated():
        return {}

    @app.post("/v1/router_code")
    def router_code():
        _raise_code(409, "trigger_slug_conflict", "slug 'nightly' already in use")

    @app.post("/v1/router_code_bare_id")
    def router_code_bare_id():
        _raise_code(404, "trigger_not_found", "nightly_job")

    @app.post("/v1/pre_write")
    def pre_write():
        raise _agent_check_as_rest_error(
            EntityCheckError("validation", "profile 'p-9' does not exist", code="profile_not_found", field="model.profile_id")
        )

    @app.post("/v1/agent_field")
    def agent_field():
        raise _agent_check_as_rest_error(
            EntityCheckError("validation", "an agent id is lowercase letters, digits, - and _", code="agent_id_invalid", field="id")
        )

    @app.post("/v1/validated")
    def validated(body: _Body):
        return {}

    check = ReferenceCheck(child_kind="agent", child_storage=lambda request: _FindsOne("builder"), child_field="model.profile_id")
    reference_hook = build_reference_block_hook([check])

    @app.delete("/v1/in_use_by")
    async def in_use_by(request: Request):
        await reference_hook(SimpleNamespace(id="llm-openchat--scripted:default"), request)

    @app.delete("/v1/in_use_by_session")
    async def in_use_by_session(request: Request):
        session_check = ReferenceCheck(
            child_kind="session", child_storage=lambda r: _FindsOne("sess-0001"), child_field="binding.agent_id",
        )
        await build_reference_block_hook([session_check])(SimpleNamespace(id="ag-1"), request)

    class _State:
        async def get_system_state(self):
            return SimpleNamespace(default_agent_id="ag-1")

    @app.delete("/v1/default_agent")
    async def default_agent():
        refusal = await default_agent_refusal(_State(), "ag-1", exempt="operator")
        raise ConflictError(refusal)

    class _ChannelProvider:
        def get_storage(self, model):
            assert model is Channel
            return _FindsOne("channel-fbf469c47c2a")

    @app.post("/v1/channel_conflict")
    async def channel_conflict():
        entity = SimpleNamespace(provider_id="rev-slack", external_id="C0AAAA0001")
        try:
            await _refuse_if_pair_taken(entity, _ChannelProvider())
        except EntityCheckError as exc:
            raise _as_rest_error(exc) from exc

    @app.get("/v1/not_found")
    def not_found():
        raise NotFoundError("Agent 'ag-9' not found")

    @app.get("/v1/provider_error")
    def provider_error():
        raise ProviderError("the provider answered 502")

    client = TestClient(app, raise_server_exceptions=False)
    return {
        "session_ended": client.post("/v1/gated").json(),
        "role_refused": client.post("/v1/gated", headers={"x-test-role": "restricted"}).json(),
        "router_code": client.post("/v1/router_code").json(),
        "router_code_bare_id": client.post("/v1/router_code_bare_id").json(),
        "pre_write": client.post("/v1/pre_write").json(),
        "agent_field": client.post("/v1/agent_field").json(),
        "validated": client.post("/v1/validated", json={"name": ""}).json(),
        "in_use_by": client.delete("/v1/in_use_by").json(),
        "in_use_by_session": client.delete("/v1/in_use_by_session").json(),
        "default_agent": client.delete("/v1/default_agent").json(),
        "channel_conflict": client.post("/v1/channel_conflict").json(),
        "not_found": client.get("/v1/not_found").json(),
        "provider_error": client.get("/v1/provider_error").json(),
    }
