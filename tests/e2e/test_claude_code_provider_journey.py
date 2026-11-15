"""E2E: the claude_code provider drives a real park, approval and resume journey (the claude_code plan, task 10).

Against the live e2e server (``scripts/e2e/bringup.sh``), a provider whose
``binary_path`` is the shared claude CLI double (``tests/llm/claude_code/
fake_claude.py`` under its shell wrapper) runs an agent whose one workspace
tool is gated by a ``required`` approval policy:

  1. the double replays a ``parallel_tools``-style stream-json for step one
     (its first tool block, stop reason ``tool_use``);
  2. the session parks on the approval;
  3. the operator approves;
  4. the workspace tool runs for real and step two's recorded stdin carries
     the ``tool_result`` (the assistant tool_use block plus the user line).

The double's stream answers come from two NDJSON fixtures the test derives
from the unit fixture ``tests/llm/fixtures/claude_code/parallel_tools.ndjson``
(the step-one block rewritten to the real workspace tool; the step-two text
turn as recorded). The server must be a HOST process (the double and its
fixtures are host paths), and its environment is frozen at bring-up, so the
FAKE_* values must be exported BEFORE ``scripts/e2e/bringup.sh`` -- they ride
the provider's ``pass_env`` names into the scrubbed child environment. The
test itself checks that environment at its start and fails fast (with this
same instruction) if the exports are missing:

  FAKE_CLAUDE_FIXTURE="$PWD/tests/.e2e/claude_code/step1.ndjson" \
  FAKE_CLAUDE_FIXTURE_SEQ="$PWD/tests/.e2e/claude_code/step1.ndjson,$PWD/tests/.e2e/claude_code/step2.ndjson" \
  FAKE_CLAUDE_RECORD="$PWD/tests/.e2e/claude_code/record.json" \
  scripts/e2e/bringup.sh

then, per AGENTS.md section 5 (the e2e suite is CPU-exclusive, targeted runs
with -n0):

  PRIMER_RUN_E2E=1 PRIMER_E2E_PORT=8765 \
    uv run pytest tests/e2e/test_claude_code_provider_journey.py -q -n0

tearing down with the default NON-destructive ``scripts/e2e/teardown.sh``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from tests._support.model_profiles import agent_model, seed_llm_provider
from tests.llm.claude_code.fake_claude import write_fake_wrapper


# The scratch dir the bring-up env above points at (gitignored: tests/.e2e/).
_E2E = Path(__file__).resolve().parents[1] / ".e2e" / "claude_code"
_RECORD = _E2E / "record.json"
_COUNTER = _E2E / "record.json.count"
_STEP1 = _E2E / "step1.ndjson"
_STEP2 = _E2E / "step2.ndjson"

# The unit fixture the journey's fixtures are derived from.
_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "llm" / "fixtures" / "claude_code" / "parallel_tools.ndjson"
)

# The tool the fake is made to call: the workspace ``write`` tool (one of the
# seven session file tools a workspace session composes,
# primer/workspace/local/tools/), under its scoped id (composed workspace
# tools run under toolset "workspace",
# primer/agent/tool_manager.py WORKSPACE_TOOLSET_ID) as the bridge lists it
# (mcp server "p", primer/llm/claude_code/bridge.py).
TOOL_SCOPED = "workspace__write"
TOOL_NAME = f"mcp__p__{TOOL_SCOPED}"
# The tool_use id the rewritten step-one block carries: it is what the
# translator puts on the ToolCallPart, what the approval record stores and
# what step two's replayed stdin carries back.
TOOL_CALL_ID = "toolu_01AJbhnkiiDzLZbxxEynCkYp"
PROBE_PATH = "probe.txt"
PROBE_CONTENT = "hello from T10"
INSTRUCTION = (
    f"Create a file at the path '{PROBE_PATH}' with the content "
    f"'{PROBE_CONTENT}'. Use the write tool."
)


def _rewrite_step_one(objs: list[dict]) -> list[dict]:
    """The fixture's first tool block, retargeted at the real workspace tool.

    The block is the fixture's own lines (its first content_block_stop closes
    it); only the tool name and the input-JSON fragments change, and the
    ``assistant`` line's copy of the block follows.
    """
    args = {"path": PROBE_PATH, "content": PROBE_CONTENT}
    assert json.dumps(args) == '{"path": "probe.txt", "content": "hello from T10"}'
    fragments = ['{"path": "probe.txt"', ', "content": "hello from T10"}']
    fragment_i = 0
    for o in objs:
        if o.get("type") != "stream_event":
            continue
        ev = o.get("event") or {}
        if ev.get("type") == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "tool_use":
                block["name"] = TOOL_NAME
        elif ev.get("type") == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "input_json_delta":
                # The block's remaining fragments are blanked (the fixture has
                # three; the rewritten arguments carry in the first two): a
                # stale fragment would break the accumulated JSON and the
                # translator drops the ToolCallEnd on garbage args.
                delta["partial_json"] = (
                    fragments[fragment_i] if fragment_i < len(fragments) else ""
                )
                fragment_i += 1
    return objs


def _write_fixtures() -> str:
    """Derive the two journey fixtures from the unit fixture and stage them.

    step one: the fixture's leading system lines up to and including its
    first tool block, then the ``tool_use`` stop delta and the message_stop
    (the adapter kills the CLI at that stop; the rest of the recording is
    never read). step two: everything after that message_stop -- the
    fixture's own text turn ending in a successful result. Returns step two's
    answer text (its text deltas joined): the test's final assertion pins it
    in the session's log.
    """
    objs = [json.loads(line) for line in _SOURCE.read_text(encoding="utf-8").splitlines()]

    def _ev(o: dict) -> dict:
        return o.get("event") or {}

    tool_stop = next(
        i for i, o in enumerate(objs)
        if o.get("type") == "stream_event" and _ev(o).get("type") == "content_block_stop"
        and _ev(o).get("index") == 0
    )
    msg_delta = next(
        i for i, o in enumerate(objs)
        if o.get("type") == "stream_event" and _ev(o).get("type") == "message_delta"
        and (_ev(o).get("delta") or {}).get("stop_reason") == "tool_use"
    )
    msg_stop = next(
        i for i, o in enumerate(objs)
        if i > msg_delta and o.get("type") == "stream_event" and _ev(o).get("type") == "message_stop"
    )
    step1 = _rewrite_step_one(objs[: tool_stop + 1] + [objs[msg_delta], objs[msg_stop]])
    step2 = objs[msg_stop + 1 :]
    step2_text = "".join(
        (o.get("event") or {}).get("delta", {}).get("text", "")
        for o in step2
        if o.get("type") == "stream_event"
        and ((o.get("event") or {}).get("delta") or {}).get("type") == "text_delta"
    )
    has_result = any(o.get("type") == "result" for o in step2)
    assert step2_text and has_result, "the step-two turn lost its text or its result"

    _E2E.mkdir(parents=True, exist_ok=True)
    _STEP1.write_text(
        "\n".join(json.dumps(o, ensure_ascii=False) for o in step1) + "\n", encoding="utf-8"
    )
    _STEP2.write_text(
        "\n".join(json.dumps(o, ensure_ascii=False) for o in step2) + "\n", encoding="utf-8"
    )
    for stale in (_RECORD, _COUNTER):
        if stale.exists():
            stale.unlink()
    return step2_text


async def _poll_session(
    client: httpx.AsyncClient, session_id: str, *, timeout_s: float, until
) -> dict:
    """Poll the session row until ``until(row)`` is true; the last row on timeout."""
    iters = max(1, int(timeout_s / 0.5))
    last: dict = {}
    for _ in range(iters):
        r = await client.get(f"/v1/sessions/{session_id}")
        if r.status_code == 200:
            last = r.json()
            if until(last):
                return last
        await asyncio.sleep(0.5)
    return last


async def _wait_record(
    *, timeout_s: float, expect: str | None = None
) -> tuple[dict, list[dict]]:
    """Poll the double's record until its stdin carries ``expect`` (or returns the last one)."""
    last: tuple[dict, list[dict]] = ({}, [])
    iters = max(1, int(timeout_s / 0.5))
    for _ in range(iters):
        if _RECORD.exists():
            try:
                rec = json.loads(_RECORD.read_text(encoding="utf-8"))
                lines = [json.loads(l) for l in rec.get("stdin", "").splitlines() if l.strip()]
            except ValueError:
                rec, lines = {}, []  # mid-write; retry on the next poll
            last = (rec, lines)
            if expect is None or any(expect in json.dumps(l) for l in lines):
                return last
        await asyncio.sleep(0.5)
    return last


