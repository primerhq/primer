"""Journey: the phone's Inbox describes what it asks you to decide, and its cards fit the screen (console review C-032, C-033).

390x844, a real gated tool call and a real question parked through the scripted mock LLM and the real worker. Before: the approval card
showed the session's name and "approval . primer" next to an Approve button that approved whatever the session was parked on when it
was tapped, with no toast; the question card drew its border about 215px wide inside a 358px card with the Review button over the
title; and there was no heading.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.mock_llm import Rule
from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests.ui_e2e._shell_helpers import open_mobile_shell

PHONE = {"width": 390, "height": 844}
WRITE = "workspaces__write_workspace_file"


def _seed(base_url: str, mock_base_url: str, suffix: str, tmp_path: Path) -> dict:
    ids = {"llm": f"mi-llm-{suffix}", "wp": f"mi-wp-{suffix}", "tpl": f"mi-tpl-{suffix}", "agent": f"mi-ag-{suffix}"}
    model_name = f"scripted:mi-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"], "provider": "openchat", "models": [{"name": model_name, "context_length": 131_072}],
            "config": {"url": mock_base_url, "flavor": "other"}, "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm failed: {r.status_code} {r.text}"
        r = c.post("/v1/workspace_providers", json={"id": ids["wp"], "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path)}})
        assert r.status_code == 201, f"seed wp failed: {r.status_code} {r.text}"
        r = c.post("/v1/workspace_templates", json={"id": ids["tpl"], "description": "mobile inbox journey", "provider_id": ids["wp"], "backend": {"kind": "local"}})
        assert r.status_code == 201, f"seed tpl failed: {r.status_code} {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed workspace failed: {r.status_code} {r.text}"
        ids["workspace"] = r.json()["id"]
        r = c.post("/v1/agents", json={
            "id": ids["agent"], "description": "mobile inbox journey agent",
            "model": agent_model(ids["llm"], model_name), "tools": ["system__ask_user", WRITE],
        })
        assert r.status_code == 201, f"seed agent failed: {r.status_code} {r.text}"
    ids["model_name"] = model_name
    return ids


def _gate_writes(client: httpx.Client, policy_id: str) -> None:
    """Policies are unique on (toolset, tool): clear a leftover, then require approval for write_workspace_file."""
    for item in client.get("/v1/tool_approval_policies", params={"limit": 200}).json().get("items", []):
        if item.get("toolset_id") == "workspaces" and item.get("tool_name") == "write_workspace_file":
            client.delete(f"/v1/tool_approval_policies/{item['id']}")
    r = client.post("/v1/tool_approval_policies", json={
        "id": policy_id, "toolset_id": "workspaces", "tool_name": "write_workspace_file", "enabled": True, "approval": {"type": "required"},
    })
    assert r.status_code == 201, f"policy failed: {r.status_code} {r.text}"
    assert client.post("/v1/tool_approval_policies/invalidate").status_code == 202


def _start(client: httpx.Client, ids: dict, instructions: str) -> str:
    r = client.post(f"/v1/workspaces/{ids['workspace']}/sessions", json={
        "binding": {"kind": "agent", "agent_id": ids["agent"]}, "initial_instructions": instructions, "auto_start": True,
    })
    assert r.status_code == 201, f"create session failed: {r.status_code} {r.text}"
    return r.json()["id"]


def _attention_row(client: httpx.Client, sid: str, kind: str, timeout_s: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout_s
    seen: list = []
    while time.monotonic() < deadline:
        seen = client.get("/v1/yields/pending").json().get("items", [])
        for row in seen:
            if row["session_id"] == sid and row["kind"] == kind:
                return row
        time.sleep(0.3)
    raise AssertionError(f"session {sid} never showed up as {kind!r} in the Inbox; last rows: {seen}")


def _wait_for_file(root: Path, name: str, timeout_s: float = 45.0) -> Path | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = list(root.rglob(name))
        if found:
            return found[0]
        time.sleep(0.3)
    return None


@pytest.mark.timeout(240)
def test_the_inbox_names_what_it_asks_you_to_decide_and_deciding_is_acknowledged(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    registry, mock_base_url = mock_llm_lan
    suffix = uuid.uuid4().hex[:8]
    ids = _seed(base_url, mock_base_url, suffix, tmp_path)
    wid = ids["workspace"]
    registry.register(ids["model_name"], [
        Rule(when_tool_result=True, emit_text="done"),
        Rule(when_last_user_contains="write the plan", emit_tool=WRITE, emit_args={"workspace_id": wid, "path": "notes/plan.md", "content": "x" * 3000}),
        Rule(when_last_user_contains="deny me", emit_tool=WRITE, emit_args={"workspace_id": wid, "path": "notes/denied.md", "content": "y"}),
        Rule(when_last_user_contains="ask me", emit_tool="system__ask_user", emit_args={"prompt": "Which environment should I deploy to?"}),
        Rule(emit_text="ok"),
    ])
    policy_id = f"mi-pol-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        _gate_writes(client, policy_id)
        try:
            approve_sid = _start(client, ids, "please write the plan")
            deny_sid = _start(client, ids, "please deny me")
            ask_sid = _start(client, ids, "please ask me")
            approve_row = _attention_row(client, approve_sid, "approval")
            _attention_row(client, deny_sid, "approval")
            ask_row = _attention_row(client, ask_sid, "ask")

            # The server describes the call: the tool, the target first, the bulky content counted not shipped.
            assert approve_row["approval"]["tool_name"] == WRITE
            assert approve_row["approval"]["arguments"].startswith("path=notes/plan.md")
            assert "content=<3000 chars>" in approve_row["approval"]["arguments"]
            assert ask_row["prompt"] == "Which environment should I deploy to?"

            page.set_viewport_size(PHONE)
            open_mobile_shell(page, console_url)
            panel = page.get_by_test_id("nv-mobile-panel:inbox")
            expect(panel).to_be_visible(timeout=20_000)
            expect(page.get_by_test_id("nv-mob-ib-count")).to_contain_text("waiting on you", timeout=15_000)

            card = page.get_by_test_id(f"nv-mobile-inbox-card:{approve_sid}")
            expect(card).to_be_visible(timeout=20_000)
            expect(card.get_by_test_id("nv-mob-ib-kind")).to_have_text("Approval")
            expect(card).to_contain_text(WRITE)
            expect(card).to_contain_text("path=notes/plan.md")

            # C-032: the question card fills its width and its button sits below the text, not over it.
            question = page.get_by_test_id(f"nv-mobile-inbox-card:{ask_sid}")
            expect(question).to_be_visible(timeout=20_000)
            expect(question.get_by_test_id("nv-mob-ib-kind")).to_have_text("Question")
            expect(question).to_contain_text("Which environment should I deploy to?")
            panel_box = panel.bounding_box()
            for c in (card, question):
                box = c.bounding_box()
                assert box["width"] >= panel_box["width"] - 40, f"card is {box['width']}px inside a {panel_box['width']}px panel"
            line_box = question.get_by_test_id("nv-mob-ib-line").bounding_box()
            review_box = question.get_by_test_id(f"nv-mobile-inbox-review:{ask_sid}").bounding_box()
            assert line_box["y"] + line_box["height"] <= review_box["y"] + 1, "the button must not sit over the question"

            # C-033: nothing is hidden: "show all" loads the whole call.
            card.get_by_test_id(f"nv-mob-ib-showall:{approve_sid}").click()
            full = page.get_by_test_id(f"nv-mob-ib-full:{approve_sid}")
            expect(full).to_contain_text("notes/plan.md", timeout=10_000)
            assert len(full.inner_text()) > 3000

            # Approve decides that call, says so, and the call actually runs.
            card.get_by_test_id(f"nv-mobile-inbox-approve:{approve_sid}").click()
            expect(page.locator(".toast", has_text="Approved " + WRITE)).to_be_visible(timeout=10_000)
            expect(card).to_have_count(0, timeout=20_000)
            written = _wait_for_file(tmp_path, "plan.md")
            assert written is not None and written.read_text().startswith("xxx"), "the approved call ran"

            # Deny is one tap, says so, and the denied call never runs.
            page.get_by_test_id(f"nv-mobile-inbox-deny:{deny_sid}").click()
            expect(page.locator(".toast", has_text="Denied " + WRITE)).to_be_visible(timeout=10_000)
            expect(page.get_by_test_id(f"nv-mobile-inbox-card:{deny_sid}")).to_have_count(0, timeout=20_000)
            assert _wait_for_file(tmp_path, "denied.md", timeout_s=5.0) is None, "the denied call must not run"
        finally:
            client.delete(f"/v1/tool_approval_policies/{policy_id}")
            client.post("/v1/tool_approval_policies/invalidate")


@pytest.mark.timeout(180)
def test_the_desktop_rail_inbox_row_says_what_it_is_about(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    """The same aggregate row feeds the desktop rail, whose rows used to read only "approval" next to a session name."""
    registry, mock_base_url = mock_llm_lan
    suffix = uuid.uuid4().hex[:8]
    ids = _seed(base_url, mock_base_url, suffix, tmp_path)
    registry.register(ids["model_name"], [
        Rule(when_tool_result=True, emit_text="done"),
        Rule(when_last_user_contains="write the plan", emit_tool=WRITE, emit_args={"workspace_id": ids["workspace"], "path": "notes/plan.md", "content": "x" * 50}),
        Rule(emit_text="ok"),
    ])
    policy_id = f"mi-pol-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        _gate_writes(client, policy_id)
        try:
            sid = _start(client, ids, "please write the plan")
            _attention_row(client, sid, "approval")
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto(console_url)
            line = page.get_by_test_id(f"nv-rail-inbox-line:{sid}")
            expect(line).to_be_visible(timeout=30_000)
            expect(line).to_contain_text(WRITE)
            expect(line).to_contain_text("(path, content)")
            assert "notes/plan.md" not in line.inner_text(), "the passive rail line names the arguments and never shows their values"
        finally:
            client.delete(f"/v1/tool_approval_policies/{policy_id}")
            client.post("/v1/tool_approval_policies/invalidate")
