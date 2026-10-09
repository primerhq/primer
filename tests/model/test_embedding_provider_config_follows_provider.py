"""An EmbeddingProvider's config is read as the class its ``provider`` names (ticket 01a11cdf part 2; found in the review of #580).

``EmbeddingProvider.config`` is a plain union of three classes. pydantic's smart mode validates the input against each member and keeps the one that sets the most fields, so
an ``openai`` row whose url is invalid (or missing) failed ``OpenAIConfig`` and was left with another member (``GoogleConfig`` with its key, or a ``HuggingFaceConfig`` once the
token is optional) with ``api_key`` None: the url was silently dropped, a row with no endpoint could be saved, and ``_discover_models`` went on to send the raw typed string to
httpx. ``LLMProvider`` has had a provider-keyed before-validator for the same reason (``_coerce_config_to_provider``); the embedding provider has one now.
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


@pytest.mark.parametrize("config", [{}, {"token": None}, {"token": ""}, {"url": "http://x.local/v1"}], ids=["empty", "null token", "empty token", "a stray url"])
def test_a_huggingface_row_needs_no_token(config: dict) -> None:
    """A local model such as all-MiniLM-L6-v2 is public: the token is for gated repos only (as ``HuggingFaceCrossEncoderConfig.token`` says), and the embedder already passes
    ``token_value or None``. #645's validator enforced the field's old ``Field(...)`` at create, where main never validated the config at all: ``POST`` of a huggingface embedder
    with ``config: {}`` answered 422 ``missing body.token`` (e2e: test_collection_documents_by_path and two more)."""
    row = EmbeddingProvider.model_validate(_row("huggingface", config))

    assert type(row.config) is HuggingFaceConfig
    assert row.config.token is None or row.config.token.get_secret_value() == ""


def test_the_huggingface_config_class_alone_needs_no_token() -> None:
    assert HuggingFaceConfig().token is None


def test_a_huggingface_row_keeps_a_token_it_is_given() -> None:
    row = EmbeddingProvider.model_validate(_row("huggingface", {"token": "hf_abcdefghijklmnop"}))

    assert row.config.token is not None and row.config.token.get_secret_value() == "hf_abcdefghijklmnop"
    assert row.model_dump(mode="json")["config"]["token"].startswith("*"), "a token that is given is still served masked"


def test_an_unknown_provider_is_the_providers_own_error_not_a_crash() -> None:
    with pytest.raises(ValidationError) as caught:
        EmbeddingProvider.model_validate(_row("deepmind", {"url": "http://x.local/v1"}))

    assert any(error["loc"][0] == "provider" for error in caught.value.errors())


def test_a_config_that_is_already_an_instance_passes_through() -> None:
    config = OpenAIConfig(url="http://emb.local:1234/v1")

    row = EmbeddingProvider.model_validate({**_row("openai", {}), "config": config})

    assert row.config is config or row.config == config


@pytest.mark.parametrize(
    ("provider", "config", "wanted"),
    [
        ("huggingface", OpenAIConfig(url="http://emb.local:1234/v1"), "HuggingFaceConfig"),
        ("openai", HuggingFaceConfig(token="hf_abcdefghijklmnop"), "OpenAIConfig"),
        ("openai", GoogleConfig(api_key="k"), "OpenAIConfig"),
        ("gemini", OpenAIConfig(url="http://emb.local:1234/v1"), "GoogleConfig"),
    ],
)
def test_a_config_object_of_the_wrong_class_is_refused(provider: str, config: object, wanted: str) -> None:
    """The validator only looked at a dict: an object of another class went through the union as it was, so ``provider="huggingface"`` could carry an ``OpenAIConfig``."""
    with pytest.raises(ValidationError) as caught:
        EmbeddingProvider.model_validate({**_row(provider, {}), "config": config})

    message = str(caught.value.errors()[0]["msg"])
    assert wanted in message and provider in message, message
    assert "hf_abcdefghijklmnop" not in message and "emb.local" not in message, "the message names the classes, never the config"


def test_a_stored_row_reads_back_as_the_same_class() -> None:
    """The storage round trip validates a dict again."""
    row = EmbeddingProvider.model_validate(_row("openai", {"url": "http://emb.local:1234/v1", "api_key": "k"}))

    again = EmbeddingProvider.model_validate(row.model_dump(mode="python"))

    assert type(again.config) is OpenAIConfig and str(again.config.url) == str(row.config.url)
