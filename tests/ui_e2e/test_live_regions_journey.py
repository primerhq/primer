"""Journey: the console's live regions announce what is news and nothing else (console review 2026-10-08, C-029, review of PR 517).

Three things were wrong with the first version of the live regions:

* the session's status strip had ``role="status"`` on the WHOLE strip, whose text ends in an elapsed time that ticks every second; a status
  region is atomic, so a screen reader re-read it about once a second for the whole turn. Only the words ("running: thinking") are the live
  region now, and the clock sits beside it;
* ``role="alert"`` sat on every failed-turn row and on every pending approval card, including ones that were already in the history when
  the session was opened: a session with three past failures fired three assertive announcements on open. An alert is now only for a
  failure or an approval that arrives while the document is open, and never for a card on a session that is over;
* the error toast's ``role="alert"`` was nested inside the polite ``role="status"`` stack, which some screen readers announce twice. Errors
  now have their own assertive region beside the polite one.

The first group mounts the REAL components into the console page and observes the DOM a screen reader would; the second drives the real
session document with the history and the pending approvals answered by the test, so a record that is "already there" and one that "arrives"
are told apart by what the server said first and what it said next.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests.ui_e2e._studio_helpers import open_session_in_studio

@pytest.mark.ui_e2e
def test_the_status_strips_live_region_holds_the_words_only_so_the_clock_does_not_re_announce_it(page: Page) -> None:
    page.evaluate(
        """() => {
          const host = document.createElement('div');
          host.id = 'strip-host';
          document.body.appendChild(host);
          window.__stripRoot = ReactDOM.createRoot(host);
          window.__stripRoot.render(React.createElement(NV_StatusStrip, {
            shown: { verb: 'thinking', object: '', startedMs: Date.now() }, canStop: true, stopping: false, onInterrupt() {},
          }));
        }"""
    )
    strip = page.locator("#strip-host").get_by_test_id("nv-status-strip")
    expect(strip).to_be_visible(timeout=10_000)
    assert strip.get_attribute("role") is None, "the whole strip must not be a live region: its text carries the ticking clock"
    live = strip.get_by_test_id("nv-status-live")
    expect(live).to_have_attribute("role", "status")
    expect(live).to_have_text("running: thinking")

    # Watch the live region (children and text) while the clock ticks.
    page.evaluate(
        """() => {
          window.__liveMutations = 0;
          const live = document.querySelector('#strip-host [data-testid=nv-status-live]');
          new MutationObserver((records) => { window.__liveMutations += records.length; })
            .observe(live, { childList: true, characterData: true, subtree: true });
        }"""
    )
    clock = strip.get_by_test_id("nv-status-clock")
    expect(clock).to_be_visible()
    seen = {clock.inner_text()}
    page.wait_for_timeout(3_200)
    seen.add(clock.inner_text())
    assert len(seen) == 2, f"the clock did not tick during the observation: {seen}"
    assert page.evaluate("window.__liveMutations") == 0, "the live region changed while only the clock ticked"
    assert live.inner_text() == "running: thinking"

    # A real change of state IS news: the live region changes, and the clock is gone from a stopping strip.
    page.evaluate(
        """() => {
          window.__stripRoot.render(React.createElement(NV_StatusStrip, {
            shown: { verb: 'thinking', object: '', startedMs: Date.now() }, canStop: true, stopping: true, onInterrupt() {},
          }));
        }"""
    )
    expect(live).to_have_text("stopping", timeout=5_000)
    assert page.evaluate("window.__liveMutations") > 0, "a change of state did not change the live region"
    expect(strip.get_by_test_id("nv-status-clock")).to_have_count(0)


_CARD_CTX = {"username": "ana", "role": "admin", "wid": "w1"}


def _mount_card(page: Page, name: str, **props) -> None:
    props = {
        "item": {
            "id": f"pending:{name}", "toolCallId": name, "gatedTool": "bash", "kind": "approval", "preview": "ls -la",
            "approvers": None, "toolName": "_approval", "resolved": False,
        },
        "onResolved": None,
        **props,
    }
    page.evaluate(
        """([name, props, ctx]) => {
          const host = document.createElement('div');
          host.setAttribute('data-card-host', name);
          document.body.appendChild(host);
          props.onResolved = () => {};
          ReactDOM.createRoot(host).render(
            React.createElement(NV_ConsoleContext.Provider, { value: ctx }, React.createElement(NV_DecisionCard, props)));
        }""",
        [name, props, _CARD_CTX],
    )


@pytest.mark.ui_e2e
def test_an_approval_card_is_an_alert_only_when_it_arrived_live_on_a_session_that_is_still_going(page: Page) -> None:
    _mount_card(page, "live-card", live=True, ended=False)
    _mount_card(page, "history-card", live=False, ended=False)
    _mount_card(page, "ended-card", live=True, ended=True)
    _mount_card(page, "bare-card")

    for name in ("live-card", "history-card", "ended-card", "bare-card"):
        expect(page.get_by_test_id(f"nv-decision:{name}")).to_be_visible(timeout=10_000)
    assert page.get_by_test_id("nv-decision:live-card").get_attribute("role") == "alert"
    for quiet in ("history-card", "ended-card", "bare-card"):
        assert page.get_by_test_id(f"nv-decision:{quiet}").get_attribute("role") is None, f"{quiet} must not be an alert"


@pytest.mark.ui_e2e
def test_error_toasts_have_their_own_assertive_region_and_nothing_is_nested(page: Page) -> None:
    expect(page.get_by_test_id("nv-toasts")).to_be_attached(timeout=20_000)       # the host takes over toastPush when it mounts
    page.evaluate("() => window.primerApi.toastPush({ kind: 'success', title: 'Saved the thing' })")
    page.evaluate("() => window.primerApi.toastPush({ kind: 'error', title: 'Could not save the thing' })")
    status = page.get_by_test_id("nv-toasts-status")
    alert = page.get_by_test_id("nv-toasts-alert")
    expect(status).to_contain_text("Saved the thing")
    expect(alert).to_contain_text("Could not save the thing")

    assert status.get_attribute("role") == "status" and status.get_attribute("aria-live") == "polite"
    assert alert.get_attribute("role") == "alert"
    assert page.get_by_test_id("nv-toasts").get_attribute("role") is None, "the stack itself is not a live region"
    assert status.locator("[role=alert]").count() == 0, "an alert nested in a status region is announced twice"
    assert alert.locator("[role=status]").count() == 0
    assert "Could not save" not in status.inner_text() and "Saved the thing" not in alert.inner_text()


# --- the real session document: what is already there is history, what arrives is news -----------------------------------------------


def _seed_session(base_url: str, mock_base_url: str, tmp_path: Path) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    ids = {"llm": f"lr-llm-{suffix}", "wp": f"lr-wp-{suffix}", "tpl": f"lr-tpl-{suffix}", "agent": f"lr-ag-{suffix}"}
    model_name = f"scripted:lr-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"], "provider": "openchat", "models": [{"name": model_name, "context_length": 131_072}],
            "config": {"url": mock_base_url, "flavor": "other"}, "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, r.text
        assert c.post("/v1/workspace_providers", json={
            "id": ids["wp"], "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path)},
        }).status_code == 201
        assert c.post("/v1/workspace_templates", json={
            "id": ids["tpl"], "description": "live regions journey", "provider_id": ids["wp"], "backend": {"kind": "local"},
        }).status_code == 201
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, r.text
        wid = r.json()["id"]
        assert c.post("/v1/agents", json={
            "id": ids["agent"], "description": "live regions journey agent",
            "model": agent_model(ids["llm"], model_name), "tools": [],
        }).status_code == 201
        r = c.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "agent", "agent_id": ids["agent"]}, "initial_instructions": "hello", "auto_start": False,
        })
        assert r.status_code == 201, r.text
        return wid, r.json()["id"]


def _failed_turn(seq: int) -> dict:
    return {
        "seq": seq, "kind": "error", "node_id": None, "created_at": "2026-10-08T09:00:00Z",
        "payload": {"message": f"model call {seq} blew up", "code": "server_error", "fatal": True},
    }


def _approval_row(sid: str, call_id: str) -> dict:
    return {
        "tool_call_id": call_id, "session_id": sid, "tool_name": "_approval", "parked_at": "2026-10-08T09:00:00Z",
        "resume_metadata": {"original_call": {"id": call_id, "name": "bash", "arguments": {"command": "ls"}}, "tool_call_id": call_id},
    }


@pytest.mark.ui_e2e
@pytest.mark.timeout(150)
def test_failures_and_approvals_already_in_the_history_are_quiet_and_ones_that_arrive_are_alerts(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    _, mock_base_url = mock_llm_lan
    wid, sid = _seed_session(base_url, mock_base_url, tmp_path)
    state = {"extra_error": False, "extra_gate": False}

    def messages(route) -> None:
        items = [_failed_turn(1), _failed_turn(2), _failed_turn(3)] + ([_failed_turn(4)] if state["extra_error"] else [])
        body = {"items": items, "total": len(items), "offset": 0, "limit": 200}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def gates(route) -> None:
        rows = [_approval_row(sid, "tc-history")] + ([_approval_row(sid, "tc-arrived")] if state["extra_gate"] else [])
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": rows}))

    page.route(f"**/v1/sessions/{sid}/messages*", messages)
    page.route(f"**/v1/workspaces/{wid}/sessions/{sid}/yields/pending*", gates)
    open_session_in_studio(page, console_url, wid, sid)

    doc = page.get_by_test_id(f"nv-session-doc:{sid}")
    expect(doc).to_be_visible(timeout=20_000)
    failed = doc.locator(".nv-turn-error")
    expect(failed).to_have_count(3, timeout=20_000)
    expect(doc.get_by_test_id("nv-decision:tc-history")).to_be_visible(timeout=20_000)

    # What the first load brought is history: nothing in the document is an assertive announcement.
    assert doc.locator("[role=alert]").count() == 0, "opening a session announced its past failures and approvals"
    for i in range(3):
        assert failed.nth(i).get_attribute("role") is None

    # A failure and an approval that arrive while it is open are news.
    state["extra_error"] = True
    state["extra_gate"] = True
    expect(failed).to_have_count(4, timeout=20_000)
    expect(doc.get_by_test_id("nv-decision:tc-arrived")).to_be_visible(timeout=20_000)
    assert doc.get_by_test_id("nv-turn:4").get_attribute("role") == "alert", "the new failure was not announced"
    assert doc.get_by_test_id("nv-decision:tc-arrived").get_attribute("role") == "alert", "the new approval was not announced"
    for quiet in ("nv-turn:1", "nv-turn:2", "nv-turn:3", "nv-decision:tc-history"):
        assert doc.get_by_test_id(quiet).get_attribute("role") is None, f"{quiet} (history) became an alert"
