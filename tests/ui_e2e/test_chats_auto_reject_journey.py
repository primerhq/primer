"""UI E2E: Chats auto-reject-on-new-message confirmation journey.

Closes the §2 (v) feature directive coverage gap: when an operator
tries to send a message in a chat that has a pending tool_approval,
the UI must show a confirmation banner explaining that proceeding
will auto-reject the pending approval — and offer Cancel + Send &
reject buttons.

Multi-page journey across /approvals and /chats/{cid}:

  1. Seed LLMProvider + Agent + Chat via API.
  2. JSONB-inject `_approval` parked_state onto the chat row.
  3. Navigate to /approvals → the parked chat shows up in the
     Pending tab (chrome.jsx aggregation; chats are part of the
     same pending view as sessions).
  4. Click the chat's row anchor → land on /chats/{cid}.
  5. Sanity: the inline approval card renders.
  6. Type a message in the composer + click Send → composer text
     stashes into pendingSendText state; the auto-reject
     confirmation Banner appears (chats.jsx:594-609).
  7. Assert the Banner carries the documented copy ("Sending a
     new message will auto-reject the pending approval") + the
     tool name ("Tool fs.delete will be marked rejected by the
     server").
  8. Click Cancel → banner closes, composer text is retained
     (operator can retry).
  9. Re-trigger by clicking Send again → banner reappears.
  10. Click "Send & reject" → message sent via the WS frame,
      composer clears, banner closes.

Multi-page (approvals → chat detail) + multi-state (compose →
auto-reject → cancel → compose → confirm). The actual server-side
auto-reject is part of roadmap §2's chat-side behaviour; this test
focuses on the UI affordance — verifying the operator sees and can
act on the confirmation surface.

Covers backlog item U0113. First test on the auto-reject
confirmation surface; complements U0112 (chats inline approval
card) on the action-side flow.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
import pytest
from playwright.sync_api import expect


# ---------------------------------------------------------------------------
# asyncpg park-injection (mirrors U0112's helper).
# ---------------------------------------------------------------------------


async def _inject_chat_approval_park_async(
    *,
    chat_id: str,
    tool_call_id: str,
    inner_tool_name: str,
    policy_id: str,
    gate_reason: str,
) -> None:
    """Stamp parked_status=parked + _approval parked_state onto the chat row."""
    import os
    db = os.environ.get("MATRIX_UI_E2E_DB", "matrix")
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    parked_state: dict[str, Any] = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "_approval",
            "event_key": f"tool_approval:{chat_id}:{tool_call_id}",
            "timeout": 600.0,
            "resume_metadata": {
                "tool_call_id": tool_call_id,
                "original_call": {
                    "id": tool_call_id,
                    "name": inner_tool_name,
                    "arguments": {"path": "/etc/passwd"},
                },
                "policy_id": policy_id,
                "approval_type": "required",
                "gate_reason": gate_reason,
            },
        },
        "llm_messages": [],
        "turn_no": 0,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
    }
    sql = """
        UPDATE chat
        SET data = jsonb_set(
                     jsonb_set(
                       jsonb_set(
                         jsonb_set(
                           jsonb_set(data,
                             '{parked_status}', to_jsonb('parked'::text)),
                           '{parked_event_key}', to_jsonb($2::text)),
                         '{parked_until}', to_jsonb($3::text)),
                       '{parked_at}', to_jsonb($4::text)),
                     '{parked_state}', $5::jsonb
                   ),
            updated_at = now()
        WHERE id = $1
    """
    conn = await asyncpg.connect(
        host="localhost", port=5432,
        user="matrix", password="matrix", database=db,
    )
    try:
        await conn.execute(
            sql,
            chat_id,
            parked_state["yielded"]["event_key"],
            parked_until.isoformat(),
            now.isoformat(),
            json.dumps(parked_state),
        )
    finally:
        await conn.close()


def _inject_chat_approval_park(**kwargs) -> None:
    """Sync wrapper — pytest-asyncio's running loop forces a worker thread."""
    import threading
    box: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    _inject_chat_approval_park_async(**kwargs),
                )
            finally:
                loop.close()
        except BaseException as exc:  # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=15.0)
    if "err" in box:
        raise box["err"]
    if t.is_alive():
        raise RuntimeError("asyncpg injection thread did not finish in 15s")


