"""_do_sync transitive-dep integration tests — Spec A §9.

Confirms that sync handles dep template additions, dep template removals,
and dep dropouts from the parent's harness.yaml via the existing 3-way
diff over ``HarnessRendering.entries``. ``build_rendered_entries`` is
multi-slug from Phase 6 so dep entries naturally key into the diff.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.harness.dispatch import (
    HarnessDispatchDeps,
    _do_fetch,
    _do_install,
    _do_sync,
)
from primer.model.agent import Agent
from primer.model.collection import Collection
from primer.model.harness import (
    Harness,
    HarnessOperation,
    HarnessRendering,
    HarnessStatus,
)
from tests._support.harness_ops import stamped


# ---------------------------------------------------------------------------
# Test-repo helpers (mirrors tests/harness/test_dispatch_install_deps.py)
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


def _commit_changes(repo_dir: Path, files: dict[str, str], msg: str) -> None:
    """Write files (creating/overwriting) and commit on main."""
    for rel, content in files.items():
        target = repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir),
         "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", msg],
        check=True, capture_output=True,
    )


def _delete_and_commit(repo_dir: Path, paths: list[str], msg: str) -> None:
    """Remove files via `git rm` and commit on main."""
    for rel in paths:
        subprocess.run(
            ["git", "-C", str(repo_dir), "rm", rel],
            check=True, capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(repo_dir),
         "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", msg],
        check=True, capture_output=True,
    )


_SUB_GUIDE_TEMPLATE = """\
kind: collection
name: guide
spec:
  description: "Guide collection"
  embedder:
    provider_id: openai
    model: text-emb
  search_provider_id: ssp
"""

_SUB_INTRO_TEMPLATE = """\
kind: collection
name: intro
spec:
  description: "Intro collection"
  embedder:
    provider_id: openai
    model: text-emb
  search_provider_id: ssp
"""

_PARENT_AGENT_TEMPLATE = """\
kind: agent
name: main
spec:
  description: "Main agent"
  model:
    profile_id: openai--gpt-4
