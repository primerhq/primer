"""Smoke test for GET /v1/health."""

from __future__ import annotations

import pytest

from primer.api.version import APP_VERSION


@pytest.mark.asyncio
async def test_health_returns_ok(client) -> None:
    response = await client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == APP_VERSION


@pytest.mark.asyncio
async def test_health_surfaces_scheduler_alive(client, app) -> None:
    """/v1/health includes scheduler.alive + scheduler.metrics (spec §14)."""
    response = await client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert "scheduler" in body
    # The test app wires an InMemoryScheduler in create_test_app, so
    # alive should be True and the metrics dict should carry the
    # spec §14 keys.
    assert body["scheduler"]["alive"] is True
    metrics = body["scheduler"]["metrics"]
    assert "primer_sessions_active" in metrics
    assert "primer_sessions_runnable_queue_depth" in metrics
    assert "primer_scheduler_notify_received_total" in metrics


@pytest.mark.asyncio
async def test_health_scheduler_not_degraded_by_default(client) -> None:
    """A healthy single-process wiring reports degraded=False."""
    response = await client.get("/v1/health")
    body = response.json()
    assert body["scheduler"]["degraded"] is False
    assert body["scheduler"]["degraded_reason"] is None


@pytest.mark.asyncio
async def test_health_surfaces_scheduler_degraded(app, client) -> None:
    """When the wiring flags an unsafe scheduler/runtime-mode combo (e.g.
    in-memory scheduler + external/multi-process worker), /v1/health surfaces
    it via scheduler.degraded + scheduler.degraded_reason."""
    reason = "in-memory scheduler with runtime_mode=worker is not safe"
    app.state.scheduler_degraded_reason = reason
    try:
        response = await client.get("/v1/health")
        body = response.json()
        assert body["scheduler"]["degraded"] is True
        assert body["scheduler"]["degraded_reason"] == reason
    finally:
        app.state.scheduler_degraded_reason = None


@pytest.mark.asyncio
async def test_health_surfaces_worker_pool_in_flight_capacity(client) -> None:
    """/v1/health includes worker_pool.in_flight + worker_pool.capacity.

    The test app does not run a real WorkerPool (worker_pool=None) and
    has no workers registered with the scheduler, so there is no live
    worker to sum a load over: in_flight is null (unknown; "no workers"
    is not "0 busy") and capacity falls back to the durable registry's
    sum — 0 for an empty registry, not null. (A registry WITH reporting
    workers sums their load: the tests below.)
    """
    response = await client.get("/v1/health")
    body = response.json()
    assert "worker_pool" in body
    assert body["worker_pool"]["in_flight"] is None
    assert body["worker_pool"]["capacity"] == 0


@pytest.mark.asyncio
async def test_health_worker_pool_capacity_falls_back_to_scheduler_registry(
    app, client,
) -> None:
    """01a063c3: an API-only process (no local WorkerPool) must not
    report capacity=null just because worker pods, not this pod, own
    the pool — that rendered as the dashboard's literal 'of ? capacity'.
    Capacity instead sums the durable scheduler registry's live
    (non-dead) workers, excluding a dead one."""
    await app.state.scheduler.register_worker(
        worker_id="w1", host="h1", pid=1, capacity=3,
    )
    await app.state.scheduler.register_worker(
        worker_id="w2", host="h2", pid=2, capacity=4,
    )
    await app.state.scheduler.register_worker(
        worker_id="w3", host="h3", pid=3, capacity=99,
    )
    app.state.scheduler.mark_worker_dead_for_test("w3")

    response = await client.get("/v1/health")
    body = response.json()
    # None because no worker has REPORTED its load here (unknown, not idle); the tests below cover a reporting fleet.
    assert body["worker_pool"]["in_flight"] is None
    assert body["worker_pool"]["capacity"] == 7


async def _register_fleet(app) -> None:
    sched = app.state.scheduler
    await sched.register_worker(worker_id="w1", host="h1", pid=1, capacity=3)
    await sched.register_worker(worker_id="w2", host="h2", pid=2, capacity=4)
    await sched.register_worker(worker_id="w3", host="h3", pid=3, capacity=99)


@pytest.mark.asyncio
async def test_health_worker_pool_in_flight_is_the_live_fleets_reported_load(app, client) -> None:
    """Lead sweep M2: an API-only process reported ``in_flight: null`` ("n/a of 6 capacity") although the workers were busy,
    because load was never persisted. Each worker now reports it with its heartbeat; health sums the LIVE workers' reports
    (a dead worker's last report is stale and does not count), the same population its capacity sums."""
    await _register_fleet(app)
    sched = app.state.scheduler
    await sched.report_worker_load("w1", in_flight=2)
    await sched.report_worker_load("w2", in_flight=1)
    await sched.report_worker_load("w3", in_flight=50)
    sched.mark_worker_dead_for_test("w3")

    body = (await client.get("/v1/health")).json()

    assert body["worker_pool"]["in_flight"] == 3
    assert body["worker_pool"]["capacity"] == 7


