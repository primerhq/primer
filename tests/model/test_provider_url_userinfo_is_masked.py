"""A provider's Base URL is not served with its password (ticket 01a11cdf part 3, option A; lead rulings of 2026-10-09).

A provider ``url`` may carry ``user:password@`` (a reverse proxy in front of an LLM server): pydantic accepts it, httpx sends it as Basic auth, and every API response, event and
tool result carried it in clear next to a masked ``api_key``. The ``url`` of the LLM, embedding and speech configs is now a field type whose JSON-mode dump masks the password
(``http://svc:**********@host/v1``; a lone userinfo, ``https://TOKEN@host``, whole), while

* a PYTHON-mode dump and the object itself keep the real URL (the probes and the adapters read those),
* ``dump_for_storage`` keeps the real URL (storage must round-trip the credential, as it does for a ``SecretStr``),
* ``preserve_masked_secrets`` puts the stored credential back when a full-replace PUT sends the served mask back, as it does for ``api_key``.

The checks the lead asked for: a JSON-mode dump used as an IDENTITY (fingerprint, cache key, reload detection) would miss a password-only change, so the storage form must see
it; and an ``HttpUrl`` must still compare equal after a round trip.
"""

from __future__ import annotations

import pytest

from primer.model.common import STORAGE_DUMP_CONTEXT, dump_for_storage, preserve_masked_secrets
from primer.model.except_ import ValidationError as PrimerValidationError
from primer.model.providers.embedding import EmbeddingProvider, OpenAIConfig
from primer.model.providers.llm import LLMProvider, OllamaConfig, OpenChatConfig
from primer.model.providers.speech import SpeechToTextConfig, TextToSpeechConfig

MASK = "**********"
PROXY = "http://svc:s3cr3t@proxy.local:8080/v1"
MASKED = f"http://svc:{MASK}@proxy.local:8080/v1"


def _llm(url: str, api_key: str = "sk-live-abcdef") -> LLMProvider:
    return LLMProvider.model_validate({
        "id": "llm-a", "provider": "openchat", "models": [{"name": "m", "context_length": 8192}],
        "config": {"url": url, "api_key": api_key, "flavor": "other"}, "limits": {"max_concurrency": 1},
    })


def _embedding(url: str) -> EmbeddingProvider:
    return EmbeddingProvider.model_validate({
        "id": "emb-a", "provider": "openai", "models": [{"name": "m"}], "config": {"url": url}, "limits": {"max_concurrency": 1},
    })


# ---- which dump masks ----------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(OpenChatConfig(url=PROXY), id="LLM openchat"),
        pytest.param(OllamaConfig(url=PROXY), id="LLM ollama"),
        pytest.param(OpenAIConfig(url=PROXY), id="embedding openai"),
        pytest.param(SpeechToTextConfig(url=PROXY), id="speech to text"),
        pytest.param(TextToSpeechConfig(url=PROXY), id="text to speech"),
    ],
)
def test_a_json_dump_masks_the_password_and_the_other_dumps_do_not(config) -> None:
    assert config.model_dump(mode="json")["url"] == MASKED
    assert "s3cr3t" not in config.model_dump_json()
    assert str(config.model_dump(mode="python")["url"]) == PROXY, "the adapters and the probes read the python dump"
    assert str(config.url) == PROXY
    assert dump_for_storage(config)["url"] == PROXY, "storage must round-trip the credential"


def test_a_row_serves_masked_and_stores_real() -> None:
    row = _llm(PROXY)

    assert row.model_dump(mode="json")["config"]["url"] == MASKED
    assert dump_for_storage(row)["config"]["url"] == PROXY
    assert dump_for_storage(row)["config"]["api_key"] == "sk-live-abcdef", "the api_key keeps its own rules"


def test_a_url_without_userinfo_is_served_as_it_is() -> None:
    assert _llm("http://lmstudio.local:1234/v1").model_dump(mode="json")["config"]["url"] == "http://lmstudio.local:1234/v1"


def test_a_lone_token_in_the_username_slot_is_masked_whole() -> None:
    assert _llm("https://ghp_abcdefghij@proxy.local/v1").model_dump(mode="json")["config"]["url"] == f"https://{MASK}@proxy.local/v1"


# ---- storage keeps the credential, and the storage form sees a password-only change --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_credential_survives_a_real_sqlite_round_trip(tmp_path) -> None:
    from primer.storage.sqlite import SqliteConfig, SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    try:
        storage = sp.get_storage(LLMProvider)
        await storage.create(_llm(PROXY))

        back = await storage.get("llm-a")

        assert str(back.config.url) == PROXY
        assert back.model_dump(mode="json")["config"]["url"] == MASKED
    finally:
        await sp.aclose()


