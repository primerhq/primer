#!/usr/bin/env python3
"""Capture bounded E2E database state for sessions named by pytest output.

The workflow runs this only after a failed live-server E2E invocation and
before teardown. It talks to the already-running compose Postgres container,
never starts a service, and never emits the database password or connection
string. Persisted JSON is recursively redacted before it is written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_SESSION_IDS = 20
MAX_CAPTURE_STDERR_CHARS = 2000
SESSION_ID_RE = re.compile(r"\bsess-[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\b")
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "env",
    "header",
    "password",
    "secret",
    "token",
)
_CREDENTIAL_URL_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)
_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pgpassword|token|secret|authorization)\s*([=:])\s*([^\s,;]+)"
)

_SESSION_STATE_SQL = r"""
WITH relevant_subscriptions AS (
    SELECT id, data, created_at, updated_at
    FROM eventsubscription
    WHERE data #>> '{sink,session_id}' = :'sid'
    ORDER BY id
    LIMIT 100
),
recent_events AS (
    SELECT e.*
    FROM events AS e
    WHERE e.session_id = :'sid'
       OR e.entity_id = :'sid'
       OR e.payload->>'session_id' = :'sid'
       OR EXISTS (
            SELECT 1
            FROM relevant_subscriptions AS s
            JOIN event_cursors AS c ON c.subscriber_id = s.id
            WHERE e.id BETWEEN GREATEST(c.cursor - 20, 0) AND c.cursor + 20
       )
    ORDER BY e.id DESC
    LIMIT 200
)
SELECT jsonb_build_object(
    'session_id', :'sid',
    'session', (
        SELECT to_jsonb(s) FROM sessions AS s WHERE s.id = :'sid'
    ),
    'pending_session_messages', COALESCE((
        SELECT jsonb_agg(to_jsonb(p) ORDER BY p.created_at, p.id)
        FROM (
            SELECT * FROM pendingsessionmessage
            WHERE data->>'session_id' = :'sid'
            ORDER BY created_at, id
            LIMIT 100
        ) AS p
    ), '[]'::jsonb),
    'session_lease', (
        SELECT to_jsonb(l) FROM leases AS l
        WHERE l.kind = 'session' AND l.entity_id = :'sid'
    ),
    'events', COALESCE((
        SELECT jsonb_agg(to_jsonb(e) ORDER BY e.id) FROM recent_events AS e
    ), '[]'::jsonb),
    'event_subscriptions', COALESCE((
        SELECT jsonb_agg(to_jsonb(s) ORDER BY s.id)
        FROM relevant_subscriptions AS s
    ), '[]'::jsonb),
    'event_cursors', COALESCE((
        SELECT jsonb_agg(to_jsonb(c) ORDER BY c.subscriber_id)
        FROM event_cursors AS c
        JOIN relevant_subscriptions AS s ON s.id = c.subscriber_id
    ), '[]'::jsonb)
);
"""


def extract_session_ids(text: str) -> list[str]:
    """Return stable, unique session ids from a pytest transcript."""
    return list(dict.fromkeys(SESSION_ID_RE.findall(text)))[:MAX_SESSION_IDS]


def redact(value: Any) -> Any:
    """Remove credential-bearing keys and user-info from credential URLs."""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                clean[str(key)] = "[REDACTED]"
            else:
                clean[str(key)] = redact(item)
        return clean
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _CREDENTIAL_URL_RE.sub(r"\1[REDACTED]@", value)
    return value


def bounded_redacted_stderr(stderr: str) -> str:
    """Return a bounded diagnostic tail with obvious credentials removed."""
    clean = _CREDENTIAL_URL_RE.sub(r"\1[REDACTED]@", stderr)
    clean = _DIAGNOSTIC_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        clean,
    )
    if len(clean) <= MAX_CAPTURE_STDERR_CHARS:
        return clean
    return "[truncated]\n" + clean[-MAX_CAPTURE_STDERR_CHARS:]


def _runtime() -> str | None:
    requested = os.environ.get("PRIMER_E2E_CONTAINER_RUNTIME")
    if requested:
        return requested if requested in {"docker", "podman"} and shutil.which(requested) else None
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    return None


def _capture_one(runtime: str, session_id: str) -> dict[str, Any]:
    user = os.environ.get("PRIMER_DB_USER", "primer")
    password = os.environ.get("PRIMER_DB_PASSWORD", "primer")
    command = [
        runtime,
        "compose",
        "exec",
        "-T",
        "-e",
        f"PGPASSWORD={password}",
        "postgres",
        "psql",
        "-X",
        "-q",
        "-t",
        "-A",
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"sid={session_id}",
        "-U",
        user,
        "-d",
        "primer_e2e",
        "-c",
        _SESSION_STATE_SQL,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        return {
            "session_id": session_id,
            "capture_stage": "compose_psql",
            "capture_error": f"psql exited {completed.returncode}",
            "capture_stderr": bounded_redacted_stderr(completed.stderr),
        }
    try:
        return redact(json.loads(completed.stdout))
    except json.JSONDecodeError:
        return {
            "session_id": session_id,
            "capture_stage": "decode_psql_json",
            "capture_error": "psql returned non-JSON output",
            "capture_stderr": bounded_redacted_stderr(completed.stderr),
        }


def capture(pytest_output: Path) -> dict[str, Any]:
    """Build the bounded diagnostic document without leaking connection data."""
    try:
        transcript = pytest_output.read_text(encoding="utf-8", errors="replace")
    except OSError:
        transcript = ""
    session_ids = extract_session_ids(transcript)
    result: dict[str, Any] = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "pytest_output": str(pytest_output),
        "session_ids": session_ids,
        "sessions": [],
    }
    if not session_ids:
        result["capture_note"] = "pytest output named no sess-* session ids"
        return result
    runtime = _runtime()
    if runtime is None:
        result["capture_error"] = "docker or podman was not available"
        return result
    result["sessions"] = [_capture_one(runtime, session_id) for session_id in session_ids]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = capture(args.pytest_output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
