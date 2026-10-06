"""A k8s ``create`` that is cancelled, times out or fails after it made cluster objects deletes the objects IT made.

Only an HTTPRoute failure rolled anything back. A cancel or a caller's timeout (the pod wait alone is up to two minutes) or a
plain failure after the first object (a StatefulSet the API refused, a pod that never ran, a ``materialise`` that failed) left the
Secret, the Service, the StatefulSet and its PVC behind, with nothing pointing at them and nothing to reclaim them (there is no
orphan sweep, and a caller that did not pin the workspace id never learns the generated one).

What it must NOT do is delete what it did not make: a ``create`` with the id of a live workspace gets a 409 from the Secret create,
and the rollback must leave that workspace's objects alone.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from primer.model.except_ import ConfigError
from primer.model.workspace import (
    K8sGatewayParentRef,
    K8sGatewayRoutingHostname,
    K8sReachabilityGateway,
)
from primer.workspace import base_backend
from primer.workspace.k8s import backend as k8s_backend
from primer.workspace.k8s.naming import k8s_object_name
from tests.workspace.test_k8s_client_is_closed_when_the_build_does_not_finish import (
    TEMPLATE,
    _backend,
    _Client,
    _create,
    _materialise_blocks,
    _materialise_raises,
)

OBJ = k8s_object_name("ws-1")
NS = "primer-ns"


@pytest.fixture
def client(monkeypatch) -> _Client:
    c = _Client()
    monkeypatch.setattr(k8s_backend, "RuntimeClient", lambda **kwargs: c)
    return c


def _deleted(backend) -> dict[str, int]:
    """How many times each kind of object was deleted."""
    return {
        "statefulset": backend._apps_v1.delete_namespaced_stateful_set.await_count,
        "pvc": backend._core_v1.delete_namespaced_persistent_volume_claim.await_count,
        "service": backend._core_v1.delete_namespaced_service.await_count,
        "secret": backend._core_v1.delete_namespaced_secret.await_count,
    }


EVERYTHING = {"statefulset": 1, "pvc": 1, "service": 1, "secret": 1}
NOTHING = {"statefulset": 0, "pvc": 0, "service": 0, "secret": 0}


def _pod_wait_blocks(backend) -> asyncio.Event:
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    backend._wait_for_pod_running = blocked
    return entered


async def test_a_cancel_while_waiting_for_the_pod_deletes_every_object_it_made(client):
    backend = _backend()
    entered = _pod_wait_blocks(backend)
    task = asyncio.create_task(_create(backend))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert _deleted(backend) == EVERYTHING
    backend._apps_v1.delete_namespaced_stateful_set.assert_awaited_once_with(name=OBJ, namespace=NS)
    backend._core_v1.delete_namespaced_service.assert_awaited_once_with(name=OBJ, namespace=NS)
    backend._core_v1.delete_namespaced_secret.assert_awaited_once_with(name=OBJ, namespace=NS)
    assert not client.connected  # the runtime connection was never opened


async def test_a_timeout_while_waiting_for_the_pod_deletes_them_too(client):
    backend = _backend()
    _pod_wait_blocks(backend)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await _create(backend)
    assert _deleted(backend) == EVERYTHING


async def test_a_cancel_after_the_pod_runs_deletes_the_objects_and_closes_the_client(monkeypatch, client):
    backend = _backend()
    entered = _materialise_blocks(monkeypatch)
    task = asyncio.create_task(_create(backend))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert _deleted(backend) == EVERYTHING
    assert client.closed == 1 and backend._workspaces == {}


async def test_a_plain_failure_after_the_pod_runs_is_rolled_back_too(monkeypatch, client):
    backend = _backend()
    _materialise_raises(monkeypatch, ConfigError("init command failed (rc=1): 'false'"))
    with pytest.raises(ConfigError, match="init command failed"):
        await _create(backend)
    assert _deleted(backend) == EVERYTHING and client.closed == 1


async def test_a_statefulset_the_api_refuses_rolls_back_the_secret_and_the_service_only(client):
    backend = _backend()
    backend._apps_v1.create_namespaced_stateful_set.side_effect = Exception("(422) the StatefulSet is invalid")
    with pytest.raises(Exception, match="StatefulSet is invalid"):
        await _create(backend)
    assert _deleted(backend) == {"statefulset": 0, "pvc": 0, "service": 1, "secret": 1}


async def test_a_name_that_a_live_workspace_holds_is_left_alone(client):
    """The Secret create answers 409: this create made nothing, so its rollback deletes nothing."""
    backend = _backend()
    backend._core_v1.create_namespaced_secret.side_effect = Exception("(409) secrets 'ws-1' already exists")
    with pytest.raises(Exception, match="already exists"):
        await _create(backend)
    assert _deleted(backend) == NOTHING


async def test_a_cancel_while_the_secret_request_is_in_flight_still_deletes_it(client):
    """The request may have landed server side; the API did not refuse it, so it is rolled back (and nothing else is)."""
    backend = _backend()
    entered = asyncio.Event()

    async def secret_request_in_flight(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    backend._core_v1.create_namespaced_secret.side_effect = secret_request_in_flight
    task = asyncio.create_task(_create(backend))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert _deleted(backend) == {"statefulset": 0, "pvc": 0, "service": 0, "secret": 1}


async def test_an_httproute_made_by_the_create_is_deleted_with_the_rest(client):
    backend = _backend()
    backend._config = backend._config.model_copy(update={
        "reachability": K8sReachabilityGateway(
            gateway=K8sGatewayParentRef(name="primer-gw"),
            routing=K8sGatewayRoutingHostname(hostname_template="{workspace_id}.ws.local"),
            external_port=32045,
        ),
    })
    backend._custom_objects = AsyncMock()
    _pod_wait_blocks(backend)
    task = asyncio.create_task(_create(backend))
    for _ in range(500):
        if backend._custom_objects.create_namespaced_custom_object.await_count:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    backend._custom_objects.delete_namespaced_custom_object.assert_awaited_once()
    assert _deleted(backend) == EVERYTHING


async def test_a_failing_delete_neither_masks_the_error_nor_skips_the_next_one(monkeypatch, client):
    backend = _backend()
    backend._apps_v1.delete_namespaced_stateful_set.side_effect = Exception("(500) the API server fell over")
    _materialise_raises(monkeypatch, ConfigError("materialise failed"))
    with pytest.raises(ConfigError, match="materialise failed"):
        await _create(backend)
    assert _deleted(backend) == EVERYTHING  # the StatefulSet delete failed (and was awaited), the rest went on


async def test_a_second_cancel_during_the_rollback_does_not_abandon_it(client):
    backend = _backend()
    entered = _pod_wait_blocks(backend)
    delete_gate = asyncio.Event()
    delete_started = asyncio.Event()

    async def statefulset_delete_in_flight(**kwargs):
        delete_started.set()
        await delete_gate.wait()

    backend._apps_v1.delete_namespaced_stateful_set.side_effect = statefulset_delete_in_flight
    task = asyncio.create_task(_create(backend))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        task.cancel()
        await asyncio.wait_for(delete_started.wait(), timeout=5)
        task.cancel()  # a drain, a second bound
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        delete_gate.set()
    for _ in range(500):
        if _deleted(backend) == EVERYTHING:
            break
        await asyncio.sleep(0.01)
    assert _deleted(backend) == EVERYTHING


async def test_a_rollback_that_hangs_does_not_hold_the_caller_past_its_bound(monkeypatch, client):
    monkeypatch.setattr(base_backend, "_ROLLBACK_WAIT_S", 0.05)
    backend = _backend()
    delete_gate = asyncio.Event()

    async def statefulset_delete_in_flight(**kwargs):
        await delete_gate.wait()

    backend._apps_v1.delete_namespaced_stateful_set.side_effect = statefulset_delete_in_flight
    _materialise_raises(monkeypatch, ConfigError("materialise failed"))
    try:
        with pytest.raises(ConfigError, match="materialise failed"):
            await asyncio.wait_for(_create(backend), timeout=2)
        assert _deleted(backend)["service"] == 0  # still behind the stuck StatefulSet delete, finishing in the background
    finally:
        delete_gate.set()
    for _ in range(500):
        if _deleted(backend) == EVERYTHING:
            break
        await asyncio.sleep(0.01)
    assert _deleted(backend) == EVERYTHING


async def test_a_create_that_finishes_deletes_nothing(client):
    backend = _backend()
    ws = await _create(backend)
    assert backend._workspaces == {"ws-1": ws} and _deleted(backend) == NOTHING
