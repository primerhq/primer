"""docs_url must reach the browser via the server-built JSX bundle.

The console is served as a static index.html (no template seam), so
server-side config reaches the page by being prepended to the
server-built /console/_app.js bundle as a window.__PRIMER_*__ global.
This test asserts docs_url rides that seam.
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from starlette.testclient import TestClient

from primer.api.app import _install_jsx_bundle


def test_docs_url_is_injected_into_the_console_bundle() -> None:
    docs_url = "https://docs.example.test/"
    app = FastAPI()
    _install_jsx_bundle(app, docs_url=docs_url)

    client = TestClient(app)
    resp = client.get("/console/_app.js")
    assert resp.status_code == 200
    body = resp.text
    expected = "window.__PRIMER_DOCS_URL__ = " + json.dumps(docs_url) + ";"
    assert expected in body
    # The preamble must come before the console app code so the global
    # is defined before any console script reads it.
    assert body.index(expected) < body.index("React")
