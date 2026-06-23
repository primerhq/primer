"""Cookbook recipe (CLI path): scheduled stock-news monitor, driven by primectl.

The ``primectl``-driven sibling of ``test_cookbook_stock_monitor``. Every setup
step is the exact ``primectl`` command the rewritten doc shows:

  * ``create -f`` the scripted LLM provider + the ``stock-monitor`` agent
    (``misc__inform_user`` bound);
  * ``create -f`` / ``--set`` the local workspace;
  * ``create -f`` the scheduled trigger and ``call trigger subscriptions`` to
    bind an ``agent_fresh_session`` subscription whose ``payload_template`` is
    the watchlist instruction; and
  * ``call trigger fire-now`` to stand in for the cron, then ``get session`` to
    poll the fired agent session to terminal and ``workspace files get`` to read
    the on-disk transcript and report back.

The success outcome asserted is the API test's: the ``agent_fresh_session``-
fired session RUNS to terminal ``completed`` (the slot-allocation regression
guard), and

  * MATERIAL path  -> the agent judges the news material and records a
    ``misc__inform_user`` tool_call (with the alert ``message``); and
  * SILENT path    -> the agent judges nothing material and ends WITHOUT any
    ``inform_user`` call (no noise on slow news days).

HERMETIC delivery: with no channel bound, ``inform_user`` degrades to
``delivered_to: 0`` but the tool_call is still RECORDED in the transcript, so we
assert on the transcript rather than a live channel post. The web-search leg is
folded into the instruction text so the test stays hermetic. The recipe's live
``delivered_to: 1`` Discord round-trip is the manual coverage for the transport.

The monitor agent is SCRIPTED via the shared in-process ``mock_llm``; the
trigger -> fresh-session dispatch + execution platform paths are REAL. Not
capability-gated.

Recipe: primerhq.github.io/docs_source/cookbook/scheduled-stock-monitor.md

Run with:
    PRIMER_RUN_E2E=1 uv run pytest \
        tests/e2e/test_cookbook_scheduled_stock_monitor_cli.py -n0 -q
"""
from __future__ import annotations

import json
import time

import yaml

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk


_ALERT_MESSAGE = (
    "ALERT NVDA: regulator opened an antitrust probe into its data-center "
    "GPU sales, likely to move the stock."
)


def _files_get(pc: Primectl, wid: str, rel: str) -> str:
    return pc.run("workspace", "files", "get", wid, rel, "--content").stdout


