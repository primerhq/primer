"""Hand-written wiki pages: the frontmatter rules and the file-to-path mapping.

One page per ``.md`` file under ``docs/wiki/``. A page opens with a YAML
frontmatter block between two ``---`` lines (``title`` and ``summary`` are
required, ``audience``, ``tools``, ``related`` and ``order`` are optional, any
other key is an error). The body must not open with a ``# `` heading: the build
renders ``# <title>`` from the frontmatter.

Every problem raises :class:`WikiSourceError`, whose message names the file and
the rule it broke, so the boot can log it and keep the stored wiki.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

import primer
from primer.knowledge.tree import STRICT_SLUG_RE

WIKI_DIR_ENV = "PRIMER_WIKI_DIR"

SUMMARY_MAX = 160
"""The longest one-line summary a page may carry."""

PageKind = Literal["toc", "section", "guide", "reference", "live", "legacy"]

_FRONTMATTER_KEYS = ("title", "summary", "audience", "tools", "related", "order")
_AUDIENCES = ("user", "admin")
_HEADING_RE = re.compile(r"#(?:[ \t]|$)")


class WikiSourceError(ValueError):
    """A wiki source file or folder breaks a rule; the message names the file and the rule."""


@dataclass(frozen=True)
class WikiPage:
    """One page of the wiki, as it will be stored.

    ``body`` opens with ``# <title>``. ``meta`` always holds ``kind`` and
    ``summary`` plus the optional frontmatter keys the page set; it is what the
    document's ``meta`` becomes.
    """

    path: str
    title: str
    summary: str
    body: str
    kind: PageKind
    meta: dict[str, Any]


def resolve_wiki_dir() -> Path:
    """``$PRIMER_WIKI_DIR`` when set, else ``docs/wiki`` beside the ``primer`` package.

    The image copies ``docs/`` next to the package (``Dockerfile``: ``COPY docs ./docs``),
    so the default holds in a deployment as well as in a checkout.
    """
    env = os.getenv(WIKI_DIR_ENV)
    if env:
        return Path(env)
    return Path(primer.__file__).resolve().parent.parent / "docs" / "wiki"


def _split_frontmatter(text: str, label: str) -> tuple[str, str]:
    lines = text.lstrip("﻿").replace("\r\n", "\n").split("\n")
    if lines[0].rstrip() != "---":
        raise WikiSourceError(
            f"{label}: the page must start with a frontmatter block (YAML between two '---' lines)"
        )
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1:])
    raise WikiSourceError(f"{label}: the frontmatter block is not closed (no second '---' line)")


def _one_line(data: dict[Any, Any], key: str, label: str) -> str:
    value = data.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise WikiSourceError(f"{label}: {key} is required in the frontmatter")
    if not isinstance(value, str):
        raise WikiSourceError(f"{label}: {key} must be a string (quote it if YAML reads it as something else)")
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise WikiSourceError(f"{label}: {key} must be one line")
    return value


def _string_list(value: Any, key: str, what: str, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise WikiSourceError(f"{label}: {key} must be a list of {what} (strings)")
    return [item.strip() for item in value]


def parse_page_source(
    text: str, *, path: str, kind: PageKind = "guide", source: str | None = None,
) -> WikiPage:
    """Parse one page file's text into a :class:`WikiPage`.

    ``source`` names the file in error messages (``start-here/broken.md``); it
    defaults to the page ``path``.
    """
    label = source or path
    front, rest = _split_frontmatter(text, label)
    try:
        data = yaml.safe_load(front)
    except yaml.YAMLError as exc:
        raise WikiSourceError(f"{label}: the frontmatter is not valid YAML: {' '.join(str(exc).split())}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise WikiSourceError(f"{label}: the frontmatter must be a mapping of keys to values")
    for key in data:
        if key not in _FRONTMATTER_KEYS:
            raise WikiSourceError(
                f"{label}: unknown key {key!r} in the frontmatter (allowed: {', '.join(_FRONTMATTER_KEYS)})"
            )
    title = _one_line(data, "title", label)
    summary = _one_line(data, "summary", label)
    if len(summary) > SUMMARY_MAX:
        raise WikiSourceError(f"{label}: summary must be at most {SUMMARY_MAX} characters (it has {len(summary)})")

    meta: dict[str, Any] = {"kind": kind, "summary": summary}
    if data.get("audience") is not None:
        if data["audience"] not in _AUDIENCES:
            raise WikiSourceError(f"{label}: audience must be 'user' or 'admin' (got {data['audience']!r})")
        meta["audience"] = data["audience"]
    if data.get("tools") is not None:
        meta["tools"] = _string_list(data["tools"], "tools", "scoped tool ids", label)
    if data.get("related") is not None:
        meta["related"] = _string_list(data["related"], "related", "wiki paths", label)
    if data.get("order") is not None:
        order = data["order"]
        if not isinstance(order, int) or isinstance(order, bool):
            raise WikiSourceError(f"{label}: order must be an integer (got {order!r})")
        meta["order"] = order

    body = rest.strip()
    first_line = body.split("\n", 1)[0]
    if _HEADING_RE.match(first_line):
        raise WikiSourceError(
            f"{label}: the body must not start with a '# ' heading; "
            f"the build renders '# {title}' from the frontmatter title"
        )
    return WikiPage(
        path=path, title=title, summary=summary, body=f"# {title}\n\n{body}\n", kind=kind, meta=meta,
    )


def load_source_pages(root: Path) -> dict[str, WikiPage]:
    """Read every page under ``root``: ``{page path: page}``, sorted by path.

    ``root/index.md`` is the page ``index`` (kind ``toc``); ``<folder>/index.md`` is the
    page ``<folder>`` (kind ``section``); any other file is a ``guide`` at its path without
    ``.md``. Files whose name starts with ``_`` or ``.``, folders whose name starts with
    ``.``, and files that are not ``.md`` are not pages. A root that does not exist is an
    empty wiki.
    """
    root = Path(root)
    if not root.exists():
        return {}
    if not root.is_dir():
        raise WikiSourceError(f"{root}: the wiki source path is not a directory")

    found: dict[str, tuple[Path, str, PageKind]] = {}
    folders_needing_index: set[tuple[str, ...]] = set()
    folders_with_index: set[tuple[str, ...]] = set()
    for file in sorted(root.rglob("*.md")):
        rel = file.relative_to(root)
        parts = rel.parts
        if not file.is_file() or parts[-1].startswith("_") or any(part.startswith(".") for part in parts):
            continue
        folder = parts[:-1]
        stem = parts[-1][: -len(".md")]
        for segment in (*folder, stem):
            if STRICT_SLUG_RE.fullmatch(segment) is None:
                raise WikiSourceError(f"{rel.as_posix()}: path segment {segment!r} must match [a-z0-9-]+")
        if stem == "index":
            segments = folder
            kind: PageKind = "section" if folder else "toc"
            folders_with_index.add(folder)
        else:
            segments = (*folder, stem)
            kind = "guide"
        path = "/".join(segments) or "index"
        if path in found:
            raise WikiSourceError(
                f"{path}: two files define this page ({found[path][1]} and {rel.as_posix()})"
            )
        found[path] = (file, rel.as_posix(), kind)
        folders_needing_index.update(folder[:depth] for depth in range(len(folder) + 1))

    for folder in sorted(folders_needing_index):
        if folder not in folders_with_index:
            name = "/".join(folder) if folder else str(root)
            raise WikiSourceError(f"{name}: a folder needs an index.md")

    pages: dict[str, WikiPage] = {}
    for path in sorted(found):
        file, rel_name, kind = found[path]
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WikiSourceError(f"{rel_name}: the file is not valid UTF-8") from exc
        except OSError as exc:
            raise WikiSourceError(f"{rel_name}: cannot read the file ({exc.strerror or exc})") from exc
        pages[path] = parse_page_source(text, path=path, kind=kind, source=rel_name)
    return pages


__all__ = [
    "SUMMARY_MAX",
    "WIKI_DIR_ENV",
    "PageKind",
    "WikiPage",
    "WikiSourceError",
    "load_source_pages",
    "parse_page_source",
    "resolve_wiki_dir",
]
