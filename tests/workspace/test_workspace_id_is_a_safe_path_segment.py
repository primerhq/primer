"""A workspace id is one safe path segment: a traversal id may not materialise a workspace outside the configured root.

From the #680 security review (N7, 2026-10-09). ``WorkspaceCreateBody.id``
carried no pattern and ``LocalWorkspaceBackend.create`` did ``self._root / workspace_id`` followed by ``mkdir(parents=True)``:
``../escape`` landed one directory ABOVE the root, ``/abs`` landed at the filesystem root, and ``.``/``..`` landed on the root
itself or its parent. The same id becomes the durable row id, a ``LocalStateRepo`` workspace id and a git trailer value, a
docker container-name suffix and a k8s object-name component.

The one rule (``WORKSPACE_ID_PATTERN`` in ``primer/model/workspace.py``): a DNS-1123 label,
``^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`` - lowercase letters, digits and ``-``, starting and ending with a letter or
digit, at most 63 characters. It matches every id the platform generates (the bootstrap default ``primer``, the generated
``ws-<hex>``). Uppercase is refused because on a case-insensitive filesystem (a macOS dev machine) ``Proj`` and ``proj``
would be two rows over ONE directory - and the lowercase rule protects NEW ids only: a new ``proj`` can still adopt a
legacy ``Proj`` directory (existing rows are NOT re-validated). k8s label VALUES allow uppercase, ``_`` and ``.``; the
lowercase requirement comes from the Gateway API hostname (RFC 1123) and the k8s object names, which - like docker DNS
names - also need no ``_`` and an alphanumeric end.

The rule is enforced at every create entry (the REST body, the ``create_workspace`` tool args,
``WorkspaceRegistry.materialise`` - the choke point REST, the tool, the bootstrap seed and any future path all pass
through) and re-asserted in the local backend as defence in depth: create refuses an id that is not EXACTLY one segment
under the root (checked lexically, so the configured root itself may be a symlink), then resolves the join for the
containment test and refuses an id that IS a symlink, so the create rollback's ``rmtree`` and ``destroy`` never delete
through an in-root symlink into another workspace's directory; a resolve()/is_symlink() failure (a looping link, a name
the filesystem cannot hold, a NUL byte) is a validation error, not a 500. Re-attach checks the id LEXICALLY only - no
resolve(), no is_symlink() - so a workspace DIRECTORY that is a symlink (an operator who moved a big workspace to
another disk and left a link) still loads; destroy leaves a linked workspace's target in place (rmtree refuses a
top-level link).

The backend tests need git because ``LocalWorkspace.materialise`` shells out to it (same gate as
``test_workspace_id_passthrough.py``).
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

import pytest

from primer.api.registries.workspace_registry import WorkspaceRegistry
from primer.model.except_ import ValidationError
from primer.model.workspace import (
    WORKSPACE_ID_PATTERN,
    ResourceLimits,
    WorkspaceTemplate,
)
from primer.workspace import LocalWorkspaceBackend

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


def _template(provider_id: str = "local-1", init_commands: list[str] | None = None) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="dev",
        description="local dev template",
        provider_id=provider_id,
        files=[],
        init_commands=init_commands or [],
        env={},
        resources=ResourceLimits(),
    )


@pytest.fixture
async def backend(tmp_path: Path) -> LocalWorkspaceBackend:
    p = LocalWorkspaceBackend(tmp_path / "provider_root")
    await p.initialize()
    return p


# (workspace id, where it lands when accepted) - each one escapes the root.
ESCAPING = [
    ("../escape", lambda root: root.parent / "escape"),
    ("/abs", lambda root: Path("/abs")),
    (".", lambda root: root),
    ("..", lambda root: root.parent),
    # A sibling whose name STARTS WITH the root's: a prefix (startswith) containment check passes it, is_relative_to does not.
    ("../provider_root-x", lambda root: root.parent / "provider_root-x"),
]


@pytest.mark.parametrize("wid,landing", ESCAPING, ids=["../escape", "/abs", ".", "..", "../provider_root-x"])
async def test_the_local_backend_refuses_an_id_that_escapes_the_root(
    backend: LocalWorkspaceBackend, wid: str, landing: object,
) -> None:
    """RED shows where the id landed; GREEN refuses it before any directory exists outside the root."""
    root = backend.root.resolve()
    try:
        ws = await backend.create(_template(), workspace_id=wid)
    except ValidationError as exc:
        assert "escapes the workspace root" in exc.message, exc.message
        # The refusal message carries no host paths: the configured root and the landing stay in the server log, not the error.
        assert str(root) not in exc.message, exc.message
        landed = Path(landing(root))
        # An absolute id IS its landing, and the message must name the id: the host-path pin covers the other landings.
        if str(landed) != wid:
            assert str(landed) not in exc.message, exc.message
        # ``.`` and ``..`` land ON the root or its parent, which pre-exist: there a refused
        # create must leave no materialised workspace (no .state repo) at the landing.
        if landed == root or landed == root.parent:
            assert not (landed / ".state").exists(), f"id {wid!r} materialised at {landed}"
        else:
            assert not landed.exists(), f"id {wid!r} left a directory at {landed}"
        return
    pytest.fail(
        f"workspace id {wid!r} was accepted and landed at {ws.root.resolve()}, "
        f"which is not under the root {root}"
    )


async def test_the_local_backend_refuses_an_id_that_is_not_exactly_one_segment(
    backend: LocalWorkspaceBackend,
) -> None:
    """A nested id stays INSIDE the root but is still refused: 'alias/x' or 'x/../y' would adopt, then delete, another workspace's directory. The backend's own defence must not depend on the entries refusing '/'."""
    with pytest.raises(ValidationError):
        await backend.create(_template(), workspace_id="a/b")
    assert not (backend.root / "a").exists(), "the refused create left a directory under the root"


async def test_the_local_backend_refuses_a_reattach_that_escapes_the_root(
    backend: LocalWorkspaceBackend,
) -> None:
    """The re-attach path joins the root too: an escaping id must not re-attach to a directory outside it."""
    with pytest.raises(ValidationError):
        await backend.get("../escape", template=_template())


async def test_reattach_loads_a_workspace_directory_that_is_a_symlink_outside_the_root(
    backend: LocalWorkspaceBackend,
) -> None:
    """An operator who moved a big workspace to another disk and left a link must not get ValidationError on every call (a regression #685 introduced): re-attach checks the id lexically, not the directory's link-ness."""
    alpha = await backend.create(_template(), workspace_id="alpha")
    marker = alpha.root / "moved.txt"
    await asyncio.to_thread(marker.write_text, "here")
    outside = backend.root.parent / "outside-alpha"
    await asyncio.to_thread(alpha.root.rename, outside)
    (backend.root / "alpha").symlink_to(outside)
    fresh = LocalWorkspaceBackend(backend.root)
    await fresh.initialize()
    ws = await fresh.get("alpha", template=_template())
    assert ws is not None, "a symlinked workspace directory must re-attach"
    assert (ws.root / "moved.txt").exists()


async def test_reattach_loads_a_workspace_directory_that_is_a_symlink_inside_the_root(
    backend: LocalWorkspaceBackend,
) -> None:
    """So does a link to a directory INSIDE the root: re-attach never follows or refuses the link."""
    alpha = await backend.create(_template(), workspace_id="alpha")
    moved = backend.root / "moved-alpha"
    await asyncio.to_thread(alpha.root.rename, moved)
    (backend.root / "alpha").symlink_to(moved)
    fresh = LocalWorkspaceBackend(backend.root)
    await fresh.initialize()
    ws = await fresh.get("alpha", template=_template())
    assert ws is not None, "a symlinked workspace directory must re-attach"
    assert (ws.root / ".state").exists()


async def test_destroy_of_a_linked_workspace_leaves_the_target_files_in_place(
    backend: LocalWorkspaceBackend,
) -> None:
    """rmtree refuses a top-level link: destroying a linked workspace deletes the link, not the moved-away directory."""
    alpha = await backend.create(_template(), workspace_id="alpha")
    marker = alpha.root / "stay.txt"
    await asyncio.to_thread(marker.write_text, "here")
    outside = backend.root.parent / "outside-destroy"
    await asyncio.to_thread(alpha.root.rename, outside)
    (backend.root / "alpha").symlink_to(outside)
    await backend.destroy("alpha")
    assert marker.exists(), "destroy deleted through the symlink into the target directory"


async def test_create_refuses_an_id_that_is_a_symlink_into_another_workspace(
    backend: LocalWorkspaceBackend,
) -> None:
    """A RESOLVED join would rmtree THROUGH the symlink: the refused alias create must leave alpha's files intact. The data loss is asserted FIRST (catch broadly), then the exception type."""
    alpha = await backend.create(_template(), workspace_id="alpha")
    marker = alpha.root / "survive.txt"
    await asyncio.to_thread(marker.write_text, "here")
    alias = backend.root / "alias"
    alias.symlink_to(alpha.root)
    with pytest.raises(Exception) as err:
        await backend.create(_template(init_commands=["exit 3"]), workspace_id="alias")
    assert marker.exists(), "the refused alias create deleted the alpha workspace's files"
    assert isinstance(err.value, ValidationError), f"expected ValidationError, got {type(err.value).__name__}"
    # The refusal message carries no host paths: the root and the link target stay in the server log.
    assert str(backend.root.resolve()) not in err.value.message, err.value.message
    assert str(alpha.root) not in err.value.message, err.value.message


async def test_create_refuses_a_looping_symlink_id_with_a_validation_error(
    backend: LocalWorkspaceBackend,
) -> None:
    """resolve() raises RuntimeError on a symlink loop: today that is a 500, not a 422."""
    loop = backend.root / "loop"
    loop.symlink_to(loop)
    with pytest.raises(ValidationError) as err:
        await backend.create(_template(), workspace_id="loop")
    assert str(backend.root.resolve()) not in err.value.message, err.value.message


async def test_create_refuses_a_name_the_filesystem_cannot_hold_with_a_validation_error(
    backend: LocalWorkspaceBackend,
) -> None:
    """resolve() raises OSError (ENAMETOOLONG) on a name this long: today that is a 500, not a 422."""
    with pytest.raises(ValidationError) as err:
        await backend.create(_template(), workspace_id="a" * 300)
    assert str(backend.root.resolve()) not in err.value.message, err.value.message


async def test_create_refuses_a_nul_id_with_a_validation_error(
    backend: LocalWorkspaceBackend,
) -> None:
    """resolve() raises ValueError on an embedded NUL byte: today that is a 500, not a 422."""
    with pytest.raises(ValidationError) as err:
        await backend.create(_template(), workspace_id="a\x00b")
    assert str(backend.root.resolve()) not in err.value.message, err.value.message


@pytest.mark.parametrize("wid", ["LegacyWS", "legacy_ws"])
async def test_ids_the_create_entries_refuse_still_reattach_when_created_directly(
    backend: LocalWorkspaceBackend, wid: str,
) -> None:
    """Existing rows are NOT re-validated: an id the entries refuse (uppercase, underscore) must re-attach when a pre-existing row carries it."""
    await backend.create(_template(), workspace_id=wid)
    fresh = LocalWorkspaceBackend(backend.root)
    await fresh.initialize()
    ws = await fresh.get(wid, template=_template())
    assert ws is not None, f"the pre-existing row {wid!r} must re-attach"


async def test_a_symlinked_root_creates_reattaches_and_destroys(tmp_path: Path) -> None:
    """The configured root itself may be a symlink (an operator mount point): create, re-attach and destroy all work through it."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    p = LocalWorkspaceBackend(link)
    await p.initialize()
    ws = await p.create(_template(), workspace_id="x")
    assert ws is not None
    fresh = LocalWorkspaceBackend(link)
    await fresh.initialize()
    ws2 = await fresh.get("x", template=_template())
    assert ws2 is not None, "a workspace under a symlinked root must re-attach"
    await p.destroy("x")
    assert not (real / "x").exists()


# ---------------------------------------------------------------------------
# The pattern itself
# ---------------------------------------------------------------------------


GOOD_IDS = ("primer", "ws-1a2b", "a", "a" * 63)
BAD_IDS = (
    "Proj",
    "ws_1",
    "ab-",
    "-ab",
    "a" * 64,
    "a.b",
    "abc\n",
    "../escape",
    "/abs",
    "a/b",
    ".",
    "..",
    "a\x00b",
)


def test_the_pattern_accepts_the_platform_ids_and_rejects_everything_outside_the_rule() -> None:
    for good in GOOD_IDS:
        assert re.fullmatch(WORKSPACE_ID_PATTERN, good), good
    for bad in BAD_IDS:
        assert not re.fullmatch(WORKSPACE_ID_PATTERN, bad), bad


def test_the_pattern_is_enforced_by_the_create_entry_models() -> None:
    """re.fullmatch and the pydantic entry models (REST body + tool args) must agree."""
    from pydantic import ValidationError as PydanticValidationError

    from primer.api.routers.workspaces import WorkspaceCreateBody
    from primer.toolset.workspaces import _CreateWorkspaceArgs

    for model in (WorkspaceCreateBody, _CreateWorkspaceArgs):
        for good in GOOD_IDS:
            assert model(template_id="tpl-1", id=good).id == good
        for bad in BAD_IDS:
            with pytest.raises(PydanticValidationError):
                model(template_id="tpl-1", id=bad)


# ---------------------------------------------------------------------------
# The registry choke point: every create path passes materialise
# ---------------------------------------------------------------------------


class _RecordingBackend:
    def __init__(self) -> None:
        self.created = False

    async def create(
        self, template, *, overrides=None, workspace_id=None, resolvers=None
    ):
        self.created = True
        return "workspace-handle"


class _Tpl:
    provider_id = "prov1"


@pytest.mark.parametrize("wid", ["../escape", "/abs", ".", "..", "a/b", "abc\n", "new\nline", "a\x00b"])
async def test_the_registry_refuses_an_id_that_fails_the_rule_before_any_backend(
    monkeypatch: pytest.MonkeyPatch, wid: str,
) -> None:
    rec = _RecordingBackend()
    reg = WorkspaceRegistry(storage_provider=object())

    async def _fake_get_backend(provider_id):
        return rec

    monkeypatch.setattr(reg, "get_backend", _fake_get_backend)
    with pytest.raises(ValidationError):
        await reg.materialise(template=_Tpl(), workspace_id=wid)
    assert rec.created is False


async def test_the_registry_still_forwards_an_id_that_passes_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _RecordingBackend()
    reg = WorkspaceRegistry(storage_provider=object())

    async def _fake_get_backend(provider_id):
        return rec

    monkeypatch.setattr(reg, "get_backend", _fake_get_backend)
    result = await reg.materialise(template=_Tpl(), workspace_id="psx-financials")
    assert result == "workspace-handle"
    assert rec.created is True
