"""Tests for matrix.model.workspace."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr, ValidationError

from matrix.model.workspace import (
    FileEntry,
    FileMount,
    LocalWorkspaceConfig,
    PackageSpec,
    ResourceLimits,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)


# ---- PackageSpec ---------------------------------------------------------


class TestPackageSpec:
    def test_minimal_construction_defaults_version_to_none(self) -> None:
        p = PackageSpec(kind="apt", name="git")
        assert p.kind == "apt"
        assert p.name == "git"
        assert p.version is None

    def test_with_pinned_version(self) -> None:
        p = PackageSpec(kind="pip", name="ruff", version=">=0.6.0")
        assert p.version == ">=0.6.0"

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PackageSpec(kind="brew", name="git")  # type: ignore[arg-type]

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PackageSpec(kind="apt", name="")


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
        assert tpl.packages == []
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
            packages=[
                PackageSpec(kind="apt", name="ripgrep"),
                PackageSpec(kind="pip", name="numpy", version=">=2.0"),
            ],
            files=[
                FileMount(
                    path="README.md",
                    source={"kind": "inline", "content": "# Research"},
                )
            ],
            env={"OPENAI_API_KEY": "redacted-secret"},
            init_commands=["python --version"],
            state_path=".matrix/state",
            tmp_path=".matrix/tmp",
            resources=ResourceLimits(cpu_cores=4, memory_bytes=8 * 1024**3),
        )
        assert len(tpl.packages) == 2
        assert tpl.files[0].source.kind == "inline"
        assert tpl.env["OPENAI_API_KEY"].get_secret_value() == "redacted-secret"
        assert tpl.state_path == ".matrix/state"
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
    def test_construction(self) -> None:
        cfg = LocalWorkspaceConfig(path="/var/lib/matrix/workspaces")
        assert cfg.path == "/var/lib/matrix/workspaces"

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LocalWorkspaceConfig(path="")


class TestWorkspaceProvider:
    def test_minimal_local(self) -> None:
        wp = WorkspaceProvider(
            id="local-1",
            provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(path="/tmp/matrix/workspaces"),
        )
        assert wp.id == "local-1"
        assert wp.provider == WorkspaceProviderType.LOCAL
        assert wp.config.path == "/tmp/matrix/workspaces"

    def test_round_trip_through_json(self) -> None:
        wp = WorkspaceProvider(
            id="local-1",
            provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(path="/srv/matrix"),
        )
        parsed = WorkspaceProvider.model_validate_json(wp.model_dump_json())
        assert parsed == wp

    def test_unknown_provider_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceProvider(
                id="x",
                provider="kubernetes",  # type: ignore[arg-type]
                config=LocalWorkspaceConfig(path="/x"),
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
                config=LocalWorkspaceConfig(path="/x"),
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
