"""Cookbook recipe (CLI path): self-improving skill loop, via primectl.

The ``primectl``-driven sibling of ``test_cookbook_skills_loop``. Every operator
step the rewritten doc shows is performed with the exact ``primectl`` commands:

  * ``create -f`` the embedder + pgvector SSP + the ``skills`` collection;
  * ``doc put`` the seed skill document;
  * ``create -f`` the scripted ``skill-evaluator`` agent;
  * ``create -f`` / ``--set`` the local workspace;
  * ``session run ... --no-watch`` to start the evaluator (it parks on
    watch_files on purpose, so the doc starts it without watching to terminal);
  * ``workspace files put`` to write the EXACT watched file (the wake), exactly
    as the doc's step 4 shows; and
  * ``doc get`` to read the rewritten skill back.

The success outcome asserted is the API test's: the evaluator parks on
``watch_files``; writing the exact watched path wakes it (its ``turn_no``
advances); it rewrites ``support-reply.md`` so the content differs from the seed.

Like the existing API test, the wake leg is SKIPPED-with-note when the host's
inotify watch limit is exhausted (a documented environment caveat, not a code
defect) -- the park step still validates the watch_files yield.

The evaluator is SCRIPTED via the shared in-process ``mock_llm``; the watch +
document IO platform paths are REAL. A collection requires a real embedder +
pgvector at create, so the test is ``@requires("embedder", "pgvector")``.

Recipe: primerhq.github.io/docs_source/cookbook/self-improving-skill.md
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk
from tests._support.testconfig import load_config, requires

pytestmark = [requires("embedder", "pgvector")]


_PGVECTOR_DSN = {
    "hostname": "localhost",
    "port": 5432,
    "database": "primer_e2e",
    "username": "primer",
    "password": "primer",
}

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


def _wait_park(pc: Primectl, sid: str, *, timeout_s: float = 30.0) -> dict:
    """Poll ``get session`` until parked (parked_status == 'parked')."""
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = pc.run("get", "session", sid, "-o", "json", "-r").json()
        if last.get("parked_status") == "parked":
            return last
        if last.get("status") == "ended":
            raise AssertionError(f"session {sid} ended before parking: {last!r}")
        time.sleep(0.25)
    raise AssertionError(f"session {sid} never parked within {timeout_s}s: {last!r}")


def _wait_turn_advanced(pc: Primectl, sid: str, wid: str, *, min_turn_no: int,
                        timeout_s: float = 30.0) -> dict:
    """Poll until turn_no advances past ``min_turn_no`` (proves the wake), or
    skip cleanly if the host's inotify limit is exhausted."""
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = pc.run("get", "session", sid, "-o", "json", "-r").json()
        if last.get("turn_no", 0) > min_turn_no:
            return last
        time.sleep(0.25)
    if _watcher_inotify_exhausted(wid):
        pytest.skip(
            "watch_files wake could not be exercised: the host's inotify watch "
            "limit is exhausted (HostInotifyProbe raised MaxFilesWatch for "
            f"{wid}). The park step above still validated the watch_files yield."
        )
    raise AssertionError(
        f"session {sid} turn_no did not advance past {min_turn_no} within "
        f"{timeout_s}s: {last!r}"
    )


