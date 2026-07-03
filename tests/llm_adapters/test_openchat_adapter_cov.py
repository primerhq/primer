"""Coverage tests for the OpenChatLLM adapter (Chat Completions).

Placed outside ``tests/llm/`` so ``primer.llm.openchat`` counts in the
CI unit sweep. The openai SDK client is patched with a MagicMock whose
``chat.completions.create`` returns an async iterator of fake chunks, so
these tests are pure in-process — no network IO.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pytest
from pydantic import BaseModel as PydanticBaseModel
from pydantic import HttpUrl, SecretStr

from primer.llm.openchat import OpenChatLLM, _POLICY_BY_FLAVOR
from primer.model.chat import (
    Error as ChatError,
    Message,
    StreamStart,
    TextPart,
    Tool,
)
from primer.model.except_ import (
    AuthenticationError,
    ConfigError,
    ModelNotFoundError,
    ProviderTimeoutError,
    RateLimitError,
)
from primer.model.provider import (
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)


def _make_provider(
    *,
    flavor: OpenChatFlavor = OpenChatFlavor.OPENAI,
    api_key: str | None = "sk-test",
    models: list[str] | None = None,
    max_concurrency: int = 4,
    url: str = "https://api.openai.com/v1/",
    total_timeout_seconds: float | None = None,
) -> LLMProvider:
    return LLMProvider(
        id="openchat-cov",
        provider=LLMProviderType.OPENCHAT,
        models=[
            LLMModel(name=name, context_length=8192)
            for name in (models or ["gpt-4o-mini"])
        ],
        config=OpenChatConfig(
            url=HttpUrl(url),
            api_key=SecretStr(api_key) if api_key is not None else None,
            flavor=flavor,
        ),
        limits=Limits(
            max_concurrency=max_concurrency,
            total_timeout_seconds=total_timeout_seconds,
        ),
    )


async def _aiter(items: list) -> AsyncIterator:
    for item in items:
        yield item


def _patched_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_instance = MagicMock()
    mock_instance.chat = MagicMock()
    mock_instance.chat.completions = MagicMock()
    mock_instance.chat.completions.create = AsyncMock()
    cls_mock = MagicMock(return_value=mock_instance)
    monkeypatch.setattr("primer.llm.openchat.AsyncOpenAI", cls_mock)
    return mock_instance


def _make_openai_error(cls: type, *, status_code: int = 400, code: str | None = None):
    exc = cls.__new__(cls)
    exc.status_code = status_code
    exc.code = code
    exc.message = f"test {cls.__name__}"
    Exception.__init__(exc, exc.message)
    return exc


def _simple_seq(model: str = "gpt-4o-mini") -> list[Any]:
    return [
        NS(
            id="chatcmpl-1",
            model=model,
            choices=[NS(index=0, delta=NS(role="assistant", content=None, tool_calls=None), finish_reason=None)],
            usage=None,
        ),
        NS(
            id="chatcmpl-1",
            model=model,
            choices=[NS(index=0, delta=NS(role=None, content="hello", tool_calls=None), finish_reason=None)],
            usage=None,
        ),
        NS(
            id="chatcmpl-1",
            model=model,
            choices=[NS(index=0, delta=NS(role=None, content=None, tool_calls=None), finish_reason="stop")],
            usage=NS(prompt_tokens=4, completion_tokens=2),
        ),
    ]


class TestFlavorPolicy:
    @pytest.mark.parametrize(
        "flavor, requires",
        [
            (OpenChatFlavor.OPENAI, True),
            (OpenChatFlavor.LMSTUDIO, False),
            (OpenChatFlavor.OLLAMA, False),
            (OpenChatFlavor.VLLM, False),
            (OpenChatFlavor.OTHER, True),
        ],
    )
    def test_policy_table(self, flavor: OpenChatFlavor, requires: bool) -> None:
        assert _POLICY_BY_FLAVOR[flavor].require_api_key is requires

    def test_policy_frozen(self) -> None:
        with pytest.raises(Exception):
            _POLICY_BY_FLAVOR[OpenChatFlavor.OPENAI].require_api_key = False  # type: ignore[misc]


class TestConstructor:
    def test_valid_openai(self) -> None:
        llm = OpenChatLLM(_make_provider())
        assert llm._policy is _POLICY_BY_FLAVOR[OpenChatFlavor.OPENAI]
        assert llm._client is None

    def test_lmstudio_no_key_ok(self) -> None:
        llm = OpenChatLLM(
            _make_provider(flavor=OpenChatFlavor.LMSTUDIO, api_key=None, url="http://localhost:1234/v1/")
        )
        assert llm._policy.require_api_key is False

    def test_empty_key_openai_raises(self) -> None:
        with pytest.raises(ConfigError, match="api_key is required"):
            OpenChatLLM(_make_provider(api_key=""))

    def test_missing_key_other_flavor_raises(self) -> None:
        with pytest.raises(ConfigError, match="api_key is required"):
            OpenChatLLM(
                _make_provider(flavor=OpenChatFlavor.OTHER, api_key=None, url="https://api.example.com/v1/")
            )

    def test_wrong_provider_type_raises(self) -> None:
        provider = _make_provider()
        object.__setattr__(provider, "provider", LLMProviderType.OPENRESPONSES)
        with pytest.raises(ConfigError, match="OPENCHAT"):
            OpenChatLLM(provider)

    def test_wrong_config_type_raises(self) -> None:
        from primer.model.provider import OpenResponsesConfig

        provider = LLMProvider(
            id="x",
            provider=LLMProviderType.OPENCHAT,
            models=[LLMModel(name="gpt-4o-mini", context_length=8192)],
            config=OpenResponsesConfig(url=HttpUrl("https://x/v1/"), api_key=SecretStr("sk-x")),
            limits=Limits(max_concurrency=1),
        )
        with pytest.raises(ConfigError, match="OpenChatConfig"):
            OpenChatLLM(provider)

    def test_logs_init(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="primer.llm.openchat")
        OpenChatLLM(_make_provider(models=["gpt-4o-mini", "gpt-4o"], max_concurrency=2))
        records = [r for r in caplog.records if "OpenChat adapter" in r.message]
        assert len(records) == 1
        assert records[0].flavor == "openai"  # type: ignore[attr-defined]


class TestGetClient:
    def test_client_uses_config_key(self) -> None:
        llm = OpenChatLLM(_make_provider(api_key="sk-xyz"))
        client = llm._get_client()
        assert client.api_key == "sk-xyz"
        assert str(client.base_url).rstrip("/").endswith("openai.com/v1")

    def test_client_no_key_uses_placeholder(self) -> None:
        llm = OpenChatLLM(
            _make_provider(flavor=OpenChatFlavor.LMSTUDIO, api_key=None, url="http://localhost:1234/v1/")
        )
        client = llm._get_client()
        assert client.api_key == "no-key-required"

    def test_client_cached(self) -> None:
        llm = OpenChatLLM(_make_provider())
        assert llm._get_client() is llm._get_client()


class TestListModelsAndTokens:
    async def test_list_models_configured(self) -> None:
        llm = OpenChatLLM(_make_provider(models=["gpt-4o-mini", "gpt-4o"]))
        assert list(await llm.list_models()) == ["gpt-4o-mini", "gpt-4o"]

    async def test_list_models_no_upstream_call(self) -> None:
        llm = OpenChatLLM(_make_provider())
        with patch.object(OpenChatLLM, "_get_client") as mock:
            await llm.list_models()
            mock.assert_not_called()

    async def test_count_tokens_positive(self) -> None:
        llm = OpenChatLLM(_make_provider(models=["gpt-4o"]))
        n = await llm.count_tokens(
            model="gpt-4o",
            messages=[Message(role="user", parts=[TextPart(text="hello world")])],
            tools=None,
        )
        assert isinstance(n, int) and n > 0


class TestAclose:
    async def test_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _patched_client(monkeypatch)
        client.close = AsyncMock()
        llm = OpenChatLLM(_make_provider())
        llm._get_client()
        await llm.aclose()
        await llm.aclose()
        assert client.close.await_count == 1


class TestStream:
    async def test_unknown_model_raises(self) -> None:
        llm = OpenChatLLM(_make_provider(models=["gpt-4o-mini"]))
        with pytest.raises(ModelNotFoundError, match="not-a-real-model"):
            async for _ in llm.stream(
                model="not-a-real-model",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_happy_path_event_sequence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_seq())
        kinds = [
            type(e).__name__
            async for e in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            )
        ]
        assert kinds == ["StreamStart", "TextDelta", "Usage", "Done"]

    async def test_request_payload_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_seq())

        class Out(PydanticBaseModel):
            value: int

        tool = Tool(
            id="search",
            description="Search",
            toolset_id="default",
            args_schema={"type": "object", "properties": {}, "required": []},
        )
        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            temperature=0.5,
            top_p=0.9,
            max_output_tokens=64,
            stop=["END"],
            tools=[tool],
            tool_choice="auto",
            response_format=Out,
            extended={"seed": 42, "frobnicate": True},
        ):
            pass
        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        assert kwargs["temperature"] == 0.5
        assert kwargs["top_p"] == 0.9
        assert kwargs["max_tokens"] == 64
        assert kwargs["stop"] == ["END"]
        assert kwargs["tools"][0]["function"]["name"] == "search"
        assert kwargs["tool_choice"] == "auto"
        assert kwargs["response_format"]["json_schema"]["name"] == "Out"
        assert kwargs["seed"] == 42
        assert "frobnicate" not in kwargs

    async def test_request_payload_omits_optionals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_seq())
        async for _ in llm.stream(
            model="gpt-4o-mini",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
        ):
            pass
        kwargs = client.chat.completions.create.call_args.kwargs
        for omitted in ("temperature", "top_p", "max_tokens", "stop", "tools", "tool_choice", "response_format"):
            assert omitted not in kwargs

    async def test_trace_llm_io_records_messages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenChatLLM(_make_provider(), trace_llm_io=True)
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_seq())
        kinds = [
            type(e).__name__
            async for e in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
                max_output_tokens=32,
            )
        ]
        assert kinds[-1] == "Done"

    async def test_pre_stream_auth_error_reraised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.side_effect = _make_openai_error(
            openai.AuthenticationError, status_code=401
        )
        with pytest.raises(AuthenticationError):
            async for _ in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

    async def test_mid_stream_error_yields_chat_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)

        async def failing() -> AsyncIterator:
            yield NS(
                id="x",
                model="gpt-4o-mini",
                choices=[NS(index=0, delta=NS(role="assistant", content=None, tool_calls=None), finish_reason=None)],
                usage=None,
            )
            raise _make_openai_error(openai.RateLimitError, status_code=429)

        client.chat.completions.create.return_value = failing()
        events = [
            e
            async for e in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            )
        ]
        assert isinstance(events[0], StreamStart)
        assert isinstance(events[-1], ChatError)
        assert events[-1].fatal is True

    async def test_generation_budget_maps_to_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from primer.llm._timeout import GenerationBudgetExceeded

        llm = OpenChatLLM(_make_provider(total_timeout_seconds=30.0))
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_seq())

        async def budget_iter(*_a, **_k):
            raise GenerationBudgetExceeded("over budget")
            yield  # pragma: no cover - marks this an async generator

        monkeypatch.setattr("primer.llm.openchat._iter_with_timeout", budget_iter)
        with pytest.raises(ProviderTimeoutError) as info:
            async for _ in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
        assert info.value.code == "generation_timeout"

    async def test_stall_timeout_maps_to_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenChatLLM(_make_provider())
        client = _patched_client(monkeypatch)
        client.chat.completions.create.return_value = _aiter(_simple_seq())

        async def stall_iter(*_a, **_k):
            raise TimeoutError("stall")
            yield  # pragma: no cover

        monkeypatch.setattr("primer.llm.openchat._iter_with_timeout", stall_iter)
        with pytest.raises(ProviderTimeoutError) as info:
            async for _ in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass
        assert info.value.code == "stream_timeout"


class TestConcurrency:
    async def test_semaphore_serialises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = OpenChatLLM(_make_provider(max_concurrency=1))
        client = _patched_client(monkeypatch)
        in_flight = 0
        peak = 0

        async def slow() -> AsyncIterator:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            yield NS(
                id="x",
                model="gpt-4o-mini",
                choices=[NS(index=0, delta=NS(role="assistant", content="hi", tool_calls=None), finish_reason="stop")],
                usage=NS(prompt_tokens=1, completion_tokens=1),
            )
            in_flight -= 1

        client.chat.completions.create.side_effect = lambda **_: slow()

        async def consume() -> None:
            async for _ in llm.stream(
                model="gpt-4o-mini",
                messages=[Message(role="user", parts=[TextPart(text="hi")])],
            ):
                pass

        await asyncio.gather(consume(), consume(), consume())
        assert peak == 1


class TestPackageReexport:
    def test_reexported(self) -> None:
        import primer.llm as pkg

        assert "OpenChatLLM" in pkg.__all__
        assert pkg.OpenChatLLM is OpenChatLLM
