"""Unconditional system-collection regeneration (amendment M12, decision D-B).

The ``system`` collection is a read-only wiki describing the platform to
its own agents. It holds two things (pivot plan 01, task 11):

* the platform wiki at the root: the hand-written ``docs/wiki`` pages, one
  generated reference page per agent-callable tool, and the legacy layer
  (the shipped agent docs and the how-to guides) until their replacements
  are written. A build writes it only when the version stamp
  (:mod:`primer.knowledge.wiki.stamp`) lets it, and a build that is missing
  hand-written pages or a skipped toolset never claims it and never deletes
  stored pages (lead ruling (b));
* the live install map under ``this-install/``, rebuilt from live state on
  every boot no matter what the stamp says.

Needs no embedder to be useful, and only gains vectors if an operator
enables search on it like any other collection.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable

from primer.ai_docs_path import resolve_ai_docs_dir
from primer.knowledge.importer import slugify_segment
from primer.knowledge.indexing import (
    make_document_indexer,
    make_document_unindexer,
)
from primer.knowledge.tree import DocumentTreeService
from primer.knowledge.wiki.build import LIVE_ROOT, LIVE_SUMMARY, build_wiki
from primer.knowledge.wiki.pages import (
    WikiPage,
    WikiSourceError,
    load_source_pages,
    resolve_wiki_dir,
)
from primer.knowledge.wiki.stamp import claim, decide, read_stamp, still_mine
from primer.model.agent import Agent
from primer.model.collection import Collection, Document
from primer.model.except_ import ConflictError, NotFoundError, PrimerError
from primer.model.graph import Graph
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

SYSTEM_COLLECTION_ID = "system"
WIKI_TITLE = "Platform wiki"
# ``LIVE_ROOT``/``LIVE_SUMMARY`` come from the build so the map the live
# part writes and the table of contents that links to it cannot drift.
ROOT_INDEX_SLUG = "index"
# Outer attempts of the claim loop: a higher version that loses the race to
# a peer keeps going while the decision still says write, so it lands
# without a reboot (lead ruling for slice 6).
_CLAIM_PASSES = 3
# Optional override for tests; None reads primer.api.registries.provider_registry.RESERVED_TOOLSET_IDS.
RESERVED_TOOLSET_IDS: frozenset[str] | None = None
_LIVE_SLUG_RE = re.compile(r"[a-z0-9._-]+")
_HASH_SUFFIX_RE = re.compile(r".*--[0-9a-f]{8}\Z")


def _live_slug(entity_id: str) -> str:
    """Map an entity id onto a deterministic, lowercased, collision-safe Document slug."""
    if (
        _LIVE_SLUG_RE.fullmatch(entity_id)
        and entity_id not in (".", "..")
        and not _HASH_SUFFIX_RE.fullmatch(entity_id)
    ):
        return entity_id
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", entity_id.lower()).strip(".-_") or "entity"
    digest = hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}--{digest}"


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
    if existing is None:
        try:
            return await colls.create(Collection(
                id=SYSTEM_COLLECTION_ID,
                description=WIKI_TITLE,
                system=True,
            ))
        except ConflictError:
            # A fresh install starts the API and every worker at once and they
            # all seed, so the miss above races the create. Losing means the
            # collection exists, which is the whole point of the call.
            existing = await colls.get(SYSTEM_COLLECTION_ID)
            if existing is None:
                raise
    if existing.description != WIKI_TITLE:
        # A pre-wiki install holds "Primer system reference". The title is
        # the platform's (the table of contents is the page it points at);
        # the operator's search block travels with the model copy.
        existing = await colls.update(
            existing.model_copy(update={"description": WIKI_TITLE}),
        )
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
# model and renderer. The subtrees live under this-install/ (the wiki's own
# agents/, graphs/ and collections/ need those root paths). Tools are absent
# on purpose: they are enumerated from toolset providers rather than stored
# as rows, so nothing mutates one and there is no hook to hang this on.
def _entity_kinds() -> dict:
    return {
        "agent": (f"{LIVE_ROOT}/agents", Agent, _agent_body, "Agents"),
        "graph": (f"{LIVE_ROOT}/graphs", Graph, _graph_body, "Graphs"),
        "collection": (
            f"{LIVE_ROOT}/collections", Collection, _collection_body, "Collections",
        ),
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


async def _workspaces_map(storage_provider, *, root: str = "") -> dict[str, str]:
    """/workspaces: one page per workspace row plus an index, under ``root``."""
    from primer.model.workspace import Workspace

    prefix = f"{root}/" if root else ""
    rows = await _all_rows(storage_provider, Workspace)
    out: dict[str, str] = {}
    lines = ["# Workspaces", "", "Places sessions run. One page each.", ""]
    for row in rows:
        label = row.name or row.id
        slug = _live_slug(row.id)
        lines.append(f"- [{label}]({prefix}workspaces/{slug}) - phase {row.phase}")
        out[f"{prefix}workspaces/{slug}"] = (
            f"# Workspace {row.id}\n\n"
            f"- name: {label}\n"
            f"- template: {row.template_id}\n"
            f"- provider: {row.provider_id}\n"
            f"- phase: {row.phase}\n"
        )
    if not rows:
        lines.append("No workspaces exist yet.")
    out[f"{prefix}workspaces"] = "\n".join(lines) + "\n"
    return out


async def _providers_map(storage_provider, *, root: str = "") -> dict[str, str]:
    """/providers: what this install can talk to, by class, under ``root``.

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

    prefix = f"{root}/" if root else ""
    out: dict[str, str] = {}
    llms = await _all_rows(storage_provider, LLMProvider)
    for row in llms:
        keys = sorted(row.config.model_dump(mode="json").keys())
        slug = _live_slug(row.id)
        out[f"{prefix}providers/llm/{slug}"] = (
            f"# LLM provider {row.id}\n\n"
            f"- type: {_enum_value(row.provider)}\n"
            f"- config keys (values omitted): {', '.join(keys)}\n"
        )
    out[f"{prefix}providers/llm"] = _index(
        "LLM providers",
        [(r.id, f"{prefix}providers/llm/{_live_slug(r.id)}", _enum_value(r.provider)) for r in llms],
    )

    def _profile_label(p) -> str:
        # kind="aggregated" (01a067c4) profiles have no model_name/
        # provider_id/context_length of their own -- the single-profile
        # label would literally render "None on None, context None".
        if p.kind == "aggregated":
            return f"aggregated pool of {len(p.members or [])}: " + ", ".join(
                p.members or [],
            )
        return f"{p.model_name} on {p.provider_id}, context {p.context_length}"

    profiles = await _all_rows(storage_provider, ModelProfile)
    out[f"{prefix}providers/model-profiles"] = _index(
        "Model profiles",
        [(p.id, None, _profile_label(p)) for p in profiles],
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
            slug = _live_slug(row.id)
            out[f"{prefix}providers/{sub}/{slug}"] = (
                f"# {label[:-1]} {row.id}\n\n- type: {_enum_value(row.provider)}\n"
            )
        out[f"{prefix}providers/{sub}"] = _index(
            label, [(r.id, f"{prefix}providers/{sub}/{_live_slug(r.id)}", _enum_value(r.provider)) for r in rows],
        )

    out[f"{prefix}providers"] = _index(
        "Providers",
        [
            ("llm", f"{prefix}providers/llm", "chat/completion backends"),
            ("model-profiles", f"{prefix}providers/model-profiles", "what agents bind to"),
            ("embedding", f"{prefix}providers/embedding", "vector embedders"),
            ("semantic-search", f"{prefix}providers/semantic-search", "vector stores"),
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


def _title_of(path: str, body: str) -> str:
    """A live or legacy page's title: the body's first heading, else the slug."""
    first = body.splitlines()[0] if body else ""
    return first[2:].strip() if first.startswith("# ") else path.rsplit("/", 1)[-1]


def _live_page(path: str, body: str, *, summary: str = "") -> WikiPage:
    return WikiPage(
        path=path, title=_title_of(path, body), summary=summary,
        body=body, kind="live", meta={"kind": "live", "summary": summary},
    )


# The fixed one-line summary of each index page of the live map (a leaf
# page carries none). The root's line is the build's, so the map and the
# table of contents that links to it cannot drift.
_LIVE_INDEX_SUMMARIES: dict[str, str] = {
    LIVE_ROOT: LIVE_SUMMARY,
    f"{LIVE_ROOT}/agents": "every agent on this install",
    f"{LIVE_ROOT}/graphs": "every graph on this install",
    f"{LIVE_ROOT}/collections": "every collection on this install",
    f"{LIVE_ROOT}/workspaces": "every workspace and its phase",
    f"{LIVE_ROOT}/providers": "LLM, embedding and search backends",
    f"{LIVE_ROOT}/providers/llm": "chat/completion backends",
    f"{LIVE_ROOT}/providers/model-profiles": "what agents bind to",
    f"{LIVE_ROOT}/providers/embedding": "vector embedders",
    f"{LIVE_ROOT}/providers/semantic-search": "vector stores",
}


def _this_install_body() -> str:
    return (
        "# This install\n\n"
        "This is the map of THIS install. It is rebuilt from live state at "
        "every startup, so it never goes stale, and it is read-only: grep "
        "it, read it, do not write to it.\n\n"
        f"- [agents]({LIVE_ROOT}/agents) - every agent, its purpose and its model\n"
        f"- [graphs]({LIVE_ROOT}/graphs) - every graph and what it orchestrates\n"
        f"- [collections]({LIVE_ROOT}/collections) - every collection on this install\n"
        f"- [workspaces]({LIVE_ROOT}/workspaces) - every workspace and its phase\n"
        f"- [providers]({LIVE_ROOT}/providers) - LLM, embedding and search backends\n"
    )


async def _live_pages(storage_provider) -> dict[str, WikiPage]:
    """The live install map: what THIS install holds, under ``this-install``."""
    live: dict[str, WikiPage] = {}

    def put(path: str, body: str) -> None:
        live[path] = _live_page(
            path, body, summary=_LIVE_INDEX_SUMMARIES.get(path, ""),
        )

    agents = await _list_all(storage_provider, Agent)
    for a in agents:
        put(f"{LIVE_ROOT}/agents/{_live_slug(a.id)}", _agent_body(a))
    put(LIVE_ROOT + "/agents", _index_body(
        "Agents", [(f"{LIVE_ROOT}/agents/{_live_slug(a.id)}", a.id) for a in agents],
    ))

    graphs = await _list_all(storage_provider, Graph)
    for g in graphs:
        put(f"{LIVE_ROOT}/graphs/{_live_slug(g.id)}", _graph_body(g))
    put(LIVE_ROOT + "/graphs", _index_body(
        "Graphs", [(f"{LIVE_ROOT}/graphs/{_live_slug(g.id)}", g.id) for g in graphs],
    ))

    colls = await _list_all(storage_provider, Collection)
    for c in colls:
        if c.id == SYSTEM_COLLECTION_ID:
            continue
        put(f"{LIVE_ROOT}/collections/{_live_slug(c.id)}", _collection_body(c))
    put(LIVE_ROOT + "/collections", _index_body(
        "Collections",
        [(f"{LIVE_ROOT}/collections/{_live_slug(c.id)}", c.id)
         for c in colls if c.id != SYSTEM_COLLECTION_ID],
    ))

    for path, body in (
        await _workspaces_map(storage_provider, root=LIVE_ROOT)
    ).items():
        put(path, body)
    for path, body in (
        await _providers_map(storage_provider, root=LIVE_ROOT)
    ).items():
        put(path, body)

    put(LIVE_ROOT, _this_install_body())
    return {path: live[path] for path in sorted(live)}


def _legacy_bodies() -> dict[str, str]:
    """Today's docs/ and how-to/ bodies, as written before the wiki existed.

    The shipped agent docs (``resolve_ai_docs_dir()``) plus their index and
    their synthesised ancestors, and the task-shaped how-to guides. They are
    the legacy layer of the wiki: kept verbatim until their replacements
    are written under ``docs/wiki``.
    """
    out: dict[str, str] = {}
    doc_children = []
    try:
        root = resolve_ai_docs_dir()
        for md in sorted(root.rglob("*.md")):
            rel = md.relative_to(root).with_suffix("").as_posix()
            slugs = [slugify_segment(seg) for seg in rel.split("/")]
            if any(s is None for s in slugs):
                continue
            path = "docs/" + "/".join(slugs)
            out[path] = md.read_text(encoding="utf-8", errors="replace")
            doc_children.append((path, rel))
    except Exception:  # noqa: BLE001 - shipped docs are optional at runtime
        logger.warning("system collection: ai docs directory unavailable")
    out["docs"] = _index_body("Docs", doc_children)

    # Nested sources imply intermediate nodes; the tree requires a parent to
    # exist before its child, so synthesise an index for every missing
    # ancestor, then rebuild each synthesised index to list its children.
    for path in sorted(out, key=lambda p: p.count("/")):
        parts = path.split("/")
        for depth in range(1, len(parts)):
            ancestor = "/".join(parts[:depth])
            if ancestor not in out:
                out[ancestor] = _index_body(parts[depth - 1], [])
    for path in list(out):
        kids = [
            (p, p.rsplit("/", 1)[-1]) for p in out
            if p.startswith(path + "/") and p.count("/") == path.count("/") + 1
        ]
        if kids and out[path].startswith("# ") and "_(none)_" in out[path]:
            out[path] = _index_body(path.rsplit("/", 1)[-1], kids)

    out.update(_how_to_map())
    return out


def _legacy_pages() -> dict[str, WikiPage]:
    pages = {
        path: WikiPage(
            path=path, title=_title_of(path, body), summary="", body=body,
            kind="legacy", meta={"kind": "legacy", "summary": ""},
        )
        for path, body in _legacy_bodies().items()
    }
    return {path: pages[path] for path in sorted(pages)}


async def _write_tree_page(
    tree: DocumentTreeService,
    *,
    path: str,
    body: str,
    title: str,
    meta: dict[str, Any],
) -> None:
    """Update or create ``path`` in the system collection, recovering if its
    content row was orphaned by an earlier entity-only delete."""
    parent, _, slug = path.rpartition("/")
    try:
        await tree.update(
            collection_id=SYSTEM_COLLECTION_ID, path=path,
            body=body, title=title, meta=meta,
        )
    except NotFoundError:
        try:
            await tree.create(
                collection_id=SYSTEM_COLLECTION_ID, parent=parent, slug=slug,
                body=body, title=title, strict_slugs=False,
                meta=meta,
            )
        except ConflictError:
            # Either a concurrent CDC convergence hook wrote this page between
            # the miss above and this create, or the Document entity row was
            # deleted earlier leaving an orphaned content row.
            try:
                await tree.update(
                    collection_id=SYSTEM_COLLECTION_ID, path=path,
                    body=body, title=title, meta=meta,
                )
            except NotFoundError:
                orphan_id = await tree._content.resolve_id(
                    SYSTEM_COLLECTION_ID, path,
                )
                if orphan_id is not None:
                    await tree._content.delete(orphan_id)
                created = await tree.create(
                    collection_id=SYSTEM_COLLECTION_ID, parent=parent,
                    slug=slug, body=body, title=title,
                    strict_slugs=False, meta=meta,
                )
                if orphan_id is not None:
                    for child in await tree._children(
                        SYSTEM_COLLECTION_ID, orphan_id,
                    ):
                        await tree._docs.update(
                            child.model_copy(update={"parent_id": created.id})
                        )


async def _converge(
    tree: DocumentTreeService, pages: dict[str, WikiPage],
    guard: Callable[[], Awaitable[bool]] | None = None,
) -> int:
    """Write every page whose body, title or meta differs; return the count.

    Parents before children so each create resolves its parent. ``guard`` is
    consulted before every write and the pass stops at the first ``False``
    (the stamp was taken by a newer build mid-write); pass None for the live
    map, which is written whatever the decision.
    """
    written = 0
    for path in sorted(pages, key=lambda p: (p.count("/"), p)):
        if guard is not None and not await guard():
            break
        page = pages[path]
        try:
            current = await tree.read(
                collection_id=SYSTEM_COLLECTION_ID, path=path,
            )
        except PrimerError:
            current = None
        if (
            current is not None
            and current.body == page.body
            and current.document.title == page.title
            and current.document.meta == page.meta
        ):
            continue
        try:
            await _write_tree_page(
                tree, path=path, body=page.body, title=page.title, meta=page.meta,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 - one bad page never stops the pass
            logger.warning("system collection: skipping page %r: %s", path, exc)
    return written


async def _live_entity_for_path(
    storage_provider, path: str,
) -> tuple[bool, str | None, str | None]:
    """Check whether a live-map leaf path still corresponds to a stored entity.

    Returns ``(exists, entity_type_if_cdc, entity_id)``.
    """
    from primer.model.provider import (
        EmbeddingProvider,
        LLMProvider,
        SemanticSearchProvider,
    )
    from primer.model.workspace import Workspace

    parent, _, slug = path.rpartition("/")
    subtrees: dict[str, tuple[type, str | None]] = {
        f"{LIVE_ROOT}/agents": (Agent, "agent"),
        f"{LIVE_ROOT}/graphs": (Graph, "graph"),
        f"{LIVE_ROOT}/collections": (Collection, "collection"),
        f"{LIVE_ROOT}/workspaces": (Workspace, None),
        f"{LIVE_ROOT}/providers/llm": (LLMProvider, None),
        f"{LIVE_ROOT}/providers/embedding": (EmbeddingProvider, None),
        f"{LIVE_ROOT}/providers/semantic-search": (SemanticSearchProvider, None),
    }
    spec = subtrees.get(parent)
    if spec is None or not slug:
        return False, None, None
    model_cls, entity_type = spec
    row = await storage_provider.get_storage(model_cls).get(slug)
    if row is None:
        for candidate in await _list_all(storage_provider, model_cls):
            if _live_slug(candidate.id) == slug:
                row = candidate
                break
    if row is None or (entity_type == "collection" and row.id == SYSTEM_COLLECTION_ID):
        return False, None, None
    return True, entity_type, row.id


async def _prune_live(
    storage_provider, tree: DocumentTreeService, live: dict[str, WikiPage],
    *, provider_registry=None, semantic_search_registry=None,
) -> int:
    """Every node under this-install/ that the live map no longer holds."""
    deleted = 0
    try:
        nodes = await tree.tree(
            collection_id=SYSTEM_COLLECTION_ID, parent=LIVE_ROOT, depth=10,
        )
    except PrimerError:
        return 0  # first pass: this-install/ does not exist yet
    for node in sorted(nodes, key=lambda n: -n.path.count("/")):
        if node.path in live:
            continue
        try:
            exists, entity_type, entity_id = await _live_entity_for_path(
                storage_provider, node.path,
            )
            if exists:
                if entity_type is not None and entity_id is not None:
                    await converge_entity(
                        storage_provider,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        provider_registry=provider_registry,
                        semantic_search_registry=semantic_search_registry,
                    )
                continue
            await tree.delete(
                collection_id=SYSTEM_COLLECTION_ID, path=node.path,
                recursive=True,
            )
            deleted += 1
        except Exception as exc:  # noqa: BLE001 - one bad node never stops pruning
            logger.warning("system collection: failed to prune %r: %s", node.path, exc)
            continue
    return deleted


async def _prune_wiki(
    tree: DocumentTreeService, build_pages: dict[str, WikiPage],
    guard: Callable[[], Awaitable[bool]] | None,
) -> int:
    """Every node outside this-install/ that the build no longer holds.

    Only a won claim calls this: a build that may not replace the wiki may
    not delete its pages either (the old layout outlives a degraded build).
    """
    deleted = 0
    try:
        nodes = await tree.tree(
            collection_id=SYSTEM_COLLECTION_ID, parent="", depth=10,
        )
    except PrimerError:
        return 0
    for node in sorted(nodes, key=lambda n: -n.path.count("/")):
        if node.path == LIVE_ROOT or node.path.startswith(LIVE_ROOT + "/"):
            continue  # the live map owns its own subtree
        if node.path in build_pages:
            continue
        if guard is not None and not await guard():
            break
        try:
            await tree.delete(
                collection_id=SYSTEM_COLLECTION_ID, path=node.path,
                recursive=True,
            )
            deleted += 1
        except PrimerError:
            continue
    return deleted


async def _wiki_pass(
    storage_provider, tree: DocumentTreeService,
    toolset_providers: dict, app_version: str,
) -> int:
    """The versioned build: written only when the stamp says this build may."""
    from primer.toolset.tool_catalog import collect_tool_entries

    entries = await collect_tool_entries(toolset_providers or {})
    source_root = resolve_wiki_dir()
    try:
        source = load_source_pages(source_root)
    except WikiSourceError as exc:
        # A malformed page must never raise out of the boot: the stored
        # wiki stays exactly as it is, and the operator sees the file.
        logger.error("system wiki degraded: %s (the stored wiki is kept)", exc)
        return 0

    # Lead ruling (b): a build that is not the full build may not replace
    # the stored wiki - it is missing the hand-written pages or a toolset
    # that failed to list, and writing it would delete pages it does not
    # hold. It never claims the stamp (a stored build of a healthy content
    # must keep writing) and never deletes.
    problems: list[str] = []
    if not source:
        problems.append(f"no hand-written pages under {source_root}")
    required_toolsets = RESERVED_TOOLSET_IDS
    if required_toolsets is None:
        from primer.api.registries.provider_registry import (
            RESERVED_TOOLSET_IDS as required_toolsets,
        )
    expected_toolsets = set(required_toolsets) | set(toolset_providers or {})
    skipped = sorted(expected_toolsets - {e.toolset_id for e in entries})
    if skipped:
        problems.append("skipped toolset(s): " + ", ".join(skipped))
    if problems:
        logger.info(
            "system collection: wiki left as stored (degraded: %s)",
            "; ".join(problems),
        )
        return 0

    try:
        build = build_wiki(source_root, entries, legacy_pages=_legacy_pages())
    except WikiSourceError as exc:
        logger.error("system wiki degraded: %s (the stored wiki is kept)", exc)
        return 0

    async def still_mine_guard() -> bool:
        """The wiki part writes and deletes only while this build holds the stamp."""
        return await still_mine(storage_provider, build.content_hash)

    stored = await read_stamp(storage_provider)
    decision = decide(
        stored, app_version=app_version, content_hash=build.content_hash,
    )
    if decision in ("older", "superseded"):
        logger.info(
            "system collection: wiki left as stored (%s): stored %s/%s, this build %s/%s",
            decision, stored.app_version, stored.content_hash,
            app_version, build.content_hash,
        )
        return 0
    if decision == "same":
        # Ruling (a): same holds the stamp, not a complete page set (a peer
        # that claimed and died mid-write leaves one). Converge and prune
        # behind still_mine.
        written = await _converge(tree, build.pages, still_mine_guard)
        written += await _prune_wiki(tree, build.pages, still_mine_guard)
        return written

    claimed = False
    for _ in range(_CLAIM_PASSES):
        claimed = await claim(
            storage_provider,
            app_version=app_version, content_hash=build.content_hash,
        )
        if claimed:
            break
        # A peer may have won the race. If the decision is no longer write
        # (an older build took over, or our hash is now superseded) there is
        # nothing left to do.
        stored = await read_stamp(storage_provider)
        decision = decide(
            stored, app_version=app_version, content_hash=build.content_hash,
        )
        if decision != "write":
            logger.info(
                "system collection: wiki left as stored (%s): stored %s/%s, this build %s/%s",
                decision, stored.app_version, stored.content_hash,
                app_version, build.content_hash,
            )
            return 0
    if not claimed:
        # The passes are exhausted and a peer keeps winning. The pages land
        # on the next boot, or in the peer's own pass.
        logger.info(
            "system collection: wiki left as stored (claim lost %d in a row)",
            _CLAIM_PASSES,
        )
        return 0
    written = await _converge(tree, build.pages, still_mine_guard)
    written += await _prune_wiki(tree, build.pages, still_mine_guard)
    return written


async def regenerate_system_collection(
    storage_provider, *, toolset_providers: dict,
    provider_registry=None, semantic_search_registry=None,
    app_version: str | None = None,
) -> int:
    """Rebuild the system collection: the live map, always; the wiki, behind the stamp.

    The live install map is written under ``this-install/`` whatever the
    stamp says, and every node it no longer holds is deleted. The platform
    wiki is the versioned part: :mod:`primer.knowledge.wiki.build` assembles
    it from the hand-written pages, the tool catalog and the legacy layer,
    and :mod:`primer.knowledge.wiki.stamp` decides whether THIS build may
    replace the stored one. See :func:`_wiki_pass` for the rules (a build
    that is missing hand-written pages or a skipped toolset never claims
    and never deletes; a malformed page never raises).

    Returns the number of pages written or deleted, live and wiki
    together. That matters beyond the write count, because when the
    registries are supplied the writes carry indexing hooks: converging
    only what changed keeps the vector index in step with the platform at
    a cost proportional to the diff, instead of re-embedding the whole
    collection at every boot.

    ``app_version`` defaults to ``primer.api.version.APP_VERSION``; supply
    it to exercise the stamp's version order. The registries are optional:
    with search off, or on a caller that has no provider stack, this is the
    plain content-only regeneration.
    """
    if app_version is None:
        from primer.api.version import APP_VERSION

        app_version = APP_VERSION

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

    # The live map, whatever the stamp says. Even if the live part fails,
    # _wiki_pass still runs so the versioned wiki is never blocked by live state.
    written = 0
    try:
        live = await _live_pages(storage_provider)
        written += await _converge(tree, live)
        written += await _prune_live(
            storage_provider, tree, live,
            provider_registry=provider_registry,
            semantic_search_registry=semantic_search_registry,
        )
    except Exception:  # noqa: BLE001 - live map failure must not block the wiki pass
        logger.exception("system collection: live map pass failed")

    # The wiki, behind the stamp.
    written += await _wiki_pass(
        storage_provider, tree, toolset_providers, app_version,
    )
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
    waiting for the next startup regeneration. It writes
    ``this-install/<kind>/<id>`` and re-renders the ``this-install/<kind>``
    index that links to it. When the registries are supplied the write
    carries the indexing hooks, so the change reaches the vector store too
    and the entity becomes searchable at once.

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
        path = f"{subtree}/{_live_slug(entity_id)}"
        wrote_page = False

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
            wrote_page = True
        else:
            body = render(entity)
            page = _live_page(path, body)
            try:
                current = await tree.read(
                    collection_id=SYSTEM_COLLECTION_ID, path=path,
                )
            except PrimerError:
                current = None
            if (
                current is None
                or current.body != page.body
                or current.document.title != page.title
                or current.document.meta != page.meta
            ):
                await _write_tree_page(
                    tree, path=path, body=page.body,
                    title=page.title, meta=page.meta,
                )
                wrote_page = True

        # Re-render the subtree index so its links match what is there.
        rows = await _list_all(storage_provider, model_cls)
        children = [
            (f"{subtree}/{_live_slug(r.id)}", r.id) for r in rows
            if not (entity_type == "collection" and r.id == SYSTEM_COLLECTION_ID)
        ]
        index_body = _index_body(title, children)
        index = _live_page(
            subtree, index_body, summary=_LIVE_INDEX_SUMMARIES.get(subtree, ""),
        )
        try:
            current_index = await tree.read(
                collection_id=SYSTEM_COLLECTION_ID, path=subtree,
            )
        except PrimerError:
            current_index = None
        wrote_index = False
        if (
            current_index is None
            or current_index.body != index.body
            or current_index.document.title != index.title
            or current_index.document.meta != index.meta
        ):
            await _write_tree_page(
                tree, path=subtree, body=index.body,
                title=index.title, meta=index.meta,
            )
            wrote_index = True
        return wrote_page or wrote_index
    except Exception:  # noqa: BLE001 - never fail the entity write
        logger.exception(
            "system collection: converging %s %r failed", entity_type, entity_id,
        )
        return False


__all__ = [
    "LIVE_ROOT",
    "SYSTEM_COLLECTION_ID",
    "WIKI_TITLE",
    "converge_entity",
    "regenerate_system_collection",
]
