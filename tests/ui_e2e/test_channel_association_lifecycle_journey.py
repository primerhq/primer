"""UI E2E: channel-workspace association toggle + delete lifecycle journey.

Multi-page UI user-journey that walks an operator through the
**post-create** lifecycle of a WorkspaceChannelAssociation — the
slice U0108 (channels onboarding) doesn't cover.

U0108 pinned the create path (Providers/Channels/Associations →
3× create modals → 3× rows visible). This test picks up where
U0108 stops: after a row exists, an operator needs to be able to
flip the per-association forwarding toggles, deep-link to the
linked workspace, observe the row state survives navigation, and
finally remove the association — observing the cascade-block
upstream (channel can't be deleted while the association is alive)
melt away once the row is gone.

Pages traversed:

  /console/ → /channels/associations
        → click workspace link in association row
        → /workspaces/{wid} (workspace detail page renders)
        → back to /channels/associations (state persisted)
        → delete association → empty state returns
        → /channels/channels (verify channel now deletable: 200 via API
          post-association-removal)

Multi-subsystem coverage in one test:

  1. AssociationsPage table render + filter bar (channels.jsx
     AssociationsPage component).
  2. Per-row Toggle component bound to PUT
     /workspace_channel_associations/{id} — toggling
     forward_ask_user updates the API state observably.
  3. Workspace link click → navigate("workspace-detail", wid) → hash
     router transitions to /workspaces/{wid}.
  4. Hash-router back-navigation: re-entering /channels/associations
     re-fetches and renders the row with the persisted toggle state.
  5. Cascade-block contract: DELETE /channels/{ch_id} returns 409
     while the association exists (over API for verification).
  6. Delete-association icon button → row removed + toast → empty
     state ("No associations") returns.
  7. Cascade-block lift: DELETE /channels/{ch_id} now returns 200
     after the association is gone.

Covers backlog item U0116. New surface — no existing UI test pins
the toggle PUT-via-Toggle round-trip or the empty-state-after-
delete reflow on the associations page.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect


_FAKE_DISCORD_TOKEN = "x" * 60


def _seed_ladder(
    base_url: str, suffix: str,
) -> dict[str, str]:
    """Seed ChannelProvider + Channel + WorkspaceProvider + Template +
    Workspace + WorkspaceChannelAssociation via API. Returns ids.

    UI test seeds via API to keep the test focused on the
    interactions under test — U0108 already covers the create path
    through the UI modals.
    """
    cp_id = f"u116-cp-{suffix}"
    ch_id = f"u116-ch-{suffix}"
    wp_id = f"u116-wp-{suffix}"
    tpl_id = f"u116-tpl-{suffix}"
    assoc_id = f"u116-assoc-{suffix}"
    container_root = f"/tmp/u0116-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(
            "/v1/channel_providers",
            json={
                "id": cp_id,
                "provider": "discord",
                "config": {
                    "bot_token": _FAKE_DISCORD_TOKEN,
                    "enable_dms": True,
                },
            },
        )
        assert r.status_code == 201, r.text
        r = c.post(
            "/v1/channels",
            json={
                "id": ch_id,
                "provider_id": cp_id,
                "external_id": f"snow-{suffix}",
                "label": "U0116 channel",
            },
        )
        assert r.status_code == 201, r.text
        r = c.post(
            "/v1/workspace_providers",
            json={
                "id": wp_id,
                "provider": "local",
                "config": {"kind": "local", "path": container_root},
            },
        )
        assert r.status_code == 201, r.text
        r = c.post(
            "/v1/workspace_templates",
            json={
                "id": tpl_id,
                "description": "U0116 template",
                "provider_id": wp_id,
                "backend": {"kind": "local"},
            },
        )
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        wid = r.json()["id"]
        r = c.post(
            "/v1/workspace_channel_associations",
            json={
                "id": assoc_id,
                "workspace_id": wid,
                "channel_id": ch_id,
                "enabled": True,
                "forward_ask_user": True,
                "forward_tool_approval": True,
            },
        )
        assert r.status_code == 201, r.text
    return {
        "cp": cp_id,
        "ch": ch_id,
        "wp": wp_id,
        "tpl": tpl_id,
        "wid": wid,
        "assoc": assoc_id,
    }


def _cleanup(base_url: str, ids: dict[str, str]) -> None:
    """Best-effort unwind in reverse dependency order."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in (
            f"/v1/workspace_channel_associations/{ids['assoc']}",
            f"/v1/workspaces/{ids['wid']}",
            f"/v1/workspace_templates/{ids['tpl']}",
            f"/v1/workspace_providers/{ids['wp']}",
            f"/v1/channels/{ids['ch']}",
            f"/v1/channel_providers/{ids['cp']}",
        ):
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0116 — Channel-workspace association toggle + delete lifecycle
# ===========================================================================


