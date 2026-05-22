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


def test_u0017_new_toolset_mcp_stdio_modal_scrolls_to_footer_at_600px(
    page,
    console_url: str,
) -> None:
    """U0017 — At 1366x600 with provider=mcp + transport=stdio
    selected (which expands the modal with the Command + Environment
    KvEditor), the Create button in the pinned footer is still
    reachable via in-modal scroll. Modal-scroll regression net
    (UI spec §3) for the toolset modal family.

    Sister of U0015 (provider modal) and U0016 (agent modal). The
    toolset modal is the third tall form in the console; covering
    it completes the fan-out across all create-modal families.
    """
    page.set_viewport_size({"width": 1366, "height": 600})

    page.goto(console_url + "#/toolsets", wait_until="domcontentloaded")
    page.locator("h1.page-title").first.wait_for(state="visible", timeout=10_000)

    page.get_by_role("button", name="New toolset").first.click()
    modal = page.locator(".modal").first
    modal.wait_for(state="visible", timeout=5_000)

    # Modal fits the viewport.
    viewport_h = page.viewport_size["height"]
    box = modal.bounding_box()
    assert box is not None, "could not measure modal bounding box"
    assert box["height"] <= viewport_h, (
        f"toolset modal exceeds viewport height ({box['height']}px > "
        f"{viewport_h}px); the UI spec §3 modal-scroll contract is "
        f"broken for the MCP-stdio form"
    )

    # Scroll body to bottom + assert Create is reachable.
    body = modal.locator(".modal-b").first
    body.evaluate("el => el.scrollTo({top: el.scrollHeight})")

    create = modal.get_by_role("button", name="Create").first
    create.wait_for(state="visible", timeout=5_000)
