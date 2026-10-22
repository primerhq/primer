"""Console journeys for an MCP toolset's shared OAuth sign-in."""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import BrowserContext, Page, expect  # noqa: E402

from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp_lan  # noqa: E402,F401
from tests._support.smk import smk  # noqa: E402
from tests.ui_e2e._shell_helpers import (  # noqa: E402
    boot_mobile_shell,
    default_wid,
    open_view,
)


pytestmark = smk("SMK-UI-03", status="partial")

PHONE = {"width": 390, "height": 844}


def _oauth_toolset(toolset_id: str, mcp_url: str, *, full: bool = False) -> dict:
    oauth = {"scopes": ["mcp"]}
    if full:
        oauth = {
            "grant": "authorization_code",
            "scopes": ["mcp", "old"],
            "static_client": {
                "client_id": "pre-client",
                "client_secret": "pre-secret",
            },
            "resource_uri": mcp_url,
            "client_name": "primer-e2e",
        }
    return {
        "id": toolset_id,
        "provider": "mcp",
        "config": {
            "transport": "http",
            "config": {
                "url": mcp_url,
                "headers": {"X-Tenant": "kept"} if full else {},
                "oauth": oauth,
            },
        },
    }


def _seed_toolset(base_url: str, toolset_id: str, mcp_url: str, *, full: bool = False) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        response = client.post(
            "/v1/toolsets",
            json=_oauth_toolset(toolset_id, mcp_url, full=full),
        )
        assert response.status_code == 201, response.text


