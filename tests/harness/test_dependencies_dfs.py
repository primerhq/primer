"""DFS walker for the transitive dep graph — Spec A §4."""

from __future__ import annotations

import pytest

from primer.harness.dependencies import (
    CanonicalKey,
    DependencyCycleError,
    DependencyVersionConflictError,
    canonical_key,
    walk_dependencies,
)


def test_canonical_key_normalises_url_and_subpath():
    k1 = canonical_key("https://GitHub.com/x/y.git", "main", "charts/a")
    k2 = canonical_key("https://github.com/x/y", "main", "charts/a/")
    assert k1 == k2


def test_canonical_key_subpath_none_equiv_empty():
    assert canonical_key("https://x", "main", None) == canonical_key("https://x", "main", "")


def _fetcher_for(yamls: dict):
    """Return a fetcher closure over {canonical_key: (slug, deps, bundle_hash, sha)}."""
    async def fetch(url, ref, subpath, token):
        key = canonical_key(url, ref, subpath)
        entry = yamls[key]
        return entry  # (slug, deps_list, bundle_hash, resolved_commit)
    return fetch


@pytest.mark.asyncio
async def test_simple_chain():
    # parent → A → B
    fetcher = _fetcher_for({
        canonical_key("https://a", "main", None): ("a", [{"name": "b", "git_url": "https://b", "ref": "main"}], "ha", "sha-a"),
        canonical_key("https://b", "main", None): ("b", [], "hb", "sha-b"),
    })
    resolved, _ = await walk_dependencies(
        parent_deps=[{"name": "a", "git_url": "https://a", "ref": "main"}],
        fetcher=fetcher,
    )
    slugs = [r.slug for r in resolved]
    assert slugs == ["b", "a"]   # post-order: deepest first


@pytest.mark.asyncio
async def test_cycle_direct():
    fetcher = _fetcher_for({
        canonical_key("https://a", "main", None): ("a", [{"name": "a", "git_url": "https://a", "ref": "main"}], "ha", "sha-a"),
    })
    with pytest.raises(DependencyCycleError):
        await walk_dependencies(
            parent_deps=[{"name": "a", "git_url": "https://a", "ref": "main"}],
            fetcher=fetcher,
        )


@pytest.mark.asyncio
async def test_cycle_indirect():
    fetcher = _fetcher_for({
        canonical_key("https://a", "main", None): ("a", [{"name": "b", "git_url": "https://b", "ref": "main"}], "ha", "sa"),
        canonical_key("https://b", "main", None): ("b", [{"name": "a", "git_url": "https://a", "ref": "main"}], "hb", "sb"),
    })
    with pytest.raises(DependencyCycleError):
        await walk_dependencies(
            parent_deps=[{"name": "a", "git_url": "https://a", "ref": "main"}],
            fetcher=fetcher,
        )


@pytest.mark.asyncio
async def test_diamond_same_ref_dedups():
    fetcher = _fetcher_for({
        canonical_key("https://a", "main", None): ("a", [{"name": "c", "git_url": "https://c", "ref": "v1"}], "ha", "sa"),
        canonical_key("https://b", "main", None): ("b", [{"name": "c", "git_url": "https://c", "ref": "v1"}], "hb", "sb"),
        canonical_key("https://c", "v1", None):   ("c", [], "hc", "sc"),
    })
    resolved, _ = await walk_dependencies(
        parent_deps=[
            {"name": "a", "git_url": "https://a", "ref": "main"},
            {"name": "b", "git_url": "https://b", "ref": "main"},
        ],
        fetcher=fetcher,
    )
    slugs = [r.slug for r in resolved]
    assert slugs.count("c") == 1


@pytest.mark.asyncio
async def test_diamond_divergent_ref_conflicts():
    fetcher = _fetcher_for({
        canonical_key("https://a", "main", None): ("a", [{"name": "c", "git_url": "https://c", "ref": "v1"}], "ha", "sa"),
        canonical_key("https://b", "main", None): ("b", [{"name": "c", "git_url": "https://c", "ref": "v2"}], "hb", "sb"),
        canonical_key("https://c", "v1", None):   ("c", [], "hc1", "sc1"),
        canonical_key("https://c", "v2", None):   ("c", [], "hc2", "sc2"),
    })
    with pytest.raises(DependencyVersionConflictError):
        await walk_dependencies(
            parent_deps=[
                {"name": "a", "git_url": "https://a", "ref": "main"},
                {"name": "b", "git_url": "https://b", "ref": "main"},
            ],
            fetcher=fetcher,
        )
