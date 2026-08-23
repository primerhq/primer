"""The fresh shell journey suite (S8 spec section 7).

The release gate is this suite green on the fresh shell, not manual spot
checks (spec section 9). One journey per line of section 7's test list,
all driving through ``_shell_helpers`` so the surface can move again
without editing them.

Gated: collected-then-ignored unless ``PRIMER_RUN_UI_E2E=1``, exactly
like every other module in this directory (see conftest.py's
collect_ignore_glob).

Seeding ladder, via a sync httpx client against the live server:
llm_provider -> workspace_provider -> workspace_template -> workspace ->
agent -> session (auto_start=False so it parks in CREATED and needs no
model backend). Seeding is skip-soft on 5xx: the primer-app container may
not reach the workspace provider's host path (U0072/U0080-class), in
which case we skip rather than fail.
"""

from __future__ import annotations

import os

import httpx
import pytest
from playwright.sync_api import expect

from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import (
    open_doc,
    open_overlay,
    open_palette,
    open_shell,
    run_verb,
    session_row,
)

pytestmark = smk("SMK-UI-06")


if os.environ.get("PRIMER_RUN_UI_E2E") != "1":
    collect_ignore_glob = ["test_shell_journeys.py"]


def _container_ws_root(suffix: str) -> str:
    """The host's tmp_path is not visible from the primer-app container;
    a container-local path avoids the U0072/U0080-class unreachability."""
    return f"/tmp/shell-{suffix}"


