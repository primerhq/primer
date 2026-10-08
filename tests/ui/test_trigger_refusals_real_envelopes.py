"""A refused trigger write reads the REAL envelopes of the auth gate and of the trigger router (the #572 review).

``TR_refusal`` first read only ``extensions.code`` and ``detail.code``. The auth gate answers differently: ``require_user`` raises
``HTTPException(401, detail={"error": "auth_required"})`` and ``(403, detail={"error": "forbidden_role"})`` with no message, so the problem's
``detail`` is the code itself (``_detail_from_mapping`` falls back to it) and the code sits in ``extensions.error``, not ``extensions.code``. The reader found no
code and fell back to ``err.detail``, so a session that had expired, a password that was reset or a user signed out everywhere got
``Fire failed / auth_required`` on Fire now, where main showed the HTTP title.

These tests do not hand-build the envelopes: a FastAPI app with the real ``register_error_handlers`` and the real ``require_user`` / ``_raise_code``
answers them through ``TestClient``, and the browser side is the real ``ui/foundation/api.js`` ``ApiError`` and the real ``triggers.jsx`` functions in MiniRacer.
Each dialog calls ``TR_refusalText`` with its own fallback; the fallbacks are read from the source, so a new call site is covered too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _envelopes() -> dict[str, dict]:
    """The bodies a real app answers with, through the real error handlers."""
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


@pytest.fixture(scope="module")
def envelopes() -> dict[str, dict]:
    return _envelopes()


def _text(envelope: dict, fallback: str) -> str:
    from py_mini_racer import MiniRacer

    start = SRC.index("function TR_writeErrorText(")
    end = SRC.index("\n}\n", SRC.index("function TR_refusalText(", start)) + 3
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval("var window = {};")
    ctx.eval(API)
    ctx.eval(SRC[start:end])
    return json.loads(ctx.eval(f"JSON.stringify(TR_refusalText(new window.primerApi.ApiError({json.dumps(envelope)}), {json.dumps(fallback)}))"))


# What the dialogs pass as the fallback: every `TR_refusalText(err, "<fallback>")` and `TR_writeErrorText(err, "<fallback>")` in the source.
FALLBACKS = sorted(set(re.findall(r'TR_(?:refusalText|writeErrorText)\(err, "([^"]+)"\)', SRC)))


def test_the_dialogs_pass_the_fallbacks_this_file_covers() -> None:
    """Guards the parametrisation: the create wizard, the edit dialog, Fire now, the subscription dialog, Rotate token, Clear HMAC and the HMAC dialog."""
    assert {"Request failed", "Fire failed", "Rotate failed", "Could not clear the HMAC secret", "Save failed"} <= set(FALLBACKS), FALLBACKS


# ---- the real envelopes really look like this ----------------------------------------------------------------------------------------------------------------


def test_the_auth_gate_puts_the_code_in_extensions_error_and_sends_no_message(envelopes) -> None:
    """The premise of the fix: if the server's shape changed, the cases below would pass for nothing."""
    ended, refused = envelopes["session_ended"], envelopes["role_refused"]

    assert ended["status"] == 401 and ended["detail"] == "auth_required" and ended["extensions"]["error"] == "auth_required" and "code" not in ended["extensions"]
    assert refused["status"] == 403 and refused["detail"] == "forbidden_role" and refused["extensions"]["error"] == "forbidden_role" and "code" not in refused["extensions"]


def test_the_trigger_router_puts_the_code_in_extensions_code_with_its_message(envelopes) -> None:
    assert envelopes["slug"]["extensions"]["code"] == "trigger_slug_conflict" and envelopes["slug"]["detail"] == "slug 'nightly' already in use"


# ---- the auth gate: a sentence, never the code --------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fallback", FALLBACKS)
def test_a_session_that_has_ended_says_so_in_every_dialog(envelopes, fallback: str) -> None:
    text = _text(envelopes["session_ended"], fallback)

    assert text == "Your session has ended; sign in again."
    assert "auth_required" not in text


@pytest.mark.parametrize("fallback", FALLBACKS)
def test_a_role_that_may_not_write_says_so_in_every_dialog(envelopes, fallback: str) -> None:
    text = _text(envelopes["role_refused"], fallback)

    assert text == "Your role does not allow this."
    assert "forbidden_role" not in text


# ---- a message that is only the code is not a sentence ------------------------------------------------------------------------------------------------------


def test_a_message_equal_to_the_code_falls_through_to_the_title(envelopes) -> None:
    env = {**envelopes["slug"], "detail": "something_unheard_of", "title": "Conflict", "extensions": {"code": "something_unheard_of", "request_id": "r"}}

    assert _text(env, "Request failed") == "Conflict"


def test_a_detail_string_that_is_only_a_snake_case_code_is_not_shown_when_there_is_no_code_at_all() -> None:
    env = {"type": "/errors/bad-request", "title": "Bad Request", "status": 400, "detail": "payload_malformed", "extensions": {"request_id": "r"}}

    assert _text(env, "Request failed") == "Bad Request"


def test_an_id_with_an_underscore_in_a_not_found_message_is_not_mistaken_for_a_code(envelopes) -> None:
    """The not-found message is the bare id, and a trigger id may be user-chosen: only a message equal to the CODE is discarded when a code is known."""
    assert _text(envelopes["not_found_underscored_id"], "Fire failed") == (
        "Trigger nightly_job was not found: it may have been deleted. Go back to the triggers list."
    )


def test_a_not_found_with_no_message_is_the_http_title_not_a_sentence_about_nothing(envelopes) -> None:
    assert _text(envelopes["not_found_empty"], "Fire failed") == "Not Found"


# ---- what the trigger router says is unchanged ----------------------------------------------------------------------------------------------------------------


def test_the_trigger_routers_own_refusals_still_get_their_remedy(envelopes) -> None:
    assert _text(envelopes["slug"], "Request failed") == "slug 'nightly' already in use. Choose a different slug."
    assert _text(envelopes["not_found"], "Fire failed") == "Trigger tr-1 was not found: it may have been deleted. Go back to the triggers list."
    assert _text(envelopes["router_forbidden"], "Rotate failed") == (
        "only the trigger's owner or an admin may rotate its webhook token. Ask the trigger's owner or an administrator."
    )
