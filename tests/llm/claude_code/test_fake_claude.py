"""The claude CLI double's own surfaces: fixture selection and the record.

The double's stream replay is exercised end-to-end through the adapter (the
``test_adapter`` tests); these pin the parts the adapter does not see: which
fixture a call replays (``FAKE_CLAUDE_FIXTURE`` vs ``FAKE_CLAUDE_FIXTURE_SEQ``)
and that the single-fixture contract is untouched by the list.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from tests.llm.claude_code.fake_claude import main


def _run_stream(
    monkeypatch, tmp_path: Path, *, seq: str | None = None, fixture: str | None = None
) -> str:
    """One stream invocation through :func:`main`; returns its stdout."""
    record = tmp_path / "record.json"
    monkeypatch.setattr(sys, "argv", ["fake-claude"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("the user line"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.delenv("FAKE_CLAUDE_FIXTURE", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_FIXTURE_SEQ", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    if fixture is not None:
        monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", fixture)
    if seq is not None:
        monkeypatch.setenv("FAKE_CLAUDE_FIXTURE_SEQ", seq)
    main()
    return out.getvalue()


def test_fixture_seq_consumes_one_per_invocation(tmp_path: Path, monkeypatch) -> None:
    """Each stream call takes the next fixture; the count survives between calls."""
    fx1, fx2 = tmp_path / "a.ndjson", tmp_path / "b.ndjson"
    fx1.write_text('{"n": 1}\n', encoding="utf-8")
    fx2.write_text('{"n": 2}\n', encoding="utf-8")
    assert _run_stream(monkeypatch, tmp_path, seq=f"{fx1},{fx2}") == '{"n": 1}\n'
    assert _run_stream(monkeypatch, tmp_path, seq=f"{fx1},{fx2}") == '{"n": 2}\n'
    assert (tmp_path / "record.json.count").read_text(encoding="utf-8") == "2"


def test_fixture_seq_replays_the_last_fixture_past_the_end(tmp_path: Path, monkeypatch) -> None:
    fx1, fx2 = tmp_path / "a.ndjson", tmp_path / "b.ndjson"
    fx1.write_text('{"n": 1}\n', encoding="utf-8")
    fx2.write_text('{"n": 2}\n', encoding="utf-8")
    seq = f"{fx1},{fx2}"
    assert _run_stream(monkeypatch, tmp_path, seq=seq) == '{"n": 1}\n'
    assert _run_stream(monkeypatch, tmp_path, seq=seq) == '{"n": 2}\n'
    assert _run_stream(monkeypatch, tmp_path, seq=seq) == '{"n": 2}\n'
    assert (tmp_path / "record.json.count").read_text(encoding="utf-8") == "3"


def test_single_fixture_contract_unchanged(tmp_path: Path, monkeypatch) -> None:
    """Without the list, every call replays FAKE_CLAUDE_FIXTURE and leaves no counter."""
    fx = tmp_path / "a.ndjson"
    fx.write_text('{"n": 1}\n', encoding="utf-8")
    assert _run_stream(monkeypatch, tmp_path, fixture=str(fx)) == '{"n": 1}\n'
    assert _run_stream(monkeypatch, tmp_path, fixture=str(fx)) == '{"n": 1}\n'
    assert not (tmp_path / "record.json.count").exists()
