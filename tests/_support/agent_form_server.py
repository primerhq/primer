"""The REAL routers the console's agent form talks to, for the UI tests that read what they answer (audit G3-02 and G3-03).

The agent form's tests used to feed it error shapes written by hand (``{"fieldErrors": [{"loc": ["body", "tools"], ...}]}``), a shape the
server never sends for a refused tool binding, so they passed while a real refusal landed in a toast (audit G3-03). ``agent_form_server()``
answers through ``TestClient`` from a FastAPI app with the API's own ``register_error_handlers`` and the routers the form calls, included
the way ``primer/api/_app_routes.py`` includes them: ``model_profile_choices_router`` behind ``require_user``, the admin-only
``model_profile_router`` behind ``require_admin``, and ``compute.agent_router`` behind ``require_user``. The storage is the root suite's
in-memory fake, holding only the profiles and agents the test names.

The caller's role is a header (``x-test-role``), turned into the signed-in ``User`` the auth middleware would set: what a route answers to
a ``user`` account and to an ``admin`` is the router's own decision, not the test's.
"""

# No ``from __future__ import annotations`` here: the middleware below is defined inside the function, and FastAPI resolves its parameter
# annotations from the real objects.

import contextlib
from collections.abc import Iterator
from typing import Any


class AgentFormServer:
    """A TestClient over the real routers, plus the rows it was seeded with."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def request(self, method: str, path: str, body: dict | None = None, *, role: str = "admin") -> tuple[int, Any]:
        """``(status, json)`` of one request made as an account with ``role``."""
        r = self._client.request(method, path, json=body, headers={"x-test-role": role})
        return r.status_code, (r.json() if r.content else None)

    def refusal(self, method: str, path: str, body: dict | None = None, *, role: str = "admin", status: int = 422) -> dict:
        """The problem envelope the route refuses this request with; fails when it answers anything but ``status``."""
        code, envelope = self.request(method, path, body, role=role)
        assert code == status, (code, envelope)
        return envelope


@contextlib.contextmanager
def agent_form_server(*, profiles: list[dict], agents: tuple[dict, ...] | list[dict] = ()) -> Iterator[AgentFormServer]:
    """The routers over a store holding exactly ``profiles`` (ModelProfile bodies) and ``agents`` (Agent bodies)."""
    from datetime import datetime, timezone

    from fastapi import Depends, FastAPI, Request
    from fastapi.testclient import TestClient

    from primer.api.deps import require_admin, require_user
    from primer.api.errors import register_error_handlers
    from primer.api.routers.compute import agent_router
    from primer.api.routers.model_profiles import model_profile_choices_router, model_profile_router
    from primer.model.agent import Agent
    from primer.model.model_profile import ModelProfile
    from primer.model.user import User
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    profile_rows = sp.get_storage(ModelProfile)._data  # noqa: SLF001
    profile_rows.clear()  # the fake seeds the suite's profile vocabulary; the form's tests name their own
    for body in profiles:
        row = ModelProfile.model_validate(body)
        profile_rows[row.id] = row
    agent_rows = sp.get_storage(Agent)._data  # noqa: SLF001
    for body in agents:
        row = Agent.model_validate(body)
        agent_rows[row.id] = row

    app = FastAPI()
    register_error_handlers(app)
    app.state.storage_provider = sp
    app.state.provider_registry = object()  # read by no route here; the dependency only checks that the lifespan set it

    @app.middleware("http")
    async def _sign_in(request: Request, call_next):
        role = request.headers.get("x-test-role")
        if role:
            request.state.user = User(id=f"u-{role}", username=role, role=role, created_at=datetime.now(timezone.utc))
        return await call_next(request)

    # As primer/api/_app_routes.py includes them: the choices before the admin router, whose GET /{id} would read "choices" as an id.
    app.include_router(model_profile_choices_router, prefix="/v1", dependencies=[Depends(require_user)])
    app.include_router(model_profile_router, prefix="/v1", dependencies=[Depends(require_admin)])
    app.include_router(agent_router, prefix="/v1", dependencies=[Depends(require_user)])

    with TestClient(app, raise_server_exceptions=False) as client:
        yield AgentFormServer(client)


__all__ = ["AgentFormServer", "agent_form_server"]
