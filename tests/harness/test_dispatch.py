"""Tests for primer.harness.dispatch — worker-side dispatch entrypoint.

Uses a real local git bare repo (file://) so we exercise the actual
git + render + storage pipeline rather than mocking everything.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from primer.bus.in_memory import InMemoryEventBus
from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.harness.dispatch import HarnessDispatchDeps, run_one_harness_operation, sweep_harnesses
from primer.int.claim import CLAIM_PRIORITY_OPERATOR, ClaimKind
from primer.model.agent import Agent
from primer.model.harness import Harness, HarnessOperation, HarnessRendering, HarnessStatus
from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
from primer.storage.factory import StorageProviderFactory
from tests._support.harness_ops import enqueued

# ---------------------------------------------------------------------------
# Local bare-repo fixture seeded with harness files
# ---------------------------------------------------------------------------

_HARNESS_YAML = """\
apiVersion: primer/v1
kind: Harness
name: Test Harness
"""

_SCHEMA_JSON = """\
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["profile_id"],
  "properties": {
    "profile_id": {"type": "string"},
    "description": {"type": "string"}
  }
}
"""

_ASSISTANT_YAML = """\
kind: agent
name: assistant
spec:
  description: "{{ overrides.description | default('A helpful assistant') }}"
  model:
    profile_id: "{{ overrides.profile_id }}"
"""


def _make_local_bare_repo(tmp_path: Path) -> str:
    """Create a tiny bare repo seeded with harness files; return file:// URL."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)

    # Create the harness file structure at the repo root (no subpath)
    (work / "harness.yaml").write_text(_HARNESS_YAML)
    (work / "overrides.schema.json").write_text(_SCHEMA_JSON)
    templates = work / "templates"
    templates.mkdir()
    (templates / "assistant.yaml").write_text(_ASSISTANT_YAML)

    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=work, check=True)

    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(work), str(bare)],
        check=True,
    )
    return f"file://{bare}"


@pytest.fixture
def bare_repo(tmp_path):
    return _make_local_bare_repo(tmp_path)


# ---------------------------------------------------------------------------
# Helper to build deps
# ---------------------------------------------------------------------------


def _make_deps(fake_storage_provider) -> HarnessDispatchDeps:
    bus = InMemoryEventBus()
    deps = HarnessDispatchDeps(
        storage_provider=fake_storage_provider,
        event_bus=bus,
    )
    return deps


def _make_harness(
    harness_id: str,
    *,
    git_url: str,
    slug: str = "acme",
    overrides: dict | None = None,
    status: HarnessStatus = HarnessStatus.DRAFT,
    pending_operation: HarnessOperation = HarnessOperation.FETCH,
    worker_id: str = "w1",
) -> Harness:
    now = datetime.now(timezone.utc)
    return Harness(
        id=harness_id,
        slug=slug,
        name="Test Harness",
        git_url=git_url,
        ref="main",
        subpath=None,
        overrides=overrides or {"profile_id": "openai--gpt-4"},
        status=status,
        pending_operation=pending_operation,
        created_at=now,
    )


# ---------------------------------------------------------------------------
# 1. Fetch happy path — DRAFT → READY; schema cached; bundle_hash set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_happy_path(fake_storage_provider, bare_repo):
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = _make_harness("h1", git_url=bare_repo, slug="acme", status=HarnessStatus.DRAFT)
    await harness_storage.create(harness)

    await run_one_harness_operation(deps, harness_id="h1", worker_id="w1")

    updated = await harness_storage.get("h1")
    assert updated is not None
    assert updated.status == HarnessStatus.READY
    assert updated.available_commit is not None
    assert len(updated.available_commit) == 40
    assert updated.available_bundle_hash is not None
    assert updated.schema_hash is not None
    assert updated.overrides_schema is not None
    assert updated.pending_operation is None


# ---------------------------------------------------------------------------
# 2. Fetch with bad ref — returns ERROR with code=git_ref_not_found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_bad_ref(fake_storage_provider, bare_repo):
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = _make_harness(
        "h2",
        git_url=bare_repo,
        slug="acme2",
        status=HarnessStatus.DRAFT,
    )
    # override ref to something that doesn't exist
    harness = harness.model_copy(update={"ref": "no-such-branch-xyz"})
    await harness_storage.create(harness)

    await run_one_harness_operation(deps, harness_id="h2", worker_id="w1")

    updated = await harness_storage.get("h2")
    assert updated is not None
    assert updated.status == HarnessStatus.ERROR
    assert updated.last_operation_error is not None
    err = json.loads(updated.last_operation_error)
    assert err["code"] in ("git_ref_not_found", "ref_not_found")


