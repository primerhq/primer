"""Unconditional system-collection regeneration (amendment M12, decision D-B).

The ``system`` collection is a read-only wiki describing the platform to
its own agents: what agents and graphs exist, which tools are callable,
what collections hold, and the shipped agent-facing docs. It is rebuilt
from live state on every boot, needs no embedder to be useful, and only
gains vectors if an operator enables search on it like any other
collection.
"""
from __future__ import annotations

import logging

from primer.ai_docs_path import resolve_ai_docs_dir
from primer.knowledge.importer import slugify_segment
from primer.knowledge.tree import DocumentTreeService
from primer.model.agent import Agent
from primer.model.collection import Collection
from primer.model.except_ import NotFoundError, PrimerError
from primer.model.graph import Graph
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

SYSTEM_COLLECTION_ID = "system"

_SUBTREES = ("agents", "graphs", "tools", "collections", "docs")


async def _list_all(storage, model_cls) -> list:
    out: list = []
    offset, page = 0, 200
    while True:
        resp = await storage.get_storage(model_cls).list(
            OffsetPage(offset=offset, length=page)
        )
        out.extend(resp.items)
        if len(resp.items) < page:
            return out
        offset += page


async def _ensure_collection(storage_provider) -> Collection:
    colls = storage_provider.get_storage(Collection)
    existing = await colls.get(SYSTEM_COLLECTION_ID)
    if existing is not None:
        # Preserve an operator-enabled search block across regeneration.
        return existing
    return await colls.create(Collection(
        id=SYSTEM_COLLECTION_ID,
        description="Primer system reference",
        system=True,
    ))


def _index_body(title: str, children: list[tuple[str, str]]) -> str:
    lines = [f"# {title}", ""]
    if not children:
        lines.append("_(none)_")
    for path, label in sorted(children):
        lines.append(f"- [{label}]({path})")
    return "\n".join(lines) + "\n"


async def _desired(storage_provider, toolset_providers: dict) -> dict[str, str]:
    """Build the full {path: body} map the system collection should hold."""
    desired: dict[str, str] = {}

    agents = await _list_all(storage_provider, Agent)
    agent_children = []
    for a in agents:
        path = f"agents/{a.id}"
        desired[path] = (
            f"# {a.id}\n\n{a.description or ''}\n\n"
            f"- model profile: `{getattr(a.model, 'profile_id', '')}`\n"
        )
        agent_children.append((path, a.id))
    desired["agents"] = _index_body("Agents", agent_children)

    graphs = await _list_all(storage_provider, Graph)
    graph_children = []
    for g in graphs:
        path = f"graphs/{g.id}"
        desired[path] = f"# {g.id}\n\n{g.description or ''}\n"
        graph_children.append((path, g.id))
    desired["graphs"] = _index_body("Graphs", graph_children)

    colls = await _list_all(storage_provider, Collection)
    coll_children = []
    for c in colls:
        if c.id == SYSTEM_COLLECTION_ID:
            continue
        path = f"collections/{c.id}"
        enabled = "enabled" if c.search is not None else "disabled"
        desired[path] = (
            f"# {c.id}\n\n{c.description or ''}\n\n"
            f"- semantic search: {enabled}\n"
        )
        coll_children.append((path, c.id))
    desired["collections"] = _index_body("Collections", coll_children)

    tool_children = []
    for toolset_id, provider in sorted((toolset_providers or {}).items()):
        try:
            async for tool in provider.list_tools(principal=None):
                path = f"tools/{toolset_id}/{tool.id}"
                desired[path] = f"# {tool.id}\n\n{tool.description or ''}\n"
                tool_children.append((path, f"{toolset_id}.{tool.id}"))
        except Exception:  # noqa: BLE001 - one broken toolset must not stop the rest
            logger.warning("system collection: toolset %r failed to enumerate",
                           toolset_id)
            continue
        desired[f"tools/{toolset_id}"] = _index_body(
            toolset_id, [(p, lbl) for p, lbl in tool_children
                         if p.startswith(f"tools/{toolset_id}/")],
        )
    desired["tools"] = _index_body(
        "Tools",
        [(f"tools/{t}", t) for t in sorted((toolset_providers or {}).keys())],
    )

    doc_children = []
    try:
        root = resolve_ai_docs_dir()
        for md in sorted(root.rglob("*.md")):
            rel = md.relative_to(root).with_suffix("").as_posix()
            slugs = [slugify_segment(seg) for seg in rel.split("/")]
            if any(s is None for s in slugs):
                continue
            path = "docs/" + "/".join(slugs)
            desired[path] = md.read_text(encoding="utf-8", errors="replace")
            doc_children.append((path, rel))
    except Exception:  # noqa: BLE001 - shipped docs are optional at runtime
        logger.warning("system collection: ai docs directory unavailable")
    desired["docs"] = _index_body("Docs", doc_children)

    # Nested sources (ai docs subdirectories, per-toolset tool folders)
    # imply intermediate nodes; the tree requires a parent to exist before
    # its child, so synthesise an index for every missing ancestor.
    for path in sorted(desired, key=lambda p: p.count("/")):
        parts = path.split("/")
        for depth in range(1, len(parts)):
            ancestor = "/".join(parts[:depth])
            if ancestor not in desired:
                desired[ancestor] = _index_body(parts[depth - 1], [])
    # Rebuild each synthesised index so it lists what actually sits under it.
    for path in list(desired):
        kids = [
            (p, p.rsplit("/", 1)[-1]) for p in desired
            if p.startswith(path + "/") and p.count("/") == path.count("/") + 1
        ]
        if kids and desired[path].startswith("# ") and "_(none)_" in desired[path]:
            desired[path] = _index_body(path.rsplit("/", 1)[-1], kids)

    return desired


async def regenerate_system_collection(
    storage_provider, *, toolset_providers: dict,
) -> int:
    """Rebuild the system collection from live state. Idempotent."""
    await _ensure_collection(storage_provider)
    tree = DocumentTreeService(storage_provider)
    desired = await _desired(storage_provider, toolset_providers)

    written = 0
    # Parents before children so each create resolves its parent.
    for path in sorted(desired, key=lambda p: (p.count("/"), p)):
        parent, _, slug = path.rpartition("/")
        body = desired[path]
        try:
            await tree.update(
                collection_id=SYSTEM_COLLECTION_ID, path=path, body=body,
            )
        except NotFoundError:
            await tree.create(
                collection_id=SYSTEM_COLLECTION_ID, parent=parent, slug=slug,
                body=body, strict_slugs=False,
            )
        written += 1

    # Prune whatever the platform no longer describes.
    for subtree in _SUBTREES:
        try:
            nodes = await tree.tree(
                collection_id=SYSTEM_COLLECTION_ID, parent=subtree, depth=10,
            )
        except PrimerError:
            continue
        for node in sorted(nodes, key=lambda n: -n.path.count("/")):
            if node.path not in desired:
                try:
                    await tree.delete(
                        collection_id=SYSTEM_COLLECTION_ID, path=node.path,
                        recursive=True,
                    )
                except PrimerError:
                    continue
    return written


__all__ = ["SYSTEM_COLLECTION_ID", "regenerate_system_collection"]
