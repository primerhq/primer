"""Seed an agent session on a local workspace for a journey, and take it all away again.

``seed_session`` creates, in this order, an LLM provider (and its model profile), an agent, a local workspace provider, a workspace template, a workspace and a session on that agent, and
remembers what it created. ``delete_seeded`` removes them in the reverse order, best effort (a row that is already gone is not an error), so a journey that seeds does not leave rows behind
on a shared install. The workspace root is the journey's ``tmp_path``: the server and the journey share a filesystem in the CI lane, so a session log can be written straight into
``<tmp_path>/<wid>/.state/sessions/<sid>/messages.jsonl``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx

from tests._support.model_profiles import agent_model, profile_id_for, seed_llm_provider_with


@dataclass
class SeededSession:
    wid: str
    sid: str
    delete_paths: list[str] = field(default_factory=list)   # in creation order; deleted in reverse


def seed_session(base_url: str, tmp_path: Path, suffix: str, *, description: str = "journey probe", transport: httpx.BaseTransport | None = None) -> SeededSession:
    """Create the rows listed above and return them. When it dies half way (a refused request), what it had created is deleted before the error goes on: a failed seed leaves nothing behind.
    Only a 201 is recorded for deletion: a 409 means the row was already there (somebody else's, or a leftover), and the journey must not take it away. ``transport`` is for a test."""
    prov, aid, wp, tpl = f"dn-prov-{suffix}", f"dn-agent-{suffix}", f"dn-wp-{suffix}", f"dn-tpl-{suffix}"
    created: list[str] = []
    try:
        with httpx.Client(base_url=base_url, timeout=30.0, transport=transport) as c:
            r = seed_llm_provider_with(c, {
                "id": prov, "provider": "ollama", "config": {"url": "http://127.0.0.1:9999"},
                "models": [{"name": "fake-model", "context_length": 4096}], "limits": {"max_concurrency": 1},
            })
            assert r.status_code in (201, 409), r.text
            if r.status_code == 201:
                created += [f"/v1/llm_providers/{prov}", f"/v1/model_profiles/{profile_id_for(prov, 'fake-model')}"]
            r = c.post("/v1/agents", json={"id": aid, "description": description, "model": agent_model(prov, "fake-model"), "tools": [], "system_prompt": ["test"]})
            assert r.status_code in (201, 409), r.text
            if r.status_code == 201:
                created.append(f"/v1/agents/{aid}")
            r = c.post("/v1/workspace_providers", json={"id": wp, "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path)}})
            assert r.status_code in (201, 409), r.text
            if r.status_code == 201:
                created.append(f"/v1/workspace_providers/{wp}")
            r = c.post("/v1/workspace_templates", json={"id": tpl, "description": "tpl", "provider_id": wp, "backend": {"kind": "local"}})
            assert r.status_code in (201, 409), r.text
            if r.status_code == 201:
                created.append(f"/v1/workspace_templates/{tpl}")
            r = c.post("/v1/workspaces", json={"template_id": tpl})
            assert r.status_code == 201, r.text
            wid = r.json()["id"]
            created.append(f"/v1/workspaces/{wid}")
            r = c.post(f"/v1/workspaces/{wid}/sessions", json={"binding": {"kind": "agent", "agent_id": aid}, "auto_start": False})
            assert r.status_code == 201, r.text
            sid = r.json()["id"]
            created.append(f"/v1/workspaces/{wid}/sessions/{sid}")
    except BaseException:
        delete_paths(base_url, created, transport=transport)
        raise
    return SeededSession(wid=wid, sid=sid, delete_paths=created)


def delete_paths(base_url: str, paths: list[str], *, transport: httpx.BaseTransport | None = None) -> list[str]:
    """Delete ``paths`` newest first (the list is in creation order). Returns the ones that could not be deleted (a non-2xx other than 404, or no answer), for the caller to report; never raises.
    ``transport`` is for a test (``httpx.MockTransport``)."""
    left: list[str] = []
    try:
        with httpx.Client(base_url=base_url, timeout=30.0, transport=transport) as c:
            for path in reversed(paths):
                try:
                    r = c.delete(path)
                except httpx.HTTPError:
                    left.append(path)
                    continue
                if r.status_code >= 300 and r.status_code != 404:
                    left.append(f"{path} ({r.status_code})")
    except httpx.HTTPError:
        return [*paths]
    return left


def delete_seeded(base_url: str, seeded: SeededSession) -> list[str]:
    """Delete what ``seed_session`` created, newest first. Returns the paths that could not be deleted, for the journey to report."""
    return delete_paths(base_url, seeded.delete_paths)