# ---------------------------------------------------------------------------
# 3. Install happy path — agent row appears; HarnessRendering written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_happy_path(fake_storage_provider, bare_repo):
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    # Step 1: fetch so we have available_commit and schema
    harness = _make_harness(
        "h3", git_url=bare_repo, slug="acme3", status=HarnessStatus.DRAFT,
        overrides={"profile_id": "openai--gpt-4"},
    )
    await harness_storage.create(harness)
    await run_one_harness_operation(deps, harness_id="h3", worker_id="w1")

    # Step 2: enqueue install
    fetched = await harness_storage.get("h3")
    assert fetched.status == HarnessStatus.READY

    fetched = enqueued(fetched, HarnessOperation.INSTALL)
    await harness_storage.update(fetched)

    await run_one_harness_operation(deps, harness_id="h3", worker_id="w1")

    updated = await harness_storage.get("h3")
    assert updated is not None
    assert updated.status == HarnessStatus.INSTALLED
    assert updated.resolved_commit == updated.available_commit
    assert updated.bundle_hash == updated.available_bundle_hash

    # Agent row must be present with harness_id set
    agent_storage = fake_storage_provider.get_storage(Agent)
    agent = await agent_storage.get("acme3__assistant")
    assert agent is not None
    assert agent.harness_id == "h3"
    assert agent.model.profile_id == "openai--gpt-4"

    # HarnessRendering snapshot must be written
    rendering = await fake_storage_provider.get_storage(HarnessRendering).get("h3")
    assert rendering is not None
    assert rendering.harness_id == "h3"
    assert len(rendering.entries) == 1
    assert rendering.entries[0].kind == "agent"


# ---------------------------------------------------------------------------
# 4. Install with invalid overrides — returns ERROR with code=overrides_invalid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_invalid_overrides(fake_storage_provider, bare_repo):
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    # Fetch first (with valid overrides)
    harness = _make_harness(
        "h4", git_url=bare_repo, slug="acme4", status=HarnessStatus.DRAFT,
        overrides={"profile_id": "openai--gpt-4"},
    )
    await harness_storage.create(harness)
    await run_one_harness_operation(deps, harness_id="h4", worker_id="w1")

    # Now set invalid overrides (missing required profile_id) and install
    fetched = await harness_storage.get("h4")
    fetched = enqueued(fetched, HarnessOperation.INSTALL, overrides={})  # missing required profile_id
    await harness_storage.update(fetched)

    await run_one_harness_operation(deps, harness_id="h4", worker_id="w1")

    updated = await harness_storage.get("h4")
    assert updated.status == HarnessStatus.ERROR
    err = json.loads(updated.last_operation_error)
    assert err["code"] == "overrides_invalid"


# ---------------------------------------------------------------------------
# 5. Sync no-op fast path — when bundle hashes match, no re-render
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_noop_fast_path(fake_storage_provider, bare_repo):
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    # Fetch + install
    harness = _make_harness(
        "h5", git_url=bare_repo, slug="acme5", status=HarnessStatus.DRAFT,
        overrides={"profile_id": "openai--gpt-4"},
    )
    await harness_storage.create(harness)
    await run_one_harness_operation(deps, harness_id="h5", worker_id="w1")

    fetched = await harness_storage.get("h5")
    fetched = enqueued(fetched, HarnessOperation.INSTALL)
    await harness_storage.update(fetched)
    await run_one_harness_operation(deps, harness_id="h5", worker_id="w1")

    # Now enqueue sync (hashes should match → fast path)
    installed = await harness_storage.get("h5")
    assert installed.status == HarnessStatus.INSTALLED

    installed = enqueued(installed, HarnessOperation.SYNC)
    await harness_storage.update(installed)

    # Track agent mutations
    agent_storage = fake_storage_provider.get_storage(Agent)
    mutations: list[str] = []
    orig_create = agent_storage.create
    orig_update = agent_storage.update
    orig_delete = agent_storage.delete

    async def track_create(e):
        mutations.append("create")
        return await orig_create(e)

    async def track_update(e):
        mutations.append("update")
        return await orig_update(e)

    async def track_delete(i):
        mutations.append("delete")
        return await orig_delete(i)

    agent_storage.create = track_create
    agent_storage.update = track_update
    agent_storage.delete = track_delete

    await run_one_harness_operation(deps, harness_id="h5", worker_id="w1")

    synced = await harness_storage.get("h5")
    assert synced.status == HarnessStatus.INSTALLED
    # Fast path: no storage mutations on agent
    assert mutations == []


