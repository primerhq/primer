"""Discovery and the OAuth calls of an MCP toolset's shared sign-in: pure HTTP, no storage (plan 07, Task 6; design 12.3, 12.4 and 12.5).

What :mod:`primer.toolset.oauth.service` builds a sign-in from:

* **Discovery** (:func:`discover`). The RFC 9728 protected-resource metadata is looked up at the ``resource_metadata`` URL of the server's
  ``WWW-Authenticate`` challenge, then path-aware, then at the root (:func:`protected_resource_urls`); the first 200 JSON object that holds
  ``authorization_servers`` wins. The authorization server is the first one it names, and its metadata is looked up path-aware through RFC 8414 and
  then OpenID discovery (:func:`auth_server_metadata_urls`). A server that publishes no protected-resource metadata has the 2025-03-26 shape: its
  authorization server is the MCP server's own origin, and no resource indicator is sent.
  A metadata document is used only when every endpoint in it is an http(s) URL with a host and no credentials (an authorization endpoint is opened by
  the console, where a ``javascript:`` URL would run), and when its ``issuer`` is the one it was looked up for (RFC 8414 section 3.3, OpenID
  Discovery 4.3; a trailing ``/`` aside).
* **PKCE S256** (:func:`pkce_pair`, :func:`require_s256`): an authorization server that does not advertise S256 is refused, as MCP requires.
* **The resource indicator** (:func:`canonical_resource`): the MCP server's full URL, not its origin.
* **The calls**: dynamic client registration (RFC 7591), the authorization-code, refresh-token and client-credentials grants, and revocation
  (RFC 7009), with the client authenticated as :func:`client_auth_for` decides.

Errors. A token endpoint that refuses (any 4xx but 408 and 429) raises :class:`TokenEndpointRejected`, a refusing registration endpoint a
:class:`~primer.model.except_.BadRequestError`. A 5xx, a 408, any other status, or a success that holds no token raises
:class:`~primer.model.except_.ServerError`; a 429 :class:`~primer.model.except_.RateLimitError`; a call that got no answer
:class:`~primer.model.except_.NetworkError`. Only :class:`TokenEndpointRejected` means "this grant is dead": a caller keeps its refresh token on every
other failure. Every error text carries the HTTP status and the RFC 6749 ``error`` and ``error_description`` fields only, as printable ASCII, cut to
200 characters, with each secret the request carried masked even when the server echoes it: never the response body. Nothing here logs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from primer.common.log import redact_credentials, redact_url_secrets
from primer.common.mcp_errors import classify_mcp_exception
from primer.model.except_ import BadRequestError, NetworkError, PrimerError, ProviderError, RateLimitError, ServerError
from primer.model.mcp_oauth import ClientAuthMethod

__all__ = [
    "ERROR_TEXT_LIMIT",
    "AuthServerMetadata",
    "ClientAuth",
    "ClientAuthMethod",
    "Discovery",
    "ProtectedResource",
    "RegisteredClient",
    "TokenEndpointRejected",
    "TokenGrant",
    "auth_server_metadata_urls",
    "authorization_url",
    "canonical_resource",
    "client_auth_for",
    "client_credentials_grant",
    "discover",
    "exchange_code",
    "parse_www_authenticate",
    "pkce_pair",
    "probe_challenge",
    "protected_resource_urls",
    "refresh_grant",
    "register_client",
    "require_s256",
    "revoke",
]

#: The longest error text a failed OAuth call produces.
ERROR_TEXT_LIMIT = 200

#: 4xx answers that say "not now" rather than "never": a refresh that meets one keeps its refresh token.
_TEMPORARY_4XX = frozenset({408, 429})
#: The longest server-provided text (an issuer, a URL) quoted inside a discovery error.
_QUOTED_LIMIT = 120
_JSON_ACCEPT = {"Accept": "application/json"}
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "primer", "version": "1"}},
}
_TOKEN_CHARS = frozenset("!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


# ---- URLs ------------------------------------------------------------------------------------------------------------------------------------------------------------------


def _http_url_problem(value: object) -> str | None:
    """Why ``value`` is not an absolute http(s) URL with a host and no credentials, or ``None`` when it is one."""
    if not isinstance(value, str) or not value or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return "must be an http or https URL"
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - a port that is not a number raises ValueError here
    except ValueError:
        return "must be an http or https URL"
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return "must be an http or https URL with a host"
    if parts.username is not None or parts.password is not None:
        return "must not carry credentials"
    return None


def _host_port(url: str) -> str:
    """The ``host[:port]`` of ``url``, lowercased, without any ``user:password@``."""
    return urlsplit(url).netloc.rpartition("@")[2].lower()


def _origin(url: str) -> str:
    return f"{urlsplit(url).scheme.lower()}://{_host_port(url)}"


def _same_issuer(found: str, expected: str) -> bool:
    """RFC 8414 section 3.3 compares issuers as strings; a trailing ``/`` (``https://as.example/`` and ``https://as.example``) is the one difference forgiven."""
    return found == expected or found.rstrip("/") == expected.rstrip("/")


def protected_resource_urls(server_url: str, resource_metadata: str | None) -> list[str]:
    """Where the RFC 9728 protected-resource metadata of ``server_url`` may be, in the order they are tried.

    The ``resource_metadata`` URL of the server's challenge (when it is an http(s) URL), then the path-aware document
    (``{origin}/.well-known/oauth-protected-resource{path}``, when the server URL has a path), then the root document. No URL is listed twice, and none
    carries the server URL's query or credentials.
    """
    urls: list[str] = []
    if resource_metadata and _http_url_problem(resource_metadata) is None:
        urls.append(resource_metadata)
    origin = _origin(server_url)
    path = urlsplit(server_url).path.rstrip("/")
    if path:
        urls.append(f"{origin}/.well-known/oauth-protected-resource{path}")
    urls.append(f"{origin}/.well-known/oauth-protected-resource")
    return list(dict.fromkeys(urls))


def auth_server_metadata_urls(issuer: str) -> list[str]:
    """Where the metadata of the authorization server ``issuer`` may be, in the order they are tried.

    With a path: RFC 8414 path-aware, OpenID path-inserted, OpenID path-appended. Without one: the RFC 8414 root document, then OpenID's.
    """
    origin = _origin(issuer)
    path = urlsplit(issuer).path.rstrip("/")
    if path:
        return [
            f"{origin}/.well-known/oauth-authorization-server{path}",
            f"{origin}/.well-known/openid-configuration{path}",
            f"{origin}{path}/.well-known/openid-configuration",
        ]
    return [f"{origin}/.well-known/oauth-authorization-server", f"{origin}/.well-known/openid-configuration"]


def canonical_resource(server_url: str) -> str:
    """The RFC 8707 resource indicator for ``server_url``: its full URL with the scheme and host lowercased, the port, path and query kept, the fragment
    and one trailing ``/`` dropped. Credentials in the URL are dropped too: they never go to an authorization server."""
    parts = urlsplit(server_url.strip())
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{_host_port(server_url.strip())}{path}{query}"


# ---- the documents -------------------------------------------------------------------------------------------------------------------------------------------------------


class AuthServerMetadata(BaseModel):
    """The part of an RFC 8414 (or OpenID) authorization-server metadata document a sign-in uses. Every endpoint is an http(s) URL with a host and no
    credentials, or the document does not validate."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    issuer: str
    authorization_endpoint: str | None = None
    token_endpoint: str
    registration_endpoint: str | None = None
    revocation_endpoint: str | None = None
    scopes_supported: list[str] | None = None
    code_challenge_methods_supported: list[str] | None = None
    token_endpoint_auth_methods_supported: list[str] | None = None
    grant_types_supported: list[str] | None = None

    @field_validator("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint")
    @classmethod
    def _an_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        problem = _http_url_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class ProtectedResource(BaseModel):
    """The part of an RFC 9728 protected-resource metadata document a sign-in uses."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    resource: str | None = None
    authorization_servers: list[str] = Field(..., min_length=1)
    scopes_supported: list[str] | None = None

    @field_validator("authorization_servers")
    @classmethod
    def _http_urls(cls, value: list[str]) -> list[str]:
        for server in value:
            problem = _http_url_problem(server)
            if problem is not None:
                raise ValueError(f"every authorization server {problem}")
        return value


@dataclass(frozen=True)
class Discovery:
    """What :func:`discover` found. ``challenged_scope`` is the ``scope`` of the server's 401 challenge, if it named one."""

    metadata: AuthServerMetadata
    protected_resource: ProtectedResource | None
    challenged_scope: str | None

    @property
    def uses_resource_indicator(self) -> bool:
        """True when the server publishes protected-resource metadata: only then is a resource indicator sent (the 2025-03-26 shape sends none)."""
        return self.protected_resource is not None


@dataclass(frozen=True)
class RegisteredClient:
    """What a dynamic registration answered. ``client_secret`` is ``None`` for a public client; it never shows in a ``repr``."""

    client_id: str
    client_secret: str | None = field(repr=False)


@dataclass(frozen=True)
class ClientAuth:
    """How a client authenticates at the token and revocation endpoints. The secret never shows in a ``repr``."""

    client_id: str
    client_secret: str | None = field(repr=False)
    method: ClientAuthMethod


@dataclass(frozen=True)
class TokenGrant:
    """A token endpoint's answer. ``expires_at`` is ``None`` when the answer carried no ``expires_in``: no known expiry. The tokens never show in a ``repr``."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    token_type: str
    expires_at: datetime | None
    scope: str | None


class TokenEndpointRejected(BadRequestError):
    """The token endpoint refused the grant (a 4xx other than 408 and 429): the code, refresh token or client credentials will not work again.

    ``error`` is the RFC 6749 error code it answered (cleaned), ``status`` the HTTP status. The message is ``token endpoint answered <status>: <error>:
    <error_description>``, cut to 200 characters; it never holds the response body. No ``status_code`` is set, so ``str()`` is the message.
    """

    def __init__(self, message: str, *, error: str | None, status: int) -> None:
        super().__init__(message)
        self.error = error
        self.status = status


# ---- parsing the challenge -----------------------------------------------------------------------------------------------------------------------------------------------


def _quoted_string(header: str, start: int) -> tuple[str, int]:
    """The quoted-string that opens at ``header[start]`` (a ``"``), unescaped, and the index after its closing quote."""
    out: list[str] = []
    i = start + 1
    while i < len(header):
        ch = header[i]
        if ch == "\\" and i + 1 < len(header):
            out.append(header[i + 1])
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return "".join(out), i


def parse_www_authenticate(header: str | None) -> dict[str, str]:
    """The parameters of the ``Bearer`` challenge in a ``WWW-Authenticate`` value, names lowercased; ``{}`` when there is none.

    ``Bearer resource_metadata="https://m/prm", scope="a b"`` gives ``{"resource_metadata": "https://m/prm", "scope": "a b"}``. Other challenges in the
    same value (``Basic realm="x", Bearer ...``) are stepped over; quoted values are unescaped; the first value of a repeated name wins.
    """
    if not header:
        return {}
    params: dict[str, str] = {}
    scheme: str | None = None
    i, n = 0, len(header)
    while i < n:
        while i < n and header[i] in " \t,":
            i += 1
        start = i
        while i < n and header[i] in _TOKEN_CHARS:
            i += 1
        token = header[start:i]
        if not token:
            i += 1                                      # a stray character: step over it
            continue
        j = i
        while j < n and header[j] in " \t":
            j += 1
        if j < n and header[j] == "=":                  # an auth-param of the current challenge
            j += 1
            while j < n and header[j] in " \t":
                j += 1
            if j < n and header[j] == '"':
                value, j = _quoted_string(header, j)
            else:
                end = j
                while end < n and header[end] not in " \t,":
                    end += 1
                value, j = header[j:end], end
            if scheme == "bearer":
                params.setdefault(token.lower(), value)
            i = j
        else:                                           # a token alone is the scheme of the next challenge
            scheme = token.lower()
            i = j
    return params


# ---- discovery -----------------------------------------------------------------------------------------------------------------------------------------------------------


def _cut(text: str, limit: int = ERROR_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _shown(value: str, limit: int = _QUOTED_LIMIT) -> str:
    """A server-provided URL or name as an error text may quote it: URL-borne credentials masked, cut."""
    return _cut(redact_url_secrets(value), limit)


def _first_problem(exc: ValidationError) -> str:
    """The first validation error as ``field: what``, without the offending value."""
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        where = ".".join(str(part) for part in error.get("loc", ())) or "document"
        what = str(error.get("msg", "invalid")).removeprefix("Value error, ")
        return _cut(f"{where}: {what}", _QUOTED_LIMIT)
    return "invalid"


async def _get_json_object(url: str, http: httpx.AsyncClient) -> tuple[dict[str, Any] | None, bool]:
    """GET ``url``: the JSON object of a 200 answer, or ``None``; and whether any answer came back at all (``False`` for a transport failure)."""
    try:
        response = await http.get(url, headers=_JSON_ACCEPT)
    except (httpx.HTTPError, httpx.InvalidURL):
        return None, False
    if response.status_code != 200:
        return None, True
    return _json_object(response), True


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:                                   # not JSON, or not UTF-8 (UnicodeDecodeError is a ValueError)
        return None
    return body if isinstance(body, dict) else None


async def probe_challenge(server_url: str, *, headers: Mapping[str, str], http: httpx.AsyncClient) -> str | None:
    """POST an MCP ``initialize`` to ``server_url`` without a token and return the ``WWW-Authenticate`` of a 401 answer; ``None`` for any other answer
    or when the server cannot be reached. ``headers`` are the toolset's own headers (an ``Authorization`` among them is not sent); the body of the answer
    is never read."""
    try:
        request_headers = httpx.Headers({name: value for name, value in headers.items() if name.lower() != "authorization"})
        request_headers["Accept"] = "application/json, text/event-stream"
        request_headers["Content-Type"] = "application/json"
        body = json.dumps(_INITIALIZE).encode("utf-8")
        async with http.stream("POST", server_url, content=body, headers=request_headers) as response:
            if response.status_code != 401:
                return None
            return response.headers.get("WWW-Authenticate")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError):
        return None


async def discover(server_url: str, *, www_authenticate: str | None, http: httpx.AsyncClient) -> Discovery:
    """Find the authorization server of the MCP server at ``server_url`` and its metadata (see the module docstring for the order).

    Raises :class:`~primer.model.except_.BadRequestError` when no usable metadata is found or a document names another issuer, and
    :class:`~primer.model.except_.NetworkError` when the authorization server answered none of the requests.
    """
    challenge = parse_www_authenticate(www_authenticate)
    protected: ProtectedResource | None = None
    for url in protected_resource_urls(server_url, challenge.get("resource_metadata")):
        document, _ = await _get_json_object(url, http)
        if document is None or "authorization_servers" not in document:
            continue
        try:
            protected = ProtectedResource.model_validate(document)
        except ValidationError as exc:
            raise BadRequestError(f"the protected-resource metadata at {_shown(url)} is not usable ({_first_problem(exc)})") from None
        break

    issuer = protected.authorization_servers[0] if protected is not None else _origin(server_url)
    answered = False
    problems: list[str] = []
    for url in auth_server_metadata_urls(issuer):
        document, reached = await _get_json_object(url, http)
        answered = answered or reached
        if document is None:
            continue
        try:
            metadata = AuthServerMetadata.model_validate(document)
        except ValidationError as exc:
            problems.append(f"the document at {_shown(url)}: {_first_problem(exc)}")
            continue
        if not _same_issuer(metadata.issuer, issuer):
            raise BadRequestError(
                f"the authorization server metadata at {_shown(url)} names the issuer {_shown(metadata.issuer)}, not {_shown(issuer)}, so it is not "
                "used (RFC 8414 section 3.3)"
            )
        return Discovery(metadata=metadata, protected_resource=protected, challenged_scope=challenge.get("scope") or None)
    if not answered:
        raise NetworkError(f"the authorization server {_shown(issuer)} could not be reached", code="network_error")
    detail = f" ({problems[0]})" if problems else ""
    raise BadRequestError(f"the authorization server {_shown(issuer)} publishes no usable OAuth metadata (RFC 8414 or OpenID discovery){detail}")


def require_s256(metadata: AuthServerMetadata) -> None:
    """Refuse an authorization server that does not advertise PKCE S256 (MCP 2025-11-25: a client must refuse when PKCE support is not advertised)."""
    if "S256" not in (metadata.code_challenge_methods_supported or []):
        raise BadRequestError(
            f"the authorization server {_shown(metadata.issuer)} does not advertise PKCE S256 (code_challenge_methods_supported), which MCP requires"
        )


def pkce_pair() -> tuple[str, str]:
    """A fresh PKCE ``(verifier, challenge)``: a 43-character verifier of 256 random bits and its S256 challenge."""
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(
    *,
    metadata: AuthServerMetadata,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    code_challenge: str,
    resource: str | None,
) -> str:
    """The URL a person opens to sign in: the authorization endpoint with ``response_type=code``, the client, the redirect URI, ``state``, the S256 challenge,
    the scopes (space-separated, left out when there are none) and the resource (left out when ``None``). A query the endpoint already has is kept, except
    for the fields this sets."""
    if not metadata.authorization_endpoint:
        raise BadRequestError(f"the authorization server {_shown(metadata.issuer)} has no authorization endpoint, so a browser sign-in cannot start")
    params = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("state", state),
        ("code_challenge", code_challenge),
        ("code_challenge_method", "S256"),
    ]
    if scopes:
        params.append(("scope", " ".join(scopes)))
    if resource:
        params.append(("resource", resource))
    parts = urlsplit(metadata.authorization_endpoint)
    ours = {name for name, _ in params}
    kept = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True) if name not in ours]
    return urlunsplit(parts._replace(query=urlencode(kept + params), fragment=""))


