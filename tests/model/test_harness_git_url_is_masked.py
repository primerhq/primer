"""A harness ``git_url`` is served without the password of its userinfo (ticket 01a11d32, the harness family).

A PAT in a git URL is the classic case (``https://ghp_xxx@github.com/org/repo`` or ``https://user:pat@host/repo``). The harness reads (list, get, the POST/PUT responses, the tools) are
user-tier, so every user read it in clear next to a masked ``git_token``. ``Harness.git_url`` and ``ResolvedDependency.git_url`` are masked in the JSON-mode dump now (``mask_userinfo``: the
username stays only when a password is present, a lone userinfo is masked whole); the object, a python-mode dump and the storage dump keep the real URL (git reads it). An update that sends
the served URL back keeps the stored credential for the same scheme, host, port and user only (:func:`restore_served_git_url`), and a create that carries the served mask is refused.
"""

from __future__ import annotations

import warnings
from datetime import datetime, timezone

import pytest

from primer.model.common import dump_for_storage
from primer.model.harness import (
    GitUrlMaskRefused,
    Harness,
    ResolvedDependency,
    refuse_served_git_url,
    restore_served_git_url,
)

MASK = "**********"
URL = "https://reader:s3cr3t@git.example.com/org/repo.git"
MASKED = f"https://reader:{MASK}@git.example.com/org/repo.git"
TOKEN_URL = "https://ghp_abcdefghij@github.com/org/repo"


def _harness(git_url: str | None = URL) -> Harness:
    return Harness(id="hns_a", slug="my-harness", name="n", git_url=git_url, created_at=datetime.now(timezone.utc))


def _dependency(git_url: str = URL) -> ResolvedDependency:
    return ResolvedDependency(name="d", slug="dep", git_url=git_url, ref="main", resolved_commit="c" * 40, bundle_hash="b", depth=1)


# ---- the dumps ---------------------------------------------------------------------------------------------------------------------------------------------------------


def test_the_json_dumps_mask_the_password_and_the_other_forms_keep_the_url() -> None:
    row = _harness()

    assert row.model_dump(mode="json")["git_url"] == MASKED
    assert MASKED in row.model_dump_json() and "s3cr3t" not in row.model_dump_json()
    assert row.git_url == URL, "the object keeps the real URL: git reads it"
    assert row.model_dump()["git_url"] == URL, "a python-mode dump keeps it too"
    assert dump_for_storage(row)["git_url"] == URL, "the stored row keeps it"


def test_a_lone_token_in_the_username_slot_is_masked_whole() -> None:
    row = _harness(TOKEN_URL)

    assert row.model_dump(mode="json")["git_url"] == f"https://{MASK}@github.com/org/repo"
    assert "ghp_abcdefghij" not in row.model_dump_json()


def test_a_url_without_userinfo_and_a_missing_url_are_served_as_they_are() -> None:
    assert _harness("https://github.com/org/repo").model_dump(mode="json")["git_url"] == "https://github.com/org/repo"
    assert _harness(None).model_dump(mode="json")["git_url"] is None


def test_a_resolved_dependency_is_masked_too() -> None:
    dep = _dependency()

    assert dep.model_dump(mode="json")["git_url"] == MASKED and dep.git_url == URL


def test_the_harness_with_its_dependencies_serves_none_of_the_passwords() -> None:
    row = _harness()
    row.dependencies_resolved = [_dependency("https://u:dep-pass@git.example.org/dep")]

    assert "dep-pass" not in row.model_dump_json() and "s3cr3t" not in row.model_dump_json()


@pytest.mark.filterwarnings("error")
def test_no_dump_of_a_harness_warns() -> None:
    row = _harness()
    row.dependencies_resolved = [_dependency()]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        row.model_dump()
        row.model_dump(mode="json")
        row.model_dump_json()
        dump_for_storage(row)
        Harness.model_json_schema()


