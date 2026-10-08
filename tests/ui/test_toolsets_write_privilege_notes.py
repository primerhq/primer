"""The new-toolset modal states the python admin rule and the re-enter-secrets rule (security sweep AUTHZ-01, SSRF-02, SEC-02).

The console has no notion of the signed-in role, so it cannot hide the python provider; the server answers 403 on save
(``tests/api/test_toolset_write_privilege.py``). Following the documented-backend-rule convention (docs/dev/subsystems/ui-pages.md),
a labelled helper line on the owning form states each rule before the operator fills the form in.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "toolsets.jsx"


def test_the_python_provider_states_the_admin_rule() -> None:
    src = SRC.read_text(encoding="utf-8")
    note = src.index('data-testid="toolset-python-admin-only"')
    guard = src.rindex('provider === "python" && (', 0, note)
    assert src.index('<FormField label="Provider"') < guard < note, "the note must render only for python"
    assert "needs the admin role" in src[note:note + 400]


def test_the_url_field_says_to_re_enter_the_secrets_when_it_changes() -> None:
    src = SRC.read_text(encoding="utf-8")
    note = src.index('data-testid="toolset-url-secrets-reenter"')
    url_label = src.index('<FormField label="URL"')
    headers = src.index('label="Headers"')
    assert url_label < note < headers, "the note must sit under the URL field, above the headers"
    assert "isEdit && (" in src[url_label:note], "the note is about editing a stored toolset"
    text = src[note:headers]
    assert "Re-enter the header values" in text
    assert "OAuth client secret" in text and "OAuth endpoints" in text, "the OAuth secret and endpoints move the same way"
