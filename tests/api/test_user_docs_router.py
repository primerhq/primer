"""REST tests for /v1/user_docs."""

from __future__ import annotations

import pytest


class TestManifest:
    @pytest.mark.asyncio
    async def test_manifest_returns_section_tree(self, client) -> None:
        r = await client.get("/v1/user_docs/manifest")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "sections" in body
        ids = [s["id"] for s in body["sections"]]
        assert "getting-started" in ids
        assert "features" in ids
        assert "cookbook" in ids


class TestDoc:
    @pytest.mark.asyncio
    async def test_unknown_slug_returns_404(self, client) -> None:
        r = await client.get("/v1/user_docs/features/nope")
        assert r.status_code == 404, r.text


class TestEmbedsManifest:
    @pytest.mark.asyncio
    async def test_embeds_manifest_returns_list(self, client) -> None:
        r = await client.get("/v1/user_docs/embeds/manifest")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "embeds" in body
        assert isinstance(body["embeds"], list)
