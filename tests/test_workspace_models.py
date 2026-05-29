"""Tests for primer.model.workspace."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr, ValidationError

from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerNetworkConfig,
    ContainerReachabilityBridge,
    ContainerReachabilityHostPort,
    ContainerTemplateConfig,
    ContainerWorkspaceConfig,
    FileEntry,
    FileMount,
    K8sConnectionInCluster,
    K8sConnectionKubeconfig,
    K8sConnectionServiceAccountToken,
    K8sReachabilityIngress,
    K8sReachabilityInCluster,
    K8sVolume,
    K8sVolumeMount,
    KubernetesTemplateConfig,
    KubernetesWorkspaceConfig,
    LocalWorkspaceConfig,
    ResourceLimits,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)


# ---- FileMount + FileSource discriminated union --------------------------


class TestFileSourceUnion:
    def test_inline_source_round_trip(self) -> None:
        fm = FileMount(
            path="hello.txt",
            source={"kind": "inline", "content": "hi there"},
        )
        assert fm.source.kind == "inline"
        assert fm.source.content == "hi there"

        dumped = fm.model_dump()
        parsed = FileMount.model_validate(dumped)
        assert parsed == fm

    def test_url_source_round_trip(self) -> None:
        fm = FileMount(
            path="vendored.tar.gz",
            source={
                "kind": "url",
                "url": "https://example.test/vendored.tar.gz",
                "sha256": "abc123",
            },
        )
        assert fm.source.kind == "url"
        assert fm.source.sha256 == "abc123"

        parsed = FileMount.model_validate(fm.model_dump(mode="json"))
        assert parsed.source.kind == "url"

    def test_document_source_round_trip(self) -> None:
        fm = FileMount(
            path="config/main.yaml",
            source={
                "kind": "document",
                "collection_id": "configs",
                "document_id": "main",
            },
        )
        assert fm.source.kind == "document"
        assert fm.source.collection_id == "configs"
        assert fm.source.document_id == "main"

    def test_secret_source_round_trip(self) -> None:
        fm = FileMount(
            path=".env",
            source={"kind": "secret", "name": "OPENAI_API_KEY"},
        )
        assert fm.source.kind == "secret"
        assert fm.source.name == "OPENAI_API_KEY"

    def test_unknown_kind_rejected_by_discriminator(self) -> None:
        with pytest.raises(ValidationError):
            FileMount(
                path="x",
                source={"kind": "carrier-pigeon", "address": "..."},  # type: ignore[arg-type]
            )

    def test_inline_source_missing_content_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FileMount(path="x", source={"kind": "inline"})  # type: ignore[arg-type]

    def test_path_required_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            FileMount(path="", source={"kind": "inline", "content": ""})


# ---- ResourceLimits ------------------------------------------------------


class TestResourceLimits:
    def test_defaults_are_unbounded_with_egress_network(self) -> None:
        r = ResourceLimits()
        assert r.cpu_cores is None
        assert r.memory_bytes is None
        assert r.disk_bytes is None
        assert r.network == "egress"

    def test_explicit_bounds(self) -> None:
        r = ResourceLimits(
            cpu_cores=2.5,
            memory_bytes=2 * 1024**3,
            network="none",
            disk_bytes=10 * 1024**3,
        )
        assert r.cpu_cores == 2.5
        assert r.network == "none"

    def test_unknown_network_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ResourceLimits(network="vpn")  # type: ignore[arg-type]


# ---- WorkspaceTemplate ---------------------------------------------------


class TestWorkspaceTemplate:
    def test_minimal_template_uses_defaults(self) -> None:
        tpl = WorkspaceTemplate(
            id="python-basic",
            description="Python 3.13 with git installed",
            provider_id="local-1",
        )
        assert tpl.id == "python-basic"
        assert tpl.description == "Python 3.13 with git installed"
        assert tpl.provider_id == "local-1"
        assert tpl.files == []
        assert tpl.env == {}
        assert tpl.init_commands == []
        assert tpl.state_path == ".state"
        assert tpl.tmp_path == ".tmp"
        assert isinstance(tpl.resources, ResourceLimits)

    def test_full_template(self) -> None:
        tpl = WorkspaceTemplate(
            id="python-research",
            description="Research workstation with science stack",
            provider_id="local-1",
            files=[
                FileMount(
                    path="README.md",
                    source={"kind": "inline", "content": "# Research"},
                )
            ],
            env={"OPENAI_API_KEY": "redacted-secret"},
            init_commands=["python --version"],
            state_path=".primer/state",
            tmp_path=".primer/tmp",
            resources=ResourceLimits(cpu_cores=4, memory_bytes=8 * 1024**3),
        )
        assert tpl.files[0].source.kind == "inline"
        assert tpl.env["OPENAI_API_KEY"].get_secret_value() == "redacted-secret"
        assert tpl.state_path == ".primer/state"
        assert tpl.resources.cpu_cores == 4

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceTemplate(
                id="",
                description="x",
                provider_id="local-1",
            )

    def test_empty_provider_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceTemplate(
                id="x",
                description="x",
                provider_id="",
            )

    def test_state_path_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceTemplate(
                id="x",
                description="x",
                provider_id="local-1",
                state_path="",
            )

    def test_env_value_is_secret_str(self) -> None:
        tpl = WorkspaceTemplate(
            id="x",
            description="x",
            provider_id="local-1",
            env={"K": "V"},
        )
        assert isinstance(tpl.env["K"], SecretStr)
        assert tpl.env["K"].get_secret_value() == "V"

    def test_provider_id_round_trips_through_json(self) -> None:
        tpl = WorkspaceTemplate(
            id="x",
            description="x",
            provider_id="local-1",
        )
        round_tripped = WorkspaceTemplate.model_validate_json(tpl.model_dump_json())
        assert round_tripped.provider_id == "local-1"


# ---- WorkspaceTemplateOverrides -----------------------------------------


class TestWorkspaceTemplateOverrides:
    def test_all_defaults_empty(self) -> None:
        ov = WorkspaceTemplateOverrides()
        assert ov.env == {}
        assert ov.files == []
        assert ov.init_commands == []

    def test_with_overrides(self) -> None:
        ov = WorkspaceTemplateOverrides(
            env={"EXTRA": "1"},
            files=[
                FileMount(
                    path="patch.txt",
                    source={"kind": "inline", "content": "patched"},
                )
            ],
            init_commands=["echo applied"],
        )
        assert ov.env["EXTRA"].get_secret_value() == "1"
        assert ov.files[0].path == "patch.txt"
        assert ov.init_commands == ["echo applied"]


# ---- LocalWorkspaceConfig + WorkspaceProvider ---------------------------


class TestLocalWorkspaceConfig:
    def test_minimal(self):
        cfg = LocalWorkspaceConfig(root_path="~/.primer/workspaces")
        assert cfg.root_path == "~/.primer/workspaces"

    def test_default_root_path(self):
        cfg = LocalWorkspaceConfig()
        assert cfg.root_path == "~/.primer/workspaces"

    def test_rejects_template_fields(self):
        with pytest.raises(ValidationError):
            LocalWorkspaceConfig(
                root_path="~/.primer/workspaces",
                workdir_default="/tmp/x",  # type: ignore[call-arg]
            )


class TestWorkspaceProvider:
    def test_minimal_local(self) -> None:
        wp = WorkspaceProvider(
            id="local-1",
            provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(root_path="/tmp/primer/workspaces"),
        )
        assert wp.id == "local-1"
        assert wp.provider == WorkspaceProviderType.LOCAL
        assert wp.config.root_path == "/tmp/primer/workspaces"

    def test_round_trip_through_json(self) -> None:
        wp = WorkspaceProvider(
            id="local-1",
            provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(root_path="/srv/primer"),
        )
        parsed = WorkspaceProvider.model_validate_json(wp.model_dump_json())
        assert parsed == wp

    def test_unknown_provider_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceProvider(
                id="x",
                provider="kubernetes",  # type: ignore[arg-type]
                config=LocalWorkspaceConfig(root_path="/x"),
            )

    def test_provider_type_enum_values(self) -> None:
        assert WorkspaceProviderType.LOCAL.value == "local"
        assert WorkspaceProviderType.CONTAINER.value == "container"
        assert WorkspaceProviderType.KUBERNETES.value == "kubernetes"
        assert {t.value for t in WorkspaceProviderType} == {
            "local", "container", "kubernetes",
        }

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceProvider(
                id="",
                provider=WorkspaceProviderType.LOCAL,
                config=LocalWorkspaceConfig(root_path="/x"),
            )


# ---- FileEntry -----------------------------------------------------------


class TestFileEntry:
    def test_construction(self) -> None:
        fe = FileEntry(
            path="src/main.py",
            kind="file",
            size_bytes=2048,
            modified_at=datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert fe.path == "src/main.py"
        assert fe.kind == "file"
        assert fe.size_bytes == 2048
        assert fe.modified_at.year == 2026

    def test_dir_kind_with_zero_size(self) -> None:
        fe = FileEntry(
            path="src",
            kind="dir",
            size_bytes=0,
            modified_at=datetime.now(timezone.utc),
        )
        assert fe.kind == "dir"

    def test_negative_size_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FileEntry(
                path="x",
                kind="file",
                size_bytes=-1,
                modified_at=datetime.now(timezone.utc),
            )

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FileEntry(
                path="x",
                kind="socket",  # type: ignore[arg-type]
                size_bytes=0,
                modified_at=datetime.now(timezone.utc),
            )


# ---- ContainerWorkspaceConfig (minimal: connection + reachability) -------


def test_container_config_host_port_reachability():
    cfg = ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(
            socket_path="/var/run/docker.sock",
        ),
        reachability=ContainerReachabilityHostPort(
            bind_host="127.0.0.1",
        ),
    )
    assert cfg.reachability.kind == "host_port"
    assert cfg.reachability.bind_host == "127.0.0.1"


def test_container_config_bridge_network_reachability():
    cfg = ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(
            socket_path="/var/run/docker.sock",
        ),
        reachability=ContainerReachabilityBridge(
            network_name="primer-net",
        ),
    )
    assert cfg.reachability.kind == "bridge_network"
    assert cfg.reachability.network_name == "primer-net"


def test_container_config_rejects_template_fields():
    # image / entrypoint / cpu / memory / mounts moved to template;
    # passing them on provider config is a validation error.
    with pytest.raises(ValidationError):
        ContainerWorkspaceConfig(
            runtime="docker",
            connection=ContainerConnectionSocket(
                socket_path="/var/run/docker.sock",
            ),
            reachability=ContainerReachabilityHostPort(
                bind_host="127.0.0.1",
            ),
            image="ghcr.io/example/img:1",  # type: ignore[call-arg]
        )


# ---- KubernetesWorkspaceConfig (minimal: connection + reachability) ------


class TestKubernetesWorkspaceConfig:
    def test_in_cluster_minimal(self):
        cfg = KubernetesWorkspaceConfig(
            connection=K8sConnectionInCluster(),
            namespace="primer",
            reachability=K8sReachabilityInCluster(),
        )
        assert cfg.connection.kind == "in_cluster"
        assert cfg.reachability.kind == "in_cluster"
        assert cfg.variant == "system"  # default

    def test_kubeconfig_and_ingress(self):
        cfg = KubernetesWorkspaceConfig(
            connection=K8sConnectionKubeconfig(path="~/.kube/config", context="prod"),
            namespace="primer",
            reachability=K8sReachabilityIngress(
                url_template="wss://workspaces.example.com/{workspace_id}/"
            ),
        )
        assert cfg.connection.path == "~/.kube/config"
        assert cfg.connection.context == "prod"
        assert cfg.reachability.url_template.startswith("wss://")

    def test_service_account_token_with_secret(self):
        cfg = KubernetesWorkspaceConfig(
            connection=K8sConnectionServiceAccountToken(
                apiserver_url="https://1.2.3.4:6443",
                ca_data="-----BEGIN CERTIFICATE-----\n...",
                token="bearer-token-here",  # SecretStr coerced
                namespace="default",
            ),
            namespace="primer",
            reachability=K8sReachabilityInCluster(),
        )
        assert cfg.connection.kind == "service_account_token"
        assert cfg.connection.apiserver_url == "https://1.2.3.4:6443"
        # Token should be SecretStr — its repr shouldn't leak
        assert "bearer-token-here" not in repr(cfg.connection)

    def test_agent_sandbox_variant_reserved(self):
        cfg = KubernetesWorkspaceConfig(
            variant="agent_sandbox",
            connection=K8sConnectionInCluster(),
            namespace="primer",
            reachability=K8sReachabilityInCluster(),
        )
        assert cfg.variant == "agent_sandbox"

    def test_rejects_template_fields(self):
        # storage_class / image_pull_policy / security_context defaults moved to template
        with pytest.raises(ValidationError):
            KubernetesWorkspaceConfig(
                connection=K8sConnectionInCluster(),
                namespace="primer",
                reachability=K8sReachabilityInCluster(),
                storage_class="fast-ssd",  # type: ignore[call-arg]
            )


# ---- ContainerTemplateConfig --------------------------------------------


class TestContainerTemplateConfig:
    def test_minimum_image_required(self):
        with pytest.raises(ValidationError):
            ContainerTemplateConfig()  # image is required

    def test_full_shape(self):
        tpl = ContainerTemplateConfig(
            image="ghcr.io/example/primer-runtime:1",
            cpu_cores=2.0,
            memory_bytes=4 * 1024**3,
            network=ContainerNetworkConfig(egress="deny_all"),
        )
        assert tpl.image == "ghcr.io/example/primer-runtime:1"
        assert tpl.cpu_cores == 2.0
        assert tpl.network.egress == "deny_all"

    def test_network_egress_optional(self):
        tpl = ContainerTemplateConfig(image="x:1")
        assert tpl.network is None
        assert tpl.cpu_cores is None
        assert tpl.memory_bytes is None


# ---- KubernetesTemplateConfig -------------------------------------------


class TestKubernetesTemplateConfig:
    def test_minimum_image_required(self):
        with pytest.raises(ValidationError):
            KubernetesTemplateConfig()

    def test_resource_requests_and_limits(self):
        tpl = KubernetesTemplateConfig(
            image="ghcr.io/example/primer-runtime:1",
            cpu_request="500m", cpu_limit="2",
            memory_request="1Gi", memory_limit="4Gi",
        )
        assert tpl.cpu_request == "500m"
        assert tpl.cpu_limit == "2"
        assert tpl.memory_request == "1Gi"
        assert tpl.memory_limit == "4Gi"

    def test_storage_defaults(self):
        tpl = KubernetesTemplateConfig(image="x:1")
        assert tpl.pvc_size == "10Gi"
        assert tpl.pvc_access_modes == ["ReadWriteOnce"]
        assert tpl.storage_class is None

    def test_pod_overrides_freeform(self):
        tpl = KubernetesTemplateConfig(
            image="x:1",
            pod_overrides={"nodeSelector": {"role": "agent"}},
        )
        assert tpl.pod_overrides == {"nodeSelector": {"role": "agent"}}


# ---- WorkspaceTemplate has no packages field ----------------------------


class TestWorkspaceTemplateNoPackages:
    def test_no_packages_field(self):
        from primer.model.workspace import WorkspaceTemplate
        fields = WorkspaceTemplate.model_fields
        assert "packages" not in fields, "packages dropped per redesign §6.1"

    def test_still_has_files_env_init(self):
        from primer.model.workspace import WorkspaceTemplate
        fields = WorkspaceTemplate.model_fields
        for f in ("files", "env", "init_commands"):
            assert f in fields


# ---- Legacy migration on read -------------------------------------------


class TestWorkspaceTemplateLegacyMigration:
    def test_legacy_packages_dropped_with_warning(self, caplog):
        import logging
        from primer.model.workspace import WorkspaceTemplate

        # Adapted from the task spec to match the actual model shape:
        # WorkspaceTemplate doesn't have top-level `kind`/`image` — those
        # live inside the `backend` discriminated-union field. The legacy
        # bit being exercised here is `packages`, which the redesign drops.
        raw = {
            "id": "legacy-1",
            "description": "old template",
            "provider_id": "prov-1",
            "backend": {"kind": "container", "image": "x:1"},
            "files": [],
            "env": {},
            "init_commands": [],
            "packages": [{"kind": "pip", "name": "requests"}],  # legacy
        }
        with caplog.at_level(logging.WARNING, logger="primer.model.workspace"):
            tpl = WorkspaceTemplate.model_validate(raw)
        # The model dropped packages on read; it's not in the resulting object.
        assert "packages" not in tpl.model_dump()
        # And the loss was logged as WARNING.
        assert "packages" in caplog.text.lower()
        # Template otherwise validates fine.
        assert tpl.id == "legacy-1"


class TestWorkspaceRuntimeMeta:
    def test_minimal(self):
        from primer.model.workspace import WorkspaceRuntimeMeta
        m = WorkspaceRuntimeMeta(url="ws://workspace-x:5959/", token=SecretStr("s"))
        assert m.url.startswith("ws://")
        assert m.mapped_host_port is None
        assert m.k8s_object_name is None

    def test_host_port(self):
        from primer.model.workspace import WorkspaceRuntimeMeta
        m = WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:32100/",
            token=SecretStr("s"),
            mapped_host_port=32100,
        )
        assert m.mapped_host_port == 32100

    def test_k8s_object_name(self):
        from primer.model.workspace import WorkspaceRuntimeMeta
        m = WorkspaceRuntimeMeta(
            url="ws://primer-ws-abc-0.primer-ws-abc.ns.svc.cluster.local:5959/",
            token=SecretStr("s"),
            k8s_object_name="primer-ws-abc",
        )
        assert m.k8s_object_name == "primer-ws-abc"


class TestWorkspaceLifecycleFields:
    def test_phase_defaults_to_pending(self):
        from datetime import datetime, timezone
        from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
        w = Workspace(
            id="ws-1", description=None, template_id="tpl-1", provider_id="prov-1",
            created_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
            runtime_meta=WorkspaceRuntimeMeta(
                url="ws://x:5959/", token=SecretStr("s"),
            ),
        )
        assert w.phase == "pending"
        assert w.last_probe_at is None
        assert w.last_probe_ok is False
        assert w.failure_reason is None

    def test_phase_terminating(self):
        from datetime import datetime, timezone
        from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
        w = Workspace(
            id="ws-1", description=None, template_id="tpl-1", provider_id="prov-1",
            created_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
            runtime_meta=WorkspaceRuntimeMeta(
                url="ws://x:5959/", token=SecretStr("s"),
            ),
            phase="terminating",
        )
        assert w.phase == "terminating"

    def test_workspace_model_dump_includes_phase_fields(self):
        """Locks the GET /v1/workspaces/{id} response contract: phase +
        probe fields ship in the JSON payload (the router uses the bare
        Workspace model as response_model, so model_dump shape is the
        wire shape)."""
        from datetime import datetime, timezone
        from primer.model.workspace import Workspace, WorkspaceRuntimeMeta

        w = Workspace(
            id="ws-1",
            template_id="tpl",
            provider_id="prov",
            created_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
            runtime_meta=WorkspaceRuntimeMeta(
                url="ws://x:5959/", token=SecretStr("s"),
            ),
        )
        dumped = w.model_dump(mode="json")
        assert "phase" in dumped
        assert "last_probe_at" in dumped
        assert "last_probe_ok" in dumped
        assert "failure_reason" in dumped
        assert "runtime_meta" in dumped
        assert dumped["phase"] == "pending"
        assert dumped["last_probe_at"] is None
        assert dumped["last_probe_ok"] is False
        assert dumped["failure_reason"] is None
