"""User-facing documentation service.

Walks ``primer/user_docs/`` at startup, parses YAML frontmatter, holds
an in-memory index with mtime-based hot-reload. Used by the
``/v1/user_docs`` REST routes (defined in
``primer.api.routers.user_docs``).

See ``docs/superpowers/specs/2026-06-04-user-documentation-system-design.md``
for the full design.

Phase 1: parser + skeleton; service walk lands in Task 1.2; REST router
in Task 1.3; lint in Phase 2.
"""

from __future__ import annotations

from typing import Any

import yaml


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


__all__ = ["FrontmatterError", "parse_frontmatter"]
