import pytest
from primer.workspace.runtime.url import build_runtime_url
from primer.model.workspace import (
    ContainerWorkspaceConfig,
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerReachabilityBridge,
    KubernetesWorkspaceConfig,
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
    K8sReachabilityIngress,
)


def test_container_host_port_uses_loopback():
    cfg = ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
        reachability=ContainerReachabilityHostPort(bind_host="127.0.0.1"),
    )
    url = build_runtime_url(
        provider_config=cfg, workspace_id="x", mapped_host_port=32100,
    )
    assert url == "ws://127.0.0.1:32100/"


def test_container_host_port_requires_mapped_port():
    cfg = ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
        reachability=ContainerReachabilityHostPort(),
    )
    with pytest.raises(ValueError, match="mapped_host_port"):
        build_runtime_url(provider_config=cfg, workspace_id="x")


def test_container_bridge_uses_container_name():
    cfg = ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
        reachability=ContainerReachabilityBridge(network_name="primer-net"),
    )
    url = build_runtime_url(provider_config=cfg, workspace_id="ws-1")
    assert url == "ws://workspace-ws-1:5959/"


def test_k8s_in_cluster_uses_service_dns():
    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer",
        reachability=K8sReachabilityInCluster(),
    )
    url = build_runtime_url(
        provider_config=cfg,
        workspace_id="ws-1",
        k8s_object_name="primer-ws-ws-1",
    )
    assert url == "ws://primer-ws-ws-1-0.primer-ws-ws-1.primer.svc.cluster.local:5959/"


def test_k8s_in_cluster_requires_object_name():
    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer",
        reachability=K8sReachabilityInCluster(),
    )
    with pytest.raises(ValueError, match="k8s_object_name"):
        build_runtime_url(provider_config=cfg, workspace_id="ws-1")


def test_k8s_ingress_substitutes_template():
    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer",
        reachability=K8sReachabilityIngress(
            url_template="wss://workspaces.example.com/{workspace_id}/",
        ),
    )
    url = build_runtime_url(provider_config=cfg, workspace_id="ws-1")
    assert url == "wss://workspaces.example.com/ws-1/"


def test_rejects_unknown_provider():
    with pytest.raises(TypeError):
        build_runtime_url(provider_config=object(), workspace_id="ws-1")
