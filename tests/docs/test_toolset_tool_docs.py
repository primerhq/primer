"""The docs say what the toolset tools do (ticket 01a16f1d, from #933's source corrections).

* Ruling 1: an edit that changes a Python toolset's source refuses a call parked in one of its tools (``PythonToolsetProvider.resume_tool``); the
  agent doc said the call "still resumes against the version that parked it".
* Ruling 7: ``call_tool`` reaches the built-in toolsets AND the stored MCP and Python toolsets (``ProviderRegistry.get_toolset`` answers a reserved id
  from the built-ins and any other from storage); the start-here page said it reaches only the ten built-in toolsets.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from primer.api.registries.provider_registry import ProviderRegistry, _build_default_toolset_factory
from tests.docs._wiki import repo_wiki

ROOT = Path(__file__).resolve().parents[2]


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_python_tools_doc_says_a_parked_call_is_refused_after_a_source_change() -> None:
    flat = _flat((ROOT / "docs/agents/python-tools.md").read_text(encoding="utf-8"))

    assert "still resumes against the version that parked it" not in flat
    assert "the answer is not applied" in flat and "The same source keeps the version" in flat


def test_the_routing_the_calling_tools_page_describes() -> None:
    """The facts the page states: a non-reserved id is read from storage, and the factory builds MCP and Python rows (an internal row is refused)."""
    lookup = inspect.getsource(ProviderRegistry.get_toolset)
    factory = inspect.getsource(_build_default_toolset_factory)
    assert "get_storage(Toolset).get(toolset_id)" in lookup
    assert "ToolsetProviderType.MCP" in factory and "ToolsetProviderType.PYTHON" in factory


def test_the_calling_tools_page_says_call_tool_reaches_the_stored_toolsets() -> None:
    flat = _flat(repo_wiki().pages["start-here/calling-tools"].body)

    assert "Reachable through the door: the ten built-in toolsets." not in flat
    assert "the stored MCP and Python toolsets" in flat
