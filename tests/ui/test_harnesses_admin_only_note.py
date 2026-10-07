"""The register-harness wizard says that harness writes need the admin role (security review 2026-10-08, AUTHZ-03).

The console has no notion of the signed-in role, so it cannot hide the wizard; the server answers 403 forbidden_role
(``tests/api/test_harness_admin_and_git_args.py``) and the wizard's fetch banner and the detail page's action banners render
that message. The documented-backend-rule convention (docs/dev/subsystems/ui-pages.md) is a labelled helper line on the owning
form, so an operator reads the rule before filling the form in.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "harnesses.jsx"


def test_the_register_wizard_states_the_admin_rule_in_step_one() -> None:
    src = SRC.read_text(encoding="utf-8")
    token_field = src.index('htmlFor="hr-token"')
    note = src.index('data-testid="harness-admin-only"')
    fetch_banner = src.index('title="Fetch failed"')
    assert token_field < note < fetch_banner, "the note must sit in step 1, under the token field, above the fetch banner"
    assert "needs the admin role" in src[note:fetch_banner]
