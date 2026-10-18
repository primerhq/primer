"""The agent page's Chat button opens a session through a route that exists (platform revamp plan 05, Task 4; lead notes, item 5).

The button used to POST ``/v1/sessions``, which has no POST route: the only create route is ``POST /v1/workspaces/{wid}/sessions`` (found by Plan 01's
slice S6, PR #760). It answered with an error toast and never opened anything. A session lives in a workspace, so Chat now creates it in the DEFAULT workspace
(the ONE row ``GET /v1/workspaces`` flags ``is_default``; pivot P2) and then asks the router to open the session in the studio, naming its workspace
(``navigate("/sessions/<id>", { workspace_id })``, which Plan 01's router shim turns into the studio tab). There is no fallback (lead ruling, review of #779):
with no row flagged on any page of the list, Chat shows the error toast "No default workspace found" and sends nothing else, and it never picks the first
workspace. The list is paged (``limit=200``, then ``offset``) until the flagged row is found or the list ends, so a default beyond the first page is found.

``AgentDetail`` (``ui/components/agents.jsx``) runs in V8 on the harness of ``tests/ui/_agent_form_harness.py``, with its real ``useMutation``.
"""

from __future__ import annotations

import pytest

from tests.ui._agent_form_harness import FULL, agent_form, calls, click, mount_detail, navigations, prop, recorded, set_api, text_of

LIST = "/workspaces?limit=200"
PAGE_2 = "/workspaces?limit=200&offset=200"
PAGE_3 = "/workspaces?limit=200&offset=400"
OPENED = {"id": "sess-9", "workspace_id": "prime", "status": "created"}
NO_DEFAULT = "No default workspace found"


def page(first: int, count: int, *, total: int | None, flagged: str | None = None) -> dict:
    """One page of the workspace list: ``count`` rows ``ws-<first>`` ..., none flagged unless ``flagged`` names one; ``total`` None leaves the count out."""
    items = [{"id": f"ws-{n}", "is_default": f"ws-{n}" == flagged} for n in range(first, first + count)]
    return {"items": items, **({} if total is None else {"total": total})}


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


def test_with_no_workspace_flagged_as_the_default_chat_says_so_and_creates_nothing(ctx):  # lead ruling: no first-workspace fallback
    set_api(ctx, "GET", LIST, {"items": [{"id": "ws-1"}, {"id": "ws-2", "is_default": False}], "total": 2})
    set_api(ctx, "POST", "/workspaces/ws-1/sessions", {"id": "sess-1"})  # answered, so a pick of the first workspace would show as a POST
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [(c["method"], c["path"]) for c in calls(ctx)] == [("GET", LIST)]  # one GET, no POST
    assert navigations(ctx) == []
    [toast] = recorded(ctx)["toasts"]
    assert toast["kind"] == "error" and toast["title"] == NO_DEFAULT
    assert "none of the workspaces" in toast["detail"] and "Nothing was created" in toast["detail"]


@pytest.mark.parametrize("flag", ["true", 1, "yes", None])
def test_only_a_boolean_true_flags_the_default(ctx, flag):
    """``is_default`` is a response-only boolean: a truthy string or number is not the flag."""
    set_api(ctx, "GET", LIST, {"items": [{"id": "ws-1", "is_default": flag}], "total": 1})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [c["method"] for c in calls(ctx)] == ["GET"] and navigations(ctx) == []
    assert recorded(ctx)["toasts"][0]["title"] == NO_DEFAULT


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


def test_with_no_workspace_at_all_chat_says_so_and_creates_nothing(ctx):
    set_api(ctx, "GET", LIST, {"items": [], "total": 0})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [(c["method"], c["path"]) for c in calls(ctx)] == [("GET", LIST)]  # one GET, no POST
    assert navigations(ctx) == []
    [toast] = recorded(ctx)["toasts"]
    assert toast["kind"] == "error" and toast["title"] == NO_DEFAULT
    assert "none of the workspaces" in toast["detail"]