@pytest.mark.asyncio
async def test_health_worker_pool_in_flight_is_unknown_while_a_live_worker_has_not_reported(app, client) -> None:
    """A worker that predates load reporting (a rolling upgrade) must not be counted as idle: the total would be an
    undercount that looks like a measurement. ``null`` says it is unknown; capacity is still known."""
    await _register_fleet(app)
    sched = app.state.scheduler
    await sched.report_worker_load("w1", in_flight=2)
    await sched.report_worker_load("w3", in_flight=0)
    sched.mark_worker_dead_for_test("w3")

    body = (await client.get("/v1/health")).json()

    assert body["worker_pool"]["in_flight"] is None
    assert body["worker_pool"]["capacity"] == 7


@pytest.mark.asyncio
async def test_health_worker_pool_in_flight_is_zero_for_an_idle_reporting_fleet(app, client) -> None:
    await _register_fleet(app)
    sched = app.state.scheduler
    for wid in ("w1", "w2", "w3"):
        await sched.report_worker_load(wid, in_flight=0)

    body = (await client.get("/v1/health")).json()

    assert body["worker_pool"]["in_flight"] == 0, "an idle fleet is 0 of N, not n/a"


@pytest.mark.asyncio
async def test_health_worker_pool_capacity_null_without_scheduler(app, client) -> None:
    """No local pool and no scheduler at all: capacity has nothing to
    fall back to, so it stays null (the UI renders its own clean
    fallback for that case, not this endpoint's job to fake a number)."""
    real_scheduler = app.state.scheduler
    app.state.scheduler = None
    try:
        response = await client.get("/v1/health")
        body = response.json()
        assert body["worker_pool"]["in_flight"] is None
        assert body["worker_pool"]["capacity"] is None
        # scheduler.alive also reflects the missing scheduler.
        assert body["scheduler"]["alive"] is False
    finally:
        app.state.scheduler = real_scheduler


@pytest.mark.asyncio
async def test_health_surfaces_worker_pool_metrics_when_attached(
    app, client,
) -> None:
    """When app.state.worker_pool is set, /v1/health surfaces its
    in_flight + capacity from the metrics snapshot."""
    from primer.claim.in_memory import InMemoryClaimEngine
    from primer.model.scheduler import WorkerConfig
    from primer.worker.pool import WorkerPool

    pool = WorkerPool(
        config=WorkerConfig(concurrency=5),
        scheduler=app.state.scheduler,
        storage=None,                  # type: ignore[arg-type]
        workspace_registry=None,       # type: ignore[arg-type]
        provider_registry=None,        # type: ignore[arg-type]
        engine=InMemoryClaimEngine(adapters={}),
    )
    app.state.worker_pool = pool
    try:
        response = await client.get("/v1/health")
        body = response.json()
        assert body["worker_pool"]["in_flight"] == 0
        assert body["worker_pool"]["capacity"] == 5
        metrics = body["worker_pool"]["metrics"]
        assert metrics["primer_worker_capacity"] == 5
        assert "primer_session_turns_total" in metrics
        assert "primer_session_turn_duration_seconds" in metrics
        # Cancels that reached a running turn through the session row because their
        # NOTIFY was lost: without this counter such a miss is invisible.
        assert metrics["primer_worker_cancels_reconciled_total"] == 0
        # Same-worker duplicate claims skipped while the first execution is still in flight
        # (a heartbeat stalled past the lease TTL): invisible without this counter.
        assert metrics["primer_worker_duplicate_claims_total"] == 0
    finally:
        app.state.worker_pool = None


# --- /v1/ready: the database check /v1/health deliberately does not do ------


