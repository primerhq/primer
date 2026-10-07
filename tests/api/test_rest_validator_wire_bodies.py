"""The REST pre-write validators answer with byte-identical problem bodies (task 01a111d1, D5 phase 2b, lead's addition (a)).

The routers' pre-create / pre-update validators were turned into one-line adapters over shared check functions
(``primer/agent/approval_checks.py``, ``primer/toolset/toolset_checks.py``, ``primer/model_profile/checks.py``,
``primer/channel/checks.py``) that raise ``EntityCheckError``. Each adapter must re-raise EXACTLY the exception the router raised
before, so the wire body cannot change. The router tests assert status codes and a field or two; this pins the WHOLE response:
status, content type and the raw body, for the same bad entity through each adapted hook.

``GOLDEN`` was captured by running these exact requests against the code BEFORE the change (main b53af70a, where the validators
were still inline in the routers), then the same file was run against the change. A failure here means a wire body moved.
"""

from __future__ import annotations

import json
import re

import pytest
from pydantic import SecretStr

from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType
from tests._support.model_profiles import profile_body, seed_profile

GOLDEN: dict[str, tuple[int, str, str]] = {
    'channel_duplicate': (
        409,
        'application/problem+json',
        '{"type":"/errors/conflict","title":"Conflict","status":409,"detail":"Channel with provider_id=\'cp-wire\', external_id=\'C0001\' already exists (id=\'ch-1\')","instance":"/v1/channels","extensions":{"request_id":"req-<id>"}}',
    ),
    'channel_missing_provider': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"ChannelProvider \'cp-ghost\' does not exist","instance":"/v1/channels","extensions":{"request_id":"req-<id>"}}',
    ),
    'policy_duplicate_create': (
        409,
        'application/problem+json',
        '{"type":"/errors/conflict","title":"Conflict","status":409,"detail":"a ToolApprovalPolicy for toolset_id=\'system\', tool_name=\'shell_exec\' already exists (id=\'p-1\')","instance":"/v1/tool_approval_policies","extensions":{"request_id":"req-<id>"}}',
    ),
    'policy_duplicate_update': (
        409,
        'application/problem+json',
        '{"type":"/errors/conflict","title":"Conflict","status":409,"detail":"a ToolApprovalPolicy for toolset_id=\'system\', tool_name=\'shell_exec\' already exists (id=\'p-1\')","instance":"/v1/tool_approval_policies/p-3","extensions":{"request_id":"req-<id>"}}',
    ),
    'policy_llm_unknown_provider': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"One or more request parameters or body fields failed validation.","instance":"/v1/tool_approval_policies","extensions":{"errors":[{"loc":["body","approval","provider_id"],"msg":"unknown LLM provider \'does-not-exist\'","type":"value_error"}],"request_id":"req-<id>"}}',
    ),
    'policy_llm_unpublished_model': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"One or more request parameters or body fields failed validation.","instance":"/v1/tool_approval_policies","extensions":{"errors":[{"loc":["body","approval","model"],"msg":"model \'not-published\' not registered on provider \'wire-prov\' (available: [\'published-model\'])","type":"value_error"}],"request_id":"req-<id>"}}',
    ),
    'policy_rego_create': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"One or more request parameters or body fields failed validation.","instance":"/v1/tool_approval_policies","extensions":{"errors":[{"loc":["body","approval","policy"],"msg":"rego compile failed: <engine output>","type":"value_error"}],"request_id":"req-<id>"}}',
    ),
    'profile_aggregate_duplicate_member': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"members must not contain duplicates; order is the routing/failover chain, so a duplicate would silently change behaviour rather than being a harmless repeat","instance":"/v1/model_profiles","extensions":{"error":"duplicate_member","field":"members","message":"members must not contain duplicates; order is the routing/failover chain, so a duplicate would silently change behaviour rather than being a harmless repeat","request_id":"req-<id>"}}',
    ),
    'profile_aggregate_missing_member': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"member profile \'ghost-member\' does not exist","instance":"/v1/model_profiles","extensions":{"error":"member_not_found","field":"members","message":"member profile \'ghost-member\' does not exist","request_id":"req-<id>"}}',
    ),
    'profile_aggregate_names_itself': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"profile \'agg-self\' cannot name itself as a member","instance":"/v1/model_profiles","extensions":{"error":"self_reference","field":"members","message":"profile \'agg-self\' cannot name itself as a member","request_id":"req-<id>"}}',
    ),
    'profile_aggregate_nested': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"member profile \'agg-inner\' is itself kind=\'aggregated\'; nested aggregation is not supported (v1)","instance":"/v1/model_profiles","extensions":{"error":"nested_aggregation","field":"members","message":"member profile \'agg-inner\' is itself kind=\'aggregated\'; nested aggregation is not supported (v1)","request_id":"req-<id>"}}',
    ),
    'profile_aggregate_too_small': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"an aggregated profile must name at least two member profiles, per the aggregation directive: \\"an aggregated profile is an aggregation of two or more model profiles\\"","instance":"/v1/model_profiles","extensions":{"error":"aggregation_too_small","field":"members","message":"an aggregated profile must name at least two member profiles, per the aggregation directive: \\"an aggregated profile is an aggregation of two or more model profiles\\"","request_id":"req-<id>"}}',
    ),
    'profile_member_becomes_aggregate': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"profile \'wire-prov-agg--model-1\' is a member of aggregate \'agg-inner\' and cannot become kind=\'aggregated\' itself (nested aggregation is not supported); remove it from that aggregate\'s members first","instance":"/v1/model_profiles/wire-prov-agg--model-1","extensions":{"error":"member_of_another_aggregate","field":"kind","message":"profile \'wire-prov-agg--model-1\' is a member of aggregate \'agg-inner\' and cannot become kind=\'aggregated\' itself (nested aggregation is not supported); remove it from that aggregate\'s members first","request_id":"req-<id>"}}',
    ),
    'profile_missing_provider_create': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"LLMProvider \'ghost-provider\' does not exist; create the provider before registering a profile on it","instance":"/v1/model_profiles","extensions":{"error":"provider_not_found","field":"provider_id","message":"LLMProvider \'ghost-provider\' does not exist; create the provider before registering a profile on it","request_id":"req-<id>"}}',
    ),
    'profile_missing_provider_update': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"LLMProvider \'ghost-provider\' does not exist; create the provider before registering a profile on it","instance":"/v1/model_profiles/wire-prov-upd--model-1","extensions":{"error":"provider_not_found","field":"provider_id","message":"LLMProvider \'ghost-provider\' does not exist; create the provider before registering a profile on it","request_id":"req-<id>"}}',
    ),
    'toolset_python_invalid_create': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"nodoc: the function needs a docstring","instance":"/v1/toolsets","extensions":{"error":"invalid_python_toolset","message":"nodoc: the function needs a docstring","field":"docstring","lineno":2,"request_id":"req-<id>"}}',
    ),
    'toolset_python_invalid_update': (
        422,
        'application/problem+json',
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,"detail":"nodoc: the function needs a docstring","instance":"/v1/toolsets/py-ok","extensions":{"error":"invalid_python_toolset","message":"nodoc: the function needs a docstring","field":"docstring","lineno":2,"request_id":"req-<id>"}}',
    ),
}

