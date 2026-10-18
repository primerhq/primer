"""The agent page's Chat button opens a session through a route that exists (platform revamp plan 05, Task 4; lead notes, item 5).

The button used to POST ``/v1/sessions``, which has no POST route: the only create route is ``POST /v1/workspaces/{wid}/sessions`` (found by Plan 01's
slice S6, PR #760). It answered with an error toast and never opened anything. A session lives in a workspace, so Chat now creates it in the DEFAULT workspace
(the row ``GET /v1/workspaces`` flags ``is_default``; pivot P2), falls back to the first workspace while no row is flagged, and then asks the router to open the
session in the studio, naming its workspace (``navigate("/sessions/<id>", { workspace_id })``, which Plan 01's router shim turns into the studio tab).

``AgentDetail`` (``ui/components/agents.jsx``) runs in V8 on the harness of ``tests/ui/_agent_form_harness.py``, with its real ``useMutation``.
"""

from __future__ import annotations

import pytest

from tests.ui._agent_form_harness import FULL, agent_form, calls, click, mount_detail, navigations, prop, recorded, set_api, text_of

LIST = "/workspaces?limit=200"
OPENED = {"id": "sess-9", "workspace_id": "prime", "status": "created"}


@pytest.fixture
def ctx():
    with agent_form() as c:
        yield c


def test_chat_creates_the_session_in_the_default_workspace_and_opens_it_in_the_studio(ctx):
    set_api(ctx, "GET", LIST, {"items": [{"id": "ws-1", "is_default": False}, {"id": "prime", "is_default": True}, {"id": "ws-3"}], "total": 3})
    set_api(ctx, "POST", "/workspaces/prime/sessions", OPENED)
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    made = calls(ctx)
    assert [(c["method"], c["path"]) for c in made] == [("GET", LIST), ("POST", "/workspaces/prime/sessions")]
    assert made[1]["body"] == {"binding": {"kind": "agent", "agent_id": "refund-triage"}, "auto_start": False}
    assert navigations(ctx) == [{"path": "/sessions/sess-9", "opts": {"workspace_id": "prime"}}]
    assert recorded(ctx)["toasts"] == []


def test_chat_never_posts_the_session_route_that_does_not_exist(ctx):
    set_api(ctx, "GET", LIST, {"items": [{"id": "prime", "is_default": True}], "total": 1})
    set_api(ctx, "POST", "/workspaces/prime/sessions", OPENED)
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert "/sessions" not in [c["path"] for c in calls(ctx)]


def test_chat_falls_back_to_the_first_workspace_while_none_is_flagged_as_the_default(ctx):
    set_api(ctx, "GET", LIST, {"items": [{"id": "ws-1"}, {"id": "ws-2"}], "total": 2})
    set_api(ctx, "POST", "/workspaces/ws-1/sessions", {"id": "sess-1"})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [c["path"] for c in calls(ctx)] == [LIST, "/workspaces/ws-1/sessions"]
    assert navigations(ctx) == [{"path": "/sessions/sess-1", "opts": {"workspace_id": "ws-1"}}]


def test_a_workspace_id_is_escaped_in_the_path(ctx):
    set_api(ctx, "GET", LIST, {"items": [{"id": "my ws/1", "is_default": True}], "total": 1})
    set_api(ctx, "POST", "/workspaces/my%20ws%2F1/sessions", {"id": "sess-1"})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [c["path"] for c in calls(ctx)] == [LIST, "/workspaces/my%20ws%2F1/sessions"]
    assert navigations(ctx)[0]["opts"] == {"workspace_id": "my ws/1"}


def test_the_session_row_may_name_its_id_session_id(ctx):
    set_api(ctx, "GET", LIST, {"items": [{"id": "prime", "is_default": True}], "total": 1})
    set_api(ctx, "POST", "/workspaces/prime/sessions", {"session_id": "sess-7"})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert navigations(ctx) == [{"path": "/sessions/sess-7", "opts": {"workspace_id": "prime"}}]


def test_with_no_workspace_chat_says_so_and_creates_nothing(ctx):
    set_api(ctx, "GET", LIST, {"items": [], "total": 0})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [(c["method"], c["path"]) for c in calls(ctx)] == [("GET", LIST)]
    assert navigations(ctx) == []
    [toast] = recorded(ctx)["toasts"]
    assert toast["kind"] == "error" and toast["title"] == "No workspace to chat in"
    assert "Create a workspace" in toast["detail"]


def test_a_refused_session_is_the_error_toast_and_opens_nothing(ctx):
    set_api(ctx, "GET", LIST, {"items": [{"id": "prime", "is_default": True}], "total": 1})
    set_api(ctx, "POST", "/workspaces/prime/sessions", {"__reject": {"status": 422, "title": "Could not start", "detail": "The agent has a missing model profile."}})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert navigations(ctx) == []
    [toast] = recorded(ctx)["toasts"]
    assert (toast["kind"], toast["title"], toast["detail"]) == ("error", "Could not start", "The agent has a missing model profile.")


def test_a_workspace_list_that_fails_is_the_error_toast_and_creates_nothing(ctx):
    set_api(ctx, "GET", LIST, {"__reject": {"status": 500, "title": "Internal Server Error", "detail": "boom"}})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [c["method"] for c in calls(ctx)] == ["GET"]
    assert recorded(ctx)["toasts"][0]["detail"] == "boom"


def test_chat_is_busy_while_the_session_is_made_and_a_second_press_does_nothing(ctx):
    set_api(ctx, "GET", LIST, {"__pending": True})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert prop(ctx, "agent-form-chat", "data-busy") == "true" and text_of(ctx, "agent-form-chat") == "Opening chat…"
    click(ctx, "agent-form-chat")
    assert len(calls(ctx)) == 1
