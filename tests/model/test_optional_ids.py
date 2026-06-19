import re

import pytest
from typing import ClassVar

from primer.model.common import Identifiable


class _WithPrefix(Identifiable):
    _id_prefix: ClassVar[str] = "thing"


class _NoPrefix(Identifiable):
    pass


def test_autogen_when_id_omitted():
    e = _WithPrefix()
    assert re.fullmatch(r"thing-[0-9a-f]{12}", e.id), e.id


def test_supplied_id_is_kept():
    e = _WithPrefix(id="my-id")
    assert e.id == "my-id"


def test_empty_string_id_autogenerates():
    e = _WithPrefix(id="")
    assert re.fullmatch(r"thing-[0-9a-f]{12}", e.id), e.id


def test_no_prefix_subclass_requires_id():
    with pytest.raises(Exception):  # ValidationError: id is required
        _NoPrefix()
    assert _NoPrefix(id="x").id == "x"


import re as _re

from primer.model.agent import Agent
from primer.model.graph import Graph
from primer.model.collection import Collection, Document
from primer.model.channel import Channel, ChannelProvider
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.workspace import WorkspaceProvider, WorkspaceTemplate
from primer.model.provider import (
    LLMProvider, EmbeddingProvider, CrossEncoderProvider, Toolset,
    SemanticSearchProvider,
)

_PREFIXES = {
    Agent: "agent", Graph: "graph", Toolset: "toolset",
    Collection: "collection", Document: "document",
    LLMProvider: "llm-provider", EmbeddingProvider: "embedding-provider",
    CrossEncoderProvider: "cross-encoder-provider",
    SemanticSearchProvider: "semantic-search-provider",
    ChannelProvider: "channel-provider", Channel: "channel",
    ToolApprovalPolicy: "tool-approval-policy",
    WorkspaceProvider: "workspace-provider",
    WorkspaceTemplate: "workspace-template",
}


def test_every_in_scope_model_declares_its_prefix():
    for cls, prefix in _PREFIXES.items():
        assert cls._id_prefix == prefix, f"{cls.__name__}: {cls._id_prefix!r}"


def test_models_autogen_with_prefix_when_id_omitted():
    a = Agent(description="x", model={"provider_id": "p", "model_name": "m"})
    assert _re.fullmatch(r"agent-[0-9a-f]{12}", a.id), a.id
    d = Document(collection_id="c1", name="n", path="n.md")
    assert d.id.startswith("document-")
