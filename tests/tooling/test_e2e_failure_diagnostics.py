from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "e2e" / "capture_failure_state.py"


def _diagnostics_module():
    spec = importlib.util.spec_from_file_location("capture_failure_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extracts_only_bounded_unique_session_ids() -> None:
    diagnostics = _diagnostics_module()
    transcript = "\n".join([
        "session sess-deadbeef never gave ANSWER-04",
        "'OK-x' never reached the transcript of sess-cafe1234",
        "session sess-deadbeef repeated",
        "not-a-session and sess-'quoted are rejected",
        *(f"session sess-{index:02d} failed" for index in range(30)),
    ])

    ids = diagnostics.extract_session_ids(transcript)

    assert ids[:2] == ["sess-deadbeef", "sess-cafe1234"]
    assert len(ids) == diagnostics.MAX_SESSION_IDS
    assert len(ids) == len(set(ids))
    assert "sess-'quoted" not in ids


def test_redacts_nested_credentials_and_url_userinfo() -> None:
    diagnostics = _diagnostics_module()

    clean = diagnostics.redact({
        "password": "p",
        "nested": {
            "headers": {"Authorization": "Bearer x"},
            "endpoint": "postgresql://primer:secret@db/primer_e2e",
            "safe": "kept",
        },
    })

    assert clean == {
        "password": "[REDACTED]",
        "nested": {
            "headers": "[REDACTED]",
            "endpoint": "postgresql://[REDACTED]@db/primer_e2e",
            "safe": "kept",
        },
    }


def test_failed_psql_capture_names_stage_and_bounds_redacted_stderr(monkeypatch) -> None:
    diagnostics = _diagnostics_module()
    noisy = (
        "x" * (diagnostics.MAX_CAPTURE_STDERR_CHARS + 50)
        + "\ncompose failed PGPASSWORD=do-not-keep "
        "postgresql://primer:secret@db/primer_e2e\n"
    )
    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=noisy,
        ),
    )

    captured = diagnostics._capture_one("docker", "sess-deadbeef")

    assert captured["capture_stage"] == "compose_psql"
    assert captured["capture_error"] == "psql exited 1"
    assert captured["capture_stderr"].startswith("[truncated]\n")
    assert len(captured["capture_stderr"]) <= diagnostics.MAX_CAPTURE_STDERR_CHARS + len("[truncated]\n")
    assert "do-not-keep" not in captured["capture_stderr"]
    assert "secret@" not in captured["capture_stderr"]


def test_state_query_is_session_scoped_and_bounded() -> None:
    diagnostics = _diagnostics_module()
    sql = diagnostics._SESSION_STATE_SQL

    assert "FROM sessions AS s WHERE s.id = :'sid'" in sql
    assert "FROM pendingsessionmessage" in sql
    assert "data->>'session_id' = :'sid'" in sql
    assert "l.kind = 'session' AND l.entity_id = :'sid'" in sql
    assert "FROM eventsubscription" in sql
    assert "data #>> '{sink,session_id}' = :'sid'" in sql
    assert "JOIN event_cursors" in sql
    assert "LIMIT 100" in sql
    assert "LIMIT 200" in sql


def test_e2e_workflow_preserves_exit_status_and_uploads_complete_failure_files() -> None:
    workflow = (ROOT / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    run_at = workflow.index("- name: Run tests/e2e")
    capture_at = workflow.index("- name: Capture failed-session durable state")
    teardown_at = workflow.index("- name: Tear down", run_at)
    upload_at = workflow.index("- name: Upload live-server failure diagnostics")

    assert run_at < capture_at < teardown_at < upload_at
    assert "2>&1 | tee tests/.e2e/pytest-output.txt" in workflow
    assert "pytest_status=${PIPESTATUS[0]}" in workflow
    assert 'exit "$pytest_status"' in workflow
    for path in (
        "tests/.e2e/server.stdout",
        "tests/.e2e/logs/primer.log",
        "tests/.e2e/pytest-output.txt",
        "tests/.e2e/failure-state.json",
    ):
        assert path in workflow[upload_at:]
    assert "retention-days: 7" in workflow[upload_at:]
