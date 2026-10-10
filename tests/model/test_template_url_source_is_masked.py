"""A workspace template's ``kind=url`` file source is served without its password and kept through a PUT (ticket 01a11d32, the first family after 01a11cdf part 3).

``_UrlSource.url`` may carry ``user:password@`` (a private file host): the platform fetches it when it materialises a workspace and aiohttp sends the userinfo as Basic auth. The template
routes are ``user_dep``, so every user used to read that password in clear in ``GET``/list, in the ``POST``/``PUT`` responses, in CRUD events and in ``get_workspace_template`` results.
It is a ``MaskedUserinfoUrl`` now, the type the provider Base URL uses: the JSON-mode dump masks the password (``https://reader:**********@host/seed``; a lone ``https://TOKEN@host`` whole), a
python-mode dump and the object keep the real URL (the fetch reads it), ``dump_for_storage`` keeps it for the stored row, and ``preserve_masked_secrets`` puts the stored password back for the
same scheme, host, port and user only.
"""

from __future__ import annotations

import warnings

import pytest

from primer.model.common import dump_for_storage, preserve_masked_secrets
from primer.model.except_ import ValidationError
from primer.model.workspace import FileMount, WorkspaceTemplate, WorkspaceTemplateOverrides

MASK = "**********"
URL = "https://reader:s3cr3t@files.example.com/seed.txt"
MASKED = f"https://reader:{MASK}@files.example.com/seed.txt"
TOKEN_URL = "https://ghp_abcdefghij@files.example.com/seed.txt"


def _file(url: str, path: str = "seed.txt") -> dict:
    return {"path": path, "source": {"kind": "url", "url": url}}


def _template(*urls: str, row_id: str = "tpl-a") -> WorkspaceTemplate:
    return WorkspaceTemplate.model_validate({
        "id": row_id, "provider_id": "p-loc", "description": "d",
        "files": [_file(url, f"f{i}.txt") for i, url in enumerate(urls)],
    })


def _served(template: WorkspaceTemplate) -> WorkspaceTemplate:
    """What a client that reads the row and writes it back sends: the JSON-mode (served) form, validated again."""
    return WorkspaceTemplate.model_validate(template.model_dump(mode="json"))


def _url(template: WorkspaceTemplate, index: int = 0) -> str:
    return str(template.files[index].source.url)


# ---- the dumps ---------------------------------------------------------------------------------------------------------------------------------------------------------


def test_the_json_dumps_mask_the_password_and_the_other_forms_keep_the_url() -> None:
    row = _template(URL)

    assert row.model_dump(mode="json")["files"][0]["source"]["url"] == MASKED
    assert MASKED in row.model_dump_json() and "s3cr3t" not in row.model_dump_json()
    assert _url(row) == URL, "the object keeps the real URL: the fetch reads it"
    assert str(row.model_dump()["files"][0]["source"]["url"]) == URL, "a python-mode dump keeps it too"
    assert dump_for_storage(row)["files"][0]["source"]["url"] == URL, "the stored row keeps it"


def test_a_lone_token_in_the_username_slot_is_masked_whole() -> None:
    row = _template(TOKEN_URL)

    assert row.model_dump(mode="json")["files"][0]["source"]["url"] == f"https://{MASK}@files.example.com/seed.txt"
    assert "ghp_abcdefghij" not in row.model_dump_json()


def test_a_url_without_userinfo_and_the_other_source_kinds_are_served_as_they_are() -> None:
    row = WorkspaceTemplate.model_validate({
        "id": "tpl-a", "provider_id": "p", "description": "d",
        "files": [_file("https://files.example.com/a.txt"), {"path": "b", "source": {"kind": "inline", "content": "x"}}],
    })

    dumped = row.model_dump(mode="json")["files"]

    assert dumped[0]["source"]["url"] == "https://files.example.com/a.txt" and dumped[1]["source"]["content"] == "x"


def test_the_overrides_of_a_workspace_create_serve_masked_too() -> None:
    overrides = WorkspaceTemplateOverrides.model_validate({"files": [_file(URL)]})

    assert overrides.model_dump(mode="json")["files"][0]["source"]["url"] == MASKED
    assert str(overrides.files[0].source.url) == URL


