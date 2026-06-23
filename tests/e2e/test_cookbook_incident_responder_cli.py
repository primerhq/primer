"""Cookbook recipe (CLI path): webhook incident responder, driven by primectl.

The ``primectl``-driven sibling of ``test_cookbook_incident_responder``. Every
setup step is the exact ``primectl`` command the rewritten doc shows:

  * ``create -f`` the scripted LLM provider + the ``incident-responder`` agent
    (``misc__inform_user`` bound);
  * ``create -f`` / ``--set`` the local workspace;
  * ``create -f`` the WEBHOOK trigger, then ``get trigger -o json -r`` to read
    its minted ``config.token``, and ``call trigger subscriptions`` to bind an
    ``agent_fresh_session`` subscription whose ``payload_template`` renders the
    raw ``{{ webhook_body }}``; and
  * ``raw POST /v1/webhooks/<token>`` to simulate the monitoring system's alert.

The webhook is a PUBLIC, token-authenticated endpoint with no first-class
console button or ``primectl`` verb, so the doc and this test post to it with
the ``primectl raw`` escape hatch (the one API-shaped call in the recipe, and
exactly what the doc flags). The dispatch is fire-and-forget (the endpoint
returns 202 immediately), so the test polls the workspace's sessions for the
one tagged with this trigger's id, then polls that session to terminal.

The success outcome asserted is the API test's: the webhook returns 202 +
``{"status": "accepted"}``, fires the subscription, and the fresh session is
CREATED, CLAIMED, and RUNS TO TERMINAL ``completed`` (the dispatch regression
guard). The rendered ``{{ webhook_body }}`` is in the session instructions and
the agent records a ``misc__inform_user`` triage in the transcript.

HERMETIC delivery: with no channel bound, ``inform_user`` degrades to
``delivered_to: 0`` but the tool_call is still RECORDED, so we assert on the
transcript rather than a live channel post. The recipe's live
``delivered_to: 1`` Discord round-trip is the manual coverage for the transport.

The responder agent is SCRIPTED via the shared in-process ``mock_llm``; the
webhook -> fresh-session dispatch + execution platform paths are REAL. Not
capability-gated.

Recipe: primerhq.github.io/docs_source/cookbook/incident-responder.md

Run with:
    PRIMER_RUN_E2E=1 uv run pytest \
        tests/e2e/test_cookbook_incident_responder_cli.py -n0 -q
"""
from __future__ import annotations

import json
import time

import yaml

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk


_TRIAGE_MESSAGE = (
    "payments-api: SEV1, 5xx 40% / p99 8s in us-east. Likely cause: a bad "
    "deploy or an overloaded dependency. First step: roll back the last "
    "release and check the DB connection pool."
)

_ALERT_BODY = {
    "service": "payments-api",
    "error": "5xx error rate 40%, p99 latency 8s",
    "region": "us-east",
}


def _files_get(pc: Primectl, wid: str, rel: str) -> str:
    return pc.run("workspace", "files", "get", wid, rel, "--content").stdout


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


def _find_dispatched_session(
    pc: Primectl, *, wid: str, trigger_id: str,
    attempts: int = 40, delay_s: float = 0.5,
) -> str | None:
    """Poll the workspace's sessions for the one this webhook dispatched.

    This is the "Sessions page filtered to the bound workspace" step: the
    session list takes a ``workspace_id`` query filter, but the CLI ``get``
    list exposes no arbitrary query param (and ``--filter`` POSTs to ``/find``,
    which 405s for sessions), so we read the filtered list via the ``primectl
    raw`` escape hatch. The fresh per-test workspace has exactly the one
    dispatched session, tagged with ``metadata.trigger_id``.
    """
    for _ in range(attempts):
        body = json.loads(pc.run(
            "raw", "GET", "/v1/sessions", "--param", f"workspace_id={wid}", "-o", "json",
        ).stdout)
        items = body.get("items", body) if isinstance(body, dict) else body
        for s in items:
            if (s.get("metadata") or {}).get("trigger_id") == trigger_id:
                return s["id"]
        time.sleep(delay_s)
    return None


