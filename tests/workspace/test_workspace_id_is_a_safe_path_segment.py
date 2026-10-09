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
would be two rows over ONE directory; k8s label values, docker DNS names and gateway hostnames need lowercase, no
``_``, and an alphanumeric end too. Existing rows are NOT re-validated: the rule bites on create only.

The rule is enforced at every create entry (the REST body, the ``create_workspace`` tool args,
``WorkspaceRegistry.materialise`` - the choke point REST, the tool, the bootstrap seed and any future path all pass
through) and re-asserted in the local backend as defence in depth: the join is resolved for the containment test
(refused when it is not strictly inside the resolved root), but the backend keeps the UNRESOLVED join, so the
create rollback's ``rmtree`` and ``destroy`` never delete through an in-root symlink into another workspace's
directory; an id that IS a symlink is refused outright.

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
]


@pytest.mark.parametrize("wid,landing", ESCAPING, ids=["../escape", "/abs", ".", ".."])
async def test_the_local_backend_refuses_an_id_that_escapes_the_root(
    backend: LocalWorkspaceBackend, wid: str, landing: object,
) -> None:
    """RED shows where the id landed; GREEN refuses it before any directory exists outside the root."""
    root = backend.root.resolve()
    try:
        ws = await backend.create(_template(), workspace_id=wid)
    except ValidationError as exc:
        assert "escapes the workspace root" in exc.message, exc.message
        landed = Path(landing(root))
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


async def test_the_local_backend_accepts_an_id_that_stays_inside_the_root(
    backend: LocalWorkspaceBackend,
) -> None:
    """A nested id stays under the root: the backend's invariant is containment, the entry rule does the rest."""
    ws = await backend.create(_template(), workspace_id="a/b")
    assert ws.id == "a/b"
    assert ws.root.resolve() == (backend.root / "a" / "b").resolve()


async def test_the_local_backend_refuses_a_reattach_that_escapes_the_root(
    backend: LocalWorkspaceBackend,
) -> None:
    """The re-attach path joins the root too: an escaping id must not re-attach to a directory outside it."""
    with pytest.raises(ValidationError):
        await backend.get("../escape", template=_template())


async def test_create_refuses_an_id_that_is_a_symlink_into_another_workspace(
    backend: LocalWorkspaceBackend,
) -> None:
    """A RESOLVED join would rmtree THROUGH the symlink: the refused alias create must leave alpha's files intact."""
    alpha = await backend.create(_template(), workspace_id="alpha")
    marker = alpha.root / "survive.txt"
    await asyncio.to_thread(marker.write_text, "here")
    alias = backend.root / "alias"
    alias.symlink_to(alpha.root)
    with pytest.raises(ValidationError):
        await backend.create(_template(init_commands=["exit 3"]), workspace_id="alias")
    assert marker.exists(), "the refused alias create deleted the alpha workspace's files"


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


@pytest.mark.parametrize("wid", ["../escape", "/abs", ".", "..", "a/b", "new\nline", "a\x00b"])
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