# ---- the calls -----------------------------------------------------------------------------------------------------------------------------------------------------------


def _call_failed(exc: Exception, what: str) -> PrimerError:
    """A call that got no usable answer: a :class:`NetworkError` when :func:`classify_mcp_exception` reads it as one (no connection, a timeout), else a
    :class:`ProviderError`. The text names the exception class, never the library's message (which can quote the request)."""
    kind = type(exc).__name__
    if isinstance(classify_mcp_exception(exc), NetworkError):
        return NetworkError(f"{what} did not answer ({kind})", code="network_error", cause=exc)
    return ProviderError(f"{what} could not be called ({kind})", cause=exc)


def _masked(text: str, secrets_sent: Iterable[str | None]) -> str:
    for secret in secrets_sent:
        if not secret:
            continue
        for form in {secret, quote(secret, safe=""), quote_plus(secret, safe="")}:
            text = text.replace(form, "[REDACTED]")
    return text


def _clean_field(value: object, secrets_sent: tuple[str | None, ...]) -> str | None:
    """An RFC 6749 ``error`` / ``error_description`` value as an error text may carry it: printable ASCII on one line, the request's secrets and any
    bearer-shaped credential masked, cut to 200 characters. ``None`` for a missing or non-text value."""
    if not isinstance(value, str):
        return None
    text = _masked(value, secrets_sent)
    text = " ".join("".join(ch if " " <= ch <= "~" else " " for ch in text).split())
    text = redact_credentials(_masked(text, secrets_sent))
    return _cut(text) or None


