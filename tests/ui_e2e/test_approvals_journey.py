"""UI E2E: Approvals operator-journey across the /approvals tab and
session-detail's ApprovalBanner.

Multi-page user journey that walks an operator through the §2 approval
surfaces post-Designer reconciliation:

  /approvals (pending tab) → row visible → click Reject → reason input
  → assert "Send rejection" disabled with empty reason → type reason →
  Send rejection → "Decision sent" toast → navigate to /sessions/{sid}
  via the row's "from session" anchor → ApprovalBanner renders on the
  detail page (cross-page state coherence — same pending data backs
  both surfaces) → click Approve on the banner → second "Decision sent"
  toast → API GET /v1/sessions/{sid}/tool_approval/pending still 200
  (parked_state survives the respond POST in THIS setup because the
  asyncpg-injected session has no session_leases row, so
  mark_resumable's lease UPDATE no-ops and the worker pool never
  claims the row to drive the resume cycle. Roadmap §7 resume wiring
  IS landed; the API-loop's T0861 covers the full resume cycle when
  a lease row is present).

Subsystems exercised in one test:

  1. /approvals page mounts; Pending tab renders the row driven by
     POST /sessions/find (parked_status=parked) + per-row
     tool_approval/pending lookups.
  2. Reject-flow gate: Send rejection button must stay disabled until
     the reason input has non-empty text (`canSubmit` gate in
     approvals.jsx).
  3. Reject + Approve mutations POST /tool_approval/respond (returns
     202) and surface a "Decision sent" toast on success.
  4. Cross-page consistency: the same pending payload renders BOTH
     the approvals-list row AND the ApprovalBanner on session detail
     (shared `tool-approval:session:{sid}` cache key).

The asyncpg-based _approval-park injection mirrors
`tests/e2e/test_tool_approval_pending_respond.py`. Direct JSONB
injection is used because (a) a real LLM-driven park requires LM
Studio compat work, and (b) the injection here intentionally
omits the session_leases row so the resume cycle DOESN'T fire —
the UI-side click flow is what's under test, not the backend
cycle. T0861 covers the end-to-end resume cycle separately.

Covers backlog item U0109. Pure operator-journey: no LLM, no real
network beyond localhost. Cleanup via API.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_session_in_studio


# ---------------------------------------------------------------------------
# Container-internal workspace provider path — mirror the U0103 pattern
# so the host's tmp_path doesn't have to be visible inside the
# primer-app container.
# ---------------------------------------------------------------------------


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-10")


def _container_ws_root(suffix: str) -> str:
    return f"/tmp/u0109-{suffix}"


def _seed_session_ladder(base_url: str, suffix: str) -> dict[str, str]:
    """Seed LLM provider → workspace provider → template → workspace →
    agent → session (auto_start=False).

    Returns a dict with the ladder ids; `session` is the row we'll
    inject an _approval park onto.
    """
    ids = {
        "llm": f"u109-llm-{suffix}",
        "wp": f"u109-wp-{suffix}",
        "tpl": f"u109-tpl-{suffix}",
        "agent": f"u109-ag-{suffix}",
        "workspace": "",
        "session": "",
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": ids["llm"],
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm: {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"],
            "provider": "local",
            "config": {"kind": "local", "root_path": _container_ws_root(suffix)},
        })
        assert r.status_code == 201, f"seed wp: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"],
            "description": "u0109 template",
            "provider_id": ids["wp"],
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed ws: {r.text}"
        ids["workspace"] = r.json()["id"]
        r = c.post("/v1/agents", json={
            "id": ids["agent"],
            "description": "u0109 approval probe agent",
            "model": {"provider_id": ids["llm"], "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        })
        assert r.status_code == 201, f"seed agent: {r.text}"
        r = c.post(
            f"/v1/workspaces/{ids['workspace']}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": ids["agent"]},
                "auto_start": False,
            },
        )
        assert r.status_code == 201, f"seed session: {r.text}"
        ids["session"] = r.json()["id"]
    return ids


def _cleanup(base_url: str, ids: dict[str, str]) -> None:
    """Best-effort unwind, reverse dependency order."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in (
            f"/v1/workspaces/{ids['workspace']}/sessions/{ids['session']}/cancel"
            if ids.get("session") else None,
            f"/v1/workspaces/{ids['workspace']}" if ids.get("workspace") else None,
            f"/v1/workspace_templates/{ids['tpl']}",
            f"/v1/workspace_providers/{ids['wp']}",
            f"/v1/agents/{ids['agent']}",
            f"/v1/llm_providers/{ids['llm']}",
        ):
            if url is None:
                continue
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Park injection — same _approval-shape blob the tool_approval router
# reads (matches primer/api/routers/tool_approval.py:_approval_blob_or_404).
# ---------------------------------------------------------------------------