@smk("SMK-COOKBOOK-CLI-07")
def test_skills_loop_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-skill-{sfx}"))

    cfg = load_config()["embedder"]
    emb_id = f"emb-skill-cli-{sfx}"
    ssp_id = f"ssp-skill-cli-{sfx}"
    pid = f"p-skill-cli-{sfx}"
    aid = f"skill-evaluator-cli-{sfx}"
    coll_id = f"skills-cli-{sfx}"
    wp = f"wp-skill-cli-{sfx}"
    tpl = f"tpl-skill-cli-{sfx}"

    scenario = f"scripted:skill-cli-{sfx}"
    # Rule 1 (no tool result): park on the EXACT watched path.
    # Rule 2 (woke from watch_files -- result carries "changes"): rewrite the
    #   skill with put_document.
    # Rule 3 (after put_document -- result carries the skill path): go back to
    #   watching. The put_document result does NOT contain "changes", so rule 2
    #   only fires on a genuine watch wake.
    registry.register(scenario, [
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
    ])

    sid: str | None = None
    wid: str | None = None
    try:
        # --- Skill collection + seed (create -f + doc put) ---------------
        pc.run("create", "-f", manifest(tmp_path, "emb", "embedding_provider", {
            "id": emb_id, "provider": "openai", "models": [{"name": cfg["model"]}],
            "config": {"url": cfg["base_url"], "api_key": cfg["api_key"], "flavor": "lmstudio"},
            "limits": {"max_concurrency": 2},
        }))
        pc.run("create", "-f", manifest(tmp_path, "ssp", "ssp", {
            "id": ssp_id, "provider": "pgvector", "config": _PGVECTOR_DSN,
        }))
        pc.run("create", "-f", manifest(tmp_path, "col", "collection", {
            "id": coll_id, "description": "Reusable skills.",
            "embedder": {"provider_id": emb_id, "model": cfg["model"]},
            "search_provider_id": ssp_id,
        }))
        pc.run("doc", "put", coll_id, _SKILL_PATH, "--content", _SEED_SKILL)
        seeded = pc.run("doc", "get", coll_id, _SKILL_PATH, "-o", "json").json()
        assert seeded.get("content") == _SEED_SKILL, seeded

        # --- The scripted evaluator agent --------------------------------
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [{"name": scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))
        pc.run("create", "-f", manifest(tmp_path, "agent", "agent", {
            "id": aid,
            "description": "Watches a results file, grades it, rewrites the skill.",
            "model": {"provider_id": pid, "model_name": scenario},
            "tools": [
                "workspace_ext__watch_files",
                "system__get_document_content",
                "system__put_document",
            ],
            "max_tool_turns": 12,
            "system_prompt": ["You continuously improve a support-reply skill."],
        }))

        # --- Local workspace ---------------------------------------------
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "skill cli", "provider_id": wp, "backend": {"kind": "local"},
        }))
        wid = pc.run("create", "workspace", "--set", f"template_id={tpl}").stdout.split("/")[1].split()[0]

        # Seed the watched file's directory so the workspace root materialises
        # before the agent registers its watcher (doc step 4 file write verb).
        pc.run("workspace", "files", "put", wid, _WATCHED, "--content", "placeholder")

        # --- Run the evaluator (it parks on watch_files; --no-watch) ------
        run = pc.run("session", "run", wid, "--agent", aid, "-i", "Begin the watch loop.", "--no-watch")
        for line in run.stdout.splitlines():
            if line.startswith("session/") and "started" in line:
                sid = line.split("/", 1)[1].split()[0]
                break
        assert sid, f"could not parse session id:\n{run.stdout}"

        # 1. The first turn parks on watch_files (zero-cost wait).
        parked1 = _wait_park(pc, sid)
        turn_at_park1 = int(parked1["turn_no"])

        # The background WatcherManager arms the watcher on a ~2-3s cadence;
        # give it a tick before mutating the file or the change would be missed.
        time.sleep(3.0)

        # 2. Write the watched file -> the parked session wakes + rewrites.
        pc.run("workspace", "files", "put", wid, _WATCHED,
               "--content", "hey. is it broken? just restart it. bye.")
        _wait_turn_advanced(pc, sid, wid, min_turn_no=turn_at_park1)

        # The skill document content changed from the seed (doc get verb).
        _wait_park(pc, sid)
        revised = pc.run("doc", "get", coll_id, _SKILL_PATH, "-o", "json").json().get("content")
        assert revised == _REVISED_SKILL, f"skill not revised after wake: {revised!r}"
        assert revised != _SEED_SKILL
    finally:
        # The evaluator is likely still parked; cancel it so it does not linger.
        # Cancel lives under the workspace, so use the raw escape hatch.
        if sid is not None and wid is not None:
            pc.run("raw", "POST", f"/v1/workspaces/{wid}/sessions/{sid}/cancel", check=False)
        for res, ident in (
            ("agent", aid), ("collection", coll_id), ("llm_provider", pid),
            ("ssp", ssp_id), ("embedding_provider", emb_id),
        ):
            pc.run("delete", res, ident, check=False)
