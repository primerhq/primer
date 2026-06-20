import json
from pathlib import Path

import pytest

from primectl.registry import build_registry

FIXTURE = Path(__file__).parent / "fixtures" / "openapi_sample.json"


@pytest.fixture
def reg():
    return build_registry(json.loads(FIXTURE.read_text()))


def test_agent_custom_ops_detected(reg):
    agent = reg.resolve("agent")
    assert "status" in agent.custom_ops          # /v1/agents/{agent_id}/status
    assert "search" in agent.custom_ops          # /v1/agents/search
    assert agent.custom_ops["status"].method == "get"
    assert agent.custom_ops["status"].path_template == "/v1/agents/{agent_id}/status"
    assert agent.custom_ops["status"].path_params == ("agent_id",)


def test_find_and_item_paths_are_not_custom_ops(reg):
    agent = reg.resolve("agent")
    assert "find" not in agent.custom_ops
    # the item path {entity_id} must not be misread as a custom op
    assert all("entity_id" not in a for a in agent.custom_ops)


def test_underscore_action_name_normalised(reg):
    p = reg.resolve("llm_provider")
    # /v1/llm_providers/_discover_models -> "discover-models"
    assert "discover-models" in p.custom_ops


def test_alias_resolves(reg):
    assert reg.resolve("llm").name == "llm_provider"


def test_workspace_reply_binding_is_custom_op(reg):
    ws = reg.resolve("workspace")
    assert "reply-binding" in ws.custom_ops
    op = ws.custom_ops["reply-binding"]
    assert op.method == "put"
    assert op.path_template == "/v1/workspaces/{workspace_id}/reply_binding"
    assert op.path_params == ("workspace_id",)