def test_the_storage_form_changes_when_only_the_password_does_and_the_wire_form_does_not() -> None:
    """The identity check: anything that fingerprints or compares a provider row must use the storage form. The served (masked) form is the same for both, by design."""
    old, new = _llm("http://svc:one@proxy.local/v1"), _llm("http://svc:two@proxy.local/v1")

    assert dump_for_storage(old) != dump_for_storage(new)
    assert old.model_dump(mode="json") == new.model_dump(mode="json")


def test_an_http_url_still_compares_equal_after_a_round_trip_through_the_storage_form() -> None:
    row = _llm("http://svc:p%40ss@proxy.local:8080/v1?x=1")

    again = LLMProvider.model_validate(dump_for_storage(row))

    assert again.config.url == row.config.url and str(again.config.url) == str(row.config.url)


# ---- a full-replace PUT of the served body ----------------------------------------------------------------------------------------------------------------------


def _put(stored: LLMProvider, served_url: str) -> LLMProvider:
    """What the PUT route does: the body parsed as the model, then preserve_masked_secrets against the stored row."""
    incoming = _llm(served_url, api_key=stored.config.api_key.get_secret_value())
    preserve_masked_secrets(incoming, stored)
    return incoming


def test_the_served_body_sent_back_unchanged_keeps_the_stored_password() -> None:
    stored = _llm(PROXY)

    assert str(_put(stored, MASKED).config.url) == PROXY


def test_a_changed_path_on_the_same_origin_keeps_the_password() -> None:
    stored = _llm(PROXY)

    assert str(_put(stored, f"http://svc:{MASK}@proxy.local:8080/v2?x=1").config.url) == "http://svc:s3cr3t@proxy.local:8080/v2?x=1"


def test_the_default_port_spelled_out_is_the_same_origin() -> None:
    stored = _llm("http://svc:s3cr3t@proxy.local/v1")

    assert str(_put(stored, f"http://svc:{MASK}@proxy.local:80/v1").config.url) == "http://svc:s3cr3t@proxy.local/v1"


@pytest.mark.parametrize(
    "incoming",
    [
        pytest.param(f"http://svc:{MASK}@other.local:9000/v2", id="another host"),
        pytest.param(f"http://svc:{MASK}@proxy.local:9000/v1", id="another port"),
        pytest.param(f"https://svc:{MASK}@proxy.local:8080/v1", id="another scheme"),
    ],
)
def test_a_changed_origin_with_the_mask_is_refused_not_given_the_stored_password(incoming: str) -> None:
    """An update that points the URL at another host and leaves the mask alone used to store the stored password next to that host: the next probe or call sent it there."""
    stored = _llm(PROXY)

    with pytest.raises(PrimerValidationError, match="re-enter the password") as caught:
        _put(stored, incoming)
    assert "s3cr3t" not in str(caught.value)


def test_a_new_real_password_is_stored() -> None:
    stored = _llm(PROXY)

    assert str(_put(stored, "http://svc:newpass@proxy.local:8080/v1").config.url) == "http://svc:newpass@proxy.local:8080/v1"


def test_another_username_with_the_mask_is_refused_and_the_literal_mask_is_never_stored() -> None:
    stored = _llm(PROXY)

    with pytest.raises(PrimerValidationError, match="re-enter the password"):
        _put(stored, f"http://other:{MASK}@proxy.local:8080/v1")


def test_removing_the_credential_removes_it() -> None:
    stored = _llm(PROXY)

    assert str(_put(stored, "http://proxy.local:8080/v1").config.url) == "http://proxy.local:8080/v1"


def test_a_lone_token_comes_back_when_the_mask_is_sent_back() -> None:
    stored = _llm("https://ghp_abcdefghij@proxy.local/v1")

    assert str(_put(stored, f"https://{MASK}@proxy.local/v1").config.url) == "https://ghp_abcdefghij@proxy.local/v1"


def test_the_api_key_is_still_preserved_beside_the_url() -> None:
    stored = _llm(PROXY)
    incoming = _llm(MASKED, api_key="**********cdef")

    preserve_masked_secrets(incoming, stored)

    assert incoming.config.api_key.get_secret_value() == "sk-live-abcdef" and str(incoming.config.url) == PROXY


