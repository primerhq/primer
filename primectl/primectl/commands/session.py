"""Session run/watch + human-in-the-loop respond commands.

The generic ``create session`` / ``get session`` verbs the registry derives
cover plain CRUD. These convenience commands add the two operator workflows the
REST surface supports but the generic verbs cannot express in one shot:

* ``session run`` - create a session bound to an agent or graph, then POLL it
  to a terminal state (the platform has no client stream), rendering turn-log
  progress to the terminal. When the session PARKS on ``ask_user`` or a
  tool-approval, surface the prompt and answer it inline (interactive stdin) or
  from canned ``--answer`` / ``--yes`` flags for scripted runs, looping until
  the session reaches ``ended``.

* ``session respond`` - a non-interactive companion that answers a single
  pending ask_user / tool-approval on an already-parked session. Useful for
  scripting the poll loop yourself and for the recipes' CLI path.

The endpoints these wrap (all confirmed request/response pollable):

* ``POST /v1/workspaces/{wid}/sessions``                    -> create (+start)
* ``GET  /v1/sessions/{sid}``                               -> status / get
* ``GET  /v1/sessions/{sid}/turn_log?since_seq=N``          -> progress events
* ``GET  /v1/sessions/{sid}/ask_user/pending``              -> ask_user prompt
* ``POST /v1/sessions/{sid}/ask_user/respond``              -> ask_user reply
* ``GET  /v1/sessions/{sid}/tool_approval/pending``         -> approval prompt
* ``POST /v1/sessions/{sid}/tool_approval/respond``         -> approval decision

Park discrimination is by PROBE: a parked session is offered to
``ask_user/pending`` first (200 -> ask_user), then ``tool_approval/pending``
(200 -> approval). A pending GET that 404s means the state already moved (or
this is the other kind), so the loop re-polls. This is robust to graph
``tool_call`` ask_user parks, whose outer yield is labelled ``_approval`` but
whose ask_user prompt the ask_user endpoint still serves.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer

from primectl.client import ApiClient, ApiError, ConnectionFailed
from primectl.commands.crud import _fail, _session
from primectl.output import render

session_app = typer.Typer(
    name="session",
    help="Run a session with live polling + inline HITL, or respond to a park.",
    no_args_is_help=True,
)

respond_app = typer.Typer(
    name="respond",
    help="Answer a parked session's pending ask_user / tool-approval.",
    no_args_is_help=True,
)

# Terminal session status: the row will not change again.
_ENDED = "ended"
# A session is awaiting an operator decision in these park states.
_PARKED = ("parked", "resumable")


def _parse_json_arg(value: str | None):
    """Parse a JSON-or-@file argument.

    ``None`` -> ``None``. A leading ``@`` reads the JSON from a local file
    (mirrors the ``-f``/@file convention the CRUD verbs use). Otherwise the
    value is parsed as inline JSON; if that fails it is treated as a bare
    string so ``--answer hello`` works without quoting.
    """
    if value is None:
        return None
    text = value
    if value.startswith("@"):
        text = Path(value[1:]).read_text()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _build_binding(agent: str | None, graph: str | None) -> dict:
    """Build the discriminated-union ``binding`` body for session create."""
    if (agent is None) == (graph is None):
        raise typer.BadParameter("pass exactly one of --agent or --graph")
    if agent is not None:
        return {"kind": "agent", "agent_id": agent}
    return {"kind": "graph", "graph_id": graph}


def _print_new_turn_log(client: ApiClient, session_id: str, since_seq: int) -> int:
    """Print turn-log items newer than ``since_seq``; return the new high seq.

    The turn log is the pollable progress stream (the WebSocket is out of
    scope for the CLI). Any read error is non-fatal: progress rendering is
    best-effort, so we keep the cursor where it was and let the next tick try
    again.
    """
    try:
        resp = client.request(
            "get",
            f"/v1/sessions/{session_id}/turn_log",
            params={"since_seq": since_seq},
        )
    except (ApiError, ConnectionFailed):
        return since_seq
    high = since_seq
    for item in resp.json().get("items", []):
        seq = item.get("seq")
        if isinstance(seq, int):
            high = max(high, seq)
        label = item.get("kind") or item.get("type") or "event"
        text = item.get("message") or item.get("text") or ""
        line = f"  [{label}]" + (f" {text}" if text else "")
        typer.echo(line)
    return high


def _pending(client: ApiClient, session_id: str, kind: str) -> dict | None:
    """GET the pending ask_user / tool_approval prompt, or None on 404.

    ``kind`` is ``"ask_user"`` or ``"tool_approval"``. A 404 means the
    session is not parked on that kind (or the state raced); the caller
    treats it as "not this one" and falls through / re-polls.
    """
    try:
        resp = client.request("get", f"/v1/sessions/{session_id}/{kind}/pending")
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise
    return resp.json()


def _answer_ask_user(
    client: ApiClient,
    session_id: str,
    pending: dict,
    *,
    canned,
    has_canned: bool,
) -> bool:
    """Resolve a pending ask_user park (interactive prompt or canned answer).

    Returns True if the answer was submitted, False if the park raced away (a
    404 on respond — the resume already advanced past it; benign).
    """
    tcid = pending.get("tool_call_id", "")
    prompt = pending.get("prompt", "")
    typer.echo(f"  ask_user: {prompt}")
    if has_canned:
        response = canned
    else:
        response = typer.prompt("  your answer")
    try:
        client.request(
            "post",
            f"/v1/sessions/{session_id}/ask_user/respond",
            json={"tool_call_id": tcid, "response": response},
        )
    except ApiError as exc:
        if exc.status == 404:
            return False
        raise
    typer.echo("  -> answer submitted")
    return True


def _answer_tool_approval(
    client: ApiClient,
    session_id: str,
    pending: dict,
    *,
    auto_yes: bool,
) -> bool:
    """Resolve a pending tool-approval park (interactive y/n or auto-approve).

    Returns True if the decision was submitted, False if the park raced away
    (a 404 on respond — already resolved; benign).
    """
    tcid = pending.get("tool_call_id", "")
    tool_name = pending.get("tool_name", "")
    args = pending.get("arguments") or {}
    reason = pending.get("gate_reason") or ""
    typer.echo(f"  tool_approval: {tool_name}({json.dumps(args)})")
    if reason:
        typer.echo(f"    reason: {reason}")
    if auto_yes:
        decision = "approved"
    else:
        decision = "approved" if typer.confirm("  approve?") else "rejected"
    try:
        client.request(
            "post",
            f"/v1/sessions/{session_id}/tool_approval/respond",
            json={"tool_call_id": tcid, "decision": decision},
        )
    except ApiError as exc:
        if exc.status == 404:
            return False
        raise
    typer.echo(f"  -> {decision}")
    return True


def _handle_park(
    client: ApiClient,
    session_id: str,
    *,
    canned_answer,
    has_canned_answer: bool,
    auto_yes: bool,
) -> bool:
    """Answer whatever the session is parked on. Return True if handled.

    Probe ask_user first (a graph tool_call ask_user park labels its outer
    yield ``_approval`` but is still served here), then tool_approval. A 404 on
    both means the park raced away; return False so the caller re-polls.

    Re-answering a still-parked session on each poll is intentional: respond is
    async (202 + a later worker resume), so a re-submit re-publishes the answer
    and retries a resume that was slow or dropped. The respond helpers treat a
    404 (the park already advanced) as benign rather than an error, so the
    re-submit can never surface a spurious CLI failure.
    """
    ask = _pending(client, session_id, "ask_user")
    if ask is not None:
        _answer_ask_user(
            client, session_id, ask,
            canned=canned_answer, has_canned=has_canned_answer,
        )
        return True
    approval = _pending(client, session_id, "tool_approval")
    if approval is not None:
        _answer_tool_approval(client, session_id, approval, auto_yes=auto_yes)
        return True
    return False


@session_app.command("run")
def run(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(..., help="Workspace id to run the session in."),
    agent: str = typer.Option(
        None, "--agent", help="Bind the session to this agent id."
    ),
    graph: str = typer.Option(
        None, "--graph", help="Bind the session to this graph id."
    ),
    instructions: str = typer.Option(
        None, "-i", "--instructions", help="Initial instructions for an agent binding."
    ),
    graph_input: str = typer.Option(
        None, "--graph-input", help="Graph input as inline JSON or @file."
    ),
    watch: bool = typer.Option(
        True, "--watch/--no-watch",
        help="Poll to terminal with inline HITL (default); --no-watch just starts it.",
    ),
    poll_interval: float = typer.Option(
        1.5, "--poll-interval", help="Seconds between status polls."
    ),
    timeout: float = typer.Option(
        None, "--timeout", help="Give up after this many seconds of watching."
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Auto-approve every tool-approval park (non-interactive)."
    ),
    answer: str = typer.Option(
        None, "--answer",
        help="Canned ask_user answer (inline JSON or @file) for non-interactive runs.",
    ),
    output: str = typer.Option(
        None, "-o", "--output", help="Output: table|json|yaml|name|wide."
    ),
) -> None:
    """Start an agent/graph session and (by default) watch it to terminal.

    Creates the session with ``auto_start`` then polls ``GET /sessions/{id}``
    on ``--poll-interval``. When it parks on ask_user or a tool-approval the
    prompt is surfaced and answered inline (or from ``--answer``/``--yes`` in
    scripted runs); the loop continues until ``status == "ended"``.
    """
    sess = _session(ctx)
    if output is not None:
        sess.output = output
    try:
        binding = _build_binding(agent, graph)
    except typer.BadParameter as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    body: dict = {"binding": binding, "auto_start": True}
    if instructions is not None:
        body["initial_instructions"] = instructions
    if graph_input is not None:
        body["graph_input"] = _parse_json_arg(graph_input)

    try:
        resp = sess.client.request(
            "post", f"/v1/workspaces/{workspace_id}/sessions", json=body
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    created = resp.json()
    session_id = created.get("id", "")
    typer.echo(f"session/{session_id} started")

    if not watch:
        fmt = sess.output if sess.output in ("json", "yaml") else "yaml"
        typer.echo(render(created, fmt=fmt))
        return

    has_answer = answer is not None
    canned = _parse_json_arg(answer) if has_answer else None
    _watch_to_terminal(
        sess.client,
        session_id,
        poll_interval=poll_interval,
        timeout=timeout,
        canned_answer=canned,
        has_canned_answer=has_answer,
        auto_yes=yes,
        on_fail=lambda exc: _fail(sess, exc),
    )


def _watch_to_terminal(
    client: ApiClient,
    session_id: str,
    *,
    poll_interval: float,
    timeout: float | None,
    canned_answer,
    has_canned_answer: bool,
    auto_yes: bool,
    on_fail,
) -> None:
    """Poll a session to ``ended``, rendering progress + answering parks.

    The single poll loop: read status; print any new turn-log lines; if parked
    answer the prompt; if ended print the reason and stop; otherwise sleep and
    repeat. ``timeout`` (when set) bounds the total watch time.
    """
    started = time.monotonic()
    last_seq = 0
    last_status: str | None = None
    while True:
        if timeout is not None and (time.monotonic() - started) > timeout:
            typer.echo(
                f"timed out after {timeout:g}s watching session {session_id}",
                err=True,
            )
            raise typer.Exit(1)
        try:
            row = client.request("get", f"/v1/sessions/{session_id}").json()
        except (ApiError, ConnectionFailed) as exc:
            on_fail(exc)
            return

        status = row.get("status")
        if status != last_status:
            typer.echo(f"status: {status}")
            last_status = status

        last_seq = _print_new_turn_log(client, session_id, last_seq)

        if status == _ENDED:
            reason = row.get("ended_reason") or "?"
            detail = row.get("ended_detail")
            suffix = f" ({detail})" if detail else ""
            typer.echo(f"ended: {reason}{suffix}")
            return

        if row.get("parked_status") in _PARKED:
            try:
                handled = _handle_park(
                    client, session_id,
                    canned_answer=canned_answer,
                    has_canned_answer=has_canned_answer,
                    auto_yes=auto_yes,
                )
            except (ApiError, ConnectionFailed) as exc:
                on_fail(exc)
                return
            if handled:
                # Answered: skip the sleep so the resumed turn is observed
                # promptly on the next poll.
                continue

        time.sleep(poll_interval)


@respond_app.command("ask-user")
def respond_ask_user(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Parked session id."),
    response: str = typer.Option(
        ..., "--response", help="The answer as inline JSON or @file."
    ),
) -> None:
    """Answer a parked session's pending ask_user prompt (one-shot)."""
    sess = _session(ctx)
    try:
        pending = sess.client.request(
            "get", f"/v1/sessions/{session_id}/ask_user/pending"
        ).json()
        sess.client.request(
            "post",
            f"/v1/sessions/{session_id}/ask_user/respond",
            json={
                "tool_call_id": pending.get("tool_call_id", ""),
                "response": _parse_json_arg(response),
            },
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    typer.echo(f"session {session_id} ask_user answered")


@respond_app.command("tool-approval")
def respond_tool_approval(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Parked session id."),
    decision: str = typer.Option(
        ..., "--decision", help="approved or rejected."
    ),
    reason: str = typer.Option(
        None, "--reason", help="Optional free-text reason surfaced to the agent."
    ),
) -> None:
    """Decide a parked session's pending tool-approval (one-shot)."""
    sess = _session(ctx)
    if decision not in ("approved", "rejected"):
        typer.echo("--decision must be approved or rejected", err=True)
        raise typer.Exit(1)
    try:
        pending = sess.client.request(
            "get", f"/v1/sessions/{session_id}/tool_approval/pending"
        ).json()
        body: dict = {
            "tool_call_id": pending.get("tool_call_id", ""),
            "decision": decision,
        }
        if reason is not None:
            body["reason"] = reason
        sess.client.request(
            "post",
            f"/v1/sessions/{session_id}/tool_approval/respond",
            json=body,
        )
    except (ApiError, ConnectionFailed) as exc:
        _fail(sess, exc)
        return
    typer.echo(f"session {session_id} tool_approval {decision}")


def register(app: typer.Typer) -> None:
    session_app.add_typer(respond_app, name="respond")
    app.add_typer(session_app, name="session")
