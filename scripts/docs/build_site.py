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
import json
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


# ---------------------------------------------------------------------------
# 404 + sitemap
# ---------------------------------------------------------------------------
def _render_404(template: str, sidebar: str, home_url: str) -> str:
    """Render a friendly 404 page using the standard page shell.

    Uses the same sidebar nav as every page and points back at the docs
    home so a mistyped url still lands somewhere navigable.
    """
    article = (
        '<nav class="breadcrumb"><span>Docs</span> / '
        "<span>Not found</span></nav>"
        "<h1>Page not found</h1>\n"
        "<p>The page you were looking for does not exist or has moved.</p>\n"
        f'<p><a href="{html.escape(home_url)}">Back to the docs home</a></p>\n'
    )
    return (
        template.replace("{{TITLE}}", "Page not found")
        .replace("{{SIDEBAR}}", sidebar)
        .replace("{{ARTICLE}}", article)
    )


def _first_nav_slug(sections: list[dict[str, Any]]) -> str | None:
    """Return the slug of the first doc in nav order: the docs home.

    Mirrors the order ``_render_sidebar`` walks, so the home page is the
    first link a reader sees in the sidebar (the Getting Started intro),
    not whatever ``all_entries()`` happens to yield first.
    """
    for sec in sections:
        for item in sec.get("docs", []) or []:
            if item.get("group"):
                overview = item.get("overview")
                if overview:
                    return overview["slug"]
                for child in item.get("children", []) or []:
                    return child["slug"]
            else:
                return item["slug"]
    return None


def _render_root_redirect(home_url: str) -> str:
    """Render a minimal root ``index.html`` that redirects to the docs home.

    When the site is served at a domain root (e.g.
    ``https://<org>.github.io/``) the bare root would otherwise 404, since
    every page lives under ``/<section>/<slug>/``. This emits a tiny
    meta-refresh + JS redirect (with a plain link fallback) to the first
    published page.
    """
    href = html.escape(home_url)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="refresh" content="0; url={href}">\n'
        f'<link rel="canonical" href="{href}">\n'
        "<title>Primer docs</title>\n"
        f"<script>location.replace({json.dumps(home_url)});</script>\n"
        "</head>\n"
        f'<body><p>Redirecting to <a href="{href}">the documentation</a>.</p></body>\n'
        "</html>\n"
    )


def _render_sitemap(page_urls: list[str]) -> str:
    """Render a sitemap urlset listing every published page url.

    Locs are root-relative absolute paths (``/section/slug/``); keeping
    them origin-free means the same sitemap works regardless of where the
    site is hosted.
    """
    locs = "\n".join(
        f"  <url><loc>{html.escape(u)}</loc></url>" for u in page_urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{locs}\n"
        "</urlset>\n"
    )


# ---------------------------------------------------------------------------
# Search index
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_EXCERPT_LEN = 200


def _strip_html(html_out: str) -> str:
    """Reduce rendered HTML to a single line of plain text: drop every tag
    then unescape entities and collapse runs of whitespace. Good enough for
    a search excerpt (not a fidelity-preserving conversion)."""
    text = _TAG_RE.sub(" ", html_out)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _excerpt(body_html: str) -> str:
    """First ``_EXCERPT_LEN`` chars of the page body as plain text."""
    text = _strip_html(body_html)
    if len(text) <= _EXCERPT_LEN:
        return text
    return text[:_EXCERPT_LEN].rstrip() + "..."


def _search_entry(entry: Any, body_html: str) -> dict[str, Any]:
    """Build one client-search-index record for a published doc.

    ``section`` is the section id (e.g. ``features``); the client uses it
    as a compact source label beside each result.
    """
    return {
        "title": entry.title,
        "section": entry.section,
        "url": _doc_url(entry.slug),
        "headings": [h["text"] for h in entry.headings],
        "excerpt": _excerpt(body_html),
    }


class DocsLintError(RuntimeError):
    """Raised when the corpus fails the build-time lint gate."""


def _run_lint_gate(src_root: Path) -> None:
    """Run the doc-corpus lint (frontmatter / ref+embed resolution /
    em-dash / ...) and raise on any error-severity issue.

    Reuses ``scripts.docs.docs_lint`` (the same checks the standalone
    linter and the FastAPI lifespan run) rather than duplicating them, so
    the static build cannot publish a corpus the linter would reject. Warnings
    are non-blocking. A broken ``ref:`` or an ``embed:`` id missing from
    ``_fixtures/registry.json`` is an error and therefore fails the build.
    """
    from scripts.docs.docs_lint import lint_corpus

    issues = lint_corpus(src_root)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        detail = "\n".join(
            f"  {i.file}"
            + (f":{i.line}" if i.line is not None else "")
            + f": {i.rule}: {i.message}"
            for i in sorted(errors, key=lambda i: (i.file, i.line or 0, i.rule))
        )
        raise DocsLintError(
            f"docs lint gate failed with {len(errors)} error(s):\n{detail}"
        )


def build_site(src_root: Path, out_dir: Path) -> None:
    """Render the user-docs corpus under ``src_root`` into a static
    multi-page site at ``out_dir``.

    Before rendering, the corpus is run through the doc lint gate; any
    error-severity issue (bad frontmatter, unresolved ``ref:`` slug,
    unregistered ``embed:`` id, em-dash, ...) aborts the build.
    """
    src_root = Path(src_root)
    out_dir = Path(out_dir)

    # Fail fast on a corpus the linter would reject (before writing files).
    _run_lint_gate(src_root)

    service = UserDocsService(src_root)
    service.reload_index()
    sections = service.list_sections()

    slug_url_map = _slug_url_map(service)
    sidebar = _render_sidebar(sections)
    section_titles = _section_titles(sections)

    template = (_TEMPLATE_DIR / "page.html").read_text(encoding="utf-8")

    out_dir.mkdir(parents=True, exist_ok=True)
    search_index: list[dict[str, Any]] = []
    page_urls: list[str] = []
    for entry in service.all_entries():
        # Skip internal authoring docs: the _meta section (authoring-guide,
        # page-template) is writer guidance, not public documentation. It is
        # absent from the nav and must not be published, indexed, or sitemapped.
        if entry.section == _META_SECTION:
            continue
        title = entry.title
        section_title = section_titles.get(entry.section, entry.section)
        body_html = render_markdown(entry.body, slug_url_map)
        search_index.append(_search_entry(entry, body_html))
        page_urls.append(_doc_url(entry.slug))
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

    # Prebuilt client search index: one compact record per published doc,
    # fetched by docs.js to power topbar search. Kept small (title +
    # section + url + heading texts + a short plain-text excerpt).
    (out_dir / "search-index.json").write_text(
        json.dumps(search_index, ensure_ascii=False),
        encoding="utf-8",
    )

    # 404 page: the page shell + a friendly "not found" article linking
    # back to the docs home (the first published page, else "/").
    home_slug = _first_nav_slug(sections)
    home_url = (
        _doc_url(home_slug)
        if home_slug
        else (page_urls[0] if page_urls else "/")
    )
    (out_dir / "404.html").write_text(
        _render_404(template, sidebar, home_url),
        encoding="utf-8",
    )

    # Root landing page: redirect the site root to the docs home (first
    # published page) so serving at a domain root does not 404.
    (out_dir / "index.html").write_text(
        _render_root_redirect(home_url),
        encoding="utf-8",
    )

    # sitemap.xml: every published page url (root-relative).
    (out_dir / "sitemap.xml").write_text(
        _render_sitemap(page_urls),
        encoding="utf-8",
    )

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
