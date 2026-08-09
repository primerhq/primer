"""CRUD + validation tests for /v1/tool_approval_policies."""

from __future__ import annotations

import pytest


_REGO_OK = (
    "package primer.tool_approval\n"
    "default required := false\n"
    "required if input.tool_name == \"x\"\n"
)
_REGO_BROKEN = "this is not valid rego"


@pytest.mark.asyncio
async def test_create_required_policy_ok(client):
    body = {
        "id": "p-req-1",
        "toolset_id": "system",
        "tool_name": "delete_session",
        "approval": {"type": "required"},
    }
    r = await client.post("/v1/tool_approval_policies", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["enabled"] is True


@pytest.mark.asyncio
async def test_create_policy_rego_compile_fail_422(client):
    body = {
        "id": "p-rego-bad",
        "toolset_id": "system",
        "tool_name": "x",
        "approval": {"type": "policy", "policy": _REGO_BROKEN},
    }
    r = await client.post("/v1/tool_approval_policies", json=body)
    assert r.status_code == 422, r.text
    envelope = r.json()
    errors = envelope.get("extensions", {}).get("errors", [])
    assert any("policy" in (e.get("loc") or []) for e in errors)


@pytest.mark.asyncio
async def test_create_policy_rego_compile_ok(client):
    body = {
        "id": "p-rego-ok",
        "toolset_id": "system",
        "tool_name": "x",
        "approval": {"type": "policy", "policy": _REGO_OK},
    }
    r = await client.post("/v1/tool_approval_policies", json=body)
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_create_llm_policy_unknown_provider_422(client):
    body = {
        "id": "p-llm-bad",
        "toolset_id": "system",
        "tool_name": "x",
        "approval": {
            "type": "llm",
            "provider_id": "does-not-exist",
            "model": "m",
            "prompt": "judge",
        },
    }
    r = await client.post("/v1/tool_approval_policies", json=body)
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_duplicate_toolset_tool_409(client):
    body = {
        "id": "p-1",
        "toolset_id": "system",
        "tool_name": "shell_exec",
        "approval": {"type": "required"},
    }
    r = await client.post("/v1/tool_approval_policies", json=body)
    assert r.status_code == 201
    dup = {**body, "id": "p-2"}
    r = await client.post("/v1/tool_approval_policies", json=dup)
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_list_and_delete(client):
    body = {
        "id": "p-list",
        "toolset_id": "system",
        "tool_name": "y",
        "approval": {"type": "required"},
    }
    await client.post("/v1/tool_approval_policies", json=body)
    r = await client.get("/v1/tool_approval_policies")
    assert r.status_code == 200
    assert any(p["id"] == "p-list" for p in r.json()["items"])
    r = await client.delete("/v1/tool_approval_policies/p-list")
    assert r.status_code in (200, 204)
    r = await client.get("/v1/tool_approval_policies/p-list")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_invalidate_endpoint_returns_202(client):
    r = await client.post("/v1/tool_approval_policies/invalidate")
    assert r.status_code in (200, 202)


class TestLlmJudgeModelValidation:
    """``approval.model`` is checked against what the provider publishes.

    That used to mean ``LLMProvider.models``. The field is gone -- the
    provider's ModelProfile rows are the registry -- and reading it raised
    AttributeError inside the create hook, turning every llm-judge policy
    create into a 500.
    """

    async def _seed_provider(self, client, pid: str) -> None:
        r = await client.post("/v1/llm_providers", json={
            "id": pid,
            "description": "judge provider",
            "provider": "anthropic",
            "config": {"api_key": "sk-test"},
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code in (200, 201), r.text

    async def _seed_profile(self, client, pid: str, model: str) -> None:
        r = await client.post("/v1/model_profiles", json={
            "id": f"{pid}--{model}",
            "description": "judge model",
            "provider_id": pid,
            "model_name": model,
            "context_length": 4096,
        })
        assert r.status_code in (200, 201), r.text

    def _body(self, pid: str, model: str) -> dict:
        return {
            "id": f"p-llm-{model}",
            "toolset_id": "system",
            "tool_name": "x",
            "approval": {
                "type": "llm",
                "provider_id": pid,
                "model": model,
                "prompt": "decide",
            },
        }

    @pytest.mark.asyncio
    async def test_model_named_by_a_profile_is_accepted(self, client):
        await self._seed_provider(client, "judge-a")
        await self._seed_profile(client, "judge-a", "claude-x")
        r = await client.post(
            "/v1/tool_approval_policies", json=self._body("judge-a", "claude-x"),
        )
        assert r.status_code == 201, r.text

    @pytest.mark.asyncio
    async def test_model_no_profile_names_is_422_not_500(self, client):
        """The regression: this raised AttributeError and surfaced as a
        500 /errors/internal instead of a field-level 422."""
        await self._seed_provider(client, "judge-b")
        await self._seed_profile(client, "judge-b", "claude-x")
        r = await client.post(
            "/v1/tool_approval_policies", json=self._body("judge-b", "nope"),
        )
        assert r.status_code == 422, r.text
        errors = r.json()["extensions"]["errors"]
        assert any(
            list(e["loc"])[-2:] == ["approval", "model"] for e in errors
        ), errors

    @pytest.mark.asyncio
    async def test_provider_with_no_profiles_rejects_every_model(self, client):
        await self._seed_provider(client, "judge-c")
        r = await client.post(
            "/v1/tool_approval_policies", json=self._body("judge-c", "anything"),
        )
        assert r.status_code == 422, r.text

    @pytest.mark.asyncio
    async def test_unknown_provider_is_still_422(self, client):
        r = await client.post(
            "/v1/tool_approval_policies", json=self._body("no-such", "m"),
        )
        assert r.status_code == 422, r.text
