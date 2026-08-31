"""Tail-revealing API-key masking (platform wave P2 addendum, B).

SecretStr's default json-mode serialization is a fixed "**********",
dropping any signal about which key is which. Signed off for
api-key-class secrets ONLY: LLM/embedding provider api_key-style
fields reveal the last 4 characters ("**********3f9a", GitHub/Stripe-
style); passwords and DSN-credential fields (storage.py's `password`,
cross_encoder.py's `token`, artifact.py's access_key/secret_key,
toolset.py's client_secret/env/headers) keep the full, tail-less mask
and are NOT touched by this change.
"""

from __future__ import annotations

from pydantic import SecretStr

from primer.model.providers._shared import _mask_with_tail
from primer.model.providers.cross_encoder import HuggingFaceCrossEncoderConfig
from primer.model.providers.embedding import HuggingFaceConfig, OpenAIConfig
from primer.model.providers.llm import (
    AnthropicConfig,
    GoogleConfig,
    OllamaConfig,
    OpenChatConfig,
    OpenRouterConfig,
)


def test_mask_with_tail_reveals_last_four_characters() -> None:
    assert _mask_with_tail(SecretStr("sk-ant-abcdef123f9a")) == "**********3f9a"


def test_mask_with_tail_stays_fully_masked_for_short_values() -> None:
    """A <=4 char value has no tail worth revealing - stays the plain,
    unmodified mask rather than exposing the whole thing."""
    assert _mask_with_tail(SecretStr("abcd")) == "**********"
    assert _mask_with_tail(SecretStr("")) == "**********"


def test_python_mode_dump_is_unaffected() -> None:
    """when_used='json' only - primer/api/routers/providers.py's
    discover_saved_llm_models dumps mode='python' to recover the REAL
    secret for an actual upstream probe; that path must keep getting
    the plain SecretStr instance, not this serializer's masked string."""
    cfg = AnthropicConfig(api_key=SecretStr("sk-ant-real-secret-9a9a"))
    dumped = cfg.model_dump(mode="python")
    assert isinstance(dumped["api_key"], SecretStr)
    assert dumped["api_key"].get_secret_value() == "sk-ant-real-secret-9a9a"


class TestInScopeFieldsRevealTheTail:
    """Every LLM/embedding api_key-style field uses ApiKeySecret."""

    def test_openresponses_and_openchat_share_the_http_api_key_field(self) -> None:
        cfg = OpenChatConfig(
            url="http://example.invalid", api_key=SecretStr("sk-oc-1234abcd"),
        )
        assert cfg.model_dump(mode="json")["api_key"] == "**********abcd"

    def test_google_gemini(self) -> None:
        cfg = GoogleConfig(api_key=SecretStr("AIzaSyABCDEFwxyz"))
        assert cfg.model_dump(mode="json")["api_key"] == "**********wxyz"

    def test_anthropic(self) -> None:
        cfg = AnthropicConfig(api_key=SecretStr("sk-ant-00001f9a"))
        assert cfg.model_dump(mode="json")["api_key"] == "**********1f9a"

    def test_ollama(self) -> None:
        cfg = OllamaConfig(
            url="http://localhost:11434", api_key=SecretStr("bearer-tok-42ab"),
        )
        assert cfg.model_dump(mode="json")["api_key"] == "**********42ab"

    def test_openrouter(self) -> None:
        cfg = OpenRouterConfig(api_key=SecretStr("sk-or-v1-99998888"))
        assert cfg.model_dump(mode="json")["api_key"] == "**********8888"

    def test_embedding_openai_compatible(self) -> None:
        cfg = OpenAIConfig(
            url="http://example.invalid", api_key=SecretStr("sk-emb-1111zzzz"),
        )
        assert cfg.model_dump(mode="json")["api_key"] == "**********zzzz"

    def test_embedding_huggingface_token(self) -> None:
        cfg = HuggingFaceConfig(token=SecretStr("hf_secretTOKEN9999"))
        assert cfg.model_dump(mode="json")["token"] == "**********9999"


class TestOutOfScopeFieldsStayFullyMasked:
    """Neither cross-encoder tokens nor storage/toolset credentials are
    api_key-class secrets per the sign-off - full mask, no tail."""

    def test_cross_encoder_token_has_no_tail(self) -> None:
        cfg = HuggingFaceCrossEncoderConfig(token=SecretStr("hf_crossenc1234"))
        assert cfg.model_dump(mode="json")["token"] == "**********"
