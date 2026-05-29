"""Tests for primer.workspace.config_compat — translation helpers placeholder."""
from primer.workspace.config_compat import (
    container_template_defaults_for_legacy_provider,
    k8s_template_defaults_for_legacy_provider,
)
from primer.model.workspace import (
    ContainerWorkspaceConfig,
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    KubernetesWorkspaceConfig,
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
)


def test_container_defaults_when_no_legacy_fields():
    cfg = ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
        reachability=ContainerReachabilityHostPort(),
    )
    # New config has nothing template-flavoured to lift — empty dict.
    assert container_template_defaults_for_legacy_provider(cfg) == {}


def test_k8s_defaults_when_no_legacy_fields():
    cfg = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer",
        reachability=K8sReachabilityInCluster(),
    )
    assert k8s_template_defaults_for_legacy_provider(cfg) == {}
