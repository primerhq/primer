"""The k8s backend's ``destroy`` closes the workspace's runtime client, bounded, before it deletes the pod.

The comment said "close the runtime WS first so reconnect attempts don't fire while we tear the Pod down underneath them", but
the call was the workspace's own ``aclose``, which only ends its sessions: the connection is the sandbox's ``RuntimeClient``
and nothing closed it, so the pod went away under a client that kept reconnecting (only a 404 handshake makes it give up; a
cluster-internal service that is gone answers with a DNS or connection error instead). Both closes also waited without a bound
for a peer that may be silent, in front of the deletion of the StatefulSet.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.workspace import base_backend
from primer.workspace.k8s import backend as k8s_backend
from tests.workspace.test_k8s_client_is_closed_when_the_build_does_not_finish import _backend, _Client, _create


@pytest.fixture
def client(monkeypatch) -> _Client:
    c = _Client()
    monkeypatch.setattr(k8s_backend, "RuntimeClient", lambda **kwargs: c)
    return c


async def test_destroy_closes_the_runtime_client_of_a_cached_workspace(client):
    backend = _backend()
    await _create(backend)
    await backend.destroy("ws-1")
    assert client.closed == 1 and backend._workspaces == {}
    backend._apps_v1.delete_namespaced_stateful_set.assert_awaited_once()


async def test_the_client_is_closed_before_the_pod_is_deleted_under_it(client):
    backend = _backend()
    await _create(backend)
    closed_when_the_statefulset_was_deleted = []

    async def delete_statefulset(**kwargs):
        closed_when_the_statefulset_was_deleted.append(client.closed)

    backend._apps_v1.delete_namespaced_stateful_set.side_effect = delete_statefulset
    await backend.destroy("ws-1")
    assert closed_when_the_statefulset_was_deleted == [1]


async def test_a_close_that_hangs_does_not_hold_up_the_deletion(monkeypatch):
    monkeypatch.setattr(base_backend, "_CLOSE_WAIT_S", 0.05)
    close_gate = asyncio.Event()
    hanging = _Client(close_gate=close_gate)
    monkeypatch.setattr(k8s_backend, "RuntimeClient", lambda **kwargs: hanging)
    backend = _backend()
    await _create(backend)
    try:
        await asyncio.wait_for(backend.destroy("ws-1"), timeout=2)
        assert hanging.close_started == 1 and hanging.closed == 0
        backend._apps_v1.delete_namespaced_stateful_set.assert_awaited_once()
    finally:
        close_gate.set()
        await asyncio.sleep(0.05)


async def test_sessions_that_cannot_be_ended_do_not_hold_up_the_deletion_or_leave_the_client_open(monkeypatch, client):
    """The workspace's own ``aclose`` ends its sessions over the connection; on a connection that no longer answers that
    waits for a reconnect that never comes."""
    monkeypatch.setattr(base_backend, "_CLOSE_WAIT_S", 0.05)
    backend = _backend()
    ws = await _create(backend)
    ending, ending_gate = asyncio.Event(), asyncio.Event()

    async def end_sessions_on_a_dead_connection():
        ending.set()
        await ending_gate.wait()

    ws.aclose = end_sessions_on_a_dead_connection
    try:
        await asyncio.wait_for(backend.destroy("ws-1"), timeout=2)
        assert ending.is_set() and client.closed == 1
        backend._apps_v1.delete_namespaced_stateful_set.assert_awaited_once()
    finally:
        ending_gate.set()
        await asyncio.sleep(0.05)


async def test_destroy_of_a_workspace_that_is_not_cached_opens_no_connection(client):
    backend = _backend()
    await backend.destroy("ws-1")
    assert not client.connected and client.close_started == 0
    backend._apps_v1.delete_namespaced_stateful_set.assert_awaited_once()
