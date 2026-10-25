"""``/console/canvas/*`` answers 404 (the lead's ruling 3 on S8).

The canvas frame's source (``ui/canvas/*``: renderer.js, html-bootstrap.js, frame.css) is served
ONLY via ``GET /canvas/frame``, which sets the frame's CSP - the canvas's only script control. The
raw static copy of those files under the console origin must not be reachable, so the console's
static mount 404s the ``canvas/`` subtree. Pinned by exercising ``_CachingStaticFiles.get_response``
directly: the ``canvas/`` subtree is refused, the rest of the console still serves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.api._app_middleware import _CachingStaticFiles

UI = Path(__file__).resolve().parents[2] / "ui"


def _scope(path: str) -> dict:
    # A minimal http scope: Starlette's StaticFiles reads scope["headers"] when it
    # builds a 200 response, so the 404 subtree and the served rest both need it.
    return {"type": "http", "method": "GET", "path": path, "headers": []}


@pytest.fixture
def files() -> _CachingStaticFiles:
    """The console's static mount, pointed at the real ui/ dir (so the canvas/ subtree exists)."""
    return _CachingStaticFiles(directory=str(UI), html=True)


async def test_the_console_serves_no_canvas_frame_source(files) -> None:
    """Every file under /console/canvas/* is refused, even one that exists on disk."""
    for path in ("canvas/renderer.js", "canvas/html-bootstrap.js", "canvas/frame.css", "canvas"):
        r = await files.get_response(path, _scope("/console/" + path))
        assert r.status_code == 404, path


async def test_the_rest_of_the_console_still_serves(files) -> None:
    """The 404 is scoped to the canvas/ subtree: the console's own pages and the bundle source still serve."""
    r_index = await files.get_response("index.html", _scope("/console/index.html"))
    assert r_index.status_code == 200
    r_style = await files.get_response("styles.css", _scope("/console/styles.css"))
    assert r_style.status_code == 200
