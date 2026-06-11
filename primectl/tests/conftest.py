import json
from pathlib import Path

import httpx
import pytest

from primectl.client import ApiClient
from primectl.config import Target

FIXTURE = Path(__file__).parent / "fixtures" / "openapi_sample.json"


@pytest.fixture
def sample_spec() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def mock_session(monkeypatch, tmp_path, sample_spec):
    """A Session whose client is backed by a programmable MockTransport.

    Use ``mock_session.set_handler(fn)`` to control responses. The OpenAPI
    fetch is auto-served from the sample fixture.
    """
    from primectl import session as session_mod
    from primectl.session import Session

    state = {"handler": lambda request: httpx.Response(200, json={})}

    def root_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/openapi.json":
            return httpx.Response(200, json=sample_spec)
        return state["handler"](request)

    def fake_make_client(target, *, verbose=False):
        return ApiClient(
            target.server, token=target.token,
            transport=httpx.MockTransport(root_handler),
        )

    monkeypatch.setattr(session_mod, "make_client", fake_make_client)
    # discovery cache in a temp dir so tests never touch ~/.primectl
    monkeypatch.setenv("PRIMECTL_CONFIG", str(tmp_path / "config.yaml"))

    target = Target(server="http://test", token=None, workspace=None, context_name="test")
    sess = Session(target=target)
    # cache spec under tmp so load_spec doesn't write to home
    monkeypatch.setattr(
        "primectl.discovery.default_cache_dir", lambda: tmp_path / "cache"
    )

    class Wrapper:
        session = sess
        def set_handler(self, fn):
            state["handler"] = fn

    return Wrapper()
