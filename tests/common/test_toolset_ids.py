"""The retired underscore-prefixed toolset ids and their one mapping (``primer.common.toolset_ids``).

The registry resolves ``_system`` to the ``system`` provider, so everything keyed on a toolset id (the approval gate above all) must read the two spellings as
one toolset. The helpers pinned here are what that is built on, and they must stay in step with the registry that gives the aliases their meaning.
"""

from __future__ import annotations

import pytest

from primer.api.registries import provider_registry
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.common import toolset_ids
from primer.common.toolset_ids import TOOLSET_ID_ALIASES, canonical_toolset_id, toolset_id_spellings


def test_the_table_is_what_the_registry_has_always_accepted():
    assert dict(TOOLSET_ID_ALIASES) == {"_system": "system", "_workspaces": "workspaces", "_search": "search", "_misc": "misc"}


def test_the_table_cannot_be_edited_by_a_caller():
    with pytest.raises(TypeError):
        TOOLSET_ID_ALIASES["_x"] = "x"  # type: ignore[index]


def test_the_registry_re_exports_the_same_function():
    """``canonical_toolset_id`` has always been importable from the registry module (the agent reference check imports it from there); it is still the one function."""
    assert provider_registry.canonical_toolset_id is toolset_ids.canonical_toolset_id
    assert "canonical_toolset_id" in provider_registry.__all__


@pytest.mark.parametrize(("alias", "current"), sorted(TOOLSET_ID_ALIASES.items()))
def test_an_alias_maps_to_its_current_id_and_the_current_id_maps_to_itself(alias, current):
    assert canonical_toolset_id(alias) == current
    assert canonical_toolset_id(current) == current


@pytest.mark.parametrize("toolset_id", ["rec", "crud", "web", "mcp-x", "", "_unknown", "System", "_SYSTEM", " _system", "_system "])
def test_nothing_else_is_rewritten(toolset_id):
    """The registry's map is an exact lookup: no case folding, no trimming, no guessing at a prefix. The gate must not accept a spelling the registry would not."""
    assert canonical_toolset_id(toolset_id) == toolset_id
    assert toolset_id_spellings(toolset_id) == (toolset_id,)


@pytest.mark.parametrize(("alias", "current"), sorted(TOOLSET_ID_ALIASES.items()))
def test_a_toolsets_spellings_are_the_same_whichever_one_is_asked_for(alias, current):
    assert toolset_id_spellings(current) == toolset_id_spellings(alias) == (current, alias)


def test_every_alias_is_a_spelling_of_exactly_one_toolset():
    seen: dict[str, str] = {}
    for current in set(TOOLSET_ID_ALIASES.values()):
        for spelling in toolset_id_spellings(current):
            assert spelling not in seen, f"{spelling!r} names both {seen[spelling]!r} and {current!r}"
            seen[spelling] = current
    assert set(seen) == set(TOOLSET_ID_ALIASES) | set(TOOLSET_ID_ALIASES.values())


def test_an_alias_never_names_a_toolset_that_is_itself_an_alias():
    """One hop is all the registry takes, and the spellings helper assumes it."""
    assert not set(TOOLSET_ID_ALIASES) & set(TOOLSET_ID_ALIASES.values())


def test_no_alias_shadows_a_built_in_toolset():
    """An alias that was also a built-in id would make the registry run the other provider for it."""
    assert not set(TOOLSET_ID_ALIASES) & RESERVED_TOOLSET_IDS
