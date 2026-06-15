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
import re
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

# Internal authoring section: writer guidance (authoring-guide, page-template),
# not part of the published site. Excluded from page output, nav, search, sitemap.
_META_SECTION = "_meta"

# ``ref:<section>/<slug>`` cross-link forms. Inline links look like
# ``[text](ref:section/slug)`` (optionally ``#anchor``); the block form
# is a fenced code block whose info string is ``ref:section/slug`` with
# an optional explanatory body. See ui/components/docs/directives-ref.jsx.
_REF_SLUG_RE = r"[A-Za-z0-9][A-Za-z0-9._/#-]*"

# Callout kinds, mirroring ui/components/docs/directives-callout.jsx
# (info/success/warning/danger/tip). Unknown kinds fall back to ``info``.
_CALLOUT_KINDS = ("info", "success", "warning", "danger", "tip")

# Splits a code-tabs body into ``--- <lang>`` sections, matching the
# ``^---\s+(\w+)\s*$`` separator used by directives-code-tabs.jsx.
_CODE_TABS_SECTION_RE = re.compile(r"^---\s+(\w+)\s*$")


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
# Markdown rendering + ref resolution
# ---------------------------------------------------------------------------
def _render_callout(kind: str, body: str, md, slug_url_map: dict[str, str]) -> str:
    """Render a ``callout:<kind>`` box. The body is re-parsed as markdown
    (so callouts can hold lists/links/inline code), mirroring
    directives-callout.jsx. ``ref:`` links inside the body resolve too.
    """
    kind = kind if kind in _CALLOUT_KINDS else "info"
    inner = _rewrite_ref_blocks(body, slug_url_map)
    rendered = _rewrite_inline_refs(md.render(inner), slug_url_map)
    return (
        f'<div class="callout callout-{kind}">'
        f'<div class="callout-title">{html.escape(kind)}</div>'
        f'<div class="callout-body">{rendered}</div>'
        "</div>\n"
    )


def _render_code_tabs(langs_spec: str, body: str) -> str:
    """Render a ``code-tabs:<langs>`` widget as ``.tabs`` markup driven by
    ``wireTabs()`` in docs.js: a row of ``.tab`` buttons and matching
    ``.tab-panel`` blocks. The body is split on ``--- <lang>`` separators,
    matching directives-code-tabs.jsx.
    """
    langs = [s.strip() for s in langs_spec.split(",") if s.strip()]
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is not None:
            sections[current] = "\n".join(buf).strip("\n")

    for line in body.split("\n"):
        m = _CODE_TABS_SECTION_RE.match(line)
        if m:
            flush()
            current = m.group(1)
            buf = []
        elif current is not None:
            buf.append(line)
    flush()

    if not langs:
        return ""

    # Stable, collision-resistant panel ids scoped to this widget.
    uid = f"{abs(hash((langs_spec, body))) & 0xFFFFFF:06x}"
    buttons: list[str] = []
    panels: list[str] = []
    for i, lang in enumerate(langs):
        active = " active" if i == 0 else ""
        panel_id = f"tab-{uid}-{lang}"
        code = html.escape(sections.get(lang, ""))
        buttons.append(
            f'<button class="tab{active}" data-tab="{panel_id}">'
            f"{html.escape(lang)}</button>"
        )
        panels.append(
            f'<div class="tab-panel{active}" id="{panel_id}">'
            f'<pre class="md-pre lang-{html.escape(lang)}"><code>{code}</code></pre>'
            "</div>"
        )
    return (
        '<div class="tabs">'
        f'<div class="tab-row">{"".join(buttons)}</div>'
        f'{"".join(panels)}'
        "</div>\n"
    )


def _render_mermaid(body: str) -> str:
    """Render a ``mermaid`` block as ``<pre class="mermaid">`` carrying the
    diagram source; docs.js runs ``mermaid.run()`` over these on load.
    """
    return f'<pre class="mermaid">{html.escape(body.strip())}</pre>\n'


def _render_ai_doc(slug: str) -> str:
    """Render an ``ai-doc:<slug>`` reference. In the console this linked to
    the in-app AI-doc mirror (``/docs/_ai/<slug>``); that route does not
    exist on the static site (the AI-doc mirror is a console-only feature),
    so emit a NON-linking labelled note rather than a dead link, preserving
    the authoring cue from directives-ai-doc.jsx without a broken target.
    """
    return (
        '<div class="ai-doc">'
        '<div class="ai-doc-label">Agent-facing reference</div>'
        f'<div class="ai-doc-slug">{html.escape(slug)}</div>'
        "</div>\n"
    )


