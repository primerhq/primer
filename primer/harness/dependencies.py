"""Transitive dependency walker for harness subharnesses.

Pure functions modulo an injected async ``fetcher`` so tests can avoid
git I/O. The fetcher signature is:

    async def fetcher(git_url: str, ref: str, subpath: str | None,
                      token: str | None) -> tuple[str, list[dict], str, str]:
        # returns (slug, deps_list, bundle_hash, resolved_commit)
"""

from __future__ import annotations

from typing import Awaitable, Callable, NamedTuple

from primer.model.harness import ResolvedDependency


class CanonicalKey(NamedTuple):
    url: str
    ref: str
    subpath: str


def canonical_key(git_url: str, ref: str, subpath: str | None) -> CanonicalKey:
    """Normalise (url, ref, subpath) for identity comparison across deps."""
    url = git_url.strip().lower()
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("https://"):
        url = url[len("https://"):]
    elif url.startswith("http://"):
        url = url[len("http://"):]
    sp = (subpath or "").strip().replace("\\", "/").strip("/")
    return CanonicalKey(url=url, ref=ref, subpath=sp)


class DependencyCycleError(Exception):
    def __init__(self, path: list[str]) -> None:
        super().__init__("dependency cycle: " + " -> ".join(path))
        self.path = path


class DependencyVersionConflictError(Exception):
    def __init__(
        self,
        slug: str,
        ref_a: str,
        ref_b: str,
        path_a: list[str],
        path_b: list[str],
    ) -> None:
        super().__init__(
            f"dependency version conflict for {slug!r}: "
            f"{ref_a} via {path_a} vs {ref_b} via {path_b}"
        )
        self.slug = slug
        self.ref_a, self.ref_b = ref_a, ref_b
        self.path_a, self.path_b = path_a, path_b


Fetcher = Callable[
    [str, str, str | None, str | None],
    Awaitable[tuple[str, list[dict], str, str]],
]


async def walk_dependencies(
    *,
    parent_deps: list[dict],
    fetcher: Fetcher,
) -> tuple[list[ResolvedDependency], dict[CanonicalKey, ResolvedDependency]]:
    """DFS post-order walk; returns the post-order list + a key→record index.

    - ``visited`` dedups subtrees by canonical key (diamonds collapse).
    - ``path`` (stack of canonical keys) detects revisit-on-path cycles.
    - ``slug_paths`` tracks (slug → (ref, name-path)) so two distinct refs
      of the same slug raise ``DependencyVersionConflictError``.
    """
    visited: dict[CanonicalKey, ResolvedDependency] = {}
    slug_paths: dict[str, tuple[str, list[str]]] = {}
    out: list[ResolvedDependency] = []
    path: list[CanonicalKey] = []
    name_path: list[str] = []

    async def visit(dep: dict, depth: int, parent_name: str | None) -> None:
        name = dep["name"]
        url = dep["git_url"]
        ref = dep.get("ref", "main")
        subpath = dep.get("subpath")
        token = dep.get("git_token")
        key = canonical_key(url, ref, subpath)
        if key in path:
            raise DependencyCycleError(name_path + [name])
        if key in visited:
            return
        path.append(key)
        name_path.append(name)
        try:
            slug, child_deps, bundle_hash, resolved_commit = await fetcher(
                url, ref, subpath, token,
            )
            if slug in slug_paths and slug_paths[slug][0] != ref:
                prev_ref, prev_path = slug_paths[slug]
                raise DependencyVersionConflictError(
                    slug, prev_ref, ref, prev_path, list(name_path),
                )
            slug_paths.setdefault(slug, (ref, list(name_path)))
            for child in child_deps:
                await visit(child, depth + 1, name)
            record = ResolvedDependency(
                name=name,
                slug=slug,
                git_url=url,
                ref=ref,
                subpath=subpath,
                resolved_commit=resolved_commit,
                bundle_hash=bundle_hash,
                depth=depth,
                parent_name=parent_name,
            )
            visited[key] = record
            out.append(record)
        finally:
            path.pop()
            name_path.pop()

    for d in parent_deps:
        await visit(d, depth=0, parent_name=None)

    return out, visited


__all__ = [
    "CanonicalKey",
    "DependencyCycleError",
    "DependencyVersionConflictError",
    "canonical_key",
    "walk_dependencies",
]
