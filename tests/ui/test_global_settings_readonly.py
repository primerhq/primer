"""ACL-P4 Task 9 console pins: the global-settings pages render read-only for non-admins.

The web settings page, the speech default selectors, the artifact store
selector and the provider catalog all read the user-tier routes now (the
API half is pinned in tests/api/test_global_settings_read.py); what a
non-admin sees is the same page with the write controls disabled and the
reason "Only an admin can change this" said next to them, never a hidden
page. These are static source pins (the house pattern for catalog tests,
tests/ui/test_provider_catalog_ia_restructure.py).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui" / "components"

NOTE = "Only an admin can change this"


def _src(name: str) -> str:
    return (UI / name).read_text(encoding="utf-8")


def _kit() -> str:
    return (UI / "shared" / "platform-kit.jsx").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Web settings page
# ---------------------------------------------------------------------------


def test_the_web_settings_page_no_longer_hides_itself_from_non_admins() -> None:
    src = _src("web-settings.jsx")
    assert "web-admin-only" not in src, "the admin-only banner is gone: the page renders for everyone"
    assert "Web settings need the admin role" not in src


def test_the_web_settings_page_reads_for_everyone_and_gates_the_writes() -> None:
    src = _src("web-settings.jsx")
    # The reads no longer depend on the role: both kinds fetch for everyone.
    assert 'WEB_useKind("search", true)' in src
    assert 'WEB_useKind("fetch", true)' in src
    # The gate is the console role, read the house way.
    assert "con.role === \"admin\"" in src
    assert "readOnly = !isAdmin" in src
    # Both sections receive the gate and say the reason.
    assert src.count("readOnly={readOnly}") == 2
    assert NOTE in src
    assert 'data-testid={t + "-readonly-note"}' in src
    # The write affordances take the gate: mode switch, default pick, the
    # chain rows and their order buttons, and the "+ Fetcher" action.
    assert "disabled={readOnly || undefined}" in src  # segmented + default select
    assert "disabled={isFirst || readOnly}" in src
    assert "disabled={isLast || readOnly}" in src
    assert "disabled: readOnly || undefined" in src  # the page header action


# ---------------------------------------------------------------------------
# Speech default selectors
# ---------------------------------------------------------------------------


def test_the_speech_selectors_are_readonly_for_non_admins() -> None:
    src = _src("providers/speech-active.jsx")
    assert NOTE in src
    # No console means no person to be an admin: read-only, like before it
    # simply drew nothing.
    assert "con.role !== \"admin\"" in src
    # Every write control takes the gate.
    assert "disabled={state.saving || readOnly}" in src  # stt + tts provider
    assert "disabled={state.saving || readOnly || !providerId || voices.loading}" in src  # the voice
    assert 'data-testid="pc-active-stt-readonly-note"' in src
    assert 'data-testid="pc-active-tts-readonly-note"' in src
    # The gate is a hook-safe read at the top of the selectors, not a
    # mid-render conditional on the console.
    for fn in ("PC_SttActiveSelector", "PC_TtsActiveSelector"):
        body = src[src.index("function " + fn) :]
        assert body.index("PC_speechReadOnly()") < body.index("return (")


# ---------------------------------------------------------------------------
# Artifact store selector
# ---------------------------------------------------------------------------


def test_the_artifact_selector_renders_for_everyone_readonly() -> None:
    src = _src("artifact-store-selector.jsx")
    # The old gate (a non-admin got null, and so no request) is gone.
    assert "if (!con || con.role !== \"admin\") return null" not in src
    assert NOTE in src
    assert 'data-testid="as-active-store-readonly-note"' in src
    # The selector itself no longer decides; the field reads the role.
    m = re.search(r"function AS_ActiveStoreSelector\(\) \{(.*?)\n\}", src, re.S)
    assert m is not None
    assert "NV_useConsole" not in m.group(1), "the gate moved into the field, where a request is already made"
    # A non-admin's select is natively disabled (unlike a save in flight,
    # which must keep its focus on the control).
    assert "disabled={readOnly || undefined}" in src
    assert "if (readOnly) return undefined" in src  # the change handler never fires a write


# ---------------------------------------------------------------------------
# Provider catalog
# ---------------------------------------------------------------------------


def test_the_catalog_gates_its_write_affordances() -> None:
    src = _src("provider-catalog.jsx")
    assert NOTE in src
    assert "con.role !== \"admin\"" in src
    # "+ Register" stays visible and says why it takes no click.
    assert "disabled: readOnly || undefined" in src
    # A card's menu (Delete) and its open (edit) do not exist for a
    # non-admin: no affordance that can only be refused.
    assert "const menu = isBuiltin || ctx.readOnly ? null" in src
    assert "if (!ctx.readOnly) ctx.onOpen(klass, row)" in src


# ---------------------------------------------------------------------------
# The kit the pages lean on
# ---------------------------------------------------------------------------


def test_the_page_header_action_supports_disabled_and_title() -> None:
    kit = _kit()
    m = re.search(r"\{action \? \(\s*<button type=\"button\" className=\"pk-action\"(.*?)\{action\.label\}</button>", kit, re.S)
    assert m is not None, "the page header's action button"
    assert "disabled={action.disabled || undefined}" in m.group(1)
    assert "title={action.title || undefined}" in m.group(1)
