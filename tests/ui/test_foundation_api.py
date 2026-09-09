"""ui/foundation/api.js — ApiError's 422 handling.

Pure logic (no DOM/fetch involved in the constructor itself), so it is
EXECUTED here via MiniRacer rather than substring-matched, mirroring
tests/ui/test_shell_url.py's own convention for foundation/*.js.

Dogfood defect hunt (2026-09-09): the constructor used to branch on
`envelope.status === 422` ALONE, discarding the backend's own `detail`
for EVERY 422 - including a domain-level PrimerError (e.g.
primer/session/rewind.py's ValidationError, "seq 1 is the newest
visible record; nothing to discard"), which never populates
`extensions.errors` the way FastAPI's own RequestValidationError does.
The operator saw a generic "Some required fields are missing or
invalid." instead of the real, specific reason. Fixed to branch on the
SHAPE of the payload (a non-empty `extensions.errors` array) instead of
the status code alone.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "api.js"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def _new_api_error(ctx, envelope: dict) -> dict:
    ctx.eval(f"var __envelope = {json.dumps(envelope)};")
    ctx.eval("var __err = new window.primerApi.ApiError(__envelope);")
    return json.loads(ctx.eval(
        "JSON.stringify({title: __err.title, detail: __err.detail, "
        "fieldErrors: __err.fieldErrors})"
    ))


def test_registered_on_primer_api() -> None:
    src = MODULE.read_text(encoding="utf-8")
    assert "ns.ApiError = ApiError" in src
    assert 'src="foundation/api.js"' in (
        (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    )


def test_domain_422_surfaces_its_own_specific_detail() -> None:
    # primer/api/errors.py's _make_primer_error_handler shape: no
    # `extensions.errors` field list, just a specific `detail` message.
    envelope = {
        "status": 422,
        "type": "/errors/validation-error",
        "title": "Validation Error",
        "detail": "seq 1 is the newest visible record; nothing to discard",
        "extensions": {"request_id": "req-abc123"},
    }
    out = _new_api_error(_ctx(), envelope)
    assert out["detail"] == "seq 1 is the newest visible record; nothing to discard"
    assert out["title"] == "Validation Error"


def test_domain_422_with_no_extensions_at_all_also_surfaces_detail() -> None:
    # Some domain 422s carry no extensions object at all (envelope.extensions
    # is undefined, not even an empty dict) - must not crash and must still
    # prefer the real detail.
    envelope = {
        "status": 422,
        "type": "/errors/conflict",
        "title": "Validation Error",
        "detail": "workspace is archived; cannot create a session",
    }
    out = _new_api_error(_ctx(), envelope)
    assert out["detail"] == "workspace is archived; cannot create a session"


def test_pydantic_422_still_gets_the_friendly_field_summary() -> None:
    # FastAPI's own RequestValidationError shape: extensions.errors is a
    # non-empty field-error list. This behavior must survive unchanged -
    # the fix narrows the OLD status-only branch to this shape, it does
    # not remove the friendliness for genuine field-validation failures.
    envelope = {
        "status": 422,
        "type": "/errors/validation-error",
        "title": "Validation Error",
        "detail": "field required",
        "extensions": {
            "errors": [
                {"loc": ["body", "name"], "msg": "field required"},
                {"loc": ["body", "email"], "msg": "field required"},
            ],
        },
    }
    out = _new_api_error(_ctx(), envelope)
    assert out["title"] == "Data is incomplete"
    assert out["detail"] == "Missing or invalid: name, email."


def test_pydantic_422_with_empty_errors_array_falls_back_to_envelope_detail() -> None:
    # extensions.errors present but EMPTY is the same "no field list"
    # shape as absent - must not render the friendly-fallback string
    # ("Some required fields are missing or invalid.") over a real detail.
    envelope = {
        "status": 422,
        "type": "/errors/validation-error",
        "title": "Validation Error",
        "detail": "a specific real reason",
        "extensions": {"errors": []},
    }
    out = _new_api_error(_ctx(), envelope)
    assert out["detail"] == "a specific real reason"


def test_non_422_status_is_unaffected() -> None:
    envelope = {
        "status": 409,
        "type": "/errors/conflict",
        "title": "Conflict",
        "detail": "session is not idle; rewind requires no turn in flight",
    }
    out = _new_api_error(_ctx(), envelope)
    assert out["title"] == "Conflict"
    assert out["detail"] == "session is not idle; rewind requires no turn in flight"
