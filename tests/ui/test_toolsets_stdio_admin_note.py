"""The new-toolset modal says that a stdio toolset needs the admin role (architecture review A-02).

The console has no notion of the signed-in role, so it cannot hide the stdio chip; the server answers 403 on save
(``tests/api/test_toolset_stdio_admin.py``). The documented-backend-rule convention (docs/dev/subsystems/ui-pages.md) is a labelled
helper line on the owning form, so an operator reads the rule before they fill the command in.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "toolsets.jsx"


def test_the_stdio_form_states_the_admin_rule_beside_the_command_field() -> None:
    src = SRC.read_text(encoding="utf-8")
    note = src.index('data-testid="toolset-stdio-admin-only"')
    command_label = src.index('<FormField label="Command"')
    environment = src.index('label="Environment"')
    assert command_label < note < environment, "the note must sit inside the stdio branch, under the command field"
    assert "needs the admin role" in src[note:environment]
