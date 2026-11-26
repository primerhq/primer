"""The Kubernetes template overlays are checked against an allowlist (security review 2026-10-08, INJ-02).

The old check was a denylist of host/privilege keys; it let through Secret, PVC and projected volume sources, a service
account (with its token automounted), node pinning and an extra container. Now only ``emptyDir`` and ``configMap``
volumes, plain volume mounts, pod-scheduling scalars and the allowlisted security-context keys pass (security review
2026-11-23, 5.2: the manifest writes its own safe defaults; an admin may adjust them), for every caller, admins included.
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
    "hostNetwork": ({"pod_overrides": {"hostNetwork": True}}, "hostNetwork"),
    "hostPID": ({"pod_overrides": {"hostPID": True}}, "hostPID"),
    "hostIPC": ({"pod_overrides": {"hostIPC": True}}, "hostIPC"),
    "nodeName": ({"pod_overrides": {"nodeName": "control-plane"}}, "nodeName"),
    "nodeSelector": ({"pod_overrides": {"nodeSelector": {"role": "agent"}}}, "nodeSelector"),
    "tolerations": ({"pod_overrides": {"tolerations": [{"operator": "Exists"}]}}, "tolerations"),
    "an extra container": ({"pod_overrides": {"containers": [{"name": "x", "image": "busybox"}]}}, "containers"),
    "initContainers": ({"pod_overrides": {"initContainers": [{"name": "x", "image": "busybox"}]}}, "initContainers"),
    "volumes through pod_overrides": ({"pod_overrides": {"volumes": [{"name": "h", "hostPath": {"path": "/"}}]}},
                                      "volumes"),
    "a structured value under an allowed key": ({"pod_overrides": {"dnsPolicy": {"x": 1}}}, "dnsPolicy"),
    # security review 2026-11-23 (5.2): the security contexts are allowlisted now, so what is refused is the
    # non-surface, not the surface itself.
    "a pod securityContext key outside the v1 surface": (
        {"pod_overrides": {"securityContext": {"bogus": 1}}}, "bogus"),
    "a structured value under a scalar pod securityContext key": (
        {"pod_overrides": {"securityContext": {"runAsUser": {"x": 1}}}}, "runAsUser"),
    "a container securityContext key outside the v1 surface": (
        {"container_security_context_overrides": {"bogus": True}}, "bogus"),
    "a structured value under a scalar container securityContext key": (
        {"container_security_context_overrides": {"runAsNonRoot": {"x": 1}}}, "runAsNonRoot"),
    "a capabilities entry that is not add or drop": (
        {"container_security_context_overrides": {"capabilities": {"keep": ["NET_RAW"]}}}, "keep"),
    # fix round 1 (CRITICAL 1): the allowlist binds admins too, so nothing that gives the pod root on the node passes.
    "a privileged container": ({"container_security_context_overrides": {"privileged": True}}, "privileged"),
    "privileged given as a truthy non-bool": ({"container_security_context_overrides": {"privileged": 1}}, "privileged"),
    "privilege escalation": (
        {"container_security_context_overrides": {"allowPrivilegeEscalation": True}}, "allowPrivilegeEscalation"),
    "removing the privilege escalation default": (
        {"container_security_context_overrides": {"allowPrivilegeEscalation": None}}, "allowPrivilegeEscalation"),
    "an unmasked /proc": ({"container_security_context_overrides": {"procMount": "Unmasked"}}, "procMount"),
    "an unconfined container seccomp profile": (
        {"container_security_context_overrides": {"seccompProfile": {"type": "Unconfined"}}}, "Unconfined"),
    "an unconfined pod seccomp profile": (
        {"pod_overrides": {"securityContext": {"seccompProfile": {"type": "Unconfined"}}}}, "Unconfined"),
    "an unconfined apparmor profile": (
        {"container_security_context_overrides": {"apparmorProfile": {"type": "Unconfined"}}}, "Unconfined"),
    "an unconfined appArmorProfile": (
        {"container_security_context_overrides": {"appArmorProfile": {"type": "Unconfined"}}}, "Unconfined"),
    "container seLinuxOptions": (
        {"container_security_context_overrides": {"seLinuxOptions": {"type": "spc_t"}}}, "seLinuxOptions"),
    "pod seLinuxOptions": (
        {"pod_overrides": {"securityContext": {"seLinuxOptions": {"type": "spc_t"}}}}, "seLinuxOptions"),
    "container windowsOptions": (
        {"container_security_context_overrides": {"windowsOptions": {"runAsUserName": "x"}}}, "windowsOptions"),
    "pod sysctls": (
        {"pod_overrides": {"securityContext": {"sysctls": [{"name": "net.core.somaxconn", "value": "1"}]}}},
        "sysctls"),
    "CAP_SYS_ADMIN": ({"container_security_context_overrides": {"capabilities": {"add": ["SYS_ADMIN"]}}}, "SYS_ADMIN"),
    "CAP_NET_ADMIN": ({"container_security_context_overrides": {"capabilities": {"add": ["NET_ADMIN"]}}}, "NET_ADMIN"),
    "CAP_SYS_PTRACE": ({"container_security_context_overrides": {"capabilities": {"add": ["SYS_PTRACE"]}}},
                       "SYS_PTRACE"),
    "all capabilities": ({"container_security_context_overrides": {"capabilities": {"add": ["ALL"]}}}, "ALL"),
    "a non-baseline capability in lower case with a CAP_ prefix": (
        {"container_security_context_overrides": {"capabilities": {"add": ["cap_sys_admin"]}}}, "cap_sys_admin"),
    "a non-ASCII capability name that str.upper folds into a baseline name": (
        {"container_security_context_overrides": {"capabilities": {"add": ["M\u212ANOD"]}}}, "non-ASCII"),
    "a non-baseline capability beside a baseline one": (
        {"container_security_context_overrides": {"capabilities": {"add": ["CHOWN", "NET_RAW"]}}}, "NET_RAW"),
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
            # security review 2026-11-23 (5.2): the manifest's safe defaults are admin-adjustable through these keys.
            "automountServiceAccountToken": True,
            "securityContext": {
                "runAsUser": 1000, "runAsGroup": 1000, "runAsNonRoot": True, "fsGroup": 1000,
                "supplementalGroups": [1000], "seccompProfile": {"type": "Localhost", "localhostProfile": "p.json"},
            },
        },
        container_security_context_overrides={
            "readOnlyRootFilesystem": True,
            "runAsGroup": 1000, "runAsNonRoot": True, "runAsUser": 1000,
            # the values that stay at or above the Pod Security Standard baseline
            "privileged": False, "allowPrivilegeEscalation": False, "procMount": "Default",
            "seccompProfile": {"type": "RuntimeDefault"},
            "apparmorProfile": {"type": "Localhost", "localhost": "x.apparmor.d"},
            "appArmorProfile": {"type": "RuntimeDefault"},
            "capabilities": {"add": ["NET_BIND_SERVICE"], "drop": ["NET_RAW"]},
        },
    ))


@pytest.mark.parametrize("capability", sorted({
    "AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "MKNOD", "NET_BIND_SERVICE", "SETFCAP",
    "SETGID", "SETPCAP", "SETUID", "SYS_CHROOT",
}))
def test_every_baseline_capability_may_be_added_back(capability) -> None:
    """The Pod Security Standard baseline set is what an admin can add back after the manifest's drop ALL (the bare
    name, the ``CAP_`` prefix and lower case all name the same capability)."""
    for spelling in (capability, f"CAP_{capability}", capability.lower()):
        _validate_template_overrides(_template(
            container_security_context_overrides={"capabilities": {"add": [spelling]}},
        ))


def test_a_non_ascii_capability_name_is_refused_before_the_fold() -> None:
    """The comparison folds names with ``str.upper``, which is Unicode-aware (the ﬁ ligature folds to "FI", sharp s
    to "SS"): a non-ASCII spelling that folds into a baseline name would pass the check while the manifest carries
    the raw name (X3.3). The guard refuses non-ASCII names before the fold, with a message that says why. Today no
    baseline name contains a foldable sequence, so this is the boundary, not a live bypass."""
    value = "\ufb01DER"  # the ﬁ ligature folds to "FI" under str.upper
    assert value.upper() == "FIDER"
    with pytest.raises(ConfigError, match="non-ASCII"):
        _validate_template_overrides(_template(
            container_security_context_overrides={"capabilities": {"add": [value]}},
        ))


# ---- platform revamp 2026-10 (plan 08, task 2): the typed placement fields do not widen the allowlist ---------------------

@pytest.mark.parametrize("overlay, key", [
    ({"nodeSelector": {"a": "b"}}, "nodeSelector"),
    ({"tolerations": []}, "tolerations"),
    ({"serviceAccountName": "x"}, "serviceAccountName"),
])
def test_pod_overrides_still_cannot_set_the_placement_keys(overlay, key) -> None:
    """Node selector, tolerations and the service account are typed template fields now, written by the manifest builder.
    ``pod_overrides`` stays an allowlist and still refuses them, so a user-tier template cannot reach the admin-only two
    through the free-form overlay."""
    with pytest.raises(ConfigError, match=key):
        _validate_template_overrides(_template(pod_overrides=overlay))


def test_the_typed_placement_fields_pass_the_overlay_check() -> None:
    from primer.model.workspace import K8sGpu, K8sToleration

    _validate_template_overrides(_template(
        node_selector={"gpu": "true"},
        tolerations=[K8sToleration(key="gpu", operator="Exists", effect="NoSchedule")],
        gpu=K8sGpu(count=1), service_account_name="runner", image_pull_policy="Always",
    ))
