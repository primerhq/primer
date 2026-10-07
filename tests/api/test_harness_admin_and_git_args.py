"""Harness writes are admin-only, and git_url / ref are validated at the API (security review 2026-10-08).

AUTHZ-03 / FS-01: installing a harness writes arbitrary entities (a stdio MCP toolset included) straight to storage, so every
harness write and action is an admin function. AUTHZ-04 / INJ-03 / FS-02: git_url and ref reached the git CLI unvalidated, so
``--upload-pack=...``, ``ext::`` and ``file://`` gave a caller code execution on the worker. SEC-03: changing git_url kept the
stored git_token, so the next fetch sent the token to the new host.

The reads (list, get, the bundle download) stay user-tier; git_token is masked on every read.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from primer.model.harness import Harness, HarnessDirection, HarnessStatus
from primer.model.storage import OffsetPage
from tests.api.test_require_user_admin import _login, _seed

pytestmark = pytest.mark.asyncio

_OK_URL = "https://github.com/example/repo"


async def _put_harness(app, *, hid: str = "hns_sec00000001", direction=HarnessDirection.INBOUND, **kw) -> Harness:
    fields = dict(
        id=hid,
        slug="sec-harness",
        name="Sec",
        git_url=_OK_URL,
        git_token=SecretStr("admin-secret-token"),
        status=HarnessStatus.READY,
        direction=direction,
        overrides_schema={"type": "object", "properties": {}},
        available_bundle_hash="bh",
        created_at=datetime.now(timezone.utc),
    )
    fields.update(kw)
    harness = Harness(**fields)
    await app.state.storage_provider.get_storage(Harness).create(harness)
    return harness


def _mutations(hid: str) -> list[tuple[str, str, dict | None]]:
    return [
        ("POST", "/v1/harnesses", {"name": "x", "slug": "new-harness", "git_url": _OK_URL}),
        ("PUT", f"/v1/harnesses/{hid}", {"description": "changed"}),
        ("PUT", f"/v1/harnesses/{hid}/overrides", {}),
        ("PUT", f"/v1/harnesses/{hid}/tracked_entities", {"tracked_entities": []}),
        ("POST", f"/v1/harnesses/{hid}/fetch", None),
        ("POST", f"/v1/harnesses/{hid}/install", None),
        ("POST", f"/v1/harnesses/{hid}/sync", None),
        ("POST", f"/v1/harnesses/{hid}/build", None),
        ("POST", f"/v1/harnesses/{hid}/push", None),
        ("DELETE", f"/v1/harnesses/{hid}", None),
    ]


# ---- AUTHZ-03 / FS-01: every harness write and action is admin-only ----------------------------------------------------


@pytest.mark.parametrize("index", range(10))
async def test_a_user_is_refused_every_harness_write(raw_client, app, index):
    before = await _put_harness(app)
    await _seed(app, uid="u-user", username="user1", role="user")
    await _login(raw_client, "user1")

    method, path, body = _mutations(before.id)[index]
    resp = await raw_client.request(method, path, json=body)

    assert resp.status_code == 403, (method, path, resp.text)
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "forbidden_role" in resp.text
    after = await app.state.storage_provider.get_storage(Harness).get(before.id)
    assert after.model_dump() == before.model_dump(), "a refused call changed the harness"
    rows = await app.state.storage_provider.get_storage(Harness).list(OffsetPage(offset=0, length=50))
    assert [h.id for h in rows.items] == [before.id], "a refused create wrote a row"


async def test_an_admin_may_create_update_and_fetch_a_harness(raw_client, app):
    await _seed(app, uid="u-admin", username="admin1", role="admin")
    await _login(raw_client, "admin1")

    created = await raw_client.post(
        "/v1/harnesses", json={"name": "A", "slug": "admin-harness", "git_url": _OK_URL, "git_token": "tok"},
    )
    assert created.status_code == 201, created.text
    hid = created.json()["id"]
    assert (await raw_client.put(f"/v1/harnesses/{hid}", json={"description": "d"})).status_code == 200
    assert (await raw_client.post(f"/v1/harnesses/{hid}/fetch")).status_code == 202


async def test_a_user_may_still_read_harnesses_and_the_token_stays_masked(raw_client, app):
    harness = await _put_harness(app)
    await _seed(app, uid="u-user", username="user1", role="user")
    await _login(raw_client, "user1")

    listed = await raw_client.get("/v1/harnesses")
    one = await raw_client.get(f"/v1/harnesses/{harness.id}")

    assert listed.status_code == 200 and one.status_code == 200
    assert one.json()["git_token"] == "**********"
    assert listed.json()["items"][0]["git_token"] == "**********"
    assert "admin-secret-token" not in listed.text + one.text


# ---- AUTHZ-04 / INJ-03 / FS-02: git_url and ref are validated at the API ------------------------------------------------


_BAD_URLS = [
    "--upload-pack=touch /tmp/pwned",
    "-oProxyCommand=touch /tmp/pwned",
    "ext::sh -c id",
    "file:///etc",
    "/etc/passwd",
    "http://example.com/repo",
    "https://",
    "https:///no-host",
    "git://example.com/repo",
    "https://example.com/repo with space",
]

_BAD_REFS = ["--output=x", "-b", "main..evil", "main;id", "refs/heads/../x", "@{-1}", "has space"]


@pytest.mark.parametrize("git_url", _BAD_URLS)
async def test_create_refuses_an_unsafe_git_url(client, git_url):
    resp = await client.post("/v1/harnesses", json={"name": "x", "slug": "bad-url", "git_url": git_url})
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("ref", _BAD_REFS)
async def test_create_refuses_an_unsafe_ref(client, ref):
    resp = await client.post(
        "/v1/harnesses", json={"name": "x", "slug": "bad-ref", "git_url": _OK_URL, "ref": ref},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("git_url", _BAD_URLS)
async def test_update_refuses_an_unsafe_git_url(client, app, git_url):
    harness = await _put_harness(app, git_token=None)
    resp = await client.put(f"/v1/harnesses/{harness.id}", json={"git_url": git_url})
    assert resp.status_code == 422, resp.text
    stored = await app.state.storage_provider.get_storage(Harness).get(harness.id)
    assert stored.git_url == _OK_URL


@pytest.mark.parametrize("ref", _BAD_REFS)
async def test_update_refuses_an_unsafe_ref(client, app, ref):
    harness = await _put_harness(app)
    resp = await client.put(f"/v1/harnesses/{harness.id}", json={"ref": ref})
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    "ref", ["main", "v1.2.3", "release/2026-10", "feature_x", "0123456789abcdef0123456789abcdef01234567"],
)
async def test_ordinary_refs_still_pass(client, ref):
    resp = await client.post(
        "/v1/harnesses", json={"name": "x", "slug": "good-ref", "git_url": _OK_URL, "ref": ref},
    )
    assert resp.status_code == 201, resp.text


# ---- SEC-03: a new git_url never inherits the stored token -------------------------------------------------------------


async def test_changing_git_url_without_a_new_token_is_refused(client, app):
    harness = await _put_harness(app)
    resp = await client.put(f"/v1/harnesses/{harness.id}", json={"git_url": "https://evil.example/repo"})
    assert resp.status_code == 422, resp.text
    assert "git_token" in resp.text
    stored = await app.state.storage_provider.get_storage(Harness).get(harness.id)
    assert stored.git_url == _OK_URL
    assert stored.git_token.get_secret_value() == "admin-secret-token"


async def test_changing_git_url_with_the_masked_token_is_refused(client, app):
    harness = await _put_harness(app)
    resp = await client.put(
        f"/v1/harnesses/{harness.id}", json={"git_url": "https://evil.example/repo", "git_token": "**********"},
    )
    assert resp.status_code == 422, resp.text
    stored = await app.state.storage_provider.get_storage(Harness).get(harness.id)
    assert stored.git_url == _OK_URL


async def test_changing_git_url_with_a_new_token_stores_both(client, app):
    harness = await _put_harness(app)
    resp = await client.put(
        f"/v1/harnesses/{harness.id}", json={"git_url": "https://other.example/repo", "git_token": "new-token"},
    )
    assert resp.status_code == 200, resp.text
    stored = await app.state.storage_provider.get_storage(Harness).get(harness.id)
    assert stored.git_url == "https://other.example/repo"
    assert stored.git_token.get_secret_value() == "new-token"


async def test_changing_git_url_with_an_empty_token_clears_it(client, app):
    harness = await _put_harness(app)
    resp = await client.put(
        f"/v1/harnesses/{harness.id}", json={"git_url": "https://other.example/repo", "git_token": ""},
    )
    assert resp.status_code == 200, resp.text
    stored = await app.state.storage_provider.get_storage(Harness).get(harness.id)
    assert stored.git_url == "https://other.example/repo"
    assert stored.git_token is None


async def test_the_masked_token_round_tripped_without_a_url_change_keeps_the_real_one(client, app):
    harness = await _put_harness(app)
    resp = await client.put(
        f"/v1/harnesses/{harness.id}", json={"description": "x", "git_token": "**********"},
    )
    assert resp.status_code == 200, resp.text
    stored = await app.state.storage_provider.get_storage(Harness).get(harness.id)
    assert stored.git_token.get_secret_value() == "admin-secret-token"


async def test_changing_git_url_on_a_harness_with_no_token_needs_nothing(client, app):
    harness = await _put_harness(app, git_token=None)
    resp = await client.put(f"/v1/harnesses/{harness.id}", json={"git_url": "https://other.example/repo"})
    assert resp.status_code == 200, resp.text


# ---- the install / sync request records who asked, so the worker can apply the toolset admin rule -------------------


async def test_install_records_the_requesting_admin(raw_client, app):
    harness = await _put_harness(app)
    await _seed(app, uid="u-admin", username="admin1", role="admin")
    await _login(raw_client, "admin1")

    resp = await raw_client.post(f"/v1/harnesses/{harness.id}/install")

    assert resp.status_code == 202, resp.text
    stored = await app.state.storage_provider.get_storage(Harness).get(harness.id)
    assert stored.operation_requested_by is not None
    assert stored.operation_requested_by.role == "admin"
    assert stored.operation_requested_by.id == "u-admin"
