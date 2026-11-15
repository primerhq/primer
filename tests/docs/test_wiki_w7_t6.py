"""T6 worked calls validate against the actual current catalogue."""

import json
import re

import jsonschema
import pytest

from tests.docs._wiki import repo_wiki, tool_entries

PAGES = ("runbooks/install-a-harness", "runbooks/configure-artifact-storage")
HARNESS = tuple("harness__" + n for n in ("register", "fetch", "update", "update_overrides", "install", "sync", "uninstall", "get", "list"))
ARTIFACT = tuple("system__" + n for n in ("create_artifact_storage_provider", "test_artifact_storage_provider", "set_active_artifact_store",
    "get_active_artifact_store", "list_artifact_storage_providers", "get_artifact_storage_provider", "delete_artifact_storage_provider"))
TOOLS = {PAGES[0]: (*HARNESS, "system__describe_tool", "system__get_agent", "system__get_graph"), PAGES[1]: (*ARTIFACT, "system__describe_tool")}


def calls(page):
    return [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", repo_wiki().pages[page].body, re.S)]


@pytest.mark.parametrize("page", PAGES)
def test_runbook_calls_citations_structure_and_index_rows(page):
    pages = repo_wiki().pages;guide = pages[page];entries = {e.scoped_id: e for e in tool_entries()}
    assert guide.kind == "guide" and guide.meta["audience"] == "admin" and guide.meta["tools"] == list(TOOLS[page])
    assert re.findall(r"^## (.+)$", guide.body, re.M) == ["Goal", "Preconditions", "Roles and gates", "Steps", "Verification", "Refusal and failure", "Related"]
    seen = set()
    for call in calls(page):
        scoped = call["toolset_id"] + "__" + call["tool_name"]
        assert scoped in TOOLS[page];seen.add(scoped)
        jsonschema.validate(call, entries["system__call_tool"].tool.args_schema)
        jsonschema.validate(call["arguments"], entries[scoped].tool.args_schema)
    assert seen == set(TOOLS[page])
    for scoped in TOOLS[page]:
        path = "reference/tools/" + scoped.replace("__", "/")
        assert path in pages and f"]({path})" in guide.body
    assert pages["purpose-map"].body.count(f"]({page})") == 2 and f"]({page})" in pages["runbooks"].body


@pytest.mark.parametrize("scoped", (*HARNESS, *ARTIFACT, "system__describe_tool", "system__get_agent", "system__get_graph"))
def test_current_roles_and_only_three_required_admin_only_gates(scoped):
    from primer.session.approvers import ADMIN_ONLY_SPEC
    from primer.toolset.crud import shipped_approvers

    entry = next(e for e in tool_entries() if e.scoped_id == scoped)
    user = {"harness__get", "harness__list", "system__describe_tool", "system__get_agent", "system__get_graph"}
    gated = {"harness__install", "harness__sync", "harness__uninstall"}
    assert entry.required_role == ("user" if scoped in user else "admin")
    assert entry.default_gate == ("required" if scoped in gated else None)
    if scoped in gated:assert shipped_approvers(*scoped.split("__", 1)).model_dump(mode="json") == ADMIN_ONLY_SPEC


def test_harness_preparation_exact_overrides_and_async_effects_are_explicit():
    values = calls(PAGES[0]);body = repo_wiki().pages[PAGES[0]].body
    for operation in ("install", "sync"):
        action = next(c for c in values if c["tool_name"] == operation)
        assert action["arguments"]["id"] == "hns-42" and action["arguments"]["overrides"]["profile_id"] == "team-profile"
    for text in ("pending_operation", "last_operation_error", "resolved_commit", "slug__template_name", "harness_id", "admin-only",
                 "do not retry", "original caller", "off by default", "overrides-schema-missing", "default workspace", "not a pause"):
        assert text.lower() in body.lower()


def test_artifact_test_before_selection_and_byte_routing_recovery_are_explicit():
    values = calls(PAGES[1]);body = repo_wiki().pages[PAGES[1]].body
    assert next(i for i, c in enumerate(values) if c["tool_name"] == "test_artifact_storage_provider") < next(i for i, c in enumerate(values) if c["tool_name"] == "set_active_artifact_store")
    provider = next(c["arguments"]["entity"] for c in values if c["tool_name"] == "create_artifact_storage_provider")
    assert provider["provider"] == "filesystem" and provider["config"] == {"root": "/srv/primer-artifacts"}
    for text in ("every API and worker", ".primer-artifact-root", "does not migrate", "byte-for-byte", "cascade_blocked",
                 "artifact_provider_holds_artifacts", "original provider_id", "both access_key/secret_key or neither", "no generic artifact-upload tool", "default workspace"):
        assert text in body
