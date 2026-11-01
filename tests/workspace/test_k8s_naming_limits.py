"""GI-25: a StatefulSet name past 52 characters never gets its pod, so such ids take the hashed object name; a workspace made under the old name is still found."""

from __future__ import annotations

import pytest

from primer.workspace.k8s.naming import k8s_object_name, legacy_k8s_object_name


def test_ids_up_to_42_characters_keep_their_object_name() -> None:
    for n in (1, 20, 42):
        assert k8s_object_name("a" * n) == "primer-ws-" + "a" * n


@pytest.mark.parametrize("n", [43, 45, 53])
def test_longer_ids_take_the_hashed_name_within_the_statefulset_limit(n) -> None:
    name = k8s_object_name("a" * n)
    assert name != "primer-ws-" + "a" * n and len(name) <= 52 and name.startswith("primer-ws-")


@pytest.mark.parametrize("n,expected", [(42, None), (43, "primer-ws-" + "a" * 43), (53, "primer-ws-" + "a" * 53), (54, None)])
def test_the_legacy_name_is_only_given_to_ids_whose_name_changed(n, expected) -> None:
    assert legacy_k8s_object_name("a" * n) == expected


@pytest.mark.asyncio
async def test_destroy_and_reattach_look_up_the_old_name_when_only_it_exists() -> None:
    from types import SimpleNamespace

    from primer.workspace.k8s.backend import KubernetesWorkspaceBackend

    wid = "a" * 45
    legacy, current = legacy_k8s_object_name(wid), k8s_object_name(wid)
    existing = {legacy}

    class Apps:
        async def read_namespaced_stateful_set(self, name, namespace):
            if name not in existing:
                raise RuntimeError("(404) Not Found")

    backend = KubernetesWorkspaceBackend.__new__(KubernetesWorkspaceBackend)
    backend._apps_v1 = Apps()
    backend._config = SimpleNamespace(namespace="ns")
    assert await backend._object_name_in_cluster(wid) == legacy
    existing.add(current)
    assert await backend._object_name_in_cluster(wid) == current
    existing.clear()
    assert await backend._object_name_in_cluster(wid) == current
