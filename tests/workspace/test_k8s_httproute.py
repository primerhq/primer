"""Unit tests for the pure HTTPRoute route + manifest builders."""
from primer.model.workspace import (
    K8sReachabilityGateway,
    K8sGatewayParentRef,
    K8sGatewayRoutingHostname,
    K8sGatewayRoutingPath,
)
from primer.workspace.k8s.httproute import render_route, build_httproute_manifest


def _hostname_reach():
    return K8sReachabilityGateway(
        gateway=K8sGatewayParentRef(name="primer-gw", namespace="primer-gateway"),
        routing=K8sGatewayRoutingHostname(hostname_template="{workspace_id}.ws.local"),
        external_port=32045,
    )


def _path_reach():
    return K8sReachabilityGateway(
        gateway=K8sGatewayParentRef(name="primer-gw", section_name="web"),
        routing=K8sGatewayRoutingPath(hostname="ws.local"),
        external_port=32045,
    )


def test_render_route_hostname():
    t = render_route(_hostname_reach(), "ws-abc")
    assert t.hostname == "ws-abc.ws.local"
    assert t.path == "/"


def test_render_route_path():
    t = render_route(_path_reach(), "ws-abc")
    assert t.hostname == "ws.local"
    assert t.path == "/ws/ws-abc"


def test_manifest_hostname_mode():
    m = build_httproute_manifest(
        reachability=_hostname_reach(),
        workspace_id="ws-abc",
        obj_name="primer-ws-ws-abc",
        namespace="primer-workspaces",
    )
    assert m["apiVersion"] == "gateway.networking.k8s.io/v1"
    assert m["kind"] == "HTTPRoute"
    assert m["metadata"]["name"] == "primer-ws-ws-abc"
    assert m["metadata"]["namespace"] == "primer-workspaces"
    assert m["metadata"]["labels"]["workspace-id"] == "ws-abc"
    assert m["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "primer"
    parent = m["spec"]["parentRefs"][0]
    assert parent == {"name": "primer-gw", "namespace": "primer-gateway"}
    assert m["spec"]["hostnames"] == ["ws-abc.ws.local"]
    rule = m["spec"]["rules"][0]
    assert rule["matches"][0]["path"] == {"type": "PathPrefix", "value": "/"}
    assert "filters" not in rule  # no rewrite in hostname mode
    assert rule["backendRefs"][0] == {"name": "primer-ws-ws-abc", "port": 5959}  # backend_port model default


def test_manifest_path_mode_has_rewrite_and_sectionname():
    m = build_httproute_manifest(
        reachability=_path_reach(),
        workspace_id="ws-abc",
        obj_name="primer-ws-ws-abc",
        namespace="primer-workspaces",
    )
    parent = m["spec"]["parentRefs"][0]
    assert parent == {"name": "primer-gw", "sectionName": "web"}
    assert m["spec"]["hostnames"] == ["ws.local"]
    rule = m["spec"]["rules"][0]
    assert rule["matches"][0]["path"] == {"type": "PathPrefix", "value": "/ws/ws-abc"}
    assert rule["filters"][0]["type"] == "URLRewrite"
    assert rule["filters"][0]["urlRewrite"]["path"] == {
        "type": "ReplacePrefixMatch", "replacePrefixMatch": "/",
    }
    assert rule["backendRefs"][0] == {"name": "primer-ws-ws-abc", "port": 5959}  # backend_port model default
