"""Pure helpers to derive the per-workspace Gateway API HTTPRoute and the
dial URL target from a ``K8sReachabilityGateway`` config.

Kept I/O-free so both the URL builder (primer/workspace/runtime/url.py) and
the K8s backend (primer/workspace/k8s/backend.py) share one source of truth
for hostname/path rendering.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, assert_never

from primer.model.workspace import K8sReachabilityGateway


@dataclass(frozen=True)
class RouteTarget:
    """The Host the platform dials / the HTTPRoute matches, plus the URL path.

    ``path`` is ``"/"`` for hostname routing and ``"/ws/<id>"`` for path
    routing (rewritten back to ``"/"`` by the HTTPRoute's URLRewrite filter
    before reaching the runtime, which serves at ``/``).
    """

    hostname: str
    path: str


def render_route(reachability: K8sReachabilityGateway, workspace_id: str) -> RouteTarget:
    """Compute the RouteTarget (hostname + URL path) for a workspace."""
    r = reachability.routing
    if r.kind == "hostname":
        return RouteTarget(
            hostname=r.hostname_template.format(workspace_id=workspace_id),
            path="/",
        )
    if r.kind == "path_prefix":
        return RouteTarget(
            hostname=r.hostname,
            path=r.path_template.format(workspace_id=workspace_id),
        )
    assert_never(r)


def build_httproute_manifest(
    *,
    reachability: K8sReachabilityGateway,
    workspace_id: str,
    obj_name: str,
    namespace: str,
) -> dict[str, Any]:
    target = render_route(reachability, workspace_id)

    parent: dict[str, Any] = {"name": reachability.gateway.name}
    if reachability.gateway.namespace:
        parent["namespace"] = reachability.gateway.namespace
    if reachability.gateway.section_name:
        parent["sectionName"] = reachability.gateway.section_name

    rule: dict[str, Any] = {
        "matches": [{"path": {"type": "PathPrefix", "value": target.path}}],
        "backendRefs": [
            {"name": obj_name, "port": reachability.backend_port},
        ],
    }
    if reachability.routing.kind == "path_prefix":
        rule["filters"] = [
            {
                "type": "URLRewrite",
                "urlRewrite": {
                    "path": {
                        "type": "ReplacePrefixMatch",
                        "replacePrefixMatch": "/",
                    },
                },
            },
        ]

    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {
            "name": obj_name,
            "namespace": namespace,
            "labels": {
                "workspace-id": workspace_id,
                "app.kubernetes.io/managed-by": "primer",
            },
        },
        "spec": {
            "parentRefs": [parent],
            "hostnames": [target.hostname],
            "rules": [rule],
        },
    }


__all__ = ["RouteTarget", "render_route", "build_httproute_manifest"]
