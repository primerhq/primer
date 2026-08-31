"""SEV regression: creating a new session while another session's doc is
open and has transcript content must NOT bleed that content into the
new session's tab.

Root cause (see tests/ui/test_session_doc_cross_session_bleed_fix.py for
the static coverage): NV_SessionDoc mounted with no `key`, so switching
which session's tab was active re-rendered the SAME component instance
instead of unmounting/remounting it - for one render the component's own
useResource("history") hook still held the PREVIOUS session's data, and
a REST-history seed effect fed those records into the NEW session's
store before the hook caught up. Fixed by keying NV_SessionDoc by
session/tab id (nv-studio.jsx, nv-mobile-shell.jsx) plus a defense-in-
depth session_id stamp before SS_apply (nv-session-doc.jsx,
session-store.js).
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_session_in_studio
from tests._support.model_profiles import agent_model, seed_llm_provider_with


def _seed_llm_provider(base_url: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": pid, "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code in (201, 409), r.text


def _seed_agent(base_url: str, aid: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/agents", json={
            "id": aid, "description": "cross-session bleed probe",
            "model": agent_model(pid, "fake-model"),
            "tools": [], "system_prompt": ["test"],
        })
        assert r.status_code in (201, 409), r.text


def _seed_workspace(base_url: str, wp: str, tpl: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code in (201, 409), r.text
        r = c.post("/v1/workspace_templates", json={
            "id": tpl, "description": "tpl", "provider_id": wp,
            "backend": {"kind": "local"},
        })
        assert r.status_code in (201, 409), r.text
        r = c.post("/v1/workspaces", json={"template_id": tpl})
        assert r.status_code == 201, r.text
        return r.json()["id"]


def _seed_agent_session(base_url: str, wid: str, aid: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "agent", "agent_id": aid},
            "auto_start": False,
        })
        assert r.status_code == 201, r.text
        return r.json()["id"]


def test_new_session_tab_starts_empty_beside_a_session_with_content(
    base_url, console_url, page, tmp_path, unique_suffix,
) -> None:
    pid = f"xsb-prov-{unique_suffix}"
    aid = f"xsb-agent-{unique_suffix}"
    wp = f"xsb-wp-{unique_suffix}"
    tpl = f"xsb-tpl-{unique_suffix}"
    marker = f"unique marker {unique_suffix}"

    _seed_llm_provider(base_url, pid)
    _seed_agent(base_url, aid, pid)
    wid = _seed_workspace(base_url, wp, tpl, tmp_path)
    sid_a = _seed_agent_session(base_url, wid, aid)

    # ----- 1. Open session A and give it real transcript content -------
    open_session_in_studio(page, console_url, wid, sid_a, kind="agent")
    # Deep-linked docs open as PREVIEW tabs (TG_openTab's default); a
    # second preview tab REPLACES the first one in the same slot rather
    # than adding beside it. The reported repro (both screenshots) shows
    # A surviving alongside B, so promote A to a permanent tab first -
    # double-click is nv-tab-groups.jsx's own promote trigger, matching
    # how an operator actually working a session (not just glancing at
    # a preview) would leave it.
    page.get_by_test_id(f"nv-tg-tab:session:{sid_a}").dblclick()
    composer = page.get_by_test_id("nv-composer-input")
    composer.click()
    composer.fill(marker)
    composer.press("Enter")
    # The user's own message is a durable record regardless of whether
    # the (fake) agent ever replies - that's enough "content" to bleed.
    expect(page.get_by_text(marker, exact=False).first).to_be_visible(
        timeout=15_000,
    )

    # ----- 2. Create a NEW session via the console's own UI flow -------
    # This is the real repro path: Alt+n opens the create-session
    # overlay (session.create, nv-shell.jsx) while session A's doc,
    # with the marker text, is still the active tab.
    page.keyboard.press("Alt+n")
    overlay = page.get_by_test_id("nv-overlay:new-session")
    expect(overlay).to_be_visible(timeout=10_000)
    page.get_by_test_id("nv-ns-create").click()
    # submit() POSTs then closes the overlay on success - wait for the
    # round trip rather than assuming the click alone was synchronous.
    expect(overlay).to_be_hidden(timeout=15_000)

    # ----- 3. The new tab must NOT show session A's content -------------
    # con.setDoc() switches the active tab to the new session as soon as
    # create succeeds. Only one session doc is ever mounted at a time
    # (renderDoc(activeTab) is a single slot), so session A's own doc
    # testid disappearing and a (different) session doc testid taking
    # its place IS the tab switch.
    expect(page.get_by_test_id(f"nv-session-doc:{sid_a}")).to_have_count(
        0, timeout=15_000,
    )
    new_doc = page.locator('[data-testid^="nv-session-doc:"]').first
    expect(new_doc).to_be_visible(timeout=15_000)
    sid_b = new_doc.get_attribute("data-testid").split(":", 1)[1]
    assert sid_b != sid_a, "the tab never switched to a new session"

    # Scoped to the new session's own doc element - the rail/tab-strip
    # may legitimately preview session A's last message elsewhere on the
    # page (a different, non-buggy surface); the bug under test is
    # specifically the NEW doc's own transcript rendering it.
    expect(new_doc.get_by_text(marker, exact=False)).to_have_count(0)

    # ----- 4. Session A's own store must still be intact ------------------
    # The reported bug also showed up as a SPLIT VIEW where two panes
    # rendered the same transcript under different session ids - but that
    # is a downstream consequence of the same store-poisoning event, not
    # a separate code path (SS_getStore(wid, sid) is a plain keyed lookup;
    # nv-tab-groups.jsx's groups are already key={g.id}-scoped, so two
    # panes never share a React subtree regardless of layout). Confirming
    # A's store is untouched after B was created is the load-bearing half
    # of that: if A ever got contaminated by B (or vice versa) here, no
    # later pane arrangement could show it correctly either.
    page.get_by_test_id(f"nv-tg-tab:session:{sid_a}").click()
    expect(page.get_by_test_id(f"nv-session-doc:{sid_a}")).to_be_visible(
        timeout=10_000,
    )
    expect(
        page.get_by_test_id(f"nv-session-doc:{sid_a}").get_by_text(
            marker, exact=False,
        ).first
    ).to_be_visible(timeout=10_000)
