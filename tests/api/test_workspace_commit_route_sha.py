"""The commit route only accepts a hex object name (review of workspaces, W13): an option-shaped sha answers 422 before any workspace is touched."""

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("sha", ["--output=pwned.txt", "--output-indicator-new=x", "HEAD~1234", "not-a-sha-at-all", "abc123", "g" * 40, "a" * 65])
async def test_the_commit_route_refuses_anything_but_hex(client, sha):
    resp = await client.get(f"/v1/workspaces/ws-any/commit/{sha}")

    assert resp.status_code == 422, (sha, resp.text)
