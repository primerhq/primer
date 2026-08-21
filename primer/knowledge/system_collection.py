"""Unconditional system-collection regeneration (amendment M12, decision D-B).

The ``system`` collection is a read-only wiki describing the platform to
its own agents: what agents and graphs exist, which tools are callable,
what collections hold, and the shipped agent-facing docs. It is rebuilt
from live state on every boot, needs no embedder to be useful, and only
gains vectors if an operator enables search on it like any other
collection.
"""
from __future__ import annotations

import asyncio
import logging

from primer.ai_docs_path import resolve_ai_docs_dir
from primer.knowledge.importer import slugify_segment
from primer.knowledge.indexing import (
    make_document_indexer,
    make_document_unindexer,
)
from primer.knowledge.tree import DocumentTreeService
from primer.model.agent import Agent
from primer.model.collection import Collection, Document
from primer.model.except_ import ConflictError, NotFoundError, PrimerError
from primer.model.graph import Graph
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

SYSTEM_COLLECTION_ID = "system"

_SUBTREES = (
    "agents", "graphs", "tools", "collections", "docs",
    # S5 subtrees. They prune on the same diff-and-converge pass; leaving
    # them out would keep a deleted workspace or provider in the map
    # forever, which is exactly the staleness the regeneration exists to
    # prevent.
    "workspaces", "providers", "how-to",
)


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



async def _all_rows(storage_provider, model_cls) -> list:
    """Page every row of one model. Alias of :func:`_list_all`."""
    return await _list_all(storage_provider, model_cls)



def _enum_value(value) -> str:
    """Render an enum as its wire value, never its repr.

    A provider type stringifies as ``LLMProviderType.OLLAMA``, which is a
    python detail; what an agent greps for is ``ollama``.
    """
    return str(getattr(value, "value", value))


def _index(
    title: str,
    entries: list[tuple[str, str | None, str]],
    *,
    preamble: str | None = None,
) -> str:
    """Render a subtree index: a heading, an optional preamble, bullets.

    Each entry is ``(label, path_or_None, note)``; a None path renders the
    label unlinked, which is how an empty class still names itself.
    """
    lines = [f"# {title}", ""]
    if preamble:
        lines.extend([preamble, ""])
    if not entries:
        lines.append("_(none)_")
    for label, path, note in entries:
        head = f"[{label}]({path})" if path else label
        lines.append(f"- {head}" + (f" - {note}" if note else ""))
    return "\n".join(lines) + "\n"


async def _ensure_collection(storage_provider) -> Collection:
    colls = storage_provider.get_storage(Collection)
    existing = await colls.get(SYSTEM_COLLECTION_ID)
    if existing is not None:
        # Preserve an operator-enabled search block across regeneration.
        return existing
    try:
        return await colls.create(Collection(
            id=SYSTEM_COLLECTION_ID,
            description="Primer system reference",
            system=True,
        ))
    except ConflictError:
        # A fresh install starts the API and every worker at once and they
        # all seed, so the miss above races the create. Losing means the
        # collection exists, which is the whole point of the call.
        existing = await colls.get(SYSTEM_COLLECTION_ID)
        if existing is None:
            raise
        return existing


def _agent_body(a) -> str:
    return (
        f"# {a.id}\n\n{a.description or ''}\n\n"
        f"- model profile: `{getattr(a.model, 'profile_id', '')}`\n"
    )


def _graph_body(g) -> str:
    return f"# {g.id}\n\n{g.description or ''}\n"


def _collection_body(c) -> str:
    best = (
        "semantic"
        if c.search is not None and c.search.state == "ready"
        else "fulltext"
    )
    return (
        f"# {c.id}\n\n{c.description or ''}\n\n"
        f"- search: {best}\n"
    )


# Entity types the CRUD routers fire CDC for, mapped onto their subtree,
# model and renderer. Tools are absent on purpose: they are enumerated
# from toolset providers rather than stored as rows, so nothing mutates
# one and there is no hook to hang this on.
def _entity_kinds() -> dict:
    return {
        "agent": ("agents", Agent, _agent_body, "Agents"),
        "graph": ("graphs", Graph, _graph_body, "Graphs"),
        "collection": ("collections", Collection, _collection_body, "Collections"),
    }


