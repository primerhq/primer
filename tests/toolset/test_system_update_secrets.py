"""A system ``update_*`` tool never writes a masked secret over the real one (task 01a111d1, D5 phase 1, item i).

``get_*`` serves every ``SecretStr`` masked (``**********`` plus the last four characters) and ``update_*`` is a full
replace. A caller that reads a row and writes it back, which is exactly what an agent does to change one field, therefore
sent the mask as the new value, and the tool stored the mask as the credential: the API key was destroyed by an ordinary
read-then-write. The REST routers prevent it with ``preserve_masked_secrets`` as an ``on_pre_update`` hook; the tools had no
such hook.

The served body is taken from the tool itself, never hand-written, so the test follows whatever mask the system serves.
"""

from __future__ import annotations

import json

import pytest

from primer.model.provider import EmbeddingProvider, LLMProvider, Toolset
from tests._support.caller import ADMIN_CALLER
from tests.toolset.test_system import _emb, _llm, _toolset_body, pr, sp, system_toolset  # noqa: F401  (fixtures)

LLM_KEY = "sk-live-0123456789"
HF_TOKEN = "hf_live_secret_9876"
ENV_TOKEN = "tok-live-abcdef"


def _llm_body() -> dict:
    body = _llm().model_dump(mode="json")
    body["config"]["api_key"] = LLM_KEY
    return body


def _emb_body() -> dict:
    body = _emb().model_dump(mode="json")
    body["config"]["token"] = HF_TOKEN
    return body


def _toolset_with_env() -> dict:
    body = _toolset_body()
    body["config"]["config"]["env"] = {"API_TOKEN": ENV_TOKEN}
    return body


async def _served(toolset, kind: str, entity_id: str) -> dict:
    result = await toolset.call(tool_name=f"get_{kind}", arguments={"id": entity_id})
    assert not result.is_error, result.output
    return json.loads(result.output)


async def _create(toolset, kind: str, body: dict) -> None:
    result = await toolset.call(tool_name=f"create_{kind}", arguments={"entity": body}, ctx=ADMIN_CALLER)
    assert not result.is_error, result.output


def _stored_llm_key(sp) -> str:
    return sp.get_storage(LLMProvider)._data["anthropic-1"].config.api_key.get_secret_value()


def _stored_hf_token(sp) -> str:
    return sp.get_storage(EmbeddingProvider)._data["hf-1"].config.token.get_secret_value()


def _stored_env_token(sp) -> str:
    return sp.get_storage(Toolset)._data["ts-1"].config.config.env["API_TOKEN"].get_secret_value()


class TestReadThenWriteKeepsTheStoredSecret:
    @pytest.mark.asyncio
    async def test_a_provider_written_back_unchanged_keeps_its_api_key(self, system_toolset, sp) -> None:
        await _create(system_toolset, "llm_provider", _llm_body())
        served = await _served(system_toolset, "llm_provider", "anthropic-1")
        assert served["config"]["api_key"] != LLM_KEY, "precondition: the tool serves the secret masked"

        result = await system_toolset.call(tool_name="update_llm_provider", arguments={"id": "anthropic-1", "entity": served})

        assert not result.is_error, result.output
        assert _stored_llm_key(sp) == LLM_KEY, "the mask was stored over the real API key"

    @pytest.mark.asyncio
    async def test_changing_another_field_keeps_the_key_and_applies_the_change(self, system_toolset, sp) -> None:
        await _create(system_toolset, "llm_provider", _llm_body())
        served = await _served(system_toolset, "llm_provider", "anthropic-1")
        served["limits"]["max_concurrency"] = 9

        result = await system_toolset.call(tool_name="update_llm_provider", arguments={"id": "anthropic-1", "entity": served})

        assert not result.is_error, result.output
        row = sp.get_storage(LLMProvider)._data["anthropic-1"]
        assert row.limits.max_concurrency == 9
        assert row.config.api_key.get_secret_value() == LLM_KEY

    @pytest.mark.asyncio
    async def test_an_embedding_provider_keeps_its_token(self, system_toolset, sp) -> None:
        await _create(system_toolset, "embedding_provider", _emb_body())
        served = await _served(system_toolset, "embedding_provider", "hf-1")

        result = await system_toolset.call(tool_name="update_embedding_provider", arguments={"id": "hf-1", "entity": served})

        assert not result.is_error, result.output
        assert _stored_hf_token(sp) == HF_TOKEN

    @pytest.mark.asyncio
    async def test_a_toolset_keeps_the_secrets_in_its_env_map(self, system_toolset, sp) -> None:
        await _create(system_toolset, "toolset", _toolset_with_env())
        served = await _served(system_toolset, "toolset", "ts-1")
        assert served["config"]["config"]["env"]["API_TOKEN"] != ENV_TOKEN, "precondition: the env secret is served masked"

        result = await system_toolset.call(
            tool_name="update_toolset", arguments={"id": "ts-1", "entity": served}, ctx=ADMIN_CALLER,
        )

        assert not result.is_error, result.output
        assert _stored_env_token(sp) == ENV_TOKEN


def _channel_provider_body() -> dict:
    from pydantic import SecretStr

    from primer.model.channel import ChannelProvider, ChannelProviderType, SlackChannelProviderConfig

    body = ChannelProvider(
        id="cp-1",
        provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(app_token=SecretStr("xapp-1-live-0123456789"), bot_token=SecretStr("xoxb-live-9876543210")),
    ).model_dump(mode="json")
    body["config"]["app_token"] = "xapp-1-live-0123456789"
    body["config"]["bot_token"] = "xoxb-live-9876543210"
    return body


