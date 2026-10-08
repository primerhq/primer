"""The agent bodies the agent docs show are bodies primer accepts (follow-up to finding A-19).

``docs/agents/`` is served to agents by search, and its cookbooks are copied almost verbatim into ``create_agent`` calls. Ten of them (and
``agents.md``) still wrote the agent's model as ``{"provider_id": ..., "model_name": ...}``, the shape from before model profiles: an agent
names a stored ModelProfile, ``{"profile_id": ...}``, and a body without one is refused by the model (and, since the create check, a
``profile_id`` that names no stored profile is refused as well). Nothing parsed the examples, so nothing noticed.

Two checks over every JSON code block in ``docs/agents/``:

* wherever a ``model`` object appears that looks like an agent's (it carries ``profile_id``, ``provider_id`` or ``model_name``), it is a valid
  :class:`~primer.model.agent.AgentModel`;
* wherever an object has the fields of a whole agent (``id``, ``model`` and ``system_prompt``), the whole object is a valid
  :class:`~primer.model.agent.Agent`.

A block that is not JSON (an ellipsis, a comment) is skipped, and a guard fails the run if the scan finds too few agent bodies to be real.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from primer.model.agent import Agent, AgentModel
from primer.model.model_profile import ModelProfile

REPO = Path(__file__).resolve().parents[2]
AGENT_DOCS = sorted(p for p in (REPO / "docs" / "agents").rglob("*.md") if not p.name.startswith("_"))
_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
_MODEL_KEYS = {"profile_id", "provider_id", "model_name"}


def _objects(value: Any):
    """Every dict nested anywhere in a parsed JSON value, the value itself included."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _json_blocks(text: str) -> list[Any]:
    parsed = []
    for body in _FENCE.findall(text):
        try:
            parsed.append(json.loads(body))
        except ValueError:
            continue  # an ellipsis, a comment, a python example
    return parsed


def _agent_model_problems(text: str) -> list[str]:
    problems = []
    for block in _json_blocks(text):
        for obj in _objects(block):
            model = obj.get("model")
            if isinstance(model, dict) and _MODEL_KEYS & set(model):
                try:
                    AgentModel.model_validate(model)
                except ValidationError as exc:
                    problems.append(f"model {json.dumps(model)} is not an AgentModel: {exc.errors()[0]['msg']} ({exc.errors()[0]['loc']})")
    return problems


def _whole_agent_problems(text: str) -> list[str]:
    problems = []
    for block in _json_blocks(text):
        for obj in _objects(block):
            if {"id", "model", "system_prompt"} <= set(obj):
                try:
                    Agent.model_validate(obj)
                except ValidationError as exc:
                    problems.append(f"agent {obj.get('id')!r} is not an Agent: {exc.errors()[0]['msg']} ({exc.errors()[0]['loc']})")
    return problems


def _profile_problems(text: str) -> list[str]:
    """A whole ModelProfile body (``id``, ``provider_id``, ``model_name`` and ``context_length``), such as the docs' ``create_model_profile``
    example, must be a valid ModelProfile."""
    problems = []
    for block in _json_blocks(text):
        for obj in _objects(block):
            if {"id", "provider_id", "model_name", "context_length"} <= set(obj):
                try:
                    ModelProfile.model_validate(obj)
                except ValidationError as exc:
                    problems.append(f"profile {obj.get('id')!r} is not a ModelProfile: {exc.errors()[0]['msg']} ({exc.errors()[0]['loc']})")
    return problems


def _count_agent_bodies(text: str) -> int:
    return sum(1 for block in _json_blocks(text) for obj in _objects(block) if {"id", "model", "system_prompt"} <= set(obj))


def test_the_scan_finds_enough_agent_bodies_to_be_real() -> None:
    total = sum(_count_agent_bodies(doc.read_text(encoding="utf-8")) for doc in AGENT_DOCS)
    assert total >= 8, f"only {total} agent bodies found in docs/agents/; the scan would pass vacuously"


@pytest.mark.parametrize("doc", AGENT_DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_model_object_in_the_docs_is_an_agent_model(doc: Path) -> None:
    problems = _agent_model_problems(doc.read_text(encoding="utf-8"))
    assert not problems, f"{doc.relative_to(REPO)}:\n" + "\n".join(f"  {p}" for p in problems)


@pytest.mark.parametrize("doc", AGENT_DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_whole_agent_body_in_the_docs_is_an_agent(doc: Path) -> None:
    problems = _whole_agent_problems(doc.read_text(encoding="utf-8"))
    assert not problems, f"{doc.relative_to(REPO)}:\n" + "\n".join(f"  {p}" for p in problems)


@pytest.mark.parametrize("doc", AGENT_DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_model_profile_body_in_the_docs_is_a_model_profile(doc: Path) -> None:
    problems = _profile_problems(doc.read_text(encoding="utf-8"))
    assert not problems, f"{doc.relative_to(REPO)}:\n" + "\n".join(f"  {p}" for p in problems)


def test_the_docs_show_a_model_profile_body_at_all() -> None:
    total = sum(len(_profile_problems(doc.read_text(encoding="utf-8"))) + _count_profile_bodies(doc.read_text(encoding="utf-8")) for doc in AGENT_DOCS)
    assert total >= 1, "no ModelProfile body in docs/agents/; the agents doc is meant to show how to create one"


def _count_profile_bodies(text: str) -> int:
    return sum(1 for block in _json_blocks(text) for obj in _objects(block) if {"id", "provider_id", "model_name", "context_length"} <= set(obj))


# ---- the scans themselves -------------------------------------------------------------------------------------------------------


def test_the_old_model_shape_is_reported() -> None:
    text = '```json\n{"entity": {"id": "a", "description": "d", "model": {"provider_id": "p", "model_name": "m"}, "system_prompt": ["x"]}}\n```'

    assert len(_agent_model_problems(text)) == 1 and len(_whole_agent_problems(text)) == 1


def test_a_profile_shape_passes_both_checks() -> None:
    text = '```json\n{"entity": {"id": "a", "description": "d", "model": {"profile_id": "p--m"}, "system_prompt": ["x"]}}\n```'

    assert _agent_model_problems(text) == [] and _whole_agent_problems(text) == []


def test_a_block_that_is_not_json_and_a_model_that_is_not_an_agents_are_skipped() -> None:
    text = '```json\n{"id": "a", ...}\n```\n```json\n{"model": {"temperature": 1}}\n```'

    assert _agent_model_problems(text) == [] and _whole_agent_problems(text) == []