CASE_NAMES = [
    "policy_duplicate_create",
    "policy_duplicate_update",
    "policy_rego_create",
    "policy_llm_unknown_provider",
    "policy_llm_unpublished_model",
    "toolset_python_invalid_create",
    "toolset_python_invalid_update",
    "profile_missing_provider_create",
    "profile_missing_provider_update",
    "profile_aggregate_too_small",
    "profile_aggregate_names_itself",
    "profile_aggregate_duplicate_member",
    "profile_aggregate_missing_member",
    "profile_aggregate_nested",
    "profile_member_becomes_aggregate",
    "channel_missing_provider",
    "channel_duplicate",
]


_REQUEST_ID = re.compile(r'"request_id":"req-[0-9a-f]+"')
_REGO_TRACE = re.compile(r'(rego compile failed: )rego compile/eval failed: .*?(","type":"value_error")')


def _normalise(text: str) -> str:
    """The two parts of a body that are not ours to pin: the random per-request id, and the Rego engine's own error trace (a
    dependency upgrade must not look like a wire change). Everything else, key order included, is compared byte for byte."""
    text = _REQUEST_ID.sub('"request_id":"req-<id>"', text)
    return _REGO_TRACE.sub(r"\1<engine output>\2", text)


def _check(name: str, response) -> None:
    actual = (response.status_code, response.headers.get("content-type", ""), _normalise(response.text))
    if name not in GOLDEN:
        print(f"WIRE {name} :: {json.dumps(actual)}")
        return
    assert actual == GOLDEN[name], f"{name}: the wire body moved\n  was: {GOLDEN[name]!r}\n  now: {actual!r}"


