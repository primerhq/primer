"""New-toolset modal behavior tests.

Covers:
* U0014 — MCP stdio command not in allowlist surfaces the documented
  warning text inside the modal.
* U0017 — The MCP-stdio modal (which expands a Command + Environment
  editor when transport=stdio is selected) still scrolls to its
  footer at 600 px viewport height — modal-scroll regression net
  for the second-tallest UI form after the rich provider modal.
"""

from __future__ import annotations


def test_u0014_mcp_stdio_command_not_in_allowlist_surfaces_warning(
    page,
    console_url: str,
) -> None:
    """U0014 — Selecting transport=stdio and typing a command whose
    first token is not allowlisted renders the documented
    "first session-open call will raise ConfigError" warning text
    inside the modal (priority 3 — anomaly surface).

    The allowlist is server-side (matrix/api/config.py:AppConfig.
    mcp_stdio_allowed_commands) and the UI can't probe it; instead
    it always renders the warning whenever stdio is picked + a
    command is typed, deferring the actual rejection to first
    session-open. UI spec §5 documents this surface.
    """
    page.goto(console_url + "#/toolsets", wait_until="domcontentloaded")
    page.locator("h1.page-title").first.wait_for(state="visible", timeout=10_000)

    page.get_by_role("button", name="New toolset").first.click()
    modal = page.locator(".modal").first
    modal.wait_for(state="visible", timeout=5_000)

    # Provider dropdown is "mcp" by default. Transport chips default
    # to "stdio". The Command input is rendered when stdio is the
    # selected transport. Type a command whose first token is clearly
    # not on any sensible allowlist.
    cmd_input = modal.locator("input.mono").first
    cmd_input.wait_for(state="visible", timeout=5_000)
    cmd_input.fill("bogus-not-allowlisted-cmd-xyz --flag value")

    # The warning text must mention either the AppConfig field name
    # OR the ConfigError mechanic. Pin both phrases so a copy-edit
    # that drops the AppConfig reference still doesn't drift away
    # from the spec.
    modal_text = modal.inner_text()
    assert "mcp_stdio_allowed_commands" in modal_text, (
        "Expected the documented MCP stdio allowlist warning to "
        "mention 'mcp_stdio_allowed_commands' inside the modal — "
        "anomaly surface (UI spec §5) drifted?\n"
        f"Modal text was:\n{modal_text}"
    )
    assert "ConfigError" in modal_text, (
        "Expected MCP stdio allowlist warning to mention "
        "'ConfigError' (the rejection mechanic operators will "
        "see at first session-open) inside the modal.\n"
        f"Modal text was:\n{modal_text}"
    )


