"""A masked secret is restored only for the origin it was stored for (ticket 01a1212a, part A; the twin of #691's rule for a URL password).

``GET`` serves every secret masked and ``PUT`` is a full replace, so ``preserve_masked_secrets`` swaps a served mask back for the stored value. It did that whatever the config's base URL became: an
update that sent ``{url: "https://attacker.example/v1", api_key: "<the mask>"}`` stored the REAL key next to the attacker's host, and the next probe or call sent it there (an admin, or an agent run an
admin started, can send that: ``update_llm_provider``, ``update_embedding_provider``, the speech and artifact tools, REST ``PUT``).

The rule now: a secret that sits next to an origin (a ``url`` / ``endpoint_url`` / ``apiserver_url`` / ``discovery_url`` / ``git_url`` / ``resource_uri``, or a ``hostname`` with its ``port``) is restored
only when that origin (scheme, host and port; for a hostname the host and port) is the stored one. A mask sent back for another origin is REFUSED with a 422 (``re-enter the key: the stored one is kept
only for the same origin (scheme, host and port)``); a real new secret is the person's change and is stored as sent; a different path, query, host case or the default port spelled out is the same origin.
The refusal names no secret. Every family that keeps a secret beside an origin is in ``FAMILIES``: the LLM, embedding and speech providers, the S3 artifact store, the Kubernetes service-account
connection, a harness and its dependencies, an OIDC provider, an HTTP MCP toolset (its headers) and the Postgres-shaped configs (the vector store and the storage provider).

WHICH ENTRIES ARE WRITTEN THROUGH ``preserve_masked_secrets`` (the REST ``PUT`` and the system ``update_*`` tools): the LLM, embedding and speech providers, the S3 artifact store, the Kubernetes
service-account connection, the HTTP MCP toolset and the vector stores. FOUR ENTRIES ARE HELPER-LEVEL ONLY, and pin the rule of the helper, not of a route: ``OIDC provider`` (its route is
``_preserve_client_secret_if_blank``, pinned in ``tests/api/test_oidc_client_secret_origin.py``), ``harness git token`` (the harness route uses ``apply_git_token_update``, which compares ``git_url``
exactly and is stricter), ``harness dependency git token`` (a ``DependencyRef`` comes only from ``harness.yaml``) and ``storage provider postgres`` (the storage provider comes from the config file).
An earlier version of this table presented all four as covered routes; the OIDC route was not bound at all (review of #711, round 1).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from primer.model.common import preserve_masked_secrets
from primer.model.except_ import ValidationError as PrimerValidationError
from primer.model.harness import DependencyRef, Harness
from primer.model.oidc import OidcProvider
from primer.model.providers.artifact import S3ArtifactConfig
from primer.model.providers.embedding import OpenAIConfig as EmbeddingOpenAIConfig
from primer.model.providers.llm import OllamaConfig, OpenChatConfig, OpenResponsesConfig
from primer.model.providers.speech import SpeechToTextConfig, TextToSpeechConfig
from primer.model.providers.storage import PostgresConfig
from primer.model.providers.toolset import HttpConfig
from primer.model.providers.vector import PgVectorConfig, PgVectorScaleConfig
from primer.model.workspace import K8sConnectionServiceAccountToken

STORED = "sk-live-abcdef0123456789"
TAIL = "**********" + STORED[-4:]          # what an ApiKeySecret serves
BARE = "**********"                         # what every other secret serves
NEW = "sk-live-a-different-key-0000"
HOME = "https://home.example"
AWAY = "https://attacker.example"


def _secret(value: str) -> SecretStr:
    return SecretStr(value)


# name -> (build(origin, secret, path) -> model, how to read the model's secrets, the mask a GET serves for them, the kind of origin: "url" or "host")
def _api(cls):
    def build(origin: str, secret: str, path: str = "/v1"):
        return cls(url=f"{origin}{path}", api_key=_secret(secret))

    return build


def _s3(origin: str, secret: str, path: str = ""):
    second = BARE if secret.startswith(BARE) else secret + "-2"
    return S3ArtifactConfig(bucket="b", endpoint_url=f"{origin}{path}", access_key=_secret(secret), secret_key=_secret(second))


def _k8s(origin: str, secret: str, path: str = ""):
    return K8sConnectionServiceAccountToken(apiserver_url=f"{origin}{path}", ca_data="ca", token=_secret(secret))


def _harness(origin: str, secret: str, path: str = "/org/repo.git"):
    return Harness(id="harness-h", slug="hh", name="h", created_at=datetime.now(UTC), git_url=f"{origin}{path}", git_token=_secret(secret))


def _dependency(origin: str, secret: str, path: str = "/org/dep.git"):
    return DependencyRef(name="d", git_url=f"{origin}{path}", git_token=_secret(secret))


def _oidc(origin: str, secret: str, path: str = "/.well-known/openid-configuration"):
    return OidcProvider(name="idp", discovery_url=f"{origin}{path}", client_id="c", client_secret=_secret(secret))


def _mcp(origin: str, secret: str, path: str = "/mcp"):
    return HttpConfig(url=f"{origin}{path}", headers={"Authorization": _secret(secret)})


def _pg(cls):
    def build(origin: str, secret: str, path: str = ""):
        host = origin.removeprefix("https://")
        return cls(hostname=host, port=5432, username="u", password=_secret(secret), database="d")

    return build


_ONE = [lambda m: m.api_key]
FAMILIES = {
    "LLM openresponses": (_api(OpenResponsesConfig), _ONE, TAIL, "url"),
    "LLM openchat": (_api(OpenChatConfig), _ONE, TAIL, "url"),
    "LLM ollama": (_api(OllamaConfig), _ONE, TAIL, "url"),
    "embedding openai": (_api(EmbeddingOpenAIConfig), _ONE, TAIL, "url"),
    "speech to text": (_api(SpeechToTextConfig), _ONE, TAIL, "url"),
    "text to speech": (_api(TextToSpeechConfig), _ONE, TAIL, "url"),
    "S3 artifact store": (_s3, [lambda m: m.access_key, lambda m: m.secret_key], BARE, "url"),
    "Kubernetes service account": (_k8s, [lambda m: m.token], BARE, "url"),
    # HELPER-LEVEL ONLY: no route writes these through preserve_masked_secrets (see the module docstring for what does).
    "harness git token": (_harness, [lambda m: m.git_token], BARE, "url"),
    "harness dependency git token": (_dependency, [lambda m: m.git_token], BARE, "url"),
    "OIDC provider": (_oidc, [lambda m: m.client_secret], BARE, "url"),
    "MCP http headers": (_mcp, [lambda m: m.headers["Authorization"]], BARE, "url"),
    "vector store pgvector": (_pg(PgVectorConfig), [lambda m: m.password], BARE, "host"),
    "vector store pgvectorscale": (_pg(PgVectorScaleConfig), [lambda m: m.password], BARE, "host"),
    "storage provider postgres": (_pg(PostgresConfig), [lambda m: m.password], BARE, "host"),          # HELPER-LEVEL ONLY: the storage provider comes from the config file
}
IDS = sorted(FAMILIES)


def _stored(name: str):
    return FAMILIES[name][0](HOME, STORED)


def _sent_back_masked(name: str, origin: str, path: str | None = None):
    """What a client sends after a GET: the same row with every secret as the mask the GET served."""
    build, _, mask, _ = FAMILIES[name]
    return build(origin, mask) if path is None else build(origin, mask, path)


def _secrets_of(model, name: str) -> list[str]:
    return [accessor(model).get_secret_value() for accessor in FAMILIES[name][1]]


@pytest.mark.parametrize("name", IDS)
def test_the_same_origin_keeps_the_stored_secret(name: str) -> None:
    stored = _stored(name)
    sent = _sent_back_masked(name, HOME)

    preserve_masked_secrets(sent, stored)

    assert _secrets_of(sent, name) == _secrets_of(stored, name)


@pytest.mark.parametrize("name", [n for n in IDS if FAMILIES[n][3] == "url"])
def test_a_new_path_on_the_same_origin_keeps_the_stored_secret(name: str) -> None:
    stored = _stored(name)
    sent = _sent_back_masked(name, HOME, "/v2/other?x=1")

    preserve_masked_secrets(sent, stored)

    assert _secrets_of(sent, name) == _secrets_of(stored, name)


@pytest.mark.parametrize("name", IDS)
def test_a_moved_origin_with_the_mask_is_refused_and_the_secret_is_not_given(name: str) -> None:
    stored = _stored(name)
    sent = _sent_back_masked(name, AWAY)

    with pytest.raises(PrimerValidationError, match="re-enter the key") as caught:
        preserve_masked_secrets(sent, stored)

    assert STORED not in str(caught.value) and "abcdef0123456789" not in str(caught.value), "the refusal names no secret"
    assert set(_secrets_of(sent, name)) == {FAMILIES[name][2]}, "nothing of the stored secret was put into the update"


@pytest.mark.parametrize("name", IDS)
def test_a_moved_origin_with_a_new_secret_stores_it(name: str) -> None:
    stored = _stored(name)
    sent = FAMILIES[name][0](AWAY, NEW)

    preserve_masked_secrets(sent, stored)

    assert all(value.startswith(NEW) for value in _secrets_of(sent, name)), "a secret the person typed is theirs, wherever the URL points"


@pytest.mark.parametrize("name", [n for n in IDS if FAMILIES[n][3] == "url"])
@pytest.mark.parametrize("moved", ["http://home.example", "https://home.example:8443", "https://elsewhere.example"])
def test_another_scheme_port_or_host_is_another_origin(name: str, moved: str) -> None:
    stored = _stored(name)
    try:
        sent = _sent_back_masked(name, moved)
    except ValueError:
        pytest.skip("this family only accepts an https origin (its own validator refuses the other scheme before the rule is reached)")

    with pytest.raises(PrimerValidationError, match="re-enter the key"):
        preserve_masked_secrets(sent, stored)


@pytest.mark.parametrize("name", [n for n in IDS if FAMILIES[n][3] == "url"])
def test_the_default_port_spelled_out_and_the_host_case_are_the_same_origin(name: str) -> None:
    stored = _stored(name)
    sent = _sent_back_masked(name, "https://HOME.example:443")

    preserve_masked_secrets(sent, stored)

    assert _secrets_of(sent, name) == _secrets_of(stored, name)


@pytest.mark.parametrize("name", [n for n in IDS if FAMILIES[n][3] == "host"])
def test_another_port_is_another_origin_for_a_hostname(name: str) -> None:
    stored = _stored(name)
    sent = _sent_back_masked(name, HOME)
    sent.port = 6543

    with pytest.raises(PrimerValidationError, match="re-enter the key"):
        preserve_masked_secrets(sent, stored)


def test_a_secret_beside_no_origin_is_restored_whatever_else_changes() -> None:
    """A model with no url or host (a Hugging Face token, a web-search key) has no origin to bind to: the rule does not apply."""
    from primer.model.providers.cross_encoder import HuggingFaceCrossEncoderConfig

    stored = HuggingFaceCrossEncoderConfig(token=_secret(STORED))
    sent = HuggingFaceCrossEncoderConfig(token=_secret(BARE))

    preserve_masked_secrets(sent, stored)

    assert sent.token.get_secret_value() == STORED


def test_an_unparseable_stored_origin_is_not_an_excuse_to_restore() -> None:
    """The origin of a value that is not a URL (a typo) is the value itself: unchanged keeps the secret, changed refuses."""
    stored = S3ArtifactConfig(bucket="b", endpoint_url="not a url", access_key=_secret(STORED), secret_key=_secret(STORED))
    kept = S3ArtifactConfig(bucket="b", endpoint_url="not a url", access_key=_secret(BARE), secret_key=_secret(BARE))
    moved = S3ArtifactConfig(bucket="b", endpoint_url="https://attacker.example", access_key=_secret(BARE), secret_key=_secret(BARE))

    preserve_masked_secrets(kept, stored)
    assert kept.access_key.get_secret_value() == STORED

    with pytest.raises(PrimerValidationError, match="re-enter the key"):
        preserve_masked_secrets(moved, stored)


def test_an_endpoint_that_appears_where_there_was_none_is_a_moved_origin() -> None:
    """An S3 store with no ``endpoint_url`` talks to AWS; pointing it at a server of one's own with the mask kept is the same exfiltration."""
    stored = S3ArtifactConfig(bucket="b", access_key=_secret(STORED), secret_key=_secret(STORED))
    sent = S3ArtifactConfig(bucket="b", endpoint_url="https://attacker.example", access_key=_secret(BARE), secret_key=_secret(BARE))

    with pytest.raises(PrimerValidationError, match="re-enter the key"):
        preserve_masked_secrets(sent, stored)
