"""Unit tests for the secret-field classifier (``primer/storage/secret_fields.py``).

The rule (ticket 01a1212a, extended by the #721 review): a TOP-LEVEL field holds a secret when its type tree carries a masked type ANYWHERE
(lists, dicts, nested models, unions, Optional), not only when the field itself is ``SecretStr`` / a masked URL. A masked type is a pydantic
secret (``SecretStr`` / ``SecretBytes`` / ``Secret[...]``) or a value whose serializer reads the dump context (the masked URLs; a
``@field_serializer``; a self-serializing model). A serializer that cannot see the context serves what it stores, so it does not count.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

import pytest
from pydantic import (
    BaseModel,
    Field,
    PlainSerializer,
    Secret,
    SecretBytes,
    SecretStr,
    SerializationInfo,
    field_serializer,
)

from primer.model.common import Identifiable
from primer.model.harness import Harness
from primer.model.providers._shared import MaskedUserinfoUrl
from primer.model.providers.llm import LLMProvider
from primer.model.providers.toolset import Toolset
from primer.model.trigger import Trigger
from primer.model.workspace import Workspace, WorkspaceTemplate
from primer.storage.secret_fields import holds_secret


def _ctx_serializer(value, info: SerializationInfo):  # reads the context => masks
    return value


def _blind_serializer(value):  # no info => serves what it stores
    return value


_CtxUrl = Annotated[str, PlainSerializer(_ctx_serializer, when_used="always")]
_BlindBytes = Annotated[bytes, PlainSerializer(_blind_serializer, return_type=str, when_used="json")]


class _Leaf(BaseModel):
    kind: Literal["url"] = "url"
    url: MaskedUserinfoUrl


class _Plain(BaseModel):
    kind: Literal["plain"] = "plain"
    text: str


class _Mount(BaseModel):
    path: str
    source: Annotated[Union[_Leaf, _Plain], Field(discriminator="kind")]


class _SelfSerializing(BaseModel):
    x: str

    @field_serializer("x")
    def _ser_x(self, v, info):
        return v


class _Sample(Identifiable):
    plain_text: str = ""
    plain_map: dict[str, list[int]] = {}
    a_secret: SecretStr | None = None
    secret_bytes: SecretBytes | None = None
    generic_secret: Secret[int] | None = None
    list_of_secrets: list[SecretStr] = []
    dict_of_secrets: dict[str, SecretStr] = {}
    nested_masked_url: list[_Mount] = []
    ctx_url: _CtxUrl = ""
    blind_bytes: _BlindBytes = b""
    self_serializing: _SelfSerializing | None = None


@pytest.mark.parametrize(
    "field",
    [
        "a_secret",
        "secret_bytes",
        "generic_secret",
        "list_of_secrets",
        "dict_of_secrets",
        "nested_masked_url",
        "ctx_url",
        "self_serializing",
    ],
)
def test_secret_bearing_fields_are_flagged(field: str) -> None:
    assert holds_secret(_Sample, field) is True


@pytest.mark.parametrize("field", ["id", "plain_text", "plain_map", "blind_bytes"])
def test_plain_fields_are_not_flagged(field: str) -> None:
    assert holds_secret(_Sample, field) is False


def test_dotted_path_into_a_masked_leaf_is_flagged() -> None:
    assert holds_secret(_Sample, "nested_masked_url.source") is True


def test_dotted_path_through_a_masked_node_is_flagged_even_to_a_plain_leaf() -> None:
    # Reaching INTO a node that holds a secret is refused whether or not the
    # named sub-path is itself plain: the backend reads something inside it.
    assert holds_secret(_Sample, "dict_of_secrets.MY_KEY") is True


def test_plain_dotted_path_is_not_flagged() -> None:
    assert holds_secret(_Sample, "plain_map.whatever") is False


def test_unknown_top_level_field_is_left_to_the_renderer() -> None:
    # Not a declared field: the classifier says nothing (the SQL renderer
    # refuses it as undeclared), so this must be False, not an error.
    assert holds_secret(_Sample, "nonexistent") is False


# ---- the real models the ticket and the #721 review name --------------------


@pytest.mark.parametrize(
    "model, field",
    [
        (LLMProvider, "config"),
        (Harness, "git_token"),
        # A MaskedGitUrl since #722, and the list of resolved dependencies
        # whose git_url is one too.
        (Harness, "git_url"),
        (Harness, "dependencies_resolved"),
        (Toolset, "config"),
        (Trigger, "config"),
        (WorkspaceTemplate, "env"),
        # A kind=url file source carries a MaskedUserinfoUrl since #721, so the list of mounts holds a secret.
        (WorkspaceTemplate, "files"),
        (Workspace, "overrides"),
        (Workspace, "runtime_meta"),
    ],
)
def test_real_secret_bearing_fields(model: type, field: str) -> None:
    assert holds_secret(model, field) is True


@pytest.mark.parametrize(
    "model, field",
    [
        (LLMProvider, "provider"),
        (LLMProvider, "config.flavor"),
        (Harness, "slug"),
        (WorkspaceTemplate, "provider_id"),
        (Workspace, "template_id"),
    ],
)
def test_real_plain_fields(model: type, field: str) -> None:
    assert holds_secret(model, field) is False


def test_a_nested_masked_url_anywhere_in_a_list_flags_the_top_field() -> None:
    # The #721 shape: list[FileMount] -> source -> a MaskedUserinfoUrl leaf.
    # The whole list is compared as JSON text, so the top field holds a secret
    # even though it is not itself a SecretStr. WorkspaceTemplate.files is the real
    # case since #721 (its kind=url source carries a MaskedUserinfoUrl; see
    # test_real_secret_bearing_fields). ``nested_masked_url`` on _Sample pins the
    # rule on a synthetic model of the same shape.
    assert holds_secret(_Sample, "nested_masked_url") is True
    assert holds_secret(_Sample, "nested_masked_url.source.url") is True