def _index_body(title: str, children: list[tuple[str, str]]) -> str:
    lines = [f"# {title}", ""]
    if not children:
        lines.append("_(none)_")
    for path, label in sorted(children):
        lines.append(f"- [{label}]({path})")
    return "\n".join(lines) + "\n"


HOW_TO_GUIDES: dict[str, str] = {
    "create-an-agent": (
        "# Create an agent\n\n"
        "Ask the operator for the capability you want. The operator delegates "
        "to the builder agent (invoke_agent 'builder'), which calls "
        "crud__create_agent. That call is approval-gated: approve it in the "
        "console's approvals view and the agent is created.\n\n"
        "An agent row needs a description, a model profile id, a tool grant "
        "(scoped ids of the form toolset__tool), and a system prompt. See "
        "/tools for every tool that can be granted, and /providers/"
        "model-profiles for the profiles this install has registered.\n"
    ),
    "enable-semantic-search": (
        "# Enable semantic search on a collection\n\n"
        "Grep works on every collection with no setup. Semantic search is "
        "opt-in per collection and needs a registered embedding provider and "
        "a semantic-search provider; see /providers.\n\n"
        "Enable it from the collection's search settings, which registers the "
        "collection for backfill. While backfill runs the collection reports "
        "an indexing state; disabling it drops the vectors again. Grep is "
        "unaffected either way.\n"
    ),
    "configure-voice": (
        "# Configure voice\n\n"
        "Voice is an edge transform: agents stay text in, text out. Register "
        "a speech-to-text provider and a text-to-speech provider (see "
        "/providers), then set the install defaults for each. An agent may "
        "override the TTS voice for its own replies.\n\n"
        "Once both defaults are set, any session composer can record audio "
        "and play replies back.\n"
    ),
    "wire-a-trigger": (
        "# Wire a trigger\n\n"
        "A trigger starts a run without a human typing. Four kinds exist: "
        "delayed, scheduled, webhook, and channel. Ask the operator, which "
        "delegates to the builder's crud__create_trigger (approval-gated).\n\n"
        "Scheduled and delayed triggers are fire and forget. Webhook and "
        "channel triggers can be interactive, meaning the caller or thread "
        "receives the run's final result. See /agents for what a trigger can "
        "target.\n"
    ),
}


async def _workspaces_map(storage_provider) -> dict[str, str]:
    """/workspaces: one page per workspace row plus an index."""
    from primer.model.workspace import Workspace

    rows = await _all_rows(storage_provider, Workspace)
    out: dict[str, str] = {}
    lines = ["# Workspaces", "", "Places sessions run. One page each.", ""]
    for row in rows:
        label = row.name or row.id
        lines.append(f"- [{label}](workspaces/{row.id}) - phase {row.phase}")
        out[f"workspaces/{row.id}"] = (
            f"# Workspace {row.id}\n\n"
            f"- name: {label}\n"
            f"- template: {row.template_id}\n"
            f"- provider: {row.provider_id}\n"
            f"- phase: {row.phase}\n"
        )
    if not rows:
        lines.append("No workspaces exist yet.")
    out["workspaces"] = "\n".join(lines) + "\n"
    return out


