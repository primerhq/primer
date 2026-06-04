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

    def reload_index(self) -> None:
        """Re-walk the tree from scratch. Called at startup and from
        :meth:`list_sections` when the manifest changes on disk."""
        self._manifest = self._load_manifest()
        self._entries.clear()
        if not self._root.exists():
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

    def list_sections(self) -> list[dict[str, Any]]:
        """Return the section tree from ``manifest.yaml`` joined with
        each listed doc's metadata. Re-walks the index if the manifest's
        mtime advanced."""
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
            for slug_basename in sec.get("docs", []) or []:
                full_slug = f"{sid}/{slug_basename}"
                e = self._entries.get(full_slug)
                if e is None:
                    continue
                section_node["docs"].append({
                    "slug": e.slug,
                    "title": e.title,
                    "summary": e.summary,
                    "section": e.section,
                    "headings": e.headings,
                    "tags": e.frontmatter.get("tags", []),
                    "difficulty": e.frontmatter.get("difficulty"),
                    "time_minutes": e.frontmatter.get("time_minutes"),
                    "features": e.frontmatter.get("features", []),
                })
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
