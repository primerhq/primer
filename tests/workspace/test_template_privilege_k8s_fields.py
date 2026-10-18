"""The Kubernetes template fields only an admin may write (platform revamp 2026-10, plan 08 task 3, design 14.2).

``service_account_name`` and ``tolerations`` join the fields ``primer.workspace.template_privilege`` gates: a service account
puts its token and permissions in the workspace pod, and a toleration lets the pod run on nodes the cluster keeps for its own
workloads. ``node_selector``, ``gpu`` and ``image_pull_policy`` do neither and stay user-tier. The REST and tool behaviour is
pinned in ``tests/api/test_workspace_template_privilege.py`` and ``tests/toolset/test_workspaces_template_privilege.py``;
this file pins the module's own contract.
"""

from __future__ import annotations

from primer.model.workspace import K8sGpu, K8sToleration, KubernetesTemplateConfig, WorkspaceTemplate
from primer.workspace import template_privilege
from primer.workspace.template_privilege import (
    admin_only_settings_held,
    admin_only_template_changes,
    refusal_message,
)


def _t(**backend) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="t", provider_id="p", description="d", backend=KubernetesTemplateConfig(image="rt:1", **backend),
    )


def test_the_admin_only_kubernetes_fields_are_exactly_these():
    """The console form gates the same list (``WT_K8S_ADMIN_FIELDS``), so a change here is a change there."""
    assert template_privilege._K8S_FIELDS == (
        "extra_volumes", "extra_volume_mounts", "pod_overrides", "container_security_context_overrides",
        "service_account_name", "tolerations",
    )


def test_a_service_account_and_tolerations_are_admin_only_when_set():
    t = _t(service_account_name="runner", tolerations=[K8sToleration(key="gpu", operator="Exists")])

    assert admin_only_template_changes(t, None) == ["backend.service_account_name", "backend.tolerations"]
    assert admin_only_settings_held(t) == ["backend.service_account_name", "backend.tolerations"]


def test_the_user_tier_placement_fields_are_not_admin_only():
    t = _t(node_selector={"gpu": "true"}, gpu=K8sGpu(count=1), image_pull_policy="Always")

    assert admin_only_template_changes(t, None) == []
    assert admin_only_settings_held(t) == []


def test_changing_or_clearing_a_service_account_is_a_change():
    before = _t(service_account_name="a")

    assert admin_only_template_changes(_t(service_account_name="b"), before) == ["backend.service_account_name"]
    assert admin_only_template_changes(_t(), before) == ["backend.service_account_name"]
    assert admin_only_template_changes(before, before) == []


def test_tolerations_compare_by_content_and_an_empty_list_is_no_value():
    one = [K8sToleration(key="gpu", operator="Exists", effect="NoSchedule")]
    stored = _t(tolerations=one)

    assert admin_only_template_changes(_t(tolerations=list(one)), stored) == []
    assert admin_only_template_changes(_t(tolerations=[K8sToleration(key="gpu", operator="Exists")]), stored) == [
        "backend.tolerations",
    ]
    assert admin_only_template_changes(_t(tolerations=[]), stored) == ["backend.tolerations"]
    assert admin_only_template_changes(_t(tolerations=[]), _t()) == []
    assert admin_only_settings_held(_t(tolerations=[])) == []


def test_a_user_tier_edit_beside_an_admin_held_field_is_not_a_change_to_it():
    stored = _t(service_account_name="runner", node_selector={"a": "b"})

    assert admin_only_template_changes(_t(service_account_name="runner", node_selector={"a": "c"}), stored) == []


def test_the_refusal_names_the_two_new_kinds_of_field():
    message = refusal_message(["backend.tolerations"])

    assert "service accounts" in message and "tolerations" in message
    assert "host mounts" in message and "secret file sources" in message
