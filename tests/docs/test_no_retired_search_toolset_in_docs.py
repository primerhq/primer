"""No doc names the ``search`` toolset or the ``_internal_ai_docs`` collection: neither exists (revamp 06 Task 7).

There is no ``search`` toolset (``ProviderRegistry.get_toolset`` has no case for it and there is no ``primer/toolset/search.py``), yet the agent docs
sent agents to ``search::search_agents``, ``search_graphs``, ``search_collections``, ``search_tools`` and ``search_ai_docs``, and said the docs live
in a ``_internal_ai_docs`` collection. The agent docs are served to agents (the wiki build writes them into the ``system`` collection's ``docs/``
subtree) and acted on, so a name in them is a call an agent makes. Discovery is the ``system`` collection: ``system::wiki_search`` and
``system::wiki_read``, or ``collections::search`` and ``collections::read_document`` on it.

The scan covers the agent docs, the dev docs (not the vision documents, which are not a contract, as in ``test_docs_name_real_tools_and_routes.py``),
the skills and ``AGENTS.md``. The platform wiki (``docs/wiki/``) has its own retired-name guard in ``test_wiki_pages.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = sorted([
    *(REPO / "docs" / "agents").rglob("*.md"),
    *(p for p in (REPO / "docs" / "dev").rglob("*.md") if "vision" not in p.relative_to(REPO / "docs" / "dev").parts),
    *(REPO / "skills").rglob("*.md"),
    REPO / "AGENTS.md",
])

RETIRED_PREFIX = re.compile(r"(?<![A-Za-z0-9_])search(?:::|__)")
RETIRED_NAMES = re.compile(r"\bsearch_(?:agents|graphs|collections|tools|ai_docs)\b")
RETIRED_COLLECTION = "_internal_ai_docs"


def _offending(doc: Path, hit) -> list[str]:
    return [f"line {n}: {line.strip()}" for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1) if hit(line)]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_no_doc_names_a_tool_of_the_retired_search_toolset(doc: Path) -> None:
    bad = _offending(doc, lambda line: RETIRED_PREFIX.search(line) or RETIRED_NAMES.search(line))
    assert not bad, f"{doc.relative_to(REPO)} names the search toolset, which does not exist:\n" + "\n".join(bad)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_no_doc_names_the_retired_internal_ai_docs_collection(doc: Path) -> None:
    bad = _offending(doc, lambda line: RETIRED_COLLECTION in line)
    assert not bad, f"{doc.relative_to(REPO)} names _internal_ai_docs; the docs live in the system collection:\n" + "\n".join(bad)


def test_the_scan_covers_the_docs_that_named_them() -> None:
    names = {str(p.relative_to(REPO)) for p in DOCS}
    assert {"docs/agents/semantic-search.md", "docs/agents/_README.md", "docs/agents/cookbook/discover-a-capability.md",
            "skills/using-primer-over-mcp.md", "AGENTS.md", "docs/dev/subsystems/semantic-search.md"} <= names


@pytest.mark.parametrize(("line", "retired"), [
    ("call `search::search_agents`", True),
    ("the wire id `search__search_tools`", True),
    ("`search_ai_docs` returns chunks", True),
    ("`collections::search` on the system collection", False),
    ("`web::search` and `web__search`", False),
    ("`system::search_collection` searches one collection", False),
    ("POST /v1/tools/search", False),
])
def test_the_patterns_tell_the_retired_names_from_the_real_ones(line: str, retired: bool) -> None:
    assert bool(RETIRED_PREFIX.search(line) or RETIRED_NAMES.search(line)) is retired


def test_the_docs_tool_check_knows_the_tools_the_new_text_names() -> None:
    """``test_docs_name_real_tools_and_routes.py`` checks every ``collections::`` and ``system::`` name the rewritten docs use."""
    from tests.docs._live_toolsets import built_in_tools

    tools = built_in_tools()
    assert {"search", "read_document", "collection_tree"} <= set(tools["collections"])
    assert {"wiki_search", "wiki_read", "describe_tool"} <= set(tools["system"])