def test_every_case_has_a_golden_body() -> None:
    assert sorted(GOLDEN) == sorted(CASE_NAMES), f"missing: {sorted(set(CASE_NAMES) - set(GOLDEN))}"


async def _seed_provider(client, provider_id: str) -> None:
    body = LLMProvider(
        id=provider_id, provider=LLMProviderType.ANTHROPIC,
        config=AnthropicConfig(api_key=SecretStr("sk-test")), limits=Limits(max_concurrency=4),
    ).model_dump(mode="json")
    r = await client.post("/v1/llm_providers", json=body)
    assert r.status_code in (200, 201), r.text


def _policy(policy_id: str, tool_name: str, approval: dict) -> dict:
    return {"id": policy_id, "toolset_id": "system", "tool_name": tool_name, "approval": approval}


def _aggregate(profile_id: str, members: list[str]) -> dict:
    return {"id": profile_id, "description": "an aggregated profile", "kind": "aggregated", "members": members}


PY_OK = (
    "@primer_tool()\n"
    "def greet(name: str) -> str:\n"
    '    """Greet a person.\n\n    Use when greeting.\n\n'
    '    Args:\n        name: Who.\n    """\n'
    "    return 'hi ' + name\n"
)
PY_BAD = "@primer_tool()\ndef nodoc(x: str) -> str:\n    return x\n"


def _python_toolset(toolset_id: str, source: str) -> dict:
    return {"id": toolset_id, "provider": "python", "config": {"source": source, "source_version": 1}}


class TestToolApprovalPolicyBodies:
    @pytest.mark.asyncio
    async def test_a_second_policy_for_the_same_tool(self, client) -> None:
        await client.post("/v1/tool_approval_policies", json=_policy("p-1", "shell_exec", {"type": "required"}))

        r = await client.post("/v1/tool_approval_policies", json=_policy("p-2", "shell_exec", {"type": "required"}))

        _check("policy_duplicate_create", r)

    @pytest.mark.asyncio
    async def test_an_update_onto_another_policys_tool(self, client) -> None:
        await client.post("/v1/tool_approval_policies", json=_policy("p-1", "shell_exec", {"type": "required"}))
        await client.post("/v1/tool_approval_policies", json=_policy("p-3", "other_tool", {"type": "required"}))

        r = await client.put("/v1/tool_approval_policies/p-3", json=_policy("p-3", "shell_exec", {"type": "required"}))

        _check("policy_duplicate_update", r)

    @pytest.mark.asyncio
    async def test_rego_that_does_not_compile(self, client) -> None:
        r = await client.post(
            "/v1/tool_approval_policies",
            json=_policy("p-rego", "x", {"type": "policy", "policy": "this is not valid rego"}),
        )

        _check("policy_rego_create", r)

    @pytest.mark.asyncio
    async def test_an_llm_judge_naming_a_missing_provider(self, client) -> None:
        r = await client.post(
            "/v1/tool_approval_policies",
            json=_policy("p-llm", "x", {"type": "llm", "provider_id": "does-not-exist", "model": "m", "prompt": "judge"}),
        )

        _check("policy_llm_unknown_provider", r)

    @pytest.mark.asyncio
    async def test_an_llm_judge_naming_an_unpublished_model(self, client) -> None:
        await _seed_provider(client, "wire-prov")
        await seed_profile(client, "wire-prov", "published-model")

        r = await client.post(
            "/v1/tool_approval_policies",
            json=_policy("p-llm", "x", {"type": "llm", "provider_id": "wire-prov", "model": "not-published", "prompt": "judge"}),
        )

        _check("policy_llm_unpublished_model", r)


class TestToolsetBodies:
    @pytest.mark.asyncio
    async def test_a_python_toolset_that_does_not_register_on_create(self, client) -> None:
        r = await client.post("/v1/toolsets", json=_python_toolset("py-bad", PY_BAD))

        _check("toolset_python_invalid_create", r)

    @pytest.mark.asyncio
    async def test_a_python_toolset_that_does_not_register_on_update(self, client) -> None:
        created = await client.post("/v1/toolsets", json=_python_toolset("py-ok", PY_OK))
        assert created.status_code == 201, created.text

        r = await client.put("/v1/toolsets/py-ok", json=_python_toolset("py-ok", PY_BAD))

        _check("toolset_python_invalid_update", r)