def _render_embed(embed_id: str) -> str:
    """Render an ``embed:<id>`` fence as a theme-aware screenshot figure.

    The live console component was captured to light+dark PNGs by
    scripts/docs/capture_embeds.py (under ``<out>/_embeds/<id>-<theme>.png``).
    We emit a ``<picture>`` so the dark variant is served under a
    ``prefers-color-scheme: dark`` query, falling back to the light variant.
    """
    eid = embed_id.strip()
    safe = html.escape(eid)
    return (
        '<figure class="embed">'
        "<picture>"
        f'<source srcset="/_embeds/{safe}-dark.png" media="(prefers-color-scheme: dark)">'
        f'<img src="/_embeds/{safe}-light.png" alt="{safe} (live component)" loading="lazy">'
        "</picture>"
        '<figcaption>Live component - open it in your console.</figcaption>'
        "</figure>\n"
    )


def _make_md(slug_url_map: dict[str, str]):
    from markdown_it import MarkdownIt
    from mdit_py_plugins.anchors import anchors_plugin

    md = MarkdownIt("commonmark", {"html": False, "linkify": True})
    md.enable("table")
    md.use(anchors_plugin, max_level=3)

    default_fence = md.renderer.rules.get("fence")

    def fence(tokens, idx, options, env):
        """Dispatch directive fences (callout/code-tabs/mermaid/ai-doc/embed)
        to their static-HTML renderers; everything else falls through to the
        normal code-block renderer. ``ref:`` fences are pre-rewritten in
        ``render_markdown`` before this runs.
        """
        info = tokens[idx].info.strip()
        content = tokens[idx].content
        if info == "mermaid":
            return _render_mermaid(content)
        if info.startswith("callout:"):
            return _render_callout(
                info[len("callout:"):], content, md, slug_url_map
            )
        if info.startswith("code-tabs:"):
            return _render_code_tabs(info[len("code-tabs:"):], content)
        if info.startswith("ai-doc:"):
            return _render_ai_doc(info[len("ai-doc:"):])
        if info.startswith("embed:"):
            return _render_embed(info[len("embed:"):])
        if default_fence is not None:
            return default_fence(tokens, idx, options, env)
        return md.renderer.renderToken(tokens, idx, options)

    md.renderer.rules["fence"] = fence
    return md


def _resolve_ref(target: str, slug_url_map: dict[str, str]) -> str:
    """Resolve a ``<slug>[#anchor]`` ref target to its page url, raising
    ``KeyError`` (after logging) when the slug is unknown."""
    slug, _, anchor = target.partition("#")
    url = slug_url_map.get(slug)
    if url is None:
        logger.warning("docs build: unresolved ref slug %r", slug)
        raise KeyError(f"unresolved ref slug: {slug}")
    return f"{url}#{anchor}" if anchor else url


def _rewrite_ref_blocks(md_source: str, slug_url_map: dict[str, str]) -> str:
    """Turn ```ref:<slug>``` fenced blocks into a markdown link.

    The fence info string is ``ref:<slug>[#anchor]``; the block body (if
    any) is a one-line note. We rewrite the whole block to an inline link
    so the standard renderer produces a normal anchor.
    """
    fence = re.compile(
        r"^```ref:(?P<target>" + _REF_SLUG_RE + r")[ \t]*\n"
        r"(?P<body>.*?)"
        r"^```[ \t]*$",
        re.MULTILINE | re.DOTALL,
    )

    def repl(m: re.Match[str]) -> str:
        url = _resolve_ref(m.group("target"), slug_url_map)
        note = (m.group("body") or "").strip()
        text = note or m.group("target")
        return f"[{text}]({url})\n"

    return fence.sub(repl, md_source)


def _rewrite_inline_refs(html_out: str, slug_url_map: dict[str, str]) -> str:
    """Rewrite ``href="ref:<slug>[#anchor]"`` produced by inline
    ``[text](ref:slug)`` links into the resolved page url."""
    href = re.compile(r'href="ref:(?P<target>' + _REF_SLUG_RE + r')"')

    def repl(m: re.Match[str]) -> str:
        return f'href="{_resolve_ref(m.group("target"), slug_url_map)}"'

    return href.sub(repl, html_out)


def render_markdown(md_source: str, slug_url_map: dict[str, str]) -> str:
    """Render ``md_source`` to HTML, resolving every ``ref:<slug>``
    cross-link (both the inline-link and fenced-block forms) to a real
    page url via ``slug_url_map``. Headings (h2/h3) get stable ``id``
    anchors. Raises ``KeyError`` on an unknown ref slug.
    """
    md = _make_md(slug_url_map)
    pre = _rewrite_ref_blocks(md_source, slug_url_map)
    rendered = md.render(pre)
    return _rewrite_inline_refs(rendered, slug_url_map)


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
        # Skip internal authoring docs: the _meta section (authoring-guide,
        # page-template) is writer guidance, not public documentation. It is
        # absent from the nav and must not be published, indexed, or sitemapped.
        if entry.section == _META_SECTION:
            continue
        title = entry.title
        section_title = section_titles.get(entry.section, entry.section)
        body_html = render_markdown(entry.body, slug_url_map)
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
    (assets / "docs.js").write_text(
        (_TEMPLATE_DIR / "docs.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )


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
