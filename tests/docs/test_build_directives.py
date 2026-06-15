"""Tests for the fenced-block directive dispatcher in ``render_markdown``.

Each docs directive (``callout``/``code-tabs``/``mermaid``/``ai-doc``) is a
fenced code block whose info string selects a renderer. The console renders
these as React components (see ``ui/components/docs/directives-*.jsx``); the
static site emits equivalent no-JS-required HTML that the client behaviours
in ``site_template/docs.js`` enhance (tabs, mermaid).
"""

import re

from scripts.docs.build_site import render_markdown


SLUG_URL_MAP = {
    "getting-started/quickstart": "/getting-started/quickstart/",
    "reference/mcp-server-reference": "/reference/mcp-server-reference/",
}


CALLOUT_MD = """\
```callout:warning
The SSP is **locked** once activated. See [quickstart](ref:getting-started/quickstart).
```
"""


def test_callout_renders_box_with_kind_and_markdown_body():
    out = render_markdown(CALLOUT_MD, SLUG_URL_MAP)
    # Box markup keyed on kind, matching directives-callout.jsx kinds.
    assert '<div class="callout callout-warning">' in out
    # Labelled header naming the kind.
    assert '<div class="callout-title">' in out
    assert "warning" in out
    # Body is re-parsed as markdown (bold + resolved ref link).
    assert "<strong>locked</strong>" in out
    assert 'href="/getting-started/quickstart/"' in out
    # No leftover raw fence.
    assert "```callout" not in out


CODE_TABS_MD = """\
```code-tabs:curl,python
--- curl
curl https://primer.example/v1/health
--- python
import httpx
httpx.get("https://primer.example/v1/health")
```
"""


def test_code_tabs_renders_tabs_buttons_and_panels():
    out = render_markdown(CODE_TABS_MD, SLUG_URL_MAP)
    # Container + tab buttons + panels driven by wireTabs() in docs.js.
    assert '<div class="tabs">' in out
    assert '<button class="tab active"' in out
    assert '<button class="tab"' in out
    # Tab buttons reference their panel id via data-tab.
    assert 'data-tab=' in out
    # Panels carry the matching id and a .tab-panel class.
    assert '<div class="tab-panel active"' in out
    assert '<div class="tab-panel"' in out
    # Lang labels rendered on the buttons.
    assert ">curl<" in out
    assert ">python<" in out
    # Code preserved (and escaped) in panels.
    assert "curl https://primer.example/v1/health" in out
    assert "import httpx" in out
    # First panel id matches first tab's data-tab.
    btn = re.search(r'<button class="tab active" data-tab="([^"]+)"', out)
    assert btn is not None
    assert f'<div class="tab-panel active" id="{btn.group(1)}"' in out
    assert "```code-tabs" not in out


MERMAID_MD = """\
```mermaid
flowchart LR
    a([Begin]) --> b[work]
    b --> c([End])
```
"""


def test_mermaid_renders_pre_with_source():
    out = render_markdown(MERMAID_MD, SLUG_URL_MAP)
    assert '<pre class="mermaid">' in out
    # Diagram source preserved verbatim (escaped) for client-side rendering.
    assert "flowchart LR" in out
    assert "a([Begin]) --&gt; b[work]" in out
    assert "```mermaid" not in out


AI_DOC_MD = """\
```ai-doc:mcp-server-reference
```
"""


def test_ai_doc_renders_non_linking_note():
    out = render_markdown(AI_DOC_MD, SLUG_URL_MAP)
    # The AI-doc mirror is a console-only route that does not exist on the
    # static site, so ai-doc renders a NON-linking labelled note (no dead
    # /docs/_ai/ href), preserving the authoring cue without a broken target.
    assert '<div class="ai-doc"' in out
    assert "/docs/_ai/" not in out
    assert "Agent-facing reference" in out
    assert "mcp-server-reference" in out
    assert "```ai-doc" not in out
