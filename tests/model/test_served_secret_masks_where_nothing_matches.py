"""A served SECRET mask is refused wherever there is nothing stored of its shape to restore it from (ticket 01a1212a, round 1 of #711, nit N1), and the refusals say what they mean.

``preserve_masked_secrets`` restores a mask from the stored value of the SAME field. Three branches have no such value: the config class changed (the stored config is another class),
a dict key is new, and a list changed length. They refused a URL password mask and stored a served SECRET mask as the literal value (an ``api_key`` of ``**********cdef`` next to a host of
the caller's choosing, a header of ``**********``). It leaked nothing, since the tail is already served, but it stored a credential that was never a credential and contradicted the create
refusal. They refuse a served secret mask now, as ``refuse_served_masks`` does; a secret the person typed in the same place is theirs and is stored.

Also pinned here: the safe-direction cases the security review listed (a default ``ws`` port is the same origin; a Postgres-shaped config's host compares case-insensitively), and the wording
of the two refusals (they say scheme, host and port, and the user, not just "the host": a changed port or scheme is refused too, and a URL carries a password, not a key).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, SecretStr

from primer.model.common import preserve_masked_secrets
from primer.model.except_ import ValidationError as PrimerValidationError
from primer.model.providers.llm import OpenChatConfig, OpenResponsesConfig
from primer.model.providers.toolset import HttpConfig
from primer.model.providers.vector import PgVectorConfig

STORED = "sk-live-abcdef0123456789"
TAIL = "**********" + STORED[-4:]
BARE = "**********"
NEW = "sk-live-typed-by-the-person"
HOME = "https://home.example/v1"


class _Holder(BaseModel):
    secrets: list[SecretStr]


def test_a_config_class_switch_with_the_served_key_is_refused() -> None:
    stored = OpenChatConfig(url=HOME, api_key=SecretStr(STORED))
    sent = OpenResponsesConfig(url="https://attacker.example/v1", api_key=SecretStr(TAIL))

    with pytest.raises(PrimerValidationError, match="re-enter the secret"):
        preserve_masked_secrets(sent, stored)

    assert sent.api_key.get_secret_value() == TAIL, "nothing of the stored key was put into the update, and the mask was not accepted"


def test_a_config_class_switch_with_a_typed_key_is_stored() -> None:
    stored = OpenChatConfig(url=HOME, api_key=SecretStr(STORED))
    sent = OpenResponsesConfig(url="https://other.example/v1", api_key=SecretStr(NEW))

    preserve_masked_secrets(sent, stored)

    assert sent.api_key.get_secret_value() == NEW


def test_a_new_header_with_a_served_mask_is_refused() -> None:
    stored = HttpConfig(url="https://home.example/mcp", headers={"Authorization": SecretStr("Bearer abcdef")})
    sent = HttpConfig(url="https://home.example/mcp", headers={"Authorization": SecretStr(BARE), "X-Extra": SecretStr(BARE)})

    with pytest.raises(PrimerValidationError, match="re-enter the secret") as caught:
        preserve_masked_secrets(sent, stored)

    assert "X-Extra" in str(caught.value) and "abcdef" not in str(caught.value)


def test_a_new_header_with_a_typed_value_is_stored_and_the_known_one_is_restored() -> None:
    stored = HttpConfig(url="https://home.example/mcp", headers={"Authorization": SecretStr("Bearer abcdef")})
    sent = HttpConfig(url="https://home.example/mcp", headers={"Authorization": SecretStr(BARE), "X-Extra": SecretStr("typed")})

    preserve_masked_secrets(sent, stored)

    assert sent.headers["Authorization"].get_secret_value() == "Bearer abcdef" and sent.headers["X-Extra"].get_secret_value() == "typed"


@pytest.mark.parametrize("served", [BARE, TAIL], ids=["the bare mask", "the mask with the last four characters"])
def test_a_list_that_changed_length_with_a_served_secret_is_refused(served: str) -> None:
    stored = _Holder(secrets=[SecretStr(STORED)])
    sent = _Holder(secrets=[SecretStr(served), SecretStr(NEW)])

    with pytest.raises(PrimerValidationError, match="re-enter the secret"):
        preserve_masked_secrets(sent, stored)


def test_a_list_that_changed_length_with_typed_secrets_is_stored() -> None:
    stored = _Holder(secrets=[SecretStr(STORED)])
    sent = _Holder(secrets=[SecretStr(NEW), SecretStr("another-typed-secret")])

    preserve_masked_secrets(sent, stored)

    assert [s.get_secret_value() for s in sent.secrets] == [NEW, "another-typed-secret"]


# ---- the two safe-direction survivors the security review named ------------------------------------------------------------------------------------------


def test_the_host_of_a_postgres_shaped_config_compares_without_case() -> None:
    stored = PgVectorConfig(hostname="db.home.example", port=5432, username="u", password=SecretStr(STORED), database="d")
    sent = PgVectorConfig(hostname="DB.Home.EXAMPLE", port=5432, username="u", password=SecretStr(BARE), database="d")

    preserve_masked_secrets(sent, stored)

    assert sent.password.get_secret_value() == STORED


def test_a_different_host_of_a_postgres_shaped_config_is_still_another_origin() -> None:
    stored = PgVectorConfig(hostname="db.home.example", port=5432, username="u", password=SecretStr(STORED), database="d")
    sent = PgVectorConfig(hostname="db.home.example.attacker.example", port=5432, username="u", password=SecretStr(BARE), database="d")

    with pytest.raises(PrimerValidationError, match="re-enter the secret"):
        preserve_masked_secrets(sent, stored)


# ---- the wording ----------------------------------------------------------------------------------------------------------------------------------------


def test_the_key_refusal_says_the_whole_origin_not_only_the_host() -> None:
    stored = OpenChatConfig(url=HOME, api_key=SecretStr(STORED))
    sent = OpenChatConfig(url="https://home.example:8443/v1", api_key=SecretStr(TAIL))

    with pytest.raises(PrimerValidationError) as caught:
        preserve_masked_secrets(sent, stored)

    text = str(caught.value)
    assert "scheme, host and port" in text, "a changed port or scheme is refused too: 'the same host' alone misleads"
    assert STORED not in text


def test_the_password_refusal_says_the_origin_and_the_user() -> None:
    from primer.model.providers.llm import OllamaConfig

    stored = OllamaConfig(url="http://svc:s3cr3t@px.lan/v1")
    sent = OllamaConfig(url="http://svc:**********@px.lan:8443/v1")

    with pytest.raises(PrimerValidationError) as caught:
        preserve_masked_secrets(sent, stored)

    text = str(caught.value)
    assert "re-enter the password" in text and "scheme, host and port" in text and "user" in text
    assert "s3cr3t" not in text
