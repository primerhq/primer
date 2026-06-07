"""SPIKE (Task 1.1) — a REAL console component renders inside a doc embed
against fixture data, with NO live network call.

This is the foundational proof for the user-docs revamp: the iframe
isolation mechanism (locked in ui/components/docs/embed-preview.jsx) hosts
the unmodified ``window.AgentsPage`` whose ``window.primerApi`` is the
fixture-backed stub ``DocsMakeStubApi``. The fixture is the committed
``primer/user_docs/_fixtures/agents-page.json`` (agent ``weekly-digest``).

Assertions:
  * the rendered DOM contains the text ``weekly-digest`` (the fixture agent),
  * it is the REAL component markup (an agent row in the ``.tbl`` table whose
    id cell text equals ``weekly-digest`` and whose model cell shows the
    fixture provider ``demo-openai`` / ``gpt-4o``), NOT a hand-drawn mock,
  * the page made NO request to ``/v1/agents`` (proof the stub, not the live
    api, served the data).

Run: PRIMER_RUN_UI_E2E=1 PRIMER_UI_E2E_BASE_URL=http://127.0.0.1:8000 \
        uv run pytest tests/ui_e2e/test_docs_embed_spike.py -q
"""

from __future__ import annotations

import re

import pytest

from tests._support.smk import smk  # noqa: E402

pytestmark = smk("SMK-DOCS-EMBED-SPIKE")

_SCREENSHOT = "/tmp/docs_embed_spike.png"


def test_real_agents_page_renders_in_iframe_under_fixture_stub(
    page,
    base_url: str,
) -> None:
    # Record every request so we can PROVE no live /v1/agents fetch happened.
    requests: list[str] = []
    page.on("request", lambda req: requests.append(req.url))

    page.goto(f"{base_url}/console/docs-embed-spike.html", wait_until="domcontentloaded")

    # The host page reports lifecycle in #spike-status; wait for "done".
    status = page.locator("#spike-status")
    status.wait_for(state="attached", timeout=10_000)
    page.wait_for_function(
        "() => { const e = document.getElementById('spike-status');"
        " return e && (e.dataset.state === 'done' || e.dataset.state.startsWith('error')); }",
        timeout=20_000,
    )
    state = status.get_attribute("data-state")
    assert state == "done", f"harness did not reach done state: {state!r}"

    # The component renders inside the embed iframe.
    frame = page.frame_locator("iframe#host")

    # --- ASSERTION 1: the fixture agent text is present. ---
    id_cell = frame.locator("td.mono", has_text=re.compile(r"^weekly-digest$"))
    id_cell.first.wait_for(state="visible", timeout=15_000)
    assert id_cell.count() >= 1, "expected an id cell with text 'weekly-digest'"

    # --- ASSERTION 2: it is the REAL component markup, not a mock. ---
    # The real AgentsPage renders each agent as a <tr> in a table.tbl; the row
    # for weekly-digest must also surface the fixture's provider+model.
    row = frame.locator("table.tbl tbody tr", has_text="weekly-digest")
    assert row.count() == 1, "expected exactly one real agent row for weekly-digest"
    row_text = row.first.inner_text()
    assert "demo-openai" in row_text, f"fixture provider missing from row: {row_text!r}"
    assert "gpt-4o" in row_text, f"fixture model missing from row: {row_text!r}"

    page.screenshot(path=_SCREENSHOT, full_page=True)

    # --- ASSERTION 3: NO live network call to the agents API. ---
    agents_calls = [u for u in requests if "/v1/agents" in u]
    assert not agents_calls, (
        "stub must serve fixture data with NO live /v1/agents fetch; saw: "
        + ", ".join(agents_calls)
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