def test_the_storage_form_changes_when_only_the_password_does_and_the_wire_form_does_not() -> None:
    a, b = _template(URL), _template("https://reader:other@files.example.com/seed.txt")

    assert a.model_dump(mode="json") == b.model_dump(mode="json")
    assert dump_for_storage(a) != dump_for_storage(b), "anything that fingerprints a template must use the storage form"


@pytest.mark.filterwarnings("error")
def test_no_dump_of_a_template_warns() -> None:
    row = _template(URL, TOKEN_URL)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        row.model_dump()
        row.model_dump(mode="json")
        row.model_dump_json()
        dump_for_storage(row)
        FileMount.model_json_schema()


# ---- a PUT of the served body -------------------------------------------------------------------------------------------------------------------------------------------


def test_the_served_body_sent_back_unchanged_keeps_the_stored_password() -> None:
    stored = _template(URL)
    incoming = _served(stored)
    assert _url(incoming) == MASKED, "the premise: the served form carries the mask"

    preserve_masked_secrets(incoming, stored)

    assert _url(incoming) == URL


def test_a_changed_path_on_the_same_origin_keeps_the_password() -> None:
    stored = _template(URL)
    incoming = _template(f"https://reader:{MASK}@files.example.com/v2/seed.txt")

    preserve_masked_secrets(incoming, stored)

    assert _url(incoming) == "https://reader:s3cr3t@files.example.com/v2/seed.txt"


@pytest.mark.parametrize(
    "moved",
    [
        pytest.param(f"https://reader:{MASK}@attacker.example/seed.txt", id="another host"),
        pytest.param(f"https://other:{MASK}@files.example.com/seed.txt", id="another username"),
        pytest.param(f"http://reader:{MASK}@files.example.com/seed.txt", id="another scheme"),
        pytest.param(f"https://reader:{MASK}@files.example.com:8443/seed.txt", id="another port"),
    ],
)
def test_a_mask_that_cannot_be_restored_is_refused_and_never_stored(moved: str) -> None:
    stored = _template(URL)
    incoming = _template(moved)

    with pytest.raises(ValidationError, match="re-enter the password") as caught:
        preserve_masked_secrets(incoming, stored)

    assert "s3cr3t" not in str(caught.value)


def test_a_new_password_is_stored_and_a_removed_credential_is_removed() -> None:
    stored = _template(URL)
    changed, removed = _template("https://reader:newpass@files.example.com/seed.txt"), _template("https://files.example.com/seed.txt")

    preserve_masked_secrets(changed, stored)
    preserve_masked_secrets(removed, stored)

    assert _url(changed) == "https://reader:newpass@files.example.com/seed.txt" and _url(removed) == "https://files.example.com/seed.txt"


def test_each_file_gets_its_own_stored_password_back() -> None:
    stored = _template(URL, "https://other:second@mirror.example.org/b.txt")
    incoming = _served(stored)

    preserve_masked_secrets(incoming, stored)

    assert [_url(incoming, 0), _url(incoming, 1)] == [URL, "https://other:second@mirror.example.org/b.txt"]


def test_a_list_whose_length_changed_does_not_get_a_password_by_position() -> None:
    stored = _template(URL)
    incoming = _template(MASKED, "https://files.example.com/extra.txt")

    with pytest.raises(ValidationError, match="re-enter the password"):
        preserve_masked_secrets(incoming, stored)


def test_a_file_that_was_not_a_url_source_gets_no_password() -> None:
    stored = WorkspaceTemplate.model_validate({
        "id": "tpl-a", "provider_id": "p", "description": "d", "files": [{"path": "f0.txt", "source": {"kind": "inline", "content": "x"}}],
    })
    incoming = _template(MASKED)

    with pytest.raises(ValidationError, match="re-enter the password"):
        preserve_masked_secrets(incoming, stored)


# ---- the real SQLite store -------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_credential_survives_a_real_sqlite_round_trip(tmp_path) -> None:
    """The storage dump must keep the real URL: a row written through the masked JSON form would read back with the mask as its password."""
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    try:
        storage = sp.get_storage(WorkspaceTemplate)
        await storage.create(_template(URL, TOKEN_URL))

        row = await storage.get("tpl-a")

        assert [_url(row, 0), _url(row, 1)] == [URL, TOKEN_URL]
        assert row.model_dump(mode="json")["files"][0]["source"]["url"] == MASKED
    finally:
        await sp.aclose()