class TestModelProfileBodies:
    @pytest.mark.asyncio
    async def test_a_missing_provider_on_create(self, client) -> None:
        r = await client.post("/v1/model_profiles", json=profile_body("ghost-provider", "model-1"))

        _check("profile_missing_provider_create", r)

    @pytest.mark.asyncio
    async def test_a_missing_provider_on_update(self, client) -> None:
        await _seed_provider(client, "wire-prov-upd")
        pid = await seed_profile(client, "wire-prov-upd", "model-1")
        body = profile_body("ghost-provider", "model-1")
        body["id"] = pid

        r = await client.put(f"/v1/model_profiles/{pid}", json=body)

        _check("profile_missing_provider_update", r)

    @pytest.mark.asyncio
    async def test_an_aggregate_with_no_members(self, client) -> None:
        r = await client.post("/v1/model_profiles", json=_aggregate("agg-empty", []))

        _check("profile_aggregate_too_small", r)

    @pytest.mark.asyncio
    async def test_an_aggregate_naming_itself(self, client) -> None:
        await _seed_provider(client, "wire-prov-agg")
        m1 = await seed_profile(client, "wire-prov-agg", "model-1")

        r = await client.post("/v1/model_profiles", json=_aggregate("agg-self", ["agg-self", m1]))

        _check("profile_aggregate_names_itself", r)

    @pytest.mark.asyncio
    async def test_an_aggregate_with_a_duplicate_member(self, client) -> None:
        await _seed_provider(client, "wire-prov-agg")
        m1 = await seed_profile(client, "wire-prov-agg", "model-1")

        r = await client.post("/v1/model_profiles", json=_aggregate("agg-dup", [m1, m1]))

        _check("profile_aggregate_duplicate_member", r)

    @pytest.mark.asyncio
    async def test_an_aggregate_naming_a_missing_member(self, client) -> None:
        await _seed_provider(client, "wire-prov-agg")
        m1 = await seed_profile(client, "wire-prov-agg", "model-1")

        r = await client.post("/v1/model_profiles", json=_aggregate("agg-missing", [m1, "ghost-member"]))

        _check("profile_aggregate_missing_member", r)

    @pytest.mark.asyncio
    async def test_an_aggregate_naming_an_aggregate(self, client) -> None:
        await _seed_provider(client, "wire-prov-agg")
        m1 = await seed_profile(client, "wire-prov-agg", "model-1")
        m2 = await seed_profile(client, "wire-prov-agg", "model-2")
        inner = await client.post("/v1/model_profiles", json=_aggregate("agg-inner", [m1, m2]))
        assert inner.status_code in (200, 201), inner.text

        r = await client.post("/v1/model_profiles", json=_aggregate("agg-outer", ["agg-inner", m1]))

        _check("profile_aggregate_nested", r)

    @pytest.mark.asyncio
    async def test_a_listed_member_becoming_an_aggregate(self, client) -> None:
        await _seed_provider(client, "wire-prov-agg")
        m1 = await seed_profile(client, "wire-prov-agg", "model-1")
        m2 = await seed_profile(client, "wire-prov-agg", "model-2")
        m3 = await seed_profile(client, "wire-prov-agg", "model-3")
        inner = await client.post("/v1/model_profiles", json=_aggregate("agg-inner", [m1, m2]))
        assert inner.status_code in (200, 201), inner.text

        r = await client.put(f"/v1/model_profiles/{m1}", json=_aggregate(m1, [m2, m3]))

        _check("profile_member_becomes_aggregate", r)


class TestChannelBodies:
    @pytest.mark.asyncio
    async def test_a_channel_naming_a_missing_provider(self, client) -> None:
        r = await client.post(
            "/v1/channels",
            json={"id": "ch-ghost", "provider_id": "cp-ghost", "provider": "slack", "external_id": "C-GHOST"},
        )

        _check("channel_missing_provider", r)

    @pytest.mark.asyncio
    async def test_a_second_channel_for_the_same_pair(self, client) -> None:
        await client.post(
            "/v1/channel_providers",
            json={"id": "cp-wire", "provider": "slack", "config": {"app_token": "xapp-test", "bot_token": "xoxb-test"}},
        )
        first = await client.post(
            "/v1/channels", json={"id": "ch-1", "provider_id": "cp-wire", "provider": "slack", "external_id": "C0001"},
        )
        assert first.status_code == 201, first.text

        r = await client.post(
            "/v1/channels", json={"id": "ch-2", "provider_id": "cp-wire", "provider": "slack", "external_id": "C0001"},
        )

        _check("channel_duplicate", r)