class TestEveryEntityNotOnlyTheOnesWithAnInvalidationHook:
    """The three entities above all carry a cache-invalidation hook. The protection is the generic update handler's, so an
    entity with secrets and NO hook (an artifact storage provider's S3 keys) is covered too."""

    @pytest.mark.asyncio
    async def test_an_artifact_storage_provider_keeps_its_s3_keys(self, system_toolset, sp) -> None:
        from pydantic import SecretStr

        from primer.model.provider import ArtifactStorageProvider
        from primer.model.providers.artifact import ArtifactStorageProviderType, S3ArtifactConfig

        body = ArtifactStorageProvider(
            id="asp-1",
            provider=ArtifactStorageProviderType.S3,
            config=S3ArtifactConfig(bucket="b", access_key=SecretStr("AKIA-live-0123456789"), secret_key=SecretStr("sec-live-9876543210")),
        ).model_dump(mode="json")
        body["config"]["access_key"] = "AKIA-live-0123456789"
        body["config"]["secret_key"] = "sec-live-9876543210"
        await _create(system_toolset, "artifact_storage_provider", body)
        served = await _served(system_toolset, "artifact_storage_provider", "asp-1")
        assert served["config"]["secret_key"] != "sec-live-9876543210", "precondition: served masked"

        result = await system_toolset.call(tool_name="update_artifact_storage_provider", arguments={"id": "asp-1", "entity": served})

        assert not result.is_error, result.output
        config = sp.get_storage(ArtifactStorageProvider)._data["asp-1"].config
        assert config.access_key.get_secret_value() == "AKIA-live-0123456789"
        assert config.secret_key.get_secret_value() == "sec-live-9876543210"


class TestAModelThatValidatesItsSecretRefusesTheMaskInsteadOfStoringIt:
    """A Slack token must start with ``xapp-`` / ``xoxb-``, so the served mask never validates: the body is rejected before any
    secret could be restored (the REST route does the same: its PUT validates the body before the pre-update hook). Not
    round-trippable, but nothing is corrupted: the stored tokens are untouched, and a real rotation still works. Tracked as its
    own follow-up; this pins that the failure is loud and harmless."""

    @pytest.mark.asyncio
    async def test_a_masked_channel_provider_body_is_refused_and_the_tokens_are_untouched(self, system_toolset, sp) -> None:
        from primer.model.channel import ChannelProvider

        await _create(system_toolset, "channel_provider", _channel_provider_body())
        served = await _served(system_toolset, "channel_provider", "cp-1")

        result = await system_toolset.call(tool_name="update_channel_provider", arguments={"id": "cp-1", "entity": served})

        assert result.is_error and json.loads(result.output)["type"] == "validation-error"
        config = sp.get_storage(ChannelProvider)._data["cp-1"].config
        assert config.bot_token.get_secret_value() == "xoxb-live-9876543210"
        assert config.app_token.get_secret_value() == "xapp-1-live-0123456789"

    @pytest.mark.asyncio
    async def test_a_real_token_rotation_is_stored(self, system_toolset, sp) -> None:
        from primer.model.channel import ChannelProvider

        await _create(system_toolset, "channel_provider", _channel_provider_body())
        body = _channel_provider_body()
        body["config"]["bot_token"] = "xoxb-rotated-1122334455"

        result = await system_toolset.call(tool_name="update_channel_provider", arguments={"id": "cp-1", "entity": body})

        assert not result.is_error, result.output
        assert sp.get_storage(ChannelProvider)._data["cp-1"].config.bot_token.get_secret_value() == "xoxb-rotated-1122334455"


class TestARealChangeIsStillStored:
    @pytest.mark.asyncio
    async def test_a_new_api_key_replaces_the_stored_one(self, system_toolset, sp) -> None:
        await _create(system_toolset, "llm_provider", _llm_body())
        served = await _served(system_toolset, "llm_provider", "anthropic-1")
        served["config"]["api_key"] = "sk-rotated-9999999999"

        result = await system_toolset.call(tool_name="update_llm_provider", arguments={"id": "anthropic-1", "entity": served})

        assert not result.is_error, result.output
        assert _stored_llm_key(sp) == "sk-rotated-9999999999"

    @pytest.mark.asyncio
    async def test_a_new_env_secret_replaces_the_stored_one_and_the_rest_are_kept(self, system_toolset, sp) -> None:
        body = _toolset_with_env()
        body["config"]["config"]["env"]["OTHER"] = "other-live-123456"
        await _create(system_toolset, "toolset", body)
        served = await _served(system_toolset, "toolset", "ts-1")
        served["config"]["config"]["env"]["API_TOKEN"] = "tok-rotated-zzzzzz"

        result = await system_toolset.call(
            tool_name="update_toolset", arguments={"id": "ts-1", "entity": served}, ctx=ADMIN_CALLER,
        )

        assert not result.is_error, result.output
        env = sp.get_storage(Toolset)._data["ts-1"].config.config.env
        assert env["API_TOKEN"].get_secret_value() == "tok-rotated-zzzzzz"
        assert env["OTHER"].get_secret_value() == "other-live-123456", "an untouched secret was lost beside a rotated one"


class TestAnEntityWithoutSecretsIsUnaffected:
    @pytest.mark.asyncio
    async def test_an_agent_updates_as_before(self, system_toolset, sp) -> None:
        from tests.toolset.test_system import _agent

        body = _agent().model_dump(mode="json")
        await _create(system_toolset, "agent", body)
        served = await _served(system_toolset, "agent", "agt-1")
        served["description"] = "edited"

        result = await system_toolset.call(tool_name="update_agent", arguments={"id": "agt-1", "entity": served})

        assert not result.is_error, result.output
        assert json.loads(result.output)["description"] == "edited"
