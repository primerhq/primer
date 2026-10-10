"""Tests for shared model helpers in primer.model.common."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, SecretStr

from primer.model.common import dump_for_storage, preserve_masked_secrets
from primer.model.except_ import ValidationError


class _Inner(BaseModel):
    secret: SecretStr


class _Outer(BaseModel):
    api_key: SecretStr
    public_id: str
    nested: _Inner | None = None
    inners: list[_Inner] = []
    env: dict[str, SecretStr] = {}


def test_dump_for_storage_unmasks_top_level_secret() -> None:
    m = _Outer(api_key=SecretStr("k1"), public_id="p", env={})
    dumped = dump_for_storage(m)
    assert dumped["api_key"] == "k1"
    assert dumped["public_id"] == "p"


def test_dump_for_storage_unmasks_nested_model() -> None:
    m = _Outer(
        api_key=SecretStr("k1"), public_id="p",
        nested=_Inner(secret=SecretStr("inner-secret")),
    )
    dumped = dump_for_storage(m)
    assert dumped["nested"]["secret"] == "inner-secret"


def test_dump_for_storage_unmasks_list_of_models() -> None:
    m = _Outer(
        api_key=SecretStr("k1"), public_id="p",
        inners=[_Inner(secret=SecretStr("a")), _Inner(secret=SecretStr("b"))],
    )
    dumped = dump_for_storage(m)
    assert dumped["inners"][0]["secret"] == "a"
    assert dumped["inners"][1]["secret"] == "b"


def test_dump_for_storage_unmasks_dict_of_secrets() -> None:
    m = _Outer(
        api_key=SecretStr("k1"), public_id="p",
        env={"OPENAI_API_KEY": SecretStr("sk-xyz"), "TOKEN": SecretStr("tok")},
    )
    dumped = dump_for_storage(m)
    assert dumped["env"]["OPENAI_API_KEY"] == "sk-xyz"
    assert dumped["env"]["TOKEN"] == "tok"


def test_default_dump_still_redacts() -> None:
    """Confirms the safe default is unchanged for API responses."""
    m = _Outer(api_key=SecretStr("k1"), public_id="p")
    dumped = m.model_dump(mode="json")
    assert dumped["api_key"] == "**********"
    assert dumped["public_id"] == "p"


def test_round_trip_through_storage_helper() -> None:
    """The whole point: storage -> json -> validate -> .get_secret_value()
    must yield the original plaintext."""
    m = _Outer(api_key=SecretStr("original"), public_id="p")
    payload = json.dumps(dump_for_storage(m))
    revived = _Outer.model_validate(json.loads(payload))
    assert revived.api_key.get_secret_value() == "original"


# ===========================================================================
# preserve_masked_secrets (01a05198: mask-sentinel preservation)
# ===========================================================================


def test_preserve_masked_secrets_restores_plain_mask() -> None:
    """A short (<=4 char) secret's served mask is always tail-less
    "**********" - an incoming echo of that exact string is restored."""
    existing = _Outer(api_key=SecretStr("abcd"), public_id="p")
    incoming = _Outer(api_key=SecretStr("**********"), public_id="p")
    preserve_masked_secrets(incoming, existing)
    assert incoming.api_key.get_secret_value() == "abcd"


def test_preserve_masked_secrets_restores_plain_mask_for_a_long_secret() -> None:
    """A >4 char secret on a field with NO tail-reveal serializer (e.g.
    artifact.py's access_key/secret_key, plain SecretStr) is served as
    bare "**********" too - not every long secret gets a tail. The
    mask-recognition must try the plain shape independently of length,
    not assume "long secret -> must have been tail-form"."""
    existing = _Outer(api_key=SecretStr("AKIA-a-very-long-real-key"), public_id="p")
    incoming = _Outer(api_key=SecretStr("**********"), public_id="p")
    preserve_masked_secrets(incoming, existing)
    assert incoming.api_key.get_secret_value() == "AKIA-a-very-long-real-key"


def test_preserve_masked_secrets_restores_tail_mask() -> None:
    """A >4 char secret's served mask reveals the last 4 chars
    (ApiKeySecret shape) - an incoming echo of THAT shape is restored,
    not just the plain 10-asterisk string."""
    existing = _Outer(api_key=SecretStr("sk-1234567890"), public_id="p")
    incoming = _Outer(api_key=SecretStr("**********7890"), public_id="p")
    preserve_masked_secrets(incoming, existing)
    assert incoming.api_key.get_secret_value() == "sk-1234567890"


def test_preserve_masked_secrets_keeps_a_real_new_value() -> None:
    """An operator who actually retypes a new secret must have it win -
    it doesn't match either mask shape, so nothing is substituted."""
    existing = _Outer(api_key=SecretStr("sk-old-1234"), public_id="p")
    incoming = _Outer(api_key=SecretStr("sk-new-5678"), public_id="p")
    preserve_masked_secrets(incoming, existing)
    assert incoming.api_key.get_secret_value() == "sk-new-5678"


def test_preserve_masked_secrets_never_held_before_is_refused() -> None:
    """A field with no prior stored secret has nothing to restore against. It used to store the incoming mask-shaped string as a literal
    secret (documented as accepted); a served mask is not a secret anybody typed, so it is REFUSED now (#711 review N1: the mask of a row
    the client read, sent for a field that row did not hold, is a copy-a-row mistake, not a key)."""
    existing = _Outer(api_key=SecretStr("abcd"), public_id="p", nested=None)
    incoming = _Outer(
        api_key=SecretStr("abcd"), public_id="p",
        nested=_Inner(secret=SecretStr("**********")),
    )
    with pytest.raises(ValidationError, match="mask a GET serves"):
        preserve_masked_secrets(incoming, existing)


def test_preserve_masked_secrets_recurses_into_nested_model() -> None:
    existing = _Outer(
        api_key=SecretStr("abcd"), public_id="p",
        nested=_Inner(secret=SecretStr("nested-secret-1234")),
    )
    incoming = _Outer(
        api_key=SecretStr("abcd"), public_id="p",
        nested=_Inner(secret=SecretStr("**********1234")),
    )
    preserve_masked_secrets(incoming, existing)
    assert incoming.nested.secret.get_secret_value() == "nested-secret-1234"


def test_preserve_masked_secrets_recurses_into_list_of_models() -> None:
    existing = _Outer(
        api_key=SecretStr("abcd"), public_id="p",
        inners=[_Inner(secret=SecretStr("alpha-secret"))],
    )
    incoming = _Outer(
        api_key=SecretStr("abcd"), public_id="p",
        inners=[_Inner(secret=SecretStr("**********cret"))],
    )
    preserve_masked_secrets(incoming, existing)
    assert incoming.inners[0].secret.get_secret_value() == "alpha-secret"


def test_preserve_masked_secrets_recurses_into_dict_of_secrets() -> None:
    """Toolset env/headers shape: dict[str, SecretStr], matched by key."""
    existing = _Outer(
        api_key=SecretStr("abcd"), public_id="p",
        env={"OPENAI_API_KEY": SecretStr("sk-real-key-9999"), "PLAIN": SecretStr("xy")},
    )
    incoming = _Outer(
        api_key=SecretStr("abcd"), public_id="p",
        env={"OPENAI_API_KEY": SecretStr("**********9999"), "PLAIN": SecretStr("**********")},
    )
    preserve_masked_secrets(incoming, existing)
    assert incoming.env["OPENAI_API_KEY"].get_secret_value() == "sk-real-key-9999"
    assert incoming.env["PLAIN"].get_secret_value() == "xy"


def test_preserve_masked_secrets_dict_new_key_with_a_mask_is_refused() -> None:
    """A brand-new dict key (an operator adding a new env var) never existed on the old side - an incoming mask-shaped value for it has
    nothing to restore, and is refused with the key named, same as the never-held-before top-level case. A new key with a real value is
    the operator's own and is kept."""
    existing = _Outer(api_key=SecretStr("abcd"), public_id="p", env={})
    masked = _Outer(api_key=SecretStr("abcd"), public_id="p", env={"NEW_VAR": SecretStr("**********")})
    with pytest.raises(ValidationError, match=r"env\.NEW_VAR"):
        preserve_masked_secrets(masked, existing)

    real = _Outer(api_key=SecretStr("abcd"), public_id="p", env={"NEW_VAR": SecretStr("a-real-value")})
    preserve_masked_secrets(real, existing)
    assert real.env["NEW_VAR"].get_secret_value() == "a-real-value"


def test_preserve_masked_secrets_optional_blank_still_nulls() -> None:
    """Deliberately clearing an optional secret (submitting an empty
    string, the UI's "blank means erase" contract) must NOT be treated
    as a mask echo - an empty string matches neither mask shape."""
    existing = _Outer(api_key=SecretStr("abcd"), public_id="p")
    incoming = _Outer(api_key=SecretStr(""), public_id="p")
    preserve_masked_secrets(incoming, existing)
    assert incoming.api_key.get_secret_value() == ""


def test_preserve_masked_secrets_mismatched_types_restore_nothing() -> None:
    """Mismatched entity classes (the provider type changed under the same id) have nothing to restore from. An incoming row with real values is
    left exactly as it is and nothing is partially mutated; one that carries a served mask is refused, because the literal mask would otherwise
    be stored as the secret (#711 review N1)."""
    existing = _Inner(secret=SecretStr("s"))
    real = _Outer(api_key=SecretStr("a-real-key"), public_id="p")
    preserve_masked_secrets(real, existing)  # must not raise
    assert real.api_key.get_secret_value() == "a-real-key"

    masked = _Outer(api_key=SecretStr("**********"), public_id="p")
    with pytest.raises(ValidationError, match="mask a GET serves"):
        preserve_masked_secrets(masked, existing)
