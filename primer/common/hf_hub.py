"""The Hugging Face Hub model check: does this model exist, and can this token pull it?

A Hugging Face embedding or cross-encoder provider names models that are pulled from the Hub (or a mirror) the first time they are used, in a worker, minutes after the
provider was saved. A typo, a gated repository or a refused token otherwise shows up there. ``check_hf_models`` asks before the provider is registered, with the one request
``huggingface_hub``'s ``HfApi.auth_check`` makes, ``GET {endpoint}/api/models/{name}/auth-check`` (a ``Bearer`` header when there is a token), and maps the answer the way the
library's ``hf_raise_for_status`` does: the ``X-Error-Code`` and ``X-Error-Message`` headers are read before the status, because a gated repository, a disabled one and a
refused token all arrive as a 401 or a 403 and only those headers tell them apart.

It is one GET, so the library is not a dependency (it ships with the optional ``huggingface`` extra, which an API pod may not have; ``httpx`` is core). The endpoint is
``HF_ENDPOINT``, the variable the Hub libraries already honour, so a deployment behind a mirror checks the mirror it will pull from.

The helper serves ``POST /v1/huggingface/_check_models``, the ``check_huggingface_models`` system tool and the draft ``_test`` of the embedding and cross-encoder providers
(``primer/api/routers/providers.py``). It stores nothing and never raises: every name gets a result.
"""

from __future__ import annotations

import os
import re
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

__all__ = [
    "HF_ENDPOINT_DEFAULT",
    "HF_REPO_ID_RE",
    "HfCheckStatus",
    "HfModelCheck",
    "check_hf_models",
    "hf_endpoint",
]

HF_ENDPOINT_DEFAULT = "https://huggingface.co"

#: What a model name may be: ``name`` or ``namespace/name``. Narrower than ``huggingface_hub``'s ``validate_repo_id`` (ASCII only, and it must start with a letter or a digit);
#: a name containing ".." or "--" fails as well (the library's rule), see :func:`_is_model_id`. Because only these characters reach the request path, a name cannot add a
#: path segment, a query or an escape to the URL the Hub is asked.
HF_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}(/[A-Za-z0-9][A-Za-z0-9._-]{0,95})?$")

HfCheckStatus = Literal["ok", "gated", "not_found", "bad_token", "disabled", "invalid_name", "error"]

_DISABLED_MESSAGE = "Access to this resource is disabled."
_REFUSED_TOKEN_MESSAGE = "Invalid credentials in Authorization header"

#: The sentence for each verdict that has one fixed sentence. ``error`` has two (the status the Hub answered, or the host that could not be reached) and ``bad_token`` has a
#: second for a token that was never sent (:data:`_UNSENDABLE_TOKEN`).
_DETAIL: dict[str, str] = {
    "ok": "Can be pulled",
    "gated": "Gated: accept the model's terms on the Hub with the token's account",
    "disabled": "Disabled on the Hub",
    "bad_token": "The Hub refused the token",
    "not_found": "Not found, or private and the token cannot see it",
    "invalid_name": "Not a Hugging Face model id (namespace/name)",
}
_UNSENDABLE_TOKEN = "The token has a space, a line break or another character that cannot be sent; paste it again"


class HfModelCheck(BaseModel):
    """The Hub's verdict on one model name."""

    name: str = Field(..., description="The model name as it was given.")
    ok: bool = Field(..., description="Whether the token can pull the model.")
    status: HfCheckStatus = Field(
        ...,
        description=(
            "ok: can be pulled. gated: the model's terms are not accepted by the token's account. not_found: it does not exist, or it is private and the token cannot see it. "
            "bad_token: the Hub refused the token. disabled: the repository is disabled. invalid_name: not a model id, so the Hub was not asked. error: the Hub could not be "
            "reached or answered something else."
        ),
    )
    detail: str = Field(..., description="One sentence for the operator. It never carries the token.")


def hf_endpoint() -> str:
    """The Hub base URL: ``HF_ENDPOINT`` without its trailing slash, else the public Hub."""
    return (os.environ.get("HF_ENDPOINT", "").strip() or HF_ENDPOINT_DEFAULT).rstrip("/")


def _is_model_id(name: str) -> bool:
    return HF_REPO_ID_RE.fullmatch(name) is not None and ".." not in name and "--" not in name


def _is_sendable(token: str) -> bool:
    """Whether ``token`` can be the value of a header: printable ASCII with no whitespace. A token pasted with a trailing space or newline is not."""
    return token.isascii() and token.isprintable() and not any(ch.isspace() for ch in token)


def _host_of(endpoint: str) -> str:
    """The host of ``endpoint`` for a sentence: never its userinfo (a mirror behind a reverse proxy can carry ``user:password@``)."""
    try:
        return urlsplit(endpoint).hostname or "the Hub"
    except ValueError:
        return "the Hub"


def _result(name: str, status: HfCheckStatus, detail: str | None = None) -> HfModelCheck:
    return HfModelCheck(name=name, ok=status == "ok", status=status, detail=detail if detail is not None else _DETAIL[status])


def _verdict(name: str, response: httpx.Response) -> HfModelCheck:
    """The mapping of ``hf_raise_for_status``, in its order: the headers first, then the status."""
    status = response.status_code
    if 200 <= status < 300:
        return _result(name, "ok")
    code = response.headers.get("X-Error-Code")
    message = response.headers.get("X-Error-Message")
    if code == "GatedRepo":
        return _result(name, "gated")
    if message == _DISABLED_MESSAGE:
        return _result(name, "disabled")
    if status == 401 and message == _REFUSED_TOKEN_MESSAGE:
        return _result(name, "bad_token")
    # The Hub answers 401 for a private or gated repository when the caller is not authenticated and for one that does not exist; the library calls all of them "not found".
    if code == "RepoNotFound" or status in (401, 404):
        return _result(name, "not_found")
    return _result(name, "error", f"The Hub answered HTTP {status}")


async def check_hf_models(
    names: list[str],
    *,
    token: str | None,
    endpoint: str | None = None,
    timeout: float = 10.0,
) -> list[HfModelCheck]:
    """One verdict per name, in the order given, asked one request after another. Never raises.

    ``token`` is sent as a ``Bearer`` header and only when it is not empty. A name that is not a model id gets ``invalid_name`` and no request is made for it; a token that
    cannot be a header value gets ``bad_token`` for every name that is a model id, also without a request (the HTTP library would refuse it and the failure would read as a
    network error). A network failure, or any failure of the request itself, is that name's ``error``: its detail names the host and the kind of failure, never the
    library's text, which can quote the request (the token, the URL of a mirror with credentials).
    """
    base = (endpoint if endpoint is not None else hf_endpoint()).rstrip("/")
    token = token or None
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    results: list[HfModelCheck] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for name in names:
            if not _is_model_id(name):
                results.append(_result(name, "invalid_name"))
            elif token is not None and not _is_sendable(token):
                results.append(_result(name, "bad_token", _UNSENDABLE_TOKEN))
            else:
                results.append(await _check_one(client, base, name, headers))
    return results


async def _check_one(client: httpx.AsyncClient, base: str, name: str, headers: dict[str, str]) -> HfModelCheck:
    try:
        # The body is never read: the verdict is in the status and two headers, and a mirror cannot make this hold memory by streaming one.
        async with client.stream("GET", f"{base}/api/models/{name}/auth-check", headers=headers) as response:
            return _verdict(name, response)
    except Exception as exc:  # noqa: BLE001 -- a diagnostic that never raises: whatever went wrong is this name's result
        return _result(name, "error", f"Could not reach {_host_of(base)} ({type(exc).__name__})")
