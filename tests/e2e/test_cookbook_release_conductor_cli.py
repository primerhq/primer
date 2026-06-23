"""Cookbook recipe (CLI path): Release Conductor (deploy gate / HITL), via primectl.

The ``primectl``-driven sibling of ``test_cookbook_release_conductor``. Every
setup step + the full session-HITL loop is driven with the exact ``primectl``
commands the rewritten doc shows:

  * ``create -f`` the scripted LLM provider + the ``release-conductor`` agent
    (``system__ask_user`` + ``workspaces__write_workspace_file`` bound);
  * ``create -f`` the REQUIRED ``tool_approval_policy`` on the deploy tool, then
    ``call tool_approval_policy invalidate`` so the running worker picks it up;
  * ``create -f`` / ``--set`` the local workspace; and
  * the two HITL paths:
      - APPROVE: ``session run --watch --answer "staging, v1.4.2" --yes`` answers
        the ``ask_user`` park and auto-approves the deploy gate inline, watching
        to ``ended: completed``; and
      - REJECT: ``session run --no-watch`` then poll for each park and resolve it
        one-shot with ``session respond ask-user`` (the confirm) and
        ``session respond tool-approval --decision rejected --reason ...``.

The success outcome asserted is the API test's:

  * APPROVE -> the transcript shows both the ask_user call and the deploy call,
    the deploy ``tool_result`` succeeded, the session ends ``completed``, and the
    deploy side effect (the ``RELEASE`` marker file) is on disk; and
  * REJECT  -> the deploy was offered but its ``tool_result`` is a rejection
    (carrying the ``reason``), there is NO deploy side effect, the session ends,
    and a durable rejected ``ToolApprovalRecord`` is written (read via the
    ``primectl raw`` records leg the doc flags).

The HITL decision is operator-driven through the CLI's HITL surface, NEVER
scripted into the mock LLM (the LLM only chooses to call ask_user then the deploy
tool; the operator chooses approve/reject). The gated "irreversible deploy" tool
is the built-in ``workspaces__write_workspace_file`` standing in for a real
``deploy-ops__run_deploy`` MCP tool; the test pins the tool-agnostic GATE
MECHANISM, with the written ``RELEASE`` file as the observable side effect.

Recipe: primerhq.github.io/docs_source/cookbook/release-conductor.md

Run with:
    PRIMER_RUN_E2E=1 uv run pytest \
        tests/e2e/test_cookbook_release_conductor_cli.py -n0 -q
"""
from __future__ import annotations

import json
import time

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk


_DEPLOY_PATH = "RELEASE"
_DEPLOY_CONTENT = "staging v1.4.2"
# The gated deploy tool. The policy keys on the bare (toolset_id, tool_name) pair
# (``workspaces`` / ``write_workspace_file``), but the pending-approval echo and
# the durable record report the call's NAMESPACED name.
_DEPLOY_TOOL = "workspaces__write_workspace_file"
_OPERATOR_ANSWER = "staging, v1.4.2"


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------


def _iter_parts(transcript: str, role: str, part_type: str):
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("role") != role:
            continue
        for part in obj.get("parts", []):
            if part.get("type") == part_type:
                yield part


def _call_names(transcript: str) -> list[str]:
    return [
        (p.get("name") or p.get("tool_name") or "")
        for p in _iter_parts(transcript, "assistant", "tool_call")
    ]


def _tool_result_outputs(transcript: str) -> list[str]:
    return [str(p.get("output") or "") for p in _iter_parts(transcript, "tool", "tool_result")]


# ---------------------------------------------------------------------------
# Scripted rules (mirror the API test): ask_user -> deploy -> report.
# ---------------------------------------------------------------------------