def _poll_session(pc: Primectl, sid: str, *, timeout_s: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while True:
        row = json.loads(pc.run("get", "session", sid, "-o", "json", "-r").stdout)
        if row.get("status") == "ended":
            return row
        if time.monotonic() > deadline:
            raise AssertionError(f"session {sid} did not end within {timeout_s}s: {row}")
        time.sleep(2.0)


@smk("SMK-COOKBOOK-CLI-12")
def test_incident_responder_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-inc-{sfx}"))

    pid = f"p-inc-cli-{sfx}"
    aid = f"incident-responder-cli-{sfx}"
    wp = f"wp-inc-cli-{sfx}"
    tpl = f"tpl-inc-cli-{sfx}"
    slug = f"incident-cli-{sfx}"

    scenario = f"scripted:inc-cli-{sfx}"
    registry.register(scenario, [
        Rule(when_tool_result=False,
             emit_tool="misc__inform_user", emit_args={"message": _TRIAGE_MESSAGE}),
        Rule(when_tool_result=True, emit_text="Triage posted. Done."),
    ])

    trigger_id: str | None = None
    cleanup = [("agent", aid), ("llm_provider", pid)]
    try:
        # --- The scripted responder LLM provider + agent -----------------
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [{"name": scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))
        pc.run("create", "-f", manifest(tmp_path, "agent", "agent", {
            "id": aid,
            "description": "Triages incidents from alerts and notifies the channel.",
            "model": {"provider_id": pid, "model_name": scenario},
            "tools": ["misc__inform_user"],
            "max_tool_turns": 5,
            "system_prompt": [
                "You are an incident responder. An alert payload is in your "
                "input. Triage it, then call inform_user ONCE with a concise "
                "summary (service, severity, likely cause, first step). Stop."
            ],
        }))

        # --- Local workspace ---------------------------------------------
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "inc cli", "provider_id": wp,
            "backend": {"kind": "local"},
        }))
        wid = pc.run(
            "create", "workspace", "--set", f"template_id={tpl}",
        ).stdout.split("/")[1].split()[0]

        # --- The webhook trigger (create -f) -> read its minted token -----
        out = pc.run("create", "-f", manifest(tmp_path, "trig", "trigger", {
            "slug": slug, "name": f"Incident webhook {sfx}",
            "config": {"kind": "webhook"}, "enabled": True,
        })).stdout
        # ``create -f`` echoes ``<name>/<server-id> created``; parse the server id.
        trigger_id = out.split("/", 1)[1].split()[0]
        cleanup.insert(0, ("trigger", trigger_id))

        trig = json.loads(pc.run("get", "trigger", trigger_id, "-o", "json", "-r").stdout)
        token = (trig.get("config") or {}).get("token")
        assert token, f"webhook trigger carries no config.token: {trig}"

        # --- The agent_fresh_session subscription rendering webhook_body --
        sub_file = tmp_path / "sub.yaml"
        sub_file.write_text(yaml.safe_dump({
            "config": {"kind": "agent_fresh_session",
                       "agent_id": aid, "workspace_id": wid},
            "payload_template": (
                "Incident alert received: {{ webhook_body }}. Triage it and "
                "post a summary."
            ),
        }))
        sub = json.loads(pc.run(
            "call", "trigger", "subscriptions", trigger_id, "-f", str(sub_file),
            "-o", "json",
        ).stdout)
        assert sub.get("id"), f"subscription create returned no id: {sub!r}"

        # --- POST a real alert to the PUBLIC webhook (raw escape hatch) ---
        # The webhook is unauthenticated except for the token and has no
        # first-class verb, so we hit it with ``primectl raw`` (what the doc
        # flags). The raw body becomes ``webhook_body`` in the template.
        alert_file = tmp_path / "alert.json"
        alert_file.write_text(json.dumps(_ALERT_BODY))
        fire = json.loads(pc.run(
            "raw", "POST", f"/v1/webhooks/{token}", "-f", str(alert_file), "-o", "json",
        ).stdout)
        assert fire.get("status") == "accepted", fire
        assert fire.get("delivery_id"), fire

        # --- The dispatch is fire-and-forget; find the fired session ------
        sid = _find_dispatched_session(pc, wid=wid, trigger_id=trigger_id)
        assert sid is not None, (
            "webhook fired but no agent_fresh_session was dispatched into the "
            "workspace (the dispatch path is dead)"
        )

        # REGRESSION GUARD: the fired session must RUN TO TERMINAL completed,
        # not hang or strand with no transcript.
        final = _poll_session(pc, sid, timeout_s=120)
        assert final.get("ended_reason") == "completed", (
            f"webhook-fired session did not run to completion: {final}"
        )

        transcript = _files_get(pc, wid, f".state/sessions/{sid}/messages.jsonl")
        # The webhook body was rendered into the instructions.
        assert "payments-api" in transcript, (
            "webhook_body was not rendered into the session instructions"
        )
        informs = _inform_user_messages(transcript)
        assert informs, (
            f"responder did not record a misc__inform_user triage: {transcript!r}"
        )
        assert _TRIAGE_MESSAGE in informs, (
            f"triage summary not found in inform_user calls: {informs!r}"
        )
    finally:
        for res, ident in cleanup:
            pc.run("delete", res, ident, check=False)
