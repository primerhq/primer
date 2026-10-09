"""The origin of a model binds the masked secrets of every model nested in it, and ``refuse_served_masks`` refuses a served mask on a create (ticket 01a1212a).

``tests/model/test_masked_secrets_are_bound_to_their_origin.py`` pins the rule for each config family with the secret beside its origin. This file pins the part those cannot see:

* the flag "the origin moved" is THREADED down: a secret in a model nested under the one that names the origin (the OAuth client secret under an HTTP MCP toolset's ``url``), in a list of models, in a
  dict of models, in a dict of secrets and in a list of secrets follows the origin of the model it sits in;
* ``refuse_served_masks`` refuses exactly the shapes a GET serves (the bare mask and the mask with the last four characters) in any position, and nothing else.
"""

from __future__ import annotations

import pytest
from pydantic import AnyUrl, BaseModel, HttpUrl, SecretStr

from primer.model.common import preserve_masked_secrets, refuse_served_masks
from primer.model.except_ import ValidationError as PrimerValidationError
from primer.model.providers.artifact import S3ArtifactConfig
from primer.model.providers.toolset import HttpConfig

STORED = "tok-live-abcdef0123456789"
NEW = "tok-live-typed-by-the-person"
BARE = "**********"
HOME = "https://home.example/v1"
AWAY = "https://attacker.example/v1"


class _Leaf(BaseModel):
    token: SecretStr


class _Holder(BaseModel):
    url: str
    child: _Leaf
    leaves: list[_Leaf]
    named: dict[str, _Leaf]
    tokens: dict[str, SecretStr]
    secrets: list[SecretStr]


POSITIONS = ["child", "leaves", "named", "tokens", "secrets"]


def _holder(url: str, **overrides: str) -> _Holder:
    values = {position: overrides.get(position, STORED) for position in POSITIONS}
    return _Holder(
        url=url,
        child=_Leaf(token=values["child"]),
        leaves=[_Leaf(token=values["leaves"])],
        named={"a": _Leaf(token=values["named"])},
        tokens={"t": values["tokens"]},
        secrets=[values["secrets"]],
    )


def _read(holder: _Holder, position: str) -> str:
    return {
        "child": lambda: holder.child.token,
        "leaves": lambda: holder.leaves[0].token,
        "named": lambda: holder.named["a"].token,
        "tokens": lambda: holder.tokens["t"],
        "secrets": lambda: holder.secrets[0],
    }[position]().get_secret_value()


def test_every_position_is_restored_for_the_same_origin() -> None:
    stored = _holder(HOME)
    sent = _holder("https://HOME.example/other", **dict.fromkeys(POSITIONS, BARE))

    preserve_masked_secrets(sent, stored)

    assert [_read(sent, p) for p in POSITIONS] == [STORED] * len(POSITIONS)


@pytest.mark.parametrize("position", POSITIONS)
def test_every_position_is_refused_for_another_origin(position: str) -> None:
    """Only ``position`` carries the mask; the others are secrets the person typed, which must not hide the refusal."""
    stored = _holder(HOME)
    sent = _holder(AWAY, **{p: (BARE if p == position else NEW) for p in POSITIONS})

    with pytest.raises(PrimerValidationError, match="re-enter the key"):
        preserve_masked_secrets(sent, stored)

    assert _read(sent, position) == BARE, "the stored secret was not put into the update"


def test_a_secret_the_person_typed_is_stored_for_another_origin_in_every_position() -> None:
    stored = _holder(HOME)
    sent = _holder(AWAY, **dict.fromkeys(POSITIONS, NEW))

    preserve_masked_secrets(sent, stored)

    assert [_read(sent, p) for p in POSITIONS] == [NEW] * len(POSITIONS)


def test_the_refusal_names_the_dict_key_and_no_secret() -> None:
    stored = _holder(HOME)
    sent = _holder(AWAY, tokens=BARE, **{p: NEW for p in POSITIONS if p != "tokens"})

    with pytest.raises(PrimerValidationError) as caught:
        preserve_masked_secrets(sent, stored)

    assert "tokens.t" in str(caught.value) and STORED not in str(caught.value)


def test_an_endpoint_that_goes_away_is_a_moved_origin() -> None:
    """The twin of ``test_an_endpoint_that_appears_where_there_was_none_is_a_moved_origin``: an S3 store pointed at its own server, sent back with no endpoint (AWS) and the key masks."""
    stored = S3ArtifactConfig(bucket="b", endpoint_url="https://minio.home.example", access_key=STORED, secret_key=STORED)
    sent = S3ArtifactConfig(bucket="b", access_key=BARE, secret_key=BARE)

    with pytest.raises(PrimerValidationError, match="re-enter the key"):
        preserve_masked_secrets(sent, stored)


