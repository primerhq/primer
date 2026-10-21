"""Bearer authentication for an MCP toolset's shared OAuth sign-in."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx2

from primer.model.mcp_oauth import McpSignInRequiredError
from primer.toolset.oauth.service import OAuthTarget, ToolsetOAuthService


class McpBearerAuth(httpx2.Auth):
    """Attach the shared token and retry one rejected request after refresh.

    The MCP SDK runs requests in its own task group and can swallow an
    exception raised by an auth flow. Refresh failures are therefore recorded
    on this object and the original 401 is returned. The provider inspects the
    record after the transport closes and raises the domain error itself.
    """

    requires_request_body = True

    def __init__(self, service: ToolsetOAuthService, target: OAuthTarget, token: str) -> None:
        self._service = service
        self._target = target
        self._token = token
        self.sign_in_required: str | None = None
        self.failure: Exception | None = None

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if response.status_code != 401:
            return
        try:
            token = await self._service.refresh_after_401(
                self._target,
                rejected=self._token,
                www_authenticate=response.headers.get("WWW-Authenticate"),
            )
        except McpSignInRequiredError as exc:
            self.sign_in_required = str(exc)
            return
        except Exception as exc:  # the provider re-raises after the SDK transport closes
            self.failure = exc
            return
        self._token = token
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            self.sign_in_required = str(McpSignInRequiredError(self._target.toolset_id))


__all__ = ["McpBearerAuth"]
