"""The service dialog hands the SAVED row to its caller (ADM-06 of the 2026-10-08 admin review).

The Platform page hosts ``SV_ServiceModal`` for "New service" and, like the toolset and trigger forms, lands the operator on the created row's
detail overlay. That needs the row. The dialog used to call ``onSaved()`` with no argument, so a host could refresh its list but not find out which
row was created. The request is now a pure function, ``SV_saveService(apiFetch, existing, fields)``, which resolves to the server's answer, and the
dialog passes that answer to ``onSaved``.

``SV_saveService`` has no JSX and no ``window`` dependency, so it runs here in MiniRacer against the real source. The dialog itself is JSX; its
hand-off is a source check (this checkout has no render harness for it).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVICES = (ROOT / "ui" / "components" / "services.jsx").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _helper_src() -> str:
    start = SERVICES.index("function SV_saveService(")
    end = SERVICES.index("\nfunction ", start + 1)
    return SERVICES[start:end]


def _ctx(*, reject: str | None = None):
    """``calls`` records every apiFetch (method, path, body); the server answers with ``{id: "svc-1", ...body}`` unless ``reject`` is a JS object."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    answer = (
        f"Promise.reject({reject})"
        if reject
        else 'Promise.resolve(Object.assign({ id: "svc-1", state: "ok" }, body))'
    )
    ctx.eval(
        "var calls = [];"
        f"function apiFetch(method, path, body) {{ calls.push([method, path, body]); return {answer}; }}"
    )
    ctx.eval(_helper_src())
    return ctx


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def test_a_new_service_posts_exactly_the_three_fields_and_resolves_the_created_row() -> None:
    ctx = _ctx()

    ctx.eval(
        'var saved = null; SV_saveService(apiFetch, null, { name: "status-page", description: "d", viewer_auth: "console" })'
        ".then(function (r) { saved = r; });"
    )

    assert _js(ctx, "calls") == [["POST", "/services", {"name": "status-page", "description": "d", "viewer_auth": "console"}]]
    assert _js(ctx, "saved") == {"id": "svc-1", "state": "ok", "name": "status-page", "description": "d", "viewer_auth": "console"}


def test_an_edit_puts_the_existing_row_with_the_three_fields_replaced_and_resolves_the_saved_row() -> None:
    ctx = _ctx()
    existing = {"id": "svc-9", "name": "old", "description": "old d", "viewer_auth": "console", "active_version_id": "v1"}

    ctx.eval(
        f"var saved = null; SV_saveService(apiFetch, {json.dumps(existing)}, "
        '{ name: "new", description: "new d", viewer_auth: "none" }).then(function (r) { saved = r; });'
    )

    assert _js(ctx, "calls") == [
        ["PUT", "/services/svc-9", {**existing, "name": "new", "description": "new d", "viewer_auth": "none"}],
    ]
    assert _js(ctx, "saved")["id"] == "svc-9"


def test_the_id_is_url_encoded_in_the_edit_path() -> None:
    ctx = _ctx()

    ctx.eval('SV_saveService(apiFetch, { id: "a b/c" }, { name: "n", description: "d", viewer_auth: "console" });')

    assert _js(ctx, "calls[0][1]") == "/services/a%20b%2Fc"


def test_a_refused_save_rejects_with_the_servers_error_untouched() -> None:
    ctx = _ctx(reject='{ detail: "name taken", requestId: "req-1" }')

    ctx.eval(
        "var failure = null; SV_saveService(apiFetch, null, { name: 'n', description: 'd', viewer_auth: 'console' })"
        ".then(null, function (e) { failure = e; });"
    )

    assert _js(ctx, "failure") == {"detail": "name taken", "requestId": "req-1"}


def test_the_dialog_hands_the_saved_row_to_its_caller() -> None:
    dialog = SERVICES[SERVICES.index("function SV_ServiceModal("):SERVICES.index("function SV_ServiceDetail(")]

    assert "SV_saveService(apiFetch, existing," in dialog, "the dialog must save through the tested helper"
    assert re.search(r"onSaved && onSaved\(saved\)", dialog), "the caller must be told which row was saved"
    assert "apiFetch(\"POST\"" not in dialog and "apiFetch(\"PUT\"" not in dialog, "the request lives in the helper, not twice"


def test_the_legacy_list_still_refreshes_on_save() -> None:
    """The list page ignores the argument: it closes the dialog and refetches, as before."""
    assert re.search(r"onSaved=\{\(\) => \{ setCreating\(false\); list\.refetch\(\); \}\}", SERVICES)
