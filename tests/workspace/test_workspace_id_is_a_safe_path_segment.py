"""A workspace id is one safe path segment: a traversal id may not materialise a workspace outside the configured root.

From the #680 security review (N7, 2026-10-09; found by static reading, not yet reproduced). ``WorkspaceCreateBody.id``
carried no pattern and ``LocalWorkspaceBackend.create`` did ``self._root / workspace_id`` followed by ``mkdir(parents=True)``:
``../escape`` landed one directory ABOVE the root, ``/abs`` landed at the filesystem root, and ``.``/``..`` landed on the root
itself or its parent. The same id becomes the durable row id, a ``LocalStateRepo`` workspace id and a git trailer value, a
docker container-name suffix and a k8s object-name component.

The one rule (``WORKSPACE_ID_PATTERN`` in ``primer/model/workspace.py``): a single alphanumeric token,
``[A-Za-z0-9][A-Za-z0-9_-]{0,62}`` - it matches every id that exists today (the bootstrap default ``primer``, the generated
``ws-<hex>``) and admits no dot, slash or control character. It is enforced at every create entry (the REST body, the
``create_workspace`` tool
args, ``WorkspaceRegistry.materialise`` - the choke point REST, the tool, the bootstrap seed and any future path all pass
through) and re-asserted in the local backend as defence in depth: the joined path is RESOLVED and refused when it is not
strictly inside the resolved root.

The backend tests need git because ``LocalWorkspace.materialise`` shells out to it (same gate as
``test_workspace_id_passthrough.py``).
"""

from __future__ import annotations

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


def _template(provider_id: str = "local-1") -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="dev",
        description="local dev template",
        provider_id=provider_id,
        files=[],
        init_commands=[],
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


# ---------------------------------------------------------------------------
# The pattern itself
# ---------------------------------------------------------------------------


def test_the_pattern_accepts_the_ids_that_exist_and_rejects_everything_that_escapes() -> None:
    for good in (
        "primer",
        "psx-financials",
        "ws-0123456789abcdef",
        "a",
        "x_y-z",
        "UPPER",
        "a" * 63,
    ):
        assert re.fullmatch(WORKSPACE_ID_PATTERN, good), good
    for bad in (
        "../escape",
        "/abs",
        "a/b",
        ".",
        "..",
        "new\nline",
        "a\x00b",
        "a" * 64,
        "-lead",
        "_lead",
        "",
    ):
        assert not re.fullmatch(WORKSPACE_ID_PATTERN, bad), bad


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
