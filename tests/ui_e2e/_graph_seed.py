"""Seed the escape-stack probe graph for a journey, in a module of its own.

``seed_graph`` posts the fixed begin / tool_call / end / end topology that the Escape-stack journey (SMK-UI-06) and the focus-layer journeys drive: the tool call routes to ``done`` when its
output says ``ok`` and ``n > 3``, otherwise to ``other``. A journey seeds it under its own graph id and deletes it afterwards, best effort. A test module is not a library, so the helper
lives here and not in a ``test_*.py`` (the ui-e2e lane's import-what-exists check, review of #668, round 3, B1'').
"""

from __future__ import annotations

import httpx


def seed_graph(base_url: str, graph_id: str) -> None:
    nodes = [
        {"kind": "begin", "id": "begin"},
        {"kind": "tool_call", "id": "check", "tool_id": "workspaces__list_files", "arguments": {"path": "."}},
        {"kind": "end", "id": "done", "output_template": ""},
        {"kind": "end", "id": "other", "output_template": ""},
    ]
    edges = [
        {"kind": "static", "from_node": "begin", "to_node": "check"},
        {"kind": "conditional", "from_node": "check", "router": {"kind": "json_path", "branches": [
            {"conditions": [{"path": "ok", "op": "eq", "value": True}, {"path": "n", "op": "gt", "value": 3}], "to_node": "done"}], "default_to": "other"}},
    ]
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={"id": graph_id, "description": "escape probe", "nodes": nodes, "edges": edges})
        assert r.status_code == 201, r.text
