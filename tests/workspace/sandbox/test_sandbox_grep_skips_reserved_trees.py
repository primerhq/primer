"""W55e: a sandbox grep walk skips the reserved trees (.state, .tmp): no read RPC for their files."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.workspace.local.tools import GrepArgs
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.tools import SandboxGrep

_CTX = SimpleNamespace(session=SimpleNamespace(was_read=lambda p: True, mark_read=lambda p: None))


class _CountingSandbox(FakeSandbox):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.reads: list[str] = []

    async def read_file(self, path: str) -> bytes:
        self.reads.append(path)
        return await super().read_file(path)


def _tree(root: Path) -> None:
    (root / ".state" / ".git" / "objects").mkdir(parents=True)
    for i in range(20):
        (root / ".state" / ".git" / "objects" / f"o{i}").write_bytes(b"TODO object\n")
    (root / ".state" / "messages.jsonl").write_text("TODO state\n")
    (root / ".tmp").mkdir()
    (root / ".tmp" / "out.txt").write_text("TODO tmp\n")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("TODO a\n")


async def test_walk_reads_no_reserved_file(tmp_path: Path) -> None:
    _tree(tmp_path)
    sb = _CountingSandbox(tmp_path)
    res = await SandboxGrep(sb, workspace_root="/workspace").execute(GrepArgs(pattern="TODO"), _CTX)
    assert res.output == "src/a.py"
    assert sb.reads == ["/workspace/src/a.py"]


async def test_configured_tree_names_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "st").mkdir()
    (tmp_path / "st" / "x").write_text("TODO\n")
    (tmp_path / "a.txt").write_text("TODO\n")
    sb = _CountingSandbox(tmp_path)
    tool = SandboxGrep(sb, workspace_root="/workspace", reserved_trees=("st", "tm"))
    res = await tool.execute(GrepArgs(pattern="TODO"), _CTX)
    assert res.output == "a.txt"


async def test_a_grep_aimed_inside_a_reserved_tree_still_searches_it(tmp_path: Path) -> None:
    _tree(tmp_path)
    sb = _CountingSandbox(tmp_path)
    res = await SandboxGrep(sb, workspace_root="/workspace").execute(GrepArgs(pattern="TODO", path=".tmp"), _CTX)
    assert res.output == ".tmp/out.txt"


@pytest.mark.skipif(shutil.which("git") is None, reason="SandboxStateRepo needs git")
async def test_workspace_grep_skips_the_template_trees(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from primer.model.workspace import ContainerTemplateConfig, WorkspaceRuntimeMeta, WorkspaceTemplate
    from primer.workspace.sandbox.workspace import SandboxWorkspace

    tpl = WorkspaceTemplate(
        id="t1", provider_id="c1", description="", backend=ContainerTemplateConfig(image="python:3.13"),
        state_path="st", tmp_path="tm",
    )
    sb = _CountingSandbox(tmp_path)
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-1", template=tpl, sandbox=sb, backend_kind="container",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://fake/ws-1", token=SecretStr("t")),
    )
    (tmp_path / "tm").mkdir(exist_ok=True)
    (tmp_path / "tm" / "x.txt").write_text("TODO\n")
    (tmp_path / "a.txt").write_text("TODO\n")
    grep = next(t for t in ws.get_tools() if t.id == "grep")
    sb.reads.clear()
    res = await grep.execute(GrepArgs(pattern="TODO"), _CTX)
    assert res.output == "a.txt"
    assert not any("/st/" in r or "/tm/" in r for r in sb.reads)
