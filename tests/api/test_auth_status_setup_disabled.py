"""S5 P2: the setup gate keys on the setup fact, never on users.

With auth disabled the middleware injects a synthetic admin, so the probe
must still answer setup_complete=false on an unconfigured install (and the
console must still show the wizard). The auth-disabled switch is flipped on
the app fixture's live config exactly as tests/api/middleware/
test_auth_disabled.py:20-30 does; there is no auth-disabled client fixture
and this module must not invent one.
"""
from __future__ import annotations

import httpx
from httpx import ASGITransport

from primer.model.model_profile import ModelProfile


async def test_auth_disabled_still_reports_the_setup_fact(app):
    app.state.config = app.state.config.model_copy(
        update={
            "auth": app.state.config.auth.model_copy(update={"enabled": False}),
        }
    )
    app.state.storage_provider.get_storage(ModelProfile)._data.clear()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.get("/v1/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["setup_complete"] is False
    # The synthetic user is an admin, so the console shows the WIZARD here,
    # not the waiting screen.
    assert body["role"] == "admin"
