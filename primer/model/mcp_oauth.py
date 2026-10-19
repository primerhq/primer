"""The stored shapes of an MCP toolset's shared OAuth sign-in (plan 07, Task 5; design 12.5 and 12.6).

A toolset has ONE sign-in, shared by every agent that uses it, and three kinds of row hold it:

* :class:`McpOAuthTokenSet`: the tokens (id = the toolset id), their status and the cross-process refresh lease;
* :class:`McpOAuthClient`: the client a dynamic registration made at the authorization server (id = the toolset id);
* :class:`McpOAuthPendingSignIn`: one started browser sign-in, waiting for the provider's redirect (stored under a hash of its ``state`` value).

Every secret in them is CIPHERTEXT: the ``*_enc`` fields hold what :class:`primer.common.secret_box.SecretBox` sealed, held in a ``SecretStr`` so that a
serialisation, a log line or a ``repr`` shows a mask. Plaintext exists only in memory, between the store's ``unseal`` and the request it goes into.

None of the three is a CDC kind (no event is written for a token change) and no route serves them. The status the API shows is built from them by the
service, and carries no token, no client secret and no verifier.

:class:`McpSignInRequiredError` is what a call raises when the toolset has no usable sign-in. It lives here, not in ``primer.model.except_``, whose file is
stored with CRLF line endings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, SecretStr

from primer.model.common import Identifiable
from primer.model.except_ import PrimerError

__all__ = [
    "ClientSource",
    "McpOAuthClient",
    "McpOAuthPendingSignIn",
    "McpOAuthTokenSet",
    "McpSignInRequiredError",
    "OAuthGrant",
    "TokenState",
]

OAuthGrant = Literal["authorization_code", "client_credentials"]
TokenState = Literal["connected", "needs_sign_in", "error"]
ClientSource = Literal["static", "registered"]


class McpOAuthTokenSet(Identifiable):
    """A toolset's tokens and the facts needed to use and refresh them. The id is the toolset id."""

    grant: OAuthGrant = Field(..., description="How the toolset signed in.")
    access_token_enc: SecretStr | None = Field(
        ...,
        description="The access token, sealed. None when the toolset is signed out (the key is still present: a signed-out row says why in `reason`).",
    )
    refresh_token_enc: SecretStr | None = Field(
        default=None,
        description="The refresh token, sealed. None when the server issued none (always so for client credentials) or the toolset is signed out.",
    )
    token_type: str = Field(default="Bearer", description="token_type of the token response.")
    expires_at: datetime | None = Field(
        default=None,
        description="When the access token expires. None means no known expiry: the token is used until the server answers 401.",
    )
    scope: str | None = Field(default=None, description="The scope the server granted, as it answered.")
    issuer: str | None = Field(default=None, description="The authorization server's issuer, when it was discovered.")
    token_endpoint: str = Field(..., description="Where refresh and client-credentials requests go.")
    revocation_endpoint: str | None = Field(default=None, description="RFC 7009 endpoint, when the server advertises one.")
    resource: str | None = Field(default=None, description="The RFC 8707 resource indicator the tokens were requested for, if any.")
    client_id: str = Field(..., description="The OAuth client the tokens belong to.")
    client_source: ClientSource = Field(..., description="'static': configured on the toolset. 'registered': made by dynamic registration.")
    config_fingerprint: str = Field(
        ...,
        description=(
            "Hash of the URL and OAuth settings the tokens were obtained for. A toolset whose settings no longer hash to this has no usable "
            "sign-in: the old token is never sent to a new URL."
        ),
    )
    state: TokenState = Field(default="connected", description="'connected', 'needs_sign_in' (an operator must connect again) or 'error' (the last refresh failed; tokens are kept).")
    reason: str | None = Field(default=None, description="Why the state is not 'connected', in words an operator can act on. Never holds a token or a response body.")
    version: int = Field(default=1, description="Compare-and-set generation: every replace and every token refresh bumps it, and a writer holds the version it read.")
    refresh_lease_until: datetime | None = Field(default=None, description="The refresh lease: until when one process may exchange the refresh token. None when free.")
    refresh_lease_owner: str | None = Field(default=None, description="The process holding the refresh lease.")
    connected_at: datetime = Field(..., description="When the toolset was connected.")
    connected_by: str | None = Field(default=None, description="The admin who connected it. Every agent that uses the toolset acts as this sign-in.")
    refreshed_at: datetime | None = Field(default=None, description="When the access token was last refreshed.")


class McpOAuthClient(Identifiable):
    """The client a dynamic registration made for a toolset. The id is the toolset id."""

    issuer: str = Field(..., description="The authorization server it is registered at.")
    redirect_uri: str = Field(..., description="The redirect URI it was registered with. A different one needs a new registration.")
    client_id: str = Field(..., description="The registered client id.")
    client_secret_enc: SecretStr | None = Field(default=None, description="The client secret the server issued, sealed. None for a public client.")
    registered_at: datetime = Field(..., description="When it was registered.")


class McpOAuthPendingSignIn(Identifiable):
    """A browser sign-in that was started and not yet finished, for one ``state`` value: 256 random bits, single use, valid for ten minutes.

    The row is built with the state as its id and STORED under the SHA-256 of it (:func:`primer.toolset.oauth.store.pending_key`, applied by the store),
    so the state, a capability while the sign-in is pending, is never a row id that storage code could print.
    """

    toolset_id: str = Field(..., description="The toolset the sign-in is for.")
    code_verifier_enc: SecretStr = Field(..., description="The PKCE code verifier, sealed.")
    redirect_uri: str = Field(..., description="The redirect URI the authorize request carried; the code exchange sends the same one.")
    issuer: str = Field(..., description="The authorization server the sign-in goes through.")
    token_endpoint: str = Field(..., description="Where the code is exchanged.")
    revocation_endpoint: str | None = Field(default=None, description="RFC 7009 endpoint, when the server advertises one.")
    resource: str | None = Field(default=None, description="The RFC 8707 resource indicator sent with the authorize request, if any.")
    client_id: str = Field(..., description="The OAuth client the sign-in uses.")
    client_source: ClientSource = Field(..., description="'static': configured on the toolset. 'registered': made by dynamic registration.")
    config_fingerprint: str = Field(..., description="Hash of the URL and OAuth settings when the sign-in started. A toolset edited meanwhile does not take the tokens.")
    consumed: bool = Field(default=False, description="Set by the one caller that takes the row, so no second caller can.")
    started_by: str | None = Field(default=None, description="The admin who started it.")
    created_at: datetime = Field(..., description="When it started.")
    expires_at: datetime = Field(..., description="When it stops being valid.")


class McpSignInRequiredError(PrimerError):
    """The MCP toolset has no usable sign-in: it was never connected, its settings changed, the provider refused its refresh token, or the key that sealed its tokens changed.

    The message is the text a tool answers with, exactly: ``the MCP server <id> needs an operator to reconnect it``. ``reason`` is the longer account (what
    the toolset's status shows); it is not part of the message. The error carries no ``code`` and no ``status_code``, so ``str()`` is the message; the API maps
    the class itself to its own problem type.
    """

    def __init__(self, toolset_id: str, *, reason: str | None = None) -> None:
        super().__init__(f"the MCP server {toolset_id} needs an operator to reconnect it")
        self.toolset_id = toolset_id
        self.reason = reason

    @property
    def problem_extensions(self) -> dict[str, object]:
        """Structured fields the RFC 7807 envelope carries, so a client can tell which toolset and why."""
        extensions: dict[str, object] = {"toolset_id": self.toolset_id}
        if self.reason is not None:
            extensions["reason"] = self.reason
        return extensions
