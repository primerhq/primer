"""Assemble the platform wiki from its sources.

``build_wiki`` reads the hand-written pages under ``docs/wiki/``, takes the tool entries
the reference pages are generated from and the legacy pages that stay until their
replacements are written, and writes nothing. It returns every page the wiki holds outside
``this-install/`` (the live install map is written by the regeneration pass, not built
here) and one content hash over all of them.

What the build adds to the hand-written pages:

* each section page gets a ``## Pages`` list of its direct children;
* the root page ``index`` becomes the table of contents: the intro of ``docs/wiki/index.md``,
  then ``## Contents``, then the legacy roots;
* the generated ``reference/`` pages, each tool page carrying a ``## Used in`` list of the
  feature pages that link to it.

A mistake in the sources raises :class:`~primer.knowledge.wiki.pages.WikiSourceError`.
A link that goes nowhere is only logged: :func:`unresolved_links` is what a coverage test
asserts on.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from primer.knowledge.wiki.pages import (
    WikiPage,
    WikiSourceError,
    load_source_pages,
    page_line,
    sibling_key,
)
from primer.knowledge.wiki.reference import (
    REFERENCE_ROOT,
    REFERENCE_TOOLS,
    ToolEntryView,
    reference_pages,
)

logger = logging.getLogger(__name__)

TOC_PATH = "index"
TOC_TITLE = "Platform wiki"
TOC_SUMMARY = "Every platform feature an agent can use, and the exact tool calls for it."

LIVE_ROOT = "this-install"
LIVE_SUMMARY = "live list of agents, graphs, collections, workspaces and providers"
LIVE_LINK_ROOTS = frozenset({
    LIVE_ROOT,
    f"{LIVE_ROOT}/agents",
    f"{LIVE_ROOT}/graphs",
    f"{LIVE_ROOT}/collections",
    f"{LIVE_ROOT}/workspaces",
    f"{LIVE_ROOT}/providers",
})
"""Link targets that resolve although the build holds no such page: the live install map is written at run time."""

_RESERVED_ROOTS = (
    (REFERENCE_ROOT, "the generated tool reference"),
    (LIVE_ROOT, "the live install map"),
)


@dataclass(frozen=True)
class WikiBuild:
    pages: dict[str, WikiPage]
    content_hash: str


# ---- links ---------------------------------------------------------------------


_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
# A link label may hold escaped characters and balanced brackets (one level, as in "[Create [admin]](x)").
_LABEL = r"(?:\\.|[^\[\]\\\n]|\[(?:\\.|[^\[\]\\\n])*\])*"
_LINK_RE = re.compile(rf"\[{_LABEL}\]\(\s*([^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:")


def wiki_links(body: str) -> list[str]:
    """The targets of the markdown links in ``body`` that point at wiki paths, in reading order.

    A target loses its ``#anchor``. ``http://``, ``https://`` and ``mailto:`` links and bare
    ``#anchor`` links are not wiki links; neither is anything inside a fenced code block or an
    inline code span, which is where a page shows what a link looks like. An image's target counts
    like a link's: the wiki holds no files, so a relative image path can never resolve.
    """
    targets: list[str] = []
    fence: tuple[str, int] | None = None
    for line in body.split("\n"):
        opening = _FENCE_RE.match(line)
        if fence is not None:
            stripped = line.strip()
            if opening and stripped[0] == fence[0] and len(stripped) >= fence[1] and set(stripped) == {fence[0]}:
                fence = None
            continue
        if opening:
            fence = (opening.group(1)[0], len(opening.group(1)))
            continue
        for match in _LINK_RE.finditer(_CODE_SPAN_RE.sub(" ", line)):
            raw = match.group(1)
            if raw.lower().startswith(_EXTERNAL_PREFIXES):
                continue
            target = raw.split("#", 1)[0]
            if target:
                targets.append(target)
    return targets


def unresolved_links(pages: Mapping[str, WikiPage]) -> list[tuple[str, str]]:
    """``(page path, target)`` for every link of a non-legacy page that goes to no page.

    The ``related`` list of a page's frontmatter counts as links (a ``#anchor`` is ignored there
    too). The live roots
    (:data:`LIVE_LINK_ROOTS`) resolve although the build holds no page there. Pages come back
    in path order, each page's targets in reading order, a target once per page.
    """
    known = set(pages) | LIVE_LINK_ROOTS
    found: list[tuple[str, str]] = []
    for path in sorted(pages):
        page = pages[path]
        if page.kind == "legacy":
            continue
        seen: set[str] = set()
        related = [entry.split("#", 1)[0] or entry for entry in page.meta.get("related", [])]
        for target in [*wiki_links(page.body), *related]:
            if target not in known and target not in seen:
                seen.add(target)
                found.append((path, target))
    return found


# ---- the hash --------------------------------------------------------------------


def hash_pages(pages: Mapping[str, WikiPage]) -> str:
    """sha256 over the pages sorted by path: path, title, ``json.dumps(meta, sort_keys=True)``, body, joined by NUL."""
    parts: list[str] = []
    for path in sorted(pages):
        page = pages[path]
        parts += [path, page.title, json.dumps(page.meta, sort_keys=True), page.body]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


# ---- the build ---------------------------------------------------------------------


def _used_in(source: Mapping[str, WikiPage]) -> dict[str, list[str]]:
    """Reference page path -> the guide and section pages that link to it."""
    linked_from: dict[str, set[str]] = defaultdict(set)
    for page in source.values():
        if page.kind not in ("guide", "section"):
            continue
        for target in wiki_links(page.body):
            if target.startswith(f"{REFERENCE_TOOLS}/"):
                linked_from[target].add(page.path)
    return {target: sorted(paths) for target, paths in linked_from.items()}


def _children(pages: Sequence[WikiPage]) -> dict[str, list[WikiPage]]:
    """Parent path (``""`` for the top level) -> its direct children."""
    children: dict[str, list[WikiPage]] = defaultdict(list)
    for page in pages:
        children[page.path.rpartition("/")[0]].append(page)
    return children


def _with_pages_section(page: WikiPage, children: Sequence[WikiPage]) -> WikiPage:
    listing = [page_line(child) for child in sorted(children, key=sibling_key)] or ["_(none)_"]
    body = "\n".join([page.body.rstrip("\n"), "", "## Pages", "", *listing]) + "\n"
    return replace(page, body=body, meta=dict(page.meta))


def _contents(parent: str, children: Mapping[str, Sequence[WikiPage]], depth: int = 0) -> Iterator[str]:
    for child in sorted(children.get(parent, ()), key=sibling_key):
        yield page_line(child, depth=depth)
        if child.kind == "section":
            yield from _contents(child.path, children, depth + 1)


def _table_of_contents(
    source: WikiPage | None,
    children: Mapping[str, Sequence[WikiPage]],
    reference_root: WikiPage,
    legacy: Mapping[str, WikiPage],
) -> WikiPage:
    title = source.title if source else TOC_TITLE
    summary = source.summary if source else TOC_SUMMARY
    intro = source.body.rstrip("\n") if source else f"# {title}"
    lines = [
        intro, "", "## Contents", "",
        *_contents("", children),
        page_line(reference_root),
        f"- [This install]({LIVE_ROOT}) - {LIVE_SUMMARY}",
    ]
    roots = sorted(path for path in legacy if "/" not in path)
    if roots:
        lines += ["", "## Legacy (being replaced)", "", *(page_line(legacy[path]) for path in roots)]
    meta = dict(source.meta) if source else {"kind": "toc", "summary": summary}
    return WikiPage(
        path=TOC_PATH, title=title, summary=summary, body="\n".join(lines) + "\n", kind="toc", meta=meta,
    )


def _refuse_reserved_paths(source: Mapping[str, WikiPage]) -> None:
    for path in source:
        for root, what in _RESERVED_ROOTS:
            if path == root or path.startswith(f"{root}/"):
                raise WikiSourceError(
                    f"{path}: the path {root!r} is reserved for {what}, so a hand-written page cannot sit there "
                    f"({path}.md or {path}/index.md)"
                )


def build_wiki(
    source_root: Path,
    entries: Sequence[ToolEntryView],
    *,
    legacy_pages: Mapping[str, WikiPage] | None = None,
) -> WikiBuild:
    """Build every page of the wiki (but the live install map) and the hash over them.

    ``legacy_pages`` are kept verbatim, listed in the table of contents under their roots (the
    pages whose path has no ``/``) and left out of the link check.

    A missing or empty source folder is not an error here (the build then holds the table of
    contents, the reference and the legacy pages only) but it is logged, and the caller must not
    let such a build replace a stored wiki that has hand-written pages: they are the pages the
    build would not contain, so writing it would delete them.
    """
    source = load_source_pages(source_root)
    if not source:
        logger.warning(
            "wiki: no hand-written pages under %s (the folder is missing or empty); "
            "this build holds only the generated and legacy pages",
            source_root,
        )
    _refuse_reserved_paths(source)
    generated = reference_pages(entries, used_in=_used_in(source))

    authored = {path: page for path, page in source.items() if path != TOC_PATH}
    children = _children(list(authored.values()))
    pages: dict[str, WikiPage] = {
        path: _with_pages_section(page, children.get(path, ())) if page.kind == "section" else page
        for path, page in authored.items()
    }
    pages.update(generated)
    legacy = dict(legacy_pages or {})
    pages[TOC_PATH] = _table_of_contents(source.get(TOC_PATH), children, generated[REFERENCE_ROOT], legacy)
    for path in legacy:
        if path in pages:
            raise WikiSourceError(f"{path}: two pages claim this path (a legacy page and another page)")
    pages.update(legacy)

    pages = dict(sorted(pages.items()))
    for path, target in unresolved_links(pages):
        logger.warning("wiki: page %r links to %r, which is not a wiki page", path, target)
    return WikiBuild(pages=pages, content_hash=hash_pages(pages))


__all__ = [
    "LIVE_LINK_ROOTS",
    "LIVE_ROOT",
    "TOC_PATH",
    "WikiBuild",
    "build_wiki",
    "hash_pages",
    "unresolved_links",
    "wiki_links",
]
