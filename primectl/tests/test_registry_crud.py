import json
from pathlib import Path

import pytest

from primectl.registry import build_registry, Resource

FIXTURE = Path(__file__).parent / "fixtures" / "openapi_sample.json"


@pytest.fixture
def spec():
    return json.loads(FIXTURE.read_text())


def test_detects_crud_resources(spec):
    reg = build_registry(spec)
    names = {r.name for r in reg.all()}
    assert "agent" in names
    assert "llm_provider" in names


def test_agent_resource_verbs(spec):
    reg = build_registry(spec)
    agent = reg.resolve("agent")
    assert isinstance(agent, Resource)
    assert agent.plural == "agents"
    assert agent.path_prefix == "/v1/agents"
    assert agent.id_param == "entity_id"
    assert agent.list_op is not None and agent.list_op.method == "get"
    assert agent.create_op is not None and agent.create_op.method == "post"
    assert agent.get_op is not None
    assert agent.update_op is not None
    assert agent.delete_op is not None
    assert agent.find_op is not None


def test_llm_provider_has_no_update_op(spec):
    # llm_providers item path has only get + delete (no put) in the fixture.
    reg = build_registry(spec)
    p = reg.resolve("llm_provider")
    assert p.id_param == "entity_id"
    assert p.update_op is None
    assert p.delete_op is not None


def test_entity_schema_ref_from_create_body(spec):
    reg = build_registry(spec)
    agent = reg.resolve("agent")
    assert agent.entity_schema_ref == "#/components/schemas/Agent"


def test_resolve_by_plural_and_singular(spec):
    reg = build_registry(spec)
    assert reg.resolve("agents").name == "agent"
    assert reg.resolve("agent").name == "agent"


def test_resolve_unknown_raises_with_suggestions(spec):
    from primectl.registry import UnknownResource

    reg = build_registry(spec)
    with pytest.raises(UnknownResource) as exc:
        reg.resolve("agnet")
    assert "agent" in str(exc.value)


def test_health_singleton_is_not_a_resource(spec):
    # /v1/health is a bare GET with no item path -> must be excluded.
    reg = build_registry(spec)
    names = {r.name for r in reg.all()}
    assert "health" not in names


def test_depluralize_handles_es_plurals():
    from primectl.registry import _depluralize

    assert _depluralize("harnesses") == "harness"
    assert _depluralize("statuses") == "status"
    assert _depluralize("providers") == "provider"
    assert _depluralize("agents") == "agent"
    assert _depluralize("ssp") == "ssp"