async def _providers_map(storage_provider) -> dict[str, str]:
    """/providers: what this install can talk to, by class.

    Config VALUES are never rendered: a provider config carries API keys.
    Only the key NAMES go in, which is what an agent needs to reason about
    whether a provider is configured.
    """
    from primer.model.model_profile import ModelProfile
    from primer.model.provider import (
        EmbeddingProvider,
        LLMProvider,
        SemanticSearchProvider,
    )

    out: dict[str, str] = {}
    llms = await _all_rows(storage_provider, LLMProvider)
    for row in llms:
        keys = sorted(row.config.model_dump(mode="json").keys())
        out[f"providers/llm/{row.id}"] = (
            f"# LLM provider {row.id}\n\n"
            f"- type: {_enum_value(row.provider)}\n"
            f"- config keys (values omitted): {', '.join(keys)}\n"
        )
    out["providers/llm"] = _index(
        "LLM providers",
        [(r.id, f"providers/llm/{r.id}", _enum_value(r.provider)) for r in llms],
    )

    profiles = await _all_rows(storage_provider, ModelProfile)
    out["providers/model-profiles"] = _index(
        "Model profiles",
        [
            (
                p.id,
                None,
                f"{p.model_name} on {p.provider_id}, "
                f"context {p.context_length}",
            )
            for p in profiles
        ],
        preamble=(
            "An agent names a profile, not a model. These are the profiles "
            "this install has registered."
        ),
    )

    for cls, sub, label in (
        (EmbeddingProvider, "embedding", "Embedding providers"),
        (SemanticSearchProvider, "semantic-search", "Semantic-search providers"),
    ):
        rows = await _all_rows(storage_provider, cls)
        for row in rows:
            out[f"providers/{sub}/{row.id}"] = (
                f"# {label[:-1]} {row.id}\n\n- type: {_enum_value(row.provider)}\n"
            )
        out[f"providers/{sub}"] = _index(
            label, [(r.id, f"providers/{sub}/{r.id}", _enum_value(r.provider)) for r in rows],
        )

    out["providers"] = _index(
        "Providers",
        [
            ("llm", "providers/llm", "chat/completion backends"),
            ("model-profiles", "providers/model-profiles", "what agents bind to"),
            ("embedding", "providers/embedding", "vector embedders"),
            ("semantic-search", "providers/semantic-search", "vector stores"),
        ],
        preamble="Everything this install can talk to, by class.",
    )
    return out


def _how_to_map() -> dict[str, str]:
    """/how-to: the authored guides plus their index."""
    out = {f"how-to/{slug}": body for slug, body in HOW_TO_GUIDES.items()}
    out["how-to"] = _index(
        "How to",
        [
            (slug, f"how-to/{slug}", body.splitlines()[0].lstrip("# "))
            for slug, body in sorted(HOW_TO_GUIDES.items())
        ],
        preamble="Task-shaped guides. Read one before configuring anything.",
    )
    return out


# Slug of the collection's root index. A collection root is a PARENT, not
# a document, so the map itself lives at `index` (model slug charset), and
# every subtree index links back to it.
ROOT_INDEX_SLUG = "index"

ROOT_INDEX = (
    "# Primer\n\n"
    "Primer runs AGENTS: an agent is a model, a prompt, and a set of "
    "tools. A SESSION is one agent (or graph) working inside a WORKSPACE, "
    "which is a real filesystem it can read and write. COLLECTIONS are "
    "document trees agents navigate and search; TRIGGERS start runs "
    "without a human typing.\n\n"
    "This collection is the map of THIS install. It is regenerated from "
    "live state at every startup, so it never goes stale, and it is "
    "read-only: grep it, read it, do not write to it.\n\n"
    "- [agents](agents) - every agent, its purpose and its tool grant\n"
    "- [graphs](graphs) - every graph and what it orchestrates\n"
    "- [tools](tools) - every toolset and every tool it exposes\n"
    "- [collections](collections) - every collection on this install\n"
    "- [docs](docs) - the shipped primer documentation\n"
    "- [workspaces](workspaces) - every workspace and its phase\n"
    "- [providers](providers) - LLM, embedding and search backends\n"
    "- [how-to](how-to) - guides for configuring the pieces above\n"
)


