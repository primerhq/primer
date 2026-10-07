"""The Kubernetes template overlays are checked against an allowlist (security review 2026-10-08, INJ-02).

The old check was a denylist of host/privilege keys; it let through Secret, PVC and projected volume sources, a service
account (with its token automounted), node pinning and an extra container. Now only ``emptyDir`` and ``configMap``
volumes, plain volume mounts and a few pod-scheduling scalars pass, for every caller, admins included.
"""

from __future__ import annotations

import pytest

from primer.model.except_ import ConfigError
from primer.model.workspace import KubernetesTemplateConfig, WorkspaceTemplate
from primer.workspace.k8s.backend import _validate_template_overrides


def _template(**backend) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="t", provider_id="p", description="", backend=KubernetesTemplateConfig(image="alpine:3", **backend),
    )


_REFUSED = {
    "secret volume": ({"extra_volumes": [{"name": "s", "secret": {"secretName": "primer-db"}}]}, "secret"),
    "pvc volume": ({"extra_volumes": [{"name": "p", "persistentVolumeClaim": {"claimName": "other"}}]},
                   "persistentVolumeClaim"),
    "projected volume": ({"extra_volumes": [{"name": "t", "projected": {"sources": [{"serviceAccountToken": {
        "path": "token"}}]}}]}, "projected"),
    "hostPath volume": ({"extra_volumes": [{"name": "h", "hostPath": {"path": "/"}}]}, "hostPath"),
    "configMap with an unknown key": ({"extra_volumes": [{"name": "c", "configMap": {"name": "x", "bogus": 1}}]},
                                      "bogus"),
    "mount propagation": ({"extra_volume_mounts": [{"name": "c", "mountPath": "/m",
                                                    "mountPropagation": "Bidirectional"}]}, "mountPropagation"),
    "serviceAccountName": ({"pod_overrides": {"serviceAccountName": "primer-admin"}}, "serviceAccountName"),
    "automountServiceAccountToken": ({"pod_overrides": {"automountServiceAccountToken": True}},
                                     "automountServiceAccountToken"),
    "hostNetwork": ({"pod_overrides": {"hostNetwork": True}}, "hostNetwork"),
    "hostPID": ({"pod_overrides": {"hostPID": True}}, "hostPID"),
    "hostIPC": ({"pod_overrides": {"hostIPC": True}}, "hostIPC"),
    "pod securityContext": ({"pod_overrides": {"securityContext": {"runAsUser": 0}}}, "securityContext"),
    "nodeName": ({"pod_overrides": {"nodeName": "control-plane"}}, "nodeName"),
    "nodeSelector": ({"pod_overrides": {"nodeSelector": {"role": "agent"}}}, "nodeSelector"),
    "tolerations": ({"pod_overrides": {"tolerations": [{"operator": "Exists"}]}}, "tolerations"),
    "an extra container": ({"pod_overrides": {"containers": [{"name": "x", "image": "busybox"}]}}, "containers"),
    "initContainers": ({"pod_overrides": {"initContainers": [{"name": "x", "image": "busybox"}]}}, "initContainers"),
    "volumes through pod_overrides": ({"pod_overrides": {"volumes": [{"name": "h", "hostPath": {"path": "/"}}]}},
                                      "volumes"),
    "a structured value under an allowed key": ({"pod_overrides": {"dnsPolicy": {"x": 1}}}, "dnsPolicy"),
    "privileged container": ({"container_security_context_overrides": {"privileged": True}}, "privileged"),
    "any container security context": ({"container_security_context_overrides": {"readOnlyRootFilesystem": True}},
                                       "readOnlyRootFilesystem"),
}


@pytest.mark.parametrize("case", sorted(_REFUSED))
def test_an_overlay_outside_the_allowlist_is_refused(case) -> None:
    backend, key = _REFUSED[case]

    with pytest.raises(ConfigError, match=key):
        _validate_template_overrides(_template(**backend))


def test_the_allowlisted_overlays_pass() -> None:
    _validate_template_overrides(_template(
        extra_volumes=[
            {"name": "scratch", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}},
            {"name": "cfg", "configMap": {"name": "tools", "defaultMode": 420, "optional": True,
                                          "items": [{"key": "a", "path": "a.sh", "mode": 493}]}},
        ],
        extra_volume_mounts=[
            {"name": "scratch", "mountPath": "/scratch"},
            {"name": "cfg", "mountPath": "/etc/tools", "readOnly": True, "subPath": "a.sh"},
        ],
        pod_overrides={
            "shareProcessNamespace": False, "dnsPolicy": "ClusterFirst", "restartPolicy": "Always",
            "terminationGracePeriodSeconds": 5,
        },
        container_security_context_overrides={},
    ))