@pytest.mark.asyncio
async def test_ready_reports_ok_when_the_database_answers(client) -> None:
    response = await client.get("/v1/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["version"] == APP_VERSION
    db = body["checks"]["database"]
    assert db["ok"] is True
    assert db["error"] is None
    assert db["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_ready_is_503_with_the_failing_check_named_when_the_db_is_down(
    app, client,
) -> None:
    """The case /v1/health cannot see. 503 so a monitor reading only the
    status code (curl -f) gets the truth, and the body says which check
    failed."""

    async def _dead() -> None:
        raise ConnectionRefusedError("connect to db-host.internal:5432 refused")

    app.state.storage_provider.ping = _dead
    response = await client.get("/v1/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    db = body["checks"]["database"]
    assert db["ok"] is False
    assert db["error"] == "error"


@pytest.mark.asyncio
async def test_ready_does_not_leak_the_exception_text(app, client) -> None:
    """The route is unauthenticated: connection errors carry hostnames,
    ports and usernames, so only a stable code may reach the response."""

    async def _dead() -> None:
        raise ConnectionRefusedError(
            "connect to secret-db-host.internal:5432 as svc_user refused"
        )

    app.state.storage_provider.ping = _dead
    response = await client.get("/v1/ready")
    assert response.status_code == 503
    assert "secret-db-host" not in response.text
    assert "svc_user" not in response.text


@pytest.mark.asyncio
async def test_ready_times_out_on_a_hung_database(app, client, monkeypatch) -> None:
    """A database that accepts the connection and never answers must read
    as a timeout, not hang the monitor for the pool's 30s acquire bound."""
    import asyncio

    import primer.api.routers.health as health_mod

    async def _hang() -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(health_mod, "_READY_DB_TIMEOUT_S", 0.05)
    app.state.storage_provider.ping = _hang
    response = await client.get("/v1/ready")
    assert response.status_code == 503
    db = response.json()["checks"]["database"]
    assert db["ok"] is False
    assert db["error"] == "timeout"


@pytest.mark.asyncio
async def test_ready_is_503_when_no_storage_provider_is_wired(app, client) -> None:
    real = app.state.storage_provider
    app.state.storage_provider = None
    try:
        response = await client.get("/v1/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
    finally:
        app.state.storage_provider = real


@pytest.mark.asyncio
async def test_health_stays_200_while_the_database_is_down(app, client) -> None:
    """The contrast that justifies /v1/ready existing at all: /v1/health
    reads in-process state only, so it reports ok with the database gone.
    If this ever starts failing, /v1/health has begun checking the
    database and the readiness/probe reasoning in /v1/ready's docstring
    needs revisiting."""

    async def _dead() -> None:
        raise ConnectionRefusedError("down")

    app.state.storage_provider.ping = _dead
    assert (await client.get("/v1/ready")).status_code == 503
    health = await client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# workspaces.local: the topology rule for local-provider workspaces (ticket 01a1072f)
# ---------------------------------------------------------------------------


def _policy(*, enforce: bool, single_process: bool = False):
    from primer.model.scheduler import RuntimeMode, SchedulerProviderType
    from primer.workspace.local_policy import LocalWorkspacePolicy

    return LocalWorkspacePolicy.from_topology(
        runtime_mode=RuntimeMode.WORKER, scheduler_provider=SchedulerProviderType.POSTGRES,
        enforce=enforce, single_process=single_process,
    )


class _CensusProbe:
    def __init__(self, count) -> None:
        self.local_workspace_count = count


def _wire(app, policy, census) -> None:
    """Only what /v1/health may read: in-process state, never a registry call that reaches storage."""
    from types import SimpleNamespace

    app.state.workspace_registry = SimpleNamespace(local_policy=policy)
    app.state.workspace_probe = _CensusProbe(census)


@pytest.mark.asyncio
async def test_health_reports_the_local_workspace_rule_off_by_default(client) -> None:
    body = (await client.get("/v1/health")).json()
    local = body["workspaces"]["local"]
    assert local["enforcing"] is False
    assert local["refusing_local"] is False
    assert local["distributed"] is False
    assert local["signals"] == []


@pytest.mark.asyncio
async def test_health_counts_the_workspaces_a_refusing_deployment_cannot_use(app, client) -> None:
    _wire(app, _policy(enforce=True), census=3)
    local = (await client.get("/v1/health")).json()["workspaces"]["local"]
    assert local["distributed"] is True and local["enforcing"] is True and local["refusing_local"] is True
    assert local["local_workspaces"] == 3
    assert local["unusable"] == 3
    assert len(local["signals"]) == 2


@pytest.mark.asyncio
async def test_health_shows_what_switching_the_rule_on_would_do(app, client) -> None:
    """Switch OFF: nothing is unusable yet, but the operator can see the count flipping it would strand."""
    _wire(app, _policy(enforce=False), census=3)
    local = (await client.get("/v1/health")).json()["workspaces"]["local"]
    assert local["distributed"] is True and local["enforcing"] is False and local["refusing_local"] is False
    assert local["local_workspaces"] == 3
    assert local["unusable"] == 0


@pytest.mark.asyncio
async def test_health_shows_the_single_process_opt_in(app, client) -> None:
    _wire(app, _policy(enforce=True, single_process=True), census=2)
    local = (await client.get("/v1/health")).json()["workspaces"]["local"]
    assert local["single_process"] is True and local["refusing_local"] is False and local["unusable"] == 0


@pytest.mark.asyncio
async def test_health_does_not_guess_a_count_the_probe_has_not_made(app, client) -> None:
    _wire(app, _policy(enforce=True), census=None)
    local = (await client.get("/v1/health")).json()["workspaces"]["local"]
    assert local["local_workspaces"] is None
    assert local["unusable"] is None, "a refusing deployment with no count yet must say unknown, not zero"