# ---- the real shape: an HTTP MCP toolset's OAuth client secret ----------------------------------------------------------------------------------------------------------------


def _oauth(url: str, secret: str, resource_uri: str | None = None) -> HttpConfig:
    oauth: dict = {"redirect_uri": "https://home.example/cb", "static_client": {"client_id": "c", "client_secret": secret}}
    if resource_uri is not None:
        oauth["resource_uri"] = resource_uri
    return HttpConfig.model_validate({"url": url, "oauth": oauth})


def test_the_oauth_client_secret_follows_the_url_of_the_toolset_it_sits_under() -> None:
    stored = _oauth("https://home.example/mcp", STORED)

    kept = _oauth("https://home.example/mcp2", BARE)
    preserve_masked_secrets(kept, stored)
    assert kept.oauth.static_client.client_secret.get_secret_value() == STORED

    moved = _oauth("https://attacker.example/mcp", BARE)
    with pytest.raises(PrimerValidationError, match="re-enter the key"):
        preserve_masked_secrets(moved, stored)


def test_moving_only_the_oauth_resource_uri_refuses_the_client_secret_mask() -> None:
    stored = _oauth("https://home.example/mcp", STORED, resource_uri="https://home.example/mcp")
    moved = _oauth("https://home.example/mcp", BARE, resource_uri="https://attacker.example/mcp")

    with pytest.raises(PrimerValidationError, match="re-enter the key"):
        preserve_masked_secrets(moved, stored)


def test_moving_only_the_oauth_redirect_uri_is_not_a_move_of_where_the_secret_is_sent() -> None:
    """The redirect URI is where the browser returns, not where the client secret goes: the toolset admin gate still asks an admin for it (``toolset_admin_reason``), the origin rule does not."""
    stored = _oauth("https://home.example/mcp", STORED)
    sent = _oauth("https://home.example/mcp", BARE)
    sent.oauth.redirect_uri = HttpUrl("https://attacker.example/cb")

    preserve_masked_secrets(sent, stored)

    assert sent.oauth.static_client.client_secret.get_secret_value() == STORED


# ---- refuse_served_masks -----------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("served", [BARE, BARE + "cdef"], ids=["the bare mask", "the mask with the last four characters"])
@pytest.mark.parametrize("position", POSITIONS)
def test_a_create_refuses_a_served_mask_in_every_position(position: str, served: str) -> None:
    body = _holder(HOME, **{position: served})

    with pytest.raises(PrimerValidationError, match="re-enter the key") as caught:
        refuse_served_masks(body)

    assert served not in str(caught.value), "a refusal repeats no secret"


@pytest.mark.parametrize(
    "real",
    [NEW, BARE + "abcdef", "x" + BARE, BARE + BARE, "*" * 9, "", "sk-" + BARE],
    ids=["a typed secret", "the mask and more than four characters", "the mask after a letter", "two masks", "nine asterisks", "empty", "a prefix then the mask"],
)
def test_a_create_accepts_anything_that_is_not_the_shape_a_get_serves(real: str) -> None:
    refuse_served_masks(_holder(HOME, **dict.fromkeys(POSITIONS, real)))


def test_a_create_refuses_a_url_that_carries_the_served_password_mask_anywhere() -> None:
    class _Mirrors(BaseModel):
        mirrors: list[AnyUrl]
        named: dict[str, AnyUrl]

    refuse_served_masks(_Mirrors(mirrors=["http://svc:pw@a.example/"], named={"a": "http://b.example/"}))
    with pytest.raises(PrimerValidationError, match="re-enter the password"):
        refuse_served_masks(_Mirrors(mirrors=["http://a.example/", f"http://svc:{BARE}@a.example/"], named={}))
    with pytest.raises(PrimerValidationError, match="re-enter the password"):
        refuse_served_masks(_Mirrors(mirrors=[], named={"a": f"http://svc:{BARE}@a.example/"}))


def test_a_secret_that_is_not_set_is_not_a_mask() -> None:
    class _Optional(BaseModel):
        token: SecretStr | None = None

    refuse_served_masks(_Optional())
    refuse_served_masks(_Optional(token=NEW))