def test_an_embedding_row_follows_the_same_rules() -> None:
    stored = _embedding(PROXY)
    incoming = _embedding(MASKED)

    preserve_masked_secrets(incoming, stored)

    assert str(incoming.config.url) == PROXY
    assert stored.model_dump(mode="json")["config"]["url"] == MASKED


def test_a_mask_for_a_row_that_stored_no_credential_is_refused() -> None:
    stored = _llm("http://proxy.local:8080/v1")

    with pytest.raises(PrimerValidationError, match="re-enter the password"):
        _put(stored, MASKED)


def test_a_mask_when_the_provider_type_changed_is_refused() -> None:
    """The stored row is an openchat config; the update makes it an Ollama one and sends the served mask: nothing of the stored credential belongs to the new config."""
    stored = _llm(PROXY)
    incoming = LLMProvider.model_validate({
        "id": "llm-a", "provider": "ollama", "models": [{"name": "m", "context_length": 8192}], "config": {"url": MASKED}, "limits": {"max_concurrency": 1},
    })

    with pytest.raises(PrimerValidationError, match="re-enter the password"):
        preserve_masked_secrets(incoming, stored)


def test_a_restored_url_over_the_length_limit_is_a_refusal_not_a_crash() -> None:
    """The stored URL (a very long password) fits the 2083 characters, the masked one sent back plus a long path does not once the password is back."""
    stored = _llm(f"http://svc:{'p' * 2000}@proxy.local:8080/v1")
    incoming = _llm(f"http://svc:{MASK}@proxy.local:8080/v1/{'a' * 300}")

    with pytest.raises(PrimerValidationError, match="too long"):
        preserve_masked_secrets(incoming, stored)


def test_an_unrestorable_mask_is_refused_in_a_nested_row_too() -> None:
    stored = _embedding("http://proxy.local:8080/v1")
    incoming = _embedding(MASKED)

    with pytest.raises(PrimerValidationError, match="re-enter the password"):
        preserve_masked_secrets(incoming, stored)


# ---- a python-mode dump must not warn (the warning printed the URL, password included, to stderr) ---------------------------------------------------------------------------

_CONFIGS = [
    pytest.param(OpenChatConfig(url=PROXY), id="LLM openchat"),
    pytest.param(OllamaConfig(url=PROXY), id="LLM ollama"),
    pytest.param(OpenAIConfig(url=PROXY), id="embedding openai"),
    pytest.param(SpeechToTextConfig(url=PROXY), id="speech to text"),
    pytest.param(TextToSpeechConfig(url=PROXY), id="text to speech"),
]


@pytest.mark.filterwarnings("error")
@pytest.mark.parametrize("config", _CONFIGS)
def test_no_dump_of_a_config_warns(config) -> None:
    """pydantic 2.13 warned (PydanticSerializationUnexpectedValue, with the URL in the text) on every PYTHON-mode dump of a field whose serializer declared ``return_type=str``;
    ``discover_saved_llm_models`` does that dump on every ``GET /v1/setup/state``."""
    assert str(config.model_dump()["url"]) == PROXY
    assert str(config.model_dump(mode="python")["url"]) == PROXY
    assert config.model_dump(mode="json")["url"] == MASKED
    assert MASK in config.model_dump_json()
    assert dump_for_storage(config)["url"] == PROXY


@pytest.mark.filterwarnings("error")
def test_no_dump_of_a_row_with_a_config_union_warns() -> None:
    for row in (_llm(PROXY), _embedding(PROXY)):
        assert str(row.config.model_dump(mode="python")["url"]) == PROXY
        assert str(row.model_dump(mode="python")["config"]["url"]) == PROXY
        row.model_dump(mode="json")
        row.model_dump_json()
        dump_for_storage(row)


# ---- the storage context is exactly the storage context ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("context", [{"storage": False}, {"other": True}, {}, "storage", None, {"storage": 1.5}])
def test_only_the_storage_context_returns_the_real_url(context) -> None:
    assert OpenChatConfig(url=PROXY).model_dump(mode="json", context=context)["url"] == MASKED


def test_the_storage_context_returns_the_real_url_even_with_more_keys() -> None:
    assert OpenChatConfig(url=PROXY).model_dump(mode="json", context={**STORAGE_DUMP_CONTEXT, "other": 1})["url"] == PROXY


def test_the_serialization_schema_of_the_field_is_still_a_uri_string() -> None:
    """A serializer with a return type of ``Any`` would turn the field into ``{}`` in the OpenAPI document."""
    properties = OpenChatConfig.model_json_schema(mode="serialization")["properties"]["url"]

    assert properties["type"] == "string" and properties["format"] == "uri"
