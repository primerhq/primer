"""Dispatch wiring for outbound BUILD + PUSH — Spec B §5, §6, §9.

End-to-end coverage of:

* ``_do_build`` flipping status to DRAFT / INSTALLED / OUTDATED based on
  whether the harness has been pushed and whether the bundle drifted.
* ``_do_push`` writing files to a file-protocol bare repo and stamping
  ``last_pushed_*`` on the row.
* The direction guard at the top of ``run_one_harness_operation``.

Uses file-protocol git bare repos so we hit the real git pipeline.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.harness.dispatch import (
    HarnessDispatchDeps,
    run_one_harness_operation,
)
from primer.model.agent import Agent, AgentModel
from primer.model.harness import (
    Harness,
    HarnessDirection,
    HarnessOperation,
    HarnessRendering,
    HarnessStatus,
    OverrideMapping,
    TrackedEntity,
)


# ---------------------------------------------------------------------------
# Bare-repo helpers
# ---------------------------------------------------------------------------


def _init_bare(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True,
    )
    return bare


def _push_extra_commit_to_bare(tmp_path: Path, bare: Path, name: str = "extra") -> str:
    """Clone the bare repo, add a stray commit, push back. Returns new SHA."""
    seed = tmp_path / f"seed-{name}"
    subprocess.run(
        ["git", "clone", f"file://{bare}", str(seed)],
        check=True, capture_output=True,
    )
    target = seed / f"{name}.txt"
    target.write_text("hi from a divergent push\n")
    subprocess.run(
        ["git", "-C", str(seed), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed),
         "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "divergent"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "push", "origin", "main"],
        check=True, capture_output=True,
    )
    out = subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"],
        check=True, capture_output=True,
    )
    return out.stdout.decode().strip()


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------


def _make_deps(fake_storage_provider) -> HarnessDispatchDeps:
    return HarnessDispatchDeps(
        storage_provider=fake_storage_provider,
        event_bus=InMemoryEventBus(),
    )


def _make_agent(*, agent_id: str = "ag-bot",
                provider_id: str = "openai",
                temperature: float = 0.2) -> Agent:
    return Agent(
        id=agent_id,
        description="Friendly bot",
        model=AgentModel(provider_id=provider_id, model_name="gpt-4"),
        temperature=temperature,
        tools=[],
    )


def _make_outbound_harness(
    harness_id: str,
    *,
    git_url: str,
    slug: str = "acme",
    pending: HarnessOperation | None = None,
    source_id: str = "ag-bot",
    last_pushed_commit: str | None = None,
    last_pushed_bundle_hash: str | None = None,
) -> Harness:
    return Harness(
        id=harness_id,
        slug=slug,
        name="Acme",
        direction=HarnessDirection.OUTBOUND,
        git_url=git_url,
        ref="main",
        status=HarnessStatus.DRAFT,
        pending_operation=pending,
        tracked_entities=[
            TrackedEntity(
                kind="agent",
                source_id=source_id,
                template_name="assistant",
                overrides=[
                    OverrideMapping(
                        field_path="/model/provider_id",
                        override_path="llm.provider_id",
                        widget="llm-provider-picker",
                    ),
                ],
            ),
        ],
        last_pushed_commit=last_pushed_commit,
        last_pushed_bundle_hash=last_pushed_bundle_hash,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 1. BUILD on a never-pushed outbound harness → DRAFT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_dry_outbound_sets_status_draft(
    fake_storage_provider, tmp_path,
):
    bare = _init_bare(tmp_path)
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)
    agent_storage = fake_storage_provider.get_storage(Agent)

    await agent_storage.create(_make_agent())

    harness = _make_outbound_harness(
        "h-build-1",
        git_url=f"file://{bare}",
        pending=HarnessOperation.BUILD,
    )
    await harness_storage.create(harness)

    await run_one_harness_operation(deps, harness_id="h-build-1", worker_id="w1")

    updated = await harness_storage.get("h-build-1")
    assert updated is not None
    assert updated.status == HarnessStatus.DRAFT
    assert updated.pending_operation is None
    assert updated.bundle_hash is not None
    assert len(updated.bundle_hash) == 64
    assert updated.overrides_schema is not None
    assert updated.schema_hash is not None

    rendering = await fake_storage_provider.get_storage(HarnessRendering).get(
        "h-build-1",
    )
    assert rendering is not None
    assert len(rendering.entries) == 1
    entry = rendering.entries[0]
    assert entry.kind == "agent"
    assert entry.template_name == "assistant"
    assert entry.source_entity_id == "ag-bot"
    assert entry.source_dependency is None
    assert entry.resolved_id == "acme__assistant"


# ---------------------------------------------------------------------------
# 2. PUSH writes to the remote and stamps last_pushed_*
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_writes_to_remote_and_flips_status(
    fake_storage_provider, tmp_path,
):
    bare = _init_bare(tmp_path)
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)
    agent_storage = fake_storage_provider.get_storage(Agent)

    await agent_storage.create(_make_agent())

    # BUILD first
    harness = _make_outbound_harness(
        "h-push-1",
        git_url=f"file://{bare}",
        pending=HarnessOperation.BUILD,
    )
    await harness_storage.create(harness)
    await run_one_harness_operation(deps, harness_id="h-push-1", worker_id="w1")

    built = await harness_storage.get("h-push-1")
    assert built is not None
    assert built.status == HarnessStatus.DRAFT
    bundle_after_build = built.bundle_hash

    # Now PUSH
    built = built.model_copy(update={"pending_operation": HarnessOperation.PUSH})
    await harness_storage.update(built)
    await run_one_harness_operation(deps, harness_id="h-push-1", worker_id="w1")

    pushed = await harness_storage.get("h-push-1")
    assert pushed is not None
    assert pushed.status == HarnessStatus.INSTALLED
    assert pushed.pending_operation is None
    assert pushed.last_pushed_commit is not None
    assert len(pushed.last_pushed_commit) == 40
    assert pushed.last_pushed_bundle_hash == bundle_after_build
    assert pushed.bundle_hash == bundle_after_build
    assert pushed.last_pushed_at is not None

    # Clone the bare repo and verify the rendered files are present.
    work = tmp_path / "verify"
    subprocess.run(
        ["git", "clone", f"file://{bare}", str(work)],
        check=True, capture_output=True,
    )
    assert (work / "harness.yaml").is_file()
    assert (work / "overrides.schema.json").is_file()
    assert (work / "templates" / "assistant.yaml").is_file()
    assert b"{{ overrides.llm.provider_id }}" in (
        work / "templates" / "assistant.yaml"
    ).read_bytes()


# ---------------------------------------------------------------------------
# 3. After a successful push, mutating a tracked entity flips next BUILD to OUTDATED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_detected_after_entity_mutation(
    fake_storage_provider, tmp_path,
):
    bare = _init_bare(tmp_path)
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)
    agent_storage = fake_storage_provider.get_storage(Agent)

    await agent_storage.create(_make_agent())

    # BUILD + PUSH
    harness = _make_outbound_harness(
        "h-drift",
        git_url=f"file://{bare}",
        pending=HarnessOperation.BUILD,
    )
    await harness_storage.create(harness)
    await run_one_harness_operation(deps, harness_id="h-drift", worker_id="w1")

    built = await harness_storage.get("h-drift")
    built = built.model_copy(update={"pending_operation": HarnessOperation.PUSH})
    await harness_storage.update(built)
    await run_one_harness_operation(deps, harness_id="h-drift", worker_id="w1")

    pushed = await harness_storage.get("h-drift")
    assert pushed.status == HarnessStatus.INSTALLED

    # Mutate the tracked agent — change provider_id (a templated field) so
    # the rendered bundle stays identical (only the inferred-default in the
    # schema would change); to force a real bundle change we also tweak
    # temperature (a NON-templated leaf), which alters the rendered spec.
    agent = await agent_storage.get("ag-bot")
    mutated = agent.model_copy(update={"temperature": 0.9})
    await agent_storage.update(mutated)

    # BUILD again
    pushed = pushed.model_copy(update={"pending_operation": HarnessOperation.BUILD})
    await harness_storage.update(pushed)
    await run_one_harness_operation(deps, harness_id="h-drift", worker_id="w1")

    drifted = await harness_storage.get("h-drift")
    assert drifted.status == HarnessStatus.OUTDATED
    assert drifted.bundle_hash != pushed.last_pushed_bundle_hash
    # The last-pushed pointer is untouched until the next push lands.
    assert drifted.last_pushed_bundle_hash == pushed.last_pushed_bundle_hash
    assert drifted.last_pushed_commit == pushed.last_pushed_commit


# ---------------------------------------------------------------------------
# 4. PUSH refuses when the remote diverged from last_pushed_commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_refuses_when_remote_diverged(
    fake_storage_provider, tmp_path,
):
    bare = _init_bare(tmp_path)
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)
    agent_storage = fake_storage_provider.get_storage(Agent)

    await agent_storage.create(_make_agent())

    # BUILD + PUSH the first version.
    harness = _make_outbound_harness(
        "h-div",
        git_url=f"file://{bare}",
        pending=HarnessOperation.BUILD,
    )
    await harness_storage.create(harness)
    await run_one_harness_operation(deps, harness_id="h-div", worker_id="w1")

    built = await harness_storage.get("h-div")
    built = built.model_copy(update={"pending_operation": HarnessOperation.PUSH})
    await harness_storage.update(built)
    await run_one_harness_operation(deps, harness_id="h-div", worker_id="w1")

    pushed = await harness_storage.get("h-div")
    assert pushed.status == HarnessStatus.INSTALLED

    # Someone else pushes a divergent commit to the remote.
    _push_extra_commit_to_bare(tmp_path, bare)

    # Mutate locally so BUILD + PUSH actually want to write something.
    agent = await agent_storage.get("ag-bot")
    await agent_storage.update(agent.model_copy(update={"temperature": 0.42}))

    # BUILD then PUSH — PUSH should reject with push_remote_diverged.
    pushed = pushed.model_copy(update={"pending_operation": HarnessOperation.BUILD})
    await harness_storage.update(pushed)
    await run_one_harness_operation(deps, harness_id="h-div", worker_id="w1")

    rebuilt = await harness_storage.get("h-div")
    assert rebuilt.status == HarnessStatus.OUTDATED

    rebuilt = rebuilt.model_copy(update={"pending_operation": HarnessOperation.PUSH})
    await harness_storage.update(rebuilt)
    await run_one_harness_operation(deps, harness_id="h-div", worker_id="w1")

    failed = await harness_storage.get("h-div")
    assert failed.status == HarnessStatus.ERROR
    assert failed.last_operation_error is not None
    err = json.loads(failed.last_operation_error)
    assert err["code"] == "push_remote_diverged"


# ---------------------------------------------------------------------------
# 5. Direction guard: FETCH on an OUTBOUND row → ERROR direction_mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direction_mismatch_blocks_inbound_op_on_outbound(
    fake_storage_provider, tmp_path,
):
    bare = _init_bare(tmp_path)
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = _make_outbound_harness(
        "h-mm-1",
        git_url=f"file://{bare}",
        pending=HarnessOperation.FETCH,  # inbound op on outbound row
    )
    await harness_storage.create(harness)

    await run_one_harness_operation(deps, harness_id="h-mm-1", worker_id="w1")

    updated = await harness_storage.get("h-mm-1")
    assert updated is not None
    assert updated.status == HarnessStatus.ERROR
    assert updated.pending_operation is None
    err = json.loads(updated.last_operation_error)
    assert err["code"] == "direction_mismatch"
    assert err["operation"] == "fetch"


# ---------------------------------------------------------------------------
# 6. Direction guard: BUILD on an INBOUND row → ERROR direction_mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direction_mismatch_blocks_outbound_op_on_inbound(
    fake_storage_provider, tmp_path,
):
    deps = _make_deps(fake_storage_provider)
    harness_storage = fake_storage_provider.get_storage(Harness)

    harness = Harness(
        id="h-mm-2",
        slug="inb",
        name="Inbound",
        # direction defaults to INBOUND
        git_url=f"file://{tmp_path}/never",
        ref="main",
        status=HarnessStatus.DRAFT,
        pending_operation=HarnessOperation.BUILD,
        created_at=datetime.now(timezone.utc),
    )
    await harness_storage.create(harness)

    await run_one_harness_operation(deps, harness_id="h-mm-2", worker_id="w1")

    updated = await harness_storage.get("h-mm-2")
    assert updated is not None
    assert updated.status == HarnessStatus.ERROR
    assert updated.pending_operation is None
    err = json.loads(updated.last_operation_error)
    assert err["code"] == "direction_mismatch"
    assert err["operation"] == "build"