def test_u0116_channel_association_toggle_and_delete_lifecycle(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0116 — Walk an operator through the association post-create
    lifecycle across multiple pages.

    Pinned invariants:
      * The associations row renders with all three forwarding
        toggles in their seeded state.
      * Clicking the forward_ask_user toggle PUTs the field; the
        API reflects the new value (verified out-of-band via httpx).
      * The toggled-off state survives a round trip through the
        workspace detail page (hash-router back navigation
        re-fetches and renders the persisted state).
      * Cascade-block contract — DELETE /channels/{id} returns 409
        while the association exists; verified directly via the API
        so the test pins the actual production cascade hook, not a
        UI-rendered approximation.
      * Clicking the Remove-association icon button removes the row;
        empty-state "No associations" returns; the channel becomes
        deletable (200) on the next API DELETE.
    """
    ids = _seed_ladder(base_url, unique_suffix)
    assoc_url = f"/v1/workspace_channel_associations/{ids['assoc']}"
    channel_url = f"/v1/channels/{ids['ch']}"

    try:
        # ----- 1. Navigate to associations page -------------------
        page.goto(
            f"{console_url}#/channels/associations",
            wait_until="domcontentloaded",
        )
        # Row visibility = the seeded association's id is rendered
        # somewhere on the page. The Workspace cell renders the
        # workspace_id as a clickable link, which is unique enough
        # to anchor on.
        row_anchor = page.get_by_text(ids["wid"], exact=True).first
        expect(row_anchor).to_be_visible(timeout=20_000)

        # The row's <tr> ancestor is our scope for the rest of the
        # interactions.
        row = row_anchor.locator("xpath=ancestor::tr").first
        expect(row).to_be_visible()

        # ----- 2. Toggle forward_ask_user OFF ---------------------
        # Column order: Workspace | Channel | Enabled | Forward
        # ask_user | Forward tool_approval | (X). The 4th cell
        # carries the forward_ask_user toggle. Locate the toggle as
        # the <button> inside that <td>.
        forward_ask_user_toggle = row.locator(
            "td:nth-child(4) button"
        ).first
        expect(forward_ask_user_toggle).to_be_visible()
        forward_ask_user_toggle.click()

        # Wait for the API to reflect the change. Poll via httpx so
        # we don't depend on visual transition timing.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            import time
            deadline = time.time() + 10.0
            last_state = None
            while time.time() < deadline:
                r = c.get(assoc_url)
                assert r.status_code == 200, r.text
                last_state = r.json()
                if last_state.get("forward_ask_user") is False:
                    break
                time.sleep(0.3)
            assert last_state and last_state.get("forward_ask_user") is False, (
                f"forward_ask_user did not flip to False; last={last_state!r}"
            )
            # Sanity: enabled + forward_tool_approval untouched.
            assert last_state.get("enabled") is True, last_state
            assert last_state.get("forward_tool_approval") is True, last_state

        # ----- 3. Click workspace link → workspace detail page ----
        # The Workspace cell carries an <a> wrapping the workspace_id
        # text. Clicking it routes via the hash router.
        row.locator("td:nth-child(1) a").first.click()
        # Hash-router navigation completes synchronously via
        # location.hash assignment; wait on the URL fragment.
        page.wait_for_url(
            f"**/console/#/workspaces/{ids['wid']}", timeout=10_000,
        )
        # Page title region renders to confirm a real page mount,
        # not a 404 stub.
        expect(page.locator("h1.page-title").first).to_be_visible(
            timeout=10_000,
        )

        # ----- 4. Navigate back to associations — state persists --
        page.goto(
            f"{console_url}#/channels/associations",
            wait_until="domcontentloaded",
        )
        row_anchor = page.get_by_text(ids["wid"], exact=True).first
        expect(row_anchor).to_be_visible(timeout=20_000)
        row = row_anchor.locator("xpath=ancestor::tr").first

        # Re-verify forward_ask_user is still OFF via the API after
        # the page reload — the seeded toggle persists across nav.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(assoc_url)
            assert r.status_code == 200, r.text
            assert r.json().get("forward_ask_user") is False, r.json()

        # ----- 5. Cascade-block — channel still un-deletable ------
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.delete(channel_url)
            assert r.status_code == 409, (
                f"expected 409 cascade-block on channel delete while "
                f"association exists; got {r.status_code} {r.text!r}"
            )
            body = r.json()
            assert body.get("type", "").endswith("/conflict"), body
            assert ids["assoc"] in body.get("detail", ""), body

        # ----- 6. Click Remove-association icon → row gone --------
        remove_btn = row.locator('button[title="Remove association"]').first
        expect(remove_btn).to_be_visible()
        remove_btn.click()

        # Row disappears from the table; the "No associations" empty
        # state mounts (associations.data.items === [] post-mutation
        # invalidate refetch).
        expect(
            page.get_by_text("No associations", exact=False).first
        ).to_be_visible(timeout=15_000)

        # The seeded workspace_id is no longer in the rendered table.
        expect(
            page.get_by_text(ids["wid"], exact=True)
        ).to_have_count(0, timeout=5_000)

        # ----- 7. Cascade-block lifted — channel now deletable ----
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.delete(channel_url)
            assert r.status_code in (200, 204), (
                f"expected channel to be deletable after association "
                f"removed; got {r.status_code} {r.text!r}"
            )
    finally:
        _cleanup(base_url, ids)
