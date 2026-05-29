"""Derive the WS URL the platform uses to reach a workspace's primer-runtime.

URL is a function of the provider's reachability mode + the workspace id,
plus (for container/host_port) the discovered host port and (for k8s)
the truncated/hashed k8s object name.
"""
from __future__ import annotations

from primer.model.workspace import (
    ContainerWorkspaceConfig,
    KubernetesWorkspaceConfig,
)

_RUNTIME_PORT = 5959


def build_runtime_url(
    *,
    provider_config,
    workspace_id: str,
    mapped_host_port: int | None = None,
    k8s_object_name: str | None = None,
) -> str:
    if isinstance(provider_config, ContainerWorkspaceConfig):
        return _container_url(provider_config, workspace_id, mapped_host_port)
    if isinstance(provider_config, KubernetesWorkspaceConfig):
        return _k8s_url(provider_config, workspace_id, k8s_object_name)
    raise TypeError(
        f"Unsupported provider config type: {type(provider_config).__name__}"
    )


def _container_url(
    cfg: ContainerWorkspaceConfig,
    workspace_id: str,
    mapped_host_port: int | None,
) -> str:
    r = cfg.reachability
    if r.kind == "host_port":
        if mapped_host_port is None:
            raise ValueError("host_port reachability requires mapped_host_port")
        return f"ws://{r.bind_host}:{mapped_host_port}/"
    if r.kind == "bridge_network":
        return f"ws://workspace-{workspace_id}:{_RUNTIME_PORT}/"
    raise ValueError(f"Unknown container reachability kind: {r.kind}")


def _k8s_url(
    cfg: KubernetesWorkspaceConfig,
    workspace_id: str,
    k8s_object_name: str | None,
) -> str:
    r = cfg.reachability
    if r.kind == "in_cluster":
        if k8s_object_name is None:
            raise ValueError("k8s in_cluster reachability requires k8s_object_name")
        # Headless service DNS: <pod-name>.<service-name>.<ns>.svc.cluster.local
        # StatefulSet pod name is <sts-name>-0
        return (
            f"ws://{k8s_object_name}-0.{k8s_object_name}."
            f"{cfg.namespace}.svc.cluster.local:{_RUNTIME_PORT}/"
        )
    if r.kind == "ingress":
        return r.url_template.format(workspace_id=workspace_id)
    raise ValueError(f"Unknown k8s reachability kind: {r.kind}")
