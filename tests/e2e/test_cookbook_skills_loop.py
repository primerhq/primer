"""Cookbook recipe #1 regression: self-improving skill loop.

A background skill-evaluator agent watches a results file, grades it against a
skill stored in a collection, rewrites that skill, and goes back to watching --
a loop that improves the skill over time with no human in the loop.

Recipe: primerhq.github.io/docs_source/cookbook/self-improving-skill.md

Asserts (the recipe's verified outcome):
  * the agent's first turn calls ``watch_files`` and the session PARKS at zero
    cost waiting for the exact watched path;
  * writing that exact file (via the workspace file API) WAKES the parked
    session, which then rewrites the skill document -- its content changes
    from the seed;
  * a SECOND write continues the loop (the session re-parks on watch_files
    after each revision and wakes again).

Quirks pinned here: ``watch_files`` matches the EXACT file path (a directory
would not fire), and the wake path relies on the platform's background watcher
(leader-elected) being healthy on the bringup server.

Uses the scripted mock LLM (deterministic Rules), not a real model.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
)
from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = pytest.mark.asyncio


_WATCHED = "results/latest.md"
_SKILL_PATH = "support-reply.md"
_SEED_SKILL = (
    "# Support reply skill\n\nWhen replying to a support ticket:\n"
    "1. Greet the customer.\n2. Restate the problem.\n3. Give the fix steps.\n"
    "4. Offer further help."
)
_REVISED_SKILL = (
    "# Support reply skill (revised)\n\nWhen replying to a support ticket:\n"
    "1. Greet the customer.\n2. Restate the problem.\n3. Give the fix steps.\n"
    "4. Offer further help.\n\nImprovement note: for urgent issues, add "
    "'Restarting now...' right after the greeting."
)

# Real embedder + pgvector SSP -- a Collection requires both at create. The
# internal-collections subsystem is NOT bootstrapped here, so put_document is
# a pure storage write (best-effort re-index is skipped when search is
# inactive); the loop's correctness does not depend on indexing.
_SSP = {
    "provider": "pgvector",
    "config": {
        "hostname": "localhost", "port": 5432, "database": "primer_e2e",
        "username": "primer", "password": "primer", "db_schema": "public",
    },
}
_EMBED = {
    "provider": "huggingface",
    "models": [{"name": "all-MiniLM-L6-v2", "dimensions": 384}],
    "config": {}, "limits": {"max_concurrency": 1},
}


async def _put_watched(client: httpx.AsyncClient, wid: str, content: str) -> None:
    r = await client.put(
        f"/v1/workspaces/{wid}/files",
        params={"path": _WATCHED},
        json={"content": content, "encoding": "text"},
    )
    assert r.status_code in (200, 201, 204), r.text


async def _wait_park(
    client: httpx.AsyncClient, sid: str, *, timeout_s: float = 30.0,
) -> dict:
    """Poll until the session parks (parked_status == 'parked')."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{sid}")
        if r.status_code == 200:
            last = r.json()
            if last.get("parked_status") == "parked":
                return last
            if last.get("status") == "ended":
                raise AssertionError(
                    f"session {sid} ended before parking: {last!r}"
                )
        await asyncio.sleep(0.25)
    raise AssertionError(f"session {sid} never parked within {timeout_s}s: {last!r}")


# The local watch_files probe (HostInotifyProbe) establishes a watch via
# watchfiles/inotify. On a host whose per-user inotify limit is saturated, the
# probe raises MaxFilesWatch, logs a warning, and the watch never fires -- the
# exact OS caveat the recipe documents. That is an environment limit, not a
# regression in the code under test, so we detect it and skip the wake step
# rather than report a false failure.
_PRIMER_LOG = Path("tests/.e2e/logs/primer.log")


def _watcher_inotify_exhausted(wid: str) -> bool:
    """True if the server log shows the inotify probe failed for ``wid``."""
    if not _PRIMER_LOG.exists():
        return False
    try:
        tail = _PRIMER_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(
        "HostInotifyProbe" in line and "MaxFilesWatch" in line and wid in line
        for line in tail.splitlines()
    )


async def _wait_turn_advanced(
    client: httpx.AsyncClient, sid: str, wid: str, *, min_turn_no: int,
    timeout_s: float = 30.0,
) -> dict:
    """Poll until the session's turn_no advances past ``min_turn_no``.

    Proves the parked session WOKE (consumed the watch result and ran another
    turn) regardless of whether it has already re-parked by the time we look.
    Skips cleanly if the wake never lands because the host's inotify limit is
    exhausted (a documented environment caveat, not a code defect).
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{sid}")
        if r.status_code == 200:
            last = r.json()
            if last.get("turn_no", 0) > min_turn_no:
                return last
        await asyncio.sleep(0.25)
    if _watcher_inotify_exhausted(wid):
        pytest.skip(
            "watch_files wake could not be exercised: the host's inotify "
            "watch limit is exhausted (HostInotifyProbe raised MaxFilesWatch "
            f"for {wid}). Raise fs.inotify.max_user_instances / "
            "max_user_watches, or reduce co-resident watchers, to run this "
            "step. The park step above still validated the watch_files yield."
        )
    raise AssertionError(
        f"session {sid} turn_no did not advance past {min_turn_no} within "
        f"{timeout_s}s: {last!r}"
    )


async def _skill_content(client: httpx.AsyncClient, cid: str) -> str:
    r = await client.get(
        f"/v1/collections/{cid}/documents", params={"path": _SKILL_PATH},
    )
    assert r.status_code == 200, r.text
    return r.json()["content"]


@smk("SMK-COOKBOOK-01")
@requires("embedder", "pgvector")
async def test_skills_loop_improves_over_time(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    registry, base_url = mock_llm
    sfx = unique_suffix
    ssp_id = f"ssp-skill-{sfx}"
    emb_id = f"emb-skill-{sfx}"
    coll_id = f"skills-{sfx}"

    cleanup: list[str] = []
    try:
        # --- Skill collection + seed -------------------------------------
        r = await authed_client.post("/v1/ssp", json={"id": ssp_id, **_SSP})
        assert r.status_code in (201, 409), r.text
        r = await authed_client.post(
            "/v1/embedding_providers", json={"id": emb_id, **_EMBED},
        )
        assert r.status_code in (201, 409), r.text
        r = await authed_client.post("/v1/collections", json={
            "id": coll_id, "description": "Reusable skills.",
            "embedder": {"provider_id": emb_id, "model": "all-MiniLM-L6-v2"},
            "search_provider_id": ssp_id,
        })
        assert r.status_code == 201, r.text
        cleanup.append(f"/v1/collections/{coll_id}")

        r = await authed_client.put(
            f"/v1/collections/{coll_id}/documents",
            params={"path": _SKILL_PATH},
            json={"content": _SEED_SKILL, "title": "Support reply skill"},
        )
        assert r.status_code in (200, 201), r.text
        assert await _skill_content(authed_client, coll_id) == _SEED_SKILL

        # --- Evaluator agent: watch -> revise -> watch (loop) ------------
        # Rule 1 (no tool result): park on the EXACT watched path.
        # Rule 2 (woke from watch_files -- result carries "changes"): rewrite
        #   the skill with put_document.
        # Rule 3 (after put_document -- result carries the skill path): go back
        #   to watching. Ordering matters: the put_document result does NOT
        #   contain "changes", so rule 2 only fires on a genuine watch wake.
        agent = await make_scripted_agent(
            authed_client, registry, base_url, suffix=f"skill{sfx}",
            scenario=f"scripted:skill-{sfx}",
            tools=[
                "workspace_ext__watch_files",
                "system__get_document_content",
                "system__put_document",
            ],
            system_prompt=["You continuously improve a support-reply skill."],
            rules=[
                Rule(when_last_tool_result_contains="changes",
                     emit_tool="system__put_document",
                     emit_args={"collection_id": coll_id, "path": _SKILL_PATH,
                                "content": _REVISED_SKILL}),
                Rule(when_last_tool_result_contains=_SKILL_PATH,
                     emit_tool="workspace_ext__watch_files",
                     emit_args={"paths": [_WATCHED]}),
                Rule(when_tool_result=False,
                     emit_tool="workspace_ext__watch_files",
                     emit_args={"paths": [_WATCHED]}),
                Rule(when_tool_result=True, emit_text="loop"),
            ],
        )

        wid = await make_local_workspace(authed_client, suffix=f"skill{sfx}", root=tmp_path)
        # Seed the watched file's directory so the workspace root materialises
        # before the agent registers its watcher.
        await _put_watched(authed_client, wid, "placeholder")

        sid = await start_agent_session(
            authed_client, workspace_id=wid, agent_id=agent["agent_id"],
            instructions="Begin the watch loop.",
        )

        # 1. The first turn parks on watch_files (zero-cost wait).
        parked1 = await _wait_park(authed_client, sid)
        turn_at_park1 = parked1["turn_no"]

        # The background WatcherManager scans for newly-parked watch sessions
        # on a ~2-3s cadence and only then starts a watcher (capturing the
        # baseline mtime). Writing before the watcher arms would miss the
        # change, so give it a tick before mutating the file.
        await asyncio.sleep(3.0)

        # 2. Write the watched file -> the parked session wakes, revises the
        #    skill, and re-parks (loop continues).
        await _put_watched(authed_client, wid, "hey. is it broken? just restart it. bye.")
        await _wait_turn_advanced(authed_client, sid, wid, min_turn_no=turn_at_park1)

        # The skill document content changed from the seed.
        parked2 = await _wait_park(authed_client, sid)
        revised = await _skill_content(authed_client, coll_id)
        assert revised == _REVISED_SKILL, f"skill not revised after first wake: {revised!r}"
        assert revised != _SEED_SKILL

        # 3. A SECOND write continues the loop: the re-parked session wakes
        #    again. The re-park has a fresh event_key, so the WatcherManager
        #    must re-arm a watcher for it before the next write.
        turn_at_park2 = parked2["turn_no"]
        await asyncio.sleep(3.0)
        await _put_watched(authed_client, wid, "did you try turning it off and on?")
        woke2 = await _wait_turn_advanced(authed_client, sid, wid, min_turn_no=turn_at_park2)
        assert woke2["turn_no"] > turn_at_park2, woke2
    finally:
        # Cancel the (likely still-parked) session so it does not linger.
        try:
            await authed_client.post(f"/v1/sessions/{sid}/cancel")
        except Exception:
            pass
        for url in reversed(cleanup):
            await authed_client.delete(url)
        await authed_client.delete(f"/v1/embedding_providers/{emb_id}")
        await authed_client.delete(f"/v1/ssp/{ssp_id}")