# ---- the list is paged: the flagged row may be beyond the first page (as the test helpers of #770 read it) ------------------------------------------------


def test_a_default_workspace_beyond_the_first_page_is_found(ctx):
    set_api(ctx, "GET", LIST, page(0, 200, total=450))
    set_api(ctx, "GET", PAGE_2, page(200, 200, total=450))
    set_api(ctx, "GET", PAGE_3, page(400, 50, total=450, flagged="ws-430"))
    set_api(ctx, "POST", "/workspaces/ws-430/sessions", {"id": "sess-1"})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [(c["method"], c["path"]) for c in calls(ctx)] == [("GET", LIST), ("GET", PAGE_2), ("GET", PAGE_3), ("POST", "/workspaces/ws-430/sessions")]
    assert navigations(ctx) == [{"path": "/sessions/sess-1", "opts": {"workspace_id": "ws-430"}}]
    assert recorded(ctx)["toasts"] == []


def test_the_pages_are_read_only_until_the_flagged_row_is_found(ctx):
    set_api(ctx, "GET", LIST, page(0, 200, total=450, flagged="ws-3"))
    set_api(ctx, "POST", "/workspaces/ws-3/sessions", {"id": "sess-1"})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [c["path"] for c in calls(ctx)] == [LIST, "/workspaces/ws-3/sessions"]


def test_with_no_flag_on_any_page_every_page_is_read_and_nothing_is_created(ctx):
    set_api(ctx, "GET", LIST, page(0, 200, total=250))
    set_api(ctx, "GET", PAGE_2, page(200, 50, total=250))
    set_api(ctx, "POST", "/workspaces/ws-0/sessions", {"id": "sess-1"})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [(c["method"], c["path"]) for c in calls(ctx)] == [("GET", LIST), ("GET", PAGE_2)]
    assert navigations(ctx) == []
    [toast] = recorded(ctx)["toasts"]
    assert (toast["kind"], toast["title"]) == ("error", NO_DEFAULT)


def test_a_last_page_that_is_exactly_full_ends_at_the_total(ctx):
    """The offset reaching ``total`` ends the list: no empty extra page is asked for."""
    set_api(ctx, "GET", LIST, page(0, 200, total=200))
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [c["path"] for c in calls(ctx)] == [LIST]
    assert recorded(ctx)["toasts"][0]["title"] == NO_DEFAULT


def test_a_list_without_a_total_goes_on_while_pages_are_full_and_finds_the_flag(ctx):
    set_api(ctx, "GET", LIST, page(0, 200, total=None))
    set_api(ctx, "GET", PAGE_2, page(200, 3, total=None, flagged="ws-201"))
    set_api(ctx, "POST", "/workspaces/ws-201/sessions", {"id": "sess-1"})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [c["path"] for c in calls(ctx)] == [LIST, PAGE_2, "/workspaces/ws-201/sessions"]


def test_a_list_without_a_total_ends_at_the_first_short_page(ctx):
    set_api(ctx, "GET", LIST, page(0, 200, total=None))
    set_api(ctx, "GET", PAGE_2, page(200, 3, total=None))
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [c["path"] for c in calls(ctx)] == [LIST, PAGE_2]  # the short page 2 is the end: no page 3 is asked for
    assert recorded(ctx)["toasts"][0]["title"] == NO_DEFAULT


def test_a_page_that_fails_is_the_error_toast_and_creates_nothing(ctx):
    set_api(ctx, "GET", LIST, page(0, 200, total=300))
    set_api(ctx, "GET", PAGE_2, {"__reject": {"status": 500, "title": "Internal Server Error", "detail": "boom"}})
    mount_detail(ctx, FULL)
    click(ctx, "agent-form-chat")
    assert [(c["method"], c["path"]) for c in calls(ctx)] == [("GET", LIST), ("GET", PAGE_2)]
    assert navigations(ctx) == []
    assert recorded(ctx)["toasts"][0]["detail"] == "boom"


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
