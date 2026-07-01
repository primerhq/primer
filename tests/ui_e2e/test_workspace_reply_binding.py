"""UI E2E: the workspace Channels tab manages the reply binding.

Task 19 renames the outbound surface from "channel association" to
"reply binding" to match the Part A model rename. This test seeds a
workspace, navigates to its detail Channels tab, and asserts the panel
now shows a "Reply binding" label (not "Channel association").

Gated by ``PRIMER_RUN_UI_E2E=1`` via the module-level skip in
``tests/ui_e2e/conftest.py``. Uses the ``page`` / ``console_url`` /
``base_url`` fixtures from that conftest, and seeds rows over the REST
API the same way ``test_workspace_file_download_journey.py`` does.
"""

from __future__ import annotations

import httpx

from tests._support.smk import smk
from tests.ui_e2e._studio_helpers import open_workspace_settings

pytestmark = smk("SMK-UI-RB-01")


def _seed_workspace(base_url: str, suffix: str) -> dict[str, str]:
    """Seed workspace provider + template + workspace via the API.

    Returns the ids dict (with the created workspace id filled in).
    """
    ids = {
        "wp": f"wp-rb-{suffix}",
        "tpl": f"tpl-rb-{suffix}",
        "workspace": "",
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"],
            "provider": "local",
            "config": {"kind": "local", "root_path": f"/tmp/u-rb-{suffix}"},
        })
        assert r.status_code == 201, f"wp: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"],
            "description": "reply-binding tpl",
            "provider_id": ids["wp"],
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"tpl: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"ws: {r.text}"
        ids["workspace"] = r.json()["id"]
    return ids


def _cleanup(base_url: str, ids: dict[str, str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in (
            f"/v1/workspaces/{ids['workspace']}" if ids.get("workspace") else None,
            f"/v1/workspace_templates/{ids['tpl']}",
            f"/v1/workspace_providers/{ids['wp']}",
        ):
            if url is None:
                continue
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


def test_workspace_channels_tab_shows_reply_binding_label(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """Open the Studio Settings modal's Channels section and assert the
    reused panel exposes a "Reply binding" label (not "Channel
    association").

    Re-pointed to the Studio: the old ``?tab=channels`` workspace-detail
    tab moved into the Settings modal (studio-settings.jsx →
    ``workspace-settings-nav:channels``), which renders the SAME
    WS_ChannelsTab, so the label/absence assertions are unchanged.
    """
    ids = _seed_workspace(base_url, unique_suffix)
    wid = ids["workspace"]
    try:
        modal = open_workspace_settings(page, console_url, wid, "channels")

        # The reused Channels panel copy frames the outbound surface as a
        # "reply binding"; the old "Channel association" wording must be gone.
        modal.get_by_text("Reply binding", exact=False).first.wait_for(
            state="visible", timeout=10_000,
        )
        body_text = modal.inner_text()
        assert "Reply binding" in body_text, body_text
        assert "Channel association" not in body_text, body_text
    finally:
        _cleanup(base_url, ids)
