"""What a role=user caller may do with a template's masked url file source, and how the files are matched (ticket 01a11d32, round 2 of #721).

The restore is the admin's credential handed back to whoever sends the served body. The lead's ruling on the round 1 review: a NON-ADMIN caller gets the stored password back only when the WHOLE URL is
unchanged (any other change that still carries the mask is a 422, "re-enter the password"), because a user who may edit a template that holds no admin-only setting could otherwise re-aim the admin's
credential at ANY path on the same origin and read the answer through a workspace; an admin keeps the origin rule (the same scheme, host, port and user). The files are matched by their PATH, not by their
position: a reorder keeps every password, and a mask on a path the stored template did not hold is a 422. The 403 for a template that holds an admin-only setting comes BEFORE any restore. A served-body
PUT also keeps the template's ``env`` values (they are ``SecretStr`` and are restored by key).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport

from primer.auth.passwords import hash_password
from primer.model.user import User
from primer.model.workspace import WorkspaceTemplate
from tests.api.conftest import app, fake_provider_registry  # noqa: F401

MASK = "**********"
URL = "https://reader:s3cr3t@files.example.com/seed.txt"
MASKED = f"https://reader:{MASK}@files.example.com/seed.txt"
OTHER = "https://second:hunter2@mirror.example.org/b.txt"
SECRET_FILE = {"path": "creds", "source": {"kind": "secret", "name": "OPENAI_API_KEY"}}


@pytest.fixture
async def admin(app) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/auth/register", json={"username": "roleadmin", "password": "roleadminpass1"})
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture
async def user(app, admin) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    await app.state.storage_provider.get_storage(User).create(
        User(id="user-role", username="roleuser", password_hash=await hash_password("roleuserpass1"), created_at=datetime.now(timezone.utc), role="user")
    )
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/auth/login", json={"username": "roleuser", "password": "roleuserpass1"})
        assert r.status_code == 200, r.text
        yield c


def _url_file(url: str, path: str = "seed.txt") -> dict:
    return {"path": path, "source": {"kind": "url", "url": url}}


def _template(files: list[dict], *, env: dict | None = None, row_id: str = "tpl-a") -> dict:
    return {"id": row_id, "provider_id": "p-loc", "description": "d", "files": files, **({"env": env} if env else {})}


async def _stored(app, row_id: str = "tpl-a") -> WorkspaceTemplate:
    return await app.state.storage_provider.get_storage(WorkspaceTemplate).get(row_id)


def _urls(row: WorkspaceTemplate) -> dict[str, str]:
    return {fm.path: str(fm.source.url) for fm in row.files if getattr(fm.source, "kind", None) == "url"}


# ---- a role=user caller: the whole URL unchanged, or a 422 -----------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_user_who_sends_the_served_body_back_unchanged_keeps_the_stored_password(app, admin, user) -> None:
    await admin.post("/v1/workspace_templates", json=_template([_url_file(URL)]))
    served = (await user.get("/v1/workspace_templates/tpl-a")).json()
    served["description"] = "edited by a user"

    r = await user.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 200, r.text
    assert _urls(await _stored(app)) == {"seed.txt": URL} and (await _stored(app)).description == "edited by a user"
    assert "s3cr3t" not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(f"https://reader:{MASK}@files.example.com/other-path.txt", id="another path on the same origin"),
        pytest.param(f"https://reader:{MASK}@files.example.com/seed.txt?x=1", id="a query on the same origin"),
        pytest.param(f"https://reader:{MASK}@attacker.example/seed.txt", id="another host"),
    ],
)
async def test_a_user_who_changes_anything_but_the_password_gets_a_422_and_the_row_is_untouched(app, admin, user, changed: str) -> None:
    await admin.post("/v1/workspace_templates", json=_template([_url_file(URL)]))
    served = (await user.get("/v1/workspace_templates/tpl-a")).json()
    served["files"][0]["source"]["url"] = changed

    r = await user.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the password" in r.text and "s3cr3t" not in r.text
    assert _urls(await _stored(app)) == {"seed.txt": URL}


@pytest.mark.asyncio
async def test_a_user_who_types_a_new_password_for_another_path_stores_what_was_typed(app, admin, user) -> None:
    await admin.post("/v1/workspace_templates", json=_template([_url_file(URL)]))
    served = (await user.get("/v1/workspace_templates/tpl-a")).json()
    served["files"][0]["source"]["url"] = "https://reader:typed@files.example.com/other-path.txt"

    r = await user.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 200, r.text
    assert _urls(await _stored(app)) == {"seed.txt": "https://reader:typed@files.example.com/other-path.txt"}


@pytest.mark.asyncio
async def test_an_admin_keeps_the_origin_rule_and_may_change_the_path(app, admin) -> None:
    await admin.post("/v1/workspace_templates", json=_template([_url_file(URL)]))
    served = (await admin.get("/v1/workspace_templates/tpl-a")).json()
    served["files"][0]["source"]["url"] = f"https://reader:{MASK}@files.example.com/other-path.txt"

    r = await admin.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 200, r.text
    assert _urls(await _stored(app)) == {"seed.txt": "https://reader:s3cr3t@files.example.com/other-path.txt"}


# ---- the 403 comes before any restore -------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_user_who_moves_a_mask_on_a_template_that_holds_an_admin_only_setting_gets_the_403_not_the_422(app, admin, user) -> None:
    """The template holds a ``kind=secret`` file source (admin-only): it is not user-editable at all. The refusal must be the 403, with nothing said about what a mask could be restored to."""
    created = await admin.post("/v1/workspace_templates", json=_template([_url_file(URL), SECRET_FILE]))
    assert created.status_code == 201, created.text
    served = (await user.get("/v1/workspace_templates/tpl-a")).json()
    served["files"][0]["source"]["url"] = f"https://reader:{MASK}@attacker.example/seed.txt"

    r = await user.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 403, r.text
    assert "re-enter" not in r.text and "s3cr3t" not in r.text
    assert _urls(await _stored(app)) == {"seed.txt": URL}


# ---- the files are matched by path ----------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reorder_of_the_files_keeps_each_password(app, admin) -> None:
    await admin.post("/v1/workspace_templates", json=_template([_url_file(URL, "a.txt"), _url_file(OTHER, "b.txt")]))
    served = (await admin.get("/v1/workspace_templates/tpl-a")).json()
    served["files"] = list(reversed(served["files"]))

    r = await admin.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 200, r.text
    assert _urls(await _stored(app)) == {"a.txt": URL, "b.txt": OTHER}


@pytest.mark.asyncio
async def test_a_file_added_with_the_mask_of_a_path_the_template_did_not_hold_is_a_422(app, admin) -> None:
    await admin.post("/v1/workspace_templates", json=_template([_url_file(URL)]))
    served = (await admin.get("/v1/workspace_templates/tpl-a")).json()
    served["files"].append({"path": "new.txt", "source": {"kind": "url", "url": f"https://reader:{MASK}@files.example.com/new.txt"}})

    r = await admin.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the password" in r.text
    assert _urls(await _stored(app)) == {"seed.txt": URL}


@pytest.mark.asyncio
async def test_a_file_removed_from_the_list_is_removed_and_the_others_keep_their_passwords(app, admin) -> None:
    await admin.post("/v1/workspace_templates", json=_template([_url_file(URL, "a.txt"), _url_file(OTHER, "b.txt")]))
    served = (await admin.get("/v1/workspace_templates/tpl-a")).json()
    served["files"] = [f for f in served["files"] if f["path"] == "b.txt"]

    r = await admin.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 200, r.text
    assert _urls(await _stored(app)) == {"b.txt": OTHER}


@pytest.mark.asyncio
async def test_a_mask_under_a_renamed_path_is_a_422(app, admin) -> None:
    await admin.post("/v1/workspace_templates", json=_template([_url_file(URL, "a.txt")]))
    served = (await admin.get("/v1/workspace_templates/tpl-a")).json()
    served["files"][0]["path"] = "renamed.txt"

    r = await admin.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 422, r.text
    assert _urls(await _stored(app)) == {"a.txt": URL}


# ---- env ------------------------------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_env_values_of_a_served_body_are_restored_too(app, admin) -> None:
    """``env`` is ``dict[str, SecretStr]``: a served-body PUT used to store the mask as the variable's value; it is restored by key now."""
    await admin.post("/v1/workspace_templates", json=_template([_url_file(URL)], env={"API_TOKEN": "tok-0123456789", "REGION": "eu"}))
    served = (await admin.get("/v1/workspace_templates/tpl-a")).json()
    assert served["env"]["API_TOKEN"].startswith(MASK)

    r = await admin.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 200, r.text
    row = await _stored(app)
    assert row.env["API_TOKEN"].get_secret_value() == "tok-0123456789" and row.env["REGION"].get_secret_value() == "eu"
