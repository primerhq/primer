"""_do_install transitive-dep integration tests — Spec A §8.

Uses file-protocol git repos so we exercise the real fetch+install path
with subharness rendering, applying the combined entry set under the
parent harness's id.
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
    _do_install,
    _do_uninstall,
)
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection, CollectionEmbedder
from primer.model.harness import (
    Harness,
    HarnessOperation,
    HarnessRendering,
    HarnessStatus,
)


# ---------------------------------------------------------------------------
# Test-repo helpers (copied from test_dispatch_fetch_deps.py shape)
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


# Sub harness: produces a collection (no cross-refs back to parent).
_SUB_COLLECTION_TEMPLATE = """\
kind: collection
name: guide
spec:
  description: "Guide collection"
  embedder:
    provider_id: openai
    model: text-emb
  search_provider_id: ssp
"""

# Parent harness: produces an agent.
_PARENT_AGENT_TEMPLATE = """\
kind: agent
name: main
spec:
  description: "Main agent"
  model:
    provider_id: openai
    model_name: gpt-4
"""

# Sub harness whose template intentionally produces an id that collides
# with another harness's entity.
_COLLIDING_TEMPLATE = """\
kind: agent
name: widget
spec:
  description: "Widget"
  model:
    provider_id: openai
    model_name: gpt-4
