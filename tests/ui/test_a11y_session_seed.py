"""``delete_paths`` of the seed helper (review of #668, round 3, N12): what it deletes, in which order, and what it reports.

A journey that seeds rows must not leave them behind on a shared install, and a row that could not be deleted is a fact for the journey to report (never an exception that hides the findings it
was called to clean up after). The server is a ``httpx.MockTransport``.
"""

from __future__ import annotations

import httpx

from tests.ui_e2e._session_seed import delete_paths


def test_rows_are_deleted_newest_first_and_only_a_failure_other_than_not_found_is_reported() -> None:
    seen: list[str] = []
    statuses = {"/v1/a": 204, "/v1/b": 404, "/v1/c": 500, "/v1/d": 403}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(statuses[request.url.path])

    left = delete_paths("http://server", ["/v1/a", "/v1/b", "/v1/c", "/v1/d"], transport=httpx.MockTransport(handler))
    assert seen == ["DELETE /v1/d", "DELETE /v1/c", "DELETE /v1/b", "DELETE /v1/a"], "newest first"
    assert left == ["/v1/d (403)", "/v1/c (500)"], "a row that is already gone (404) is not a problem"


def test_a_request_that_cannot_be_made_is_reported_and_does_not_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert delete_paths("http://server", ["/v1/a", "/v1/b"], transport=httpx.MockTransport(handler)) == ["/v1/b", "/v1/a"]


def test_nothing_to_delete_is_nothing_left() -> None:
    assert delete_paths("http://server", [], transport=httpx.MockTransport(lambda request: httpx.Response(500))) == []