def _error_fields(response: httpx.Response, secrets_sent: tuple[str | None, ...]) -> tuple[str | None, str | None]:
    body = _json_object(response)
    if body is None:
        return None, None
    return _clean_field(body.get("error"), secrets_sent), _clean_field(body.get("error_description"), secrets_sent)


def _answered(what: str, status: int, error: str | None, description: str | None) -> str:
    return _cut(": ".join([f"{what} answered {status}", *(part for part in (error, description) if part)]))


def _is_refusal(status: int) -> bool:
    return 400 <= status < 500 and status not in _TEMPORARY_4XX


def _temporary(message: str, status: int) -> ProviderError:
    if status == 429:
        return RateLimitError(message, code="rate_limit", status_code=status)
    return ServerError(message, code="server_error", status_code=status)


def client_auth_for(metadata: AuthServerMetadata | None, client_id: str, client_secret: str | None) -> ClientAuth:
    """How ``client_id`` authenticates: ``none`` without a secret; with one, ``client_secret_basic`` (which RFC 6749 makes every server support), unless the
    metadata lists methods that leave Basic out and include ``client_secret_post``."""
    if not client_secret:
        return ClientAuth(client_id=client_id, client_secret=None, method="none")
    methods = metadata.token_endpoint_auth_methods_supported if metadata is not None else None
    if methods and "client_secret_basic" not in methods and "client_secret_post" in methods:
        return ClientAuth(client_id=client_id, client_secret=client_secret, method="client_secret_post")
    return ClientAuth(client_id=client_id, client_secret=client_secret, method="client_secret_basic")


