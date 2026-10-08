"""A subharness dependency never receives the parent harness's git_token unless it lives on the parent's own origin.

Install and sync clone every resolved dependency at its pinned commit. They used to pass the PARENT's token to each clone, so
a ``harness.yaml`` that declared a dependency on another host received the parent's credential (``oauth2:<token>@`` in the
clone URL). The rule now: the parent's token goes only to a dependency whose scheme, host and port equal the parent
``git_url``'s; any other dependency is cloned with no token.

The parent repo is a real local bare repo (file://); the dependency clone on another host is intercepted at
``create_subprocess_exec`` and fails without touching the network, after its argv and env are recorded.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import SecretStr

from primer.harness.dispatch import _do_fetch, _do_install, _do_sync
from primer.model.harness import Harness, HarnessStatus, ResolvedDependency
from tests.harness.test_dispatch_install_deps import _make_deps_for, _make_harness_row, _make_parent_repo

_SECRET = "parent-secret-token"


class _FailedProc:
    returncode = 128

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b"fatal: repository not found"

    def kill(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def foreign_clones(monkeypatch) -> list[dict[str, Any]]:
    """Record (and fail) every git call that names the foreign host; let every other call run for real."""
    real_exec = asyncio.create_subprocess_exec
    recorded: list[dict[str, Any]] = []

    async def exec_(*argv: str, **kwargs: Any):
        if any("other.example" in a for a in argv):
            recorded.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
            return _FailedProc()
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    return recorded


async def _fetched_parent_with_foreign_dep(fake_storage_provider, tmp_path) -> tuple[Any, Harness]:
    parent_url = _make_parent_repo(tmp_path / "parent", deps=[])
    deps = _make_deps_for(fake_storage_provider)
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(_make_harness_row("h-tok", git_url=parent_url, slug="parent-slug"))
    _status, error = await _do_fetch(deps, await storage.get("h-tok"))
    assert error is None, error
    harness = await storage.get("h-tok")
    harness.git_token = SecretStr(_SECRET)
    harness.available_bundle_hash = None  # the foreign dep below was never really fetched
    harness.dependencies_resolved = [
        ResolvedDependency(
            name="docs", slug="docs-base", git_url="https://other.example/org/docs.git", ref="main",
            resolved_commit="a" * 40, bundle_hash="b" * 64, depth=0,
        ),
    ]
    return deps, harness


def _assert_no_secret(calls: list[dict[str, Any]]) -> None:
    assert calls, "the foreign dependency was never cloned"
    for call in calls:
        assert not any(_SECRET in a for a in call["argv"]), call["argv"]
        assert _SECRET not in " ".join(call["env"].values())


async def test_install_clones_a_dependency_on_another_host_without_the_parent_token(
    fake_storage_provider, tmp_path, foreign_clones,
):
    deps, harness = await _fetched_parent_with_foreign_dep(fake_storage_provider, tmp_path)

    status, _error = await _do_install(deps, harness)

    assert status == HarnessStatus.ERROR  # the intercepted clone fails; what matters is what it was given
    _assert_no_secret(foreign_clones)


async def test_sync_clones_a_dependency_on_another_host_without_the_parent_token(
    fake_storage_provider, tmp_path, foreign_clones,
):
    deps, harness = await _fetched_parent_with_foreign_dep(fake_storage_provider, tmp_path)
    harness.status = HarnessStatus.INSTALLED

    status, _error = await _do_sync(deps, harness)

    assert status == HarnessStatus.ERROR
    _assert_no_secret(foreign_clones)


@pytest.mark.parametrize(
    ("parent", "dep", "gets_token"),
    [
        ("https://git.example/org/p.git", "https://git.example/org/d.git", True),
        ("https://git.example/org/p.git", "https://GIT.example/other/d", True),
        ("https://git.example/org/p.git", "https://git.example:8443/org/d.git", False),
        ("https://git.example:8443/p", "https://git.example:8443/d", True),
        ("https://git.example/org/p.git", "https://other.example/org/d.git", False),
        ("https://git.example/org/p.git", "https://git.example.evil.test/d.git", False),
        ("file:///srv/p.git", "https://git.example/d.git", False),
        (None, "https://git.example/d.git", False),
    ],
)
def test_the_parent_token_goes_only_to_the_parents_own_origin(parent, dep, gets_token):
    from primer.harness.dispatch import _token_for_dependency  # imported here so the clone tests run red on their own
    assert _token_for_dependency(parent, dep, _SECRET) == (_SECRET if gets_token else None)
    assert _token_for_dependency(parent, dep, None) is None
