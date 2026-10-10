"""``restore_template_secrets``: what a served-body PUT gets back of a template's secrets, by caller (ticket 01a11d32, round 2 of #721).

The url file sources are matched by their PATH (not by position), a NON-ADMIN caller gets a password back only when the WHOLE URL is unchanged, an admin keeps the origin rule (same scheme, host, port
and user), and every other served mask of the template (``env``) is restored by key, as ``preserve_masked_secrets`` does for the rest of a row.
"""

from __future__ import annotations

import pytest

from primer.model.except_ import ValidationError
from primer.model.workspace import WorkspaceTemplate
from primer.workspace.template_secrets import restore_template_secrets

MASK = "**********"
A = "https://reader:s3cr3t@files.example.com/a.txt"
B = "https://second:hunter2@mirror.example.org/b.txt"


def _template(files: list[tuple[str, str]], env: dict | None = None) -> WorkspaceTemplate:
    return WorkspaceTemplate.model_validate({
        "id": "tpl-a", "provider_id": "p", "description": "d", "env": env or {},
        "files": [{"path": path, "source": {"kind": "url", "url": url}} for path, url in files],
    })


def _served(template: WorkspaceTemplate) -> WorkspaceTemplate:
    return WorkspaceTemplate.model_validate(template.model_dump(mode="json"))


def _urls(template: WorkspaceTemplate) -> dict[str, str]:
    return {fm.path: str(fm.source.url) for fm in template.files}


@pytest.mark.parametrize("admin", [True, False], ids=["an admin", "a user"])
def test_the_whole_served_body_gets_every_password_back(admin: bool) -> None:
    stored = _template([("a.txt", A), ("b.txt", B)], env={"API_TOKEN": "tok-0123456789"})
    incoming = _served(stored)

    restore_template_secrets(incoming, stored, admin=admin)

    assert _urls(incoming) == {"a.txt": A, "b.txt": B} and incoming.env["API_TOKEN"].get_secret_value() == "tok-0123456789"


@pytest.mark.parametrize("admin", [True, False], ids=["an admin", "a user"])
def test_a_reorder_keeps_each_password_because_the_files_are_matched_by_path(admin: bool) -> None:
    stored = _template([("a.txt", A), ("b.txt", B)])
    incoming = _served(stored)
    incoming.files.reverse()

    restore_template_secrets(incoming, stored, admin=admin)

    assert _urls(incoming) == {"b.txt": B, "a.txt": A}


def test_an_admin_may_change_the_path_of_the_url_on_the_same_origin_and_a_user_may_not() -> None:
    stored = _template([("a.txt", A)])
    changed = f"https://reader:{MASK}@files.example.com/elsewhere.txt"

    admin_body = _template([("a.txt", changed)])
    restore_template_secrets(admin_body, stored, admin=True)
    assert _urls(admin_body) == {"a.txt": "https://reader:s3cr3t@files.example.com/elsewhere.txt"}

    user_body = _template([("a.txt", changed)])
    with pytest.raises(ValidationError, match="re-enter the password") as caught:
        restore_template_secrets(user_body, stored, admin=False)
    assert "s3cr3t" not in str(caught.value)


@pytest.mark.parametrize("admin", [True, False], ids=["an admin", "a user"])
@pytest.mark.parametrize(
    "moved",
    [
        pytest.param(f"https://reader:{MASK}@attacker.example/a.txt", id="another host"),
        pytest.param(f"https://other:{MASK}@files.example.com/a.txt", id="another user"),
        pytest.param(f"http://reader:{MASK}@files.example.com/a.txt", id="another scheme"),
    ],
)
def test_a_mask_for_another_origin_is_refused_for_everyone(admin: bool, moved: str) -> None:
    with pytest.raises(ValidationError, match="re-enter the password") as caught:
        restore_template_secrets(_template([("a.txt", moved)]), _template([("a.txt", A)]), admin=admin)

    assert "s3cr3t" not in str(caught.value)


@pytest.mark.parametrize("admin", [True, False], ids=["an admin", "a user"])
def test_a_mask_under_a_path_the_template_did_not_hold_is_refused(admin: bool) -> None:
    stored = _template([("a.txt", A)])
    incoming = _template([("a.txt", f"https://reader:{MASK}@files.example.com/a.txt"), ("new.txt", f"https://reader:{MASK}@files.example.com/new.txt")])

    with pytest.raises(ValidationError, match="re-enter the password"):
        restore_template_secrets(incoming, stored, admin=admin)


@pytest.mark.parametrize("admin", [True, False], ids=["an admin", "a user"])
def test_a_mask_when_the_stored_file_at_that_path_is_not_a_url_source_is_refused(admin: bool) -> None:
    stored = WorkspaceTemplate.model_validate({"id": "t", "provider_id": "p", "description": "d", "files": [{"path": "a.txt", "source": {"kind": "inline", "content": "x"}}]})

    with pytest.raises(ValidationError, match="re-enter the password"):
        restore_template_secrets(_template([("a.txt", f"https://reader:{MASK}@files.example.com/a.txt")]), stored, admin=admin)


@pytest.mark.parametrize("admin", [True, False], ids=["an admin", "a user"])
def test_a_password_the_person_typed_is_theirs_and_a_removed_file_is_removed(admin: bool) -> None:
    stored = _template([("a.txt", A), ("b.txt", B)])
    incoming = _template([("a.txt", "https://reader:typed@files.example.com/a.txt")])

    restore_template_secrets(incoming, stored, admin=admin)

    assert _urls(incoming) == {"a.txt": "https://reader:typed@files.example.com/a.txt"}