def _authenticate(client: ClientAuth, body: dict[str, str], headers: dict[str, str]) -> None:
    """Add the client authentication to a token or revocation request. Basic form-encodes the id and the secret before the base64 (RFC 6749 2.3.1)."""
    if client.method == "client_secret_basic" and client.client_secret:
        pair = f"{quote(client.client_id, safe='')}:{quote(client.client_secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
    elif client.method == "client_secret_post" and client.client_secret:
        body["client_id"] = client.client_id
        body["client_secret"] = client.client_secret
    else:
        body["client_id"] = client.client_id


def _expiry(value: object, now: datetime) -> datetime | None:
    """``now + expires_in``; ``None`` (no known expiry) when the answer carried none or something that is not a non-negative number of seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not (value.isascii() and value.isdigit()):
            return None
        value = int(value)
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    try:
        return now + timedelta(seconds=value)
    except OverflowError:
        return None


def _text(body: Mapping[str, Any], key: str) -> str | None:
    value = body.get(key)
    return value if isinstance(value, str) and value else None


def _grant_of(response: httpx.Response, now: datetime) -> TokenGrant:
    body = _json_object(response)
    access = _text(body, "access_token") if body is not None else None
    if body is None or access is None:
        raise ServerError(f"token endpoint answered {response.status_code} without an access token", code="server_error", status_code=response.status_code)
    return TokenGrant(
        access_token=access,
        refresh_token=_text(body, "refresh_token"),
        token_type=_text(body, "token_type") or "Bearer",
        expires_at=_expiry(body.get("expires_in"), now),
        scope=_text(body, "scope"),
    )


async def _token_request(
    token_endpoint: str,
    client: ClientAuth,
    form: dict[str, str],
    *,
    secrets_sent: tuple[str | None, ...],
    http: httpx.AsyncClient,
    now: datetime,
) -> TokenGrant:
    body = dict(form)
    headers = dict(_JSON_ACCEPT)
    _authenticate(client, body, headers)
    try:
        response = await http.post(token_endpoint, data=body, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _call_failed(exc, "the token endpoint") from exc
    status = response.status_code
    if 200 <= status < 300:
        return _grant_of(response, now)
    error, description = _error_fields(response, (*secrets_sent, client.client_secret))
    message = _answered("token endpoint", status, error, description)
    if _is_refusal(status):
        raise TokenEndpointRejected(message, error=error, status=status)
    raise _temporary(message, status)


async def exchange_code(
    *,
    token_endpoint: str,
    client: ClientAuth,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    resource: str | None,
    http: httpx.AsyncClient,
    now: datetime,
) -> TokenGrant:
    """The authorization-code grant: the code, the redirect URI it was issued for, the PKCE verifier and, when given, the resource."""
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": code_verifier}
    if resource:
        form["resource"] = resource
    return await _token_request(token_endpoint, client, form, secrets_sent=(code, code_verifier), http=http, now=now)


async def refresh_grant(
    *,
    token_endpoint: str,
    client: ClientAuth,
    refresh_token: str,
    resource: str | None,
    http: httpx.AsyncClient,
    now: datetime,
) -> TokenGrant:
    """The refresh-token grant. It sends no ``scope``: the scope of the original grant applies (RFC 6749 section 6)."""
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if resource:
        form["resource"] = resource
    return await _token_request(token_endpoint, client, form, secrets_sent=(refresh_token,), http=http, now=now)


async def client_credentials_grant(
    *,
    token_endpoint: str,
    client: ClientAuth,
    scopes: list[str],
    resource: str | None,
    http: httpx.AsyncClient,
    now: datetime,
) -> TokenGrant:
    """The client-credentials grant, with the scopes (space-separated, left out when there are none) and, when given, the resource."""
    form = {"grant_type": "client_credentials"}
    if scopes:
        form["scope"] = " ".join(scopes)
    if resource:
        form["resource"] = resource
    return await _token_request(token_endpoint, client, form, secrets_sent=(), http=http, now=now)


async def register_client(*, metadata: AuthServerMetadata, redirect_uri: str, client_name: str, http: httpx.AsyncClient) -> RegisteredClient:
    """Register a public client by RFC 7591 dynamic registration, for the authorization-code grant with refresh tokens and this one redirect URI."""
    if not metadata.registration_endpoint:
        raise BadRequestError("the authorization server does not support dynamic client registration; enter a client id from the provider")
    payload = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    try:
        response = await http.post(metadata.registration_endpoint, json=payload, headers=dict(_JSON_ACCEPT))
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _call_failed(exc, "the registration endpoint") from exc
    status = response.status_code
    if 200 <= status < 300:
        body = _json_object(response)
        client_id = _text(body, "client_id") if body is not None else None
        if body is None or client_id is None:
            raise ServerError(f"registration endpoint answered {status} without a client_id", code="server_error", status_code=status)
        return RegisteredClient(client_id=client_id, client_secret=_text(body, "client_secret"))
    error, description = _error_fields(response, ())
    message = _answered("registration endpoint", status, error, description)
    if _is_refusal(status):
        raise BadRequestError(message)
    raise _temporary(message, status)


async def revoke(*, revocation_endpoint: str, client: ClientAuth, token: str, token_type_hint: str, http: httpx.AsyncClient) -> bool:
    """RFC 7009 revocation of ``token``, best effort: ``True`` when the server answered 2xx, ``False`` for any other answer or failure. It never raises
    (a cancellation still propagates)."""
    try:
        body = {"token": token, "token_type_hint": token_type_hint}
        headers = dict(_JSON_ACCEPT)
        _authenticate(client, body, headers)
        response = await http.post(revocation_endpoint, data=body, headers=headers)
    except Exception:  # noqa: BLE001 - best effort by contract: the caller deletes the tokens whatever this answers
        return False
    return 200 <= response.status_code < 300
