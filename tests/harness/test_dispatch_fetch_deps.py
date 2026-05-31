"""_do_fetch transitive-dep integration tests — Spec A §7.

Uses file-protocol git repos so we exercise the real fetch + walk +
schema-compose pipeline without network access.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.harness.dispatch import (
    HarnessDispatchDeps,
    _do_fetch,
)
from primer.model.harness import (
    Harness,
    HarnessOperation,
    HarnessStatus,
)


# ---------------------------------------------------------------------------
# Test-repo helpers
# ---------------------------------------------------------------------------


def _init_repo_with_files(repo_dir: Path, files: dict[str, str]) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo_dir)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir),
         "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "init"],
        check=True, capture_output=True,
    )


def _tag_at_head(repo_dir: Path, tag: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_dir), "tag", tag],
        check=True, capture_output=True,
    )


def _add_commit(repo_dir: Path, files: dict[str, str], msg: str) -> None:
    for rel, content in files.items():
        target = repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "-C", str(repo_dir), "add", "."],
                   check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir),
         "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", msg],
        check=True, capture_output=True,
    )


_MIN_TEMPLATE = """\
kind: agent
name: bot
spec:
  description: hi
  model:
    provider_id: openai
    model_name: gpt-4
"""

_EMPTY_SCHEMA = '{"type": "object", "properties": {}}'


def _make_sub_repo(repo_dir: Path, *, name: str, slug: str | None = None,
                   deps: list[dict] | None = None) -> str:
    """Create a sub harness repo (with templates/ + harness.yaml). Returns file://."""
    meta_lines = ["metadata:", f"  name: {name}"]
    if slug:
        meta_lines.append(f"  slug: {slug}")
    deps_block = ""
    if deps:
        deps_block = "dependencies:\n"
        for d in deps:
            deps_block += f"  - name: {d['name']}\n"
            deps_block += f"    git_url: {d['git_url']}\n"
            deps_block += f"    ref: {d.get('ref', 'main')}\n"
    yaml_text = (
        "apiVersion: primer/v1\n"
        "kind: Harness\n"
        + "\n".join(meta_lines) + "\n"
        + deps_block
    )
    files = {
        "harness.yaml": yaml_text,
        "overrides.schema.json": _EMPTY_SCHEMA,
        "templates/bot.yaml": _MIN_TEMPLATE,
    }
    _init_repo_with_files(repo_dir, files)
    return f"file://{repo_dir}"


def _make_parent_repo(repo_dir: Path, *, deps: list[dict]) -> str:
    deps_block = "dependencies:\n"
    for d in deps:
        deps_block += f"  - name: {d['name']}\n"
        deps_block += f"    git_url: {d['git_url']}\n"
        deps_block += f"    ref: {d.get('ref', 'main')}\n"
    yaml_text = (
        "apiVersion: primer/v1\n"
        "kind: Harness\n"
        "metadata:\n"
        "  name: Parent\n"
        + deps_block
    )
    files = {
        "harness.yaml": yaml_text,
        "overrides.schema.json": '{"type": "object", "properties": {"x": {"type": "string"}}}',
        "templates/parent_bot.yaml": _MIN_TEMPLATE.replace("name: bot", "name: parent-bot"),
    }
    _init_repo_with_files(repo_dir, files)
    return f"file://{repo_dir}"


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


def _make_deps(fake_storage_provider) -> HarnessDispatchDeps:
    return HarnessDispatchDeps(
        storage_provider=fake_storage_provider,
        event_bus=InMemoryEventBus(),
    )