def _conductor_rules(*, workspace_id: str, abort_on_reject: bool) -> list[Rule]:
    """Deterministic rules discriminated purely by the latest tool-result.

      * no tool result yet           -> ask_user (confirm env+version)
      * last result is a rejection   -> abort report (reject path)
      * last result is the ask_user reply (``{"response": ...}``)
                                     -> write_workspace_file (the deploy)
      * otherwise (deploy ran)       -> success report (approve path)

    Order matters: the rejection echo still contains ``staging`` (the original
    deploy args), so it MUST be checked before the deploy rule, and the deploy
    rule keys off the ask_user reply envelope key (``"response"``) -- which the
    rejection result lacks -- so a rejected gate cannot re-trigger the deploy.

    The deploy call carries the real ``workspace_id``: the internal
    ``write_workspace_file`` tool requires it (the bound-session workspace is not
    auto-injected for the internal-toolset route).
    """
    rules: list[Rule] = [
        Rule(when_tool_result=False, emit_tool="system__ask_user",
             emit_args={"prompt": "Which environment and version should I deploy?"}),
    ]
    if abort_on_reject:
        rules.append(Rule(when_last_tool_result_contains="rejected",
                          emit_text="Deploy aborted: the operator denied the release."))
    rules.append(Rule(when_last_tool_result_contains='"response"',
                      emit_tool="workspaces__write_workspace_file",
                      emit_args={"workspace_id": workspace_id,
                                 "path": _DEPLOY_PATH, "content": _DEPLOY_CONTENT}))
    rules.append(Rule(when_tool_result=True, emit_text="Deploy complete."))
    return rules


# ---------------------------------------------------------------------------
# Session / park polling over the CLI
# ---------------------------------------------------------------------------


