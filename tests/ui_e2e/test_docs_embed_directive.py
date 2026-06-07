"""Task 1.2 + 1.4 -- embed: directive renders real console component with fixtures.

Tests that:
  * ``DocsBootEmbedIframe`` (from embed-preview.jsx) is available and works,
  * it fetches ``/v1/user_docs/_fixtures/agents-page.json`` from the server
    (Task 1.4 route),
  * the real ``window.AgentsPage`` renders the ``weekly-digest`` fixture row
    inside the iframe,
  * no ``/v1/agents`` live network call is made (data came from the stub).

The test uses the ``docs-embed-directive-test.html`` harness (a standalone
HTML page, served as a static file) rather than the full docs app page, so
login is handled once via ``page.request.post`` before the page navigates.

Run:
  PRIMER_RUN_UI_E2E=1 PRIMER_UI_E2E_BASE_URL=http://127.0.0.1:8000 \\
    uv run pytest tests/ui_e2e/test_docs_embed_directive.py -q
"""

from __future__ import annotations

import re

import pytest

from tests._support.smk import smk  # noqa: E402

pytestmark = smk("SMK-DOCS-EMBED-DIRECTIVE")

_SCREENSHOT = "/tmp/docs_embed_directive.png"

# Credentials: try the e2e user first; fall back to the default dev user.
_CANDIDATE_USERS = [
    {"username": "e2e", "password": "e2e-password-123"},
    {"username": "testuser", "password": "testpassword"},
]


def _try_login(page, base_url: str) -> bool:
    """Attempt to log in via the API using known candidate credentials.

    Returns True on success. Registers the e2e user if not yet present.
    """
    # Try to register the e2e user (ignored if already exists).
    try:
        resp = page.request.post(
            f"{base_url}/v1/auth/register",
            data='{"username":"e2e","password":"e2e-password-123"}',
            headers={"Content-Type": "application/json"},
        )
        _ = resp  # 201 on first run, 4xx if user exists -- both ok
    except Exception:
        pass

    for creds in _CANDIDATE_USERS:
        try:
            resp = page.request.post(
                f"{base_url}/v1/auth/login",
                data=f'{{"username":"{creds["username"]}","password":"{creds["password"]}"}}',
                headers={"Content-Type": "application/json"},
            )
            if resp.status == 200:
                return True
        except Exception:
            continue
    return False


def test_embed_directive_agents_page_renders_with_fixture(
    page,
    base_url: str,
) -> None:
    """embed: directive mechanism: iframe boots real AgentsPage under fixture stub."""

    # Record all requests so we can assert no live /v1/agents call.
    requests: list[str] = []
    page.on("request", lambda req: requests.append(req.url))

    # Log in so the fixture route (/v1/user_docs/_fixtures/...) is reachable.
    logged_in = _try_login(page, base_url)
    assert logged_in, (
        "Could not log in with any known credential; "
        "ensure the server has a user registered"
    )

    # Navigate to the standalone test harness (not the full docs app).
    page.goto(
        f"{base_url}/console/docs-embed-directive-test.html",
        wait_until="domcontentloaded",
    )

    # The host page reports lifecycle in #status; wait for "done".
    status = page.locator("#status")
    status.wait_for(state="attached", timeout=10_000)
    page.wait_for_function(
        "() => { const e = document.getElementById('status');"
        " return e && (e.dataset.state === 'done'"
        "  || e.dataset.state.startsWith('error')); }",
        timeout=30_000,
    )
    state = status.get_attribute("data-state")
    assert state == "done", f"harness did not reach done state: {state!r}"

    # The component renders inside the embed iframe.
    frame = page.frame_locator("iframe#host")

    # --- ASSERTION 1: the fixture agent text is present. ---
    id_cell = frame.locator("td.mono", has_text=re.compile(r"^weekly-digest$"))
    id_cell.first.wait_for(state="visible", timeout=15_000)
    assert id_cell.count() >= 1, "expected an id cell with text 'weekly-digest'"

    # --- ASSERTION 2: it is REAL component markup (not a mock). ---
    row = frame.locator("table.tbl tbody tr", has_text="weekly-digest")
    assert row.count() == 1, "expected exactly one real agent row for weekly-digest"
    row_text = row.first.inner_text()
    assert "demo-openai" in row_text, f"fixture provider missing from row: {row_text!r}"
    assert "gpt-4o" in row_text, f"fixture model missing from row: {row_text!r}"

    page.screenshot(path=_SCREENSHOT, full_page=True)

    # --- ASSERTION 3: no live /v1/agents call. ---
    agents_calls = [u for u in requests if "/v1/agents" in u]
    assert not agents_calls, (
        "stub must serve fixture data with NO live /v1/agents fetch; saw: "
        + ", ".join(agents_calls)
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
