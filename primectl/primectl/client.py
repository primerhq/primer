"""Thin HTTP client for the Primer API.

Wraps ``httpx.Client``: injects the bearer token when present, returns the raw
response on success, and raises typed errors on failure. A ``transport`` may be
injected for tests (``httpx.MockTransport``).
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """A non-2xx HTTP response. Carries the status + parsed ProblemDetails."""

    def __init__(self, status: int, problem: dict | None, body_text: str) -> None:
        self.status = status
        self.problem = problem or {}
        self.body_text = body_text
        detail = self.problem.get("detail") or self.problem.get("title") or body_text
        super().__init__(f"HTTP {status}: {detail}")


class ConnectionFailed(Exception):
    """The request never reached the server (DNS/connect/timeout)."""


class ApiClient:
    def __init__(
        self,
        server: str,
        token: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        verbose: bool = False,
        timeout: float = 30.0,
    ) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._verbose = verbose
        self._http = httpx.Client(
            base_url=server.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        if self._verbose:
            import sys

            print(f"> {method.upper()} {path} params={params}", file=sys.stderr)
        try:
            resp = self._http.request(method.upper(), path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise ConnectionFailed(str(exc)) from exc
        if self._verbose:
            import sys

            print(f"< {resp.status_code}", file=sys.stderr)
        if resp.status_code >= 400:
            problem: dict | None = None
            try:
                body = resp.json()
                if isinstance(body, dict):
                    problem = body
            except Exception:
                problem = None
            raise ApiError(resp.status_code, problem, resp.text)
        return resp

    def close(self) -> None:
        self._http.close()