def _make_harness_row(
    harness_id: str,
    *,
    git_url: str,
    slug: str = "parent",
    overrides: dict | None = None,
) -> Harness:
    return Harness(
        id=harness_id,
        slug=slug,
        name="Parent",
        git_url=git_url,
        ref="main",
        overrides=overrides or {},
        status=HarnessStatus.DRAFT,
        pending_operation=HarnessOperation.FETCH,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 1. Single direct dep — populates dependencies_resolved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_with_single_dep_populates_dependencies_resolved(
    fake_storage_provider, tmp_path,
):
    sub_url = _make_sub_repo(
        tmp_path / "docs-base", name="Docs Base", slug="docs-base",
    )
    parent_url = _make_parent_repo(
        tmp_path / "parent",
        deps=[{"name": "docs", "git_url": sub_url, "ref": "main"}],
    )

    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = _make_harness_row("h-single", git_url=parent_url)
    await harness_storage.create(harness)

    next_status, error_json = await _do_fetch(deps, harness)
    assert error_json is None, error_json
    assert next_status in (HarnessStatus.READY, HarnessStatus.OUTDATED)

    updated = await harness_storage.get("h-single")
    assert updated is not None
    assert len(updated.dependencies_resolved) == 1
    resolved = updated.dependencies_resolved[0]
    assert resolved.slug == "docs-base"
    assert resolved.name == "docs"
    assert len(resolved.resolved_commit) == 40
    assert resolved.depth == 0

    # Composite schema mounts the sub schema at properties.dependencies.properties.docs
    assert updated.overrides_schema is not None
    deps_props = (
        updated.overrides_schema.get("properties", {})
        .get("dependencies", {})
        .get("properties", {})
    )
    assert "docs" in deps_props

    # Parent's own property still present (`x` from the schema we wrote)
    assert "x" in updated.overrides_schema.get("properties", {})

    assert updated.available_bundle_hash is not None
    assert len(updated.available_bundle_hash) == 64
    assert updated.available_commit is not None
    assert len(updated.available_commit) == 40


# ---------------------------------------------------------------------------
# 2. Direct cycle — parent → parent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_direct_cycle_fails(fake_storage_provider, tmp_path):
    parent_dir = tmp_path / "selfdep"
    # We need to write the harness.yaml that references its own git_url BEFORE
    # the initial commit; the file:// URL is the path itself.
    parent_url = f"file://{parent_dir}"
    yaml_text = (
        "apiVersion: primer/v1\n"
        "kind: Harness\n"
        "metadata:\n"
        "  name: SelfDep\n"
        "  slug: selfdep\n"
        "dependencies:\n"
        f"  - name: me\n    git_url: {parent_url}\n    ref: main\n"
    )
    _init_repo_with_files(parent_dir, {
        "harness.yaml": yaml_text,
        "overrides.schema.json": _EMPTY_SCHEMA,
        "templates/bot.yaml": _MIN_TEMPLATE,
    })

    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)
    harness = _make_harness_row("h-cycle", git_url=parent_url, slug="selfdep")
    await harness_storage.create(harness)

    next_status, error_json = await _do_fetch(deps, harness)
    assert next_status == HarnessStatus.ERROR
    assert error_json is not None
    err = json.loads(error_json)
    assert err["code"] == "dependency_cycle"


# ---------------------------------------------------------------------------
# 3. Diamond same ref — C appears once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_diamond_same_ref_dedupes(fake_storage_provider, tmp_path):
    c_url = _make_sub_repo(tmp_path / "c", name="C Base", slug="c-base")
    a_url = _make_sub_repo(
        tmp_path / "a", name="A Base", slug="a-base",
        deps=[{"name": "c", "git_url": c_url, "ref": "main"}],
    )
    b_url = _make_sub_repo(
        tmp_path / "b", name="B Base", slug="b-base",
        deps=[{"name": "c", "git_url": c_url, "ref": "main"}],
    )
    parent_url = _make_parent_repo(
        tmp_path / "parent",
        deps=[
            {"name": "a", "git_url": a_url, "ref": "main"},
            {"name": "b", "git_url": b_url, "ref": "main"},
        ],
    )

    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)
    harness = _make_harness_row("h-diamond", git_url=parent_url)
    await harness_storage.create(harness)

    next_status, error_json = await _do_fetch(deps, harness)
    assert error_json is None, error_json
    assert next_status in (HarnessStatus.READY, HarnessStatus.OUTDATED)

    updated = await harness_storage.get("h-diamond")
    slugs = [r.slug for r in updated.dependencies_resolved]
    assert slugs.count("c-base") == 1
    assert slugs.count("a-base") == 1
    assert slugs.count("b-base") == 1


# ---------------------------------------------------------------------------
# 4. Diamond divergent refs — version conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_diamond_divergent_ref_conflicts(
    fake_storage_provider, tmp_path,
):
    # Build C repo first at v1, then tag, then commit again + tag v2.
    c_dir = tmp_path / "c"
    _init_repo_with_files(c_dir, {
        "harness.yaml": (
            "apiVersion: primer/v1\nkind: Harness\n"
            "metadata:\n  name: C Base\n  slug: c-base\n"
        ),
        "overrides.schema.json": _EMPTY_SCHEMA,
        "templates/bot.yaml": _MIN_TEMPLATE,
    })
    _tag_at_head(c_dir, "v1")
    _add_commit(c_dir, {"README.md": "v2 changes\n"}, "v2 update")
    _tag_at_head(c_dir, "v2")
    c_url = f"file://{c_dir}"

    a_url = _make_sub_repo(
        tmp_path / "a", name="A Base", slug="a-base",
        deps=[{"name": "c", "git_url": c_url, "ref": "v1"}],
    )
    b_url = _make_sub_repo(
        tmp_path / "b", name="B Base", slug="b-base",
        deps=[{"name": "c", "git_url": c_url, "ref": "v2"}],
    )
    parent_url = _make_parent_repo(
        tmp_path / "parent",
        deps=[
            {"name": "a", "git_url": a_url, "ref": "main"},
            {"name": "b", "git_url": b_url, "ref": "main"},
        ],
    )

    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)
    harness = _make_harness_row("h-conflict", git_url=parent_url)
    await harness_storage.create(harness)

    next_status, error_json = await _do_fetch(deps, harness)
    assert next_status == HarnessStatus.ERROR
    err = json.loads(error_json)
    assert err["code"] == "dependency_version_conflict"
    assert err["slug"] == "c-base"
