"""The OpenAI-family classifier maps every 4xx (Part 2 T51, AG-M19).

402, 403 and 404 stayed a bare ``ProviderError``, which an aggregated pool does not fail over on, while the Anthropic,
Google and Ollama classifiers already map them. A 403 is now an authentication failure and any other 4xx a bad request.
"""

from __future__ import annotations

import openai
import pytest

from primer.common.openai_errors import classify_openai_exception
from primer.llm.aggregated import AggregatedLLM
from primer.model.chat import Done, Message, StreamStart, TextDelta, TextPart
from primer.model.except_ import AuthenticationError, BadRequestError
from primer.model.model_profile import ModelProfile, ModelProfileConfig
from primer.model_profile import ResolvedModel
from tests.llm.test_adapter_characterization import _ADAPTERS, _collect, _provider, _route, _sdk_http

_HTTP = _sdk_http(openai.AsyncOpenAI(api_key="probe"))


def _status_error(cls, status: int, code: str | None = None) -> openai.APIStatusError:
    request = _HTTP.Request("POST", "http://x/v1/chat/completions")
    body = {"message": "nope", **({"code": code} if code else {})}  # the SDK passes the inner ``error`` object
    return cls("nope", response=_HTTP.Response(status, request=request), body=body)


@pytest.mark.parametrize(("cls", "status", "expected", "code"), [
    (openai.PermissionDeniedError, 403, AuthenticationError, "auth_error"),
    (openai.NotFoundError, 404, BadRequestError, "bad_request"),
    (openai.ConflictError, 409, BadRequestError, "bad_request"),
    (openai.APIStatusError, 402, BadRequestError, "bad_request"),
    (openai.APIStatusError, 413, BadRequestError, "bad_request"),
])
def test_the_classifier_maps_every_4xx(cls, status, expected, code) -> None:
    err = classify_openai_exception(_status_error(cls, status))
    assert (type(err), err.code, err.status_code) == (expected, code, status)


def test_a_provider_code_is_kept() -> None:
    err = classify_openai_exception(_status_error(openai.NotFoundError, 404, code="model_not_found"))
    assert (type(err), err.code) == (BadRequestError, "model_not_found")


@pytest.mark.parametrize("kind", ["openchat", "openrouter", "openresponses"])
@pytest.mark.parametrize(("status", "expected", "code"), [
    (403, AuthenticationError, "auth_error"), (404, BadRequestError, "bad_request"), (402, BadRequestError, "bad_request"),
])
async def test_the_adapters_raise_the_mapped_error(monkeypatch, kind, status, expected, code) -> None:
    _route(monkeypatch, kind, [(status, b'{"error": {"message": "nope"}}', {})])
    with pytest.raises(expected) as caught:
        await _collect(_ADAPTERS[kind](_provider(kind)), kind)
    assert (caught.value.code, caught.value.status_code) == (code, status)


class _Member:
    def __init__(self, events=None, raises=None) -> None:
        self._events, self._raises = events or [], raises

    def profile_extended(self, config, *, model=None):
        return {}

    async def stream(self, **kwargs):
        if self._raises is not None:
            raise self._raises
        for event in self._events:
            yield event


async def test_a_pool_fails_over_on_an_openai_family_404() -> None:
    members = {
        "ma": _Member(raises=classify_openai_exception(_status_error(openai.NotFoundError, 404))),
        "mb": _Member([StreamStart(request_id="b", model="b"), TextDelta(text="ok", index=0),
                       Done(stop_reason="stop", raw_reason="stop")]),
    }

    async def resolve(member_id):
        return members[member_id], ResolvedModel(
            profile_id=member_id, provider_id="p", model_name="m", context_length=8192, config=ModelProfileConfig(),
        )

    profile = ModelProfile(id="agg", description="d", kind="aggregated", members=["ma", "mb"])
    events = [e async for e in AggregatedLLM(profile, resolve_member=resolve).stream(
        model="x", messages=[Message(role="user", parts=[TextPart(text="hi")])],
    )]
    assert [type(e).__name__ for e in events] == ["StreamStart", "TextDelta", "Done"]
