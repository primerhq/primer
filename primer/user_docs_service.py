"""User-facing documentation service.

Walks ``primer/user_docs/`` at startup, parses YAML frontmatter, holds
an in-memory index with mtime-based hot-reload. Used by the
``/v1/user_docs`` REST routes (defined in
``primer.api.routers.user_docs``).

See ``docs/superpowers/specs/2026-06-04-user-documentation-system-design.md``
for the full design.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)


class FrontmatterError(ValueError):
    """Raised when a doc file's YAML frontmatter cannot be parsed."""


def parse_frontmatter(src: str) -> tuple[dict[str, Any], str]:
    """Split a markdown source into ``(frontmatter_dict, body)``.

    Recognises the conventional ``---\\n...\\n---\\n`` block at the very
    start of the file. Returns ``({}, src)`` when no frontmatter is
    present. Raises :class:`FrontmatterError` when the opening fence is
    present but the closing fence is missing, or when the YAML between
    the fences is malformed.
    """
    if not src.startswith("---\n"):
        return {}, src
    rest = src[4:]
    end = rest.find("\n---\n")
    if end == -1:
        if rest.endswith("\n---"):
            end = len(rest) - 4
        else:
            raise FrontmatterError(
                "unclosed frontmatter: expected '---' on its own line "
                "to close the block"
            )
    fm_text = rest[:end]
    body = rest[end + 5:]
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"invalid YAML in frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise FrontmatterError(
            f"frontmatter must be a YAML mapping, got "
            f"{type(data).__name__}"
        )
    return data, body


_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
_ANCHOR_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slugify_anchor(text: str) -> str:
    """Auto-generate a heading anchor matching the spec's rule:
    lowercased, non-alnum collapsed to '-', stripped."""
    s = _ANCHOR_NON_ALNUM.sub("-", text.lower()).strip("-")
    return s or "section"


def _extract_headings(body: str) -> list[dict[str, Any]]:
    """Pull h2 + h3 headings out of the markdown body, in document order.
    Skips headings that fall inside fenced code blocks."""
    out: list[dict[str, Any]] = []
    in_fence = False
    for line in body.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        hashes, text = m.group(1), m.group(2)
        level = len(hashes)
        out.append({
            "level": level,
            "text": text,
            "anchor": _slugify_anchor(text),
        })
    return out


@dataclass
class DocEntry:
    """One indexed user-doc file."""

    slug: str
    section: str
    title: str
    summary: str
    body: str
    frontmatter: dict[str, Any]
    headings: list[dict[str, Any]]
    path: Path
    mtime: float = field(default=0.0)


