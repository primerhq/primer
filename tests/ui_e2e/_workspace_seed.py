"""Seed a local workspace provider, template and workspace for a journey, and take the rows away again.

``seed_workspace`` creates, in this order, a local workspace provider rooted at the journey's ``tmp_path``, a workspace template on it, and a workspace from the template, and returns the workspace id. ``cleanup`` deletes the URLs a journey
lists, best effort (an already-gone row is not an error), so a journey that seeds does not leave rows behind on a shared install. A test module is not a library, so the helpers live here and not in a ``test_*.py`` (the ui-e2e lane's
import-what-exists check, review of #668, round 3, B1'').
"""

from __future__ import annotations

import httpx


def seed_workspace(base_url: str, wp_id: str, tpl_id: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id, "description": "ws tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
        })
        assert r.status_code == 201
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201
        return r.json()["id"]


def cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass
