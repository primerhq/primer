"""Workspace tap — SSE stream consumer for ``primectl tap``.

Implements ``primectl tap <workspace_id>`` which opens the read-only
``GET /v1/workspaces/{wid}/tap`` Server-Sent Events stream, parses the
frames, and prints each event's JSON payload to stdout.

Selector construction
---------------------
``--event-class`` (repeatable) builds an ``events`` predicate::

    class IN [<values>]

``--session`` (repeatable) builds a ``sessions`` predicate::

    id IN [<ids>]

``--selector-json`` accepts a raw TapSelector JSON blob that wins over the
flag-built selector when both are supplied.

Auth
----
The bearer token injected by :class:`~primectl.client.ApiClient` is reused
here via direct access to the client's internal httpx instance.  No new auth
mechanism is introduced.

SSE streaming
-------------
Uses ``httpx`` in streaming mode (``client.stream("GET", path, ...)``) rather
than a separate library.  Frames are accumulated line-by-line; a blank line
flushes the current frame.  ``: keepalive`` comment lines are silently
skipped.
"""

from __future__ import annotations

import json

import typer

from primectl.commands.crud import _session
from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.tap.selector import TapSelector


def _build_selector(
    *,
    event_classes: list[str],
    session_ids: list[str],
    selector_json: str | None,
) -> str | None:
    """Return the ``?selector=`` query-parameter value (raw JSON or None).

    ``--selector-json`` wins unconditionally when supplied.  Otherwise, if
    either ``--event-class`` or ``--session`` flags were provided, a
    :class:`~primer.tap.selector.TapSelector` is built from them and
    serialised to JSON.  When no flags are set, ``None`` is returned so the
    query parameter is omitted entirely (match-all).
    """
    if selector_json is not None:
        # Validate that it parses as a TapSelector so we get a friendly error.
        try:
            TapSelector.model_validate_json(selector_json)
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"--selector-json is not valid TapSelector JSON: {exc}", err=True)
            raise typer.Exit(1) from exc
        return selector_json

    events_pred: Predicate | None = None
    if event_classes:
        events_pred = Predicate(
            left=FieldRef(name="class"),
            op=Op.IN,
            right=Value(value=list(event_classes)),
        )

    sessions_pred: Predicate | None = None
    if session_ids:
        sessions_pred = Predicate(
            left=FieldRef(name="id"),
            op=Op.IN,
            right=Value(value=list(session_ids)),
        )

    if events_pred is None and sessions_pred is None:
        return None

    selector = TapSelector(events=events_pred, sessions=sessions_pred)
    return selector.model_dump_json(exclude_none=True)


def _parse_sse_frames(lines: list[str]) -> tuple[str | None, str | None]:
    """Parse a buffered SSE frame (lines up to a blank line).

    Returns ``(event_id, data)`` where either may be ``None`` if the field
    was absent.
    """
    event_id: str | None = None
    data_parts: list[str] = []
    for line in lines:
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].strip())
    data = "\n".join(data_parts) if data_parts else None
    return event_id, data


def register(app: typer.Typer) -> None:
    """Register the ``tap`` command directly on *app*."""

    @app.command("tap")
    def tap(
        ctx: typer.Context,
        workspace_id: str = typer.Argument(..., help="Workspace id to tap."),
        event_class: list[str] | None = typer.Option(
            None,
            "--event-class",
            help=(
                "Filter by event class (repeatable). Builds "
                "``events: class IN [<values>]``."
            ),
        ),
        session: list[str] | None = typer.Option(
            None,
            "--session",
            help=(
                "Filter by session id (repeatable). Builds "
                "``sessions: id IN [<ids>]``."
            ),
        ),
        selector_json: str | None = typer.Option(
            None,
            "--selector-json",
            help=(
                "Raw TapSelector JSON escape hatch. Wins over "
                "--event-class / --session when supplied."
            ),
        ),
        cursor: str | None = typer.Option(
            None,
            "--cursor",
            help="Resume from an opaque cursor token (sent as ?cursor=).",
        ),
        pretty: bool = typer.Option(
            False,
            "--pretty",
            help="Pretty-print each event's JSON (default: compact, one per line).",
        ),
    ) -> None:
        """Stream workspace SSE tap events to stdout.

        Opens ``GET /v1/workspaces/{workspace_id}/tap`` and prints each
        event's JSON payload — one compact line per event by default, or
        pretty-printed with ``--pretty``.  Press Ctrl-C to stop; the last
        cursor is printed to stderr so you can resume with ``--cursor``.

        Selector precedence: ``--selector-json`` wins over flag-built
        predicates when both are given.
        """
        sess = _session(ctx)

        selector_val = _build_selector(
            event_classes=event_class or [],
            session_ids=session or [],
            selector_json=selector_json,
        )

        # Build the query parameters.
        params: dict[str, str] = {}
        if selector_val is not None:
            params["selector"] = selector_val
        if cursor is not None:
            params["cursor"] = cursor

        # Stream via the ApiClient's public stream() so the base URL and
        # Authorization header (Bearer token) match every other primectl
        # command — no hardcoded credentials, no reach into internals.
        path = f"/v1/workspaces/{workspace_id}/tap"

        last_cursor: str | None = None

        try:
            with sess.client.stream("GET", path, params=params) as response:
                if response.status_code >= 400:
                    body = response.read()
                    typer.echo(
                        f"HTTP {response.status_code}: "
                        f"{body.decode('utf-8', errors='replace')}",
                        err=True,
                    )
                    raise typer.Exit(1)

                frame_lines: list[str] = []
                for raw_line in response.iter_lines():
                    line = raw_line  # already str from httpx

                    # Blank line -> flush current frame.
                    if not line:
                        if frame_lines:
                            # Skip pure-comment frames (e.g. ": keepalive").
                            is_comment_frame = all(
                                ln.startswith(":") for ln in frame_lines
                            )
                            if not is_comment_frame:
                                event_id, data = _parse_sse_frames(frame_lines)
                                if event_id is not None:
                                    last_cursor = event_id
                                if data is not None:
                                    if pretty:
                                        try:
                                            parsed = json.loads(data)
                                            typer.echo(
                                                json.dumps(parsed, indent=2)
                                            )
                                        except (json.JSONDecodeError, ValueError):
                                            typer.echo(data)
                                    else:
                                        typer.echo(data)
                            frame_lines = []
                        continue

                    frame_lines.append(line)

        except KeyboardInterrupt:
            pass

        if last_cursor is not None:
            typer.echo(f"resume cursor: {last_cursor}", err=True)