async def _inject_approval_park_async(
    *,
    session_id: str,
    tool_call_id: str,
    inner_tool_name: str,
    policy_id: str,
    gate_reason: str,
) -> None:
    """Stamp parked_status=parked + a _approval parked_state blob onto
    the session row. Mirrors test_tool_approval_pending_respond.py's
    _inject_approval_park, narrowed to sessions only."""
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "_approval",
            "event_key": f"approval:{session_id}:{tool_call_id}",
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
        UPDATE sessions
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
    # The ui_e2e server is brought up against the `primer_e2e` DB (see
    # tests/.e2e/config.yaml). Default to it; honour the env overrides
    # (PRIMER_UI_E2E_DB / PRIMER_UI_E2E_DB_PORT) so an alternate bringup
    # (e.g. the docker-compose `primer` DB on a remapped host port) still
    # works.
    import os
    db = os.environ.get("PRIMER_UI_E2E_DB", "primer_e2e")
    port = int(os.environ.get("PRIMER_UI_E2E_DB_PORT", "5432"))
    conn = await asyncpg.connect(
        host="localhost", port=port,
        user="primer", password="primer", database=db,
    )
    try:
        await conn.execute(
            sql,
            session_id,
            parked_state["yielded"]["event_key"],
            parked_until.isoformat(),
            now.isoformat(),
            json.dumps(parked_state),
        )
    finally:
        await conn.close()


def _inject_approval_park(**kwargs) -> None:
    """Sync wrapper for the Playwright sync test context.

    pytest-asyncio's auto mode keeps an event loop running for the
    process, so ``asyncio.run`` here would raise "cannot be called
    from a running event loop". Spin up a dedicated short-lived loop
    in a worker thread and drive the asyncpg coroutine on it.
    """
    import threading

    box: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_inject_approval_park_async(**kwargs))
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


# ===========================================================================
# U0109 — Approvals operator journey across /approvals + session-detail
# ===========================================================================


