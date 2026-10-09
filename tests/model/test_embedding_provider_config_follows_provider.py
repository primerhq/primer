"""An EmbeddingProvider's config is read as the class its ``provider`` names (ticket 01a11cdf part 2; found in the review of #580).

``EmbeddingProvider.config`` is a plain union of three classes. pydantic tries them in order and takes the first that validates, so an ``openai`` row whose url is invalid
(or missing) failed ``OpenAIConfig``, failed ``HuggingFaceConfig`` (no token), and validated as ``GoogleConfig`` with ``api_key`` None: the url was silently dropped, a row
with no endpoint could be saved, and ``_discover_models`` went on to send the raw typed string to httpx. ``LLMProvider`` has had a provider-keyed before-validator for the
same reason (``_coerce_config_to_provider``); the embedding provider has one now.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.providers.embedding import EmbeddingProvider, HuggingFaceConfig, OpenAIConfig
from primer.model.providers.llm import GoogleConfig


def _row(provider: str, config: object) -> dict:
    return {"id": "emb-1", "provider": provider, "models": [{"name": "m"}], "config": config, "limits": {"max_concurrency": 1}}


@pytest.mark.parametrize(
    ("provider", "config", "expected"),
    [
        ("openai", {"url": "http://emb.local:1234/v1", "api_key": "k"}, OpenAIConfig),
        ("huggingface", {"token": "hf_abcdefghijklmnop"}, HuggingFaceConfig),
        ("gemini", {"api_key": "AIza-key"}, GoogleConfig),
    ],
)
def test_a_valid_row_gets_the_config_class_its_provider_names(provider: str, config: dict, expected: type) -> None:
    assert type(EmbeddingProvider.model_validate(_row(provider, config)).config) is expected


@pytest.mark.parametrize("config", [{"url": "not a url", "api_key": "k"}, {"url": "http://emb.local:notaport/v1"}, {"api_key": "k"}, {}])
def test_an_openai_row_whose_url_is_invalid_or_missing_is_refused_at_the_url(config: dict) -> None:
    """It used to validate as a GoogleConfig with the url dropped. (A ValidationError raised inside the before-validator keeps the config class's own loc, ``("url",)``, as LLMProvider's does.)"""
    with pytest.raises(ValidationError) as caught:
        EmbeddingProvider.model_validate(_row("openai", config))

    assert any(error["loc"][-1] == "url" for error in caught.value.errors()), caught.value.errors()


def test_a_huggingface_row_without_a_token_is_refused_at_the_token() -> None:
    with pytest.raises(ValidationError) as caught:
        EmbeddingProvider.model_validate(_row("huggingface", {"url": "http://x.local/v1"}))

    assert any(error["loc"][-1] == "token" for error in caught.value.errors()), caught.value.errors()


def test_an_unknown_provider_is_the_providers_own_error_not_a_crash() -> None:
    with pytest.raises(ValidationError) as caught:
        EmbeddingProvider.model_validate(_row("deepmind", {"url": "http://x.local/v1"}))

    assert any(error["loc"][0] == "provider" for error in caught.value.errors())


def test_a_config_that_is_already_an_instance_passes_through() -> None:
    config = OpenAIConfig(url="http://emb.local:1234/v1")

    row = EmbeddingProvider.model_validate({**_row("openai", {}), "config": config})

    assert row.config is config or row.config == config


def test_a_stored_row_reads_back_as_the_same_class() -> None:
    """The storage round trip validates a dict again."""
    row = EmbeddingProvider.model_validate(_row("openai", {"url": "http://emb.local:1234/v1", "api_key": "k"}))

    again = EmbeddingProvider.model_validate(row.model_dump(mode="python"))

    assert type(again.config) is OpenAIConfig and str(again.config.url) == str(row.config.url)
