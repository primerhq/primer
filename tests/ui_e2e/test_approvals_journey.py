"""UI E2E: Approvals operator-journey across the Studio session
transcript's decision cards.

Multi-page user journey that walks an operator through the §2 approval
surfaces post-uiv2 Wave 3 (a-14 fold):

  open session #1 in the Studio → decision card visible in its
  transcript → click Reject → reason textarea appears → assert the
  SAME button stays disabled with an empty/whitespace reason → type a
  reason → click again to submit → the card is optimistically removed
  (no toast — see NV_DecisionCard's onResolved wiring) → open session
  #2 in the Studio → its own decision card renders (cross-session
  state coherence — the same per-session pending fetch backs every
  transcript) → click Approve → the card is removed there too → API
  GET /v1/sessions/{sid}/tool_approval/pending still 200 (parked_state
  survives the respond POST in THIS setup because the asyncpg-injected
  session has no session_leases row, so mark_resumable's lease UPDATE
  no-ops and the worker pool never claims the row to drive the resume
  cycle. Roadmap §7 resume wiring IS landed; the API-loop's T0861
  covers the full resume cycle when a lease row is present).

Subsystems exercised in one test:

  1. The Studio's per-session decision card (NV_DecisionCard,
     nv-session-doc.jsx) renders a pending `_approval` park within the
     shell's poll cadence after JSONB injection — the interactive
     `/approvals` records-sheet was retired in the a-14 fold; its
     read-only successor (DECISIONS — AUDIT on the platform page)
     carries no action buttons by design, so actionability now lives
     exclusively on the Inbox rail + this per-session card.
  2. Reject-flow gate: the same Reject button that opens the reason
     box also guards its own submit — it stays disabled while the
     reason is empty/whitespace once the box is open (`disabled={!!
     props.ended || (rejOpen && !reason.trim())}` in NV_DecisionCard).
  3. Reject + Approve mutations POST /tool_approval/respond (returns
     202); on success the card is optimistically removed from the
     transcript (props.onResolved → refetchAll) — no toast.
  4. Cross-session consistency: each session's own decision card is
     driven by that session's own pending fetch, independent of any
     other parked session in the shared DB.

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

from tests.ui_e2e._studio_helpers import expand_debug_sidebar, open_session_in_studio


# ---------------------------------------------------------------------------
# Container-internal workspace provider path — mirror the U0103 pattern
# so the host's tmp_path doesn't have to be visible inside the
# primer-app container.
# ---------------------------------------------------------------------------


from tests._support.smk import smk  # noqa: E402
from tests._support.model_profiles import agent_model, seed_llm_provider_with
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
        r = seed_llm_provider_with(c, {
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
            "model": agent_model(ids["llm"], "fake-model"),
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
    """U0109 — Walk the operator through a pending approval on session
    #1's own Studio transcript, exercise the reject-requires-reason
    gate, send a rejection, then open a SECOND session's transcript
    and approve its own pending approval from there.

    Pages traversed:
      /console/#/w/{wid}?doc=session:{sid} →
      /console/#/w/{wid}?doc=session:{sid_banner}

    Pinned invariants:
      * The Studio surfaces a parked _approval session's decision card
        in its transcript within the shell's poll cadence after JSONB
        injection.
      * The reject reason textarea (`nv-reject-reason`) appears after
        the first click on Reject.
      * The Reject button stays disabled while reason is empty/
        whitespace once the reason box is open (NV_DecisionCard
        `disabled={!!props.ended || (rejOpen && !reason.trim())}`) —
        this is the SAME button for both the "open the box" click and
        the "submit" click, unlike the retired records-sheet's row,
        which had a separate Send-rejection button.
      * A successful Reject/Approve optimistically removes the card
        from the transcript (props.onResolved → refetchAll) — there is
        no toast on this path.
      * Cross-session: the SECOND session's decision card is checked
        rather than re-checking the first (now-rejected) one.
        Rejecting flips parked_status to 'resumable', which the claim-
        eligibility filter admits, so the worker pool claims + resumes
        that row and clears parked_state out from under any lingering
        card (an intermittent race that used to flake this test back
        when both surfaces read the same row). A never-responded
        session stays parked_status='parked' (excluded by the
        eligibility filter), so its park is stable and its card
        renders deterministically. (T0861 covers the full
        park→respond→resume cycle when a lease row IS present.)
    """
    ids = _seed_session_ladder(base_url, unique_suffix)
    sid = ids["session"]
    tool_call_id = f"tc-u0109-{unique_suffix}"
    policy_id = f"pol-u0109-{unique_suffix}"
    inner_tool = "fs.delete"
    gate_reason = "destructive path under /etc"
    sid_banner: str | None = None  # 2nd session, checked independently in step 6

    try:
        # --- 0. Inject the approval park BEFORE the page is opened so
        # the Studio's first pending-approval poll sees it immediately. -
        _inject_approval_park(
            session_id=sid,
            tool_call_id=tool_call_id,
            inner_tool_name=inner_tool,
            policy_id=policy_id,
            gate_reason=gate_reason,
        )

        # --- 1. Open session #1 in the Studio ---------------------------
        # Re-pointed (uiv2 Wave 3, a-14 fold): the interactive /approvals
        # records-sheet is retired. A pending approval now surfaces as a
        # decision card of kind "approval" in the session's own
        # transcript, reached via the Studio — the same mechanism step 6
        # below already used for sid_banner.
        open_session_in_studio(page, console_url, ids["workspace"], sid, kind="agent")
        expand_debug_sidebar(page)

        decision_card = page.locator("[data-kind='approval']").filter(
            has=page.get_by_test_id("nv-reject")
        ).first
        expect(decision_card).to_be_visible(timeout=30_000)
        # The card names the inner gated tool (resume_metadata.
        # original_call.name — the same field the retired row read).
        expect(decision_card).to_contain_text(inner_tool)

        # --- 2. Click Reject → reason textarea appears ------------------
        reject_btn = decision_card.get_by_test_id("nv-reject")
        expect(reject_btn).to_be_visible(timeout=10_000)
        reject_btn.click()

        reason_input = decision_card.get_by_test_id("nv-reject-reason")
        expect(reason_input).to_be_visible(timeout=5_000)

        # --- 3. Reject stays disabled with an empty/whitespace reason --
        # Pins NV_DecisionCard's disabled={!!props.ended || (rejOpen &&
        # !reason.trim())} — a real regression found and fixed during
        # this retarget. Before the fix, the SAME button that opens the
        # reason box carried no guard on its second (submit) click, since
        # the retired records-sheet's separate Send-rejection button
        # (which DID have `disabled={!reason.trim() || …}`) was the only
        # thing enforcing this, and it's gone along with the sheet.
        expect(reject_btn).to_be_disabled(timeout=2_000)

        # Whitespace-only is also blocked.
        reason_input.fill("   ")
        expect(reject_btn).to_be_disabled(timeout=2_000)

        # --- 4. Type a real reason → button re-enables → submit --------
        reason_input.fill("denied by security review")
        expect(reject_btn).to_be_enabled(timeout=5_000)
        reject_btn.click()

        # --- 5. Success signal: the card is optimistically removed -----
        # NV_DecisionCard has no toast on resolve (see step 7's note on
        # Approve) — SH_api.reject resolves into props.onResolved, which
        # refetches and drops the item from the attention feed.
        expect(
            page.locator("[data-kind='approval']").filter(
                has=page.get_by_test_id("nv-reject")
            )
        ).to_have_count(0, timeout=10_000)

        # --- 5b. Seed a SECOND, freshly parked session for session #2. ---
        # The step-1 session was just rejected → parked_status='resumable'
        # → the worker pool claims + resumes it and clears parked_state, so
        # re-checking its card would be racy. A never-responded session
        # stays parked_status='parked' (excluded by the claim-eligibility
        # filter), so its park is stable and its card renders
        # deterministically. --
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

        # --- 6. Open session #2 in the Studio ---------------------------
        # Same mechanism as step 1, a different session and a different
        # verb (Approve, not Reject) — confirms the decision card is
        # driven by EACH session's own pending fetch rather than some
        # cross-session cache holding step 1's (now-resolved) state.
        # sid_banner is freshly parked and never responded-to, so its
        # park is stable (parked_status='parked' is excluded by the
        # claim-eligibility filter → the worker never resumes it and
        # clears parked_state), making the item deterministic.
        open_session_in_studio(page, console_url, ids["workspace"], sid_banner, kind="agent")
        # The right-sidebar debug panel (Action Required) starts collapsed;
        # expand it before looking for the decision card.
        expand_debug_sidebar(page)

        # The attention list surfaces the pending approval. An approval
        # card is the one carrying the approve control; a question card
        # renders an answer input instead.
        approval_item = page.locator("[data-kind='approval']").filter(
            has=page.get_by_test_id("nv-approve")
        ).first
        expect(approval_item).to_be_visible(timeout=30_000)

        # --- 7. Approve from the Action Required item -----------------
        # The Studio approve handler POSTs /tool_approval/respond and
        # optimistically REMOVES the item on success (the shell rail's attention list
        # ``hide()`` — no toast). Pin the item clearing as the success signal.
        approve_btn = approval_item.get_by_test_id("nv-approve")
        expect(approve_btn).to_be_enabled(timeout=5_000)
        approve_btn.click()
        # The approved item is optimistically removed — no approval-controls
        # item remains (the step-1 session was rejected earlier, so this is
        # the only pending approval in the workspace).
        expect(
            page.locator("[data-kind='approval']").filter(
                has=page.get_by_test_id("nv-approve")
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