"""

_EMPTY_SCHEMA = '{"type": "object", "properties": {}}'


def _make_sub_repo(
    repo_dir: Path,
    *,
    name: str,
    slug: str,
    template_text: str = _SUB_COLLECTION_TEMPLATE,
    template_name: str = "guide.yaml",
) -> str:
    yaml_text = (
        "apiVersion: primer/v1\n"
        "kind: Harness\n"
        "metadata:\n"
        f"  name: {name}\n"
        f"  slug: {slug}\n"
    )
    files = {
        "harness.yaml": yaml_text,
        "overrides.schema.json": _EMPTY_SCHEMA,
        f"templates/{template_name}": template_text,
    }
    _init_repo_with_files(repo_dir, files)
    return f"file://{repo_dir}"


def _make_parent_repo(
    repo_dir: Path,
    *,
    deps: list[dict],
    template_text: str = _PARENT_AGENT_TEMPLATE,
    template_name: str = "main.yaml",
) -> str:
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
        "metadata:\n"
        "  name: Parent\n"
        + deps_block
    )
    files = {
        "harness.yaml": yaml_text,
        "overrides.schema.json": _EMPTY_SCHEMA,
        f"templates/{template_name}": template_text,
    }
    _init_repo_with_files(repo_dir, files)
    return f"file://{repo_dir}"


def _make_deps_for(fake_storage_provider) -> HarnessDispatchDeps:
    return HarnessDispatchDeps(
        storage_provider=fake_storage_provider,
        event_bus=InMemoryEventBus(),
    )


def _make_harness_row(
    harness_id: str,
    *,
    git_url: str,
    slug: str,
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
# 1. Install parent + 1 dep: both entity sets get created under parent's id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_parent_with_one_dep_creates_both_entity_sets(
    fake_storage_provider, tmp_path,
):
    sub_url = _make_sub_repo(
        tmp_path / "docs-base", name="Docs Base", slug="docs-base",
    )
    parent_url = _make_parent_repo(
        tmp_path / "parent",
        deps=[{"name": "docs", "git_url": sub_url, "ref": "main"}],
    )

    deps = _make_deps_for(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = _make_harness_row(
        "h-install-1", git_url=parent_url, slug="parent-slug",
    )
    await harness_storage.create(harness)

    # Step 1: fetch
    next_status, error_json = await _do_fetch(deps, harness)
    assert error_json is None, error_json

    fetched = await harness_storage.get("h-install-1")
    assert fetched is not None
    assert len(fetched.dependencies_resolved) == 1

    # Step 2: install
    next_status, error_json = await _do_install(deps, fetched)
    assert error_json is None, error_json
    assert next_status == HarnessStatus.INSTALLED

    # The sub's collection MUST be present under docs-base__guide.
    coll = await fake_storage_provider.get_storage(Collection).get(
        "docs-base__guide",
    )
    assert coll is not None
    assert coll.harness_id == "h-install-1"

    # Parent's agent MUST be present under parent-slug__main.
    agent = await fake_storage_provider.get_storage(Agent).get(
        "parent-slug__main",
    )
    assert agent is not None
    assert agent.harness_id == "h-install-1"

    # HarnessRendering MUST include both entries, with source_dependency
    # populated correctly.
    rendering = await fake_storage_provider.get_storage(HarnessRendering).get(
        "h-install-1",
    )
    assert rendering is not None
    by_id = {e.resolved_id: e for e in rendering.entries}
    assert "docs-base__guide" in by_id
    assert "parent-slug__main" in by_id
    assert by_id["docs-base__guide"].source_dependency == "docs"
    assert by_id["parent-slug__main"].source_dependency is None


# ---------------------------------------------------------------------------
# 2. Uninstall removes parent + sub entities + rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_uninstall_removes_all_including_sub_entities(
    fake_storage_provider, tmp_path,
):
    sub_url = _make_sub_repo(
        tmp_path / "docs-base", name="Docs Base", slug="docs-base",
    )
    parent_url = _make_parent_repo(
        tmp_path / "parent",
        deps=[{"name": "docs", "git_url": sub_url, "ref": "main"}],
    )

    deps = _make_deps_for(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = _make_harness_row(
        "h-uninstall", git_url=parent_url, slug="parent-uninstall",
    )
    await harness_storage.create(harness)

    next_status, error_json = await _do_fetch(deps, harness)
    assert error_json is None, error_json

    fetched = await harness_storage.get("h-uninstall")
    next_status, error_json = await _do_install(deps, fetched)
    assert error_json is None, error_json

    # Pre-flight: both rows exist
    assert await fake_storage_provider.get_storage(Collection).get(
        "docs-base__guide",
    ) is not None
    assert await fake_storage_provider.get_storage(Agent).get(
        "parent-uninstall__main",
    ) is not None

    installed = await harness_storage.get("h-uninstall")
    await _do_uninstall(deps, installed)

    # Both entity rows AND the rendering AND the harness row are gone.
    assert await fake_storage_provider.get_storage(Collection).get(
        "docs-base__guide",
    ) is None
    assert await fake_storage_provider.get_storage(Agent).get(
        "parent-uninstall__main",
    ) is None
    assert await fake_storage_provider.get_storage(HarnessRendering).get(
        "h-uninstall",
    ) is None
    assert await harness_storage.get("h-uninstall") is None


# ---------------------------------------------------------------------------
# 3. Cross-harness id collision → apply_id_conflict + rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_cross_harness_id_collision(
    fake_storage_provider, tmp_path,
):
    # Pre-seed an existing agent owned by a different harness, with the
    # exact id the new sub will try to produce.
    other_agent = Agent(
        id="acme__widget",
        description="external owner",
        model=AgentModel(provider_id="p", model_name="m"),
        harness_id="h-other",
    )
    await fake_storage_provider.get_storage(Agent).create(other_agent)

    # Sub repo: slug "acme", template "widget" → id "acme__widget" (collides).
    sub_url = _make_sub_repo(
        tmp_path / "acme-sub", name="Acme", slug="acme",
        template_text=_COLLIDING_TEMPLATE,
        template_name="widget.yaml",
    )
    parent_url = _make_parent_repo(
        tmp_path / "corp-parent",
        deps=[{"name": "acme", "git_url": sub_url, "ref": "main"}],
    )

    deps = _make_deps_for(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = _make_harness_row(
        "h-y", git_url=parent_url, slug="corp",
    )
    await harness_storage.create(harness)

    # Fetch must succeed (the collision is detected at install time).
    next_status, error_json = await _do_fetch(deps, harness)
    assert error_json is None, error_json

    fetched = await harness_storage.get("h-y")
    next_status, error_json = await _do_install(deps, fetched)

    assert next_status == HarnessStatus.ERROR
    assert error_json is not None
    err = json.loads(error_json)
    assert err["code"] == "apply_id_conflict"
    assert err["conflicting_id"] == "acme__widget"
    assert err["existing_harness_id"] == "h-other"

    # Y's own entities MUST NOT be in storage (rollback).
    assert await fake_storage_provider.get_storage(Agent).get(
        "corp__main",
    ) is None
    # The original X-owned agent MUST still be present, untouched.
    still = await fake_storage_provider.get_storage(Agent).get("acme__widget")
    assert still is not None
    assert still.harness_id == "h-other"
    assert still.description == "external owner"
