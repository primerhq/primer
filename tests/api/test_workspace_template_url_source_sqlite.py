"""A template's url file source over REST with a REAL SQLite store (ticket 01a11d32, round 2 of #721).

The in-memory REST tests cannot see a regression of the storage dump (a row written through the masked JSON form would read back with the mask as its password). Here the app runs on
``SqliteStorageProvider``: a credentialed template is created, served masked, kept through a PUT of the served body (the password and the ``env`` value read back from SQLite), and refused (422, row
untouched) when the mask is sent back for another host or under a renamed path.
"""

from __future__ import annotations

import pytest

from primer.model.workspace import WorkspaceTemplate
from tests.api.test_provider_url_userinfo_sqlite import client, sp  # noqa: F401  (fixtures: an app on a real SQLite store)

MASK = "**********"
URL = "https://reader:s3cr3t@files.example.com/seed.txt"
BODY = {
    "id": "tpl-a", "provider_id": "p-loc", "description": "d", "env": {"API_TOKEN": "tok-0123456789"},
    "files": [{"path": "seed.txt", "source": {"kind": "url", "url": URL}}, {"path": "b.txt", "source": {"kind": "url", "url": "https://t0ken-only@mirror.example.org/b.txt"}}],
}


@pytest.mark.asyncio
async def test_the_template_is_served_masked_stored_real_and_kept_through_a_put(client, sp) -> None:
    created = await client.post("/v1/workspace_templates", json=BODY)
    assert created.status_code in (200, 201), created.text

    served = (await client.get("/v1/workspace_templates/tpl-a")).json()
    assert served["files"][0]["source"]["url"] == f"https://reader:{MASK}@files.example.com/seed.txt"
    assert served["files"][1]["source"]["url"] == f"https://{MASK}@mirror.example.org/b.txt"
    assert "s3cr3t" not in str(served) and "t0ken-only" not in str(served) and "tok-0123456789" not in str(served)
    row = await sp.get_storage(WorkspaceTemplate).get("tpl-a")
    assert str(row.files[0].source.url) == URL and str(row.files[1].source.url) == "https://t0ken-only@mirror.example.org/b.txt", "SQLite holds the real URLs"

    served["description"] = "edited"
    put = await client.put("/v1/workspace_templates/tpl-a", json=served)

    assert put.status_code == 200, put.text
    row = await sp.get_storage(WorkspaceTemplate).get("tpl-a")
    assert row.description == "edited" and str(row.files[0].source.url) == URL, "read back from SQLite after the PUT"
    assert str(row.files[1].source.url) == "https://t0ken-only@mirror.example.org/b.txt" and row.env["API_TOKEN"].get_secret_value() == "tok-0123456789"


@pytest.mark.asyncio
@pytest.mark.parametrize("what", ["another host", "a renamed path"])
async def test_a_mask_that_cannot_be_restored_is_refused_and_the_row_is_untouched(client, sp, what: str) -> None:
    await client.post("/v1/workspace_templates", json=BODY)
    served = (await client.get("/v1/workspace_templates/tpl-a")).json()
    if what == "another host":
        served["files"][0]["source"]["url"] = f"https://reader:{MASK}@attacker.example/seed.txt"
    else:
        served["files"][0]["path"] = "renamed.txt"

    r = await client.put("/v1/workspace_templates/tpl-a", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the password" in r.text and "s3cr3t" not in r.text
    row = await sp.get_storage(WorkspaceTemplate).get("tpl-a")
    assert str(row.files[0].source.url) == URL and row.files[0].path == "seed.txt"