async def _desired(storage_provider, toolset_providers: dict) -> dict[str, str]:
    """Build the full {path: body} map the system collection should hold."""
    desired: dict[str, str] = {}

    agents = await _list_all(storage_provider, Agent)
    agent_children = []
    for a in agents:
        path = f"agents/{a.id}"
        desired[path] = _agent_body(a)
        agent_children.append((path, a.id))
    desired["agents"] = _index_body("Agents", agent_children)

    graphs = await _list_all(storage_provider, Graph)
    graph_children = []
    for g in graphs:
        path = f"graphs/{g.id}"
        desired[path] = _graph_body(g)
        graph_children.append((path, g.id))
    desired["graphs"] = _index_body("Graphs", graph_children)

    colls = await _list_all(storage_provider, Collection)
    coll_children = []
    for c in colls:
        if c.id == SYSTEM_COLLECTION_ID:
            continue
        path = f"collections/{c.id}"
        desired[path] = _collection_body(c)
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

    # S5 subtrees: the operator's map of THIS install, plus the root index
    # every subtree links back to. Merged after the synthesised-ancestor
    # pass because each one already carries its own index document.
    desired.update(await _workspaces_map(storage_provider))
    desired.update(await _providers_map(storage_provider))
    desired.update(_how_to_map())
    desired[ROOT_INDEX_SLUG] = ROOT_INDEX

    return desired


