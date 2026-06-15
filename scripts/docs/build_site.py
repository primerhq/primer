"""Static docs-site generator.

Renders the user-docs markdown corpus (``primer/user_docs/*.md`` plus
``manifest.yaml``) into a multi-page HTML site using the designer's
mockup shell vendored at ``scripts/docs/site_template/``.

The manifest IA drives both the sidebar nav and the set of pages: each
indexed doc ``<section>/<basename>`` becomes ``<out>/<section>/<basename>/
index.html`` served at the url ``/<section>/<basename>/``.

Usage::

    uv run python -m scripts.docs.build_site <src_root> <out_dir>

For example::

    uv run python -m scripts.docs.build_site primer/user_docs dist
"""

from __future__ import annotations

import html
import logging
import sys
from pathlib import Path
from typing import Any

# Resolve repo root (two levels above this file: scripts/docs/ -> root)
# so the script runs both as ``python -m scripts.docs.build_site`` and
# directly, mirroring scripts/docs/docs_lint.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from primer.user_docs_service import UserDocsService  # noqa: E402

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "site_template"


def _doc_url(slug: str) -> str:
    """Map a full ``<section>/<basename>`` slug to its page url."""
    return f"/{slug}/"


def _slug_url_map(service: UserDocsService) -> dict[str, str]:
    """Build a ``slug -> url`` map covering every indexed doc."""
    return {e.slug: _doc_url(e.slug) for e in service.all_entries()}


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
def _nav_link(slug: str, title: str) -> str:
    return (
        f'<a class="nav-link" href="{_doc_url(slug)}">'
        f"{html.escape(title)}</a>"
    )


def _render_sidebar(sections: list[dict[str, Any]]) -> str:
    """Render the sidebar nav from ``list_sections()``.

    Top-level sections become ``.nav-group`` blocks with a ``.nav-title``
    header. A leaf doc renders as a ``.nav-link``. A group (the nested
    Features shape) renders its title as a link to the group's
    ``overview`` doc followed by an indented ``.nav-link`` list of its
    children.
    """
    parts: list[str] = []
    for sec in sections:
        parts.append('<div class="nav-group">')
        parts.append(f'<div class="nav-title">{html.escape(sec["title"])}</div>')
        for item in sec.get("docs", []) or []:
            if item.get("group"):
                overview = item.get("overview")
                title = item.get("title", "")
                if overview:
                    parts.append(_nav_link(overview["slug"], title))
                else:
                    parts.append(
                        f'<div class="nav-title">{html.escape(title)}</div>'
                    )
                for child in item.get("children", []) or []:
                    parts.append(_nav_link(child["slug"], child["title"]))
            else:
                parts.append(_nav_link(item["slug"], item["title"]))
        parts.append("</div>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Body rendering
# ---------------------------------------------------------------------------
def render_body(md_source: str, slug_url_map: dict[str, str]) -> str:
    """Render a doc body to HTML.

    Placeholder implementation: escapes the raw markdown into a ``<pre>``
    block. The real markdown renderer (with ref resolution) lands in the
    next task. ``slug_url_map`` is accepted now so the call site is
    stable.
    """
    return f"<pre>{html.escape(md_source)}</pre>"


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------
def _breadcrumb(section_title: str, doc_title: str) -> str:
    return (
        '<nav class="breadcrumb">'
        f"<span>{html.escape(section_title)}</span>"
        " / "
        f"<span>{html.escape(doc_title)}</span>"
        "</nav>"
    )


def _section_titles(sections: list[dict[str, Any]]) -> dict[str, str]:
    return {sec["id"]: sec["title"] for sec in sections}


def build_site(src_root: Path, out_dir: Path) -> None:
    """Render the user-docs corpus under ``src_root`` into a static
    multi-page site at ``out_dir``."""
    src_root = Path(src_root)
    out_dir = Path(out_dir)

    service = UserDocsService(src_root)
    service.reload_index()
    sections = service.list_sections()

    slug_url_map = _slug_url_map(service)
    sidebar = _render_sidebar(sections)
    section_titles = _section_titles(sections)

    template = (_TEMPLATE_DIR / "page.html").read_text(encoding="utf-8")

    out_dir.mkdir(parents=True, exist_ok=True)
    for entry in service.all_entries():
        title = entry.title
        section_title = section_titles.get(entry.section, entry.section)
        body_html = render_body(entry.body, slug_url_map)
        article = (
            _breadcrumb(section_title, title)
            + f"<h1>{html.escape(title)}</h1>\n"
            + body_html
        )
        page = (
            template.replace("{{TITLE}}", html.escape(title))
            .replace("{{SIDEBAR}}", sidebar)
            .replace("{{ARTICLE}}", article)
        )
        page_dir = out_dir / Path(entry.slug)
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(page, encoding="utf-8")

    # Assets: the vendored stylesheet plus a placeholder docs.js so the
    # template's <script> tag resolves (the SPA bundle lands in a later
    # phase).
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "docs.css").write_text(
        (_TEMPLATE_DIR / "docs.css").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (assets / "docs.js").write_text("", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        sys.stderr.write(
            "usage: python -m scripts.docs.build_site <src_root> <out_dir>\n"
        )
        return 2
    build_site(Path(args[0]), Path(args[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