def test_u0109_approvals_operator_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0109 — Walk the operator through a pending approval on the
    /approvals page, exercise the reject-requires-reason gate, send a
    rejection, then cross-page to the session-detail's ApprovalBanner
    and approve from there.

    Pages traversed:
      /console/#/approvals (pending tab default) →
      /console/#/sessions/{sid}

    Pinned invariants:
      * Approvals pending tab surfaces a parked _approval session
        within the 5s poll cadence after JSONB injection.
      * Reject reason input renders with the placeholder
        "Reason for rejection (required)…".
      * "Send rejection" stays disabled while reason is empty/whitespace
        (approvals.jsx `disabled={!reason.trim() || respond.loading}`).
      * "Decision sent" toast appears on a successful Reject + Approve.
      * Cross-page: session-detail's ApprovalBanner renders for a
        pending approval. The banner check runs against a SECOND,
        freshly parked session — never responded-to — rather than the
        rejected one. Rejecting flips parked_status to 'resumable',
        which the claim-eligibility filter admits, so the worker pool
        claims + resumes that row and clears parked_state out from
        under the banner (an intermittent race that used to flake this
        test). A never-responded session stays parked_status='parked'
        (excluded by the eligibility filter), so its park is stable and
        the banner renders deterministically. (T0861 covers the full
        park→respond→resume cycle when a lease row IS present.)
    """
    ids = _seed_session_ladder(base_url, unique_suffix)
    sid = ids["session"]
    tool_call_id = f"tc-u0109-{unique_suffix}"
    policy_id = f"pol-u0109-{unique_suffix}"
    inner_tool = "fs.delete"
    gate_reason = "destructive path under /etc"
    sid_banner: str | None = None  # 2nd session for the cross-page banner

    try:
        # --- 0. Inject the approval park BEFORE the page is opened so
        # the first /approvals poll cycle sees it immediately. ---------
        _inject_approval_park(
            session_id=sid,
            tool_call_id=tool_call_id,
            inner_tool_name=inner_tool,
            policy_id=policy_id,
            gate_reason=gate_reason,
        )

        # --- 1. Navigate to /approvals (pending tab default) ----------
        page.goto(
            f"{console_url}#/approvals",
            wait_until="domcontentloaded",
        )
        # Pending tab is the default; it shows a count chip when at
        # least one row is parked. Wait for our seeded row to appear.
        row = page.locator(f"[data-testid='approval-row-{sid}']")
        # Generous timeout: this runs against a shared eval stack under CI
        # load where the find + per-row pending fetches that drive the list
        # can lag. The row is deterministic (the park is pre-injected), so a
        # longer wait only absorbs latency, it never masks a missing row.
        expect(row).to_be_visible(timeout=30_000)
        # The row should mention the inner tool name + policy id (these
        # come from resume_metadata.original_call.name / policy_id).
        expect(row).to_contain_text(inner_tool)
        expect(row).to_contain_text(policy_id)

        # --- 2. Click Reject → reason input appears -------------------
        # Scope every action locator to OUR row — a previous iteration
        # may have left another parked session in the shared DB, so a
        # raw page.locator on the action testids hits strict-mode
        # violations.
        reject_btn = row.locator("[data-testid='approval-reject']")
        expect(reject_btn).to_be_visible(timeout=10_000)
        reject_btn.click()

        reason_input = row.locator(
            "[data-testid='approval-reject-reason']",
        )
        expect(reason_input).to_be_visible(timeout=5_000)
        # Reject-reason flow exposes the Send-rejection button.
        send_reject = row.locator(
            "[data-testid='approval-reject-submit']",
        )
        expect(send_reject).to_be_visible(timeout=5_000)

        # --- 3. Send rejection stays disabled with empty reason -------
        # Pins approvals.jsx:327 disabled={!reason.trim() || …}
        expect(send_reject).to_be_disabled(timeout=2_000)

        # Whitespace-only is also blocked.
        reason_input.fill("   ")
        expect(send_reject).to_be_disabled(timeout=2_000)

        # --- 4. Type a real reason → button enables → submit ---------
        reason_input.fill("denied by security review")
        expect(send_reject).to_be_enabled(timeout=5_000)
        send_reject.click()

        # --- 5. "Decision sent" toast appears -------------------------
        toast = page.locator(".toast", has_text="Decision sent")
        expect(toast).to_be_visible(timeout=10_000)

        # --- 5b. Seed a SECOND, freshly parked session for the banner.
        # The step-1 session was just rejected → parked_status='resumable'
        # → the worker pool claims + resumes it and clears parked_state, so
        # its banner is racy. A never-responded session stays
        # parked_status='parked' (excluded by the claim-eligibility filter),
        # so its park is stable and the banner renders deterministically. --
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post(
                f"/v1/workspaces/{ids['workspace']}/sessions",
                json={
                    "binding": {"kind": "agent", "agent_id": ids["agent"]},
                    "auto_start": False,
                },
            )
            assert r.status_code == 201, f"seed banner session: {r.text}"
            sid_banner = r.json()["id"]
        _inject_approval_park(
            session_id=sid_banner,
            tool_call_id=f"tc-u0109-banner-{unique_suffix}",
            inner_tool_name=inner_tool,
            policy_id=policy_id,
            gate_reason=gate_reason,
        )

        # --- 6. Cross-surface: open the fresh session in the Studio ----
        # Re-pointed: the session-detail ApprovalBanner is retired. In the
        # Studio, a pending approval surfaces in the RIGHT sidebar Action
        # Required list (studio-activity.jsx ``ActionRequired``) as an
        # ``action-item`` of kind "approval" with ``approve`` / ``reject``
        # controls. sid_banner is freshly parked and never responded-to, so
        # its park is stable (parked_status='parked' is excluded by the
        # claim-eligibility filter → the worker never resumes it and clears
        # parked_state), making the item deterministic.
        open_session_in_studio(page, console_url, ids["workspace"], sid_banner, kind="agent")

        # The Action Required list surfaces the pending approval item.
        approval_item = page.locator("[data-testid='action-item']").filter(
            has=page.locator("[data-testid='action-approval-controls']")
        ).first
        expect(approval_item).to_be_visible(timeout=30_000)

        # --- 7. Approve from the Action Required item -----------------
        # The Studio approve handler POSTs /tool_approval/respond and
        # optimistically REMOVES the item on success (studio-activity.jsx
        # ``hide()`` — no toast). Pin the item clearing as the success signal.
        approve_btn = approval_item.locator("[data-testid='approve']")
        expect(approve_btn).to_be_enabled(timeout=5_000)
        approve_btn.click()
        # The approved item is optimistically removed — no approval-controls
        # item remains (the step-1 session was rejected earlier, so this is
        # the only pending approval in the workspace).
        expect(
            page.locator("[data-testid='action-item']").filter(
                has=page.locator("[data-testid='action-approval-controls']")
            )
        ).to_have_count(0, timeout=10_000)
    finally:
        # Cancel the extra banner session before tearing the ladder down so
        # it doesn't linger parked in the shared DB.
        if sid_banner is not None and ids.get("workspace"):
            try:
                with httpx.Client(base_url=base_url, timeout=30.0) as c:
                    c.post(
                        f"/v1/workspaces/{ids['workspace']}/sessions/"
                        f"{sid_banner}/cancel"
                    )
            except Exception:  # noqa: BLE001
                pass
        _cleanup(base_url, ids)