async def regenerate_system_collection(
    storage_provider, *, toolset_providers: dict,
    provider_registry=None, semantic_search_registry=None,
) -> int:
    """Rebuild the system collection from live state. Idempotent.

    Returns the number of documents actually written: a path whose body
    already matches is left alone. That matters beyond the write count,
    because when the registries are supplied the writes carry indexing
    hooks. Rewriting all of them would re-embed the entire map on every
    boot, whereas converging only what changed keeps the vector index in
    step with the platform at a cost proportional to the diff.

    The registries are optional: with search off, or on a caller that has
    no provider stack, this is the plain content-only regeneration.
    """
    await _ensure_collection(storage_provider)
    indexer = unindexer = None
    if semantic_search_registry is not None:
        unindexer = make_document_unindexer(
            storage_provider=storage_provider,
            semantic_search_registry=semantic_search_registry,
        )
        if provider_registry is not None:
            indexer = make_document_indexer(
                storage_provider=storage_provider,
                provider_registry=provider_registry,
                semantic_search_registry=semantic_search_registry,
            )
    tree = DocumentTreeService(
        storage_provider, indexer=indexer, unindexer=unindexer,
    )
    desired = await _desired(storage_provider, toolset_providers)

    written = 0
    # Parents before children so each create resolves its parent.
    for path in sorted(desired, key=lambda p: (p.count("/"), p)):
        parent, _, slug = path.rpartition("/")
        body = desired[path]
        try:
            current = await tree.read(
                collection_id=SYSTEM_COLLECTION_ID, path=path,
            )
        except PrimerError:
            current = None
        if current is not None and current.body == body:
            continue
        try:
            await tree.update(
                collection_id=SYSTEM_COLLECTION_ID, path=path, body=body,
            )
        except NotFoundError:
            try:
                await tree.create(
                    collection_id=SYSTEM_COLLECTION_ID, parent=parent, slug=slug,
                    body=body, strict_slugs=False,
                )
            except ConflictError:
                # A CDC convergence hook wrote this page between the miss
                # above and this create. It races us by construction: the
                # seed step before this one creates the agents, and every
                # create fires converge_entity for the very path this loop
                # is about to write. Take the page over rather than failing
                # the whole pass over a write that arrived first.
                await tree.update(
                    collection_id=SYSTEM_COLLECTION_ID, path=path, body=body,
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


# Background index tasks, held so the event loop does not garbage-collect
# a task nobody is awaiting.
_INDEX_TASKS: set[asyncio.Task] = set()


def _deferred(indexer):
    """Run an index-on-write hook off the caller's critical path.

    The page write itself must stay synchronous: a CRUD create is
    expected to leave the entity's page readable the moment it returns.
    Embedding that page is a different matter. It calls out to the
    embedding provider, which on a cold container also downloads the
    model, and doing that inline put a network round trip inside every
    agent, graph and collection write. Five concurrent creates against a
    bootstrap that is already regenerating the collection then queue
    behind each other and the client times out before any of them
    answer.

    Deferring only the embed keeps the page immediate and lets search
    catch up a moment later, which is what searchability has always
    been: index_document is best-effort and a document is simply not
    findable until something indexes it.
    """

    async def _run(*, document: Document, content: str) -> None:
        try:
            await indexer(document=document, content=content)
        except Exception:
            logger.exception(
                "deferred index failed for %s", getattr(document, "path", "?"),
            )

    async def _schedule(*, document: Document, content: str) -> None:
        task = asyncio.create_task(_run(document=document, content=content))
        _INDEX_TASKS.add(task)
        task.add_done_callback(_INDEX_TASKS.discard)

    return _schedule


async def converge_entity(
    storage_provider, *, entity_type: str, entity_id: str,
    provider_registry=None, semantic_search_registry=None,
) -> bool:
    """Bring one entity's page in the system collection up to date.

    Called from the CRUD CDC hooks so that creating, editing or deleting
    an agent, graph or collection is reflected immediately, rather than
    waiting for the next startup regeneration. When the registries are
    supplied the write carries the indexing hooks, so the change reaches
    the vector store too and the entity becomes searchable at once.

    Deliberately narrow: it writes the entity's own page and re-renders
    the one subtree index that links to it. Running the full
    regeneration per mutation would enumerate every toolset provider's
    tools on each write, which is the slow, network-touching part of
    that pass and has no business on a CRUD path.

    Returns True when something was written. Never raises: a system
    collection that is briefly stale must not fail the write that
    changed the entity.
    """
    kinds = _entity_kinds()
    if entity_type not in kinds:
        return False
    subtree, model_cls, render, title = kinds[entity_type]

    try:
        colls = storage_provider.get_storage(Collection)
        if await colls.get(SYSTEM_COLLECTION_ID) is None:
            return False

        indexer = unindexer = None
        if semantic_search_registry is not None:
            unindexer = make_document_unindexer(
                storage_provider=storage_provider,
                semantic_search_registry=semantic_search_registry,
            )
            if provider_registry is not None:
                indexer = _deferred(make_document_indexer(
                    storage_provider=storage_provider,
                    provider_registry=provider_registry,
                    semantic_search_registry=semantic_search_registry,
                ))
        tree = DocumentTreeService(
            storage_provider, indexer=indexer, unindexer=unindexer,
        )

        entity = await storage_provider.get_storage(model_cls).get(entity_id)
        path = f"{subtree}/{entity_id}"

        if entity is None or (
            entity_type == "collection" and entity_id == SYSTEM_COLLECTION_ID
        ):
            # Gone (or the system collection itself, which never lists
            # itself): drop the page if one is there.
            try:
                await tree.delete(
                    collection_id=SYSTEM_COLLECTION_ID, path=path,
                    recursive=True,
                )
            except PrimerError:
                pass
        else:
            body = render(entity)
            try:
                current = await tree.read(
                    collection_id=SYSTEM_COLLECTION_ID, path=path,
                )
            except PrimerError:
                current = None
            if current is not None and current.body == body:
                return False
            try:
                await tree.update(
                    collection_id=SYSTEM_COLLECTION_ID, path=path, body=body,
                )
            except NotFoundError:
                await tree.create(
                    collection_id=SYSTEM_COLLECTION_ID, parent=subtree,
                    slug=entity_id, body=body, strict_slugs=False,
                )

        # Re-render the subtree index so its links match what is there.
        rows = await _list_all(storage_provider, model_cls)
        children = [
            (f"{subtree}/{r.id}", r.id) for r in rows
            if not (entity_type == "collection" and r.id == SYSTEM_COLLECTION_ID)
        ]
        index_body = _index_body(title, children)
        try:
            current_index = await tree.read(
                collection_id=SYSTEM_COLLECTION_ID, path=subtree,
            )
        except PrimerError:
            current_index = None
        if current_index is None or current_index.body != index_body:
            try:
                await tree.update(
                    collection_id=SYSTEM_COLLECTION_ID, path=subtree,
                    body=index_body,
                )
            except NotFoundError:
                await tree.create(
                    collection_id=SYSTEM_COLLECTION_ID, parent="",
                    slug=subtree, body=index_body, strict_slugs=False,
                )
        return True
    except Exception:  # noqa: BLE001 - never fail the entity write
        logger.exception(
            "system collection: converging %s %r failed", entity_type, entity_id,
        )
        return False


__all__ = [
    "SYSTEM_COLLECTION_ID",
    "converge_entity",
    "regenerate_system_collection",
]
