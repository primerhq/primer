"""S3's dispatcher, mounted where the fresh shell's session view lives.

S3's mount language is shell-agnostic by design (spec section 4), so the
only new thing here is the host: openDoc must land a PREVIEW tab WITHOUT
focus, because a focus-stealing agent open is an explicit antipattern.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-client-tools.jsx"
API = UI / "components" / "shell" / "sh-api.jsx"
SESSION = UI / "components" / "shell" / "sh-session-doc.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_registered_and_mounted_on_the_session_view() -> None:
    assert 'src="components/shell/sh-client-tools.jsx"' in (
        UI / "index.html"
    ).read_text(encoding="utf-8")
    assert "SH_ClientTools" in SESSION.read_text(encoding="utf-8")


def test_it_delegates_every_rule_to_the_s3_module() -> None:
    src = _src()
    assert "CT_createExecutor" in src
    for key in ("openDoc:", "toast:", "attachLifecycle:"):
        assert key in src, key
    # No second vocabulary: the tool names come from S3's constants.
    assert '"open_file"' not in src
    assert '"inform_user"' not in src


def test_an_agent_open_never_steals_focus() -> None:
    src = _src()
    m = re.search(r"openDoc:\s*function[\s\S]*?\n    \},", src)
    assert m, "openDoc host entry"
    body = m.group(0)
    assert "focus: false" in body
    assert "preview: true" in body


def test_delivery_rides_the_shared_tap_not_a_second_socket() -> None:
    src = _src()
    assert "useWorkspaceTapListener" in src
    for banned in ("WebSocket", "EventSource", "new Worker"):
        assert banned not in src, banned


def test_the_heartbeat_stays_inside_the_server_ttl() -> None:
    src = _src()
    assert "SH_CT_HEARTBEAT_MS = 10000" in src, (
        "a third of the server's 30s ATTACH_TTL_SECONDS, so one dropped "
        "beat is survivable"
    )


def test_a_reload_is_a_new_attachment_not_a_resumed_one() -> None:
    src = _src()
    assert "SH_CT_CLIENT_ID" in src
    assert "Math.random()" in src


def test_the_attach_endpoints_live_in_the_api_module() -> None:
    api = API.read_text(encoding="utf-8")
    assert '"/attach"' in api
    assert re.search(r"attach:\s*function\s*\(wid,\s*sid,\s*clientId\)", api)
    assert re.search(r"detach:\s*function\s*\(wid,\s*sid,\s*clientId\)", api)
    assert '"DELETE"' in api


def test_detach_sends_client_id_where_the_route_actually_reads_it() -> None:
    """Regression: every detach 422'd on a missing required parameter.

    ``detach_session`` declares ``client_id: str = Query(...)``. The shell
    sent it as a JSON body, so it never arrived, the call failed 422 every
    time, and a client attachment could only ever lapse by its TTL. The
    two halves are pinned against each other here rather than separately,
    since either one moving alone is the bug.
    """
    import inspect

    from primer.api.routers.workspaces import detach_session

    param = inspect.signature(detach_session).parameters["client_id"]
    assert type(param.default).__name__ == "Query", (
        "the route takes client_id from the query string; if that changed, "
        "the shell's detach call has to change with it"
    )

    api = API.read_text(encoding="utf-8")
    detach_src = api[api.index("detach: function"):]
    detach_src = detach_src[:detach_src.index("},")]
    assert "?client_id=" in detach_src, (
        "detach must put client_id in the query string, not a body"
    )
    assert "{ client_id:" not in detach_src