def _connect_browser(base_url: str, toolset_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        started = client.post(f"/v1/toolsets/{toolset_id}/oauth/start")
        assert started.status_code == 200, started.text
        authorized = client.get(
            started.json()["authorize_url"],
            follow_redirects=False,
        )
        assert authorized.status_code == 302, authorized.text
        callback = client.get(authorized.headers["location"], follow_redirects=False)
        assert callback.status_code == 200, callback.text
        assert callback.json()["status"]["state"] == "connected"


def _open_toolsets(page: Page, console_url: str) -> None:
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id("nv-plat-page:toolsets")
    for attempt in (1, 2):
        open_view(
            page,
            console_url,
            default_wid(console_url),
            "platform:toolsets",
        )
        try:
            expect(marker).to_be_visible(
                timeout=10_000 if attempt == 1 else 20_000
            )
            return
        except AssertionError:
            if attempt == 2:
                raise


def _open_toolset_overlay(page: Page, console_url: str, toolset_id: str) -> None:
    _open_toolsets(page, console_url)
    page.get_by_test_id("nv-plat-filter").fill(toolset_id)
    card = page.get_by_test_id(f"pk-card:{toolset_id}")
    expect(card).to_be_visible(timeout=15_000)
    card.get_by_test_id(f"pk-card-open:{toolset_id}").click()
    overlay = page.get_by_test_id("nv-overlay-body")
    expect(overlay).to_be_visible(timeout=15_000)
    expect(overlay).to_contain_text(toolset_id)
    config = overlay.get_by_role("tab", name="Config")
    expect(config).to_have_attribute("aria-selected", "true")


def _cleanup(base_url: str, toolset_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        client.delete(f"/v1/toolsets/{toolset_id}")


def test_connect_from_the_overlay(
    page: Page,
    context: BrowserContext,
    base_url: str,
    console_url: str,
    mock_oauth_mcp_lan,
    unique_suffix: str,
) -> None:
    toolset_id = f"ui-oauth-connect-{unique_suffix}"
    _seed_toolset(base_url, toolset_id, mock_oauth_mcp_lan.mcp_url)
    popup = None
    try:
        _open_toolsets(page, console_url)
        page.get_by_test_id("nv-plat-filter").fill(toolset_id)
        card = page.get_by_test_id(f"pk-card:{toolset_id}")
        expect(card).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id(f"pk-card-status:{toolset_id}")).to_have_text(
            "Needs sign-in"
        )
        card.get_by_test_id(f"pk-card-open:{toolset_id}").click()
        expect(page.get_by_test_id("nv-overlay-body")).to_be_visible(timeout=15_000)

        connect = page.get_by_test_id("ts-auth-connect")
        expect(connect).to_be_visible(timeout=10_000)
        with context.expect_page() as popup_info:
            connect.click()
        popup = popup_info.value
        # The popup toast is a fragile fresh-document seam; the opener poll is
        # the durable signal that the callback completed the exchange.
        popup.wait_for_url(lambda url: "/console/" in url, timeout=20_000)
        expect(page.get_by_test_id("ts-auth-status")).to_contain_text(
            "Connected", timeout=20_000
        )
        page.get_by_test_id("nv-overlay-close").click()
        expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
        expect(page.get_by_test_id(f"pk-card-status:{toolset_id}")).to_have_text(
            "Connected", timeout=10_000
        )
    finally:
        if popup is not None:
            popup.close()
        _cleanup(base_url, toolset_id)


def test_an_edit_keeps_every_auth_field(
    page: Page,
    base_url: str,
    console_url: str,
    mock_oauth_mcp_lan,
    unique_suffix: str,
) -> None:
    toolset_id = f"ui-oauth-edit-{unique_suffix}"
    _seed_toolset(
        base_url,
        toolset_id,
        mock_oauth_mcp_lan.mcp_url,
        full=True,
    )
    _connect_browser(base_url, toolset_id)
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            before = client.get(f"/v1/toolsets/{toolset_id}").json()
        before_http = before["config"]["config"]
        before_oauth = before_http["oauth"]
        assert before_oauth["static_client"]["client_secret"] != "pre-secret"

        _open_toolset_overlay(page, console_url, toolset_id)
        overlay = page.get_by_test_id("nv-overlay-body")
        overlay.get_by_role("button", name="Edit", exact=True).click()
        dialog = page.locator(".modal-overlay")
        expect(dialog).to_contain_text(f"Edit toolset · {toolset_id}", timeout=10_000)
        page.get_by_test_id("ts-oauth-scopes").fill("mcp changed")
        dialog.get_by_role("button", name="Save changes", exact=True).click()
        done = page.get_by_test_id("toolset-connect-done")
        expect(done).to_be_enabled(timeout=20_000)
        done.click()

        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            after_response = client.get(f"/v1/toolsets/{toolset_id}")
            assert after_response.status_code == 200, after_response.text
            after = after_response.json()
            status = client.get(f"/v1/toolsets/{toolset_id}/oauth/status")
            assert status.status_code == 200, status.text

        after_http = after["config"]["config"]
        after_oauth = after_http["oauth"]
        assert after_oauth["scopes"] == ["mcp", "changed"]
        assert {
            key: value for key, value in after_oauth.items() if key != "scopes"
        } == {
            key: value for key, value in before_oauth.items() if key != "scopes"
        }
        assert after_http["headers"] == before_http["headers"]
        assert after_oauth["static_client"]["client_secret"] == before_oauth[
            "static_client"
        ]["client_secret"]
        assert status.json()["state"] == "needs_sign_in"
        expect(page.get_by_test_id("ts-auth-status")).to_contain_text(
            "Needs sign-in", timeout=10_000
        )
    finally:
        _cleanup(base_url, toolset_id)


def test_the_panel_on_a_phone(
    page: Page,
    base_url: str,
    console_url: str,
    mock_oauth_mcp_lan,
    unique_suffix: str,
) -> None:
    toolset_id = f"ui-oauth-phone-{unique_suffix}"
    _seed_toolset(base_url, toolset_id, mock_oauth_mcp_lan.mcp_url)
    try:
        page.set_viewport_size(PHONE)
        wid = default_wid(console_url)
        boot_mobile_shell(
            page,
            f"{console_url}#/w/{wid}?overlay=toolsets::{toolset_id}",
        )
        row = page.get_by_test_id(f"nv-mob-plat-row:{toolset_id}")
        expect(row).to_be_visible(timeout=20_000)
        row.click()
        sheet = page.get_by_test_id("nv-mob-fact-body")
        expect(sheet).to_be_visible(timeout=15_000)
        status = sheet.locator(".nv-mob-fact-status")
        expect(status).to_have_count(1)
        expect(status).to_contain_text("Needs sign-in")
        expect(status).to_have_attribute("data-tone", "warn")
        expect(page.get_by_test_id("nv-mob-fact-edit-note")).to_have_text(
            "Edit on desktop"
        )
    finally:
        _cleanup(base_url, toolset_id)