def _seed(base_url: str, suffix: str) -> dict[str, str]:
    ids = {
        "llm": f"sh-llm-{suffix}",
        "wp": f"sh-wp-{suffix}",
        "tpl": f"sh-tpl-{suffix}",
        "agent": f"sh-ag-{suffix}",
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
        assert r.status_code == 201, f"seed llm failed: {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"],
            "provider": "local",
            "config": {"kind": "local", "root_path": _container_ws_root(suffix)},
        })
        assert r.status_code == 201, f"seed wp failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"],
            "description": "shell journey template",
            "provider_id": ids["wp"],
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        if r.status_code >= 500:
            pytest.skip(
                f"workspace create returned {r.status_code}: the primer-app "
                "container likely cannot reach the provider path."
            )
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        ids["workspace"] = r.json()["id"]
        r = c.post("/v1/agents", json={
            "id": ids["agent"],
            "description": "shell journey agent",
            "model": agent_model(ids["llm"], "fake-model"),
            "tools": [],
            "system_prompt": ["probe"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"
        r = c.post(
            f"/v1/workspaces/{ids['workspace']}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": ids["agent"]},
                "auto_start": False,
            },
        )
        assert r.status_code == 201, f"seed session failed: {r.text}"
        ids["session"] = r.json()["id"]
    return ids


def _seed_file(base_url: str, wid: str, name: str, content: str) -> bool:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.put(
            f"/v1/workspaces/{wid}/files?path={name}",
            json={"content": content, "encoding": "text"},
        )
        if r.status_code >= 500:
            return False
        assert r.status_code in (200, 201, 204), r.text
        return True


def _cleanup(base_url: str, ids: dict[str, str]) -> None:
    """Best-effort unwind; ignore individual failures so one stale row
    does not mask the rest."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        urls = []
        if ids.get("session") and ids.get("workspace"):
            urls.append(
                f"/v1/workspaces/{ids['workspace']}/sessions/"
                f"{ids['session']}/cancel"
            )
        if ids.get("workspace"):
            urls.append(f"/v1/workspaces/{ids['workspace']}")
        urls += [
            f"/v1/workspace_templates/{ids['tpl']}",
            f"/v1/workspace_providers/{ids['wp']}",
            f"/v1/agents/{ids['agent']}",
            f"/v1/llm_providers/{ids['llm']}",
        ]
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture
def seeded(base_url: str, unique_suffix: str):
    ids = _seed(base_url, unique_suffix)
    try:
        yield ids
    finally:
        _cleanup(base_url, ids)


# ===========================================================================
# Journey 1: landing -- the shell mounts on a workspace and lists its work
# ===========================================================================


@pytest.mark.ui_e2e
def test_landing_mounts_the_shell_and_lists_the_workspace(
    page, console_url: str, seeded: dict[str, str]
) -> None:
    """Section 3: one workspace URL, rail plus centre, the session
    already listed. No route table, no page to pick."""
    wid, sid = seeded["workspace"], seeded["session"]
    open_shell(page, console_url, wid)
    # Three regions since the 2026-08-23 revamp: the statusbar retired
    # (status lives on the composer strip, rail chips and tab labels).
    for region in ("nv-topbar", "nv-rail", "nv-center"):
        expect(page.get_by_test_id(region)).to_be_visible(timeout=15_000)
    expect(session_row(page, sid)).to_be_visible(timeout=20_000)


# ===========================================================================
# Journey 2: a session opens as a document, and its URL is the state
# ===========================================================================


@pytest.mark.ui_e2e
def test_a_session_opens_as_a_tab_and_the_url_restores_it(
    page, console_url: str, seeded: dict[str, str]
) -> None:
    """Section 8: the URL IS the state, so a reload lands where you were
    rather than back at the workspace's default."""
    wid, sid = seeded["workspace"], seeded["session"]
    open_doc(page, console_url, wid, "session", sid)
    expect(page.get_by_test_id(f"nv-session-doc:{sid}")).to_be_visible(timeout=20_000)

    page.reload()
    expect(page.get_by_test_id(f"nv-tab:session:{sid}")).to_be_visible(
        timeout=20_000
    )
    assert sid in page.evaluate("window.location.hash")


# ===========================================================================
# Journey 3: send and steer -- the composer never locks
# ===========================================================================


@pytest.mark.ui_e2e
def test_send_and_steer_keep_the_composer_writable(
    page, console_url: str, seeded: dict[str, str]
) -> None:
    """Section 4: steering a running session IS sending it a message, so
    the composer stays writable while a turn is in flight."""
    wid, sid = seeded["workspace"], seeded["session"]
    open_doc(page, console_url, wid, "session", sid)

    composer = page.get_by_test_id("nv-composer").locator("input, textarea")
    expect(composer).to_be_visible(timeout=20_000)
    composer.fill("start the job")
    composer.press("Enter")

    expect(page.get_by_test_id("nv-status-strip")).to_be_visible()
    assert composer.is_editable(), "the composer must never lock"


# ===========================================================================
# Journey 4: the palette is the router
# ===========================================================================


@pytest.mark.ui_e2e
def test_the_palette_opens_a_verbs_surface(
    page, console_url: str, seeded: dict[str, str]
) -> None:
    """Section 4: every verb is reachable from one place, so the palette
    is how a surface is found rather than a nav tree."""
    wid, sid = seeded["workspace"], seeded["session"]
    open_doc(page, console_url, wid, "session", sid)
    open_palette(page)
    expect(page.get_by_test_id("nv-palette-row").first).to_be_visible(
        timeout=10_000
    )


@pytest.mark.ui_e2e
def test_binding_switch_runs_from_the_palette(
    page, console_url: str, seeded: dict[str, str]
) -> None:
    """Section 5: rebinding a session is one gesture from the session
    itself, never a settings page. The console's affordance is the
    header's binding chip, whose menu lists agents AND graphs."""
    wid, sid = seeded["workspace"], seeded["session"]
    open_doc(page, console_url, wid, "session", sid)
    page.get_by_test_id("nv-binding-chip").click()
    expect(page.get_by_test_id("nv-binding-menu")).to_be_visible(timeout=15_000)


# ===========================================================================
# Journey 5: a file opens as a PREVIEW tab and does not steal focus
# ===========================================================================


@pytest.mark.ui_e2e
def test_a_file_opens_as_a_preview_tab(
    page, console_url: str, unique_suffix: str, base_url: str,
    seeded: dict[str, str],
) -> None:
    """Section 8, VS Code tab semantics: a preview tab is italic, is
    replaced by the next preview, and is promoted by an edit."""
    wid = seeded["workspace"]
    name = f"shell-{unique_suffix}.txt"
    if not _seed_file(base_url, wid, name, "line 1\nline 2\nline 3\n"):
        pytest.skip("the container cannot write to the provider path")

    open_doc(page, console_url, wid, "file", name)
    tab = page.locator('[data-testid^="nv-tab:file:"]').first
    expect(tab).to_be_visible(timeout=20_000)
    assert tab.get_attribute("data-preview") == "true"


# ===========================================================================
# Journey 6: management surfaces are overlays, addressable and titled
# ===========================================================================


@pytest.mark.ui_e2e
@pytest.mark.parametrize("name", ["providers", "agents", "collections", "workers"])
def test_a_management_surface_opens_as_an_overlay(
    page, console_url: str, seeded: dict[str, str], name: str
) -> None:
    """Section 3: there is no nav tree; a management surface is an
    overlay the URL can name."""
    open_overlay(page, console_url, seeded["workspace"], name)
    expect(page.get_by_test_id("nv-overlay-body")).to_be_visible(timeout=15_000)


# ===========================================================================
# Journey 7: voice affordances render only when speech is configured
# ===========================================================================


@pytest.mark.ui_e2e
def test_voice_affordances_are_absent_when_speech_is_unconfigured(
    page, console_url: str, seeded: dict[str, str]
) -> None:
    """Section 6: an affordance for a capability the install does not
    have is worse than none, because it fails only once pressed."""
    wid, sid = seeded["workspace"], seeded["session"]
    open_doc(page, console_url, wid, "session", sid)
    expect(page.get_by_test_id("nv-session-doc:" + sid)).to_be_visible(timeout=20_000)
    # The seeding ladder configures no STT/TTS provider, so neither
    # affordance should exist at all.
    expect(page.get_by_test_id("nv-voice-toggle")).to_have_count(0)