class UserDocsService:
    """Walks ``root/`` (typically ``primer/user_docs/``), parses every
    ``*.md`` file, holds an in-memory index keyed by ``<section>/<slug>``,
    and re-reads individual files when their mtime advances.

    The manifest at ``root/manifest.yaml`` controls section ordering
    and visible-doc membership. Docs on disk but not listed in the
    manifest are still indexed (reachable by direct slug) but excluded
    from :meth:`list_sections`.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._entries: dict[str, DocEntry] = {}
        self._manifest: dict[str, Any] = {"sections": []}
        self._manifest_mtime: float = 0.0
        self._lint_issues: list = []
        self._embeds_manifest: list[str] = []

    def reload_index(self) -> None:
        """Re-walk the tree from scratch. Called at startup and from
        :meth:`list_sections` when the manifest changes on disk."""
        self._manifest = self._load_manifest()
        self._entries.clear()
        if not self._root.exists():
            self._lint_issues = []
            return
        for path in sorted(self._root.rglob("*.md")):
            try:
                entry = self._load_entry(path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "user_docs: failed to index %s: %s: %s",
                    path, type(exc).__name__, exc,
                )
                continue
            self._entries[entry.slug] = entry
        # Run lint after every reload. The embeds_manifest is initialised
        # empty; the lifespan handler calls set_embeds_manifest() once
        # the registry is wired so rule 3 (unknown_embed_id) sees the
        # live allowlist.
        try:
            from primer.user_docs_lint import run_lint
            self._lint_issues = run_lint(
                self, embeds_manifest=self._embeds_manifest,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("user_docs: lint runner failed: %s", exc)
            self._lint_issues = []

    def lint_issues(self) -> list:
        """Return the most recent lint pass's findings."""
        return list(self._lint_issues)

    def set_embeds_manifest(self, ids: list[str]) -> None:
        """Update the embed-id allowlist used by lint rule 3 and re-run
        lint. Called from the lifespan handler once the live React
        embed registry is wired."""
        self._embeds_manifest = list(ids)
        try:
            from primer.user_docs_lint import run_lint
            self._lint_issues = run_lint(
                self, embeds_manifest=self._embeds_manifest,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("user_docs: re-lint failed: %s", exc)

    def get_ai_doc(self, slug: str) -> dict[str, Any] | None:
        """Return the AI-doc at docs/agents/<slug>.md, parsed the
        same way as a user doc. Returns None when the file does not
        exist. Used by the /v1/user_docs/_ai/<slug> mirror route."""
        from primer.ai_docs_path import resolve_ai_docs_dir

        ai_root = resolve_ai_docs_dir()
        path = ai_root / f"{slug}.md"
        if not path.exists():
            return None
        src = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(src)
        return {
            "slug": slug,
            "title": fm.get("title", slug),
            "summary": fm.get("summary", ""),
            "section": "_ai",
            "source": body,
            "frontmatter": fm,
            "headings": _extract_headings(body),
        }

    def _load_manifest(self) -> dict[str, Any]:
        mpath = self._root / "manifest.yaml"
        if not mpath.exists():
            return {"sections": []}
        try:
            self._manifest_mtime = mpath.stat().st_mtime
            data = yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("user_docs: manifest parse failed: %s", exc)
            return {"sections": []}
        if not isinstance(data, dict) or "sections" not in data:
            return {"sections": []}
        return data

    def _load_entry(self, path: Path) -> DocEntry:
        rel = path.relative_to(self._root)
        section = rel.parts[0]
        slug_basename = path.stem
        slug = f"{section}/{slug_basename}"
        src = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(src)
        return DocEntry(
            slug=slug,
            section=fm.get("section", section),
            title=fm.get("title", slug_basename),
            summary=fm.get("summary", ""),
            body=body,
            frontmatter=fm,
            headings=_extract_headings(body),
            path=path,
            mtime=path.stat().st_mtime,
        )

    def get_doc(self, slug: str) -> DocEntry | None:
        """Return the entry for ``slug``, hot-reloading from disk if the
        source mtime advanced since last load. Returns ``None`` when the
        slug is unknown."""
        entry = self._entries.get(slug)
        if entry is None:
            return None
        try:
            current_mtime = entry.path.stat().st_mtime
        except FileNotFoundError:
            self._entries.pop(slug, None)
            return None
        if current_mtime > entry.mtime:
            try:
                fresh = self._load_entry(entry.path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "user_docs: hot-reload failed for %s: %s", slug, exc,
                )
                return entry
            self._entries[slug] = fresh
            return fresh
        return entry

    def _doc_meta(self, full_slug: str) -> dict[str, Any] | None:
        """Resolve a full ``<section>/<slug>`` to its metadata dict, or
        ``None`` when the slug is not indexed."""
        e = self._entries.get(full_slug)
        if e is None:
            return None
        return {
            "slug": e.slug,
            "title": e.title,
            "summary": e.summary,
            "section": e.section,
            "headings": e.headings,
            "tags": e.frontmatter.get("tags", []),
            "difficulty": e.frontmatter.get("difficulty"),
            "time_minutes": e.frontmatter.get("time_minutes"),
            "features": e.frontmatter.get("features", []),
        }

    def list_sections(self) -> list[dict[str, Any]]:
        """Return the section tree from ``manifest.yaml`` joined with
        each listed doc's metadata. Re-walks the index if the manifest's
        mtime advanced.

        A section may list its docs in either of two shapes:

        - ``docs``: an ordered list of slug basenames resolved under the
          section id (the flat shape; getting-started/cookbook/reference).
        - ``items``: an ordered list of either a full-slug leaf string or
          a group mapping ``{title, overview, children}`` (the nested
          two-level shape; used by the Features section). Group children
          and the overview are full ``<section>/<slug>`` slugs and may
          live in any physical directory.

        Leaves resolve to a doc-meta dict (as in the flat shape). A group
        resolves to ``{"group": True, "title", "overview": <meta|None>,
        "children": [<meta>, ...]}``. Unresolved slugs are skipped, as
        before.
        """
        mpath = self._root / "manifest.yaml"
        if mpath.exists():
            try:
                cur = mpath.stat().st_mtime
                if cur > self._manifest_mtime:
                    self.reload_index()
            except FileNotFoundError:
                pass

        out: list[dict[str, Any]] = []
        for sec in self._manifest.get("sections", []):
            sid = sec.get("id", "")
            section_node: dict[str, Any] = {
                "id": sid,
                "title": sec.get("title", sid.title()),
                "icon": sec.get("icon", "doc"),
                "order": sec.get("order", 0),
                "docs": [],
            }

            items = sec.get("items")
            if items is not None:
                for item in items or []:
                    if isinstance(item, str):
                        meta = self._doc_meta(item)
                        if meta is not None:
                            section_node["docs"].append(meta)
                        continue
                    if not isinstance(item, dict):
                        continue
                    children: list[dict[str, Any]] = []
                    for child_slug in item.get("children", []) or []:
                        child_meta = self._doc_meta(child_slug)
                        if child_meta is not None:
                            children.append(child_meta)
                    overview_slug = item.get("overview")
                    section_node["docs"].append({
                        "group": True,
                        "title": item.get("title", ""),
                        "overview": (
                            self._doc_meta(overview_slug)
                            if overview_slug else None
                        ),
                        "children": children,
                    })
                out.append(section_node)
                continue

            for slug_basename in sec.get("docs", []) or []:
                meta = self._doc_meta(f"{sid}/{slug_basename}")
                if meta is not None:
                    section_node["docs"].append(meta)
            out.append(section_node)
        return out

    def all_entries(self) -> list[DocEntry]:
        """Every indexed entry, in slug-sorted order. Used by the lint
        runner."""
        return [self._entries[k] for k in sorted(self._entries)]


__all__ = [
    "DocEntry",
    "FrontmatterError",
    "UserDocsService",
    "parse_frontmatter",
]
