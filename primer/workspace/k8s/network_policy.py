"""The default-deny egress NetworkPolicy for Kubernetes workspaces.

With a provider's ``KubernetesWorkspaceConfig.default_deny_egress`` on, the
k8s backend applies ONE namespace-scoped NetworkPolicy (idempotently, at
backend start) that selects the primer workspace pods and allows NO egress
(lane D, 2026-11-23, deferred to X3.2): the workspace pods cannot reach the
internet, other namespaces or cluster services until the operator adds allow
rules (DNS, package registries, ...). The platform's own reach to the pods is
INBOUND (headless service, ingress or the per-workspace Gateway HTTPRoute)
and is not affected.

The policy is selected by label, not namespace-wide: only the pods that carry
the label every workspace StatefulSet template writes are bound by it, so
other workloads sharing the namespace are untouched. The backend never
deletes it: removing it while a workspace pod still runs would open the hole
the flag closes, so its lifecycle belongs to the operator.
"""

from __future__ import annotations

from typing import Any

#: The policy name, stable across workspaces and processes (idempotent create).
DEFAULT_DENY_EGRESS_POLICY_NAME = "primer-workspaces-default-deny-egress"

#: The label every workspace pod carries (the StatefulSet template labels,
#: k8s/backend.py): the podSelector binding.
_WORKSPACE_POD_LABEL = {"app.kubernetes.io/managed-by": "primer"}


def build_default_deny_egress_policy_manifest(namespace: str) -> dict[str, Any]:
    """The NetworkPolicy body the k8s backend applies when the default-deny
    egress opt-in is on: no egress for the workspace pods of *namespace*."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": DEFAULT_DENY_EGRESS_POLICY_NAME,
            "namespace": namespace,
            "labels": dict(_WORKSPACE_POD_LABEL),
        },
        "spec": {
            "podSelector": {"matchLabels": dict(_WORKSPACE_POD_LABEL)},
            "policyTypes": ["Egress"],
            "egress": [],  # an empty egress list DENIES all egress
        },
    }


__all__ = ["DEFAULT_DENY_EGRESS_POLICY_NAME", "build_default_deny_egress_policy_manifest"]