def _seed_chat_ladder(
    base_url: str, suffix: str,
) -> tuple[str, list[str]]:
    """Seed LLMProvider + Agent + Chat. Returns (chat_id, cleanup_urls)."""
    pid = f"u113-llm-{suffix}"
    aid = f"u113-ag-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": pid,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, r.text
        r = c.post("/v1/agents", json={
            "id": aid,
            "description": "U0113 auto-reject probe agent",
            "model": {"provider_id": pid, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        })
        assert r.status_code == 201, r.text
        r = c.post("/v1/chats", json={"agent_id": aid})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
    cleanup_urls = [
        f"/v1/chats/{cid}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    return cid, cleanup_urls


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0113 — Auto-reject confirmation journey across /approvals + /chats/{cid}
# ===========================================================================


def test_u0113_chats_auto_reject_confirmation_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0113 — Multi-page operator-journey across /approvals and
    /chats/{cid} pinning the auto-reject-on-new-message confirmation
    surface (§2 v).

    Steps:

      1. Seed LLMProvider + Agent + Chat via API.
      2. asyncpg-inject _approval park onto the chat row.
      3. /approvals (pending tab default) → row visible for the
         seeded chat. (Cross-page consistency sanity — the same
         data feeds /chats/{cid}'s inline card.)
      4. Navigate /chats/{cid} (deep-link, since the row click
         navigates via the chat-detail route).
      5. Inline approval card renders (sanity).
      6. Type composer text + click Send → auto-reject Banner
         appears (chats.jsx:594-609).
      7. Banner copy includes the warning + tool name.
      8. Click Cancel → banner closes, composer text retained.
      9. Re-trigger by clicking Send → banner reappears.
      10. Click "Send & reject" → composer clears (chats.jsx:534
          sets composer to ""), banner closes.

    Pinned invariants:
      * Cross-page aggregation: the parked chat appears on the
        /approvals page (chrome.jsx pulls /chats?limit=200 +
        filters client-side on parked_status).
      * Auto-reject Banner only renders when pendingSendText is
        truthy (chats.jsx:595).
      * Cancel restores composer-edit state (operator can retry).
      * Send & reject clears composer + closes banner.
    """
    cid, cleanup_urls = _seed_chat_ladder(base_url, unique_suffix)
    tool_call_id = f"tc-u0113-{unique_suffix}"
    policy_id = f"pol-u0113-{unique_suffix}"
    inner_tool = "fs.delete"
    gate_reason = "operator gate (U0113)"

    try:
        # ----- 1. Inject the park ------------------------------------
        _inject_chat_approval_park(
            chat_id=cid,
            tool_call_id=tool_call_id,
            inner_tool_name=inner_tool,
            policy_id=policy_id,
            gate_reason=gate_reason,
        )

        # ----- 2. /approvals — cross-page sanity ---------------------
        page.goto(
            f"{console_url}#/approvals",
            wait_until="domcontentloaded",
        )
        row = page.locator(f"[data-testid='approval-row-{cid}']")
        expect(row).to_be_visible(timeout=15_000)
        expect(row).to_contain_text(inner_tool)

        # ----- 3. Navigate /chats/{cid} ------------------------------
        page.goto(
            f"{console_url}#/chats/{cid}",
            wait_until="domcontentloaded",
        )

        # Sanity: inline approval card visible (U0112 covers this in
        # depth; here we just make sure the precondition holds before
        # exercising the auto-reject surface).
        banner = page.locator("[data-testid='approval-banner']")
        expect(banner).to_be_visible(timeout=15_000)

        # ----- 4. Wait for WS to come up; composer enables when so ---
        # The Send button is disabled until WS state == "open"
        # (chats.jsx:625). Wait for the live pill to surface — a
        # plain Send-button enable check is enough.
        composer = page.get_by_placeholder("Send a message", exact=False)
        expect(composer).to_be_visible(timeout=10_000)
        first_message = "What's the status of the deploy?"
        composer.fill(first_message)

        send_btn = page.get_by_role("button", name="Send", exact=True).last
        expect(send_btn).to_be_enabled(timeout=15_000)
        send_btn.click()

        # ----- 5. Auto-reject Banner appears ------------------------
        # The Banner has the warning title text from chats.jsx:599.
        auto_reject_banner = page.locator(
            ".banner", has_text="auto-reject the pending approval",
        )
        expect(auto_reject_banner).to_be_visible(timeout=10_000)
        # Detail line includes the tool name from chats.jsx:600.
        expect(auto_reject_banner).to_contain_text(inner_tool)
        # Both action buttons render.
        send_and_reject = auto_reject_banner.get_by_role(
            "button", name="Send & reject", exact=True,
        )
        cancel_btn = auto_reject_banner.get_by_role(
            "button", name="Cancel", exact=True,
        )
        expect(send_and_reject).to_be_visible(timeout=5_000)
        expect(cancel_btn).to_be_visible(timeout=5_000)

        # ----- 6. Cancel — banner closes, composer text retained ----
        cancel_btn.click()
        expect(auto_reject_banner).not_to_be_visible(timeout=5_000)
        # composer still has the text — the user can retry.
        expect(composer).to_have_value(first_message)

        # ----- 7. Re-trigger by clicking Send again ------------------
        send_btn.click()
        expect(auto_reject_banner).to_be_visible(timeout=10_000)

        # ----- 8. Send & reject — composer clears + banner closes ----
        send_and_reject.click()
        # Banner closes (pendingSendText -> null in chats.jsx:534).
        expect(auto_reject_banner).not_to_be_visible(timeout=10_000)
        # Composer clears (chats.jsx:534 setComposer("")).
        expect(composer).to_have_value("", timeout=5_000)
    finally:
        _cleanup(base_url, cleanup_urls)
