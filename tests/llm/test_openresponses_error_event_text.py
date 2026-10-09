"""The OpenResponses ``error`` event reaches the stream with the provider's configured key and URL credentials masked (security ticket 01a11f35-ad20).

An in-stream EXCEPTION goes through ``describe_failure``; the ``error`` EVENT carries the provider's own text and used to reach the record untouched.
The adapter masks what only it can see (the configured key, Bearer and Basic tokens) with ``scrubbed_event_text``; the record writer then redacts the
URL-shaped credentials (``tests/session/test_error_records_carry_no_url_credentials.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import HttpUrl, SecretStr

from primer.llm.openresponses import OpenResponsesLLM
from primer.model.chat import Error, Message, TextPart
from primer.model.provider import Limits, LLMProvider, LLMProviderType, OpenResponsesConfig, OpenResponsesFlavor

LEAKY = "upstream refused https://svc-user:hunter2pw@gateway.internal/v1/chat?api_key=SKSECRET123456&x=1 (try again)"
API_KEY = "sk-live-Q7xZ9pL2mN4vB8kR1tY6wE3"


class _Stream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_the_openresponses_error_event_reaches_the_stream_without_credentials(monkeypatch):
    """The adapter itself masks what it can see: URL credentials AND the provider's configured key, which the record writer does not know."""
    provider = LLMProvider(
        id="or-box", provider=LLMProviderType.OPENRESPONSES, limits=Limits(max_concurrency=1),
        config=OpenResponsesConfig(url=HttpUrl("http://lmstudio.local:1234/v1"), api_key=SecretStr(API_KEY), flavor=OpenResponsesFlavor.LMSTUDIO),
    )
    event = SimpleNamespace(type="error", code="rate_limited", message=f"{LEAKY} key={API_KEY}")
    sdk = MagicMock()
    sdk.responses.create = AsyncMock(return_value=_Stream([event]))
    monkeypatch.setattr("primer.llm.openresponses.AsyncOpenAI", MagicMock(return_value=sdk))
    llm = OpenResponsesLLM(provider)

    events = [e async for e in llm.stream(model="qwen", messages=[Message(role="user", parts=[TextPart(text="hi")])])]

    errors = [e for e in events if isinstance(e, Error)]
    assert errors, events
    message = errors[0].message
    assert "hunter2pw" not in message and "SKSECRET123456" not in message and API_KEY not in message, message
    assert "gateway.internal" in message and "try again" in message, "the rest of the message must survive"
    assert errors[0].code == "rate_limited" and errors[0].fatal is False
