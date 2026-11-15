"""Journey: the console registers a claude_code agent runtime (the claude_code plan, task 10).

Opens the Providers page at ``view=platform:providers:agent_runtime``
through the ``_shell_helpers`` facade (waits for the shell, assigns the
view hash, retries once if mount-time address normalisation wins),
registers a runtime whose
``binary_path`` is the shared claude CLI double (``tests/llm/claude_code/
fake_claude.py`` under its shell wrapper: the default version 2.1.322 and a
logged-in account), reads the discovery's status block (installed, version,
logged in), imports the ``sonnet`` profile, and confirms the row stays out of
the LLM class (the two classes read one plural; the LLM class excludes
``claude_code`` rows, ui/components/providers/register-common.jsx's
``PC_classAccepts``).

The server must run on THIS host (the double is a host path), so the journey
runs against the host-process e2e server with auth disabled:

  PRIMER_E2E_AUTH_DISABLED=1 scripts/e2e/bringup.sh
  PRIMER_RUN_UI_E2E=1 PRIMER_E2E_PORT=8765 \
    uv run pytest tests/ui_e2e/test_agent_runtime_register_journey.py -q -n0
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect
from tests.llm.claude_code.fake_claude import write_fake_wrapper
from tests.ui_e2e._shell_helpers import default_wid, open_view

pytestmark = pytest.mark.ui_e2e


def _open_providers_page(page: Page, console_url: str, sub: str) -> None:
    """The Providers platform page at ``view=platform:providers:<sub>``: wait for the shell before assigning its view hash, retrying
    once if mount-time address normalisation wins the first navigation."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id("nv-plat-page:providers")
    for attempt in (1, 2):
        open_view(page, console_url, default_wid(console_url), f"platform:providers:{sub}")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


@pytest.mark.ui_e2e
def test_agent_runtime_register_journey(
    page: Page, console_url: str, base_url: str,
    tmp_path: Path, unique_suffix: str,
) -> None:
    binary = write_fake_wrapper(tmp_path)
    provider_id = f"cc-ui-{unique_suffix}"

    try:
        # ---- the agent runtimes view ----
        _open_providers_page(page, console_url, "agent_runtime")
        page.wait_for_selector(
            '[data-testid="provider-body-agent_runtime"], [data-testid="pc-empty:agent_runtime"]',
            timeout=15_000,
        )

        # ---- register: the connection page (only the Claude Code kind is offered) ----
        page.get_by_test_id("pc-register-open").click()
        dialog = page.get_by_test_id("pc-register")
        expect(dialog).to_be_visible(timeout=15_000)
        dialog.get_by_test_id("pc-field:id").fill(provider_id)
        dialog.get_by_test_id("pc-field:binary_path").fill(binary)
        expect(dialog.get_by_test_id("pc-register-next")).to_be_enabled(timeout=10_000)
        dialog.get_by_test_id("pc-register-next").click()

        # ---- the models page: the discovery's status block ----
        status = dialog.get_by_test_id("pc-cc-runtime")
        expect(status).to_be_visible(timeout=20_000)
        text = status.inner_text()
        assert "2.1.322" in text, f"the status block does not carry the version: {text}"
        assert "logged in" in text, f"the status block does not carry the login: {text}"
        for bad in ("not installed", "not logged in", "below the minimum"):
            assert bad not in text, f"the status block says {bad!r}: {text}"

        # ---- import the sonnet profile ----
        dialog.get_by_test_id("pc-import:sonnet").check()
        dialog.get_by_test_id("pc-import-context:sonnet").fill("200000")
        submit = dialog.get_by_test_id("pc-register-submit")
        expect(submit).to_have_text("Import 1 model", timeout=10_000)
        submit.click()
        expect(dialog).to_have_count(0, timeout=30_000)

        # ---- the row is under the agent runtimes class ----
        expect(page.get_by_test_id(f"pk-card:agent_runtime:{provider_id}")).to_be_visible(
            timeout=15_000
        )

        # ---- ...and not under the LLM class ----
        _open_providers_page(page, console_url, "llm")
        # provider-body-llm renders even while pc-loading is showing, so a
        # wait on it succeeds at once; wait the load out first, then take a
        # positive control (the card grid or the empty marker) to prove the
        # rows settled before the zero count can pass.
        expect(page.get_by_test_id("pc-loading")).to_be_hidden(timeout=15_000)
        page.wait_for_selector(
            '[data-testid="pc-cards"], [data-testid="pc-empty:llm"]',
            timeout=15_000,
        )
        expect(page.get_by_test_id(f"pk-card:llm:{provider_id}")).to_have_count(0)
    finally:
        # Best-effort cleanup: the profile, then the provider.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/model_profiles/{provider_id}--sonnet")
            c.delete(f"/v1/llm_providers/{provider_id}")
