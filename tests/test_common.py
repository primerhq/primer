"""Tests for shared model helpers in matrix.model.common."""

from __future__ import annotations

import json

from pydantic import BaseModel, SecretStr

from matrix.model.common import dump_for_storage


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
