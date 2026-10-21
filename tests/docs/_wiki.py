"""The wiki build over the checked-in docs/wiki sources, for the coverage tests (pivot P1, task 13).

``tool_entries`` is the live catalog the tests share: the tools of the built-in
toolsets that ``tests/docs/_live_toolsets.py`` builds, plus the session file
tools and the client tool, exactly what ``regenerate_system_collection`` feeds
the build at boot. ``repo_wiki`` is the build of the repo's own ``docs/wiki``
sources over that catalog, with no legacy layer: the pages the coverage tests
read. Both are cached, so a suite that reads them many times lists the toolsets
once.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.knowledge.wiki.build import WikiBuild
    from primer.toolset.tool_catalog import ToolEntry

REPO = Path(__file__).resolve().parents[2]
WIKI_DIR = REPO / "docs" / "wiki"


@lru_cache(maxsize=1)
def tool_entries() -> tuple[ToolEntry, ...]:
    """Every agent-callable tool, sorted by scoped id, built once and shared."""
    from primer.toolset.tool_catalog import collect_tool_entries
    from tests.docs._live_toolsets import built_in_providers

    return tuple(asyncio.run(collect_tool_entries(built_in_providers())))


@lru_cache(maxsize=1)
def repo_wiki() -> WikiBuild:
    """The build of the checked-in wiki sources over the live catalog (no legacy pages)."""
    from primer.knowledge.wiki.build import build_wiki

    return build_wiki(WIKI_DIR, tool_entries(), legacy_pages={})
