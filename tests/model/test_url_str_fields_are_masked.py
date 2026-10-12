"""The URL fields stored as plain strings serve their userinfo password masked (ticket 01a1294c, the 01a11d32 remainder).

The MCP toolset ``config.url`` and the OIDC ``discovery_url`` are plain ``str`` (a URL is compared and keyed as typed, and a git URL may be a ``file://`` under the operator opt-in), so neither is an ``HttpUrl``; the OpenRouter ``app_url`` is an
``HttpUrl | None``. All three used to serve ``user:password@`` in clear next to a masked secret. They serve ``user:**********@`` now (the username stays because a password is present; a lone ``https://TOKEN@host`` is masked whole), a
python-mode dump and the stored row keep the real URL, a create that carries the served mask is refused, and a full-replace PUT puts the stored credential back for the same origin and user only. ``MaskedGitUrl`` (the harness ``git_url``)
is the same ``str`` flavour registered on the same serializer, not a copy in ``primer.model.harness`` (the review of #722, nit N6).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.common import STORAGE_DUMP_CONTEXT, dump_for_storage, preserve_masked_secrets, refuse_served_masks
from primer.model.except_ import ValidationError as PrimerValidationError
from primer.model.harness import Harness, MaskedGitUrl
from primer.model.masked_url import MaskedUserinfoStr
from primer.model.oidc import OidcProvider
from primer.model.providers.llm import OpenRouterConfig
from primer.model.providers.toolset import HttpConfig

MASK = "**********"
PROXY = "http://svc:s3cr3t@proxy.local:8080/mcp"
MASKED = f"http://svc:{MASK}@proxy.local:8080/mcp"
DISCOVERY_PROXY = "https://user:s3cr3t@idp.example.com/.well-known/openid-configuration"
MASKED_DISCOVERY = f"https://user:{MASK}@idp.example.com/.well-known/openid-configuration"
APP_PROXY = "http://svc:s3cr3t@app.example/"
MASKED_APP = f"http://svc:{MASK}@app.example/"


def _oidc(discovery_url: str) -> OidcProvider:
    return OidcProvider(name="IdP", discovery_url=discovery_url, client_id="c")


# ---- served masked ---------------------------------------------------------------------------------------------------------------------------------------------------


def test_an_mcp_toolset_url_is_served_masked_in_a_json_dump() -> None:
    assert HttpConfig(url=PROXY).model_dump(mode="json")["url"] == MASKED


def test_an_mcp_toolset_url_keeps_the_real_value_out_of_a_json_dump() -> None:
    cfg = HttpConfig(url=PROXY)
    assert cfg.model_dump()["url"] == PROXY, "a python-mode dump keeps the real URL (the adapters read it)"
    assert cfg.url == PROXY
    assert dump_for_storage(cfg)["url"] == PROXY, "the stored row keeps the real URL"
    assert cfg.model_dump(mode="json", context={**STORAGE_DUMP_CONTEXT, "other": 1})["url"] == PROXY, "the storage context is read as a subset of its flags"
    assert cfg.model_dump(mode="json", context={"storage": False})["url"] == MASKED, "every other JSON-mode dump masks"


def test_an_oidc_discovery_url_is_served_masked_in_a_json_dump() -> None:
    assert _oidc(DISCOVERY_PROXY).model_dump(mode="json")["discovery_url"] == MASKED_DISCOVERY


def test_an_openrouter_app_url_is_served_masked_in_a_json_dump() -> None:
    assert OpenRouterConfig(api_key="sk-or-live", app_url=APP_PROXY).model_dump(mode="json")["app_url"] == MASKED_APP
    assert OpenRouterConfig(api_key="sk-or-live").model_dump(mode="json")["app_url"] is None, "absent stays absent"


# ---- create refuses the served mask ------------------------------------------------------------------------------------------------------------------------------------


def test_a_create_body_that_carries_the_served_mask_in_a_str_url_field_is_refused() -> None:
    with pytest.raises(PrimerValidationError, match="re-enter the password"):
        refuse_served_masks(HttpConfig(url=MASKED))
    with pytest.raises(PrimerValidationError, match="re-enter the password"):
        refuse_served_masks(_oidc(MASKED_DISCOVERY))
    # the AnyUrl flavour (the OpenRouter app_url) already refuses on create: the str flavour joins it
    with pytest.raises(PrimerValidationError, match="re-enter the password"):
        refuse_served_masks(OpenRouterConfig(api_key="sk-or-live", app_url=MASKED_APP))


# ---- a full-replace PUT of the served body ----------------------------------------------------------------------------------------------------------------------------------


def test_a_mask_sent_back_for_a_str_url_field_is_restored_for_the_same_origin() -> None:
    stored = HttpConfig(url=PROXY)
    incoming = HttpConfig(url=MASKED)
    preserve_masked_secrets(incoming, stored)
    assert incoming.url == PROXY

    incoming = HttpConfig(url=f"http://svc:{MASK}@proxy.local:8080/other")
    preserve_masked_secrets(incoming, stored)
    assert incoming.url == "http://svc:s3cr3t@proxy.local:8080/other", "a changed path keeps the password"

    stored_provider = _oidc(DISCOVERY_PROXY)
    incoming_provider = _oidc(MASKED_DISCOVERY)
    preserve_masked_secrets(incoming_provider, stored_provider)
    assert incoming_provider.discovery_url == DISCOVERY_PROXY


@pytest.mark.parametrize(
    "moved",
    [
        pytest.param(f"http://svc:{MASK}@attacker.example:9000/mcp", id="another host"),
        pytest.param(f"http://other:{MASK}@proxy.local:8080/mcp", id="another username"),
        pytest.param(f"https://svc:{MASK}@proxy.local:8080/mcp", id="another scheme"),
    ],
)
def test_a_mask_sent_back_under_another_origin_is_a_422_and_leaves_the_value(moved: str) -> None:
    """An update that points the URL at another host (or another user) and leaves the mask alone is REFUSED: the stored password is not given to a host the person controls, and the literal mask is not stored as a password either."""
    stored = HttpConfig(url=PROXY)
    incoming = HttpConfig(url=moved)
    with pytest.raises(PrimerValidationError, match="re-enter the password"):
        preserve_masked_secrets(incoming, stored)


def test_a_mask_with_no_stored_credential_is_refused() -> None:
    stored = HttpConfig(url="http://proxy.local:8080/mcp")
    incoming = HttpConfig(url=MASKED)
    with pytest.raises(PrimerValidationError, match="re-enter the password"):
        preserve_masked_secrets(incoming, stored)


def test_a_typed_url_is_stored_as_sent() -> None:
    stored = HttpConfig(url=PROXY)
    incoming = HttpConfig(url="http://svc:newpass@proxy.local:8080/mcp")
    preserve_masked_secrets(incoming, stored)
    assert incoming.url == "http://svc:newpass@proxy.local:8080/mcp", "a real new password is the person's change"


# ---- the #722 nit N6 fold ------------------------------------------------------------------------------------------------------------------------------------------------


def test_the_masked_git_url_is_the_shared_str_flavour() -> None:
    """#722 review nit N6: the harness ``git_url`` mask is the ``str`` flavour registered in ``primer.model.masked_url`` (one rule, one serializer), not a copy in ``primer.model.harness``."""
    assert MaskedGitUrl is MaskedUserinfoStr

    harness = Harness(
        id="hns_n00000000001",
        slug="a-harness",
        name="A",
        git_url="https://reader:s3cr3t@git.example.com/org/repo.git",
        created_at=datetime.now(timezone.utc),
    )
    assert harness.model_dump(mode="json")["git_url"] == f"https://reader:{MASK}@git.example.com/org/repo.git"