# ---------------------------------------------------------------------------
# 6. Uninstall — agent row deleted; HarnessRendering deleted; Harness deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_cleans_up(fake_storage_provider, bare_repo):
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    # Fetch + install first
    harness = _make_harness(
        "h6", git_url=bare_repo, slug="acme6", status=HarnessStatus.DRAFT,
        overrides={"profile_id": "openai--gpt-4"},
    )
    await harness_storage.create(harness)
    await run_one_harness_operation(deps, harness_id="h6", worker_id="w1")

    fetched = await harness_storage.get("h6")
    fetched = enqueued(fetched, HarnessOperation.INSTALL)
    await harness_storage.update(fetched)
    await run_one_harness_operation(deps, harness_id="h6", worker_id="w1")

    # Verify things exist pre-uninstall
    agent = await fake_storage_provider.get_storage(Agent).get("acme6__assistant")
    assert agent is not None

    # Now uninstall
    installed = await harness_storage.get("h6")
    installed = installed.model_copy(update={
        "pending_operation": HarnessOperation.UNINSTALL,
        "uninstall_cascade": True,  # inbound uninstall removes installed objects
    })
    await harness_storage.update(installed)

    await run_one_harness_operation(deps, harness_id="h6", worker_id="w1")

    # Agent row must be gone
    agent_after = await fake_storage_provider.get_storage(Agent).get("acme6__assistant")
    assert agent_after is None

    # HarnessRendering row must be gone
    rendering = await fake_storage_provider.get_storage(HarnessRendering).get("h6")
    assert rendering is None

    # Harness row must be gone
    harness_after = await harness_storage.get("h6")
    assert harness_after is None


@pytest.mark.asyncio
async def test_uninstall_no_cascade_keeps_entities(fake_storage_provider, bare_repo):
    """Default (non-cascade) uninstall removes ONLY the harness + rendering
    rows; the installed entities survive. This is the data-loss fix — deleting
    a harness must not wipe the objects it tracks unless cascade is set."""
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = _make_harness(
        "h7", git_url=bare_repo, slug="acme7", status=HarnessStatus.DRAFT,
        overrides={"profile_id": "openai--gpt-4"},
    )
    await harness_storage.create(harness)
    await run_one_harness_operation(deps, harness_id="h7", worker_id="w1")
    fetched = await harness_storage.get("h7")
    fetched = enqueued(fetched, HarnessOperation.INSTALL)
    await harness_storage.update(fetched)
    await run_one_harness_operation(deps, harness_id="h7", worker_id="w1")

    assert await fake_storage_provider.get_storage(Agent).get("acme7__assistant") is not None

    # Uninstall with NO cascade flag (uninstall_cascade defaults False).
    installed = await harness_storage.get("h7")
    installed = installed.model_copy(update={"pending_operation": HarnessOperation.UNINSTALL})
    await harness_storage.update(installed)
    await run_one_harness_operation(deps, harness_id="h7", worker_id="w1")

    # The installed entity SURVIVES; only the harness + rendering rows are gone.
    assert await fake_storage_provider.get_storage(Agent).get("acme7__assistant") is not None
    assert await fake_storage_provider.get_storage(HarnessRendering).get("h7") is None
    assert await harness_storage.get("h7") is None


# ---------------------------------------------------------------------------
# 7. sweep_harnesses: a no-op without a claim engine (legacy shim); with one,
#    it re-arms the lease the release window can drop (round 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_without_a_claim_engine_is_a_noop(fake_storage_provider):
    """The legacy caller shape (no claim engine) stays a no-op returning 0.

    Claim expiry and worker-death reclaim are owned by the ClaimEngine
    heartbeat loop in the worker pool; the sweep only repairs the harness
    lease the release window can drop, and it cannot do that without an
    engine.
    """
    deps = _make_deps(fake_storage_provider)
    count = await sweep_harnesses(deps, heartbeat_stale_after=timedelta(seconds=90))
    assert count == 0