@pytest.mark.asyncio
async def test_claude_code_provider_park_approve_resume(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path
) -> None:
    suffix = unique_suffix
    llm_id = f"t10-cc-{suffix}"
    agent_id = f"t10-ag-{suffix}"
    wp_id = f"t10-wp-{suffix}"
    tpl_id = f"t10-tpl-{suffix}"
    policy_id = f"t10-pol-{suffix}"
    workspace_id: str | None = None
    session_id: str | None = None

    # The server's env is frozen at bring-up: without the FAKE_* exports the
    # double has nothing to replay and the park poll would fail 90 s later
    # with "never parked". Fail fast instead (the server must be a HOST
    # process for the exports to reach it at all -- see the docstring).
    expected_seq = f"{_STEP1},{_STEP2}"
    assert os.environ.get("FAKE_CLAUDE_FIXTURE_SEQ") == expected_seq, (
        "export the FAKE_* names and bring the host-process server up first: "
        f"export FAKE_CLAUDE_FIXTURE={_STEP1} FAKE_CLAUDE_FIXTURE_SEQ={expected_seq} "
        f"FAKE_CLAUDE_RECORD={_RECORD} -- then scripts/e2e/bringup.sh -- then run "
        "this test in the SAME shell (the docstring has the full command)"
    )
    assert os.environ.get("FAKE_CLAUDE_RECORD") == str(_RECORD), (
        f"FAKE_CLAUDE_RECORD must be {str(_RECORD)}: export it and re-bring the "
        "host-process server up (the docstring has the command)"
    )
    step2_text = _write_fixtures()

    try:
        # ----- 1. The claude_code provider on the double (+ its sonnet profile) -----
        r = await seed_llm_provider(client, {
            "id": llm_id,
            "provider": "claude_code",
            "config": {
                "binary_path": write_fake_wrapper(tmp_path),
                "pass_env": [
                    "FAKE_CLAUDE_FIXTURE",
                    "FAKE_CLAUDE_FIXTURE_SEQ",
                    "FAKE_CLAUDE_RECORD",
                ],
            },
            "models": [{"name": "sonnet", "context_length": 200_000}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, r.text

        # ----- 2. The agent (workspace tools are composed by the session) -----
        r = await client.post("/v1/agents", json={
            "id": agent_id,
            "description": "T10 claude_code park/approve/resume journey probe",
            "model": agent_model(llm_id, "sonnet"),
            "tools": [],
            "system_prompt": [
                "You are a test probe agent. When the user asks you to "
                "create a file, use the write tool with the "
                "exact path and content the user specifies."
            ],
        })
        assert r.status_code == 201, r.text

        # ----- 3. The workspace ladder -----
        r = await client.post("/v1/workspace_providers", json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, r.text
        r = await client.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "T10 template",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, r.text
        r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        workspace_id = r.json()["id"]

        # ----- 4. The required approval policy on the workspace write tool -----
        r = await client.post("/v1/tool_approval_policies", json={
            "id": policy_id,
            "toolset_id": "workspace",
            "tool_name": "write",
            "enabled": True,
            "approval": {"type": "required"},
        })
        assert r.status_code == 201, r.text
        r = await client.post("/v1/tool_approval_policies/invalidate")
        assert r.status_code == 202, r.text

        # ----- 5. The session: step one is the fake's rewritten tool block -----
        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": agent_id},
                "auto_start": True,
                "initial_instructions": INSTRUCTION,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # ----- 6. The session parks on the required approval -----
        row = await _poll_session(
            client, session_id, timeout_s=90.0,
            until=lambda row: row.get("parked_status") == "parked",
        )
        assert row.get("parked_status") == "parked", (
            f"the session never parked on the approval; last row: {row}"
        )

        # Step one's recorded stdin: the instruction, as the user tail line.
        rec, lines = await _wait_record(timeout_s=10.0, expect=INSTRUCTION)
        assert lines and INSTRUCTION in json.dumps(lines), (
            f"step one's recorded stdin does not carry the instruction: {rec}"
        )

        # ----- 7. The pending approval names the gated call -----
        r = await client.get(f"/v1/sessions/{session_id}/tool_approval/pending")
        assert r.status_code == 200, r.text
        pending = r.json()
        # The pending envelope carries the call's scoped name (the gate's
        # original_call stores ToolCallPart.name, not the bare tool id).
        assert pending["tool_name"] == TOOL_SCOPED, pending
        assert pending["approval_type"] == "required", pending
        assert pending["policy_id"] == policy_id, pending
        assert pending["tool_call_id"] == TOOL_CALL_ID, pending

        # ----- 8. Approve -----
        r = await client.post(
            f"/v1/sessions/{session_id}/tool_approval/respond",
            json={"tool_call_id": pending["tool_call_id"], "decision": "approved"},
        )
        assert r.status_code == 202, r.text
        assert r.json() == {"status": "accepted"}, r.text

        # ----- 9. Step two's recorded stdin carries the tool_result -----
        # The tool runs for real on the approved call, and the next model step
        # replays the assistant tool_use block plus the user tool_result line.
        rec, lines = await _wait_record(
            timeout_s=60.0, expect='"type": "tool_result"'
        )
        assert any('"type": "tool_result"' in json.dumps(l) for l in lines), (
            f"step two's recorded stdin never carried the tool_result: {rec}"
        )
        assert len(lines) == 2, f"the tool round is one assistant line + one user line: {lines}"
        assistant, user = lines
        use = assistant["message"]["content"][0]
        assert use["type"] == "tool_use" and use["id"] == TOOL_CALL_ID, use
        assert use["name"] == TOOL_NAME, use
        assert use["input"] == {"path": PROBE_PATH, "content": PROBE_CONTENT}, use
        result = user["message"]["content"][0]
        assert result["type"] == "tool_result" and result["tool_use_id"] == TOOL_CALL_ID, result
        assert not result.get("is_error"), f"the real write tool errored: {result}"
        assert "wrote" in result["content"], (
            f"the tool_result is not the real tool's answer: {result}"
        )
        # And the file is actually on disk under the workspace root.
        probe = tmp_path / workspace_id / PROBE_PATH
        assert probe.read_text(encoding="utf-8") == PROBE_CONTENT, (
            "the probe file is missing or wrong under the workspace root"
        )

        # ----- 10. The park clears and the turn ran to its end -----
        row = await _poll_session(
            client, session_id, timeout_s=60.0,
            until=lambda row: row.get("parked_status") != "parked",
        )
        assert row.get("parked_status") != "parked", (
            f"the park never cleared after the approval; last row: {row}"
        )
        assert row.get("status") != "ended", (
            f"the session ended instead of finishing its turn: {row}"
        )
        # The turn's end is the fake's step-two answer in the log (step two's
        # text deltas, joined by _write_fixtures; step one carried no text).
        for _ in range(20):  # 10 s: the log lags the session row a write at most
            items = (
                await client.get(
                    f"/v1/sessions/{session_id}/messages", params={"limit": 500}
                )
            ).json()["items"]
            answers = "".join(
                r.get("payload", {}).get("text", "") or ""
                for r in items if r.get("kind") == "assistant_token"
            )
            if step2_text in answers:
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError(
                f"the turn never delivered step two's answer {step2_text!r}; "
                f"last assistant log text: {answers[-200:]!r}"
            )

    finally:
        # Best-effort cleanup, in the order the deletes unblock (T0850's).
        urls = [
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel"
            if (workspace_id and session_id) else None,
            f"/v1/workspaces/{workspace_id}" if workspace_id else None,
            f"/v1/workspace_templates/{tpl_id}",
            f"/v1/workspace_providers/{wp_id}",
            f"/v1/tool_approval_policies/{policy_id}",
            f"/v1/agents/{agent_id}",
            f"/v1/model_profiles/{llm_id}--sonnet",
            f"/v1/llm_providers/{llm_id}",
        ]
        for url in urls:
            if url is None:
                continue
            try:
                if "/cancel" in url:
                    await client.post(url)
                else:
                    await client.delete(url)
            except Exception:  # noqa: BLE001 -- teardown is best-effort
                pass