def test_the_storage_form_changes_when_only_the_password_does_and_the_wire_form_does_not() -> None:
    a, b = _harness(URL), _harness("https://reader:other@git.example.com/org/repo.git")

    assert a.model_dump(mode="json")["git_url"] == b.model_dump(mode="json")["git_url"]
    assert dump_for_storage(a)["git_url"] != dump_for_storage(b)["git_url"]


# ---- an update that sends the served url back -------------------------------------------------------------------------------------------------------------------------------


def test_the_served_url_sent_back_unchanged_keeps_the_stored_password() -> None:
    assert restore_served_git_url(MASKED, URL) == URL


def test_a_changed_path_on_the_same_origin_keeps_the_password() -> None:
    assert restore_served_git_url(f"https://reader:{MASK}@git.example.com/org/other.git", URL) == "https://reader:s3cr3t@git.example.com/org/other.git"


def test_a_lone_token_comes_back_when_the_mask_is_sent_back() -> None:
    assert restore_served_git_url(f"https://{MASK}@github.com/org/repo", TOKEN_URL) == TOKEN_URL


@pytest.mark.parametrize(
    "moved",
    [
        pytest.param(f"https://reader:{MASK}@attacker.example/org/repo.git", id="another host"),
        pytest.param(f"https://other:{MASK}@git.example.com/org/repo.git", id="another username"),
        pytest.param(f"http://reader:{MASK}@git.example.com/org/repo.git", id="another scheme"),
        pytest.param(f"https://reader:{MASK}@git.example.com:8443/org/repo.git", id="another port"),
    ],
)
def test_a_mask_that_cannot_be_restored_is_refused_and_never_handed_to_another_host(moved: str) -> None:
    with pytest.raises(GitUrlMaskRefused, match="re-enter the password") as caught:
        restore_served_git_url(moved, URL)

    assert "s3cr3t" not in str(caught.value)


def test_a_new_password_a_removed_credential_and_a_cleared_url_are_the_persons_change() -> None:
    assert restore_served_git_url("https://reader:newpass@git.example.com/org/repo.git", URL) == "https://reader:newpass@git.example.com/org/repo.git"
    assert restore_served_git_url("https://git.example.com/org/repo.git", URL) == "https://git.example.com/org/repo.git"
    assert restore_served_git_url(None, URL) is None


def test_a_mask_when_nothing_was_stored_is_refused() -> None:
    with pytest.raises(GitUrlMaskRefused, match="re-enter the password"):
        restore_served_git_url(MASKED, None)
    with pytest.raises(GitUrlMaskRefused, match="re-enter the password"):
        restore_served_git_url(MASKED, "https://git.example.com/org/repo.git")


# ---- a create ---------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_a_create_that_carries_the_served_mask_is_refused() -> None:
    """Copy-a-harness: read a row, register it under a new slug. There is no stored URL to restore from, and the literal mask must not become the password."""
    with pytest.raises(GitUrlMaskRefused, match="re-enter the password"):
        refuse_served_git_url(MASKED)
    with pytest.raises(GitUrlMaskRefused, match="re-enter the password"):
        refuse_served_git_url(f"https://{MASK}@github.com/org/repo")


def test_a_create_with_a_real_credential_a_plain_url_or_none_is_fine() -> None:
    for url in (URL, TOKEN_URL, "https://git.example.com/org/repo.git", None):
        refuse_served_git_url(url)


# ---- the real SQLite store ------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_credential_survives_a_real_sqlite_round_trip(tmp_path) -> None:
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    try:
        storage = sp.get_storage(Harness)
        row = _harness()
        row.dependencies_resolved = [_dependency(TOKEN_URL)]
        await storage.create(row)

        back = await storage.get("hns_a")

        assert back.git_url == URL and back.dependencies_resolved[0].git_url == TOKEN_URL
        assert back.model_dump(mode="json")["git_url"] == MASKED
    finally:
        await sp.aclose()
