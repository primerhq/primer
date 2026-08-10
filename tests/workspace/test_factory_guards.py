"""Missing-extra guards on WorkspaceBackendFactory (modular-monolith spec).

Absence is simulated at the has_extra seam (primer.common.optional._find_spec)
so the test passes identically on full and core-only installs.
"""

from __future__ import annotations

import pytest

import primer.common.optional as optional_mod
from primer.model.except_ import ConfigError
from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerWorkspaceConfig,
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
    KubernetesWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
)
from primer.workspace.factory import WorkspaceBackendFactory


def _container_config() -> ContainerWorkspaceConfig:
    return ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(
            socket_path="/var/run/docker.sock",
        ),
        reachability=ContainerReachabilityHostPort(bind_host="127.0.0.1"),
    )


def _kubernetes_config() -> KubernetesWorkspaceConfig:
    return KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(),
        namespace="primer",
        reachability=K8sReachabilityInCluster(),
    )


def _provider(provider_type, config) -> WorkspaceProvider:
    return WorkspaceProvider(
        id="wsp-guard-test",
        description="guard test provider",
        provider=provider_type,
        config=config,
    )


@pytest.mark.parametrize(
    ("provider_type", "config_factory", "extra"),
    [
        (WorkspaceProviderType.CONTAINER, _container_config, "docker"),
        (WorkspaceProviderType.KUBERNETES, _kubernetes_config, "kubernetes"),
    ],
)
def test_backend_factory_raises_configerror_when_extra_missing(
    monkeypatch: pytest.MonkeyPatch,
    provider_type: WorkspaceProviderType,
    config_factory,
    extra: str,
) -> None:
    """A missing backend SDK must surface as the standard ConfigError.

    Before this guard the factory imported the backend module directly, so
    an operator without the extra got a raw ModuleNotFoundError from deep
    inside the import, naming a third-party module rather than the extra
    that would fix it.
    """
    monkeypatch.setattr(optional_mod, "_find_spec", lambda name: None)
    with pytest.raises(ConfigError) as exc_info:
        WorkspaceBackendFactory.create(_provider(provider_type, config_factory()))
    msg = str(exc_info.value)
    assert f"'{extra}' extra" in msg
    assert f"pip install 'primer-ai[{extra}]'" in msg


def test_backend_factory_local_needs_no_extra(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The local backend is core; it must not be gated by any extra.

    Guards this pair from over-reach: a require_extra accidentally applied
    to the shared path would break the zero-config install everything else
    is measured against.
    """
    from primer.model.workspace import LocalWorkspaceConfig

    monkeypatch.setattr(optional_mod, "_find_spec", lambda name: None)
    backend = WorkspaceBackendFactory.create(
        _provider(
            WorkspaceProviderType.LOCAL,
            LocalWorkspaceConfig(root_path=str(tmp_path)),
        ),
    )
    assert backend is not None