def _session_id_from_run(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith("session/") and "started" in line:
            return line.split("/", 1)[1].split()[0]
    return None


def _get_session(pc: Primectl, sid: str) -> dict:
    return json.loads(pc.run("get", "session", sid, "-o", "json", "-r").stdout)


def _wait_parked_on(pc: Primectl, sid: str, *, tool_name: str,
                    timeout_s: float = 45.0, interval_s: float = 0.5) -> dict:
    """Poll until the session parks on ``tool_name`` (the inner yielded tool)."""
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = _get_session(pc, sid)
        if last.get("parked_status") == "parked":
            yielded = (last.get("parked_state") or {}).get("yielded") or {}
            if yielded.get("tool_name") == tool_name:
                return last
        if last.get("status") == "ended":
            raise AssertionError(
                f"session {sid} ended before parking on {tool_name!r}: "
                f"reason={last.get('ended_reason')!r}"
            )
        time.sleep(interval_s)
    raise AssertionError(
        f"session {sid} never parked on {tool_name!r} within {timeout_s}s; last={last!r}"
    )


def _wait_terminal(pc: Primectl, sid: str, *, timeout_s: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = _get_session(pc, sid)
        if last.get("status") == "ended":
            return last
        time.sleep(0.5)
    raise AssertionError(f"session {sid} did not end within {timeout_s}s: {last!r}")


def _transcript(pc: Primectl, wid: str, sid: str) -> str:
    return pc.run(
        "workspace", "files", "get", wid,
        f".state/sessions/{sid}/messages.jsonl", "--content",
    ).stdout


def _release_marker(pc: Primectl, wid: str) -> str | None:
    """Return the RELEASE marker contents if the deploy ran, else None.

    ``workspace files get`` exits non-zero when the path is absent, so a failed
    read is the "no side effect" signal.
    """
    res = pc.run("workspace", "files", "get", wid, _DEPLOY_PATH, "--content", check=False)
    return res.stdout if res.returncode == 0 else None


def _make_agent(pc: Primectl, tmp_path, *, aid: str, pid: str, scenario: str) -> None:
    pc.run("create", "-f", manifest(tmp_path, f"agent-{aid}", "agent", {
        "id": aid,
        "description": "Confirms a deploy target with a human, then deploys behind a gate.",
        "model": {"provider_id": pid, "model_name": scenario},
        "tools": ["system__ask_user", "workspaces__write_workspace_file"],
        "max_tool_turns": 5,
        "system_prompt": [
            "You are a Release Conductor. Confirm the target and version with "
            "ask_user, then deploy by writing the RELEASE marker. Never deploy "
            "without confirming."
        ],
    }))


def _make_workspace(pc: Primectl, tmp_path, *, wp: str, tpl: str) -> str:
    pc.run("create", "-f", manifest(tmp_path, wp, "workspace_provider", {
        "id": wp, "provider": "local",
        "config": {"kind": "local", "root_path": str(tmp_path)},
    }))
    pc.run("create", "-f", manifest(tmp_path, tpl, "workspace_template", {
        "id": tpl, "description": "rc cli", "provider_id": wp,
        "backend": {"kind": "local"},
    }))
    return pc.run(
        "create", "workspace", "--set", f"template_id={tpl}",
    ).stdout.split("/")[1].split()[0]


@smk("SMK-COOKBOOK-CLI-13")
def test_release_conductor_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-rc-{sfx}"))

    pid = f"p-rc-cli-{sfx}"
    ok_agent = f"release-conductor-ok-cli-{sfx}"
    no_agent = f"release-conductor-no-cli-{sfx}"
    policy_id = f"tap-rc-cli-{sfx}"
    ok_scenario = f"scripted:rc-ok-cli-{sfx}"
    no_scenario = f"scripted:rc-no-cli-{sfx}"

    cleanup = [
        ("agent", ok_agent), ("agent", no_agent),
        ("tool_approval_policy", policy_id), ("llm_provider", pid),
    ]
    try:
        # --- Shared scripted LLM provider (two scenarios) ----------------
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [
                {"name": ok_scenario, "context_length": 8192},
                {"name": no_scenario, "context_length": 8192},
            ],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))

        # --- REQUIRED approval policy on the deploy tool + invalidate -----
        # Policies are unique on (toolset_id, tool_name); clear any leftover for
        # the pair first so re-runs don't 409.
        existing = json.loads(pc.run(
            "get", "tool_approval_policy", "-o", "json", "-r",
        ).stdout)
        for it in existing if isinstance(existing, list) else []:
            if it.get("toolset_id") == "workspaces" and it.get("tool_name") == "write_workspace_file":
                pc.run("delete", "tool_approval_policy", it["id"], check=False)
        pc.run("create", "-f", manifest(tmp_path, "pol", "tool_approval_policy", {
            "id": policy_id, "toolset_id": "workspaces", "tool_name": "write_workspace_file",
            "enabled": True, "approval": {"type": "required"}, "timeout_seconds": 600,
        }))
        # The resolver caches policies in-process; invalidate so the freshly
        # created row is visible to the running worker.
        pc.run("call", "tool_approval_policy", "invalidate")

        # ============================================================
        # APPROVE path: session run --watch answers ask_user (--answer) and
        # auto-approves the deploy gate (--yes), running to completion.
        # ============================================================
        # The deploy call must carry the real workspace id, so the workspace is
        # created BEFORE the scripted rules are registered.
        wid_a = _make_workspace(pc, tmp_path, wp=f"wp-rc-ok-cli-{sfx}", tpl=f"tpl-rc-ok-cli-{sfx}")
        registry.register(ok_scenario, _conductor_rules(workspace_id=wid_a, abort_on_reject=False))
        _make_agent(pc, tmp_path, aid=ok_agent, pid=pid, scenario=ok_scenario)

        run_a = pc.run(
            "session", "run", wid_a, "--agent", ok_agent,
            "-i", "Deploy the latest build.",
            "--answer", _OPERATOR_ANSWER, "--yes", "--timeout", "90",
        )
        assert "ended: completed" in run_a.stdout, run_a.stdout
        sid_a = _session_id_from_run(run_a.stdout)
        assert sid_a, f"could not parse approve session id:\n{run_a.stdout}"

        transcript_a = _transcript(pc, wid_a, sid_a)
        names_a = _call_names(transcript_a)
        assert any("ask_user" in n for n in names_a), f"approve path missing ask_user: {names_a}"
        assert any("write_workspace_file" in n for n in names_a), (
            f"approve path missing deploy call: {names_a}"
        )
        # The deploy actually ran: the RELEASE marker is on disk.
        marker = _release_marker(pc, wid_a)
        assert marker is not None and _DEPLOY_CONTENT in marker, (
            f"approved deploy did not write the RELEASE marker (got {marker!r})"
        )
        # The deploy tool_result succeeded (carries the write shape {path, size_bytes}),
        # not the ask_user resolution ({response: ...}) nor a rejection.
        deploy_ok = [
            out for out in _tool_result_outputs(transcript_a)
            if "size_bytes" in out and _DEPLOY_PATH in out and '"rejected"' not in out
        ]
        assert deploy_ok, (
            f"approve path recorded no successful deploy tool_result: {transcript_a!r}"
        )

        # ============================================================
        # REJECT path: start --no-watch, then resolve each park one-shot.
        # ============================================================
        wid_r = _make_workspace(pc, tmp_path, wp=f"wp-rc-no-cli-{sfx}", tpl=f"tpl-rc-no-cli-{sfx}")
        registry.register(no_scenario, _conductor_rules(workspace_id=wid_r, abort_on_reject=True))
        _make_agent(pc, tmp_path, aid=no_agent, pid=pid, scenario=no_scenario)

        run_r = pc.run(
            "session", "run", wid_r, "--agent", no_agent,
            "-i", "Deploy the latest build.", "--no-watch",
        )
        sid_r = _session_id_from_run(run_r.stdout)
        assert sid_r, f"could not parse reject session id:\n{run_r.stdout}"

        # 1. ask_user park -> answer it one-shot.
        _wait_parked_on(pc, sid_r, tool_name="ask_user")
        pc.run("session", "respond", "ask-user", sid_r, "--response", _OPERATOR_ANSWER)

        # 2. approval park (the gate tripped) -> reject it one-shot.
        approval = _wait_parked_on(pc, sid_r, tool_name="_approval")
        yielded = (approval.get("parked_state") or {}).get("yielded") or {}
        assert yielded.get("tool_name") == "_approval", approval
        pc.run(
            "session", "respond", "tool-approval", sid_r,
            "--decision", "rejected", "--reason", "change freeze window",
        )

        final_r = _wait_terminal(pc, sid_r, timeout_s=60)
        assert final_r.get("status") == "ended", final_r

        transcript_r = _transcript(pc, wid_r, sid_r)
        names_r = _call_names(transcript_r)
        assert any("write_workspace_file" in n for n in names_r), (
            f"reject path: the deploy was never offered: {names_r}"
        )
        # The gated call resolved to a rejection result, not a success.
        rejections = [out for out in _tool_result_outputs(transcript_r) if '"rejected"' in out]
        assert rejections, f"reject path: no rejection tool_result recorded: {transcript_r!r}"
        assert "change freeze window" in rejections[0], rejections
        # No deploy side effect: the RELEASE marker was NOT written.
        assert _release_marker(pc, wid_r) is None, "rejected deploy still wrote the marker"

        # --- The durable denial audit trail: a rejected ToolApprovalRecord. --
        # There is no first-class records resource, so read it via the raw leg
        # the doc flags.
        records = json.loads(pc.run(
            "raw", "GET", "/v1/tool_approval/records",
            "--param", "status=rejected", "--param", "length=50", "-o", "json",
        ).stdout).get("items", [])
        ours = [rec for rec in records if rec.get("session_id") == sid_r]
        assert ours, f"no rejected ToolApprovalRecord written for session {sid_r}: {records!r}"
        rec = ours[0]
        assert rec["decision"] == "rejected", rec
        assert rec["tool_name"] == _DEPLOY_TOOL, rec
        assert rec.get("reason") == "change freeze window", rec
    finally:
        for res, ident in cleanup:
            pc.run("delete", res, ident, check=False)
