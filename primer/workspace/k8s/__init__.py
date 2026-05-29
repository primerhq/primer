"""Kubernetes workspace backend.

Wraps a single-replica StatefulSet + PVC per workspace and uses
``kubernetes-asyncio`` to manage Pod lifecycle and exec.
"""

from primer.workspace.k8s.backend import KubernetesWorkspaceBackend


__all__ = ["KubernetesWorkspaceBackend"]
