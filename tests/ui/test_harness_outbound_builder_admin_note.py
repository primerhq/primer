"""The outbound harness builder says its writes need the admin role (security review 2026-10-08, AUTHZ-03).

The builder replaces tracked_entities and enqueues build and push, all admin-only now; a lower role gets 403 forbidden_role.
The console has no notion of the signed-in role, so the documented-backend-rule convention applies: a labelled helper line on
the owning form, read before the operator fills the wizard in.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "harness_outbound_builder.jsx"


def test_the_outbound_builder_states_the_admin_rule() -> None:
    src = SRC.read_text(encoding="utf-8")
    note = src.index('data-testid="harness-outbound-admin-only"')
    assert "needs the admin role" in src[note:note + 600]