"""

_EMPTY_SCHEMA = '{"type": "object", "properties": {}}'


def _sub_harness_yaml(name: str, slug: str) -> str:
    return (
        "apiVersion: primer/v1\n"
        "kind: Harness\n"
        "metadata:\n"
        f"  name: {name}\n"
        f"  slug: {slug}\n"
    )


def _parent_harness_yaml(deps: list[dict]) -> str:
    deps_block = ""
    if deps:
        deps_block = "dependencies:\n"
        for d in deps:
            deps_block += f"  - name: {d['name']}\n"
            deps_block += f"    git_url: {d['git_url']}\n"
            deps_block += f"    ref: {d.get('ref', 'main')}\n"
    return (
        "apiVersion: primer/v1\n"
        "kind: Harness\n"
        "metadata:\n"
        "  name: Parent\n"
        + deps_block
    )


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
) -> Harness:
    return Harness(
        id=harness_id,
        slug=slug,
        name="Parent",
        git_url=git_url,
        ref="main",
        overrides={},
        status=HarnessStatus.DRAFT,
        pending_operation=HarnessOperation.FETCH,
        created_at=datetime.now(timezone.utc),
    )


async def _fetch_and_install(deps, harness_storage, harness_id: str) -> None:
    """Run a fetch then install for the given harness id."""
    harness = await harness_storage.get(harness_id)
    assert harness is not None
    _next, error_json = await _do_fetch(deps, harness)
    assert error_json is None, error_json
    fetched = await harness_storage.get(harness_id)
    assert fetched is not None
    _next, error_json = await _do_install(deps, stamped(fetched))
    assert error_json is None, error_json


async def _refetch_and_sync(deps, harness_storage, harness_id: str) -> None:
    """Re-run fetch then sync. Mirrors what the real worker dispatch loop does
    after an external trigger; the existing tests in
    ``test_dispatch_install_deps.py`` use the same pattern (fetch → install).
    """
    harness = await harness_storage.get(harness_id)
    assert harness is not None
    _next, error_json = await _do_fetch(deps, harness)
    assert error_json is None, error_json
    refetched = await harness_storage.get(harness_id)
    assert refetched is not None
    _next, error_json = await _do_sync(deps, stamped(refetched))
    assert error_json is None, error_json


# ---------------------------------------------------------------------------
# 1. Sync picks up a new template added to a dep's repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_handles_new_template_in_dep(
    fake_storage_provider, tmp_path,
):
    sub_dir = tmp_path / "docs-base"
    parent_dir = tmp_path / "parent"

    _init_repo_with_files(sub_dir, {
        "harness.yaml": _sub_harness_yaml("Docs Base", "docs-base"),
        "overrides.schema.json": _EMPTY_SCHEMA,
        "templates/guide.yaml": _SUB_GUIDE_TEMPLATE,
    })
    sub_url = f"file://{sub_dir}"

    _init_repo_with_files(parent_dir, {
        "harness.yaml": _parent_harness_yaml(
            [{"name": "docs", "git_url": sub_url, "ref": "main"}],
        ),
        "overrides.schema.json": _EMPTY_SCHEMA,
        "templates/main.yaml": _PARENT_AGENT_TEMPLATE,
    })
    parent_url = f"file://{parent_dir}"

    deps = _make_deps_for(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = _make_harness_row(
        "h-sync-add", git_url=parent_url, slug="parent-add",
    )
    await harness_storage.create(harness)
    await _fetch_and_install(deps, harness_storage, "h-sync-add")

    coll_storage = fake_storage_provider.get_storage(Collection)
    assert await coll_storage.get("docs-base__guide") is not None
    assert await coll_storage.get("docs-base__intro") is None

    # Add a SECOND template to the sub repo on main.
    _commit_changes(
        sub_dir,
        {"templates/intro.yaml": _SUB_INTRO_TEMPLATE},
        "add intro",
    )

    # Re-fetch + sync; the new template must materialise.
    await _refetch_and_sync(deps, harness_storage, "h-sync-add")

    assert await coll_storage.get("docs-base__guide") is not None
    intro = await coll_storage.get("docs-base__intro")
    assert intro is not None
    assert intro.harness_id == "h-sync-add"

    # The parent's agent must still be present (sync didn't drop it).
    agent = await fake_storage_provider.get_storage(Agent).get("parent-add__main")
    assert agent is not None

    # HarnessRendering snapshot reflects both sub entries.
    rendering = await fake_storage_provider.get_storage(HarnessRendering).get(
        "h-sync-add",
    )
    assert rendering is not None
    by_id = {e.resolved_id: e for e in rendering.entries}
    assert "docs-base__guide" in by_id
    assert "docs-base__intro" in by_id
    assert "parent-add__main" in by_id


# ---------------------------------------------------------------------------
# 2. Sync removes a template dropped from a dep's repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_handles_removed_template_in_dep(
    fake_storage_provider, tmp_path,
):
    sub_dir = tmp_path / "docs-base"
    parent_dir = tmp_path / "parent"

    _init_repo_with_files(sub_dir, {
        "harness.yaml": _sub_harness_yaml("Docs Base", "docs-base"),
        "overrides.schema.json": _EMPTY_SCHEMA,
        "templates/guide.yaml": _SUB_GUIDE_TEMPLATE,
        "templates/intro.yaml": _SUB_INTRO_TEMPLATE,
    })
    sub_url = f"file://{sub_dir}"

    _init_repo_with_files(parent_dir, {
        "harness.yaml": _parent_harness_yaml(
            [{"name": "docs", "git_url": sub_url, "ref": "main"}],
        ),
        "overrides.schema.json": _EMPTY_SCHEMA,
        "templates/main.yaml": _PARENT_AGENT_TEMPLATE,
    })
    parent_url = f"file://{parent_dir}"

    deps = _make_deps_for(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = _make_harness_row(
        "h-sync-rm", git_url=parent_url, slug="parent-rm",
    )
    await harness_storage.create(harness)
    await _fetch_and_install(deps, harness_storage, "h-sync-rm")

    coll_storage = fake_storage_provider.get_storage(Collection)
    assert await coll_storage.get("docs-base__guide") is not None
    assert await coll_storage.get("docs-base__intro") is not None

    # Remove the intro template from the sub repo.
    _delete_and_commit(sub_dir, ["templates/intro.yaml"], "drop intro")

    await _refetch_and_sync(deps, harness_storage, "h-sync-rm")

    # guide survives; intro is gone.
    assert await coll_storage.get("docs-base__guide") is not None
    assert await coll_storage.get("docs-base__intro") is None

    # Parent's agent is untouched.
    assert await fake_storage_provider.get_storage(Agent).get(
        "parent-rm__main",
    ) is not None

    # Rendering snapshot drops the intro entry.
    rendering = await fake_storage_provider.get_storage(HarnessRendering).get(
        "h-sync-rm",
    )
    assert rendering is not None
    by_id = {e.resolved_id: e for e in rendering.entries}
    assert "docs-base__intro" not in by_id
    assert "docs-base__guide" in by_id


# ---------------------------------------------------------------------------
# 3. Sync drops dep entities when the dep is removed from the parent's yaml
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_handles_dep_dropped_from_dependencies(
    fake_storage_provider, tmp_path,
):
    sub_dir = tmp_path / "docs-base"
    parent_dir = tmp_path / "parent"

    _init_repo_with_files(sub_dir, {
        "harness.yaml": _sub_harness_yaml("Docs Base", "docs-base"),
        "overrides.schema.json": _EMPTY_SCHEMA,
        "templates/guide.yaml": _SUB_GUIDE_TEMPLATE,
    })
    sub_url = f"file://{sub_dir}"

    _init_repo_with_files(parent_dir, {
        "harness.yaml": _parent_harness_yaml(
            [{"name": "docs", "git_url": sub_url, "ref": "main"}],
        ),
        "overrides.schema.json": _EMPTY_SCHEMA,
        "templates/main.yaml": _PARENT_AGENT_TEMPLATE,
    })
    parent_url = f"file://{parent_dir}"

    deps = _make_deps_for(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = _make_harness_row(
        "h-sync-drop", git_url=parent_url, slug="parent-drop",
    )
    await harness_storage.create(harness)
    await _fetch_and_install(deps, harness_storage, "h-sync-drop")

    coll_storage = fake_storage_provider.get_storage(Collection)
    assert await coll_storage.get("docs-base__guide") is not None

    # Drop the dependency from the parent's harness.yaml.
    _commit_changes(
        parent_dir,
        {"harness.yaml": _parent_harness_yaml(deps=[])},
        "drop dep",
    )

    await _refetch_and_sync(deps, harness_storage, "h-sync-drop")

    # The sub's collection must be deleted.
    assert await coll_storage.get("docs-base__guide") is None
    # Parent's own agent survives.
    assert await fake_storage_provider.get_storage(Agent).get(
        "parent-drop__main",
    ) is not None

    # dependencies_resolved is now empty on the row.
    after = await harness_storage.get("h-sync-drop")
    assert after is not None
    assert after.dependencies_resolved == []

    # Rendering snapshot no longer includes the sub entry.
    rendering = await fake_storage_provider.get_storage(HarnessRendering).get(
        "h-sync-drop",
    )
    assert rendering is not None
    by_id = {e.resolved_id: e for e in rendering.entries}
    assert "docs-base__guide" not in by_id
    assert "parent-drop__main" in by_id


# ---------------------------------------------------------------------------
# 4. GX-5: the parent and a sub-harness declare the same template names
# ---------------------------------------------------------------------------


def _assistant(description: str) -> str:
    return f"kind: agent\nname: assistant\nspec:\n  description: \"{description}\"\n  model:\n    profile_id: openai--gpt-4\n"


_KB = "kind: collection\nname: kb\nspec:\n  description: kb\n"


def _readme(body: str) -> str:
    return f"kind: document\nname: readme\nspec:\n  collection_id: kb\n  title: Readme\ncontent_inline: \"{body}\"\n"


@pytest.mark.asyncio
async def test_sync_applies_sub_entries_that_share_a_template_name_with_the_parent(tmp_path):
    """Each sub entry is diffed and written by its own resolved id, not merged with the parent's same-named one: the
    sub's document gets its own body at install, a sync writes the sub's changed agent, and a sync deletes the
    agent the sub dropped. Real SQLite storage."""
    from primer.model.collection import Document
    from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
    from primer.storage.factory import StorageProviderFactory

    sub_dir = tmp_path / "dep"
    parent_dir = tmp_path / "parent"
    _init_repo_with_files(sub_dir, {
        "harness.yaml": _sub_harness_yaml("Dep", "dep"), "overrides.schema.json": _EMPTY_SCHEMA,
        "templates/assistant.yaml": _assistant("dep v1"), "templates/kb.yaml": _KB,
        "templates/readme.yaml": _readme("dep body"),
    })
    _init_repo_with_files(parent_dir, {
        "harness.yaml": _parent_harness_yaml([{"name": "dep", "git_url": f"file://{sub_dir}"}]),
        "overrides.schema.json": _EMPTY_SCHEMA,
        "templates/assistant.yaml": _assistant("parent v1"), "templates/kb.yaml": _KB,
        "templates/readme.yaml": _readme("parent body"),
    })

    sp = StorageProviderFactory.create(StorageProviderConfig(
        provider=StorageProviderType.SQLITE, config=SqliteConfig(path=tmp_path / "t.sqlite")))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    try:
        deps = _make_deps_for(sp)
        harness_storage = sp.get_storage(Harness)
        await harness_storage.create(_make_harness_row("acme-id", git_url=f"file://{parent_dir}", slug="acme"))
        await _fetch_and_install(deps, harness_storage, "acme-id")

        agents = sp.get_storage(Agent)
        content = sp.get_content_store()
        assert (await sp.get_storage(Document).get("dep__readme")).collection_id == "dep__kb"
        assert await content.get("dep__readme") == "dep body"
        assert await content.get("acme__readme") == "parent body"

        _commit_changes(sub_dir, {"templates/assistant.yaml": _assistant("dep v2")}, "change assistant")
        await _refetch_and_sync(deps, harness_storage, "acme-id")
        assert (await agents.get("dep__assistant")).description == "dep v2"
        assert (await agents.get("acme__assistant")).description == "parent v1"

        _delete_and_commit(sub_dir, ["templates/assistant.yaml"], "drop assistant")
        await _refetch_and_sync(deps, harness_storage, "acme-id")
        assert await agents.get("dep__assistant") is None
        assert (await agents.get("acme__assistant")).description == "parent v1"
        rendering = await sp.get_storage(HarnessRendering).get("acme-id")
        assert sorted(e.resolved_id for e in rendering.entries) == [
            "acme__assistant", "acme__kb", "acme__readme", "dep__kb", "dep__readme",
        ]
    finally:
        await sp.aclose()
