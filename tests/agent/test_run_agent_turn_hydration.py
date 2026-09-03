"""run_agent_turn(artifact_storage=...) hydrates artifact-backed parts
(image/document attachments) to inline data before each llm.stream() call.

This is the one shared choke point every executor funnels through
(the base agent executor, the graph agent node, and the subagent runner
all call run_agent_turn) -- see _observe_llm_call's docstring in
primer.agent.loop. artifact_storage=None (the default) must be a total
no-op so every existing caller that doesn't pass one keeps today's
behaviour byte-for-byte.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.int.artifact_storage import ArtifactBlob
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel

from primer.agent.loop import run_agent_turn
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    ImagePart,
    Message,
    StreamEvent,
    TextPart,
)


class _MemArtifacts:
    def __init__(self) -> None:
        self.blobs: dict[str, ArtifactBlob] = {}

    async def put(self, *, data, mime_type, filename=None):
        aid = f"art-{len(self.blobs) + 1}"
        self.blobs[aid] = ArtifactBlob(data=data, mime_type=mime_type, filename=filename)
        return aid

    async def get(self, artifact_id):
        return self.blobs.get(artifact_id)


class _CapturingLLM:
    """Records every messages= list it was called with, then stops."""

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs) -> AsyncIterator[StreamEvent]:
        del model, kwargs
        self.calls.append(list(messages))
        return self._gen()

    async def _gen(self) -> AsyncIterator[StreamEvent]:
        yield Done(stop_reason="stop", raw_reason="stop")


class _NoToolManager:
    def is_notifying(self, tool_name: str) -> bool:
        del tool_name
        return False

    async def list_tools(self, *, principal=None):
        return []


def _agent() -> Agent:
    return Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"))


def _model() -> ResolvedModel:
    return ResolvedModel(
        profile_id="test-profile", provider_id="test-provider",
        model_name="m", context_length=4096, config=ModelProfileConfig(),
    )


async def _drain(agen) -> None:
    async for _ in agen:
        pass


@pytest.mark.asyncio
async def test_artifact_backed_part_is_hydrated_before_stream() -> None:
    arts = _MemArtifacts()
    aid = await arts.put(data=b"PNGBYTES", mime_type="image/png")
    llm = _CapturingLLM()
    prompt = [
        Message(role="user", parts=[
            TextPart(text="look at this"),
            ImagePart(artifact_id=aid, mime_type="image/png"),
        ]),
    ]

    await _drain(run_agent_turn(
        agent=_agent(), llm=llm, llm_model=_model(),
        tool_manager=_NoToolManager(), prompt=prompt,
        artifact_storage=arts,
    ))

    assert len(llm.calls) == 1
    sent_image = llm.calls[0][0].parts[1]
    assert isinstance(sent_image, ImagePart)
    assert sent_image.data == b"PNGBYTES"
    assert sent_image.artifact_id is None
    # The caller's own prompt list is untouched -- hydrate_prompt_parts
    # returns a new list/messages, never mutates in place.
    assert prompt[0].parts[1].data is None
    assert prompt[0].parts[1].artifact_id == aid


@pytest.mark.asyncio
async def test_no_artifact_storage_is_a_total_noop() -> None:
    """Every existing caller omits artifact_storage. It must behave exactly
    as before: the artifact-backed part rides through unresolved."""
    llm = _CapturingLLM()
    prompt = [
        Message(role="user", parts=[
            ImagePart(artifact_id="art-1", mime_type="image/png"),
        ]),
    ]

    await _drain(run_agent_turn(
        agent=_agent(), llm=llm, llm_model=_model(),
        tool_manager=_NoToolManager(), prompt=prompt,
    ))

    sent_image = llm.calls[0][0].parts[0]
    assert sent_image.artifact_id == "art-1"
    assert sent_image.data is None


@pytest.mark.asyncio
async def test_text_only_prompt_unaffected_by_artifact_storage() -> None:
    arts = _MemArtifacts()
    llm = _CapturingLLM()
    prompt = [Message(role="user", parts=[TextPart(text="hello")])]

    await _drain(run_agent_turn(
        agent=_agent(), llm=llm, llm_model=_model(),
        tool_manager=_NoToolManager(), prompt=prompt,
        artifact_storage=arts,
    ))

    assert llm.calls[0][0].parts[0].text == "hello"
