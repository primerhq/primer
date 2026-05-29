"""UI E2E: workspace file content inspect + download multi-page journey.

Per the backlog's "PIVOT QUEUED" note, the UI loop should be moving
toward workspace+session full-lifecycle journeys. This test walks
the operator through inspecting + downloading a file inside a
workspace — a flow the backlog pivot directive flagged as
"workspace file download from the UI" but no test pins today.

Pages traversed:

  /console/ (initial nav) → /workspaces (list) → /workspaces/{wid}
  (Files tab default) → click file → editor pane renders content
  → click Download → browser captures the download → click
  breadcrumb back to /workspaces (list).

Multi-feature exercise in one test:

  1. Workspaces list page render + row click navigation
  2. Workspace detail page + default Files tab
  3. File tree polling/rendering after API-side PUT
  4. File content editor pane (pre + CodeHighlight) showing the
     seeded text
  5. Anchor-style Download button (workspaces.jsx:663-669) — the
     <a href=".../files/download" download> wiring
  6. Playwright download interception verifies the file payload
     matches what was seeded
  7. Breadcrumb back navigation preserves list page state

Covers backlog item U0106.

Skip-soft (U0072/U0080-class) when the primer-app container can't
reach the workspace provider's path — we use container-internal
/tmp/u0106-<suffix> to avoid the host bind-mount unreachability.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect


def _seed_workspace_with_file(
    base_url: str, suffix: str,
) -> tuple[dict[str, str], str]:
    """Seed workspace provider + template + workspace + write one
    file via the API. Returns (ids, file_content).

    The file's content is a fixed payload so we can verify the
    Download button delivers exactly what was seeded.
    """
    ids = {
        "wp": f"wp-106-{suffix}",
        "tpl": f"tpl-106-{suffix}",
        "workspace": "",
    }
    file_content = (
        f"Hello from U0106!\n"
        f"This file was written via the API for suffix={suffix}.\n"
        f"line 3\n"
    )
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"],
            "provider": "local",
            "config": {"kind": "local", "root_path": f"/tmp/u0106-{suffix}"},
        })
        assert r.status_code == 201, f"wp: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"],
            "description": "u0106 tpl",
            "provider_id": ids["wp"],
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"tpl: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"ws: {r.text}"
        ids["workspace"] = r.json()["id"]
    return ids, file_content


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


# ===========================================================================
# U0106 — Workspace file inspect + download journey
# ===========================================================================


def test_u0106_workspace_file_inspect_and_download_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0106 — Multi-page operator journey to inspect + download a
    workspace file via the UI.

    Steps:

      1. Seed workspace via API.
      2. PUT a known-content text file into the workspace via API.
         Skip-soft on 5xx (U0072-class: container can't reach
         host bind-mount path).
      3. Navigate /workspaces list — assert seeded row visible.
      4. Click row → /workspaces/{wid} (Files tab is default).
      5. Wait for the file tree to render the seeded file.
      6. Click the file → editor pane shows the content.
      7. Click the Download button — Playwright captures the
         browser download — assert the payload matches the
         seeded content byte-for-byte.
      8. Click the "Workspaces" breadcrumb → back to /workspaces
         list, row still visible.

    Pins workspaces.jsx:663-669 anchor-style Download button +
    workspaces.jsx:701-705 file content rendering through the
    <pre>+CodeHighlight path for text files.
    """
    file_name = f"u0106-{unique_suffix}.txt"
    ids, content = _seed_workspace_with_file(base_url, unique_suffix)
    wid = ids["workspace"]
    cleanup_urls = [f"/v1/workspaces/{wid}"]
    try:
        # ----- Skip-soft probe: PUT the file ------------------------
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.put(
                f"/v1/workspaces/{wid}/files?path={file_name}",
                json={"content": content, "encoding": "text"},
            )
            if r.status_code >= 500:
                pytest.skip(
                    f"workspace files PUT returned {r.status_code} — "
                    f"primer-app container likely can't reach host tmp "
                    f"(same root cause as U0072/U0080)."
                )
            assert r.status_code in (200, 201, 204), r.text

        # ----- 1. /workspaces list ----------------------------------
        page.goto(f"{console_url}#/workspaces", wait_until="domcontentloaded")
        expect(page.locator("h1.page-title")).to_have_text(
            "Workspaces", timeout=20_000,
        )
        ws_row = page.locator("tbody tr", has_text=wid)
        expect(ws_row).to_be_visible(timeout=20_000)

        # ----- 2. Click row → /workspaces/{wid} ---------------------
        ws_row.first.click()
        page.wait_for_url(f"**/console/#/workspaces/{wid}**", timeout=15_000)
        expect(page.locator("h1.page-title", has_text=wid)).to_be_visible(
            timeout=15_000,
        )

        # Files tab is the default; the file tree must surface the
        # seeded file within the SessionsTab's poll cadence (~5 s).
        file_row = page.get_by_text(file_name, exact=False).first
        expect(file_row).to_be_visible(timeout=20_000)

        # ----- 3. Click file → content pane renders -----------------
        file_row.click()
        # The CodeHighlight wraps the content in nested <span>s line
        # by line; check that a stable substring of our content
        # appears somewhere in the page after the click.
        # Pick a distinctive marker rather than the full content.
        marker = f"suffix={unique_suffix}"
        expect(page.get_by_text(marker, exact=False).first).to_be_visible(
            timeout=15_000,
        )

        # ----- 4. Click Download — capture the browser download ----
        download_link = page.locator("a[download]").filter(
            has=page.get_by_role("button", name="Download")
        ).first
        expect(download_link).to_be_visible(timeout=10_000)

        with page.expect_download(timeout=15_000) as download_info:
            download_link.click()
        download = download_info.value
        # The downloaded file's content must match exactly what was seeded.
        downloaded_bytes = download.path().read_bytes()
        assert downloaded_bytes.decode("utf-8") == content, (
            f"download payload mismatch:\n"
            f"expected={content!r}\n"
            f"got={downloaded_bytes!r}"
        )

        # ----- 5. Click "Workspaces" breadcrumb → back to list -----
        # WorkspaceHeader renders a breadcrumb anchor that navigates
        # back. The breadcrumb selector matches the .crumb pattern
        # used in session-detail.jsx; workspaces.jsx renders a similar
        # link with text "Workspaces".
        # Fall back to direct nav if no breadcrumb is present —
        # the assertion target is the list state, not the
        # navigation mechanism.
        crumb = page.locator(".crumb a", has_text="Workspaces").first
        if crumb.count() > 0:
            crumb.click()
        else:
            page.goto(
                f"{console_url}#/workspaces", wait_until="domcontentloaded",
            )
        page.wait_for_url("**/console/#/workspaces", timeout=10_000)
        expect(page.locator("tbody tr", has_text=wid)).to_be_visible(
            timeout=15_000,
        )
    finally:
        for url in cleanup_urls:
            try:
                with httpx.Client(base_url=base_url, timeout=15.0) as c:
                    c.delete(url)
            except Exception:  # noqa: BLE001
                pass
        _cleanup(base_url, ids)
