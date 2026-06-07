"""SMK-DST-06 — a parked workspace session resumes cluster-wide.

This is the DISTRIBUTED counterpart of the hermetic ask_user park ->
respond -> resume cycle (T0862,
``tests/e2e/test_ask_user_resume_cycle_journey.py``). Where the hermetic
test runs everything in one process against the in-memory bus, this test
runs the genuine multi-process path:

  * 2 API processes + 2 worker processes against a shared Postgres
    (real ClaimEngine + Postgres LISTEN/NOTIFY event bus).
  * A scripted ``misc__ask_user`` agent emits the yielding tool call;
    whichever worker claims the first turn runs it and the engine PARKS
    the session (drops the lease, writes the park columns).
  * The operator answers via a DIFFERENT API process
    (``POST /v1/sessions/{sid}/ask_user/respond`` on API#1). That router
    publishes the resume event onto the Postgres bus.
  * The YieldEventListener (running in the worker processes) flips
    parked -> resumable and re-arms the engine lease; the ClaimEngine
    claim loop on ANY worker picks the row up, looks up the
    ``ask_user`` resume hook, synthesises the tool_result, and advances
    the turn to termination.

Cross-process invariants proven:
  * A session created on API#0 is claimed and parked by a worker
    process (visible REST-wide via ``parked_status``).
  * A respond issued on API#1 (a different API than the one used to
    create/observe the park) resumes the session cluster-wide.
  * The park clears, ``turn_no`` advances, and the session ends.

Worker LLM access
-----------------
Worker subprocesses cannot use an in-process fake LLM, so this test
stands up a real uvicorn server bound to 127.0.0.1:<port> serving the
deterministic ``tests._support.mock_llm`` app, and registers a scripted
OpenChat provider pointing at it. The shared on-disk workspace root is a
``tempfile.mkdtemp`` directory reachable by every subprocess on the
same host.

Requires:
- A live Postgres container + Docker for testcontainers.
- The distributed marker (``pytest -m distributed``).
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import uvicorn

from tests._support.mock_llm import Rule, ScriptRegistry, build_app
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
)
from tests._support.smk import smk
from tests.distributed.cluster import TestCluster


# ---------------------------------------------------------------------------
# Loopback mock-LLM uvicorn server (reachable by worker subprocesses)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Grab an ephemeral loopback port and release it for uvicorn to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _MockLLMServer:
    """Runs ``tests._support.mock_llm.build_app`` on a background thread.

    The worker subprocesses reach it over loopback at
    ``http://127.0.0.1:<port>/v1``.
    """

    def __init__(self, registry: ScriptRegistry) -> None:
        self.registry = registry
        self.port = _free_port()
        self._config = uvicorn.Config(
            build_app(registry),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="off",
        )
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.run, name="mock-llm-uvicorn", daemon=True
        )
        self._thread.start()
        # Wait for uvicorn to report it has started serving.
        deadline = time.monotonic() + 15.0
        while not self._server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("mock-LLM uvicorn did not start within 15s")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10.0)


@pytest_asyncio.fixture
async def cluster_2x2_resume(
    postgres_container: str, db_schema: str
) -> TestCluster:
    """2 API + 2 worker cluster for the cross-worker resume scenario.

    Relies on the default Postgres pool ceiling (PoolConfig.max_size),
    which is sized to cover a worker/coordinator process's long-lived
    LISTEN connections (event bus, scheduler, claim engine) plus per-turn
    storage + rate-limiter acquires. The old default of 10 starved the
    turn's ``pool.acquire()`` here; the current default leaves headroom,
    so no per-cluster override is needed (this test also guards that the
    default stays adequate for a basic distributed deployment).
    """
    cluster = TestCluster(
        postgres_url=postgres_container,
        api_count=2,
        worker_count=2,
        start_port=8360,
        schema=db_schema,
    )
    await cluster.start()
    try:
        yield cluster
    finally:
        await cluster.stop()


# ---------------------------------------------------------------------------
# Scenario — SMK-DST-06
# ---------------------------------------------------------------------------


@smk("SMK-DST-06")
@pytest.mark.distributed
@pytest.mark.asyncio
async def test_parked_session_resumes_cluster_wide(
    cluster_2x2_resume: TestCluster,
) -> None:
    """A parked ask_user session resumes cluster-wide after a cross-API respond.

    Flow:
      1. Stand up a loopback mock-LLM uvicorn server.
      2. Via API#0: register a scripted ``misc__ask_user`` agent + a
         local workspace, and start a workspace agent session.
      3. Poll ``GET /v1/sessions/{sid}`` until ``parked_status ==
         'parked'``; capture ``initial_turn_no`` and the parking worker.
      4. Respond to ask_user via API#1 (a DIFFERENT API process).
      5. Poll until ``parked_status`` clears AND ``turn_no`` advances
         AND ``status == 'ended'``.
      6. Assert park cleared, turn advanced, session ended; opportunistically
         assert cross-worker resume.
    """
    cluster = cluster_2x2_resume

    registry = ScriptRegistry()
    server = _MockLLMServer(registry)
    server.start()

    suffix = uuid.uuid4().hex[:8]
    scenario = f"scripted:{suffix}"
    prompt = "What is the airspeed velocity of an unladen swallow?"
    # Shared on-disk workspace root, reachable by every subprocess on
    # this host (workers rehydrate the on-disk session slot).
    root = Path(tempfile.mkdtemp(prefix=f"primer_dst06_{suffix}_"))

    try:
        await cluster.authenticate()

        # ----- 2. scripted ask_user agent + workspace + session (API#0) ----
        async with cluster.client(0) as c0:
            agent = await make_scripted_agent(
                c0, registry, server.base_url,
                suffix=suffix, scenario=scenario, tools=["misc__ask_user"],
                rules=[
                    # First turn: offered ask_user, no tool result yet ->
                    # emit the ask_user tool call, which PARKS the session.
                    Rule(
                        when_tool_offered="ask_user",
                        when_tool_result=False,
                        emit_tool="misc__ask_user",
                        emit_args={"prompt": prompt},
                    ),
                    # On resume (tool result present) -> terminating text.
                    Rule(when_tool_result=True, emit_text="done"),
                ],
            )
            wid = await make_local_workspace(c0, suffix=suffix, root=root)
            sid = await start_agent_session(
                c0, workspace_id=wid, agent_id=agent["agent_id"],
            )

        # ----- 3. Poll API#0 until the session parks ----------------------
        async def _parked_body() -> dict | None:
            async with cluster.client(0) as c0:
                r = await c0.get(f"/v1/sessions/{sid}")
            if r.status_code != 200:
                return None
            body = r.json()
            if body.get("status") == "ended":
                raise AssertionError(
                    f"session {sid} ended before parking: {body!r}"
                )
            if body.get("parked_status") == "parked":
                return body
            return None

        parked: dict = {}

        async def _is_parked() -> bool:
            nonlocal parked
            got = await _parked_body()
            if got is not None:
                parked = got
                return True
            return False

        # Generous: cross-process claim + first-turn run + park, with
        # container/loopback latency.
        await cluster.wait_for(_is_parked, timeout_s=120.0, interval_s=0.5)

        initial_turn_no = parked.get("turn_no") or 0

        # ----- 3b. Sanity: the pending prompt is visible (via API#1) ------
        async with cluster.client(1) as c1:
            r = await c1.get(f"/v1/sessions/{sid}/ask_user/pending")
            assert r.status_code == 200, (
                f"GET /ask_user/pending on API#1 returned {r.status_code}:"
                f" {r.text}"
            )
            pending = r.json()
            tool_call_id = pending["tool_call_id"]
            assert prompt in pending["prompt"], pending

        # ----- 4. Respond via a DIFFERENT API process (API#1) -------------
        async with cluster.client(1) as c1:
            r = await c1.post(
                f"/v1/sessions/{sid}/ask_user/respond",
                json={
                    "tool_call_id": tool_call_id,
                    "response": "African or European?",
                },
            )
            assert r.status_code == 202, (
                f"POST /ask_user/respond on API#1 returned {r.status_code}:"
                f" {r.text}"
            )
            assert r.json() == {"status": "accepted"}, r.text

        # ----- 5. Poll (via API#0) until resumed + advanced + ended -------
        resumed: dict = {}

        async def _is_resumed() -> bool:
            nonlocal resumed
            async with cluster.client(0) as c0:
                r = await c0.get(f"/v1/sessions/{sid}")
            if r.status_code != 200:
                return False
            body = r.json()
            resumed = body
            parked_clear = body.get("parked_status") in (None, "null")
            advanced = (body.get("turn_no") or 0) > initial_turn_no
            ended = body.get("status") == "ended"
            return bool(parked_clear and advanced and ended)

        # Generous: cross-process re-arm + claim + resume hook + terminating
        # turn, with container/loopback latency.
        await cluster.wait_for(_is_resumed, timeout_s=120.0, interval_s=0.5)

        # ----- 6. Assertions ----------------------------------------------
        # Park fully cleared.
        assert resumed.get("parked_status") in (None, "null"), resumed
        assert resumed.get("parked_state") in (None, {}, "null"), resumed
        assert resumed.get("parked_event_key") in (None, "", "null"), resumed

        # Turn advanced through the resume.
        final_turn_no = resumed.get("turn_no") or 0
        assert final_turn_no > initial_turn_no, (
            f"turn_no didn't advance through cluster-wide resume:"
            f" initial={initial_turn_no}, final={final_turn_no};"
            f" body={resumed!r}"
        )

        # Session reached its terminal state, having run the REAL
        # continuation turn (not the fail-closed path). turn_no == 2 means:
        # park turn (no bump) -> resume-inject release (+1) -> continuation
        # turn completes (+1); a fail-closed resume would stop at turn_no == 1.
        assert resumed.get("status") == "ended", resumed
        assert resumed.get("ended_reason") == "completed", resumed
        assert final_turn_no >= 2, (
            f"resume did not run the continuation turn (fail-closed?):"
            f" turn_no={final_turn_no}; body={resumed!r}"
        )

        # Cross-process proof: the session was created on API#0, parked by a
        # worker process, responded to via API#1 (a DIFFERENT API process),
        # and resumed to completion on the shared Postgres claim engine. API
        # processes never run turns, so a worker process executed both the
        # park turn and the continuation. Worker IDENTITY is intentionally
        # not asserted: the session adapter's on_release clears last_worker_id
        # on every release (park and completion), so the persisted terminal
        # row always reports last_worker_id == None — asserting on it would
        # flake. The cross-API respond + continuation-to-completion is the
        # load-bearing cluster-wide-resume invariant.

    finally:
        server.stop()
        import shutil

        shutil.rmtree(root, ignore_errors=True)