def _poll_session(pc: Primectl, sid: str, *, timeout_s: float = 120.0) -> dict:
    """Poll GET /v1/sessions/<id> via ``primectl get session`` until terminal.

    ``get session <id> -o json -r`` prints the bare (un-enveloped) session body.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        row = json.loads(
            pc.run("get", "session", sid, "-o", "json", "-r").stdout
        )
        if row.get("status") == "ended":
            return row
        if time.monotonic() > deadline:
            raise AssertionError(f"session {sid} did not end within {timeout_s}s: {row}")
        time.sleep(2.0)


def _tool_calls(transcript: str) -> list[dict]:
    calls: list[dict] = []
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("role") != "assistant":
            continue
        for part in obj.get("parts", []):
            if part.get("type") == "tool_call":
                calls.append(part)
    return calls


def _inform_user_messages(transcript: str) -> list[str]:
    out: list[str] = []
    for part in _tool_calls(transcript):
        name = part.get("name") or part.get("tool_name") or ""
        if "inform_user" not in name:
            continue
        args = part.get("args") or part.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if isinstance(args, dict) and "message" in args:
            out.append(str(args["message"]))
    return out


def _fire_and_read_transcript(
    pc: Primectl, tmp_path, trigger_id: str, wid: str,
) -> str:
    """fire-now the trigger, resolve the dispatched session, poll to terminal,
    and return its on-disk transcript text."""
    empty = tmp_path / "empty.json"
    empty.write_text("{}\n")
    fire = json.loads(pc.run(
        "call", "trigger", "fire-now", trigger_id, "-f", str(empty), "-o", "json",
    ).stdout)
    assert not fire.get("skipped"), fire
    sid = None
    for res in fire.get("results", []):
        if res.get("ok") and res.get("artefact_id"):
            sid = res["artefact_id"]
            break
    assert sid, f"fire-now did not dispatch an agent session: {fire}"

    final = _poll_session(pc, sid, timeout_s=120)
    # The regression guard: a trigger-fired fresh session must RUN, not silently
    # strand. It must complete the agent turn and write a transcript.
    assert final.get("ended_reason") == "completed", (
        f"trigger-fired session did not run to completion: {final}"
    )
    return _files_get(pc, wid, f".state/sessions/{sid}/messages.jsonl")


def _make_workspace(pc: Primectl, tmp_path, sfx: str) -> str:
    wp = f"wp-stk-cli-{sfx}"
    tpl = f"tpl-stk-cli-{sfx}"
    pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
        "id": wp, "provider": "local",
        "config": {"kind": "local", "root_path": str(tmp_path)},
    }))
    pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
        "id": tpl, "description": "stk cli", "provider_id": wp,
        "backend": {"kind": "local"},
    }))
    return pc.run(
        "create", "workspace", "--set", f"template_id={tpl}",
    ).stdout.split("/")[1].split()[0]


def _make_trigger_and_sub(
    pc: Primectl, tmp_path, *, slug: str, agent_id: str, wid: str, payload: str,
) -> str:
    """Create a scheduled trigger (cron fixed far in the future so the scheduler
    never fires it on its own) + an agent_fresh_session subscription. Returns
    the trigger id (the test drives it via ``call trigger fire-now``)."""
    out = pc.run("create", "-f", manifest(tmp_path, f"trig-{slug}", "trigger", {
        "slug": slug, "name": f"Stock news monitor {slug}",
        "config": {"kind": "scheduled", "cron": "0 0 1 1 *",  # 00:00 Jan 1, never
                   "timezone": "UTC", "catchup": "none"},
        "enabled": True,
    })).stdout
    # ``create -f`` echoes ``<name>/<server-id> created``; parse the server id.
    trigger_id = out.split("/", 1)[1].split()[0]

    sub_file = tmp_path / f"sub-{slug}.yaml"
    sub_file.write_text(yaml.safe_dump({
        "config": {"kind": "agent_fresh_session",
                   "agent_id": agent_id, "workspace_id": wid},
        "payload_template": payload,
    }))
    sub_out = pc.run(
        "call", "trigger", "subscriptions", trigger_id, "-f", str(sub_file),
        "-o", "json",
    ).stdout
    assert json.loads(sub_out).get("id"), f"subscription create returned no id: {sub_out!r}"
    return trigger_id


@smk("SMK-COOKBOOK-CLI-11")
def test_stock_monitor_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-stk-{sfx}"))

    pid = f"p-stk-cli-{sfx}"
    mat_agent = f"stock-monitor-mat-cli-{sfx}"
    sil_agent = f"stock-monitor-sil-cli-{sfx}"

    # Two scripted scenarios: material -> alert; silent -> plain text, no call.
    mat_scenario = f"scripted:stk-mat-cli-{sfx}"
    sil_scenario = f"scripted:stk-sil-cli-{sfx}"
    registry.register(mat_scenario, [
        Rule(when_tool_result=False,
             emit_tool="misc__inform_user", emit_args={"message": _ALERT_MESSAGE}),
        Rule(when_tool_result=True, emit_text="Alert sent. Done."),
    ])
    registry.register(sil_scenario, [
        Rule(when_tool_result=False,
             emit_text="Reviewed the news; nothing material today."),
    ])

    mat_trigger: str | None = None
    sil_trigger: str | None = None
    cleanup = [("agent", mat_agent), ("agent", sil_agent), ("llm_provider", pid)]
    try:
        # --- The scripted monitor LLM provider ---------------------------
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [
                {"name": mat_scenario, "context_length": 8192},
                {"name": sil_scenario, "context_length": 8192},
            ],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))

        # --- Two monitor agents (material + silent), web+inform bound ----
        for aid, scenario in ((mat_agent, mat_scenario), (sil_agent, sil_scenario)):
            pc.run("create", "-f", manifest(tmp_path, f"agent-{aid}", "agent", {
                "id": aid,
                "description": "Monitors stock news and alerts a channel on material news.",
                "model": {"provider_id": pid, "model_name": scenario},
                "tools": ["web__web_search", "misc__inform_user"],
                "max_tool_turns": 8,
                "system_prompt": [
                    "You monitor stock news. Judge whether the news in your input "
                    "is materially impactful. If material, call inform_user ONCE "
                    "with a one-line alert; otherwise do not call it. Then stop."
                ],
            }))

        # --- Local workspace ---------------------------------------------
        wid = _make_workspace(pc, tmp_path, sfx)

        # ============================================================
        # MATERIAL path: scheduled fire -> session runs -> alert recorded
        # ============================================================
        mat_trigger = _make_trigger_and_sub(
            pc, tmp_path, slug=f"stk-mat-cli-{sfx}", agent_id=mat_agent, wid=wid,
            payload=(
                "Check these tickers for material news and alert if material: "
                "NVDA. News: a regulator opened an antitrust probe into NVDA's "
                "data-center GPU sales."
            ),
        )
        cleanup.insert(0, ("trigger", mat_trigger))
        mat_transcript = _fire_and_read_transcript(pc, tmp_path, mat_trigger, wid)
        msgs = _inform_user_messages(mat_transcript)
        assert msgs, (
            f"material path did not record a misc__inform_user tool_call: "
            f"{mat_transcript!r}"
        )
        assert _ALERT_MESSAGE in msgs, (
            f"inform_user alert message not found in transcript calls: {msgs!r}"
        )

        # ============================================================
        # SILENT path: fire -> session runs -> NO inform_user (filtering)
        # ============================================================
        sil_trigger = _make_trigger_and_sub(
            pc, tmp_path, slug=f"stk-sil-cli-{sfx}", agent_id=sil_agent, wid=wid,
            payload=(
                "Check these tickers for material news and alert if material: "
                "TSLA. News: a minor blog reposted last quarter's known delivery "
                "figures; nothing new."
            ),
        )
        cleanup.insert(0, ("trigger", sil_trigger))
        sil_transcript = _fire_and_read_transcript(pc, tmp_path, sil_trigger, wid)
        assert "inform_user" not in sil_transcript, (
            f"silent path unexpectedly called inform_user: {sil_transcript!r}"
        )
        assert not _inform_user_messages(sil_transcript)
    finally:
        for res, ident in cleanup:
            pc.run("delete", res, ident, check=False)