@pytest_asyncio.fixture
async def sp(tmp_path):
    p = StorageProviderFactory.create(StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "sweep.sqlite"),
    ))
    await p.initialize()
    await p.get_content_store().ensure_schema()
    yield p
    await p.aclose()


def _sweep_engine(sp) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(adapters={
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=sp.get_storage(Harness)),
    })


class _RecordingEngine:
    """Delegates to the real engine and records every upsert the sweep attempts."""

    def __init__(self, engine: InMemoryClaimEngine) -> None:
        self._engine = engine
        self.upserts: list[tuple] = []

    async def upsert(self, kind, entity_id, **kwargs):
        self.upserts.append((kind, entity_id, kwargs))
        return await self._engine.upsert(kind, entity_id, **kwargs)

    async def has_lease(self, kind, entity_id):
        return await self._engine.has_lease(kind, entity_id)


def _sweep_deps(sp) -> HarnessDispatchDeps:
    return HarnessDispatchDeps(storage_provider=sp, event_bus=InMemoryEventBus())


@pytest.mark.asyncio
async def test_sweep_rearms_a_pending_harness_with_no_lease(sp):
    """Round 1 (review 2026-11-23, finding 1.3 residual window): a harness row
    with a pending operation and NO lease - the exact shape a
    ``drop_lease=True`` release leaves when an enqueue armed the lease in the
    window after the handler's fresh re-read - gets its lease from one sweep,
    and a second sweep leaves it alone (the same re-arm helper the boot
    recovery phase uses, idempotent)."""
    harness_storage = sp.get_storage(Harness)
    await harness_storage.create(Harness(
        id="sw-1", slug="sw", name="sw",
        status=HarnessStatus.READY, pending_operation=HarnessOperation.SYNC,
        created_at=datetime.now(timezone.utc),
    ))
    engine = _sweep_engine(sp)
    assert not await engine.has_lease(ClaimKind.HARNESS, "sw-1")

    deps = _sweep_deps(sp)
    assert await sweep_harnesses(deps, claim_engine=engine) == 1
    assert await engine.has_lease(ClaimKind.HARNESS, "sw-1")
    # Idempotent: the lease now exists, so the next sweep interval re-arms
    # nothing.
    assert await sweep_harnesses(deps, claim_engine=engine) == 0
    assert await engine.has_lease(ClaimKind.HARNESS, "sw-1")


@pytest.mark.asyncio
async def test_sweep_does_not_touch_a_harness_that_already_has_a_lease(sp):
    """Round 1: a harness that already holds a lease is not rewritten by the
    sweep (skip_existing): no upsert is even attempted for it."""
    harness_storage = sp.get_storage(Harness)
    await harness_storage.create(Harness(
        id="sw-2", slug="sw", name="sw",
        status=HarnessStatus.READY, pending_operation=HarnessOperation.FETCH,
        created_at=datetime.now(timezone.utc),
    ))
    engine = _sweep_engine(sp)
    await engine.upsert(
        ClaimKind.HARNESS, "sw-2", priority=CLAIM_PRIORITY_OPERATOR,
    )
    assert await engine.has_lease(ClaimKind.HARNESS, "sw-2")

    rec = _RecordingEngine(engine)
    assert await sweep_harnesses(_sweep_deps(sp), claim_engine=rec) == 0
    assert rec.upserts == [], "the sweep must not touch a harness that already holds a lease"
    assert await engine.has_lease(ClaimKind.HARNESS, "sw-2")


@pytest.mark.asyncio
async def test_sweep_ignores_a_harness_without_a_pending_operation(sp):
    """Round 1: a harness with no pending operation gets no lease from the
    sweep (eligibility is ``pending_operation IS NOT NULL``, as for every
    other harness claim surface)."""
    harness_storage = sp.get_storage(Harness)
    await harness_storage.create(Harness(
        id="sw-3", slug="sw", name="sw",
        status=HarnessStatus.READY,
        created_at=datetime.now(timezone.utc),
    ))
    engine = _sweep_engine(sp)
    assert await sweep_harnesses(_sweep_deps(sp), claim_engine=engine) == 0
    assert not await engine.has_lease(ClaimKind.HARNESS, "sw-3")
