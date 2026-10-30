"""The five Phase-1 owned models carry the ``Access`` field (spec 6.1, plan Task 4).

Each of ``Agent``, ``Graph``, ``Toolset``, ``WorkspaceTemplate`` and ``Channel``
round-trips a populated ``Access`` through a JSON dump; a model built without
one gets an empty ``Access`` (no owners, not public, no members). The list
queries filter on the derived ``access.members`` tokens; the index they lean
on is pinned by ``tests/storage/test_access_index.py``.
"""

from __future__ import annotations

import pytest

from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.channel import Channel, ChannelProviderType
from primer.model.graph import Graph
from primer.model.providers.toolset import Toolset, ToolsetProviderType
from primer.model.workspace import WorkspaceTemplate

_MODELS = [
    (Agent, dict(description="d", model=AgentModel(profile_id="p"))),
    (Graph, dict(description="d")),
    (Toolset, dict(provider=ToolsetProviderType.INTERNAL)),
    (WorkspaceTemplate, dict(description="d", provider_id="p")),
    (Channel, dict(provider_id="p", provider=ChannelProviderType.SLACK, external_id="e")),
]

#: A fully populated Access: two levels of sharing, public, public tools.
FULL = Access(
    owners=["u1"],
    grants={"u2": "full", "u3": "read"},
    public=True,
    public_tools=["fetch"],
)


@pytest.mark.parametrize(("model_cls", "kwargs"), _MODELS, ids=[c.__name__ for c, _ in _MODELS])
def test_the_model_carries_access(model_cls, kwargs):
    assert "access" in model_cls.model_fields
    # The field is inherited last, so it dumps after the model's own fields.
    assert list(model_cls.model_fields)[-1] == "access"


@pytest.mark.parametrize(("model_cls", "kwargs"), _MODELS, ids=[c.__name__ for c, _ in _MODELS])
def test_default_access_is_empty_and_private(model_cls, kwargs):
    row = model_cls(**kwargs)
    assert row.access == Access()
    assert row.access.members == []


@pytest.mark.parametrize(("model_cls", "kwargs"), _MODELS, ids=[c.__name__ for c, _ in _MODELS])
def test_access_round_trips_through_a_json_dump(model_cls, kwargs):
    row = model_cls(**kwargs, access=FULL)
    assert row.access == FULL
    restored = model_cls.model_validate(row.model_dump(mode="json"))
    assert restored.access == FULL
